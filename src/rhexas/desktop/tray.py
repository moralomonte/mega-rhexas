"""Tray menu. The same labels are what a future macOS status item should show."""
from __future__ import annotations

import math
import threading

MENU_LABELS = ("Open dashboard", "Eco", "Live", "Turbo", "Pause", "Quit")


def triskelion_image():
    """A small triskelion for the tray. None when Pillow is not installed."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    size = 64
    image = Image.new("RGBA", (size, size), (8, 10, 28, 255))
    draw = ImageDraw.Draw(image)
    colors = ((165, 243, 252, 255), (129, 140, 248, 255), (192, 132, 252, 255))
    center = size / 2
    for index, color in enumerate(colors):
        angle = -math.pi / 2 + index * (2 * math.pi / 3)
        x = center + math.cos(angle) * 14
        y = center + math.sin(angle) * 14
        draw.ellipse((x - 9, y - 9, x + 9, y + 9), fill=color)
        arm = angle + 0.9
        x2 = center + math.cos(arm) * 24
        y2 = center + math.sin(arm) * 24
        draw.line((center, center, x2, y2), fill=color, width=6)
    draw.ellipse((center - 5, center - 5, center + 5, center + 5), fill=(255, 255, 255, 255))
    return image


def start_tray(*, on_open, on_mode, on_toggle_pause, on_quit, mode, paused):
    """Start the tray on a background thread. Returns the icon, or None.

    ``mode`` and ``paused`` are callables so the checkmarks follow the worker.
    ``on_quit`` is called with the icon; it should stop the icon and the worker.
    """
    try:
        import pystray
    except ImportError:
        print("tray icon is unavailable; the dashboard is still open in the browser", flush=True)
        return None
    image = triskelion_image()
    if image is None:
        print("tray icon is unavailable; the dashboard is still open in the browser", flush=True)
        return None

    def _mode(name: str):
        def handler(icon, item):
            on_mode(name)
        return handler

    menu = pystray.Menu(
        pystray.MenuItem("Open dashboard", lambda icon, item: on_open(), default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Eco", _mode("eco"), checked=lambda item: mode() == "eco", radio=True),
        pystray.MenuItem("Live", _mode("live"), checked=lambda item: mode() == "live", radio=True),
        pystray.MenuItem("Turbo", _mode("turbo"), checked=lambda item: mode() == "turbo", radio=True),
        pystray.MenuItem("Pause", lambda icon, item: on_toggle_pause(), checked=lambda item: bool(paused())),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda icon, item: on_quit(icon)),
    )
    icon = pystray.Icon("mega-rhexas", image, "Mega Rhexas", menu)
    threading.Thread(target=icon.run, name="rhexas-tray", daemon=True).start()
    return icon
