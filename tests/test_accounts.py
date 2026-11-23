"""Accounts, device linking, and legacy invites. No MAST and no public port."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from rhexas.config import ConfigError, default_config
from rhexas.coord import auth
from rhexas.coord.cli import main as coord_main
from rhexas.coord.httpapi import App
from rhexas.coord.server import make_server
from rhexas.coord.settings import AccountsConfig, SmtpConfig, load_coord_file
from rhexas.coord.store import Store, hash_token
from rhexas.worker.login import link_this_computer, load_saved_token, token_path
from rhexas.worker.loop import load_token, resolve_worker


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class CapturedMail:
    def __init__(self):
        self.messages = []

    def enabled(self):
        return True

    def send(self, to, subject, body):
        self.messages.append({"to": to, "subject": subject, "body": body})


def _store(tmp_path):
    store = Store(tmp_path / "coord.sqlite", clock=Clock())
    store.init_schema()
    return store


def _open(store, **kwargs):
    accounts = AccountsConfig(signup="open", cookie_secure=True, **kwargs)
    return App(store, accounts=accounts)


def _json(response):
    return json.loads(response.body.decode())


def _cookie(response):
    for key, value in response.headers:
        if key.lower() == "set-cookie" and "rhexas_session=" in value:
            return value
    return ""


def _session(response):
    raw = _cookie(response)
    if not raw:
        return ""
    return raw.split(";", 1)[0].split("=", 1)[1]


def _post(app, path, payload, headers=None):
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    return app.handle("POST", path, "", hdrs, json.dumps(payload).encode())


def _signup(app, email="ada@example.com", name="Ada", password="correct horse", invite=""):
    return _post(app, "/api/v1/signup", {
        "email": email,
        "password": password,
        "display_name": name,
        "invite_code": invite,
    })


def _seed(store, email="ada@example.com", name="Ada", password="correct horse"):
    """Insert an account without the closed signup route."""
    from rhexas.coord.store import iso
    email_key = auth.normalize_email(email)
    display = auth.check_display_name(name)
    now = iso(store.clock())
    store.conn.execute(
        """INSERT INTO accounts (
               email, email_key, password_hash, display_name, display_key, provider,
               provider_sub, email_verified_at, created_at, legacy, trust_score, tier
           ) VALUES (?, ?, ?, ?, ?, 'password', NULL, NULL, ?, 0, NULL, '')""",
        (email_key, email_key, auth.hash_password(password), display, display.casefold(), now),
    )
    store.conn.commit()
    return int(store.conn.execute(
        "SELECT id FROM accounts WHERE email_key=?", (email_key,),
    ).fetchone()["id"])


def _login(app, email="ada@example.com", password="correct horse"):
    return _post(app, "/api/v1/login", {"email": email, "password": password})


def _own(store, account_id, name, hostname):
    token = store.create_worker(name)
    worker = store.authenticate(token)
    store.conn.execute(
        "UPDATE workers SET account_id=?, hostname=?, kind='device' WHERE id=?",
        (account_id, hostname, worker["id"]),
    )
    store.conn.commit()
    return token, int(worker["id"])


def _star(tic, period=1.5, sde=10.0, depth=0.01):
    return {"tic": tic, "period": period, "sde": sde, "depth": depth, "error": None, "snr": 9.0, "n_transits": 4}


def test_signup_login_logout_and_me(tmp_path):
    store = _store(tmp_path)
    try:
        app = _open(store)
        closed = _signup(app)
        assert closed.status == 403
        assert "Signup is closed" in closed.body.decode()
        _seed(store)
        created = _login(app)
        assert created.status == 200
        body = _json(created)
        assert body["display_name"] == "Ada"
        assert body["email"] == "ada@example.com"
        assert body["email_verified"] is False
        assert body["provider"] == "password"
        assert "scrypt" not in created.body.decode()
        assert "rhs_" not in created.body.decode()
        cookie = _cookie(created)
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=None" in cookie
        session = _session(created)
        me = app.handle("GET", "/api/v1/me", "", {"Cookie": f"rhexas_session={session}"}, b"")
        assert me.status == 200
        assert _json(me)["id"] == body["id"]
        assert "password" not in _json(me)

        bad = _post(app, "/api/v1/login", {"email": "ada@example.com", "password": "wrong-password"})
        assert bad.status == 401
        assert _json(bad)["error"] == "incorrect email or password"
        missing = _post(app, "/api/v1/login", {"email": "nope@example.com", "password": "wrong-password"})
        assert missing.status == 401
        assert _json(missing)["error"] == "incorrect email or password"

        logged = _post(app, "/api/v1/login", {"email": "ada@example.com", "password": "correct horse"})
        assert logged.status == 200
        assert _json(logged)["display_name"] == "Ada"
        session = _session(logged)
        out = _post(app, "/api/v1/logout", {}, {"Cookie": f"rhexas_session={session}"})
        assert out.status == 204
        assert "Max-Age=0" in _cookie(out)
        gone = app.handle("GET", "/api/v1/me", "", {"Cookie": f"rhexas_session={session}"}, b"")
        assert gone.status == 401

        short = _signup(app, email="bea@example.com", name="Bea", password="short")
        assert short.status == 403
        cols = {row["name"] for row in store.conn.execute("PRAGMA table_info(accounts)")}
        assert {"provider", "provider_sub", "trust_score", "tier", "password_hash"} <= cols
    finally:
        store.close()


def test_local_cookie_is_lax_without_secure(tmp_path):
    store = _store(tmp_path)
    try:
        app = App(store, accounts=AccountsConfig(signup="open", cookie_secure=False))
        _seed(store, email="cam@example.com", name="Cam")
        created = _login(app, email="cam@example.com")
        cookie = _cookie(created)
        assert "SameSite=Lax" in cookie
        assert "Secure" not in cookie
        assert "HttpOnly" in cookie
    finally:
        store.close()


def test_open_signup_signs_in_and_links_a_device(tmp_path):
    store = _store(tmp_path)
    try:
        app = _open(store)
        config = _json(app.handle("GET", "/api/v1/public/config", "", {}, b""))
        assert config == {"signup": "closed", "copies_required": 1, "device_linking": "closed"}
        created = _signup(app)
        assert created.status == 403
        assert store.conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"] == 0
        _seed(store)
        logged = _login(app)
        assert logged.status == 200
        cookie = {"Cookie": f"rhexas_session={_session(logged)}", "Content-Type": "application/json"}
        started = _post(app, "/api/v1/device/start", {"hostname": "laptop"})
        assert started.status == 403
        assert "Device linking is closed" in started.body.decode()
        approved = _post(app, "/api/v1/device/approve", {"code": "ABCD"}, cookie)
        assert approved.status == 403
        assert store.conn.execute("SELECT COUNT(*) AS n FROM device_grants").fetchone()["n"] == 0
        token = store.create_worker("ada-pc")
        assert store.authenticate(token)["name"] == "ada-pc"
    finally:
        store.close()


def test_honeypot_email_and_device_cap(tmp_path, monkeypatch):
    store = _store(tmp_path)
    try:
        app = _open(store)
        bad_email = _signup(app, email="not-an-email", name="Ada")
        assert bad_email.status == 403
        trapped = _post(app, "/api/v1/signup", {
            "email": "ada@example.com",
            "password": "correct horse",
            "display_name": "Ada",
            "company": "filled by a bot",
        })
        assert trapped.status == 403
        assert store.conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"] == 0
        account_id = _seed(store)
        cookie = {"Cookie": f"rhexas_session={_session(_login(app))}", "Content-Type": "application/json"}
        assert _post(app, "/api/v1/device/start", {"hostname": "laptop"}).status == 403
        token, device_id = _own(store, account_id, "laptop", "laptop")
        devices = _json(app.handle("GET", "/api/v1/me/devices", "", cookie, b""))["devices"]
        assert devices[0]["hostname"] == "laptop"
        assert app.handle(
            "POST", f"/api/v1/me/devices/{device_id}/revoke", "", cookie, b"{}",
        ).status == 200
        assert store.authenticate(token) is None
    finally:
        store.close()


def test_login_and_device_link_are_rate_limited_per_ip(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "LOGIN_FAIL_PER_IP", 2)
    monkeypatch.setattr(auth, "DEVICE_START_PER_IP", 1)
    store = _store(tmp_path)
    try:
        app = _open(store)
        _seed(store)
        assert _login(app).status == 200
        headers = {"X-Forwarded-For": "203.0.113.8"}
        assert _post(app, "/api/v1/login", {
            "email": "ada@example.com", "password": "wrong-password-1",
        }, headers).status == 401
        assert _post(app, "/api/v1/login", {
            "email": "ada@example.com", "password": "wrong-password-2",
        }, headers).status == 401
        limited = _post(app, "/api/v1/login", {
            "email": "ada@example.com", "password": "wrong-password-3",
        }, headers)
        assert limited.status == 429
        other = _post(app, "/api/v1/login", {
            "email": "ada@example.com", "password": "correct horse",
        }, {"X-Forwarded-For": "203.0.113.9"})
        assert other.status == 200
        assert _post(app, "/api/v1/device/start", {"hostname": "one"}, headers).status == 403
        started = _post(app, "/api/v1/device/start", {"hostname": "two"}, headers)
        assert started.status == 403
    finally:
        store.close()


def test_public_config_follows_invite_and_one_copy(tmp_path):
    store = Store(tmp_path / "coord.sqlite", clock=Clock(), copies_required=1)
    store.init_schema()
    try:
        invite = App(store)
        hidden = _json(invite.handle("GET", "/api/v1/public/config", "", {}, b""))
        assert hidden == {"signup": "closed", "copies_required": 1, "device_linking": "closed"}
        blocked = _signup(invite)
        assert blocked.status == 403
        page = invite.handle("GET", "/signup", "", {}, b"").body.decode()
        assert "Signup is closed" in page
        opened = App(store, accounts=AccountsConfig(signup="open", cookie_secure=False))
        shown = _json(opened.handle("GET", "/api/v1/public/config", "", {}, b""))
        assert shown["signup"] == "closed"
        assert shown["copies_required"] == 1
        stats = _json(opened.handle("GET", "/api/v1/public/stats", "", {}, b""))
        assert stats["copies_required"] == 1
        created = _signup(opened, invite="")
        assert created.status == 403
        open_page = opened.handle("GET", "/signup", "", {}, b"").body.decode()
        assert "Signup is closed" in open_page
    finally:
        store.close()


def test_invite_only_is_the_default_and_codes_are_single_use(tmp_path, capsys):
    store = _store(tmp_path)
    try:
        app = App(store)
        blocked = _signup(app)
        assert blocked.status == 403
        code = __import__("rhexas.coord.identity", fromlist=["create_invite"]).create_invite(
            store, label="friend",
        )
        assert code.startswith("inv_")
        ok = _signup(app, invite=code)
        assert ok.status == 403
        again = _signup(app, email="bea@example.com", name="Bea", invite=code)
        assert again.status == 403
        assert store.conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"] == 0
    finally:
        store.close()

    path = tmp_path / "coord.toml"
    path.write_text("[coord]\ndb = \"coord.sqlite\"\n", encoding="utf-8")
    assert coord_main(["--config", str(path), "init"]) == 0
    capsys.readouterr()
    assert coord_main(["--config", str(path), "invite", "create", "--label", "ada"]) == 0
    printed = capsys.readouterr().out
    code = next(line.strip() for line in printed.splitlines() if line.strip().startswith("inv_"))
    assert coord_main(["--config", str(path), "invite", "list"]) == 0
    listed = capsys.readouterr().out
    assert "ada" in listed
    assert code not in listed
    assert coord_main(["--config", str(path), "invite", "revoke", "1"]) == 0


def test_rate_limit_trusts_the_proxy_hop(tmp_path, monkeypatch):
    assert auth.client_ip({"X-Forwarded-For": "1.1.1.1, 203.0.113.8"}) == "203.0.113.8"
    assert auth.client_ip({}) == "local"
    monkeypatch.setattr(auth, "SIGNUP_PER_IP", 1)
    store = _store(tmp_path)
    try:
        app = _open(store)
        headers = {"X-Forwarded-For": "198.51.100.9, 203.0.113.8"}
        first = _post(app, "/api/v1/signup", {
            "email": "ada@example.com", "password": "correct horse", "display_name": "Ada",
        }, headers)
        assert first.status == 403
        second = _post(app, "/api/v1/signup", {
            "email": "bea@example.com", "password": "correct horse", "display_name": "Bea",
        }, headers)
        assert second.status == 403
        other = _post(app, "/api/v1/signup", {
            "email": "cam@example.com", "password": "correct horse", "display_name": "Cam",
        }, {"X-Forwarded-For": "203.0.113.9"})
        assert other.status == 403
    finally:
        store.close()


def test_signup_is_rate_limited(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "SIGNUP_PER_IP", 2)
    store = _store(tmp_path)
    try:
        app = _open(store)
        assert _signup(app, email="a@example.com", name="Aaa").status == 403
        assert _signup(app, email="b@example.com", name="Bbb").status == 403
        limited = _signup(app, email="c@example.com", name="Ccc")
        assert limited.status == 403
        assert "Signup is closed" in limited.body.decode()
    finally:
        store.close()


def test_public_site_cors_and_public_stats_hide_emails(tmp_path):
    store = _store(tmp_path)
    try:
        app = _open(store)
        assert _signup(app).status == 403
        origin = {"Origin": "https://megarhexas.com"}
        stats = app.handle("GET", "/api/v1/public/stats", "", origin, b"")
        assert stats.status == 200
        raw = stats.body.decode()
        assert "ada@example.com" not in raw
        assert "scrypt" not in raw
        cors = dict(stats.headers)
        assert cors["Access-Control-Allow-Origin"] == "https://megarhexas.com"
        assert cors["Access-Control-Allow-Credentials"] == "true"
        evil = app.handle("GET", "/api/v1/public/stats", "", {"Origin": "https://evil.test"}, b"")
        assert "Access-Control-Allow-Origin" not in dict(evil.headers)
        preflight = app.handle("OPTIONS", "/api/v1/signup", "", origin, b"")
        assert preflight.status == 204
        assert dict(preflight.headers)["Access-Control-Allow-Origin"] == "https://megarhexas.com"
        body = _json(stats)
        assert "sectors" in body and "leaderboard" in body
        assert "email" not in raw
    finally:
        store.close()


def test_html_pages_and_pasted_token(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    store = _store(tmp_path)
    try:
        app = _open(store)
        page = app.handle("GET", "/signup", "", {}, b"").body.decode()
        assert app.handle("GET", "/signup", "", {}, b"").status == 403
        assert "Signup is closed" in page
        assert "animation:trspin 12s linear infinite" in page
        form = "email=ada%40example.com&password=correct+horse&display_name=Ada".encode()
        signed = app.handle("POST", "/signup", "", {"Content-Type": "application/x-www-form-urlencoded"}, form)
        assert signed.status == 403
        account_id = _seed(store)
        session = _session(_login(app))
        account = app.handle("GET", "/account", "", {"Cookie": f"rhexas_session={session}"}, b"")
        assert account.status == 200
        assert b"Ada" in account.body

        started = _post(app, "/api/v1/device/start", {"hostname": "laptop"})
        assert started.status == 403
        link = app.handle("GET", "/link", "", {}, b"")
        assert link.status == 403
        assert b"Device linking is closed" in link.body
        token, _device = _own(store, account_id, "laptop", "laptop")
        assert store.authenticate(token)["name"] == "laptop"
        from rhexas.worker.login import save_worker_token
        saved = save_worker_token(token)
        assert load_saved_token() == token
        assert saved == token_path()
    finally:
        store.close()


def test_device_code_expires_and_cannot_be_reused(tmp_path):
    store = _store(tmp_path)
    try:
        app = _open(store)
        _seed(store)
        session = _session(_login(app))
        cookie = {"Cookie": f"rhexas_session={session}", "Content-Type": "application/json"}
        started = _post(app, "/api/v1/device/start", {"hostname": "laptop"})
        assert started.status == 403
        expired = _post(app, "/api/v1/device/poll", {"device_code": "gone"})
        assert expired.status == 403
        denied = app.handle(
            "POST", "/api/v1/device/approve", "", cookie,
            json.dumps({"code": "ABCD"}).encode(),
        )
        assert denied.status == 403
        assert "rhx_" not in started.body.decode()
        assert store.conn.execute("SELECT COUNT(*) AS n FROM device_grants").fetchone()["n"] == 0
    finally:
        store.close()


def test_revoke_device_and_same_account_cannot_replicate(tmp_path):
    store = _store(tmp_path)
    try:
        app = _open(store)
        account_id = _seed(store)
        session = _session(_login(app))
        cookie = {"Cookie": f"rhexas_session={session}"}
        laptop, laptop_id = _own(store, account_id, "laptop", "laptop")
        desktop, desktop_id = _own(store, account_id, "desktop", "desktop")
        assert laptop_id != desktop_id
        store.enroll_tics(107, [1, 2, 3, 4], unit_size=2)
        first = store.claim(laptop_id)
        assert first["tics"] == [1, 2]
        done = store.submit(laptop_id, first["assignment_id"], [_star(1), _star(2)], [])
        assert done["verdict"] == "agreed"
        second = store.claim(desktop_id)
        assert second is not None
        assert second["unit_id"] != first["unit_id"]
        other = store.create_worker("bea")
        bea = store.authenticate(other)["id"]
        assert store.claim(bea) is None
        credited = [row for row in store.contributions() if row["outcome"] == "agreed"]
        accounts = {row["account_id"] for row in credited}
        assert accounts == {account_id}

        stats = app.handle("GET", "/api/v1/me/stats", "", cookie, b"")
        mine = _json(stats)
        assert mine["stars_verified"] == 2
        assert mine["stars_pending"] == 0
        assert mine["email"] == "ada@example.com"
        public = _json(app.handle("GET", "/api/v1/public/stats", "", {}, b""))
        names = {row["display_name"] for row in public["leaderboard"]}
        assert "Ada" in names
        assert public["sectors"][0]["sector"] == 107
        assert public["tics_agreed"] == 2
        dumped = json.dumps(public)
        assert "ada@example.com" not in dumped
        assert "email" not in dumped

        ada_account = next(row["account_id"] for row in credited if row["user_id"] == laptop_id)
        store.conn.execute(
            "UPDATE contributions SET revoked=1 WHERE account_id=? AND outcome='agreed'",
            (ada_account,),
        )
        after = _json(app.handle("GET", "/api/v1/public/stats", "", {}, b""))
        by_name = {row["display_name"]: row for row in after["leaderboard"]}
        assert "Ada" not in by_name or by_name["Ada"]["stars_verified"] == 0

        devices = _json(app.handle("GET", "/api/v1/me/devices", "", cookie, b""))["devices"]
        desktop_row = next(row for row in devices if row["hostname"] == "desktop")
        revoked = app.handle(
            "POST", f"/api/v1/me/devices/{desktop_row['id']}/revoke", "",
            {**cookie, "Content-Type": "application/json"},
            b"{}",
        )
        assert revoked.status == 200
        assert store.authenticate(desktop) is None
        held = store.conn.execute(
            "SELECT status FROM assignments WHERE worker_id=?", (desktop_id,),
        ).fetchone()
        assert held["status"] == "expired"
        claim = app.handle(
            "POST", "/api/v1/work/claim", "",
            {"Authorization": f"Bearer {desktop}", "Content-Type": "application/json"},
            b"{}",
        )
        assert claim.status == 401
    finally:
        store.close()


def test_pending_credit_until_another_account_agrees(tmp_path):
    store = _store(tmp_path)
    try:
        app = _open(store)
        account_id = _seed(store)
        session = _session(_login(app))
        cookie = {"Cookie": f"rhexas_session={session}", "Content-Type": "application/json"}
        token, worker = _own(store, account_id, "laptop", "laptop")
        assert token.startswith("rhx_")
        store.enroll_tics(96, [7], unit_size=500)
        assignment = store.claim(worker)
        done = store.submit(worker, assignment["assignment_id"], [_star(7)], [])
        assert done["verdict"] == "agreed"
        pending = _json(app.handle("GET", "/api/v1/me/stats", "", cookie, b""))
        assert pending["stars_pending"] == 0
        assert pending["stars_verified"] == 1
        assert pending["email_verified"] is False
    finally:
        store.close()


def test_legacy_invites_migrate_and_still_count_as_different_accounts(tmp_path):
    path = tmp_path / "coord.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE workers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            revoked_at TEXT,
            last_seen_at TEXT,
            stars_searched INTEGER NOT NULL DEFAULT 0,
            candidates_found INTEGER NOT NULL DEFAULT 0,
            units_agreed INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE units (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL DEFAULT 'exoplanets',
            sector INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            n_tics INTEGER NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            agreed_at TEXT,
            UNIQUE (project, sector, ordinal)
        );
        CREATE TABLE assignments (
            id INTEGER PRIMARY KEY,
            unit_id INTEGER NOT NULL,
            project TEXT NOT NULL DEFAULT 'exoplanets',
            generation INTEGER NOT NULL,
            worker_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            leased_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            submitted_at TEXT,
            result_id INTEGER
        );
        CREATE TABLE contributions (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            project TEXT NOT NULL,
            unit_id INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            outcome TEXT NOT NULL,
            verified INTEGER NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        """
    )
    one = "rhx_legacytokenone"
    two = "rhx_legacytokentwo"
    conn.execute(
        "INSERT INTO workers (name, token_hash, created_at) VALUES ('ada', ?, '2026-01-01T00:00:00+00:00')",
        (hash_token(one),),
    )
    conn.execute(
        "INSERT INTO workers (name, token_hash, created_at) VALUES ('bea', ?, '2026-01-01T00:00:00+00:00')",
        (hash_token(two),),
    )
    conn.execute(
        """INSERT INTO units (project, sector, ordinal, generation, payload_json, n_tics, state, created_at)
           VALUES ('exoplanets', 107, 0, 1, '{"sector":107,"tics":[1,2]}', 2, 'open', '2026-01-01T00:00:00+00:00')"""
    )
    conn.execute(
        """INSERT INTO contributions (user_id, project, unit_id, generation, outcome, verified, created_at)
           VALUES (1, 'exoplanets', 1, 1, 'submitted', 0, '2026-01-01T00:00:00+00:00')"""
    )
    conn.commit()
    conn.close()

    store = Store(path, clock=Clock())
    try:
        store.init_schema()
        store.init_schema()
        ada = store.authenticate(one)
        bea = store.authenticate(two)
        assert ada["name"] == "ada"
        assert bea["name"] == "bea"
        accounts = {
            row["name"]: row["display_name"]
            for row in store.conn.execute(
                """SELECT w.name AS name, a.display_name AS display_name, a.legacy AS legacy, a.provider AS provider
                   FROM workers w JOIN accounts a ON a.id = w.account_id"""
            )
        }
        assert accounts == {"ada": "ada", "bea": "bea"}
        first = store.claim(ada["id"])
        second = store.claim(bea["id"])
        assert first["tics"] == [1, 2]
        assert second is None
        backfilled = store.contributions()[0]
        assert backfilled["user_id"] == ada["id"]
        assert backfilled["account_id"] is not None
        assert backfilled["verified"] == 0
    finally:
        store.close()


def test_verification_email_is_optional(tmp_path):
    store = _store(tmp_path)
    try:
        mail = CapturedMail()
        app = App(store, accounts=AccountsConfig(signup="open", cookie_secure=True), mailer=mail)
        created = _signup(app)
        assert created.status == 403
        assert mail.messages == []
        assert SmtpConfig().enabled() is False
    finally:
        store.close()


def test_accounts_config_and_saved_worker_token(tmp_path, monkeypatch):
    path = tmp_path / "coord.toml"
    path.write_text(
        """
[coord]
port = 8791
[accounts]
signup = "open"
cookie_secure = false
public_origin = "https://megarhexas.com"
api_public_url = "https://api.megarhexas.com"
[smtp]
host = "smtp.example.com"
port = 2525
username = "mailer"
password = "secret"
from = "Mega Rhexas <noreply@megarhexas.com>"
starttls = false
""",
        encoding="utf-8",
    )
    loaded = load_coord_file(path)
    assert loaded.accounts.signup == "open"
    assert loaded.accounts.secure_cookies is False
    assert loaded.smtp.enabled() is True
    assert loaded.smtp.from_addr.startswith("Mega Rhexas")
    assert loaded.smtp.port == 2525
    path.write_text("[coord]\nport = 8787\n[accounts]\nsignup = \"nope\"\n", encoding="utf-8")
    try:
        load_coord_file(path)
        raised = False
    except ConfigError:
        raised = True
    assert raised
    path.write_text("[nope]\nx = 1\n", encoding="utf-8")
    try:
        load_coord_file(path)
        raised = False
    except ConfigError:
        raised = True
    assert raised

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    from rhexas.worker.login import save_worker_token
    dest = save_worker_token("rhx_saved")
    assert dest == token_path()
    assert (dest.stat().st_mode & 0o777) == 0o600
    cfg = resolve_worker(default_config(tmp_path), environ={})
    assert load_token(cfg) == "rhx_saved"
    token_file = tmp_path / "token.txt"
    token_file.write_text("rhx_from_file\n", encoding="utf-8")
    cfg = resolve_worker(default_config(tmp_path), token_file=str(token_file), environ={})
    assert load_token(cfg) == "rhx_from_file"


def test_origin_is_rejected_for_cookie_mutations(tmp_path):
    store = _store(tmp_path)
    try:
        app = _open(store)
        blocked = app.handle(
            "POST", "/api/v1/signup", "",
            {"Content-Type": "application/json", "Origin": "https://evil.test"},
            json.dumps({
                "email": "ada@example.com", "password": "correct horse", "display_name": "Ada",
            }).encode(),
        )
        assert blocked.status == 403
    finally:
        store.close()
