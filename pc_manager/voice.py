"""Claude voice assistant — STT, Claude Code subprocess (with MCP tools), TTS.

Uses the local `claude` CLI via `-p` (print) mode. MCP servers registered with
Claude Code (e.g. pc-manager) are available, so the model can invoke them when
the user asks PC management questions by voice.

No Anthropic API key required — uses your Claude Code subscription.
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Callable

import speech_recognition as sr


# Hide every console window the Claude Agent SDK spawns. The SDK uses
# `anyio.open_process` which on Windows falls through to
# `asyncio.create_subprocess_exec`. anyio doesn't expose `creationflags`, so we
# patch the asyncio call site to always add CREATE_NO_WINDOW. Done at import
# time, before the SDK is loaded.
if sys.platform == "win32":
    _CREATE_NO_WINDOW = 0x08000000
    _orig_create_subprocess_exec = asyncio.create_subprocess_exec

    async def _windowless_create_subprocess_exec(*args, **kwargs):
        kwargs["creationflags"] = (
            kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
        )
        return await _orig_create_subprocess_exec(*args, **kwargs)

    asyncio.create_subprocess_exec = _windowless_create_subprocess_exec  # type: ignore[assignment]

    _orig_create_subprocess_shell = asyncio.create_subprocess_shell

    async def _windowless_create_subprocess_shell(*args, **kwargs):
        kwargs["creationflags"] = (
            kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
        )
        return await _orig_create_subprocess_shell(*args, **kwargs)

    asyncio.create_subprocess_shell = _windowless_create_subprocess_shell  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Locate Claude Code CLI (npm-installed on Windows is usually claude.cmd)
# ---------------------------------------------------------------------------
def _find_claude() -> str | None:
    for name in ("claude", "claude.cmd", "claude.exe"):
        path = shutil.which(name)
        if path:
            return path
    return None


CLAUDE_CMD = _find_claude()


def has_claude_cli() -> bool:
    return CLAUDE_CMD is not None


# ---------------------------------------------------------------------------
# edge-tts → MP3 → Windows MCI playback (no extra audio dependency)
# ---------------------------------------------------------------------------
_mci = ctypes.windll.winmm.mciSendStringW
_mci.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_uint, wintypes.HANDLE]
_mci.restype = ctypes.c_uint

_alias_counter = 0
_alias_lock = threading.Lock()
# Global MCI lock — Windows MCI is a shared, not-quite-thread-safe service.
# Serializing every command keeps `open` / `play` / `status` / `close` atomic.
_mci_lock = threading.Lock()


def _next_alias() -> str:
    global _alias_counter
    with _alias_lock:
        _alias_counter += 1
        return f"pcm_tts_{_alias_counter}"


def _mci_send(cmd: str, ret_buf: ctypes.Array | None = None) -> int:
    """Thread-safe wrapper for mciSendStringW. Returns the MCI error code."""
    with _mci_lock:
        return _mci(cmd, ret_buf, 64 if ret_buf is not None else 0, None)


def _play_mp3_blocking(path: str, stop_evt: threading.Event | None = None) -> None:
    """Play MP3 synchronously; if `stop_evt` is set during playback, abort.

    Two-phase polling: first wait until MCI reports `playing` (the `play`
    command is async, so the status briefly reads `stopped`/`open` before the
    audio actually starts — exiting in that window swallowed chunks). Then
    poll for end-of-playback or interrupt.
    """
    import time
    alias = _next_alias()
    rc = _mci_send(f'open "{path}" type mpegvideo alias {alias}')
    if rc != 0:
        return  # couldn't open the file (corrupt MP3 / device busy)
    try:
        if _mci_send(f"play {alias}") != 0:
            return

        buf = ctypes.create_unicode_buffer(64)

        # Phase 1: wait up to ~600 ms for play to actually begin
        for _ in range(12):
            if stop_evt is not None and stop_evt.is_set():
                _mci_send(f"stop {alias}")
                return
            _mci_send(f"status {alias} mode", buf)
            if buf.value.lower() == "playing":
                break
            time.sleep(0.05)

        # Phase 2: poll until playback finishes or we're asked to stop
        while True:
            if stop_evt is not None and stop_evt.is_set():
                _mci_send(f"stop {alias}")
                return
            _mci_send(f"status {alias} mode", buf)
            if buf.value.lower() != "playing":
                return
            time.sleep(0.05)
    finally:
        _mci_send(f"close {alias}")


TTS_RATE = os.environ.get("PC_MANAGER_TTS_RATE", "+18%")

# 'edge' = Microsoft neural voices via edge-tts (natural, ~500ms first-audio)
# 'sapi' = Windows built-in SAPI 5 voices (instant, more robotic)
TTS_BACKEND = os.environ.get("PC_MANAGER_TTS_BACKEND", "edge").lower()


# ---------------------------------------------------------------------------
# SAPI 5 backend — uses Windows' built-in speech engine via COM. No network,
# no MP3 generation; audio plays directly through the system mixer.
# Interruptible via Speak("", flags=async|purge).
# ---------------------------------------------------------------------------
_SAPI_FLAG_ASYNC  = 1
_SAPI_FLAG_PURGE  = 2  # purge any queued speech before this Speak
# SpeechRunState enum: 1 = SRSEDone, 2 = SRSEIsSpeaking
_SAPI_STATE_SPEAKING = 2


class _SapiSpeaker:
    """Owns a single SAPI.SpVoice for a thread (COM is apartment-bound).

    Searches BOTH voice registries for installed voices, then picks the best:
      - SAPI 5 legacy: HKLM\\SOFTWARE\\Microsoft\\Speech\\Voices\\Tokens
                       (David, Zira, etc. — what GetVoices() shows by default)
      - OneCore:        HKLM\\SOFTWARE\\Microsoft\\Speech_OneCore\\Voices\\Tokens
                       (Mark, Hera, Ravi, Davis, plus Win11 neural voices —
                        invisible to GetVoices() but loadable via SetId)

    Priority order:
      1. PC_MANAGER_SAPI_VOICE env var match (substring, case-insensitive)
      2. Natural neural voices (Aria > Ava > Jenny > Michelle > Andrew > Brian)
      3. Specific legacy voices (Mark > Zira > Hazel)
      4. Anything female
      5. First listed
    """
    ONECORE_REG = r"SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens"
    PRIORITY = [
        "Aria", "Ava", "Jenny", "Michelle",  # natural female
        "Andrew", "Brian", "Christopher", "Davis",  # natural male
        "Sonia",
        "Mark", "Zira", "Hazel",
        "Female",
    ]

    def __init__(self, rate_steps: int = 2):
        import pythoncom, win32com.client
        self._pythoncom = pythoncom
        self._win32com = win32com.client
        pythoncom.CoInitialize()
        self._voice = win32com.client.Dispatch("SAPI.SpVoice")
        self._chosen = self._pick_voice()
        try:
            self._voice.Rate = rate_steps
        except Exception:
            pass

    @staticmethod
    def _list_onecore_voices() -> list[tuple[str, str]]:
        """Return [(description, registry_path), ...] for OneCore voices."""
        import winreg
        out: list[tuple[str, str]] = []
        try:
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                               _SapiSpeaker.ONECORE_REG)
        except OSError:
            return out
        try:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                full = f"HKEY_LOCAL_MACHINE\\{_SapiSpeaker.ONECORE_REG}\\{name}"
                desc = name
                try:
                    sub = winreg.OpenKey(k, name)
                    try:
                        desc = winreg.QueryValueEx(sub, "")[0] or name
                    finally:
                        winreg.CloseKey(sub)
                except OSError:
                    pass
                out.append((desc, full))
        finally:
            winreg.CloseKey(k)
        return out

    def _candidates(self) -> list[tuple[str, str, object]]:
        """Build (description, kind, payload) for every installed voice.
           kind = 'sapi5' (payload is the IVoice), or 'onecore' (registry path).
        """
        cands: list[tuple[str, str, object]] = []
        try:
            for v in self._voice.GetVoices():
                cands.append((v.GetDescription(), "sapi5", v))
        except Exception:
            pass
        for desc, path in self._list_onecore_voices():
            cands.append((desc, "onecore", path))
        return cands

    def _select(self, cand: tuple[str, str, object]) -> str:
        desc, kind, payload = cand
        if kind == "sapi5":
            self._voice.Voice = payload
        else:
            token = self._win32com.Dispatch("SAPI.SpObjectToken")
            token.SetId(payload)  # type: ignore[arg-type]
            self._voice.Voice = token
        return desc

    def _pick_voice(self) -> str:
        cands = self._candidates()
        if not cands:
            return "(none)"

        # 1. Explicit env var override
        explicit = os.environ.get("PC_MANAGER_SAPI_VOICE", "").strip().lower()
        if explicit:
            for c in cands:
                if explicit in c[0].lower():
                    return self._select(c)

        # 2. Priority keywords
        for keyword in self.PRIORITY:
            kw = keyword.lower()
            for c in cands:
                if kw in c[0].lower():
                    return self._select(c)

        # 3. First listed
        return self._select(cands[0])

    @property
    def voice_name(self) -> str:
        return self._chosen

    def speak_blocking(self, text: str, stop_evt: threading.Event | None) -> None:
        import time
        self._voice.Speak(text, _SAPI_FLAG_ASYNC)

        # Phase 1: wait briefly for SAPI to actually transition to speaking
        # (Speak() returns immediately with state still at "Done" for ~50ms)
        for _ in range(20):  # up to ~800 ms
            if stop_evt is not None and stop_evt.is_set():
                self._purge()
                return
            try:
                if self._voice.Status.RunningState == _SAPI_STATE_SPEAKING:
                    break
            except Exception:
                return
            time.sleep(0.04)

        # Phase 2: poll until done or interrupted
        while True:
            if stop_evt is not None and stop_evt.is_set():
                self._purge()
                return
            try:
                if self._voice.Status.RunningState != _SAPI_STATE_SPEAKING:
                    return
            except Exception:
                return
            time.sleep(0.04)

    def _purge(self):
        try:
            self._voice.Speak("", _SAPI_FLAG_ASYNC | _SAPI_FLAG_PURGE)
        except Exception:
            pass

    def close(self):
        try:
            self._pythoncom.CoUninitialize()
        except Exception:
            pass


def _gen_edge_mp3(text: str, voice: str) -> str:
    """Synthesize text to a temp MP3 file via edge-tts. Returns the file path."""
    import edge_tts
    fd, path = tempfile.mkstemp(suffix=".mp3", prefix="pcm_tts_")
    os.close(fd)
    try:
        async def _gen():
            comm = edge_tts.Communicate(text, voice=voice, rate=TTS_RATE)
            await comm.save(path)
        asyncio.run(_gen())
        return path
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def _speak_edge(
    text: str, voice: str, stop_evt: threading.Event | None = None
) -> None:
    """Generate-and-play helper (kept for compatibility / single-shot calls)."""
    path = _gen_edge_mp3(text, voice)
    try:
        if stop_evt is not None and stop_evt.is_set():
            return
        _play_mp3_blocking(path, stop_evt)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Claude Code subprocess wrapper
# ---------------------------------------------------------------------------
MEMORY_PATH = Path.home() / ".pc-manager-memory.md"
SESSION_PATH = Path.home() / ".pc-manager-voice-session.json"


def _load_memory() -> str:
    try:
        return MEMORY_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except Exception:
        return ""


def _build_system_prompt() -> str:
    """Compose the voice-mode system prompt with persistent memory injected."""
    base = (
        "You are speaking with the user by voice — your reply will be read aloud "
        "by a TTS engine. Rules: keep it to 1–2 short sentences, plain natural "
        "language only, no markdown, no tables, no bullet lists, no code blocks, "
        "no asterisks, no hyphens for bullets. When the user asks about their "
        "computer, use the pc-manager MCP tools and then summarize the result "
        "conversationally with rounded numbers."
    )
    memory = _load_memory()
    memory_block = (
        "\n\n## Long-term memory (persists across sessions)\n\n"
        f"{memory or '_(empty — call `remember` when the user shares something worth keeping)_'}"
        "\n\nWhen the user shares a preference, ongoing project, or fact you "
        "should know in future sessions, call `mcp__pc-manager__remember` "
        "with a one-line note. To curate (merge dupes, prune stale facts), "
        "use `mcp__pc-manager__update_memory`."
    )
    return base + memory_block


SYSTEM_HINT = _build_system_prompt()


PC_MCP_TOOLS = ",".join([
    "mcp__pc-manager__get_system_stats",
    "mcp__pc-manager__get_cpu_stats",
    "mcp__pc-manager__get_ram_stats",
    "mcp__pc-manager__get_gpu_stats",
    "mcp__pc-manager__get_disk_stats",
    "mcp__pc-manager__get_top_processes",
    "mcp__pc-manager__clear_ram",
    "mcp__pc-manager__kill_process",
])


# Opus by default — change with PC_MANAGER_VOICE_MODEL.
VOICE_MODEL = os.environ.get("PC_MANAGER_VOICE_MODEL", "claude-opus-4-7")


PC_MCP_TOOL_LIST = [
    # System / monitor
    "mcp__pc-manager__get_system_stats",
    "mcp__pc-manager__get_cpu_stats",
    "mcp__pc-manager__get_ram_stats",
    "mcp__pc-manager__get_gpu_stats",
    "mcp__pc-manager__get_disk_stats",
    "mcp__pc-manager__get_top_processes",
    # Actions
    "mcp__pc-manager__clear_ram",
    "mcp__pc-manager__kill_process",
    # Tasks (checklist)
    "mcp__pc-manager__list_tasks",
    "mcp__pc-manager__add_task",
    "mcp__pc-manager__set_task_done",
    "mcp__pc-manager__delete_task",
    "mcp__pc-manager__clear_completed_tasks",
    # Claude Code instances
    "mcp__pc-manager__list_claude_groups",
    "mcp__pc-manager__create_claude_group",
    "mcp__pc-manager__delete_claude_group",
    "mcp__pc-manager__add_claude_instance",
    "mcp__pc-manager__remove_claude_instance",
    "mcp__pc-manager__launch_claude_instance",
    # Long-term memory
    "mcp__pc-manager__read_memory",
    "mcp__pc-manager__remember",
    "mcp__pc-manager__update_memory",
    "mcp__pc-manager__clear_memory",
]


# ---------------------------------------------------------------------------
# Persistent Claude session via Claude Agent SDK
# ---------------------------------------------------------------------------
# A long-lived ClaudeSDKClient keeps the MCP servers connected so each turn
# only pays the model-roundtrip cost (~5s) instead of the cold-start cost
# (~15s including MCP init). The asyncio event loop runs on a background
# thread so the synchronous voice pipeline can call .ask() like a function.
def _load_session_id() -> str | None:
    """Return the previously-saved Claude session ID, if any."""
    try:
        data = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
        sid = data.get("session_id")
        return sid if isinstance(sid, str) and sid else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _save_session_id(session_id: str) -> None:
    try:
        SESSION_PATH.write_text(
            json.dumps({"session_id": session_id}), encoding="utf-8"
        )
    except OSError:
        pass


class ClaudeSession:
    def __init__(
        self,
        model: str = VOICE_MODEL,
        system_prompt: str | None = None,
        allowed_tools: list[str] | None = None,
    ):
        # Rebuild prompt fresh so newly-stored memories from prior sessions
        # are picked up when the dock relaunches.
        if system_prompt is None:
            system_prompt = _build_system_prompt()

        # Lazy import — keeps voice module importable even if SDK isn't installed
        from claude_agent_sdk import ClaudeAgentOptions
        opts_kw: dict = dict(
            model=model,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools or PC_MCP_TOOL_LIST,
            permission_mode="bypassPermissions",
            include_partial_messages=True,  # token-level streaming for low-latency TTS
        )
        # Resume the previous session if we have one — gives Claude continuous
        # memory of prior conversations across panel restarts.
        prior = _load_session_id()
        if prior:
            opts_kw["resume"] = prior
        self._opts = ClaudeAgentOptions(**opts_kw)
        import asyncio as _aio
        self._aio = _aio
        self._loop = _aio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, daemon=True, name="ClaudeSession",
        )
        self._loop_thread.start()
        self._client = None
        self._connected = False

    def _run_loop(self):
        self._aio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _ensure(self):
        if self._connected:
            return
        from claude_agent_sdk import ClaudeSDKClient
        self._client = ClaudeSDKClient(options=self._opts)
        await self._client.connect()
        self._connected = True

    def prewarm(self):
        """Kick off connect + MCP init in the background, returns immediately."""
        self._aio.run_coroutine_threadsafe(self._ensure(), self._loop)

    def ask(self, prompt: str, timeout: float = 120) -> str:
        async def _do():
            await self._ensure()
            await self._client.query(prompt)
            parts: list[str] = []
            async for msg in self._client.receive_response():
                for block in getattr(msg, "content", []) or []:
                    text = getattr(block, "text", None)
                    if text:
                        parts.append(text)
            return "".join(parts).strip()
        future = self._aio.run_coroutine_threadsafe(_do(), self._loop)
        return future.result(timeout=timeout)

    def stream(self, prompt: str, timeout: float = 120):
        """Yield text chunks from Claude as they arrive (generator).

        With include_partial_messages=True, the SDK emits StreamEvent objects
        containing raw Anthropic content_block_delta events. We pluck the
        token-level text deltas as they stream, plus dedupe against the final
        AssistantMessage so we don't speak the same text twice.
        """
        import queue as _q
        SENTINEL = object()
        ERROR = object()
        chunk_q: _q.Queue = _q.Queue()

        async def _do():
            try:
                await self._ensure()
                await self._client.query(prompt)
                streamed_any = False  # if we got partial deltas, skip final block re-emit
                async for msg in self._client.receive_response():
                    # Stash the session id from any message that carries one,
                    # so future panel launches can resume this conversation.
                    sid = getattr(msg, "session_id", None)
                    if isinstance(sid, str) and sid:
                        _save_session_id(sid)

                    # Token-level deltas (StreamEvent → event dict)
                    evt = getattr(msg, "event", None)
                    if isinstance(evt, dict):
                        if evt.get("type") == "content_block_delta":
                            delta = evt.get("delta") or {}
                            if delta.get("type") == "text_delta":
                                text = delta.get("text") or ""
                                if text:
                                    chunk_q.put(text)
                                    streamed_any = True
                        continue
                    # Fallback: final assembled AssistantMessage. Only emit if
                    # we never saw partials (otherwise we'd duplicate the text).
                    if streamed_any:
                        continue
                    for block in getattr(msg, "content", []) or []:
                        text = getattr(block, "text", None)
                        if text:
                            chunk_q.put(text)
            except Exception as e:
                chunk_q.put((ERROR, e))
            finally:
                chunk_q.put(SENTINEL)

        self._aio.run_coroutine_threadsafe(_do(), self._loop)
        while True:
            item = chunk_q.get(timeout=timeout)
            if item is SENTINEL:
                return
            if isinstance(item, tuple) and item and item[0] is ERROR:
                raise item[1]
            yield item


def _drain_sentences(text: str, min_clause_words: int = 10) -> tuple[list[str], str]:
    """Pop speakable chunks off `text`, return (chunks, leftover).

    A chunk is either:
      - a full sentence ending in .!? followed by whitespace, OR
      - a clause ending in , ; : with at least `min_clause_words` words

    Triggering on long clauses lets TTS start before Claude finishes the
    sentence, dropping time-to-first-audio by another beat or two.
    """
    chunks: list[str] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        is_sentence_end = c in ".!?" and (i + 1 == n or text[i + 1].isspace())
        is_clause_end   = c in ",;:" and (i + 1 == n or text[i + 1].isspace())
        if is_sentence_end or is_clause_end:
            chunk = text[start:i + 1].strip()
            if chunk and (
                is_sentence_end or len(chunk.split()) >= min_clause_words
            ):
                chunks.append(chunk)
                start = i + 1
                while start < n and text[start].isspace():
                    start += 1
                i = start
                continue
        i += 1
    return chunks, text[start:]

    def close(self):
        async def _disc():
            if self._client:
                try:
                    await self._client.disconnect()
                except Exception:
                    pass
        try:
            f = self._aio.run_coroutine_threadsafe(_disc(), self._loop)
            f.result(timeout=5)
        except Exception:
            pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Voice assistant
# ---------------------------------------------------------------------------
class VoiceAssistant:
    """Always-on voice loop with barge-in.

    listen_in_background runs a phrase detector continuously. Each detected
    utterance is handed to a worker thread that transcribes → asks Claude →
    speaks the reply. If a new phrase arrives while TTS is playing, the
    playback aborts and the new phrase is processed (barge-in).
    """

    _DEFAULT_VOICE = os.environ.get("PC_MANAGER_TTS_VOICE", "en-US-AriaNeural")

    def __init__(
        self,
        on_user: Callable[[str], None],
        on_claude: Callable[[str], None],
        on_status: Callable[[str], None],
    ):
        self.on_user = on_user
        self.on_claude = on_claude
        self.on_status = on_status
        self.recognizer = sr.Recognizer()
        self.recognizer.dynamic_energy_threshold = False
        self.recognizer.energy_threshold = 200
        self.recognizer.pause_threshold = 0.6
        self.recognizer.non_speaking_duration = 0.3

        self._mic = sr.Microphone()
        self._stop_listener = None       # callable returned by listen_in_background
        self._user_listening = False     # whether the user wants the session active
        self._calibrated = False
        self._base_threshold = self.recognizer.energy_threshold

        self._speaking = False
        self._stop_speaking_evt = threading.Event()

        # Persistent Claude session — pre-warmed on first listen
        self._session: ClaudeSession | None = None

        # Serializes the LLM/TTS pipeline so two phrases don't pile up
        self._pipeline_lock = threading.Lock()
        self._tts = None
        self._tts_lock = threading.Lock()

    # ------------------- Claude session ------------------
    def _ensure_session(self) -> "ClaudeSession":
        if self._session is None:
            self._session = ClaudeSession()
        return self._session

    # ------------------- Continuous listen -------------------
    def start_continuous(self):
        if self._stop_listener is not None:
            return
        # Calibrate once for the room
        try:
            with self._mic as src:
                self.recognizer.adjust_for_ambient_noise(src, duration=0.5)
            self._calibrated = True
            # Don't trust the dynamic adjustment — bake in a sane floor
            self._base_threshold = max(self.recognizer.energy_threshold, 180)
            self.recognizer.energy_threshold = self._base_threshold
        except Exception as e:
            self.on_status(f"mic error: {e}")
            return
        # Pre-warm the Claude session in the background — by the time the user
        # finishes speaking their first phrase, MCP servers should be connected.
        try:
            self._ensure_session().prewarm()
        except Exception:
            pass
        self._user_listening = True
        self._resume_listening()
        self.on_status("listening")

    def stop_continuous(self):
        self._user_listening = False
        self._pause_listening()
        self._stop_speaking_evt.set()
        self.on_status("stopped")

    def is_running(self) -> bool:
        return self._user_listening

    # ------------------- Pause/resume the background listener ----
    def _pause_listening(self):
        if self._stop_listener is not None:
            try:
                self._stop_listener(wait_for_stop=False)
            except Exception:
                pass
            self._stop_listener = None

    def _resume_listening(self):
        if not self._user_listening or self._stop_listener is not None:
            return
        try:
            self._stop_listener = self.recognizer.listen_in_background(
                self._mic, self._on_phrase, phrase_time_limit=15
            )
        except Exception as e:
            self.on_status(f"mic error: {e}")

    # ------------------- Phrase callback (background thread) -------------------
    def _on_phrase(self, _recognizer, audio):
        # We pause the listener during TTS, but its callback can still fire
        # once after stop_listening(wait_for_stop=False). Ignore residual
        # callbacks so they don't interrupt playback or double-process.
        if self._speaking or not self._user_listening:
            return
        threading.Thread(
            target=self._process_phrase, args=(audio,), daemon=True
        ).start()

    def _process_phrase(self, audio):
        # Only one phrase progresses through the pipeline at a time
        if not self._pipeline_lock.acquire(blocking=False):
            return
        try:
            # 1. Transcribe
            self.on_status("transcribing…")
            try:
                user_text = self.recognizer.recognize_google(audio)
            except sr.UnknownValueError:
                self.on_status("listening")
                return  # background noise, not real speech
            except sr.RequestError as e:
                self.on_status(f"STT error: {e}")
                return
            user_text = (user_text or "").strip()
            if len(user_text) < 2:
                self.on_status("listening")
                return
            self.on_user(user_text)

            # 2 + 3. Stream Claude → sentence buffer → TTS queue (pipelined)
            self.on_status("thinking…")
            self._stream_and_speak(user_text)
            self._after_reply()
        finally:
            self._pipeline_lock.release()

    # ------------------- Streaming Claude → pipelined TTS -------------------
    def _stream_and_speak(self, prompt: str):
        """Stream Claude's reply and dispatch chunks to the chosen TTS backend.

        - Edge backend (default, natural voice, ~500 ms first-audio): three
          pipelined stages — text drain → MP3 gen → MCI playback in parallel.
        - SAPI backend (instant, robotic): single speaker thread that calls
          SAPI directly with no MP3 round-trip.
        """
        if TTS_BACKEND == "sapi":
            return self._stream_and_speak_sapi(prompt)
        return self._stream_and_speak_edge(prompt)

    def _stream_and_speak_edge(self, prompt: str):
        """edge-tts pipelined backend (was the previous _stream_and_speak)."""
        import queue as _q

        text_q:  _q.Queue = _q.Queue()
        audio_q: _q.Queue = _q.Queue()
        full_parts: list[str] = []
        self._stop_speaking_evt.clear()

        # ---- Generator: text → MP3 (runs ahead of the player) ----
        def generator():
            try:
                while True:
                    text = text_q.get()
                    if text is None:
                        break
                    if self._stop_speaking_evt.is_set():
                        continue
                    try:
                        path = _gen_edge_mp3(text, self._DEFAULT_VOICE)
                        audio_q.put(path)
                    except Exception as e:
                        self.on_status(f"tts gen error: {e}")
            finally:
                audio_q.put(None)

        # ---- Player: MP3 path → audio out ----
        def player():
            self._pause_listening()
            spoken = False
            try:
                while True:
                    path = audio_q.get()
                    if path is None:
                        break
                    if self._stop_speaking_evt.is_set():
                        try: os.unlink(path)
                        except OSError: pass
                        continue
                    if not spoken:
                        spoken = True
                        self._speaking = True
                        self.on_status("speaking…")
                    try:
                        _play_mp3_blocking(path, self._stop_speaking_evt)
                    except Exception as e:
                        self.on_status(f"tts play error: {e}")
                    finally:
                        try: os.unlink(path)
                        except OSError: pass
            finally:
                self._speaking = False
                self._stop_speaking_evt.clear()
                self._resume_listening()

        gen_t = threading.Thread(target=generator, daemon=True)
        play_t = threading.Thread(target=player, daemon=True)
        gen_t.start()
        play_t.start()

        buf = ""
        try:
            for chunk in self._ensure_session().stream(prompt):
                full_parts.append(chunk)
                buf += chunk
                chunks, buf = _drain_sentences(buf)
                for s in chunks:
                    text_q.put(s)
            tail = buf.strip()
            if tail:
                text_q.put(tail)
        except Exception as e:
            self.on_status(f"error: {e}")
        finally:
            text_q.put(None)

        full = "".join(full_parts).strip()
        if full:
            self.on_claude(full)
        gen_t.join()
        play_t.join()

    def _stream_and_speak_sapi(self, prompt: str):
        """SAPI backend — single speaker thread, instant first-audio."""
        import queue as _q
        text_q: _q.Queue = _q.Queue()
        full_parts: list[str] = []
        self._stop_speaking_evt.clear()

        def speaker():
            try:
                sapi = _SapiSpeaker()
            except Exception as e:
                self.on_status(f"sapi init error: {e}")
                return
            self._pause_listening()
            spoken = False
            try:
                while True:
                    text = text_q.get()
                    if text is None:
                        break
                    if self._stop_speaking_evt.is_set():
                        continue
                    if not spoken:
                        spoken = True
                        self._speaking = True
                        self.on_status("speaking…")
                    try:
                        sapi.speak_blocking(text, self._stop_speaking_evt)
                    except Exception as e:
                        self.on_status(f"sapi error: {e}")
            finally:
                self._speaking = False
                self._stop_speaking_evt.clear()
                sapi.close()
                self._resume_listening()

        speaker_t = threading.Thread(target=speaker, daemon=True)
        speaker_t.start()

        buf = ""
        try:
            for chunk in self._ensure_session().stream(prompt):
                full_parts.append(chunk)
                buf += chunk
                chunks, buf = _drain_sentences(buf)
                for s in chunks:
                    text_q.put(s)
            tail = buf.strip()
            if tail:
                text_q.put(tail)
        except Exception as e:
            self.on_status(f"error: {e}")
        finally:
            text_q.put(None)

        full = "".join(full_parts).strip()
        if full:
            self.on_claude(full)
        speaker_t.join()

    # ------------------- Legacy single-shot speak (kept for compatibility) -----
    def _speak_interruptible(self, text: str):
        text = (text or "").strip()
        if not text:
            return
        self._stop_speaking_evt.clear()
        self._speaking = True
        # Boost threshold so the TTS doesn't make the mic trigger on itself
        self.recognizer.energy_threshold = self._base_threshold * 4
        try:
            # Aria via edge-tts only — no SAPI fallback, since dual TTS is worse
            # than a brief silence if internet is flaky.
            try:
                _speak_edge(text, self._DEFAULT_VOICE, self._stop_speaking_evt)
            except Exception as e:
                self.on_status(f"tts error: {e}")
        finally:
            self._speaking = False
            self.recognizer.energy_threshold = self._base_threshold
            self._stop_speaking_evt.clear()

    def process_text(self, text: str):
        """Send a typed message through the same Claude → TTS pipeline as voice."""
        if not self._pipeline_lock.acquire(blocking=False):
            return
        try:
            self.on_status("thinking…")
            self._stream_and_speak(text)
            self._after_reply()
        finally:
            self._pipeline_lock.release()

    def _after_reply(self):
        """Settle back to listening. No celebration emote between turns —
        the racing-flag finish is reserved for explicit task completion."""
        if not self.is_running():
            self.on_status("stopped")
            return
        self.on_status("listening")

    def reset(self):
        pass
