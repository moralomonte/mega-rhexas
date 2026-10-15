"""Look up the latest GitHub release and offer its installer. Never install it."""
from __future__ import annotations

import json
import threading
import urllib.request

RELEASES_URL = "https://api.github.com/repos/moralomonte/mega-rhexas/releases/latest"
RELEASES_PAGE = "https://github.com/moralomonte/mega-rhexas/releases/latest"


def parse_version(text: str) -> tuple[int, ...]:
    raw = str(text or "").strip()
    if raw[:1] in ("v", "V"):
        raw = raw[1:]
    parts: list[int] = []
    for piece in raw.split("."):
        digits = ""
        for char in piece:
            if char.isdigit():
                digits += char
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    left, right = parse_version(latest), parse_version(current)
    if not left:
        return False
    width = max(len(left), len(right))
    left = left + (0,) * (width - len(left))
    right = right + (0,) * (width - len(right))
    return left > right


def offer_from_release(payload: dict, current: str) -> dict | None:
    """A download offer, or None when this copy is current. No file is fetched."""
    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        return None
    tag = str(payload.get("tag_name") or "")
    if not is_newer(tag, current):
        return None
    assets = payload.get("assets") if isinstance(payload.get("assets"), list) else []
    chosen = _prefer_installer(assets)
    html = str(payload.get("html_url") or RELEASES_PAGE)
    asset_url = str(chosen.get("browser_download_url") or "") if chosen else ""
    asset_name = str(chosen.get("name") or "") if chosen else ""
    version = tag[1:] if tag[:1] in ("v", "V") else tag
    return {
        "version": version,
        "html_url": html,
        "asset_url": asset_url or html,
        "asset_name": asset_name,
    }


def _prefer_installer(assets: list) -> dict | None:
    exe = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        if name.lower().endswith(".exe"):
            exe.append(asset)
    for asset in exe:
        if "setup" in str(asset.get("name") or "").lower():
            return asset
    return exe[0] if exe else None


class UpdateBoard:
    """The offer the dashboard reads. ``set_offer(None)`` means no update."""

    def __init__(self, current: str):
        self.current = current
        self._lock = threading.Lock()
        self._offer = None

    def set_offer(self, offer: dict | None) -> None:
        with self._lock:
            self._offer = offer

    def snapshot(self) -> dict:
        with self._lock:
            offer = self._offer
        return {"current": self.current, "offer": offer}


def check_for_update(current: str, *, url: str = RELEASES_URL, opener=None, timeout: float = 10) -> dict | None:
    """GET the GitHub releases API once. Does not download the installer."""
    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "mega-rhexas")
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read()
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return offer_from_release(payload, current)
