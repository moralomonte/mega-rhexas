"""Device-flow login for ``rhexas worker --login`` and for an installer.

The worker asks the coordinator for a short code, opens a browser, and polls
until the signed-in account approves. The token that comes back is per computer
(``rhx_...``) and is stored in the user app-data directory. ``--token-file``
still overrides that file for one run.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import quote


class DeviceLoginError(Exception):
    pass


def app_data_dir() -> Path:
    """Per-user directory for the saved worker token. Override the root with env vars."""
    if os.name == "nt":
        root = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(root) / "rhexas"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "rhexas"
    root = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(root) / "rhexas"


def token_path() -> Path:
    return app_data_dir() / "worker.token"


def save_worker_token(token: str, path: Path | None = None) -> Path:
    dest = Path(path) if path is not None else token_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(token.strip() + "\n", encoding="utf-8")
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return dest


def load_saved_token(path: Path | None = None) -> str:
    dest = Path(path) if path is not None else token_path()
    if not dest.is_file():
        return ""
    for line in dest.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def login_device(
    coordinator_url: str,
    *,
    hostname: str | None = None,
    open_browser: bool = True,
    on_verification_url=None,
    opener=None,
    sleeper=None,
    timeout: float = 600,
    request_timeout: float = 30,
) -> str:
    """Run the device flow and return the ``rhx_`` worker token. Does not save it.

    ``on_verification_url`` is called with the link the person should open.
    Pass ``opener`` to replace ``webbrowser.open`` (tests do this).
    """
    base = coordinator_url.rstrip("/")
    started = _post(base + "/api/v1/device/start", {
        "hostname": (hostname or socket.gethostname() or "computer").strip()[:80],
    }, request_timeout)
    user_code = str(started.get("user_code") or "")
    device_code = str(started.get("device_code") or "")
    verification = str(started.get("verification_url") or "")
    if not device_code or not user_code or not verification:
        raise DeviceLoginError("coordinator did not start a device login")
    link = verification + ("&" if "?" in verification else "?") + "code=" + quote(user_code)
    if on_verification_url is not None:
        on_verification_url(link)
    if open_browser:
        (opener or webbrowser.open)(link)
    interval = float(started.get("interval") or 5)
    wait = sleeper or time.sleep
    deadline = time.monotonic() + float(timeout)
    while True:
        if time.monotonic() >= deadline:
            raise DeviceLoginError("timed out waiting for approval")
        wait(interval)
        if time.monotonic() >= deadline:
            raise DeviceLoginError("timed out waiting for approval")
        result = _post(base + "/api/v1/device/poll", {"device_code": device_code}, request_timeout)
        status = result.get("status")
        if status == "approved" and result.get("token"):
            return str(result["token"])
        if status == "slow_down":
            interval = float(result.get("interval") or interval + 5)
            continue
        if status in ("authorization_pending", "pending"):
            interval = float(result.get("interval") or interval)
            continue
        raise DeviceLoginError(str(result.get("error") or "device login failed"))


def link_this_computer(coordinator_url: str = "https://api.megarhexas.com", **kwargs) -> str:
    """Link this computer, store its worker token, and return the token.

    The installer and the dashboard setup screen call this. It opens the
    browser (unless ``open_browser`` is false) and writes the token under the
    user app-data directory.
    """
    token = login_device(coordinator_url, **kwargs)
    save_worker_token(token)
    return token


def _post(url: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DeviceLoginError(f"coordinator returned {exc.code}") from None
        return body if isinstance(body, dict) else {}
    except urllib.error.URLError as exc:
        raise DeviceLoginError(f"could not reach the coordinator: {exc.reason}") from None
    if not raw:
        return {}
    body = json.loads(raw.decode("utf-8"))
    if not isinstance(body, dict):
        raise DeviceLoginError("coordinator returned an unexpected response")
    return body
