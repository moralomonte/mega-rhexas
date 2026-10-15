"""Accounts, sessions, invite codes, and device grants.

The coordinator used to treat each invite token as a person. An account can now
own several devices. Existing invites are legacy accounts named after the invite,
so those tokens still work and still count as different people.

Credit stays a list of contribution rows. Verified stays 0 until a different
account agrees. Phase 4 can set ``revoked`` on a pending row and can fill
``accounts.tier`` and ``accounts.trust_score``. This module does not keep a
point balance.
"""
from __future__ import annotations

import re
import secrets
import sqlite3
from datetime import timedelta

from . import auth
from .logic import check_worker_name
from .store import CoordError, hash_token, iso, new_token, parse_iso


def signup(store, *, email: str, password: str, display_name: str, invite_code: str,
           require_invite: bool, ip: str, user_agent: str, session_days: int,
           company: str = "") -> tuple[dict, str]:
    _gate(store, [
        ("signup", f"ip:{ip}", auth.SIGNUP_PER_IP, auth.SIGNUP_WINDOW_S),
        ("signup", "global", auth.SIGNUP_PER_HOUR_GLOBAL, auth.SIGNUP_WINDOW_S),
    ], record=True)
    if str(company or "").strip():
        raise CoordError("could not create the account", 400)
    email_key = auth.normalize_email(email)
    display = auth.check_display_name(display_name)
    password = auth.check_password(password)
    digest = auth.hash_password(password)
    try:
        with store._tx() as conn:
            if require_invite:
                _consume_invite(conn, invite_code)
            if conn.execute("SELECT 1 FROM accounts WHERE email_key=?", (email_key,)).fetchone():
                raise CoordError("an account with that email already exists", 409)
            if conn.execute(
                "SELECT 1 FROM accounts WHERE display_key=?", (display.casefold(),),
            ).fetchone():
                raise CoordError("that display name is taken", 409)
            now = iso(store.clock())
            cur = conn.execute(
                """INSERT INTO accounts (
                       email, email_key, password_hash, display_name, display_key, provider,
                       provider_sub, email_verified_at, created_at, legacy, trust_score, tier
                   ) VALUES (?, ?, ?, ?, ?, 'password', NULL, NULL, ?, 0, NULL, '')""",
                (email_key, email_key, digest, display, display.casefold(), now),
            )
            account_id = int(cur.lastrowid)
            store._event(conn, "info", f"account created {display}")
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            account = public_account(row)
            token = _open_session(store, conn, account_id, user_agent, session_days)
    except sqlite3.IntegrityError as exc:
        raise CoordError("an account with that email or display name already exists", 409) from exc
    return account, token


def login(store, *, email: str, password: str, ip: str, user_agent: str, session_days: int) -> tuple[dict, str]:
    try:
        email_key = auth.normalize_email(email)
    except CoordError:
        email_key = ""
    _gate(store, _login_checks(ip, email_key), record=False)
    row = None
    if email_key:
        row = store.conn.execute(
            "SELECT * FROM accounts WHERE email_key=? AND provider='password'",
            (email_key,),
        ).fetchone()
    stored = row["password_hash"] if row is not None and row["password_hash"] else auth.dummy_hash()
    ok = bool(password) and len(password) <= 200 and auth.verify_password(password, stored)
    if row is None or not row["password_hash"] or not ok:
        _gate(store, _login_checks(ip, email_key), record=True)
        raise CoordError("incorrect email or password", 401)
    account = public_account(row)
    with store._tx() as conn:
        token = _open_session(store, conn, int(row["id"]), user_agent, session_days)
    return account, token


def logout(store, token: str) -> None:
    if not token:
        return
    with store._tx() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (hash_token(token),))


def account_for_session(store, token: str) -> dict | None:
    if not token or not token.startswith(auth.SESSION_PREFIX):
        return None
    now = iso(store.clock())
    with store._tx() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        row = conn.execute(
            """SELECT a.* FROM sessions s
               JOIN accounts a ON a.id = s.account_id
               WHERE s.token_hash=?""",
            (hash_token(token),),
        ).fetchone()
    if row is None:
        return None
    return public_account(row)


def public_account(row) -> dict:
    return {
        "id": int(row["id"]),
        "email": row["email"],
        "display_name": row["display_name"],
        "email_verified": bool(row["email_verified_at"]),
        "provider": row["provider"],
        "legacy": bool(row["legacy"]),
        "created_at": row["created_at"],
    }


def create_invite(store, *, uses: int = 1, label: str = "") -> str:
    if isinstance(uses, bool) or not isinstance(uses, int) or not 1 <= uses <= 1000:
        raise ValueError("uses must be from 1 to 1000")
    label = " ".join((label or "").split())[:40]
    code = "inv_" + secrets.token_urlsafe(12)
    with store._tx() as conn:
        conn.execute(
            """INSERT INTO invite_codes (code_hash, label, uses_left, created_at)
               VALUES (?, ?, ?, ?)""",
            (hash_token(code), label, int(uses), iso(store.clock())),
        )
        store._event(conn, "info", "invite created" + (f" for {label}" if label else ""))
    return code


def list_invites(store) -> list[dict]:
    rows = store.conn.execute(
        "SELECT id, label, uses_left, created_at, revoked_at FROM invite_codes ORDER BY id"
    ).fetchall()
    return [dict(row) for row in rows]


def revoke_invite(store, code_or_id: str) -> None:
    text = (code_or_id or "").strip()
    if not text:
        raise ValueError("invite id or code is required")
    with store._tx() as conn:
        if text.isdigit():
            row = conn.execute(
                "SELECT id, revoked_at FROM invite_codes WHERE id=?", (int(text),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, revoked_at FROM invite_codes WHERE code_hash=?",
                (hash_token(text),),
            ).fetchone()
        if row is None:
            raise ValueError("no invite with that id or code")
        if row["revoked_at"]:
            return
        conn.execute(
            "UPDATE invite_codes SET revoked_at=? WHERE id=?",
            (iso(store.clock()), row["id"]),
        )
        store._event(conn, "warn", f"invite {row['id']} revoked")


def devices_for(store, account_id: int) -> list[dict]:
    rows = store.conn.execute(
        """SELECT id, name, hostname, kind, created_at, last_seen_at, revoked_at
           FROM workers WHERE account_id=? ORDER BY id""",
        (int(account_id),),
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "name": row["name"],
            "hostname": row["hostname"] or "",
            "kind": row["kind"] or "legacy",
            "created_at": row["created_at"],
            "last_seen_at": row["last_seen_at"],
            "revoked_at": row["revoked_at"],
        }
        for row in rows
    ]


def revoke_device(store, account_id: int, device_id: int) -> None:
    with store._tx() as conn:
        row = conn.execute(
            "SELECT id, name, account_id, revoked_at FROM workers WHERE id=?",
            (int(device_id),),
        ).fetchone()
        if row is None or row["account_id"] is None or int(row["account_id"]) != int(account_id):
            raise CoordError("device not found", 404)
        if row["revoked_at"]:
            return
        _revoke_worker_id(conn, store, int(row["id"]), "owner revoked the device")


def device_start(store, hostname: str, ip: str) -> dict:
    _gate(store, [
        ("device_start", f"ip:{ip}", auth.DEVICE_START_PER_IP, auth.DEVICE_START_WINDOW_S),
    ], record=True)
    hostname = auth.clean_hostname(hostname)
    now = store.clock()
    expires = now + timedelta(seconds=auth.DEVICE_TTL_S)
    device_code = secrets.token_urlsafe(32)
    for _ in range(5):
        user_code = auth.new_user_code()
        try:
            with store._tx() as conn:
                conn.execute(
                    """INSERT INTO device_grants (
                           device_code_hash, user_code_hash, hostname, status, created_at,
                           expires_at, interval_s
                       ) VALUES (?, ?, ?, 'pending', ?, ?, ?)""",
                    (
                        hash_token(device_code),
                        hash_token(auth.canonical_user_code(user_code)),
                        hostname,
                        iso(now),
                        iso(expires),
                        auth.DEVICE_INTERVAL_S,
                    ),
                )
            break
        except sqlite3.IntegrityError:
            continue
    else:
        raise CoordError("could not start device login", 500)
    return {
        "device_code": device_code,
        "user_code": user_code,
        "interval": auth.DEVICE_INTERVAL_S,
        "expires_in": auth.DEVICE_TTL_S,
    }


def device_poll(store, device_code: str, ip: str) -> tuple[int, dict]:
    _gate(store, [
        ("device_poll", f"ip:{ip}", auth.DEVICE_POLL_PER_IP, auth.DEVICE_POLL_WINDOW_S),
    ], record=True)
    if not device_code or len(device_code) > 200:
        return 400, {"status": "expired", "error": "code expired"}
    now = store.clock()
    with store._tx() as conn:
        _expire_device_grants(store, conn, now)
        row = conn.execute(
            "SELECT * FROM device_grants WHERE device_code_hash=?",
            (hash_token(device_code),),
        ).fetchone()
        if row is None:
            return 400, {"status": "expired", "error": "code expired"}
        status = row["status"]
        if status == "pending":
            interval = int(row["interval_s"])
            if row["last_poll_at"]:
                elapsed = (now - parse_iso(row["last_poll_at"])).total_seconds()
                if elapsed < max(0, interval - 0.5):
                    interval = min(interval + 5, 30)
                    conn.execute(
                        "UPDATE device_grants SET interval_s=?, last_poll_at=? WHERE id=?",
                        (interval, iso(now), row["id"]),
                    )
                    return 200, {"status": "slow_down", "interval": interval}
            conn.execute(
                "UPDATE device_grants SET last_poll_at=? WHERE id=?",
                (iso(now), row["id"]),
            )
            return 200, {"status": "authorization_pending", "interval": interval}
        if status == "approved" and row["delivery_token"]:
            token = row["delivery_token"]
            cur = conn.execute(
                """UPDATE device_grants
                   SET status='consumed', delivery_token=NULL
                   WHERE id=? AND status='approved' AND delivery_token IS NOT NULL""",
                (row["id"],),
            )
            if cur.rowcount != 1:
                return 400, {"status": "consumed", "error": "code already used"}
            return 200, {"status": "approved", "token": token}
        if status == "denied":
            return 400, {"status": "denied", "error": "the request was denied"}
        if status == "consumed":
            return 400, {"status": "consumed", "error": "code already used"}
        return 400, {"status": "expired", "error": "code expired"}


def device_preview(store, user_code: str) -> dict:
    canonical = auth.canonical_user_code(user_code)
    with store._tx() as conn:
        _expire_device_grants(store, conn, store.clock())
        row = _pending_grant(conn, canonical)
        hostname = row["hostname"]
    return {
        "hostname": hostname,
        "status": "pending",
        "code": auth.format_user_code(canonical),
    }


def device_approve(store, account_id: int, user_code: str) -> dict:
    canonical = auth.canonical_user_code(user_code)
    token = new_token()
    with store._tx() as conn:
        _expire_device_grants(store, conn, store.clock())
        row = _pending_grant(conn, canonical)
        linked = conn.execute(
            """SELECT COUNT(*) AS n FROM workers
               WHERE account_id=? AND revoked_at IS NULL""",
            (int(account_id),),
        ).fetchone()["n"]
        if int(linked) >= auth.MAX_DEVICES_PER_ACCOUNT:
            raise CoordError("this account already has the maximum number of linked computers", 409)
        name = _device_worker_name(conn, row["hostname"])
        now = iso(store.clock())
        cur = conn.execute(
            """INSERT INTO workers (name, token_hash, created_at, account_id, hostname, kind)
               VALUES (?, ?, ?, ?, ?, 'device')""",
            (name, hash_token(token), now, int(account_id), row["hostname"]),
        )
        worker_id = int(cur.lastrowid)
        updated = conn.execute(
            """UPDATE device_grants
               SET status='approved', account_id=?, worker_id=?, delivery_token=?
               WHERE id=? AND status='pending'""",
            (int(account_id), worker_id, token, row["id"]),
        )
        if updated.rowcount != 1:
            raise CoordError("that code is expired or already used", 409)
        account = conn.execute(
            "SELECT display_name FROM accounts WHERE id=?", (int(account_id),),
        ).fetchone()
        label = account["display_name"] if account else str(account_id)
        hostname = row["hostname"]
        store._event(conn, "info", f"{label} linked {hostname} ({name})")
    return {"hostname": hostname, "device_id": worker_id, "name": name}


def device_deny(store, account_id: int, user_code: str) -> dict:
    canonical = auth.canonical_user_code(user_code)
    with store._tx() as conn:
        _expire_device_grants(store, conn, store.clock())
        row = _pending_grant(conn, canonical)
        hostname = row["hostname"]
        conn.execute(
            """UPDATE device_grants SET status='denied', account_id=?
               WHERE id=? AND status='pending'""",
            (int(account_id), row["id"]),
        )
        store._event(conn, "info", f"device link denied for {hostname}")
    return {"hostname": hostname, "status": "denied"}


def issue_email_token(store, account_id: int, ip: str) -> str:
    _gate(store, [
        ("resend", f"account:{int(account_id)}", auth.RESEND_PER_ACCOUNT, auth.RESEND_WINDOW_S),
        ("resend", f"ip:{ip}", auth.RESEND_PER_ACCOUNT, auth.RESEND_WINDOW_S),
    ], record=True)
    token = secrets.token_urlsafe(32)
    now = store.clock()
    with store._tx() as conn:
        row = conn.execute(
            "SELECT email, email_verified_at FROM accounts WHERE id=?", (int(account_id),),
        ).fetchone()
        if row is None or not row["email"]:
            raise CoordError("account not found", 404)
        if row["email_verified_at"]:
            raise CoordError("email is already verified", 409)
        conn.execute(
            """INSERT INTO email_tokens (account_id, token_hash, created_at, expires_at)
               VALUES (?, ?, ?, ?)""",
            (
                int(account_id),
                hash_token(token),
                iso(now),
                iso(now + timedelta(hours=48)),
            ),
        )
    return token


def confirm_email(store, token: str) -> dict:
    if not token or len(token) > 200:
        raise CoordError("verification link is not valid", 400)
    now = iso(store.clock())
    with store._tx() as conn:
        row = conn.execute(
            """SELECT * FROM email_tokens WHERE token_hash=?""",
            (hash_token(token),),
        ).fetchone()
        if row is None or row["used_at"] or row["expires_at"] < now:
            raise CoordError("verification link is not valid", 400)
        conn.execute(
            "UPDATE accounts SET email_verified_at=? WHERE id=? AND email_verified_at IS NULL",
            (now, row["account_id"]),
        )
        conn.execute(
            "UPDATE email_tokens SET used_at=? WHERE account_id=? AND used_at IS NULL",
            (now, row["account_id"]),
        )
        account = public_account(
            conn.execute("SELECT * FROM accounts WHERE id=?", (row["account_id"],)).fetchone()
        )
        store._event(conn, "info", f"email verified for {account['display_name']}")
    return account


def my_stats(store, account_id: int) -> dict:
    account = store.conn.execute("SELECT * FROM accounts WHERE id=?", (int(account_id),)).fetchone()
    if account is None:
        raise CoordError("account not found", 404)
    board = _stats_rows(store, int(account_id))
    row = board[0] if board else None
    devices = devices_for(store, int(account_id))
    return {
        "display_name": account["display_name"],
        "email": account["email"],
        "email_verified": bool(account["email_verified_at"]),
        "stars_verified": _num(row, "stars_verified"),
        "stars_pending": _num(row, "stars_pending"),
        "batches_verified": _num(row, "batches_verified"),
        "batches_pending": _num(row, "batches_pending"),
        "devices": sum(1 for device in devices if not device["revoked_at"]),
    }


def public_stats(store) -> dict:
    """Sector progress and a leaderboard of display names. No emails."""
    snap = store.status()
    keys = (
        "units_total", "units_agreed", "units_submitted", "units_in_progress",
        "tics_total", "tics_agreed", "tics_submitted", "tics_in_progress",
        "replicas", "copies_required",
    )
    body = {key: snap[key] for key in keys}
    body["sectors"] = snap["sectors"]
    board = []
    for row in _stats_rows(store, None):
        verified = _num(row, "stars_verified")
        pending = _num(row, "stars_pending")
        if verified == 0 and pending == 0:
            continue
        board.append({
            "display_name": row["display_name"],
            "stars_verified": verified,
            "stars_pending": pending,
            "batches_verified": _num(row, "batches_verified"),
            "batches_pending": _num(row, "batches_pending"),
        })
    body["leaderboard"] = board
    return body


def note(store, level: str, message: str) -> None:
    with store._tx() as conn:
        store._event(conn, level, message)


def _stats_rows(store, account_id: int | None):
    where = ""
    args: list = []
    if account_id is not None:
        where = "WHERE a.id=?"
        args.append(int(account_id))
    return store.conn.execute(
        f"""SELECT a.id AS account_id, a.display_name AS display_name,
                   SUM(CASE WHEN c.outcome='agreed' AND c.verified=1 AND IFNULL(c.revoked, 0)=0
                            THEN u.n_tics ELSE 0 END) AS stars_verified,
                   SUM(CASE WHEN c.outcome='agreed' AND c.verified=1 AND IFNULL(c.revoked, 0)=0
                            THEN 1 ELSE 0 END) AS batches_verified,
                   SUM(CASE WHEN c.outcome='submitted' AND IFNULL(c.revoked, 0)=0 AND c.verified=0
                             AND NOT EXISTS (
                                 SELECT 1 FROM contributions done
                                 WHERE done.unit_id = c.unit_id
                                   AND done.generation = c.generation
                                   AND done.account_id = c.account_id
                                   AND done.outcome IN ('agreed', 'rejected')
                             )
                            THEN u.n_tics ELSE 0 END) AS stars_pending,
                   SUM(CASE WHEN c.outcome='submitted' AND IFNULL(c.revoked, 0)=0 AND c.verified=0
                             AND NOT EXISTS (
                                 SELECT 1 FROM contributions done
                                 WHERE done.unit_id = c.unit_id
                                   AND done.generation = c.generation
                                   AND done.account_id = c.account_id
                                   AND done.outcome IN ('agreed', 'rejected')
                             )
                            THEN 1 ELSE 0 END) AS batches_pending
            FROM accounts a
            LEFT JOIN contributions c ON c.account_id = a.id
            LEFT JOIN units u ON u.id = c.unit_id
            {where}
            GROUP BY a.id
            ORDER BY stars_verified DESC, stars_pending DESC, a.display_name COLLATE NOCASE""",
        args,
    ).fetchall()


def _num(row, key: str) -> int:
    if row is None:
        return 0
    return int(row[key] or 0)


def _login_checks(ip: str, email_key: str) -> list[tuple]:
    checks = [("login", f"ip:{ip}", auth.LOGIN_FAIL_PER_IP, auth.LOGIN_WINDOW_S)]
    if email_key:
        checks.append(("login", f"email:{email_key}", auth.LOGIN_FAIL_PER_EMAIL, auth.LOGIN_WINDOW_S))
    return checks


def _gate(store, checks: list[tuple], *, record: bool) -> None:
    now = store.clock()
    with store._tx() as conn:
        conn.execute(
            "DELETE FROM auth_attempts WHERE at < ?",
            (iso(now - timedelta(days=2)),),
        )
        for action, bucket, limit, window_s in checks:
            cutoff = iso(now - timedelta(seconds=window_s))
            count = conn.execute(
                """SELECT COUNT(*) AS n FROM auth_attempts
                   WHERE action=? AND bucket=? AND at>=?""",
                (action, bucket, cutoff),
            ).fetchone()["n"]
            if int(count) >= int(limit):
                raise CoordError("too many attempts, try again later", 429)
        if record:
            moment = iso(now)
            for action, bucket, _limit, _window in checks:
                conn.execute(
                    "INSERT INTO auth_attempts (action, bucket, at) VALUES (?, ?, ?)",
                    (action, bucket, moment),
                )


def _consume_invite(conn, code: str) -> None:
    text = (code or "").strip()
    if not text:
        raise CoordError("an invite code is required", 403)
    row = conn.execute(
        "SELECT id, uses_left, revoked_at FROM invite_codes WHERE code_hash=?",
        (hash_token(text),),
    ).fetchone()
    if row is None or row["revoked_at"] or int(row["uses_left"]) < 1:
        raise CoordError("invite code is not valid", 403)
    conn.execute(
        "UPDATE invite_codes SET uses_left = uses_left - 1 WHERE id=? AND uses_left > 0",
        (row["id"],),
    )


def _open_session(store, conn, account_id: int, user_agent: str, session_days: int) -> str:
    token = auth.SESSION_PREFIX + secrets.token_urlsafe(32)
    now = store.clock()
    conn.execute(
        """INSERT INTO sessions (account_id, token_hash, created_at, expires_at, user_agent)
           VALUES (?, ?, ?, ?, ?)""",
        (
            int(account_id),
            hash_token(token),
            iso(now),
            iso(now + timedelta(days=int(session_days))),
            (user_agent or "")[:200],
        ),
    )
    conn.execute(
        """DELETE FROM sessions WHERE account_id=? AND id NOT IN (
               SELECT id FROM sessions WHERE account_id=? ORDER BY id DESC LIMIT 20
           )""",
        (int(account_id), int(account_id)),
    )
    return token


def _pending_grant(conn, canonical: str):
    row = conn.execute(
        "SELECT * FROM device_grants WHERE user_code_hash=?",
        (hash_token(canonical),),
    ).fetchone()
    if row is None or row["status"] != "pending":
        raise CoordError("that code is expired or already used", 400)
    return row


def _expire_device_grants(store, conn, now) -> None:
    rows = conn.execute(
        """SELECT id, worker_id, status, delivery_token FROM device_grants
           WHERE status IN ('pending', 'approved') AND expires_at < ?""",
        (iso(now),),
    ).fetchall()
    for row in rows:
        if row["status"] == "approved" and row["delivery_token"] and row["worker_id"]:
            _revoke_worker_id(conn, store, int(row["worker_id"]), "link was not finished")
        conn.execute(
            "UPDATE device_grants SET status='expired', delivery_token=NULL WHERE id=?",
            (row["id"],),
        )


def _revoke_worker_id(conn, store, worker_id: int, reason: str) -> None:
    row = conn.execute(
        "SELECT id, name, revoked_at FROM workers WHERE id=?", (int(worker_id),),
    ).fetchone()
    if row is None or row["revoked_at"]:
        return
    now = iso(store.clock())
    conn.execute("UPDATE workers SET revoked_at=? WHERE id=?", (now, int(worker_id)))
    conn.execute(
        "UPDATE assignments SET status='expired' WHERE worker_id=? AND status='leased'",
        (int(worker_id),),
    )
    store._event(conn, "warn", f"revoked {row['name']} ({reason})")


def _device_worker_name(conn, hostname: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", hostname).strip("-._")
    if not cleaned or not cleaned[0].isalnum():
        cleaned = "device"
    cleaned = cleaned[:32]
    candidate = cleaned
    number = 2
    while conn.execute("SELECT 1 FROM workers WHERE name=?", (candidate,)).fetchone():
        suffix = f"-{number}"
        candidate = cleaned[:40 - len(suffix)] + suffix
        number += 1
        if number > 500:
            candidate = "device-" + secrets.token_hex(3)
            break
    return check_worker_name(candidate)
