"""JSON account API and the small HTML fallbacks. Admin routes stay in httpapi."""
from __future__ import annotations

from urllib.parse import quote

from . import auth, identity, pages
from .httpapi import Response, _cookies, _error, _json, _payload
from .store import CoordError


def account_route(app, method: str, path: str, query: dict, headers: dict, body: bytes) -> Response | None:
    if path == "/signup":
        return _signup_html(app, method, headers, body, query)
    if path == "/login" and method == "GET":
        return _html(pages.login_page(next_path=auth.safe_next(_first(query, "next"))))
    if path == "/account/login" and method == "POST":
        return _login_html(app, headers, body)
    if path == "/account" and method == "GET":
        return _account_html(app, headers, query)
    if path == "/account/logout" and method == "POST":
        return _logout(app, headers, html=True)
    if path == "/account/verify/resend" and method == "POST":
        return _resend(app, headers, html=True)
    if path == "/link" and method == "GET":
        return _link_html(app, headers, query)
    if path == "/link/approve" and method == "POST":
        return _link_decision(app, headers, body, approve=True)
    if path == "/link/deny" and method == "POST":
        return _link_decision(app, headers, body, approve=False)
    if path == "/verify-email" and method == "GET":
        return _verify_html(app, query)
    device_revoke = _device_id(path, "/account/devices/", "/revoke")
    if device_revoke is not None and method == "POST":
        return _revoke_html(app, headers, device_revoke)

    if path == "/api/v1/signup" and method == "POST":
        return _signup_json(app, headers, body)
    if path == "/api/v1/login" and method == "POST":
        return _login_json(app, headers, body)
    if path == "/api/v1/logout" and method == "POST":
        return _logout(app, headers, html=False)
    if path == "/api/v1/me" and method == "GET":
        account = _require(app, headers)
        return _json(200, account)
    if path == "/api/v1/me/devices" and method == "GET":
        account = _require(app, headers)
        return _json(200, {"devices": identity.devices_for(app.store, account["id"])})
    if path == "/api/v1/me/stats" and method == "GET":
        account = _require(app, headers)
        return _json(200, identity.my_stats(app.store, account["id"]))
    if path == "/api/v1/public/stats" and method == "GET":
        return _json(200, identity.public_stats(app.store))
    if path == "/api/v1/device/start" and method == "POST":
        return _device_start(app, headers, body)
    if path == "/api/v1/device/poll" and method == "POST":
        return _device_poll(app, headers, body)
    if path == "/api/v1/device" and method == "GET":
        return _device_lookup(app, headers, query)
    if path == "/api/v1/device/approve" and method == "POST":
        return _device_decision_json(app, headers, body, approve=True)
    if path == "/api/v1/device/deny" and method == "POST":
        return _device_decision_json(app, headers, body, approve=False)
    if path == "/api/v1/verify-email/resend" and method == "POST":
        return _resend(app, headers, html=False)
    api_revoke = _device_id(path, "/api/v1/me/devices/", "/revoke")
    if api_revoke is not None and method == "POST":
        account = _require(app, headers)
        _origin(app, headers)
        identity.revoke_device(app.store, account["id"], api_revoke)
        return _json(200, {"status": "revoked"})
    if _known(path):
        return _error(405, "method not allowed", "method")
    return None


def _known(path: str) -> bool:
    if path in {
        "/signup", "/account", "/account/login", "/account/logout", "/account/verify/resend",
        "/link", "/link/approve", "/link/deny", "/verify-email",
        "/api/v1/signup", "/api/v1/login", "/api/v1/logout", "/api/v1/me",
        "/api/v1/me/devices", "/api/v1/me/stats", "/api/v1/public/stats",
        "/api/v1/device", "/api/v1/device/start", "/api/v1/device/poll",
        "/api/v1/device/approve", "/api/v1/device/deny", "/api/v1/verify-email/resend",
    }:
        return True
    return (
        _device_id(path, "/account/devices/", "/revoke") is not None
        or _device_id(path, "/api/v1/me/devices/", "/revoke") is not None
    )


def _signup_json(app, headers, body) -> Response:
    _origin(app, headers)
    payload = _json_body(body, headers)
    account, token, verification = _create(app, headers, payload)
    return _json(201, {**account, "email_verification": verification}, [_session(app, token)])


def _login_json(app, headers, body) -> Response:
    _origin(app, headers)
    payload = _json_body(body, headers)
    account, token = identity.login(
        app.store,
        email=str(payload.get("email") or ""),
        password=str(payload.get("password") or ""),
        ip=auth.client_ip(headers),
        user_agent=auth.header(headers, "user-agent"),
        session_days=app.accounts.session_days,
    )
    return _json(200, account, [_session(app, token)])


def _signup_html(app, method, headers, body, query) -> Response:
    nxt = auth.safe_next(_first(query, "next"))
    if method == "GET":
        return _html(pages.signup_page(invite=app.accounts.invite_only, next_path=nxt))
    if method != "POST":
        return _error(405, "method not allowed", "method")
    _origin(app, headers)
    form = _form(body)
    nxt = auth.safe_next(form.get("next") or "", nxt)
    try:
        _account, token, _verification = _create(app, headers, form)
    except CoordError as exc:
        return _html(pages.signup_page(
            error=str(exc),
            invite=app.accounts.invite_only,
            email=str(form.get("email") or ""),
            display_name=str(form.get("display_name") or ""),
            invite_code=str(form.get("invite_code") or ""),
            next_path=nxt,
        ), exc.status)
    return _redirect(nxt, [_session(app, token)])


def _login_html(app, headers, body) -> Response:
    _origin(app, headers)
    form = _form(body)
    nxt = auth.safe_next(form.get("next") or "")
    try:
        _account, token = identity.login(
            app.store,
            email=str(form.get("email") or ""),
            password=str(form.get("password") or ""),
            ip=auth.client_ip(headers),
            user_agent=auth.header(headers, "user-agent"),
            session_days=app.accounts.session_days,
        )
    except CoordError as exc:
        return _html(pages.login_page(
            error=str(exc), email=str(form.get("email") or ""), next_path=nxt,
        ), exc.status)
    return _redirect(nxt, [_session(app, token)])


def _create(app, headers, payload: dict) -> tuple[dict, str, str]:
    account, token = identity.signup(
        app.store,
        email=str(payload.get("email") or ""),
        password=str(payload.get("password") or ""),
        display_name=str(payload.get("display_name") or ""),
        invite_code=str(payload.get("invite_code") or payload.get("invite") or ""),
        require_invite=app.accounts.invite_only,
        ip=auth.client_ip(headers),
        user_agent=auth.header(headers, "user-agent"),
        session_days=app.accounts.session_days,
    )
    return account, token, _send_verification(app, account, auth.client_ip(headers))


def _send_verification(app, account: dict, ip: str) -> str:
    if not app.mailer.enabled():
        return "skipped"
    try:
        token = identity.issue_email_token(app.store, account["id"], ip)
    except CoordError:
        return "failed"
    link = app.accounts.api_public_url.rstrip("/") + "/verify-email?token=" + quote(token)
    body = (
        f"Confirm {account['email']} for Mega Rhexas.\n\n{link}\n\n"
        "The link expires in 48 hours. If you did not sign up, ignore this message.\n"
    )
    try:
        app.mailer.send(account["email"], "Verify your Mega Rhexas email", body)
    except Exception:
        identity.note(app.store, "warn", f"verification email failed for {account['display_name']}")
        return "failed"
    return "sent"


def _logout(app, headers, *, html: bool) -> Response:
    _origin(app, headers)
    identity.logout(app.store, _session_token(headers))
    cookie = auth.session_cookie("", 0, secure=app.accounts.secure_cookies)
    if html:
        return _redirect("/login", [cookie])
    return Response(204, b"", "text/plain", [cookie])


def _account_html(app, headers, query) -> Response:
    account = identity.account_for_session(app.store, _session_token(headers))
    if account is None:
        return _redirect("/login?next=/account")
    note = ""
    if _first(query, "verified"):
        note = "Email verified."
    elif _first(query, "linked"):
        note = "This computer is linked. You can close this page."
    elif _first(query, "denied"):
        note = "Link cancelled."
    elif _first(query, "resent"):
        note = "Verification email sent."
    stats = identity.my_stats(app.store, account["id"])
    # The session was opened before verification; reload so the flag is current.
    account = identity.account_for_session(app.store, _session_token(headers)) or account
    return _html(pages.account_page(
        account=account,
        devices=identity.devices_for(app.store, account["id"]),
        stats=stats,
        note=note,
        verify_offered=app.mailer.enabled(),
    ))


def _link_html(app, headers, query) -> Response:
    code = _first(query, "code")
    if not code:
        return _html(pages.message_page(
            "Link a computer",
            "Missing code. On the computer you want to link, run rhexas worker --login.",
            error=True,
        ), 400)
    try:
        preview = identity.device_preview(app.store, code)
    except CoordError as exc:
        return _html(pages.link_page(code=code, hostname="", logged_in=False, error=str(exc)), exc.status)
    account = identity.account_for_session(app.store, _session_token(headers))
    return _html(pages.link_page(
        code=preview["code"],
        hostname=preview["hostname"],
        logged_in=account is not None,
        display_name=account["display_name"] if account else "",
    ))


def _link_decision(app, headers, body, *, approve: bool) -> Response:
    _origin(app, headers)
    account = identity.account_for_session(app.store, _session_token(headers))
    form = _form(body)
    code = str(form.get("code") or "")
    if account is None:
        return _redirect("/login?next=" + quote("/link?code=" + code, safe="/?=&"))
    try:
        if approve:
            identity.device_approve(app.store, account["id"], code)
            return _redirect("/account?linked=1")
        identity.device_deny(app.store, account["id"], code)
        return _redirect("/account?denied=1")
    except CoordError as exc:
        return _html(pages.link_page(code=code, hostname="", logged_in=True, error=str(exc)), exc.status)


def _verify_html(app, query) -> Response:
    try:
        identity.confirm_email(app.store, _first(query, "token"))
    except CoordError as exc:
        return _html(pages.message_page("Verification", str(exc), error=True), exc.status)
    return _redirect("/account?verified=1")


def _revoke_html(app, headers, device_id: int) -> Response:
    _origin(app, headers)
    account = identity.account_for_session(app.store, _session_token(headers))
    if account is None:
        return _redirect("/login?next=/account")
    try:
        identity.revoke_device(app.store, account["id"], device_id)
    except CoordError as exc:
        return _html(pages.message_page("Devices", str(exc), error=True), exc.status)
    return _redirect("/account")


def _resend(app, headers, *, html: bool) -> Response:
    _origin(app, headers)
    account = _require(app, headers) if not html else identity.account_for_session(
        app.store, _session_token(headers),
    )
    if account is None:
        if html:
            return _redirect("/login?next=/account")
        raise CoordError("unauthorized", 401)
    if not app.mailer.enabled():
        if html:
            return _html(pages.message_page(
                "Verification", "Email verification is not configured.", error=True,
            ), 400)
        raise CoordError("email verification is not configured", 400)
    status = _send_verification(app, account, auth.client_ip(headers))
    if html:
        if status == "sent":
            return _redirect("/account?resent=1")
        return _html(pages.message_page("Verification", "Could not send email.", error=True), 502)
    return _json(200, {"email_verification": status})


def _device_start(app, headers, body) -> Response:
    payload = _json_body(body, headers)
    started = identity.device_start(
        app.store,
        str(payload.get("hostname") or ""),
        auth.client_ip(headers),
    )
    started["verification_url"] = app.accounts.verification_url()
    return _json(200, started)


def _device_poll(app, headers, body) -> Response:
    payload = _json_body(body, headers)
    status, result = identity.device_poll(
        app.store, str(payload.get("device_code") or ""), auth.client_ip(headers),
    )
    return _json(status, result)


def _device_lookup(app, headers, query) -> Response:
    account = _require(app, headers)
    preview = identity.device_preview(app.store, _first(query, "code"))
    preview["account"] = account["display_name"]
    return _json(200, preview)


def _device_decision_json(app, headers, body, *, approve: bool) -> Response:
    _origin(app, headers)
    account = _require(app, headers)
    payload = _json_body(body, headers)
    code = str(payload.get("code") or payload.get("user_code") or "")
    if approve:
        return _json(200, identity.device_approve(app.store, account["id"], code))
    return _json(200, identity.device_deny(app.store, account["id"], code))


def _require(app, headers) -> dict:
    account = identity.account_for_session(app.store, _session_token(headers))
    if account is None:
        raise CoordError("unauthorized", 401)
    return account


def _origin(app, headers) -> None:
    origin = auth.header(headers, "origin").strip()
    if origin and not app.accounts.origin_allowed(origin):
        raise CoordError("origin not allowed", 403)


def _session(app, token: str) -> tuple[str, str]:
    return auth.session_cookie(
        token,
        app.accounts.session_days * 86400,
        secure=app.accounts.secure_cookies,
    )


def _session_token(headers) -> str:
    return _cookies(auth.header(headers, "cookie")).get(auth.SESSION_COOKIE, "")


def _json_body(body: bytes, headers) -> dict:
    if body and "application/json" not in auth.header(headers, "content-type").lower():
        raise CoordError("body must be JSON", 415)
    return _payload(body or b"{}")


def _form(body: bytes) -> dict:
    if not body:
        return {}
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CoordError("body must be UTF-8", 400) from exc
    from urllib.parse import parse_qs
    parsed = parse_qs(text, keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


def _first(query: dict, key: str) -> str:
    values = query.get(key) or []
    if not values:
        return ""
    return str(values[0])


def _device_id(path: str, prefix: str, suffix: str) -> int | None:
    if not (path.startswith(prefix) and path.endswith(suffix)):
        return None
    middle = path[len(prefix):-len(suffix)]
    if not middle.isdigit():
        return None
    return int(middle)


def _html(page: bytes, status: int = 200) -> Response:
    return Response(status, page, "text/html; charset=utf-8")


def _redirect(location: str, extra: list | None = None) -> Response:
    headers = [("Location", location)]
    if extra:
        headers.extend(extra)
    return Response(303, b"", "text/plain", headers)
