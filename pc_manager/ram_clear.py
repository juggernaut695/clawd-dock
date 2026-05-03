"""Clear RAM by trimming working sets of running processes (Windows)."""
from __future__ import annotations

import ctypes
import sys
import time
from typing import Any

import psutil


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SET_QUOTA = 0x0100


def _is_windows() -> bool:
    return sys.platform == "win32"


def trim_working_sets(
    skip_pids: set[int] | None = None,
    min_working_set_mb: float = 25.0,
) -> dict[str, Any]:
    """Trim accessible processes' working sets. Returns a report.

    Only trims processes whose RSS is at least `min_working_set_mb` (MiB).
    Small system/graphics helpers (RuntimeBroker, ApplicationFrameHost,
    Audio/Search services, GPU driver shims, etc.) tend to cause visible
    flicker on our dock when their pages are evicted, while contributing
    almost nothing in freed memory — so we skip them by default.
    """
    if not _is_windows():
        return {"ok": False, "error": "Working-set trimming is Windows-only.", "trimmed": 0}

    skip_pids = skip_pids or set()
    skip_pids.add(0)  # System Idle

    min_rss = int(min_working_set_mb * 1024 * 1024)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
    psapi.EmptyWorkingSet.restype = ctypes.c_int

    before = psutil.virtual_memory()
    trimmed = 0
    failed = 0
    skipped = 0
    skipped_small = 0

    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        pid = proc.info["pid"]
        if pid in skip_pids:
            skipped += 1
            continue
        # Skip lightweight helpers — they're not worth the flicker
        try:
            mem = proc.info.get("memory_info")
            if mem and mem.rss < min_rss:
                skipped_small += 1
                continue
        except Exception:
            pass
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA, False, pid
        )
        if not handle:
            failed += 1
            continue
        try:
            if psapi.EmptyWorkingSet(handle):
                trimmed += 1
            else:
                failed += 1
        finally:
            kernel32.CloseHandle(handle)

    # Give Windows a moment to move pages to standby
    time.sleep(0.5)
    after = psutil.virtual_memory()

    freed_mb = round((before.used - after.used) / (1024 * 1024), 1)
    return {
        "ok": True,
        "trimmed": trimmed,
        "failed": failed,
        "skipped": skipped + skipped_small,
        "ram_used_before_gb": round(before.used / 1024**3, 2),
        "ram_used_after_gb": round(after.used / 1024**3, 2),
        "freed_mb": freed_mb,
        "ram_percent_before": before.percent,
        "ram_percent_after": after.percent,
    }


def kill_process(pid: int) -> dict[str, Any]:
    """Terminate a process by PID."""
    try:
        p = psutil.Process(pid)
        name = p.name()
        p.terminate()
        try:
            p.wait(timeout=3)
        except psutil.TimeoutExpired:
            p.kill()
        return {"ok": True, "pid": pid, "name": name}
    except psutil.NoSuchProcess:
        return {"ok": False, "error": f"No process with PID {pid}"}
    except psutil.AccessDenied:
        return {"ok": False, "error": f"Access denied for PID {pid} (try running as admin)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
