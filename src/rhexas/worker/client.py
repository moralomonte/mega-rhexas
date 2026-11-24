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

    def claim(self, *, prefetch: bool = False, sectors: list[int] | None = None) -> dict:
        """Lease a batch. ``prefetch`` asks for another lease while earlier ones are held.

        ``sectors`` limits the lease. Omit it, as the installed app does, to take any sector.
        """
        body: dict = {}
        if prefetch:
            body["prefetch"] = True
        if sectors:
            body["sectors"] = [int(sector) for sector in sectors]
        return self._request("POST", self._task("claim"), body)

    def peek(self, *, sectors: list[int] | None = None) -> dict:
        """Next unit's size, without reserving it. Older coordinators 404."""
        body: dict = {}
        if sectors:
            body["sectors"] = [int(sector) for sector in sectors]
        return self._request("POST", self._task("peek"), body)

    def receipt(self, assignment_id: int) -> dict:
        """Whether the coordinator stored a row for every star in this unit."""
        return self._request("POST", self._task("receipt"), {"assignment_id": int(assignment_id)})

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

    def mission_progress(self) -> dict:
        """Compact overall progress for enrolled sectors (worker or operator auth)."""
        return self._request("GET", "/api/v1/mission/progress", None)

    def candidates_science(self, *, limit: int = 50) -> dict:
        """Shortlist and progress from the coordinator science API."""
        path = f"/api/v1/candidates/science?limit={max(1, min(int(limit), 200))}"
        return self._request("GET", path, None)

    def post_local_check(self, payload: dict) -> dict:
        """Upload an untrusted local check. Worker auth only."""
        tic = int(payload["tic"])
        query = f"/api/v1/candidates/local-check?tic={tic}"
        return self._request("POST", query, payload)

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
