"""Small JSON client for the coordinator. Stdlib only, so a worker does not add a second HTTP stack."""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class ProtocolError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class CoordinatorClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 120, project: str = "exoplanets"):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.project = project

    def _task(self, action: str) -> str:
        return f"/api/v1/projects/{self.project}/tasks/{action}"

    def current(self) -> dict:
        return self._request("GET", self._task("current"), None)

    def claim(self) -> dict:
        return self._request("POST", self._task("claim"), {})

    def heartbeat(self, assignment_id: int, progress: dict | None = None) -> dict:
        """Renew the lease. ``progress`` is omitted when unknown so older coordinators still accept the body.

        The coordinator ignores a missing progress object. Shape, when sent:
        ``{"phase": "download", "done": 400, "total": 500}``.
        """
        body: dict = {"assignment_id": assignment_id}
        if progress:
            body["progress"] = {
                "phase": progress.get("phase"),
                "done": progress.get("done"),
                "total": progress.get("total"),
            }
        return self._request("POST", self._task("heartbeat"), body)

    def submit(self, assignment_id: int, stars: list, candidates: list) -> dict:
        return self._request("POST", self._task("submit"), {
            "assignment_id": assignment_id,
            "stars": stars,
            "candidates": candidates,
        })

    def release(self, assignment_id: int) -> dict:
        return self._request("POST", self._task("release"), {"assignment_id": assignment_id})

    def _request(self, method: str, path: str, payload) -> dict:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            message = raw
            try:
                message = json.loads(raw).get("error", raw)
            except json.JSONDecodeError:
                pass
            raise ProtocolError(exc.code, str(message)) from None
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))
