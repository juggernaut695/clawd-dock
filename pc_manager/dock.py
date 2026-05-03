"""Side Dock — native frameless webview rendering the design HTML/CSS/JSX.

This replaces the old Tk implementation. The actual UI is the design from the
handoff bundle, served from `pc_manager/web/`. We expose a Python `API` class
to the page (system stats, voice control, notes persistence, system actions),
and push voice-loop callbacks back into the page via `evaluate_js`.
"""
from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import threading
import time
from ctypes import (POINTER, Structure, byref, c_int, c_size_t, c_uint,
                    pointer, sizeof, windll, wintypes)
from pathlib import Path
from urllib.request import Request, urlopen

import psutil
import webview

from pc_manager import monitor, ram_clear, terminal as ptyterm


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
WEB_DIR    = Path(__file__).parent / "web"
INDEX_HTML = WEB_DIR / "index.html"

CONFIG_PATH = Path.home() / ".pc-manager-dock.json"
NOTES_PATH  = Path.home() / ".pc-manager-notes.txt"
TASKS_PATH  = Path.home() / ".pc-manager-tasks.json"
CLAUDE_PATH = Path.home() / ".pc-manager-claude.json"
CHAT_SESSIONS_PATH = Path.home() / ".pc-manager-chat-sessions.json"

PANEL_W = 480  # physical pixels at the docked edge
TERMINAL_WS_PORT = 7654  # localhost-only WebSocket bridge for embedded terminals

# Set if the dock WS server thread fails to bind (port collision, OSError).
# JS reads this via api.get_ws_status() to show a dock-level error banner
# instead of every WS-backed feature silently failing.
WS_SERVER_ERROR: str | None = None
CHAT_WINDOW_TITLE = "Claude Code"
TASK_WINDOW_TITLE = "Task Editor"
# Fixed window surface sizes — must match create_window() so WebView2's
# transparent backbuffer is allocated at the right dimensions and never has
# to be resized (which would paint it opaque white).
CHAT_WINDOW_W = 1100
CHAT_WINDOW_H = 800
TASK_WINDOW_W = 880
TASK_WINDOW_H = 700


# ---------------------------------------------------------------------------
# Win32 helpers (DPI awareness, work area, AppBar)
# ---------------------------------------------------------------------------
def _make_dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_make_dpi_aware()


def _get_work_area() -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    SPI_GETWORKAREA = 0x0030
    windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, byref(rect), 0)
    return rect.left, rect.top, rect.right, rect.bottom


# ---- AppBar (reserve screen space alongside the taskbar) ----
ABM_NEW       = 0x00000000
ABM_REMOVE    = 0x00000001
ABM_QUERYPOS  = 0x00000002
ABM_SETPOS    = 0x00000003
ABE_LEFT      = 0
ABE_RIGHT     = 2


class _APPBARDATA(Structure):
    _fields_ = [
        ("cbSize",           c_uint),
        ("hWnd",             wintypes.HWND),
        ("uCallbackMessage", c_uint),
        ("uEdge",            c_uint),
        ("rc",               wintypes.RECT),
        ("lParam",           wintypes.LPARAM),
    ]


def _appbar_register(hwnd: int, edge: int, width_px: int) -> _APPBARDATA | None:
    """Register `hwnd` as an appbar at `edge` reserving `width_px` pixels.

    We deliberately skip the `ABM_QUERYPOS` round-trip. QUERYPOS asks the OS
    to adjust the rect to avoid existing appbars — but if a previous instance
    of this app crashed or was killed without unregistering, the OS still
    counts that dead registration and shoves us 480 px inward. Forcing the
    exact desired rect via `ABM_SETPOS` directly always lands us flush at
    the screen edge.
    """
    if not hwnd:
        return None
    shell32 = windll.shell32
    abd = _APPBARDATA()
    abd.cbSize = ctypes.sizeof(_APPBARDATA)
    abd.hWnd = hwnd
    abd.uCallbackMessage = 0
    abd.uEdge = edge

    if not shell32.SHAppBarMessage(ABM_NEW, byref(abd)):
        # Already registered — fall through and SETPOS will move us
        pass

    # Use the work-area rect (excludes taskbar) so the AppBar matches
    # exactly what `_compute_geometry()` gave the window at create time.
    # When the rects match, the subsequent MoveWindow is a no-op — important
    # because resizing a `transparent=True` pywebview window after creation
    # paints uncomposited white pixels on the left edge.
    l, t, r, b = _get_work_area()
    abd.rc.top    = t
    abd.rc.bottom = b
    if edge == ABE_RIGHT:
        abd.rc.left  = r - width_px
        abd.rc.right = r
    else:
        abd.rc.left  = l
        abd.rc.right = l + width_px

    shell32.SHAppBarMessage(ABM_SETPOS, byref(abd))
    return abd


def _appbar_unregister(abd: _APPBARDATA | None) -> None:
    if abd is None:
        return
    try:
        windll.shell32.SHAppBarMessage(ABM_REMOVE, byref(abd))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Network helpers (live up/down rate, public IP, local IP)
# ---------------------------------------------------------------------------
class NetSampler:
    def __init__(self):
        self._last_io = psutil.net_io_counters()
        self._last_t  = time.monotonic()
        self._public_ip: str | None = None
        threading.Thread(target=self._fetch_public_ip, daemon=True).start()

    def _fetch_public_ip(self):
        try:
            req = Request("https://api.ipify.org",
                          headers={"User-Agent": "side-dock/1.0"})
            with urlopen(req, timeout=4) as r:
                self._public_ip = r.read().decode().strip()
        except Exception:
            self._public_ip = None

    @staticmethod
    def local_ip() -> str:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
        except Exception:
            return "—"

    def sample(self) -> dict:
        now = psutil.net_io_counters()
        t = time.monotonic()
        dt = t - self._last_t
        down = (now.bytes_recv - self._last_io.bytes_recv) / dt if dt > 0 else 0
        up   = (now.bytes_sent - self._last_io.bytes_sent) / dt if dt > 0 else 0
        self._last_io = now
        self._last_t = t
        return {
            "down_bps":  max(0.0, down),
            "up_bps":    max(0.0, up),
        }


# ---------------------------------------------------------------------------
# JS-Python API exposed to the page via window.pywebview.api
# ---------------------------------------------------------------------------
class API:
    def __init__(self):
        self._window: webview.Window | None = None
        self._chat_window: webview.Window | None = None
        self._task_window: webview.Window | None = None
        self._net = NetSampler()
        self._va = None  # lazy VoiceAssistant
        self._user_listening = False

    def _bind_window(self, w: webview.Window):
        self._window = w

    def _bind_chat_window(self, w: webview.Window):
        self._chat_window = w

    def _bind_task_window(self, w: webview.Window):
        self._task_window = w

    # ---------- monitor ----------
    def get_stats(self) -> dict:
        snap = monitor.get_snapshot()
        snap["net_rate"]  = self._net.sample()
        snap["public_ip"] = self._net._public_ip
        snap["local_ip"]  = NetSampler.local_ip()
        return snap

    # ---------- control / system actions ----------
    # Process names whose working sets we never trim — touching these
    # blanks out our own UI or freezes the desktop while pages page back:
    #   pythonw / python              → us
    #   msedgewebview2                → WebView2 host + renderer + GPU + utility.
    #                                   Some children get re-parented to the
    #                                   WebView2 runtime broker so they don't
    #                                   show up under `pythonw.children()`.
    #   dwm                           → Desktop Window Manager (compositor).
    #                                   Evicting its pages forces every
    #                                   visible window (including our dock)
    #                                   to flash black during the repaint.
    #   explorer                      → Shell (taskbar + desktop). Less
    #                                   critical but causes visible flicker.
    _CLEAR_RAM_SKIP_NAMES = {
        "pythonw.exe", "python.exe",
        "msedgewebview2.exe",
        "dwm.exe",
        "explorer.exe",
    }

    def clear_ram(self) -> dict:
        skip: set[int] = {os.getpid()}
        # Walk our own descendants
        try:
            me = psutil.Process()
            for child in me.children(recursive=True):
                try:
                    skip.add(child.pid)
                except Exception:
                    pass
        except Exception:
            pass
        # Also catch any process with a critical executable name —
        # WebView2 renderer/GPU/utility processes are often re-parented
        # to the WebView2 runtime broker and won't show up as our
        # descendants, even though killing them blanks our UI.
        try:
            for p in psutil.process_iter(["pid", "name"]):
                try:
                    name = (p.info.get("name") or "").lower()
                    if name in self._CLEAR_RAM_SKIP_NAMES:
                        skip.add(p.info["pid"])
                except Exception:
                    pass
        except Exception:
            pass
        result = ram_clear.trim_working_sets(skip_pids=skip)
        self._push_state({"actionResult": result})
        return result

    def lock(self):
        threading.Thread(target=lambda: subprocess.run(
            ["rundll32.exe", "user32.dll,LockWorkStation"],
            creationflags=0x08000000,
        ), daemon=True).start()

    def sleep(self):
        threading.Thread(target=lambda: subprocess.run(
            ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
            creationflags=0x08000000,
        ), daemon=True).start()

    def show_desktop(self):
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command",
                 "(New-Object -ComObject Shell.Application).ToggleDesktop()"],
                creationflags=0x08000000,
            )
        except Exception:
            pass

    def wake(self):
        try:
            ctypes.windll.user32.mouse_event(0x0001, 1, 1, 0, 0)
            ctypes.windll.user32.mouse_event(0x0001, -1, -1, 0, 0)
        except Exception:
            pass

    def mute(self):
        try:
            VK_VOLUME_MUTE = 0xAD
            user32 = ctypes.windll.user32
            user32.keybd_event(VK_VOLUME_MUTE, 0, 0, 0)
            user32.keybd_event(VK_VOLUME_MUTE, 0, 2, 0)
        except Exception:
            pass

    def capture(self):
        try:
            user32 = ctypes.windll.user32
            VK_LWIN, VK_SHIFT, VK_S = 0x5B, 0x10, 0x53
            for vk, flags in (
                (VK_LWIN, 0), (VK_SHIFT, 0), (VK_S, 0),
                (VK_S, 2), (VK_SHIFT, 2), (VK_LWIN, 2),
            ):
                user32.keybd_event(vk, 0, flags, 0)
        except Exception:
            pass

    # ---------- notes ----------
    def load_notes(self) -> str:
        try:
            return NOTES_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        except Exception:
            return ""

    def save_notes(self, text: str) -> bool:
        try:
            NOTES_PATH.write_text(text or "", encoding="utf-8")
            return True
        except Exception:
            return False

    # ---------- tasks (checklist) ----------
    def load_tasks(self) -> list:
        try:
            return json.loads(TASKS_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception:
            return []

    def save_tasks(self, tasks: list) -> bool:
        try:
            TASKS_PATH.write_text(
                json.dumps(tasks or [], indent=2), encoding="utf-8"
            )
            return True
        except Exception:
            return False

    def get_task(self, task_id) -> dict | None:
        """Return one task by id (string-compared so JS Number ids match)."""
        try:
            tasks = self.load_tasks()
            for t in tasks:
                if str(t.get("id")) == str(task_id):
                    return t
        except Exception:
            pass
        return None

    def update_task(self, task_id, patch: dict) -> dict:
        """Patch one task in place. Returns {ok, task} or {ok: False, error}."""
        try:
            tasks = self.load_tasks()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        updated = None
        for t in tasks:
            if str(t.get("id")) == str(task_id):
                if isinstance(patch, dict):
                    t.update(patch)
                updated = t
                break
        if updated is None:
            return {"ok": False, "error": "task not found"}
        if not self.save_tasks(tasks):
            return {"ok": False, "error": "save failed"}
        return {"ok": True, "task": updated}

    def delete_task(self, task_id) -> dict:
        """Remove one task by id."""
        try:
            tasks = self.load_tasks()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        before = len(tasks)
        tasks = [t for t in tasks if str(t.get("id")) != str(task_id)]
        if len(tasks) == before:
            return {"ok": False, "error": "task not found"}
        if not self.save_tasks(tasks):
            return {"ok": False, "error": "save failed"}
        return {"ok": True}

    # ---------- Task editor window ----------
    def open_task_window(self, task_id) -> dict:
        """Show the pre-created task editor window navigated to this task.

        Encodes the task's full state (id/text/description/done) into the
        URL hash so the freshly-loaded window can render immediately
        without round-tripping back through pywebview's JSON-RPC bridge —
        which sometimes hangs on a window that's been re-navigated via
        load_url().
        """
        if self._task_window is None:
            return {"ok": False, "error": "task window not initialised"}

        from urllib.parse import quote
        task = self.get_task(task_id) or {
            "id": task_id, "text": "", "description": "", "done": False,
        }
        href = (str(INDEX_HTML)
                + f"#mode=task&id={quote(str(task.get('id', '')))}"
                + f"&text={quote(str(task.get('text', '')))}"
                + f"&description={quote(str(task.get('description', '') or ''))}"
                + f"&done={'1' if task.get('done') else '0'}")

        # Centre in the area to the LEFT of the dock, same logic as chat
        u32 = windll.user32
        sw = u32.GetSystemMetrics(0)
        l, t, r, b = _get_work_area()
        right_edge = min(r, sw - PANEL_W)
        avail = (l, t, right_edge, b)

        try:
            self._task_window.load_url(href)
            self._task_window.show()
            threading.Thread(
                target=_chrome_chat_window,
                args=(TASK_WINDOW_TITLE, avail),
                daemon=True,
            ).start()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def close_task_window(self) -> dict:
        """Hide + park the task window off-screen so transparency persists."""
        if self._task_window is None:
            return {"ok": False, "error": "task window not initialised"}
        try:
            self._task_window.hide()
            try:
                self._task_window.move(-9999, -9999)
            except Exception:
                pass
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    # ---------- WS server health probe -------------------------------------
    def get_ws_status(self) -> dict:
        """Returns whether the localhost WS bridge bound successfully and,
        if not, the user-facing error string. JS uses this on boot to
        decide whether to show a dock-level error banner."""
        return {
            "ok": WS_SERVER_ERROR is None,
            "error": WS_SERVER_ERROR,
            "port": TERMINAL_WS_PORT,
        }

    # ---------- Claude Code chat discovery (from ~/.claude/projects/) -------
    def get_claude_chats(self, limit: int = 25) -> list[dict]:
        """Flat list of Claude chats — exactly mirrors what the Claude
        desktop app shows in its left sidebar.

        The desktop app emits an `{"type":"ai-title","aiTitle":"…"}` event
        into each session's `<uuid>.jsonl` once Claude has summarised the
        conversation. We use that as the title verbatim. Sessions without
        an ai-title (still warming up, just opened, etc.) fall back to
        the first substantive user prompt, then to the cwd's basename.

        We dedupe by (project + ai-title) so re-resumed sessions don't
        appear multiple times.
        """
        base = Path.home() / ".claude" / "projects"
        if not base.is_dir():
            return []

        TRIVIAL = {
            "hi", "hey", "hello", "yo", "hola", "test", "ok", "k",
            "run this", "go", "do it", "thanks", "thank you", "ty",
        }

        def is_real_prompt(text: str) -> bool:
            t = (text or "").strip()
            if len(t) < 12:
                return False
            if t.lower() in TRIVIAL:
                return False
            if t.startswith(("<command-", "<system", "<local-command-",
                             "[Request", "Tool result")):
                return False
            return True

        def session_meta(jsonl: Path) -> tuple[str, str, str | None]:
            """Returns (ai_title, fallback_prompt, cwd). Walks the *whole*
            session because the ai-title event lands AFTER several rounds
            of conversation; capping early would miss it for any chat
            that produced more than a few messages."""
            ai_title = ""
            cwd = None
            fallback = ""
            try:
                with jsonl.open("r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        try:
                            evt = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        t = evt.get("type")
                        if t == "ai-title":
                            ai = (evt.get("aiTitle") or "").strip()
                            if ai:
                                ai_title = ai  # latest wins (titles can re-rev)
                            continue
                        if cwd is None and isinstance(evt.get("cwd"), str):
                            cwd = evt["cwd"]
                        if not fallback and t == "user":
                            msg = evt.get("message") or {}
                            content = msg.get("content")
                            text = ""
                            if isinstance(content, str):
                                text = content
                            elif isinstance(content, list):
                                for blk in content:
                                    if (isinstance(blk, dict)
                                            and blk.get("type") == "text"):
                                        text = blk.get("text") or ""
                                        break
                            text = (text or "").strip()
                            if is_real_prompt(text):
                                sentence = text.split("\n", 1)[0]
                                sentence = sentence.split(". ", 1)[0]
                                fallback = sentence.strip()[:80]
            except OSError:
                pass
            return (ai_title, fallback, cwd)

        chats: list[dict] = []
        for proj_dir in base.iterdir():
            if not proj_dir.is_dir():
                continue
            jsonls = sorted(
                proj_dir.glob("*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for jsonl in jsonls:
                try:
                    stat = jsonl.stat()
                except OSError:
                    continue
                if stat.st_size < 1024:
                    # Aborted / empty session — skip
                    continue
                ai_title, fallback, cwd = session_meta(jsonl)
                # Sessions without an ai-title yet are still warming up;
                # the desktop sidebar shows them with placeholder titles
                # too, so include them with our best guess.
                title = ai_title or fallback
                if not title:
                    continue  # truly nothing meaningful — skip
                if not cwd:
                    # The encoded project-dir name (e.g. `D--Code-foo-bar`)
                    # is irrecoverable when the source path itself contained
                    # hyphens — `D-Code-foo-bar` could mean `D:\Code\foo\bar`
                    # or `D-Code\foo\bar`. Don't fabricate a wrong cwd that
                    # would later make every reply fail with "bad cwd".
                    # Best-effort guess only when there's at most one hyphen
                    # past the drive separator, otherwise skip the chat.
                    candidate = proj_dir.name.replace("--", ":\\", 1)
                    candidate = candidate.replace("-", "\\")
                    if Path(candidate).is_dir():
                        cwd = candidate
                    else:
                        # Last resort: skip rather than mislead. The user
                        # can still see their other chats.
                        continue
                project = Path(cwd).name or proj_dir.name
                chats.append({
                    "id": jsonl.stem,
                    "title": title[:120],
                    "project": project,
                    "cwd": cwd,
                    "modified_at": stat.st_mtime,
                    "size_kb": round(stat.st_size / 1024, 1),
                    "has_ai_title": bool(ai_title),
                })

        # Dedupe — keep latest for any (project, lowercase title) pair
        # since resuming a session re-emits the same ai-title.
        seen: dict[tuple[str, str], dict] = {}
        for c in chats:
            key = (c["project"].lower(), c["title"].lower())
            cur = seen.get(key)
            if cur is None or cur["modified_at"] < c["modified_at"]:
                seen[key] = c
        deduped = list(seen.values())
        deduped.sort(key=lambda c: c["modified_at"], reverse=True)
        return deduped[:max(1, int(limit))]

    # Back-compat alias used by older app.jsx revisions
    def get_claude_projects(self) -> list[dict]:
        return self.get_claude_chats(limit=20)

    def get_session_messages(self, session_id: str,
                              limit: int = 200) -> list[dict]:
        """Read the Claude Code session JSONL identified by `session_id`
        (the file's stem under ~/.claude/projects/<proj>/<session>.jsonl)
        and return user/assistant text messages in chronological order.

        Used by the chat window to seed its history when the user opens
        a Recent — so they see the prior conversation instead of a blank
        chat. Tool-output / scaffolding events (`<command-...>`, system
        attachments, etc.) are filtered out so the rendered chat looks
        like a normal back-and-forth.
        """
        base = Path.home() / ".claude" / "projects"
        if not base.is_dir():
            return []
        jsonl: Path | None = None
        for proj in base.iterdir():
            if not proj.is_dir():
                continue
            cand = proj / f"{session_id}.jsonl"
            if cand.exists():
                jsonl = cand
                break
        if jsonl is None:
            return []

        messages: list[dict] = []
        meta: dict[str, object] = {"duration_ms": None, "output_tokens": None}
        try:
            with jsonl.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    t = evt.get("type")
                    if t == "user":
                        msg = evt.get("message") or {}
                        content = msg.get("content")
                        text = ""
                        if isinstance(content, str):
                            text = content
                        elif isinstance(content, list):
                            parts = []
                            for blk in content:
                                if (isinstance(blk, dict)
                                        and blk.get("type") == "text"):
                                    parts.append(blk.get("text") or "")
                            text = "\n".join(parts)
                        text = (text or "").strip()
                        if not text:
                            continue
                        if text.startswith(("<command-", "<system",
                                             "<local-command-",
                                             "[Request",
                                             "Tool result")):
                            continue
                        messages.append({"role": "user", "text": text})
                    elif t == "assistant":
                        msg = evt.get("message") or {}
                        content = msg.get("content")
                        text = ""
                        if isinstance(content, list):
                            parts = []
                            for blk in content:
                                if (isinstance(blk, dict)
                                        and blk.get("type") == "text"):
                                    parts.append(blk.get("text") or "")
                            text = "\n".join(parts)
                        text = (text or "").strip()
                        if not text:
                            continue
                        messages.append({"role": "assistant", "text": text})
        except OSError:
            pass

        # Cap to the most recent N messages — huge sessions otherwise spike
        # memory + render time on the client.
        if limit and len(messages) > limit:
            messages = messages[-limit:]
        return messages

    # ---------- claude code instances (tagged groups) ----------
    def load_claude_groups(self) -> list:
        try:
            return json.loads(CLAUDE_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception:
            return []

    def save_claude_groups(self, groups: list) -> bool:
        try:
            CLAUDE_PATH.write_text(
                json.dumps(groups or [], indent=2), encoding="utf-8"
            )
            return True
        except Exception:
            return False

    def pick_directory(self) -> str | None:
        """Native folder picker — returns selected path or None."""
        if self._window is None:
            return None
        try:
            result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
            if result:
                return result[0] if isinstance(result, (list, tuple)) else str(result)
        except Exception:
            pass
        return None

    def pick_file(self, start_dir: str | None = None) -> str | None:
        """Native file picker — returns absolute path or None.

        Used by the chat composer's `+` button to insert a `@path` reference
        for Claude.
        """
        if self._window is None:
            return None
        try:
            kwargs = {}
            if start_dir and Path(start_dir).is_dir():
                kwargs["directory"] = start_dir
            result = self._window.create_file_dialog(
                webview.OPEN_DIALOG, **kwargs
            )
            if result:
                return result[0] if isinstance(result, (list, tuple)) else str(result)
        except Exception:
            pass
        return None

    # ---------- Git status (composer branch / diff badges) ----------
    def git_status(self, cwd: str) -> dict:
        """Run a few `git` commands in `cwd` and return branch + diff stats
        for the chat composer's branch/diff/PR row.

        Returns `{ok: False, error}` if `cwd` isn't a git repo or git is
        missing, otherwise `{ok: True, branch, base, on_base, added,
        deleted, files_changed, ahead, behind, dirty, has_gh}`.
        """
        if not cwd or not Path(cwd).is_dir():
            return {"ok": False, "error": "bad cwd"}

        import re as _re
        import shutil as _shutil

        def run(args: list[str], timeout: float = 6) -> tuple[int, str]:
            try:
                r = subprocess.run(
                    args, capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                    cwd=cwd, timeout=timeout,
                    creationflags=0x08000000,
                )
                return r.returncode, (r.stdout or "").strip()
            except Exception:
                return -1, ""

        rc, branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        if rc != 0 or not branch:
            return {"ok": False, "error": "not a git repo"}

        # Pick a base branch — main → master → branch itself
        base = None
        for cand in ("main", "master"):
            rc2, _ = run(["git", "rev-parse", "--verify", "--quiet", cand])
            if rc2 == 0:
                base = cand
                break
        if base is None:
            base = branch
        on_base = (branch == base)

        # Diff stats: vs base on a feature branch, else uncommitted
        if on_base:
            _, stat = run(["git", "diff", "--shortstat", "HEAD"])
            if not stat:  # nothing staged or unstaged → also try cached
                _, stat = run(["git", "diff", "--shortstat", "--cached"])
        else:
            _, stat = run(["git", "diff", "--shortstat", f"{base}...HEAD"])

        files = added = deleted = 0
        m = _re.search(r"(\d+)\s+files?\s+changed", stat)
        if m: files = int(m.group(1))
        m = _re.search(r"(\d+)\s+insertions?", stat)
        if m: added = int(m.group(1))
        m = _re.search(r"(\d+)\s+deletions?", stat)
        if m: deleted = int(m.group(1))

        ahead = behind = 0
        if not on_base:
            rc3, ab = run(
                ["git", "rev-list", "--left-right", "--count",
                 f"{base}...HEAD"]
            )
            if rc3 == 0:
                parts = ab.split()
                if len(parts) == 2:
                    try:
                        behind = int(parts[0])
                        ahead = int(parts[1])
                    except ValueError:
                        pass

        _, dirty_out = run(["git", "status", "--porcelain"])
        dirty = bool(dirty_out)

        has_gh = bool(_shutil.which("gh") or _shutil.which("gh.exe"))

        return {
            "ok": True,
            "branch": branch,
            "base": base,
            "on_base": on_base,
            "added": added,
            "deleted": deleted,
            "files_changed": files,
            "ahead": ahead,
            "behind": behind,
            "dirty": dirty,
            "has_gh": has_gh,
        }

    def git_create_pr(self, cwd: str) -> dict:
        """Spawn `gh pr create --web` in `cwd` so the user finishes the PR
        in their browser. Non-blocking — returns immediately."""
        if not cwd or not Path(cwd).is_dir():
            return {"ok": False, "error": "bad cwd"}
        import shutil as _shutil
        gh = _shutil.which("gh") or _shutil.which("gh.exe")
        if not gh:
            return {"ok": False,
                    "error": "GitHub CLI (gh) not found in PATH"}
        try:
            subprocess.Popen(
                [gh, "pr", "create", "--web"],
                cwd=cwd,
                creationflags=0x08000000,
            )
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- Chat-session persistence + new-window helpers ----------
    @staticmethod
    def _load_chat_sessions() -> dict:
        try:
            return json.loads(CHAT_SESSIONS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _save_chat_sessions(d: dict) -> None:
        try:
            CHAT_SESSIONS_PATH.write_text(
                json.dumps(d, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def get_chat_session_id(self, chat_id: str | int) -> str | None:
        return self._load_chat_sessions().get(str(chat_id))

    def update_chat_session_id(self, chat_id: str | int,
                                session_id: str | None) -> dict:
        d = self._load_chat_sessions()
        if session_id:
            d[str(chat_id)] = session_id
        else:
            d.pop(str(chat_id), None)
        self._save_chat_sessions(d)
        return {"ok": True}

    def open_chat_window(self, chat_id: str | int, cwd: str,
                         name: str = "claude",
                         session_id: str | None = None) -> dict:
        """Move the pre-created transparent chat window on-screen, navigate
        it to this session, and show it. Reusing the same window keeps the
        transparent surface working across opens.

        If `session_id` is provided, it's used directly as the prior
        claude session (and persisted for future opens). Otherwise we
        fall back to the saved mapping in `chat_sessions.json`. Passing
        it explicitly avoids a write/read race when the caller just
        called `update_chat_session_id` and then immediately opens.
        """
        if self._chat_window is None:
            return {"ok": False, "error": "chat window not initialised"}

        from urllib.parse import quote
        if session_id:
            self.update_chat_session_id(chat_id, session_id)
            prior = session_id
        else:
            prior = self.get_chat_session_id(chat_id) or ""
        href = str(INDEX_HTML)
        href += (f"#mode=chat&id={quote(str(chat_id))}"
                 f"&cwd={quote(cwd or '')}"
                 f"&name={quote(name or '')}"
                 f"&prior={quote(prior)}")

        # Compute the available rect to the LEFT of the dock. The chrome
        # thread will GetWindowRect() the actual window size after show()
        # and centre within this rect — that way DPI scaling can't push
        # the window past the dock's left edge.
        u32 = windll.user32
        sw = u32.GetSystemMetrics(0)
        l, t, r, b = _get_work_area()
        right_edge = min(r, sw - PANEL_W)  # always sit LEFT of the dock
        avail = (l, t, right_edge, b)

        try:
            self._chat_window.load_url(href)
            self._chat_window.show()
            threading.Thread(
                target=_chrome_chat_window,
                args=(CHAT_WINDOW_TITLE, avail),
                daemon=True,
            ).start()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def close_chat_window(self) -> dict:
        """Hide the chat window AND park it off-screen so transparency is
        preserved for the next open()."""
        if self._chat_window is None:
            return {"ok": False, "error": "chat window not initialised"}
        try:
            self._chat_window.hide()
            try:
                self._chat_window.move(-9999, -9999)
            except Exception:
                pass
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    # ---------- Code session chat (claude -p with session resume) ----------
    def send_code_message(
        self,
        text: str,
        cwd: str,
        prior_claude_session: str | None = None,
    ) -> dict:
        """Send one user message to Claude Code in `cwd`. Returns the full
        assistant reply plus the new claude session id (so the next call can
        --resume the same conversation)."""
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "empty message"}
        if not Path(cwd).is_dir():
            return {"ok": False, "error": f"directory not found: {cwd}"}

        import shutil as _shutil
        claude_exe = (_shutil.which("claude")
                      or _shutil.which("claude.cmd")
                      or _shutil.which("claude.exe"))
        if not claude_exe:
            return {"ok": False, "error": "Claude CLI not found in PATH"}

        cmd = [claude_exe, "-p", text,
               "--output-format", "json",
               "--allowedTools", "mcp__pc-manager"]
        if prior_claude_session:
            cmd += ["--resume", prior_claude_session]

        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                cwd=cwd, timeout=180,
                creationflags=0x08000000,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "claude timed out"}

        if r.returncode != 0:
            return {"ok": False,
                    "error": (r.stderr or "").strip() or f"exit {r.returncode}"}

        # claude -p --output-format json emits one JSON object on stdout
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            return {"ok": False, "error": "couldn't parse claude output"}
        return {
            "ok": True,
            "text": data.get("result", "") or "",
            "claude_session_id": data.get("session_id"),
            "duration_ms": data.get("duration_ms"),
        }

    # ---------- embedded terminals (xterm.js + ConPTY) ----------
    def start_terminal(self, working_dir: str, name: str = "claude") -> dict:
        """Spawn a `claude` ConPTY in working_dir and return its WebSocket URL."""
        if not Path(working_dir).is_dir():
            return {"ok": False, "error": f"directory not found: {working_dir}"}
        try:
            term = ptyterm.spawn_claude(working_dir, name)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {
            "ok": True,
            "id": term.id,
            "name": term.name,
            "cwd": term.cwd,
            "ws_url": f"ws://localhost:{TERMINAL_WS_PORT}/term/{term.id}",
        }

    def list_terminals(self) -> list[dict]:
        return ptyterm.list_all()

    def stop_terminal(self, term_id: str) -> dict:
        term = ptyterm.remove(term_id)
        if not term:
            return {"ok": False, "error": "terminal not found"}
        term.kill()
        return {"ok": True}

    def resize_terminal(self, term_id: str, cols: int, rows: int) -> dict:
        term = ptyterm.get(term_id)
        if not term:
            return {"ok": False, "error": "terminal not found"}
        term.resize(int(cols), int(rows))
        return {"ok": True}

    def launch_claude_instance(self, working_dir: str, name: str = "claude") -> dict:
        """Open a new terminal window in working_dir running `claude`.

        Locates claude.cmd via PATH explicitly so we don't depend on the
        spawned terminal's own PATH/PATHEXT lookup, then routes through
        `cmd /K` so the .cmd shim runs correctly.
        """
        import shutil as _shutil
        if not Path(working_dir).is_dir():
            return {"ok": False, "error": f"directory not found: {working_dir}"}

        claude_exe = (_shutil.which("claude")
                      or _shutil.which("claude.cmd")
                      or _shutil.which("claude.exe"))
        if not claude_exe:
            return {"ok": False,
                    "error": "Claude CLI not found in PATH. Install Claude Code first."}

        try:
            wt = _shutil.which("wt") or _shutil.which("wt.exe")
            if wt:
                # Hand wt a `cmd /K` so the .cmd shim is interpreted properly,
                # and use an explicit path so we don't depend on wt's PATH.
                subprocess.Popen(
                    [wt, "-d", working_dir, "--title", name,
                     "cmd", "/K", claude_exe],
                )
                return {"ok": True, "via": "wt"}
        except (OSError, FileNotFoundError):
            pass  # fall through to cmd

        try:
            subprocess.Popen(
                f'start "{name}" cmd /K "{claude_exe}"',
                cwd=working_dir, shell=True,
            )
            return {"ok": True, "via": "cmd"}
        except Exception as e:
            return {"ok": False, "error": f"launch failed: {e}"}

    # ---------- voice ----------
    def _ensure_va(self):
        if self._va is not None:
            return
        from pc_manager.voice import VoiceAssistant
        self._va = VoiceAssistant(
            on_user=lambda t: self._push_bubble("you", t),
            on_claude=lambda t: self._push_bubble("claude", t),
            on_status=lambda s: self._on_status(s),
        )

    def start_listening(self):
        self._ensure_va()
        self._user_listening = True
        self._push_state({"listening": True})
        try:
            self._va.start_continuous()
        except Exception as e:
            self._on_status(f"err: {e}")

    def stop_listening(self):
        if self._va:
            self._va.stop_continuous()
        self._user_listening = False
        self._push_state({"listening": False})

    def submit_text(self, text: str):
        text = (text or "").strip()
        if not text:
            return
        self._ensure_va()
        self._push_bubble("you", text)
        threading.Thread(
            target=lambda: self._va.process_text(text), daemon=True
        ).start()

    # ---------- push helpers ----------
    # Map a voice-loop status string → (mascot state, status label).
    # Mascot states drive clawd.jsx STATE_ANIMS:
    #   listening = still pose (frame 0)
    #   thinking  = look L↔R (frames 0,4 @ 1fps)
    #   speaking  = fast walk cycle (frames 1,3,5,7 @ 12fps)
    #   happy     = wave both arms (frames 2,6 @ 6fps)
    def _on_status(self, s: str):
        sl = s.lower()
        if "listening" in sl:
            state, label = "listening", "● listening"
        elif "transcrib" in sl:
            state, label = "thinking", "○ transcribing…"
        elif "thinking" in sl:
            state, label = "thinking", "○ thinking…"
        elif "speaking" in sl:
            state, label = "speaking", "◆ speaking"
        elif "ready" in sl:
            state, label = "finish", "🏁 finish"
        elif "stopped" in sl:
            state, label = "idle", "○ idle"
        elif "didn't catch" in sl or "no speech" in sl:
            state, label = "thinking", s
        elif "error" in sl or "fail" in sl:
            state, label = "idle", s
        else:
            state, label = "idle", s
        self._push_state({"voiceState": state, "voiceStatusLabel": label})

    def _push_bubble(self, who: str, text: str):
        self._eval_js(
            "window.PCStore.appendBubble("
            + json.dumps({"who": who, "text": text})
            + ")"
        )

    def _push_state(self, updates: dict):
        self._eval_js("window.PCStore.set(" + json.dumps(updates) + ")")

    def _eval_js(self, code: str):
        if self._window is None:
            return
        try:
            self._window.evaluate_js(code)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Window bootstrap
# ---------------------------------------------------------------------------
def _compute_geometry() -> tuple[int, int, int, int]:
    """(x, y, width, height) in physical pixels — flush against right edge
    of the work area (excludes the taskbar)."""
    l, t, r, b = _get_work_area()
    width  = PANEL_W
    height = b - t
    x = r - width
    y = t
    return x, y, width, height


def _on_loaded(window: webview.Window, api: API):
    """Fires once the DOM is ready. Inject username + initial tab, register AppBar."""
    api._bind_window(window)
    user = os.environ.get("USERNAME", "you")
    initial_tab = os.environ.get("PC_MANAGER_INITIAL_TAB", "").lower()
    try:
        window.evaluate_js(f"window.PC_USER = {json.dumps(user)};")
        if initial_tab:
            window.evaluate_js(
                f"window.PC_INITIAL_TAB = {json.dumps(initial_tab)};"
            )
    except Exception:
        pass
    threading.Thread(target=_register_appbar_when_ready,
                     args=(window,), daemon=True).start()


def _find_hwnd_by_title(title: str) -> int:
    """Find the topmost window with this title (our pywebview window)."""
    user32 = ctypes.windll.user32
    EnumWindows = user32.EnumWindows
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, wintypes.HWND, wintypes.LPARAM
    )
    found = []

    def cb(hwnd, _lparam):
        n = user32.GetWindowTextLengthW(hwnd)
        if n > 0:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value == title:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, byref(pid))
                if pid.value == os.getpid():
                    found.append(hwnd)
                    return False
        return True

    EnumWindows(EnumWindowsProc(cb), 0)
    return found[0] if found else 0


def _chrome_chat_window(title: str,
                         avail: tuple[int, int, int, int] | None = None) -> None:
    """Wait for a window with `title` to appear, then give it rounded corners,
    an acrylic backdrop, and (optionally) center it within the available rect
    `avail = (left, top, right, bottom)` — usually the work-area to the LEFT
    of the dock.

    We center based on the window's *actual* rendered size (queried via
    GetWindowRect after `show()`), not the size we passed to pywebview's
    create_window. pywebview / WebView2 / WinForms layer DPI scaling on
    top of those values, so a value of 1100 logical can render at 1375
    physical on a 125%-scaled display, which would push the window past
    the dock's left edge.
    """
    user32 = windll.user32
    DWMWA_WINDOW_CORNER_PREFERENCE = 33
    DWMWCP_ROUND = 2  # large round (Win11)
    DWMWA_SYSTEMBACKDROP_TYPE = 38
    DWMSBT_TRANSIENTWINDOW = 3  # Win11 acrylic
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    SWP_NOSIZE = 0x0001
    for _ in range(60):
        time.sleep(0.05)
        hwnd = _find_hwnd_by_title(title)
        if not hwnd:
            continue
        # Move (no resize — preserves the transparent WebView2 backbuffer)
        # using the window's ACTUAL pixel size, centred inside `avail`.
        if avail is not None:
            try:
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, byref(rect))
                actual_w = rect.right - rect.left
                actual_h = rect.bottom - rect.top
                al, at, ar, ab = avail
                aw, ah = ar - al, ab - at
                gx = al + max(0, (aw - actual_w) // 2)
                gy = at + max(0, (ah - actual_h) // 2)
                user32.SetWindowPos(
                    hwnd, 0, gx, gy, 0, 0,
                    SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOSIZE,
                )
            except Exception:
                pass
        # Win11 rounded corners — works regardless of WS style
        try:
            v = ctypes.c_int(DWMWCP_ROUND)
            windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(v), ctypes.sizeof(v),
            )
        except Exception:
            pass
        # Win11 system acrylic backdrop (preferred over our manual SetWCA)
        try:
            v2 = ctypes.c_int(DWMSBT_TRANSIENTWINDOW)
            windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_SYSTEMBACKDROP_TYPE,
                ctypes.byref(v2), ctypes.sizeof(v2),
            )
        except Exception:
            pass
        # Fallback to legacy SetWindowCompositionAttribute acrylic on older builds
        try:
            _apply_acrylic(hwnd, gradient_color=0xC0_1A_0E_0A)
        except Exception:
            pass
        return


def _register_appbar_when_ready(window: webview.Window):
    """Find our HWND, register the appbar, snap the window to its rect, and
    apply DWM acrylic + rounded corners so the dock matches the chat window."""
    user32 = windll.user32
    DWMWA_WINDOW_CORNER_PREFERENCE = 33
    DWMWCP_ROUND = 2
    DWMWA_SYSTEMBACKDROP_TYPE = 38
    DWMSBT_TRANSIENTWINDOW = 3
    for _ in range(60):  # up to ~6s while WebView2 boots
        try:
            hwnd = _find_hwnd_by_title("Side Dock")
            if hwnd:
                abd = _appbar_register(hwnd, ABE_RIGHT, PANEL_W)
                if abd:
                    window._appbar_data = abd
                    # MoveWindow into the rect the OS just allocated for us —
                    # this is the actual snap. Without it the window stays
                    # wherever pywebview placed it on creation.
                    user32.MoveWindow(
                        hwnd,
                        abd.rc.left, abd.rc.top,
                        abd.rc.right - abd.rc.left,
                        abd.rc.bottom - abd.rc.top,
                        True,
                    )
                # Win11 rounded corners
                try:
                    v = ctypes.c_int(DWMWCP_ROUND)
                    windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
                        ctypes.byref(v), ctypes.sizeof(v),
                    )
                except Exception:
                    pass
                # Win11 system acrylic backdrop — desktop blur shows through
                try:
                    v2 = ctypes.c_int(DWMSBT_TRANSIENTWINDOW)
                    windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, DWMWA_SYSTEMBACKDROP_TYPE,
                        ctypes.byref(v2), ctypes.sizeof(v2),
                    )
                except Exception:
                    pass
                # Legacy SetWindowCompositionAttribute fallback (older Win10)
                try:
                    _apply_acrylic(hwnd, gradient_color=0x80_1A_0E_0A)
                except Exception:
                    pass
                return
        except Exception:
            pass
        time.sleep(0.1)


def _on_closing(window: webview.Window):
    abd = getattr(window, "_appbar_data", None)
    _appbar_unregister(abd)
    # Clean up any running ConPTY terminals
    try:
        ptyterm.shutdown_all()
    except Exception:
        pass
    # Tear down the chat window too — otherwise it's left orphaned with a
    # dead WS server, showing a transparent black surface forever.
    api = getattr(window, "_api_ref", None)
    chat_w = getattr(api, "_chat_window", None) if api else None
    if chat_w is not None:
        try:
            chat_w.destroy()
        except Exception:
            try:
                chat_w.hide()
            except Exception:
                pass


def _summarize_tools(tools: list[dict]) -> str:
    """Build a compact "Edited a file, ran 2 commands" line from a list
    of tool_use blocks: each block is `{name, input}`."""
    if not tools:
        return ""
    counts: dict[str, int] = {}
    for t in tools:
        counts[t.get("name", "?")] = counts.get(t.get("name", "?"), 0) + 1
    parts: list[str] = []
    edit_n = (counts.get("Edit", 0) + counts.get("Write", 0)
              + counts.get("MultiEdit", 0) + counts.get("NotebookEdit", 0))
    if edit_n:
        parts.append(f"Edited {edit_n} file{'s' if edit_n > 1 else ''}")
    read_n = counts.get("Read", 0)
    if read_n:
        parts.append(f"Read {read_n} file{'s' if read_n > 1 else ''}")
    bash_n = counts.get("Bash", 0)
    if bash_n:
        parts.append(f"Ran {bash_n} command{'s' if bash_n > 1 else ''}")
    search_n = counts.get("Grep", 0) + counts.get("Glob", 0)
    if search_n:
        parts.append(f"Searched {search_n} time{'s' if search_n > 1 else ''}")
    web_n = counts.get("WebSearch", 0) + counts.get("WebFetch", 0)
    if web_n:
        parts.append(f"Web {web_n} time{'s' if web_n > 1 else ''}")
    if counts.get("TodoWrite", 0):
        parts.append("Updated todos")
    if counts.get("Task", 0):
        parts.append(f"Spawned {counts['Task']} sub-agent{'s' if counts['Task'] > 1 else ''}")
    handled = {
        "Edit", "Write", "MultiEdit", "NotebookEdit",
        "Read", "Bash", "Grep", "Glob",
        "WebSearch", "WebFetch", "TodoWrite", "Task",
    }
    other = sum(c for n, c in counts.items() if n not in handled)
    if other:
        parts.append(f"Used {other} other tool{'s' if other > 1 else ''}")
    if not parts:
        n = len(tools)
        return f"Used {n} tool{'s' if n > 1 else ''}"
    if len(parts) == 1:
        return parts[0]
    return parts[0] + ", " + ", ".join(p.lower() for p in parts[1:])


def _format_tool_detail(t: dict) -> str:
    """One-line readable description of a single tool_use block."""
    name = t.get("name", "?")
    inp = t.get("input") or {}
    if not isinstance(inp, dict):
        inp = {}
    if name in ("Edit", "Write", "MultiEdit"):
        path = inp.get("file_path", "") or ""
        base = Path(path).name if path else "?"
        return f"{name}  {base}"
    if name == "Read":
        path = inp.get("file_path", "") or ""
        return f"Read  {Path(path).name if path else '?'}"
    if name == "Bash":
        cmd = (inp.get("command") or "").strip().replace("\n", " ⏎ ")
        if len(cmd) > 110:
            cmd = cmd[:110] + "…"
        return f"$ {cmd}"
    if name == "Grep":
        return f"grep  {(inp.get('pattern') or '')[:80]}"
    if name == "Glob":
        return f"glob  {(inp.get('pattern') or '')[:80]}"
    if name == "WebFetch":
        return f"fetch  {(inp.get('url') or '')[:80]}"
    if name == "WebSearch":
        return f"search  {(inp.get('query') or '')[:80]}"
    if name == "TodoWrite":
        todos = inp.get("todos") or []
        return f"TodoWrite  {len(todos)} item{'s' if len(todos) != 1 else ''}"
    if name == "Task":
        desc = (inp.get("description") or "").strip()
        return f"Task  {desc[:80] if desc else 'sub-agent'}"
    if name.startswith("mcp__"):
        return f"mcp  {name[5:]}"
    desc = (inp.get("description") or "").strip()
    return f"{name}{' — ' + desc[:80] if desc else ''}"


def _start_terminal_ws_server() -> None:
    """Run a localhost-only WebSocket server bridging two kinds of streams:

      /term/<id>  — xterm.js ↔ ConPTY (binary both ways)
      /chat/<id>  — Code-tab chat: JSON control messages + streaming claude -p
                    output deltas

    Both share a single asyncio loop on a background thread.
    """
    import asyncio
    import re
    import websockets
    import shutil as _shutil

    # UUIDv4 (or any safe alphanumeric+hyphen) — guards path-traversal in
    # /history/<sid> and /chat/<sid> WS endpoints.
    _SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

    # In-flight chat sessions — second connection to /chat/<sid> while one
    # is already streaming gets rejected, so two browser tabs can't double-
    # spawn `claude -p --resume` and corrupt the shared JSONL.
    _active_chats: set[str] = set()

    # ---- /term/<id> handler (existing) ----
    async def term_handler(ws, term_id: str):
        term = ptyterm.get(term_id)
        if term is None:
            await ws.close(code=1008, reason="unknown terminal")
            return

        loop = asyncio.get_event_loop()
        stop = asyncio.Event()

        async def pump_out():
            while not stop.is_set() and term.alive:
                data = await loop.run_in_executor(None, term.read_blocking, 4096)
                if not data:
                    if not term.alive: break
                    await asyncio.sleep(0.02); continue
                try: await ws.send(data)
                except Exception: break
            try: await ws.close()
            except Exception: pass

        out_task = asyncio.create_task(pump_out())
        try:
            async for msg in ws:
                if isinstance(msg, str) and msg.startswith("\x1b!"):
                    body = msg[2:]
                    if body.startswith("resize:"):
                        try:
                            cols, rows = body[7:].split(",")
                            term.resize(int(cols), int(rows))
                        except Exception: pass
                    continue
                if isinstance(msg, (bytes, bytearray)): term.write(bytes(msg))
                else: term.write(msg)
        except Exception: pass
        finally:
            stop.set(); out_task.cancel()

    # ---- /chat/<id> handler — streams claude -p output ----
    async def chat_stream_one(ws, text, cwd, prior_session_id,
                              accept_edits=False, model=None):
        claude_exe = (_shutil.which("claude")
                      or _shutil.which("claude.cmd")
                      or _shutil.which("claude.exe"))
        if not claude_exe:
            await ws.send(json.dumps({"type": "error",
                                     "error": "claude CLI not found"}))
            return
        cmd = [claude_exe, "-p", text,
               "--output-format", "stream-json",
               "--include-partial-messages",
               "--verbose",
               "--allowedTools", "mcp__pc-manager"]
        if prior_session_id:
            cmd += ["--resume", prior_session_id]
        if accept_edits:
            cmd += ["--permission-mode", "acceptEdits"]
        if model:
            cmd += ["--model", model]
        try:
            # CREATE_NO_WINDOW (0x08000000) suppresses the cmd.exe console
            # that would otherwise pop while claude.cmd is streaming —
            # asyncio's subprocess defaults inherit the parent's console
            # otherwise and on a pythonw host that means a fresh window.
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                creationflags=0x08000000,
            )
        except (OSError, ValueError, FileNotFoundError) as e:
            try:
                await ws.send(json.dumps({
                    "type": "error",
                    "error": f"failed to spawn claude: {e}",
                }))
            except Exception:
                pass
            return

        try:
            await ws.send(json.dumps({"type": "started"}))
        except Exception:
            # Client gone before we even started — kill the proc and bail.
            try:
                proc.kill()
            except Exception:
                pass
            return

        session_id = None
        duration_ms = None
        output_tokens = None
        saw_any_output = False
        result_error = None
        ws_alive = True
        # 5 minutes — claude -p with a long prompt + tool turns can legitimately
        # be silent for a while. Anything past 5 min indicates a hang (waiting
        # on stdin, network, etc.) and we give up.
        READ_TIMEOUT_S = 300

        async def _send(payload: dict) -> bool:
            nonlocal ws_alive
            if not ws_alive:
                return False
            try:
                await ws.send(json.dumps(payload))
                return True
            except Exception:
                ws_alive = False
                return False

        try:
            while True:
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=READ_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    await _send({
                        "type": "error",
                        "error": (
                            "claude -p is unresponsive (no output for 5m); "
                            "killing"
                        ),
                    })
                    break
                if not line:
                    break
                line = line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                saw_any_output = True
                # Capture session id at init
                if evt.get("type") == "system" and evt.get("subtype") == "init":
                    session_id = evt.get("session_id") or session_id
                    if session_id:
                        await _send({
                            "type": "session", "session_id": session_id,
                        })
                # Stream text deltas (partial assistant messages)
                elif evt.get("type") == "stream_event":
                    inner = evt.get("event") or {}
                    if inner.get("type") == "content_block_delta":
                        d = inner.get("delta") or {}
                        if d.get("type") == "text_delta":
                            t = d.get("text") or ""
                            if t:
                                await _send({"type": "delta", "text": t})
                # Assembled assistant message — emit text deltas (fallback
                # if streaming partials weren't seen) and a single
                # collapsible "actions" summary for any tool_use blocks.
                elif evt.get("type") == "assistant":
                    msg = evt.get("message") or {}
                    text_parts: list[str] = []
                    tools_in_turn: list[dict] = []
                    for blk in msg.get("content") or []:
                        if not isinstance(blk, dict):
                            continue
                        bt = blk.get("type")
                        if bt == "text":
                            t_text = blk.get("text") or ""
                            if t_text:
                                text_parts.append(t_text)
                        elif bt == "tool_use":
                            tools_in_turn.append({
                                "name": blk.get("name") or "?",
                                "input": blk.get("input") or {},
                            })
                    if text_parts:
                        await _send({
                            "type": "delta", "text": "".join(text_parts),
                        })
                    if tools_in_turn:
                        await _send({
                            "type": "actions",
                            "summary": _summarize_tools(tools_in_turn),
                            "details": [
                                _format_tool_detail(t) for t in tools_in_turn
                            ],
                        })
                elif evt.get("type") == "result":
                    session_id = evt.get("session_id") or session_id
                    duration_ms = evt.get("duration_ms") or duration_ms
                    usage = evt.get("usage") or {}
                    output_tokens = (
                        usage.get("output_tokens")
                        or output_tokens
                    )
                    if evt.get("is_error"):
                        result_error = (
                            evt.get("result")
                            or evt.get("error_message")
                            or "claude reported an error"
                        )
                # Client gone — stop reading and let `finally` clean up.
                if not ws_alive:
                    break
            # If claude exited with non-zero and never streamed anything,
            # surface stderr so the user sees what went wrong (most common
            # cause: --resume on a session ID this CLI version can't load).
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            if (proc.returncode and proc.returncode != 0) or not saw_any_output:
                stderr_data = b""
                try:
                    stderr_data = await asyncio.wait_for(
                        proc.stderr.read(), timeout=2,
                    )
                except Exception:
                    pass
                err_text = stderr_data.decode(
                    "utf-8", errors="replace",
                ).strip()
                if not err_text and result_error:
                    err_text = result_error
                if not err_text:
                    err_text = (
                        f"claude exited with code {proc.returncode} "
                        "and no output"
                    )
                await _send({"type": "error", "error": err_text[:1500]})
            elif result_error:
                await _send({"type": "error", "error": result_error[:1500]})
        finally:
            # Always reap the subprocess — orphaned `claude` procs hold a
            # session token and lock the cwd's JSONL.
            if proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except Exception:
                    pass
            await _send({
                "type": "done",
                "session_id": session_id,
                "duration_ms": duration_ms,
                "output_tokens": output_tokens,
            })

    async def chat_handler(ws, chat_id: str):
        # Reject obviously unsafe chat ids (we don't read paths off it, but
        # we tag in-flight state with it — keep it sane).
        if not _SESSION_ID_RE.fullmatch(chat_id or ""):
            try:
                await ws.send(json.dumps({
                    "type": "error", "error": "invalid chat id",
                }))
            except Exception:
                pass
            return
        # First connection wins — second tab to /chat/<same-id> gets a
        # clear error rather than silently double-spawning `claude -p`.
        if chat_id in _active_chats:
            try:
                await ws.send(json.dumps({
                    "type": "error",
                    "error": (
                        "another window is already chatting with this "
                        "session — close it and retry"
                    ),
                }))
            except Exception:
                pass
            return
        _active_chats.add(chat_id)
        try:
            async for msg in ws:
                if not isinstance(msg, str):
                    continue
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    continue
                if data.get("type") != "msg":
                    continue
                text = (data.get("text") or "").strip()
                cwd = data.get("cwd") or "."
                prior = data.get("prior_session_id")
                accept_edits = bool(data.get("accept_edits"))
                model = data.get("model") or None
                if not text:
                    continue
                # Validate cwd before spawning. is_dir() also catches paths
                # to disconnected network drives that would crash later.
                try:
                    if not cwd or not Path(cwd).is_dir():
                        await ws.send(json.dumps({
                            "type": "error", "error": f"bad cwd: {cwd}",
                        }))
                        continue
                except OSError as e:
                    await ws.send(json.dumps({
                        "type": "error", "error": f"cwd unreachable: {e}",
                    }))
                    continue
                await chat_stream_one(
                    ws, text, cwd, prior, accept_edits, model
                )
        except Exception:
            pass
        finally:
            _active_chats.discard(chat_id)

    # ---- /history/<session_id> handler — replays a Claude Code session ----
    async def history_handler(ws, session_id: str):
        """One-shot: return the parsed user/assistant messages of a Claude
        Code session JSONL, then close.

        Lives on the WebSocket bridge (instead of pywebview's JSON-RPC)
        because the chat window's RPC channel is flaky after `load_url`.
        """
        # Reject anything that doesn't look like a UUID — blocks `..\\..\\`
        # path-traversal attempts via the URL fragment.
        if not _SESSION_ID_RE.fullmatch(session_id or ""):
            try:
                await ws.send(json.dumps({
                    "type": "error", "error": "invalid session id",
                }))
            except Exception:
                pass
            return

        base = Path.home() / ".claude" / "projects"
        if not base.is_dir():
            try:
                await ws.send(json.dumps({"type": "error",
                                          "error": "no projects dir"}))
            except Exception:
                pass
            return

        jsonl: Path | None = None
        for proj in base.iterdir():
            if not proj.is_dir():
                continue
            cand = proj / f"{session_id}.jsonl"
            # Defense-in-depth: ensure resolved path stays under `base` even
            # if the regex above is ever loosened.
            try:
                if cand.exists() and cand.resolve().is_relative_to(
                    base.resolve()
                ):
                    jsonl = cand
                    break
            except (OSError, ValueError):
                continue
        if jsonl is None:
            try:
                await ws.send(json.dumps({
                    "type": "error", "error": "session not found",
                }))
            except Exception:
                pass
            return

        messages: list[dict] = []
        loop = asyncio.get_event_loop()

        def parse_jsonl() -> list[dict]:
            out: list[dict] = []
            try:
                with jsonl.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        try:
                            evt = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        t = evt.get("type")
                        if t == "user":
                            msg = evt.get("message") or {}
                            content = msg.get("content")
                            text = ""
                            if isinstance(content, str):
                                text = content
                            elif isinstance(content, list):
                                parts = []
                                for blk in content:
                                    if (isinstance(blk, dict)
                                            and blk.get("type") == "text"):
                                        parts.append(blk.get("text") or "")
                                text = "\n".join(parts)
                            text = (text or "").strip()
                            if not text:
                                continue
                            if text.startswith(
                                ("<command-", "<system", "<local-command-",
                                 "[Request", "Tool result"),
                            ):
                                continue
                            out.append({"role": "user", "text": text})
                        elif t == "assistant":
                            msg = evt.get("message") or {}
                            content = msg.get("content")
                            text_parts: list[str] = []
                            tools: list[dict] = []
                            if isinstance(content, list):
                                for blk in content:
                                    if not isinstance(blk, dict):
                                        continue
                                    bt = blk.get("type")
                                    if bt == "text":
                                        tx = blk.get("text") or ""
                                        if tx.strip():
                                            text_parts.append(tx)
                                    elif bt == "tool_use":
                                        tools.append({
                                            "name": blk.get("name") or "?",
                                            "input": blk.get("input") or {},
                                        })
                            text = "\n".join(text_parts).strip()
                            if text:
                                out.append({"role": "assistant", "text": text})
                            if tools:
                                out.append({
                                    "role": "actions",
                                    "summary": _summarize_tools(tools),
                                    "details": [_format_tool_detail(t) for t in tools],
                                })
            except OSError:
                pass
            # Cap by user-turn count, not flat row count: long agentic
            # sessions emit many `actions` rows per assistant turn, so a
            # flat 200-cap drops user messages disproportionately.
            user_turns_cap = 80
            user_count = 0
            for r in reversed(out):
                user_count += 1 if r["role"] == "user" else 0
                if user_count > user_turns_cap:
                    out = out[out.index(r) + 1:]
                    break
            # Per-message text truncation — caps frame size so a session
            # with a giant pasted log can't blow past max_size=4 MB.
            MAX_TEXT = 16_000
            for r in out:
                if "text" in r and isinstance(r["text"], str) \
                        and len(r["text"]) > MAX_TEXT:
                    r["text"] = r["text"][:MAX_TEXT] + "\n… [truncated]"
                if r.get("role") == "actions":
                    details = r.get("details") or []
                    if len(details) > 50:
                        r["details"] = details[:50] + [
                            f"… +{len(details) - 50} more",
                        ]
            return out

        try:
            messages = await loop.run_in_executor(None, parse_jsonl)
        except Exception as e:
            try:
                await ws.send(json.dumps({"type": "error", "error": str(e)}))
            except Exception:
                pass
            return

        # Defense-in-depth: if the serialized payload still exceeds the
        # WS frame budget (very long convo with many short messages), keep
        # halving the message count until it fits.
        try:
            payload = json.dumps({"type": "messages", "messages": messages})
            while len(payload) > 3_500_000 and len(messages) > 4:
                messages = messages[len(messages) // 2:]
                payload = json.dumps({
                    "type": "messages", "messages": messages,
                })
            await ws.send(payload)
        except Exception:
            pass

    # ---- Top-level router ----
    async def router(ws, path=None):
        full_path = path if path is not None else getattr(
            getattr(ws, "request", None), "path", "/"
        )
        parts = full_path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "term":
            return await term_handler(ws, parts[1])
        if len(parts) == 2 and parts[0] == "chat":
            return await chat_handler(ws, parts[1])
        if len(parts) == 2 and parts[0] == "history":
            return await history_handler(ws, parts[1])
        await ws.close(code=1008, reason="bad path")

    async def main():
        async with websockets.serve(
            router, "127.0.0.1", TERMINAL_WS_PORT, max_size=2**22
        ):
            await asyncio.Future()

    def run():
        try:
            asyncio.run(main())
        except OSError as e:
            # Port already bound — second dock instance, dev tool on 7654,
            # or stale pythonw still holding it. Stamp a global flag the
            # UI layer can surface to the user instead of silently failing
            # every WebSocket feature.
            try:
                import pc_manager.dock as _self
                _self.WS_SERVER_ERROR = (
                    f"Dock WebSocket port {TERMINAL_WS_PORT} is in use "
                    f"({e}). Close the other instance or change the port."
                )
            except Exception:
                pass
        except Exception as e:
            try:
                import pc_manager.dock as _self
                _self.WS_SERVER_ERROR = f"Dock WebSocket server crashed: {e}"
            except Exception:
                pass

    threading.Thread(target=run, daemon=True, name="DockWS").start()


def main():
    _start_terminal_ws_server()
    api = API()
    x, y, w, h = _compute_geometry()
    # Initial tab via URL hash (read by app.jsx before React mounts).
    initial_tab = os.environ.get("PC_MANAGER_INITIAL_TAB", "").strip()
    url = str(INDEX_HTML)
    if initial_tab:
        url = f"{url}#tab={initial_tab}"

    # Pre-create the chat window with transparent=True NOW (works only for
    # windows constructed before webview.start). Park it off-screen + hidden
    # so nothing flashes on boot. open_chat_window() moves it on-screen and
    # navigates to the real session URL.
    #
    # NOTE: the URL must live under the same WEB_DIR as the dock window —
    # pywebview's HTTP server runs `os.path.commonpath()` over every window's
    # initial URL and crashes on mixed absolute/blank paths. Pointing at the
    # same index.html with `#mode=chat&pending=1` makes the page render an
    # invisible placeholder until load_url() swaps in real session data.
    # Fix the chat window's surface size at create time and never resize
    # it later — WebView2's transparent backbuffer only survives if the
    # surface dimensions stay constant. Resizing post-show() repaints the
    # whole control opaque white, killing the frosted-glass effect.
    chat_window = webview.create_window(
        title=CHAT_WINDOW_TITLE,
        url=str(INDEX_HTML) + "#mode=chat&pending=1",
        width=CHAT_WINDOW_W, height=CHAT_WINDOW_H,
        x=-9999, y=-9999,
        resizable=True,
        frameless=True,
        transparent=True,
        on_top=False,
        hidden=True,
        js_api=api,
    )
    api._bind_chat_window(chat_window)

    # Pre-created task editor window — same transparent treatment
    task_window = webview.create_window(
        title=TASK_WINDOW_TITLE,
        url=str(INDEX_HTML) + "#mode=task&pending=1",
        width=TASK_WINDOW_W, height=TASK_WINDOW_H,
        x=-9999, y=-9999,
        resizable=True,
        frameless=True,
        transparent=True,
        on_top=False,
        hidden=True,
        js_api=api,
    )
    api._bind_task_window(task_window)

    window = webview.create_window(
        title="Side Dock",
        url=url,
        js_api=api,
        width=w, height=h, x=x, y=y,
        resizable=False,
        frameless=True,
        easy_drag=False,        # we use CSS -webkit-app-region: drag instead
        # Opaque dark backing — `transparent=True` here paints an
        # uncomposited white vertical strip on the LEFT edge that survives
        # any AppBar / DWM / CSS workaround we've tried (WebView2's
        # transparent backbuffer drifts out of sync over a long-lived
        # full-height, complex-DOM window). Chat window stays transparent
        # because its rendering path is simpler.
        background_color="#0a0e1a",
        on_top=False,           # appbar reserves space, no need to force on top
    )
    # Stash the api on the dock window so _on_closing can reach the chat
    # window via api._chat_window and tear it down in unison.
    window._api_ref = api
    window.events.loaded   += lambda: _on_loaded(window, api)
    window.events.closing  += lambda: _on_closing(window)

    # Use Edge WebView2 (gui="edgechromium") so backdrop-filter and modern CSS
    # work. webview.start picks this automatically on Windows when available.
    # Set PC_MANAGER_DEBUG=1 to enable DevTools (F12 in the dock window)
    webview.start(debug=os.environ.get("PC_MANAGER_DEBUG") == "1")


if __name__ == "__main__":
    main()
