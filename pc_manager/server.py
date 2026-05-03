"""MCP server exposing PC management tools to Claude."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from pc_manager import monitor, ram_clear

mcp = FastMCP("pc-manager")


# ---------------------------------------------------------------------------
# Shared persistence — same JSON files the dock UI reads/writes
# ---------------------------------------------------------------------------
TASKS_PATH  = Path.home() / ".pc-manager-tasks.json"
CLAUDE_PATH = Path.home() / ".pc-manager-claude.json"
MEMORY_PATH = Path.home() / ".pc-manager-memory.md"


def _load_json(path: Path) -> list:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    except Exception:
        return []


def _save_json(path: Path, data: list) -> bool:
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def _new_id() -> int:
    return int(time.time() * 1000)


@mcp.tool()
def get_system_stats() -> dict[str, Any]:
    """Snapshot of CPU, RAM, GPU(s), disk, network, battery, and uptime.

    Returns a single dict with all current system stats. Use this for
    a quick "how is my PC doing right now?" overview.
    """
    return monitor.get_snapshot()


@mcp.tool()
def get_cpu_stats() -> dict[str, Any]:
    """Detailed CPU info: overall %, per-core %, frequency, core counts."""
    return monitor.get_cpu_stats()


@mcp.tool()
def get_ram_stats() -> dict[str, Any]:
    """RAM and swap usage in GB and percent."""
    return monitor.get_ram_stats()


@mcp.tool()
def get_gpu_stats() -> list[dict[str, Any]]:
    """Per-GPU stats: util, VRAM, temp, power draw (NVIDIA only).

    Returns an empty list on machines without a supported GPU.
    """
    return monitor.get_gpu_stats()


@mcp.tool()
def get_disk_stats() -> list[dict[str, Any]]:
    """Per-partition disk usage in GB and percent."""
    return monitor.get_disk_stats()


@mcp.tool()
def get_top_processes(by: str = "ram", limit: int = 10) -> list[dict[str, Any]]:
    """Top processes ordered by 'ram' or 'cpu'.

    Args:
        by: 'ram' (default) or 'cpu'.
        limit: max number of processes to return (default 10).
    """
    if by not in ("ram", "cpu"):
        return [{"error": "by must be 'ram' or 'cpu'"}]
    return monitor.get_top_processes(by=by, limit=limit)


@mcp.tool()
def clear_ram() -> dict[str, Any]:
    """Free RAM by trimming working sets of all accessible processes.

    Calls Windows EmptyWorkingSet on each process, moving in-use pages
    to the standby list. Returns before/after RAM usage and freed MB.
    """
    return ram_clear.trim_working_sets()


@mcp.tool()
def kill_process(pid: int) -> dict[str, Any]:
    """Terminate a process by its PID. Returns {ok, name} or {ok: False, error}."""
    return ram_clear.kill_process(pid)


# ---------------------------------------------------------------------------
# Tasks (the user's checklist in the Side Dock Tasks tab)
# ---------------------------------------------------------------------------
@mcp.tool()
def list_tasks() -> list[dict[str, Any]]:
    """List every task in the user's checklist (each: id, text, done)."""
    return _load_json(TASKS_PATH)


@mcp.tool()
def add_task(text: str) -> dict[str, Any]:
    """Add a new task to the checklist. Returns the created task."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty task text"}
    tasks = _load_json(TASKS_PATH)
    new = {"id": _new_id(), "text": text, "done": False}
    tasks.append(new)
    _save_json(TASKS_PATH, tasks)
    return {"ok": True, "task": new}


@mcp.tool()
def set_task_done(task_id: int, done: bool = True) -> dict[str, Any]:
    """Mark a task as completed (done=True) or pending (done=False)."""
    tasks = _load_json(TASKS_PATH)
    for t in tasks:
        if t.get("id") == task_id:
            t["done"] = bool(done)
            _save_json(TASKS_PATH, tasks)
            return {"ok": True, "task": t}
    return {"ok": False, "error": f"task {task_id} not found"}


@mcp.tool()
def delete_task(task_id: int) -> dict[str, Any]:
    """Remove a task from the checklist by ID."""
    tasks = _load_json(TASKS_PATH)
    remaining = [t for t in tasks if t.get("id") != task_id]
    if len(remaining) == len(tasks):
        return {"ok": False, "error": f"task {task_id} not found"}
    _save_json(TASKS_PATH, remaining)
    return {"ok": True, "deleted": task_id}


@mcp.tool()
def clear_completed_tasks() -> dict[str, Any]:
    """Remove all completed (done=True) tasks. Returns {ok, removed}."""
    tasks = _load_json(TASKS_PATH)
    remaining = [t for t in tasks if not t.get("done")]
    removed = len(tasks) - len(remaining)
    _save_json(TASKS_PATH, remaining)
    return {"ok": True, "removed": removed}


# ---------------------------------------------------------------------------
# Claude Code instances — tagged groups of working directories the user can
# launch a fresh `claude` session in
# ---------------------------------------------------------------------------
@mcp.tool()
def list_claude_groups() -> list[dict[str, Any]]:
    """List every tagged group of Claude Code instances (each group has a
    name and a list of {id, name, dir} instances)."""
    return _load_json(CLAUDE_PATH)


@mcp.tool()
def create_claude_group(name: str) -> dict[str, Any]:
    """Create a new tag/group to organize Claude Code instances."""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "empty group name"}
    groups = _load_json(CLAUDE_PATH)
    g = {"id": _new_id(), "name": name, "instances": []}
    groups.append(g)
    _save_json(CLAUDE_PATH, groups)
    return {"ok": True, "group": g}


@mcp.tool()
def delete_claude_group(group_id: int) -> dict[str, Any]:
    """Remove a tag/group and all its instances."""
    groups = _load_json(CLAUDE_PATH)
    remaining = [g for g in groups if g.get("id") != group_id]
    if len(remaining) == len(groups):
        return {"ok": False, "error": f"group {group_id} not found"}
    _save_json(CLAUDE_PATH, remaining)
    return {"ok": True, "deleted": group_id}


@mcp.tool()
def add_claude_instance(group_id: int, name: str, working_dir: str) -> dict[str, Any]:
    """Add a Claude Code instance (named working directory) to a group."""
    name = (name or "").strip()
    working_dir = (working_dir or "").strip()
    if not name or not working_dir:
        return {"ok": False, "error": "name and working_dir required"}
    if not Path(working_dir).is_dir():
        return {"ok": False, "error": f"directory not found: {working_dir}"}
    groups = _load_json(CLAUDE_PATH)
    for g in groups:
        if g.get("id") == group_id:
            inst = {"id": _new_id(), "name": name, "dir": working_dir}
            g.setdefault("instances", []).append(inst)
            _save_json(CLAUDE_PATH, groups)
            return {"ok": True, "instance": inst}
    return {"ok": False, "error": f"group {group_id} not found"}


@mcp.tool()
def remove_claude_instance(group_id: int, instance_id: int) -> dict[str, Any]:
    """Remove a Claude Code instance from a group."""
    groups = _load_json(CLAUDE_PATH)
    for g in groups:
        if g.get("id") == group_id:
            insts = g.get("instances") or []
            new = [i for i in insts if i.get("id") != instance_id]
            if len(new) == len(insts):
                return {"ok": False, "error": f"instance {instance_id} not found"}
            g["instances"] = new
            _save_json(CLAUDE_PATH, groups)
            return {"ok": True, "deleted": instance_id}
    return {"ok": False, "error": f"group {group_id} not found"}


@mcp.tool()
def launch_claude_instance(working_dir: str, name: str = "claude") -> dict[str, Any]:
    """Open a new terminal window in `working_dir` running `claude`. Tries
    Windows Terminal first, falls back to cmd.exe. Resolves the claude CLI
    path explicitly to avoid PATHEXT lookup failures from the spawned shell."""
    if not Path(working_dir).is_dir():
        return {"ok": False, "error": f"directory not found: {working_dir}"}

    claude_exe = (shutil.which("claude")
                  or shutil.which("claude.cmd")
                  or shutil.which("claude.exe"))
    if not claude_exe:
        return {"ok": False,
                "error": "Claude CLI not found in PATH. Install Claude Code first."}

    try:
        wt = shutil.which("wt") or shutil.which("wt.exe")
        if wt:
            subprocess.Popen(
                [wt, "-d", working_dir, "--title", name,
                 "cmd", "/K", claude_exe],
            )
            return {"ok": True, "via": "wt", "claude": claude_exe}
    except (OSError, FileNotFoundError):
        pass

    try:
        subprocess.Popen(
            f'start "{name}" cmd /K "{claude_exe}"',
            cwd=working_dir, shell=True,
        )
        return {"ok": True, "via": "cmd", "claude": claude_exe}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Long-term memory — markdown file injected into the voice system prompt on
# session start and writable by Claude via these tools. Lives across panel
# restarts, separate from any per-session conversation history.
# ---------------------------------------------------------------------------
@mcp.tool()
def read_memory() -> str:
    """Return the full long-term memory file. Empty if none yet."""
    try:
        return MEMORY_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except Exception as e:
        return f"(error reading memory: {e})"


@mcp.tool()
def remember(text: str) -> dict[str, Any]:
    """Append a single fact/preference/note to long-term memory.

    The text is timestamped and added as a markdown bullet. Use this when the
    user tells you something worth remembering across sessions (preferences,
    ongoing projects, recurring people, decisions you've made together).
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty memory text"}
    ts = time.strftime("%Y-%m-%d")
    line = f"- ({ts}) {text}\n"
    try:
        existing = ""
        try:
            existing = MEMORY_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            pass
        if existing and not existing.endswith("\n"):
            existing += "\n"
        MEMORY_PATH.write_text(existing + line, encoding="utf-8")
        return {"ok": True, "line": line.strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def update_memory(content: str) -> dict[str, Any]:
    """Replace the entire memory file with new content. Use for curation —
    merging duplicates, removing stale facts, reorganizing into sections."""
    try:
        MEMORY_PATH.write_text(content or "", encoding="utf-8")
        return {"ok": True, "bytes": len(content or "")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def clear_memory() -> dict[str, Any]:
    """Wipe long-term memory completely."""
    try:
        if MEMORY_PATH.exists():
            MEMORY_PATH.unlink()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
