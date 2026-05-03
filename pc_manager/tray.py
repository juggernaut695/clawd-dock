"""System tray icon with live CPU/RAM/GPU bars + popup dashboard."""
from __future__ import annotations

import threading
import time

import customtkinter as ctk
import psutil
import pystray
from PIL import Image, ImageDraw

from pc_manager import monitor, ram_clear
from pc_manager.dashboard import Dashboard, _color_for


ICON_SIZE = 64


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def make_icon_image(cpu: float, ram: float, gpu: float) -> Image.Image:
    """Render a tray icon with 3 vertical bars (CPU/RAM/GPU)."""
    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Background pill
    d.rounded_rectangle((2, 2, ICON_SIZE - 2, ICON_SIZE - 2),
                        radius=10, fill=(15, 23, 42, 230))

    # Three bars
    pad = 8
    gap = 4
    bar_w = (ICON_SIZE - pad * 2 - gap * 2) // 3
    base_y = ICON_SIZE - pad
    max_h = ICON_SIZE - pad * 2

    for i, value in enumerate((cpu, ram, gpu)):
        x = pad + i * (bar_w + gap)
        v = max(0.0, min(value, 100.0))
        h = int(max_h * (v / 100))
        # Track
        d.rectangle((x, pad, x + bar_w, base_y), fill=(51, 65, 85, 255))
        # Filled portion
        if h > 0:
            color = _hex_to_rgb(_color_for(v)) + (255,)
            d.rectangle((x, base_y - h, x + bar_w, base_y), fill=color)

    return img


class TrayApp:
    def __init__(self):
        # Hidden root keeps Tk alive; dashboards are Toplevels on top of it
        self.root = ctk.CTk()
        self.root.withdraw()
        self.dashboard: Dashboard | None = None
        self.icon: pystray.Icon | None = None
        self._stop = threading.Event()

    # ---------- Dashboard control ----------
    def show_dashboard(self):
        if self.dashboard is None or not self.dashboard.winfo_exists():
            self.dashboard = Dashboard(on_close=lambda: None)
        self.dashboard.deiconify()
        self.dashboard.lift()
        self.dashboard.focus_force()
        self.dashboard.attributes("-topmost", True)
        self.dashboard.after(200, lambda: self.dashboard.attributes("-topmost", False))

    # ---------- Menu actions (run in pystray thread) ----------
    def _on_show(self, icon, item):
        self.root.after(0, self.show_dashboard)

    def _on_clear_ram(self, icon, item):
        def work():
            r = ram_clear.trim_working_sets()
            if self.icon:
                if r.get("ok"):
                    self.icon.notify(
                        f"Trimmed {r['trimmed']} processes  ·  freed {r['freed_mb']:.0f} MB",
                        "PC Manager",
                    )
                else:
                    self.icon.notify(r.get("error", "Failed"), "PC Manager")
        threading.Thread(target=work, daemon=True).start()

    def _on_quit(self, icon, item):
        self._stop.set()
        if self.icon:
            self.icon.stop()
        self.root.after(0, self.root.destroy)

    # ---------- Update loop ----------
    def _update_loop(self):
        while not self._stop.is_set():
            try:
                cpu = psutil.cpu_percent(interval=None)
                ram = psutil.virtual_memory().percent
                gpu_pct = 0.0
                gpu_text = "no GPU"
                gpus = monitor.get_gpu_stats()
                if gpus and "util_percent" in gpus[0]:
                    gpu_pct = float(gpus[0]["util_percent"])
                    gpu_text = f"GPU {gpu_pct:.0f}%"
                if self.icon:
                    self.icon.icon = make_icon_image(cpu, ram, gpu_pct)
                    self.icon.title = (
                        f"PC Manager\nCPU {cpu:.0f}%   RAM {ram:.0f}%   {gpu_text}"
                    )
            except Exception:
                pass
            self._stop.wait(2.0)

    # ---------- Run ----------
    def run(self):
        menu = pystray.Menu(
            pystray.MenuItem("Show Dashboard", self._on_show, default=True),
            pystray.MenuItem("Clear RAM", self._on_clear_ram),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )
        self.icon = pystray.Icon(
            "pc-manager",
            make_icon_image(0, 0, 0),
            "PC Manager",
            menu,
        )

        threading.Thread(target=self.icon.run, daemon=True).start()
        threading.Thread(target=self._update_loop, daemon=True).start()

        try:
            self.root.mainloop()
        finally:
            self._stop.set()
            if self.icon:
                try:
                    self.icon.stop()
                except Exception:
                    pass


def main():
    TrayApp().run()


if __name__ == "__main__":
    main()
