"""System stats: CPU, RAM, GPU, disk, network, processes."""
from __future__ import annotations

import time
from dataclasses import dataclass, asdict
from typing import Any

import warnings

import psutil

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        import pynvml
    pynvml.nvmlInit()
    _NVML_OK = True
    _NVML_HANDLES = [pynvml.nvmlDeviceGetHandleByIndex(i) for i in range(pynvml.nvmlDeviceGetCount())]
except Exception:
    _NVML_OK = False
    _NVML_HANDLES = []


# Prime cpu_percent so the first real call returns a meaningful value
psutil.cpu_percent(interval=None)
for _p in psutil.process_iter():
    try:
        _p.cpu_percent(interval=None)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


def _bytes_to_mb(b: int) -> float:
    return round(b / (1024 * 1024), 1)


def _bytes_to_gb(b: int) -> float:
    return round(b / (1024 * 1024 * 1024), 2)


def get_cpu_stats() -> dict[str, Any]:
    freq = psutil.cpu_freq()
    return {
        "percent": psutil.cpu_percent(interval=None),
        "per_core": psutil.cpu_percent(interval=None, percpu=True),
        "count_logical": psutil.cpu_count(logical=True),
        "count_physical": psutil.cpu_count(logical=False),
        "freq_mhz": round(freq.current, 0) if freq else None,
        "freq_max_mhz": round(freq.max, 0) if freq and freq.max else None,
    }


def get_ram_stats() -> dict[str, Any]:
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    return {
        "total_gb": _bytes_to_gb(vm.total),
        "used_gb": _bytes_to_gb(vm.used),
        "available_gb": _bytes_to_gb(vm.available),
        "percent": vm.percent,
        "swap_total_gb": _bytes_to_gb(sm.total),
        "swap_used_gb": _bytes_to_gb(sm.used),
        "swap_percent": sm.percent,
    }


def get_gpu_stats() -> list[dict[str, Any]]:
    if not _NVML_OK or not _NVML_HANDLES:
        return []
    out = []
    for idx, h in enumerate(_NVML_HANDLES):
        try:
            name = pynvml.nvmlDeviceGetName(h)
            if isinstance(name, bytes):
                name = name.decode("utf-8", errors="ignore")
            util = pynvml.nvmlDeviceGetUtilizationRates(h)
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            try:
                temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
            except Exception:
                temp = None
            try:
                power = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
            except Exception:
                power = None
            out.append({
                "index": idx,
                "name": name,
                "util_percent": util.gpu,
                "mem_util_percent": util.memory,
                "vram_used_gb": _bytes_to_gb(mem.used),
                "vram_total_gb": _bytes_to_gb(mem.total),
                "vram_percent": round(mem.used / mem.total * 100, 1) if mem.total else 0,
                "temp_c": temp,
                "power_w": round(power, 1) if power is not None else None,
            })
        except Exception as e:
            out.append({"index": idx, "error": str(e)})
    return out


def get_disk_stats() -> list[dict[str, Any]]:
    out = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts or part.fstype == "":
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
            out.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "total_gb": _bytes_to_gb(u.total),
                "used_gb": _bytes_to_gb(u.used),
                "free_gb": _bytes_to_gb(u.free),
                "percent": u.percent,
            })
        except (PermissionError, OSError):
            continue
    return out


def get_network_stats() -> dict[str, Any]:
    n = psutil.net_io_counters()
    return {
        "bytes_sent_mb": _bytes_to_mb(n.bytes_sent),
        "bytes_recv_mb": _bytes_to_mb(n.bytes_recv),
        "packets_sent": n.packets_sent,
        "packets_recv": n.packets_recv,
    }


def get_uptime_seconds() -> float:
    return time.time() - psutil.boot_time()


def get_battery() -> dict[str, Any] | None:
    try:
        b = psutil.sensors_battery()
    except Exception:
        return None
    if b is None:
        return None
    secs_left = b.secsleft if b.secsleft not in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) else None
    return {
        "percent": round(b.percent, 1),
        "plugged": b.power_plugged,
        "seconds_left": secs_left,
    }


def get_top_processes(by: str = "ram", limit: int = 10) -> list[dict[str, Any]]:
    """Top processes by 'ram' or 'cpu'."""
    procs = []
    for p in psutil.process_iter(["pid", "name", "username"]):
        try:
            with p.oneshot():
                cpu = p.cpu_percent(interval=None)
                mem = p.memory_info()
                procs.append({
                    "pid": p.info["pid"],
                    "name": p.info["name"] or "?",
                    "user": p.info["username"] or "?",
                    "cpu_percent": round(cpu, 1),
                    "ram_mb": _bytes_to_mb(mem.rss),
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    key = "ram_mb" if by == "ram" else "cpu_percent"
    procs.sort(key=lambda x: x[key], reverse=True)
    return procs[:limit]


def get_snapshot() -> dict[str, Any]:
    """One-shot: everything you'd want in a heads-up display."""
    return {
        "cpu": get_cpu_stats(),
        "ram": get_ram_stats(),
        "gpu": get_gpu_stats(),
        "disk": get_disk_stats(),
        "network": get_network_stats(),
        "battery": get_battery(),
        "uptime_seconds": round(get_uptime_seconds(), 0),
    }


def format_uptime(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if not parts:
        parts.append(f"{s}s")
    return " ".join(parts)
