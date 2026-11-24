"""Open the local dashboard in a dedicated browser window (--app=URL).

Prefers Microsoft Edge, then Google Chrome, via usual install paths or (on
Windows) the App Paths registry. Falls back to the system default browser.
"""
from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from pathlib import Path


def open_dashboard(url: str, *, app_window: bool = True) -> bool:
    """Open ``url``. When ``app_window`` is true, try Edge/Chrome app mode first."""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False
    if app_window:
        exe = find_chromium_app()
        if exe is not None:
            try:
                subprocess.Popen(
                    [str(exe), f"--app={url}"],
                    close_fds=os.name != "nt",
                    start_new_session=True,
                )
                return True
            except OSError:
                pass
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def find_chromium_app() -> Path | None:
    """Return Edge or Chrome executable path, or None."""
    for candidate in _candidates():
        if candidate.is_file():
            return candidate
    return None


def _candidates() -> list[Path]:
    if sys.platform == "win32":
        return _windows_candidates()
    if sys.platform == "darwin":
        return _macos_candidates()
    return _linux_candidates()


def _windows_candidates() -> list[Path]:
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    paths = [
        Path(program_files) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(program_files_x86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(local) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(program_files) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(program_files_x86) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    reg = _edge_from_registry()
    if reg is not None:
        paths.insert(0, reg)
    return paths


def _edge_from_registry() -> Path | None:
    try:
        import winreg
    except ImportError:
        return None
    subkey = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        path = Path(str(value).strip().strip('"'))
        if path.is_file():
            return path
    return None


def _macos_candidates() -> list[Path]:
    return [
        Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path.home() / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]


def _linux_candidates() -> list[Path]:
    return [
        Path("/usr/bin/microsoft-edge"),
        Path("/usr/bin/microsoft-edge-stable"),
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/google-chrome-stable"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
    ]