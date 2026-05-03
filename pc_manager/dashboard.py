"""CustomTkinter heads-up dashboard window."""
from __future__ import annotations

import threading
import tkinter as tk
from typing import Callable

import customtkinter as ctk

from pc_manager import monitor, ram_clear


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


def _color_for(pct: float) -> str:
    if pct >= 85:
        return "#ef4444"  # red
    if pct >= 65:
        return "#f59e0b"  # amber
    return "#22c55e"      # green


class StatRow(ctk.CTkFrame):
    """A label + progress bar + value row."""
    def __init__(self, parent, label: str):
        super().__init__(parent, fg_color="transparent")
        self.grid_columnconfigure(1, weight=1)
        self.lbl = ctk.CTkLabel(self, text=label, width=60, anchor="w",
                                font=ctk.CTkFont(size=13, weight="bold"))
        self.lbl.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.bar = ctk.CTkProgressBar(self, height=14, corner_radius=4)
        self.bar.set(0)
        self.bar.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.value = ctk.CTkLabel(self, text="—", width=180, anchor="e",
                                  font=ctk.CTkFont(size=12))
        self.value.grid(row=0, column=2, sticky="e")

    def update(self, percent: float, text: str):
        self.bar.set(max(0, min(percent, 100)) / 100)
        self.bar.configure(progress_color=_color_for(percent))
        self.value.configure(text=text)


class Dashboard(ctk.CTkToplevel):
    def __init__(self, on_close: Callable | None = None):
        super().__init__()
        self.title("PC Manager")
        self.geometry("520x620")
        self.minsize(480, 560)
        self._on_close = on_close
        self.protocol("WM_DELETE_WINDOW", self._handle_close)

        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(16, 8))
        ctk.CTkLabel(header, text="PC Manager",
                     font=ctk.CTkFont(size=20, weight="bold")).pack(side="left")
        self.uptime_lbl = ctk.CTkLabel(header, text="", text_color="#94a3b8",
                                       font=ctk.CTkFont(size=12))
        self.uptime_lbl.pack(side="right")

        # Stat rows
        stats_frame = ctk.CTkFrame(self)
        stats_frame.pack(fill="x", padx=16, pady=8)
        self.cpu_row = StatRow(stats_frame, "CPU")
        self.cpu_row.pack(fill="x", padx=12, pady=(12, 6))
        self.ram_row = StatRow(stats_frame, "RAM")
        self.ram_row.pack(fill="x", padx=12, pady=6)
        self.gpu_row = StatRow(stats_frame, "GPU")
        self.gpu_row.pack(fill="x", padx=12, pady=6)
        self.disk_row = StatRow(stats_frame, "Disk")
        self.disk_row.pack(fill="x", padx=12, pady=(6, 12))

        # Battery (hidden when no battery)
        self.battery_lbl = ctk.CTkLabel(self, text="", text_color="#94a3b8",
                                        font=ctk.CTkFont(size=12))

        # Top processes
        proc_header = ctk.CTkFrame(self, fg_color="transparent")
        proc_header.pack(fill="x", padx=16, pady=(8, 4))
        ctk.CTkLabel(proc_header, text="Top processes",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")
        self.sort_var = tk.StringVar(value="ram")
        sort_menu = ctk.CTkSegmentedButton(
            proc_header, values=["ram", "cpu"],
            variable=self.sort_var, command=lambda _: self._refresh_now(),
            height=24,
        )
        sort_menu.pack(side="right")

        self.proc_frame = ctk.CTkScrollableFrame(self, height=180)
        self.proc_frame.pack(fill="both", expand=True, padx=16, pady=4)

        # Footer buttons
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=16, pady=(8, 16))
        self.clear_btn = ctk.CTkButton(footer, text="Clear RAM", height=36,
                                       command=self._clear_ram)
        self.clear_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.refresh_btn = ctk.CTkButton(footer, text="Refresh", height=36,
                                         fg_color="#334155", hover_color="#475569",
                                         command=self._refresh_now)
        self.refresh_btn.pack(side="right", fill="x", expand=True, padx=(4, 0))

        self.status_lbl = ctk.CTkLabel(self, text="", text_color="#94a3b8",
                                       font=ctk.CTkFont(size=11))
        self.status_lbl.pack(pady=(0, 8))

        self._refresh_loop()

    def _handle_close(self):
        if self._on_close:
            self._on_close()
        self.withdraw()  # hide instead of destroy so tray can re-open

    def _refresh_loop(self):
        self._refresh_now()
        self.after(1500, self._refresh_loop)

    def _refresh_now(self):
        snap = monitor.get_snapshot()

        cpu = snap["cpu"]
        cpu_text = f"{cpu['percent']:.0f}%  ·  {cpu['count_logical']} cores"
        if cpu.get("freq_mhz"):
            cpu_text += f"  ·  {cpu['freq_mhz']/1000:.1f} GHz"
        self.cpu_row.update(cpu["percent"], cpu_text)

        ram = snap["ram"]
        self.ram_row.update(
            ram["percent"],
            f"{ram['used_gb']:.1f} / {ram['total_gb']:.1f} GB  ·  {ram['percent']:.0f}%",
        )

        gpus = snap["gpu"]
        if gpus and "error" not in gpus[0]:
            g = gpus[0]
            text = f"{g['util_percent']}%  ·  VRAM {g['vram_used_gb']:.1f}/{g['vram_total_gb']:.1f} GB"
            if g.get("temp_c") is not None:
                text += f"  ·  {g['temp_c']}°C"
            self.gpu_row.update(g["util_percent"], text)
        else:
            self.gpu_row.update(0, "no NVIDIA GPU")

        disks = snap["disk"]
        if disks:
            d = max(disks, key=lambda x: x["percent"])
            self.disk_row.update(
                d["percent"],
                f"{d['device']}  ·  {d['used_gb']:.0f}/{d['total_gb']:.0f} GB",
            )

        bat = snap["battery"]
        if bat:
            charge = "⚡ charging" if bat["plugged"] else "on battery"
            self.battery_lbl.configure(text=f"Battery {bat['percent']:.0f}%  ·  {charge}")
            if not self.battery_lbl.winfo_ismapped():
                self.battery_lbl.pack(after=self.disk_row.master, padx=16)
        else:
            self.battery_lbl.pack_forget()

        self.uptime_lbl.configure(text=f"up {monitor.format_uptime(snap['uptime_seconds'])}")
        self._refresh_processes()

    def _refresh_processes(self):
        for w in self.proc_frame.winfo_children():
            w.destroy()
        procs = monitor.get_top_processes(by=self.sort_var.get(), limit=10)
        for p in procs:
            row = ctk.CTkFrame(self.proc_frame, fg_color="#1e293b", corner_radius=4)
            row.pack(fill="x", pady=2)
            row.grid_columnconfigure(0, weight=1)
            name = ctk.CTkLabel(row, text=f"{p['name']}  ·  PID {p['pid']}",
                                anchor="w", font=ctk.CTkFont(size=12))
            name.grid(row=0, column=0, sticky="w", padx=8, pady=4)
            stats = ctk.CTkLabel(row, text=f"{p['ram_mb']:.0f} MB  ·  {p['cpu_percent']:.0f}%",
                                 font=ctk.CTkFont(size=11), text_color="#94a3b8")
            stats.grid(row=0, column=1, padx=4)
            kill = ctk.CTkButton(row, text="Kill", width=50, height=22,
                                 fg_color="#7f1d1d", hover_color="#991b1b",
                                 font=ctk.CTkFont(size=11),
                                 command=lambda pid=p["pid"], n=p["name"]: self._kill(pid, n))
            kill.grid(row=0, column=2, padx=8, pady=2)

    def _kill(self, pid: int, name: str):
        result = ram_clear.kill_process(pid)
        if result["ok"]:
            self._set_status(f"Killed {name} (PID {pid})")
        else:
            self._set_status(f"Failed to kill {name}: {result.get('error', '?')}")
        self._refresh_now()

    def _clear_ram(self):
        self.clear_btn.configure(state="disabled", text="Clearing…")
        self._set_status("Trimming working sets…")

        def work():
            result = ram_clear.trim_working_sets()
            self.after(0, lambda: self._after_clear(result))

        threading.Thread(target=work, daemon=True).start()

    def _after_clear(self, result: dict):
        self.clear_btn.configure(state="normal", text="Clear RAM")
        if result.get("ok"):
            freed = result["freed_mb"]
            sign = "+" if freed >= 0 else ""
            self._set_status(
                f"Trimmed {result['trimmed']} processes  ·  freed {sign}{freed:.0f} MB"
            )
        else:
            self._set_status(f"Error: {result.get('error', '?')}")
        self._refresh_now()

    def _set_status(self, text: str):
        self.status_lbl.configure(text=text)
