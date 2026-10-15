"""Where the desktop app keeps the token and the light-curve cache.

The token file sits next to the cache, not inside it, so emptying the cache
does not forget the token. Nothing in here prints the token.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "Mega Rhexas"


def app_data_dir(platform: str | None = None, env: dict | None = None, home: Path | None = None) -> Path:
    """Per-user data directory. Windows, macOS, and Linux each have their own place."""
    system = platform or sys.platform
    environ = os.environ if env is None else env
    home_dir = Path(home) if home is not None else Path.home()
    if system == "win32":
        base = (environ.get("LOCALAPPDATA") or "").strip() or str(home_dir / "AppData" / "Local")
        return Path(base) / APP_DIR_NAME
    if system == "darwin":
        return home_dir / "Library" / "Application Support" / APP_DIR_NAME
    base = (environ.get("XDG_DATA_HOME") or "").strip() or str(home_dir / ".local" / "share")
    return Path(base) / "mega-rhexas"


class AppPaths:
    """``root/token`` and ``root/cache``. ``root`` is the app-data directory."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.token_file = self.root / "token"
        self.cache = self.root / "cache"

    @classmethod
    def discover(cls, platform: str | None = None, env: dict | None = None, home: Path | None = None) -> AppPaths:
        return cls(app_data_dir(platform=platform, env=env, home=home))

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)
