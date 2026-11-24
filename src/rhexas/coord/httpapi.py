"""HTTP routes for the coordinator. No sockets here, so tests can call ``App.handle``."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote

from .projects import DEFAULT_PROJECT
from .store import CoordError, Store
from .vetting import PRIORITY_HELP, REVIEW_STATUSES

_TASKS = {"claim", "current", "heartbeat", "submit", "release", "peek", "receipt"}

_PAGE = "status.html"
_SECTORS_PAGE = "sectors.html"
_DISCOVERIES_PAGE = "discoveries.html"
_DISCOVERY_PAGE = "discovery_detail.html"
_CANDIDATES_PAGE = "candidates.html"
_VETTING_PAGE = "vetting.html"
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
        self.sectors_page = _load_asset(_SECTORS_PAGE)
        self.discoveries_page = _load_asset(_DISCOVERIES_PAGE)
        self.discovery_page = _load_asset(_DISCOVERY_PAGE)
        self.candidates_page = _load_asset(_CANDIDATES_PAGE)
        self.vetting_page = _load_asset(_VETTING_PAGE)
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
        if method == "GET" and path == "/sectors":
            return self._sectors_page(headers)
        if method == "GET" and path == "/discoveries":
            return self._discoveries_page(headers)
        if method == "GET" and path == "/candidates":
            return self._candidates_page(headers)
        if method == "GET" and path == "/vetting":
            return self._vetting_page(headers)
        discovery = _discovery_tic_path(path)
        if method == "GET" and discovery is not None:
            return self._discovery_detail_page(headers, discovery)
        if method == "GET" and path.startswith("/api/v1/discoveries/") and path.endswith("/thumb.svg"):
            return self._discovery_thumb(headers, path)
        if method == "GET" and path == "/api/v1/mission/progress":
            return self._mission_progress(headers, query)
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
            if path == "/api/v1/mission/overview":
                if method != "GET":
                    return _error(405, "method not allowed", "method")
                if not self._operator_ok(headers):
                    return _error(401, "unauthorized", "unauthorized")
                from .mission import mission_progress
                project = _first_text(query.get("project"))
                return _json(200, mission_progress(self.store, project or None))
            if path == "/api/v1/discoveries":
                if method != "GET":
                    return _error(405, "method not allowed", "method")
                if not self._operator_ok(headers):
                    return _error(401, "unauthorized", "unauthorized")
                return self._discoveries_api(query)
            detail_path = _discovery_api_path(path)
            if detail_path is not None:
                if method != "GET":
                    return _error(405, "method not allowed", "method")
                if not self._operator_ok(headers):
                    return _error(401, "unauthorized", "unauthorized")
                return self._discovery_api(detail_path, query)
            science = _science_api(method, path)
            if science is not None:
                if path == "/api/v1/candidates/science":
                    if not self._worker_or_operator(headers):
                        return _error(401, "unauthorized", "unauthorized")
                elif not self._operator_ok(headers):
                    return _error(401, "unauthorized", "unauthorized")
                return science(self, query, body)
            local = _local_check_api(method, path)
            if local is not None:
                worker = self.store.authenticate(_bearer(headers))
                if worker is None:
                    return _error(401, "unauthorized", "unauthorized")
                return local(self, query, body, worker)
            if method == "POST" and path == "/api/v1/candidates/review":
                if not self._operator_ok(headers):
                    return _error(401, "unauthorized", "unauthorized")
                return self._set_review(body)
            if not self.store.admin_ok(_admin_token(headers)):
                return _error(401, "unauthorized", "unauthorized")
            return self._admin_read(method, path, query)
        return _error(404, "not found", "not_found")

    def _operator_ok(self, headers: dict) -> bool:
        from . import identity
        from .auth import SESSION_COOKIE
        if self.store.admin_ok(_admin_token(headers)):
            return True
        cookie_header = ""
        for key, value in headers.items():
            if key.lower() == "cookie":
                cookie_header = value
                break
        session = _cookies(cookie_header).get(SESSION_COOKIE, "")
        return identity.account_for_session(self.store, session) is not None

    def _worker_or_operator(self, headers: dict) -> bool:
        if self._operator_ok(headers):
            return True
        worker = self.store.authenticate(_bearer(headers))
        return worker is not None

    def _sectors_page(self, headers: dict) -> Response:
        if not self._operator_ok(headers):
            from .auth import safe_next
            from .routes_account import _redirect
            return _redirect(f"/login?next={quote('/sectors')}")
        return Response(200, self.sectors_page, "text/html; charset=utf-8")

    def _discoveries_page(self, headers: dict) -> Response:
        if not self._operator_ok(headers):
            from .routes_account import _redirect
            return _redirect(f"/login?next={quote('/discoveries')}")
        return Response(200, self.discoveries_page, "text/html; charset=utf-8")

    def _candidates_page(self, headers: dict) -> Response:
        if not self._operator_ok(headers):
            from .routes_account import _redirect
            return _redirect(f"/login?next={quote('/candidates')}")
        return Response(200, self.candidates_page, "text/html; charset=utf-8")

    def _vetting_page(self, headers: dict) -> Response:
        if not self._operator_ok(headers):
            from .routes_account import _redirect
            return _redirect(f"/login?next={quote('/vetting')}")
        return Response(200, self.vetting_page, "text/html; charset=utf-8")

    def _science_dashboard(self, query: dict) -> Response:
        limit = _first_int(query.get("limit"), 200)
        project = _first_text(query.get("project")) or None
        refresher = getattr(self.store, "science_refresher", None)
        refresh_meta = refresher.status() if refresher is not None else {"stale": True, "refreshing": False}
        return _json(200, {
            **self.store.candidates_science(limit=limit, project=project),
            "science_refresh": refresh_meta,
            "priority_help": PRIORITY_HELP,
            "disclaimer": (
                "Transiting signal candidates only — not confirmed or newly discovered planets."
            ),
        })

    def _post_local_check(self, query: dict, body: bytes, worker: dict) -> Response:
        tic_raw = _first_text(query.get("tic"))
        if not tic_raw:
            return _error(400, "tic query parameter is required", "bad_request")
        try:
            tic = int(tic_raw)
        except ValueError:
            return _error(400, "tic must be an integer", "bad_request")
        try:
            if body:
                import json
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("object")
                payload = dict(payload)
                payload.setdefault("tic", tic)
            else:
                payload = {"tic": tic}
            stored = self.store.save_local_check(int(worker["id"]), payload)
        except CoordError as exc:
            return _error(exc.status, str(exc), _code(exc.status))
        except Exception:
            return _error(400, "invalid local check body", "bad_request")
        return _json(200, stored)
    def _science_refresh_trigger(self) -> Response:
        refresher = getattr(self.store, "science_refresher", None)
        if refresher is None:
            from .science import refresh_candidate_recheck

            refresh_candidate_recheck(self.store)
            return _json(200, {"refreshing": False, "triggered": "inline"})
        refresher.trigger()
        return _json(200, {"triggered": True, **refresher.status()})

    def _science_export(self, query: dict) -> Response:
        fmt = _first_text(query.get("format")) or "md"
        shortlist = _first_text(query.get("shortlist")).lower() in ("1", "true", "yes")
        tic_raw = _first_text(query.get("tic"))
        tic = None
        if tic_raw:
            try:
                tic = int(tic_raw)
            except ValueError as exc:
                raise CoordError("tic must be an integer", 400) from exc
        if not shortlist and tic is None:
            raise CoordError("tic or shortlist=1 is required", 400)
        content_type, ext, payload = self.store.candidate_export(tic, shortlist=shortlist, fmt=fmt)
        name = "shortlist" if shortlist else (f"TIC{tic}" if tic is not None else "candidates")
        headers = [("Content-Disposition", f'attachment; filename="{name}.{ext}"')]
        return Response(200, payload, content_type, headers)

    def _discovery_detail_page(self, headers: dict, tic: int) -> Response:
        if not self._operator_ok(headers):
            from .routes_account import _redirect
            return _redirect(f"/login?next={quote(f'/discoveries/{tic}')}")
        html = self.discovery_page.replace(b"__TIC__", str(int(tic)).encode("utf-8"))
        return Response(200, html, "text/html; charset=utf-8")

    def _discoveries_api(self, query: dict) -> Response:
        project = _first_text(query.get("project")) or None
        page = _first_int(query.get("page"), 1)
        per_page = _first_int(query.get("per_page"), 24)
        status = _first_text(query.get("status")) or None
        sort = _first_text(query.get("sort")) or "priority"
        return _json(200, self.store.discoveries_gallery(
            page=page, per_page=per_page, status=status, sort=sort, project=project,
        ))

    def _discovery_api(self, tic: int, query: dict) -> Response:
        project = _first_text(query.get("project")) or None
        detail = self.store.discovery_detail(tic, project)
        if detail is None:
            return _error(404, "no candidate for that TIC", "not_found")
        return _json(200, detail)

    def _discovery_thumb(self, headers: dict, path: str) -> Response:
        if not self._operator_ok(headers):
            return _error(401, "unauthorized", "unauthorized")
        try:
            tic = int(path.split("/")[-2])
        except (IndexError, ValueError):
            return _error(404, "not found", "not_found")
        from .discoveries_gallery import read_or_write_thumbnail, thumb_cache_dir
        body = read_or_write_thumbnail(thumb_cache_dir(self.store.path), tic, store=self.store)
        return Response(200, body, "image/svg+xml")

    def _mission_progress(self, headers: dict, query: dict) -> Response:
        if not self._worker_or_operator(headers):
            return _error(401, "unauthorized", "unauthorized")
        from .mission import compact_progress
        project = _first_text(query.get("project"))
        return _json(200, compact_progress(self.store, project or None))

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
            return _json(200, {"candidates": self.store.candidates_public(limit), "priority_help": PRIORITY_HELP})
        if path == "/api/v1/events":
            after = _first_int(query.get("after"), 0)
            return _json(200, {"events": self.store.events_since(after)})
        project_read = _project_read(path)
        if project_read is not None:
            project, kind = project_read
            if kind == "status":
                return _json(200, self.store.status(project))
            limit = _first_int(query.get("limit"), 50)
            return _json(200, {
                "candidates": self.store.candidates_public(limit, project),
                "priority_help": PRIORITY_HELP,
            })
        return _error(404, "not found", "not_found")

    def _set_review(self, body: bytes) -> Response:
        payload = _payload(body)
        try:
            tic = int(payload.get("tic"))
        except (TypeError, ValueError):
            raise CoordError("tic must be an integer", 400) from None
        raw = payload.get("status", None)
        if raw is None or str(raw).strip().lower() in ("", "auto", "automatic"):
            status = None
        else:
            status = str(raw).strip().lower()
            if status not in REVIEW_STATUSES:
                raise CoordError("unknown status", 400)
        note = payload.get("note", "")
        if note is None:
            note = ""
        if not isinstance(note, str):
            raise CoordError("note must be text", 400)
        return _json(200, self.store.set_candidate_review(tic, status, note))

    def _work(self, method: str, action: str, headers: dict, body: bytes, project: str) -> Response:
        if method != "GET" and method != "POST":
            return _error(405, "method not allowed", "method")
        worker = self.store.authenticate(_bearer(headers))
        if worker is None:
            return _error(401, "unauthorized", "unauthorized")
        if method == "GET" and action == "current":
            leases = self.store.leases(worker["id"])
            return _json(200, {
                "assignment": leases[0] if leases else None,
                "assignments": leases,
            })
        if method != "POST":
            return _error(404, "not found", "not_found")
        payload = _payload(body or b"{}")
        if action == "claim":
            # ``prefetch`` asks for another lease, up to max_outstanding.
            # Omitted, claim stays one lease so a v0.3 worker is unchanged.
            # ``sectors`` is optional. The installed app omits it.
            assignment = self.store.claim(
                worker["id"], project=project, prefetch=payload.get("prefetch") is True,
                sectors=_sector_list(payload.get("sectors")),
            )
            if assignment is None:
                return _json(200, {"assignment": None, "retry_after_s": 60})
            return _json(200, {"assignment": assignment})
        if action == "peek":
            preview = self.store.peek(
                worker["id"], project=project, sectors=_sector_list(payload.get("sectors")),
            )
            return _json(200, {"preview": preview})
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
        if action == "receipt":
            return _json(200, self.store.receipt(worker["id"], assignment_id, project))
        if action == "submit":
            return _json(200, self.store.submit(
                worker["id"], assignment_id, payload.get("stars"), payload.get("candidates"), project,
            ))
        return _error(404, "not found", "not_found")


def _sector_list(raw) -> list[int] | None:
    """Sector filter from a claim body. Missing or empty means no filter."""
    if raw is None:
        return None
    if isinstance(raw, (str, int)) and not isinstance(raw, bool):
        raw = [raw]
    if not isinstance(raw, list):
        return None
    found: list[int] = []
    for item in raw:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0:
            found.append(number)
    return found or None


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


def _first_text(values) -> str:
    if not values:
        return ""
    return str(values[0]).strip()


def _first_int(values, default: int) -> int:
    if not values:
        return default
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return default


def _discovery_tic_path(path: str) -> int | None:
    prefix = "/discoveries/"
    if not path.startswith(prefix):
        return None
    tail = path[len(prefix):].strip("/")
    if not tail.isdigit():
        return None
    return int(tail)


def _discovery_api_path(path: str) -> int | None:
    prefix = "/api/v1/discoveries/"
    if not path.startswith(prefix) or path.endswith("/thumb.svg"):
        return None
    tail = path[len(prefix):].strip("/")
    if not tail.isdigit():
        return None
    return int(tail)


def _task_path(path: str) -> tuple[str, str] | None:
    prefix = "/api/v1/projects/"
    if not path.startswith(prefix):
        return None
    parts = path[len(prefix):].split("/")
    if len(parts) == 3 and parts[1] == "tasks" and parts[2] in _TASKS:
        return parts[0], parts[2]
    return None


def _science_api(method: str, path: str):
    if method == "GET" and path == "/api/v1/candidates/science":
        return lambda app, query, body: app._science_dashboard(query)
    if method == "POST" and path == "/api/v1/candidates/science/refresh":
        return lambda app, query, body: app._science_refresh_trigger()
    if method == "GET" and path == "/api/v1/candidates/export":
        return lambda app, query, body: app._science_export(query)
    return None


def _local_check_api(method: str, path: str):
    if method == "POST" and path == "/api/v1/candidates/local-check":
        return lambda app, query, body, worker: app._post_local_check(query, body, worker)
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
