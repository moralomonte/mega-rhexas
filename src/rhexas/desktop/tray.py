"""Tray menu. The same labels are what a future macOS status item should show."""
from __future__ import annotations

import io
import threading
from pathlib import Path

MENU_LABELS = ("Open dashboard", "Eco", "Live", "Turbo", "Pause", "Quit")

# Native 16 and 32 (and the other sizes) from site/img/triskelion.svg.
_ICON = Path(__file__).resolve().parent / "icons" / "triskelion.ico"


def triskelion_image():
    """The site triskelion for the tray. None when Pillow or the icon is missing.

    The in-memory image is the native 32px frame. Saving as ICO writes every
    size in the file, including the native 16px frame, so Windows can pick a
    sharp icon instead of scaling one drawing.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    if not _ICON.is_file():
        return None
    blob = _ICON.read_bytes()
    image = Image.open(io.BytesIO(blob))
    if (32, 32) in image.info.get("sizes", ()):
        image.size = (32, 32)
        image.load()

    def save(fp, format=None, **params):
        if (format or "").upper() in ("", "ICO"):
            if hasattr(fp, "write"):
                fp.write(blob)
            else:
                Path(fp).write_bytes(blob)
            return
        Image.Image.save(image, fp, format=format, **params)

    image.save = save
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
