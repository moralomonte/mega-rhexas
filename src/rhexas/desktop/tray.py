"""Tray menu. The same labels are what a future macOS status item should show."""
from __future__ import annotations

import io
import sys
import threading
from pathlib import Path

MENU_LABELS = ("Open dashboard", "Pause", "Quit")

# Native 16 and 32 (and the other sizes) from site/img/triskelion.svg.
_ICON = Path(__file__).resolve().parent / "icons" / "triskelion.ico"
# The installer also copies the icon next to pythonw.exe; use it if package data is missing.
_FALLBACK_ICON = Path(sys.prefix) / "triskelion.ico"


def icon_path() -> Path | None:
    for path in (_ICON, _FALLBACK_ICON):
        if path.is_file():
            return path
    return None


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
    path = icon_path()
    if path is None:
        return None
    blob = path.read_bytes()
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


def start_tray(*, on_open, on_toggle_pause, on_quit, paused):
    """Start the tray on a background thread. Returns the icon, or None.

    ``paused`` is a callable so the checkmark follows the worker.
    ``on_quit`` is called with the icon; it should stop the icon and the worker.
    Picture speed is chosen on the dashboard page, not from this menu, and it
    does not change how hard the search runs.
    """
    try:
        import pystray
    except ImportError as exc:
        print(f"tray icon is unavailable ({exc}); the dashboard is still open in the browser", flush=True)
        return None
    image = triskelion_image()
    if image is None:
        print(f"tray icon is unavailable (no icon at {_ICON}); the dashboard is still open in the browser", flush=True)
        return None

    menu = pystray.Menu(
        pystray.MenuItem("Open dashboard", lambda icon, item: on_open(), default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Pause", lambda icon, item: on_toggle_pause(), checked=lambda item: bool(paused())),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda icon, item: on_quit(icon)),
    )
    icon = pystray.Icon("mega-rhexas", image, "Mega Rhexas", menu)
    threading.Thread(target=icon.run, name="rhexas-tray", daemon=True).start()
    return icon
