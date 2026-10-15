"""Browser sign-in for the desktop app.

Same wire format as :func:`rhexas.worker.login.login_device` (the coordinator's
device flow), split into non-blocking start/poll steps for the setup screen:

* ``POST {coordinator}/api/v1/device/start`` with ``{"hostname": ...}`` returns
  ``device_code``, ``user_code``, ``verification_url``, ``interval``, ``expires_in``.
  The browser opens ``verification_url?code=<user_code>``.
* ``POST {coordinator}/api/v1/device/poll`` with ``{"device_code": ...}`` returns
  ``authorization_pending`` / ``slow_down`` (200), ``approved`` with the ``rhx_``
  token once (200), or ``expired`` / ``denied`` / ``consumed`` (400 with JSON). The setup screen calls this through
:class:`rhexas.desktop.setup.SetupGate`, which stores the token and never
returns it to the page.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from urllib.parse import quote


class DeviceError(Exception):
    """Sign-in could not finish. The message is safe to show; it has no token."""


class DeviceStart:
    def __init__(self, device_code: str, verification_uri: str, interval: float, expires_in: float, user_code: str = ""):
        self.device_code = device_code
        self.verification_uri = verification_uri
        self.interval = interval
        self.expires_in = expires_in
        self.user_code = user_code


class DevicePoll:
    def __init__(self, status: str, token: str = "", message: str = "", interval: float = 0.0):
        self.interval = interval
        self.status = status
        self.token = token
        self.message = message


class DeviceLink:
    """HTTP client for the device-link endpoints. ``opener`` is ``urlopen`` in production."""

    def __init__(self, base_url: str, opener=None, timeout: float = 30):
        self.base = (base_url or "").rstrip("/")
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def start(self) -> DeviceStart:
        host = (socket.gethostname() or "computer").strip()[:80]
        data = self._post("/api/v1/device/start", {"hostname": host})
        code = str(data.get("device_code") or "").strip()
        user_code = str(data.get("user_code") or "").strip()
        uri = str(data.get("verification_url") or data.get("verification_uri") or "").strip()
        if uri and user_code and "code=" not in uri:
            uri = uri + ("&" if "?" in uri else "?") + "code=" + quote(user_code)
        if not code or not uri.startswith(("https://", "http://")):
            raise DeviceError("Sign-in from megarhexas.com did not start. Paste the invite token instead.")
        try:
            interval = float(data.get("interval") or 5)
        except (TypeError, ValueError):
            interval = 5.0
        try:
            expires = float(data.get("expires_in") or 600)
        except (TypeError, ValueError):
            expires = 600.0
        return DeviceStart(code, uri, interval, expires, user_code)

    def poll(self, device_code: str) -> DevicePoll:
        data = self._post("/api/v1/device/poll", {"device_code": device_code})
        token = str(data.get("token") or "").strip()
        status = str(data.get("status") or ("ready" if token else "pending")).strip() or "pending"
        if token:
            status = "ready"
        poll = DevicePoll(status, token, str(data.get("error") or ""))
        try:
            poll.interval = float(data.get("interval") or 0)
        except (TypeError, ValueError):
            poll.interval = 0.0
        return poll

    def _post(self, path: str, payload: dict) -> dict:
        if not self.base.startswith(("https://", "http://")):
            raise DeviceError("The coordinator address is not set. Paste the invite token instead.")
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", "mega-rhexas")
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 403, 410):
                try:
                    body = json.loads(exc.read().decode("utf-8"))
                except Exception:
                    body = None
                if isinstance(body, dict) and body.get("status"):
                    return body
            if exc.code in (404, 405, 501):
                raise DeviceError(
                    "Sign-in from megarhexas.com is not available yet. Paste the invite token instead."
                ) from None
            raise DeviceError("Could not reach megarhexas.com. Paste the invite token, or try sign-in again.") from None
        except DeviceError:
            raise
        except Exception:
            raise DeviceError("Could not reach megarhexas.com. Paste the invite token, or try sign-in again.") from None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DeviceError(
                "Sign-in from megarhexas.com sent an unexpected reply. Paste the invite token instead."
            ) from None
        if not isinstance(data, dict):
            raise DeviceError(
                "Sign-in from megarhexas.com sent an unexpected reply. Paste the invite token instead."
            )
        return data
