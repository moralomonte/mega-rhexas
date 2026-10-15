"""First-run gate. The dashboard asks this; the page never receives the token."""
from __future__ import annotations

import threading
from pathlib import Path

from rhexas.worker.live import public_coordinator_url, redact

from .device import DeviceError, DeviceLink
from .tokenstore import TokenError, read_token, write_token

SIGNIN_LABEL = "Sign in with megarhexas.com"
_WAITING = {"pending", "authorization_pending", "slow_down"}


class SetupGate:
    """First-run sign-in. The main button calls ``begin_device`` / ``poll_device``.

    Pasting a token calls :meth:`save_pasted`. Both paths store the same file,
    and :meth:`status` tells the page that a token is still needed.
    """

    def __init__(self, app_dir: Path, coordinator_url: str, linker: DeviceLink | None = None):
        self.app_dir = Path(app_dir)
        self.coordinator_url = coordinator_url
        self.linker = linker or DeviceLink(coordinator_url)
        self.ready = threading.Event()
        self._lock = threading.Lock()
        self._device_code = ""
        self._interval = 5.0
        if not read_token(self.app_dir):
            # A computer already linked with ``rhexas worker --login`` keeps that token.
            try:
                from rhexas.worker.login import load_saved_token
                saved = load_saved_token()
                if saved:
                    write_token(self.app_dir, saved)
            except (OSError, TokenError):
                pass
        if read_token(self.app_dir):
            self.ready.set()

    def status(self) -> dict:
        return {
            "needs_token": not self.ready.is_set(),
            "coordinator_url": public_coordinator_url(self.coordinator_url),
            "signin": {
                "label": SIGNIN_LABEL,
                "start_path": "/api/v1/device/start",
                "poll_path": "/api/v1/device/poll",
            },
        }

    def save_pasted(self, token) -> None:
        write_token(self.app_dir, "" if token is None else str(token))
        self.ready.set()

    def begin_device(self) -> dict:
        started = self.linker.start()
        with self._lock:
            self._device_code = started.device_code
            self._interval = started.interval or 5
        return {
            "verification_uri": started.verification_uri,
            "user_code": started.user_code,
            "interval": started.interval,
            "expires_in": started.expires_in,
        }

    def poll_device(self) -> dict:
        with self._lock:
            code = self._device_code
            interval = self._interval
        if not code:
            return {
                "status": "idle",
                "error": "Sign-in has not started. Try Sign in again, or paste a token.",
            }
        try:
            result = self.linker.poll(code)
        except DeviceError as exc:
            return {"status": "error", "error": str(exc), "interval": interval}
        if result.token:
            try:
                self.save_pasted(result.token)
                try:
                    # Same place ``rhexas worker --login`` stores it, so the CLI worker finds it too.
                    from rhexas.worker.login import save_worker_token
                    save_worker_token(result.token)
                except OSError:
                    pass
            except TokenError:
                return {
                    "status": "error",
                    "error": "Sign-in did not return a usable token. Paste a token instead.",
                }
            return {"status": "ready"}
        status = result.status or "pending"
        if status == "slow_down":
            interval = min(float(getattr(result, "interval", 0) or float(interval) + 5), 60)
            with self._lock:
                self._interval = interval
        if status in _WAITING:
            return {"status": "pending", "interval": interval}
        if status == "ready":
            return {
                "status": "error",
                "error": "Sign-in finished without a token. Paste a token instead.",
            }
        payload = {"status": status, "interval": interval}
        if result.message:
            payload["error"] = redact(result.message)
        elif status in {"expired", "access_denied", "denied"}:
            payload["error"] = "Sign-in did not finish. Paste a token instead."
        return payload
