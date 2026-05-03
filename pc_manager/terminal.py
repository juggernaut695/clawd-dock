"""ConPTY-based terminals that the dock's xterm.js front-end attaches to.

Each `Terminal` wraps a `winpty.PtyProcess`. A WebSocket server (in dock.py)
exposes them at ws://localhost:PORT/term/<id>; bytes flow both ways.
"""
from __future__ import annotations

import shutil
import threading
import time
import uuid
from typing import Optional

import winpty


class Terminal:
    """A live ConPTY child process, identified by `id`."""
    def __init__(
        self,
        command: str,
        cwd: str | None = None,
        cols: int = 100,
        rows: int = 30,
        name: str = "",
    ):
        self.id = uuid.uuid4().hex[:12]
        self.command = command
        self.cwd = cwd
        self.cols = cols
        self.rows = rows
        self.name = name or "shell"
        self._proc: Optional[winpty.PtyProcess] = None
        self._lock = threading.Lock()
        self.created_at = time.time()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.isalive()

    def spawn(self) -> None:
        with self._lock:
            self._proc = winpty.PtyProcess.spawn(
                self.command,
                cwd=self.cwd,
                dimensions=(self.rows, self.cols),
            )

    def read_blocking(self, n: int = 4096, timeout: float = 0.05) -> bytes:
        """Read up to n bytes. Returns b'' on no-data, b'' also if closed."""
        if not self._proc:
            return b""
        try:
            data = self._proc.read(n)
        except EOFError:
            return b""
        except OSError:
            return b""
        if data is None:
            return b""
        if isinstance(data, str):
            return data.encode("utf-8", errors="replace")
        return data

    def write(self, data: bytes | str) -> None:
        if not self._proc:
            return
        if isinstance(data, bytes):
            try:
                data = data.decode("utf-8", errors="replace")
            except Exception:
                return
        try:
            self._proc.write(data)
        except (OSError, EOFError):
            pass

    def resize(self, cols: int, rows: int) -> None:
        self.cols = max(20, cols)
        self.rows = max(5, rows)
        if self._proc:
            try:
                self._proc.setwinsize(self.rows, self.cols)
            except Exception:
                pass

    def kill(self) -> None:
        if self._proc:
            try:
                self._proc.terminate(force=True)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Global registry — terminals by id
# ---------------------------------------------------------------------------
_terminals: dict[str, Terminal] = {}
_registry_lock = threading.Lock()


def register(term: Terminal) -> None:
    with _registry_lock:
        _terminals[term.id] = term


def get(term_id: str) -> Terminal | None:
    return _terminals.get(term_id)


def remove(term_id: str) -> Terminal | None:
    with _registry_lock:
        return _terminals.pop(term_id, None)


def list_all() -> list[dict]:
    with _registry_lock:
        return [
            {"id": t.id, "name": t.name, "cwd": t.cwd, "alive": t.alive}
            for t in _terminals.values()
        ]


def shutdown_all() -> None:
    with _registry_lock:
        for t in list(_terminals.values()):
            t.kill()
        _terminals.clear()


# ---------------------------------------------------------------------------
# Convenience: spawn `claude` in a working dir
# ---------------------------------------------------------------------------
def spawn_claude(working_dir: str, name: str = "claude") -> Terminal:
    """Create a Terminal running the claude.cmd shim under cmd.exe."""
    claude_exe = (
        shutil.which("claude")
        or shutil.which("claude.cmd")
        or shutil.which("claude.exe")
    )
    if not claude_exe:
        raise RuntimeError("Claude CLI not found in PATH")
    # cmd /K so the .cmd shim runs and the shell stays alive after claude exits
    cmd = f'cmd.exe /K "{claude_exe}"'
    term = Terminal(cmd, cwd=working_dir, name=name)
    term.spawn()
    register(term)
    return term
