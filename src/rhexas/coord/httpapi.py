"""HTTP routes for the coordinator. No sockets here, so tests can call ``App.handle``."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.parse import parse_qs

from .projects import DEFAULT_PROJECT
from .store import CoordError, Store

_TASKS = {"claim", "current", "heartbeat", "submit", "release"}

_PAGE = "status.html"
_FAVICON = "favicon.svg"
_COOKIE = "rhexas_admin"
_MAX_BODY = 8_000_000


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)


def _json(status: int, payload: dict, headers: list[tuple[str, str]] | None = None) -> Response:
    return Response(status, json.dumps(payload).encode("utf-8"), headers=list(headers or []))


def _error(status: int, message: str, code: str) -> Response:
    return _json(status, {"error": message, "code": code})


def _cookies(header: str) -> dict[str, str]:
    found = {}
    for part in (header or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        found[key.strip()] = value.strip()
    return found


def _bearer(headers: dict) -> str:
    auth = ""
    for key, value in headers.items():
        if key.lower() == "authorization":
            auth = value
            break
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _admin_token(headers: dict) -> str:
    bearer = _bearer(headers)
    if bearer:
        return bearer
    cookie_header = ""
    for key, value in headers.items():
        if key.lower() == "cookie":
            cookie_header = value
            break
    return _cookies(cookie_header).get(_COOKIE, "")


def _set_cookie(token: str, max_age: int) -> tuple[str, str]:
    value = token if max_age else ""
    return (
        "Set-Cookie",
        f"{_COOKIE}={value}; HttpOnly; SameSite=Lax; Path=/; Max-Age={max_age}",
    )


class App:
    def __init__(self, store: Store, *, page: bytes | None = None, max_body: int = _MAX_BODY,
                 accounts=None, smtp=None, mailer=None):
        from .auth import SmtpMailer
        from .settings import AccountsConfig
        self.store = store
        self.max_body = max_body
        self.page = page if page is not None else _load_page()
        self.favicon = _load_asset(_FAVICON)
        self.accounts = accounts or AccountsConfig()
        self.mailer = mailer if mailer is not None else SmtpMailer(smtp)

    def handle(self, method: str, path: str, query: str, headers: dict, body: bytes) -> Response:
        hdrs = {k: v for k, v in headers.items()}
        try:
            response = self._route(method.upper(), path, parse_qs(query), hdrs, body)
        except CoordError as exc:
            response = _error(exc.status, str(exc), _code(exc.status))
        except Exception:
            import traceback
            traceback.print_exc()
            response = _error(500, "internal error", "internal")
        return self._with_cors(hdrs, response)

    def _with_cors(self, headers: dict, response: Response) -> Response:
        origin = ""
        for key, value in headers.items():
            if key.lower() == "origin":
                origin = value.strip()
                break
        if origin and self.accounts.origin_allowed(origin):
            response.headers.append(("Access-Control-Allow-Origin", origin))
            response.headers.append(("Access-Control-Allow-Credentials", "true"))
            response.headers.append(("Vary", "Origin"))
        return response

    def _route(self, method: str, path: str, query: dict, headers: dict, body: bytes) -> Response:
        if method == "OPTIONS":
            return Response(204, b"", "text/plain", [
                ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
                ("Access-Control-Allow-Headers", "Authorization, Content-Type, Accept"),
                ("Access-Control-Max-Age", "600"),
            ])
        if method == "GET" and path in ("/healthz", "/api/v1/health"):
            return _json(200, {"ok": True})
        if method == "GET" and path in ("/", "/index.html"):
            return Response(200, self.page, "text/html; charset=utf-8")
        if method == "GET" and path == "/favicon.svg":
            return Response(200, self.favicon, "image/svg+xml")
        if method == "POST" and path == "/login":
            return self._login(body)
        if method == "POST" and path == "/logout":
            return Response(204, b"", "text/plain", [_set_cookie("", 0)])
        from .routes_account import account_route
        account = account_route(self, method, path, query, headers, body)
        if account is not None:
            return account
        if method == "GET" and path == "/api/v1/projects":
            return _json(200, {"projects": self.store.projects_public()})
        task = _task_path(path)
        if task is not None:
            return self._work(method, task[1], headers, body, task[0])
        if path.startswith("/api/v1/work/"):
            action = path.removeprefix("/api/v1/work/")
            if action not in _TASKS:
                return _error(404, "not found", "not_found")
            return self._work(method, action, headers, body, DEFAULT_PROJECT)
        if path.startswith("/api/v1/"):
            if not self.store.admin_ok(_admin_token(headers)):
                return _error(401, "unauthorized", "unauthorized")
            return self._admin_read(method, path, query)
        return _error(404, "not found", "not_found")

    def _login(self, body: bytes) -> Response:
        payload = _payload(body)
        token = str(payload.get("token") or "")
        if not self.store.admin_ok(token):
            return _error(401, "unauthorized", "unauthorized")
        return Response(204, b"", "text/plain", [_set_cookie(token, 30 * 24 * 3600)])

    def _admin_read(self, method: str, path: str, query: dict) -> Response:
        if method != "GET":
            return _error(405, "method not allowed", "method")
        if path == "/api/v1/status":
            return _json(200, self.store.status())
        if path == "/api/v1/workers":
            return _json(200, {"workers": self.store.workers_public()})
        if path == "/api/v1/candidates":
            limit = _first_int(query.get("limit"), 50)
            return _json(200, {"candidates": self.store.candidates_public(limit)})
        if path == "/api/v1/events":
            after = _first_int(query.get("after"), 0)
            return _json(200, {"events": self.store.events_since(after)})
        project_read = _project_read(path)
        if project_read is not None:
            project, kind = project_read
            if kind == "status":
                return _json(200, self.store.status(project))
            limit = _first_int(query.get("limit"), 50)
            return _json(200, {"candidates": self.store.candidates_public(limit, project)})
        return _error(404, "not found", "not_found")

    def _work(self, method: str, action: str, headers: dict, body: bytes, project: str) -> Response:
        if method != "GET" and method != "POST":
            return _error(405, "method not allowed", "method")
        worker = self.store.authenticate(_bearer(headers))
        if worker is None:
            return _error(401, "unauthorized", "unauthorized")
        if method == "GET" and action == "current":
            return _json(200, {"assignment": self.store.current(worker["id"])})
        if method != "POST":
            return _error(404, "not found", "not_found")
        payload = _payload(body or b"{}")
        if action == "claim":
            assignment = self.store.claim(worker["id"], project=project)
            if assignment is None:
                return _json(200, {"assignment": None, "retry_after_s": 60})
            return _json(200, {"assignment": assignment})
        assignment_id = payload.get("assignment_id")
        if assignment_id is None:
            raise CoordError("assignment_id is required", 400)
        try:
            assignment_id = int(assignment_id)
        except (TypeError, ValueError) as exc:
            raise CoordError("assignment_id must be an integer", 400) from exc
        if action == "heartbeat":
            # ``progress`` is optional. Workers that only send assignment_id still renew the lease.
            return _json(200, self.store.heartbeat(
                worker["id"], assignment_id, project, progress=payload.get("progress"),
            ))
        if action == "release":
            self.store.release(worker["id"], assignment_id, project)
            return _json(200, {"status": "released"})
        if action == "submit":
            return _json(200, self.store.submit(
                worker["id"], assignment_id, payload.get("stars"), payload.get("candidates"), project,
            ))
        return _error(404, "not found", "not_found")


def _payload(body: bytes) -> dict:
    if not body:
        return {}
    if len(body) > _MAX_BODY:
        raise CoordError("body too large", 413)
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CoordError("body must be JSON", 400) from exc
    if not isinstance(data, dict):
        raise CoordError("body must be a JSON object", 400)
    return data


def _first_int(values, default: int) -> int:
    if not values:
        return default
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return default


def _task_path(path: str) -> tuple[str, str] | None:
    prefix = "/api/v1/projects/"
    if not path.startswith(prefix):
        return None
    parts = path[len(prefix):].split("/")
    if len(parts) == 3 and parts[1] == "tasks" and parts[2] in _TASKS:
        return parts[0], parts[2]
    return None


def _project_read(path: str) -> tuple[str, str] | None:
    prefix = "/api/v1/projects/"
    if not path.startswith(prefix):
        return None
    parts = path[len(prefix):].split("/")
    if len(parts) == 2 and parts[1] in ("status", "candidates"):
        return parts[0], parts[1]
    return None


def _code(status: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        413: "too_large",
        415: "unsupported",
        429: "rate_limited",
    }.get(status, "error")


def _load_page() -> bytes:
    return _load_asset(_PAGE)


def _load_asset(name: str) -> bytes:
    from pathlib import Path
    return (Path(__file__).with_name(name)).read_bytes()
