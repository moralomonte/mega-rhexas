"""SQLite state for the friends-beta coordinator.

One connection, explicit transactions. Callers that share the store across
threads (the HTTP server) must hold their own lock around each public method.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .logic import (
    chunk_tics,
    measured_count,
    new_tic_ids,
    normalize_candidates,
    normalize_stars,
)
from .projects import DEFAULT_PROJECT, get_handler, parse_project
from .settings import Settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workers (
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
CREATE TABLE IF NOT EXISTS units (
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
CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY,
    unit_id INTEGER NOT NULL REFERENCES units(id),
    project TEXT NOT NULL DEFAULT 'exoplanets',
    generation INTEGER NOT NULL,
    worker_id INTEGER NOT NULL REFERENCES workers(id),
    status TEXT NOT NULL,
    leased_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    submitted_at TEXT,
    result_id INTEGER,
    progress_phase TEXT,
    progress_done INTEGER,
    progress_total INTEGER
);
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY,
    assignment_id INTEGER NOT NULL UNIQUE REFERENCES assignments(id),
    unit_id INTEGER NOT NULL REFERENCES units(id),
    project TEXT NOT NULL DEFAULT 'exoplanets',
    worker_id INTEGER NOT NULL REFERENCES workers(id),
    generation INTEGER NOT NULL,
    stars_json TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    n_stars INTEGER NOT NULL,
    n_candidates INTEGER NOT NULL,
    n_errors INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    verdict TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contributions (
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
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    unit_id INTEGER NOT NULL REFERENCES units(id),
    generation INTEGER NOT NULL,
    tic INTEGER NOT NULL,
    sector INTEGER NOT NULL,
    period REAL,
    depth REAL,
    sde REAL,
    snr REAL,
    status TEXT,
    score REAL,
    flags TEXT,
    UNIQUE (unit_id, tic)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assign_unit ON assignments(unit_id, generation, status);
CREATE INDEX IF NOT EXISTS idx_assign_worker ON assignments(worker_id, status);
CREATE INDEX IF NOT EXISTS idx_units_project ON units(project, state);
CREATE INDEX IF NOT EXISTS idx_contrib ON contributions(project, user_id, verified);

-- Accounts. ``provider`` is password, legacy, or (later) google.
-- trust_score and tier stay empty until Phase 4 scoring.
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    email TEXT,
    email_key TEXT,
    password_hash TEXT,
    display_name TEXT NOT NULL,
    display_key TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL DEFAULT 'password',
    provider_sub TEXT,
    email_verified_at TEXT,
    created_at TEXT NOT NULL,
    legacy INTEGER NOT NULL DEFAULT 0,
    trust_score REAL,
    tier TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_email
    ON accounts(email_key) WHERE email_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_provider_sub
    ON accounts(provider, provider_sub) WHERE provider_sub IS NOT NULL;
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    user_agent TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS invite_codes (
    id INTEGER PRIMARY KEY,
    code_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    uses_left INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS device_grants (
    id INTEGER PRIMARY KEY,
    device_code_hash TEXT NOT NULL UNIQUE,
    user_code_hash TEXT NOT NULL UNIQUE,
    hostname TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    account_id INTEGER,
    worker_id INTEGER,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    interval_s INTEGER NOT NULL,
    last_poll_at TEXT,
    delivery_token TEXT
);
CREATE INDEX IF NOT EXISTS idx_device_status ON device_grants(status, expires_at);
CREATE TABLE IF NOT EXISTS auth_attempts (
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    bucket TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_attempts ON auth_attempts(action, bucket, at);
CREATE TABLE IF NOT EXISTS email_tokens (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
"""


class CoordError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


# Heartbeat ``phase`` values the worker sends, and the word shown on the status page.
_PHASE_WORD = {
    "download": "downloaded",
    "downloading": "downloaded",
    "downloaded": "downloaded",
    "files": "downloaded",
    "search": "searched",
    "searching": "searched",
    "searched": "searched",
    "stars": "searched",
    "filter": "filtering",
    "filtering": "filtering",
    "filtered": "filtered",
    "upload": "uploading",
    "uploading": "uploading",
    "uploaded": "uploaded",
}


def format_ago(when: str | None, now: datetime) -> str:
    """Relative time for the status page and CLI. ``when`` is a coordinator timestamp."""
    if not when:
        return "never"
    try:
        seen = parse_iso(when)
    except ValueError:
        return "never"
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    seconds = int((now - seen).total_seconds())
    if seconds < 45:
        return "just now"
    if seconds < 3600:
        return f"{max(1, seconds // 60)} min ago"
    if seconds < 86400:
        hours = seconds // 3600
        unit = "hour" if hours == 1 else "hours"
        return f"{hours} {unit} ago"
    days = seconds // 86400
    unit = "day" if days == 1 else "days"
    return f"{days} {unit} ago"


# The batch being searched, plus one prefetch download. A plain claim stays
# idempotent; the worker asks for the extra slot explicitly.
MAX_WORKER_LEASES = 2


def activity_line(sector: int, unit_id: int, phase: str | None, done, total) -> str:
    """What a leased worker is doing. Progress is optional; old heartbeats omit it."""
    base = f"working on sector {int(sector)} unit {int(unit_id)}"
    word = _PHASE_WORD.get((phase or "").strip().lower(), "")
    if done is None or total is None:
        return base
    try:
        done_i = int(done)
        total_i = int(total)
    except (TypeError, ValueError):
        return base
    if word:
        return f"{base}, {done_i}/{total_i} {word}"
    return f"{base}, {done_i}/{total_i}"


def _presence(last_seen: str | None, busy: bool, now: datetime, window_s: int) -> str:
    """online: heartbeat is fresh and a batch is leased. idle: fresh, waiting. offline: quiet."""
    if not last_seen:
        return "offline"
    try:
        seen = parse_iso(last_seen)
    except ValueError:
        return "offline"
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    if (now - seen).total_seconds() > window_s:
        return "offline"
    return "online" if busy else "idle"


def _count_field(value) -> int:
    """Non-negative integer from a heartbeat progress field. Rejects bools and fractions."""
    if isinstance(value, bool) or value is None:
        raise ValueError
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError
        value = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            raise ValueError
        value = int(text)
    elif not isinstance(value, int):
        raise ValueError
    if value < 0 or value > 50_000_000:
        raise ValueError
    return value


def _clean_progress(progress) -> tuple[str, int, int] | None:
    """Normalize an optional heartbeat progress object. Junk is ignored, not an error."""
    if not isinstance(progress, dict):
        return None
    phase = str(progress.get("phase") or progress.get("stage") or "").strip().lower()
    if len(phase) > 32 or any(not (ch.isalnum() or ch in "-_") for ch in phase):
        phase = ""
    try:
        done = _count_field(progress.get("done", progress.get("completed")))
        total = _count_field(progress.get("total"))
    except ValueError:
        return None
    if total and done > total:
        done = total
    if not phase and done == 0 and total == 0:
        return None
    return phase, done, total


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return "rhx_" + secrets.token_urlsafe(32)


class Store:
    def __init__(self, path: Path | str, settings: Settings | None = None, clock=None, copies_required: int = 2):
        self.path = Path(path)
        self.settings = settings or Settings()
        # 1 pauses the second copy. Anything else keeps the two-copy path.
        self.copies_required = 1 if int(copies_required) == 1 else 2
        self.clock = clock or utcnow
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(_SCHEMA)
        self._migrate()

    def replica_count(self, project: str) -> int:
        """How many distinct workers must finish a unit.

        ``copies_required = 1`` pauses the second copy. The two-account rule,
        the agreement check, and pending credit stay in this file and run
        again when ``copies_required`` is 2. A toml ``replicas`` override
        still applies in that two-copy mode.
        """
        project = self._require_project(project)
        if self.copies_required == 1:
            return 1
        if self.settings.replicas is not None:
            return int(self.settings.replicas)
        return get_handler(project).replicas

    def public_copies(self) -> int:
        """1 when a batch is kept after one upload, otherwise 2.

        This is what public pages should say. It follows the verification
        switch, and also the older ``replicas = 1`` override.
        """
        if self.copies_required == 1:
            return 1
        try:
            if self.replica_count(DEFAULT_PROJECT) <= 1:
                return 1
        except CoordError:
            return 2
        return 2

    def projects_public(self) -> list[dict]:
        from .projects import HANDLERS
        return [
            {"name": name, "replicas": self.replica_count(name)}
            for name in sorted(HANDLERS)
        ]

    @contextmanager
    def _tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def ensure_admin(self) -> str | None:
        """Create the status-page token if it is missing. The token is returned only then."""
        with self._tx() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='admin_token_hash'").fetchone()
            if row:
                return None
            token = new_token()
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('admin_token_hash', ?)",
                (hash_token(token),),
            )
            self._event(conn, "info", "admin token created")
            return token

    def admin_configured(self) -> bool:
        row = self.conn.execute("SELECT 1 FROM meta WHERE key='admin_token_hash'").fetchone()
        return row is not None

    def admin_ok(self, token: str) -> bool:
        if not token:
            return False
        row = self.conn.execute("SELECT value FROM meta WHERE key='admin_token_hash'").fetchone()
        if row is None:
            return False
        return hmac.compare_digest(hash_token(token), row["value"])

    def reset_admin(self) -> str:
        token = new_token()
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO meta (key, value) VALUES ('admin_token_hash', ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (hash_token(token),),
            )
            self._event(conn, "warn", "admin token reset")
        return token

    def create_worker(self, name: str) -> str:
        from .logic import check_worker_name
        name = check_worker_name(name)
        token = new_token()
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM workers WHERE name=?", (name,)).fetchone():
                raise ValueError(f"worker {name} already exists")
            now = iso(self.clock())
            account_id = self._insert_legacy_account(conn, name, now, suffix_id=None)
            conn.execute(
                """INSERT INTO workers (name, token_hash, created_at, account_id, hostname, kind)
                   VALUES (?, ?, ?, ?, NULL, 'legacy')""",
                (name, hash_token(token), now, account_id),
            )
            self._event(conn, "info", f"invite created for {name}")
        return token

    def revoke_worker(self, name: str) -> None:
        with self._tx() as conn:
            row = conn.execute("SELECT id, revoked_at FROM workers WHERE name=?", (name,)).fetchone()
            if row is None:
                raise ValueError(f"no worker named {name}")
            if row["revoked_at"]:
                return
            now = iso(self.clock())
            conn.execute("UPDATE workers SET revoked_at=? WHERE id=?", (now, row["id"]))
            conn.execute(
                "UPDATE assignments SET status='expired' WHERE worker_id=? AND status='leased'",
                (row["id"],),
            )
            self._event(conn, "warn", f"revoked {name}")

    def list_workers(self) -> list[dict]:
        """Invite list for ``token list``. Includes revoked rows and the same display split as the page."""
        return self._worker_rows(self.clock(), include_revoked=True)

    def authenticate(self, token: str) -> dict | None:
        if not token or not token.startswith("rhx_"):
            return None
        row = self.conn.execute(
            "SELECT id, name, revoked_at FROM workers WHERE token_hash=?",
            (hash_token(token),),
        ).fetchone()
        if row is None or row["revoked_at"]:
            return None
        return {"id": int(row["id"]), "name": row["name"]}

    def enroll_tics(
        self,
        sector: int,
        tics: list[int],
        unit_size: int | None = None,
        project: str = DEFAULT_PROJECT,
    ) -> dict:
        project = self._require_project(project)
        sector = int(sector)
        if sector < 1:
            raise ValueError("sector must be >= 1")
        size = self.settings.unit_size if unit_size is None else int(unit_size)
        chunks = chunk_tics(list(tics), size)
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT COUNT(*) AS n FROM units WHERE project=? AND sector=?",
                (project, sector),
            ).fetchone()
            if existing["n"]:
                raise ValueError(f"{project} sector {sector} is already enrolled ({existing['n']} units)")
            now = iso(self.clock())
            for ordinal, chunk in enumerate(chunks):
                payload = json.dumps({"sector": sector, "tics": chunk})
                conn.execute(
                    """INSERT INTO units
                           (project, sector, ordinal, generation, payload_json, n_tics, state, created_at)
                       VALUES (?, ?, ?, 1, ?, ?, 'open', ?)""",
                    (project, sector, ordinal, payload, len(chunk), now),
                )
            stars = sum(len(chunk) for chunk in chunks)
            self._event(conn, "info", f"enrolled {project} sector {sector}: {stars} stars in {len(chunks)} units")
        return {"project": project, "sector": sector, "stars": stars, "units": len(chunks), "unit_size": size}

    def claim(self, worker_id: int, project: str = DEFAULT_PROJECT, *, prefetch: bool = False) -> dict | None:
        """Lease one batch. With ``prefetch``, a worker may hold a second lease.

        A call without ``prefetch`` still returns the batch already leased, so a
        worker from before download-ahead does not pick up an extra unit. The
        second lease follows the same unit rules: two copies come from two
        accounts, and a unit that already has a copy is chosen first.
        """
        project = self._require_project(project)
        with self._tx() as conn:
            now = self.clock()
            self._expire(conn, now)
            self._touch(conn, worker_id, now)
            live = self._live_leases(conn, worker_id)
            limit = MAX_WORKER_LEASES if prefetch else 1
            if len(live) >= limit:
                if prefetch:
                    return None
                return live[0] if live else None
            chosen = self._pick_unit(conn, worker_id, project)
            if chosen is None:
                return None
            unit, filled = chosen
            expires = now + timedelta(seconds=self.settings.lease_s)
            cur = conn.execute(
                """INSERT INTO assignments
                       (unit_id, project, generation, worker_id, status, leased_at, expires_at)
                   VALUES (?, ?, ?, ?, 'leased', ?, ?)""",
                (unit["id"], project, unit["generation"], worker_id, iso(now), iso(expires)),
            )
            copies = self.replica_count(project)
            name = self._name(conn, worker_id)
            self._event(
                conn, "info",
                f"{name} claimed unit {unit['id']} sector {unit['sector']} "
                f"({unit['n_tics']} stars, copy {filled + 1}/{copies})",
            )
            return self._assignment_body(
                assignment_id=int(cur.lastrowid),
                unit_id=int(unit["id"]),
                project=project,
                generation=int(unit["generation"]),
                sector=int(unit["sector"]),
                payload_json=unit["payload_json"],
                n_tics=int(unit["n_tics"]),
                worker_id=worker_id,
                leased_at=iso(now),
                expires_at=iso(expires),
            )

    def current(self, worker_id: int) -> dict | None:
        rows = self.leases(worker_id)
        return rows[0] if rows else None

    def leases(self, worker_id: int) -> list[dict]:
        """Live leases, oldest first. The oldest is the batch being searched."""
        with self._tx() as conn:
            now = self.clock()
            self._expire(conn, now)
            self._touch(conn, worker_id, now)
            return self._live_leases(conn, worker_id)

    def heartbeat(self, worker_id: int, assignment_id: int, project: str | None = None, progress=None) -> dict:
        """Extend a lease. ``progress`` is optional, so workers from before that field still check in."""
        with self._tx() as conn:
            now = self.clock()
            self._expire(conn, now)
            assignment = self._owned_lease(conn, worker_id, assignment_id)
            self._same_project(conn, assignment, project)
            leased = parse_iso(assignment["leased_at"])
            cap = leased + timedelta(seconds=self.settings.max_lease_s)
            if now >= cap:
                conn.execute("UPDATE assignments SET status='expired' WHERE id=?", (assignment_id,))
                self._event(conn, "warn", f"lease hit its maximum and was reissued (unit {assignment['unit_id']})")
                raise CoordError("lease reached its maximum and was reissued", 409)
            expires = min(now + timedelta(seconds=self.settings.lease_s), cap)
            cleaned = _clean_progress(progress)
            if cleaned is None:
                conn.execute("UPDATE assignments SET expires_at=? WHERE id=?", (iso(expires), assignment_id))
            else:
                phase, done, total = cleaned
                conn.execute(
                    """UPDATE assignments
                       SET expires_at=?, progress_phase=?, progress_done=?, progress_total=?
                       WHERE id=?""",
                    (iso(expires), phase, done, total, assignment_id),
                )
            self._touch(conn, worker_id, now)
            return {"expires_at": iso(expires)}

    def release(self, worker_id: int, assignment_id: int, project: str | None = None) -> None:
        with self._tx() as conn:
            now = self.clock()
            self._expire(conn, now)
            assignment = self._owned_lease(conn, worker_id, assignment_id)
            self._same_project(conn, assignment, project)
            conn.execute("UPDATE assignments SET status='released' WHERE id=?", (assignment_id,))
            self._event(
                conn, "info",
                f"{self._name(conn, worker_id)} released unit {assignment['unit_id']}",
            )

    def submit(self, worker_id: int, assignment_id: int, stars, candidates, project: str | None = None) -> dict:
        try:
            stars = normalize_stars(stars)
            candidates = normalize_candidates(candidates)
        except ValueError as exc:
            raise CoordError(str(exc), 400) from exc
        with self._tx() as conn:
            now = self.clock()
            self._expire(conn, now)
            assignment = self._owned_lease(conn, worker_id, assignment_id)
            self._same_project(conn, assignment, project)
            unit = conn.execute("SELECT * FROM units WHERE id=?", (assignment["unit_id"],)).fetchone()
            if unit["state"] == "agreed" or int(unit["generation"]) != int(assignment["generation"]):
                raise CoordError("assignment is out of date", 409)
            limit = max(int(unit["n_tics"]) * 3, 1)
            if len(stars) > limit:
                raise CoordError("too many star rows", 400)
            if len(candidates) > limit:
                raise CoordError("too many candidate rows", 400)
            cur = conn.execute(
                """INSERT INTO results (
                       assignment_id, unit_id, project, worker_id, generation, stars_json, candidates_json,
                       n_stars, n_candidates, n_errors, created_at, verdict
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    assignment_id, unit["id"], unit["project"], worker_id, assignment["generation"],
                    json.dumps(stars), json.dumps(candidates),
                    measured_count(stars), len(new_tic_ids(candidates)),
                    sum(1 for star in stars if star.get("error")),
                    iso(now),
                ),
            )
            result_id = int(cur.lastrowid)
            conn.execute(
                "UPDATE assignments SET status='submitted', submitted_at=?, result_id=? WHERE id=?",
                (iso(now), result_id, assignment_id),
            )
            self._touch(conn, worker_id, now)
            self._contribute(conn, worker_id, unit, "submitted", 0, "", now)
            peers = self._pending_copies(conn, unit)
            return self._settle(conn, unit, peers, now, announce_wait=True, worker_id=worker_id)

    def reverify(self) -> dict:
        """Apply the current copy count to batches that already have uploads.

        Nothing is deleted. With one copy required, a batch that already has
        an accepted upload is marked verified. With two, a batch that already
        has two uploads is compared; a batch with one upload stays pending.
        Safe to run twice: an agreed batch is left as it is.
        """
        promoted = 0
        reissued = 0
        left = 0
        with self._tx() as conn:
            units = conn.execute(
                "SELECT * FROM units WHERE state='open' ORDER BY id"
            ).fetchall()
            now = self.clock()
            for unit in units:
                peers = self._pending_copies(conn, unit)
                need = self.replica_count(unit["project"])
                if len(peers) < need:
                    left += 1
                    continue
                outcome = self._settle(
                    conn, unit, peers, now, announce_wait=False, worker_id=None,
                )
                if outcome["verdict"] == "agreed":
                    promoted += 1
                elif outcome["verdict"] == "disagreed":
                    reissued += 1
                else:
                    left += 1
        return {
            "copies_required": self.public_copies(),
            "promoted": promoted,
            "reissued": reissued,
            "left_pending": left,
        }

    def _pending_copies(self, conn, unit):
        return conn.execute(
            """SELECT r.* FROM results r
               JOIN assignments a ON a.id = r.assignment_id
               WHERE r.unit_id=? AND r.generation=? AND r.verdict='pending'
                 AND a.status='submitted'
               ORDER BY r.id""",
            (unit["id"], unit["generation"]),
        ).fetchall()

    def _settle(self, conn, unit, peers, now, *, announce_wait: bool, worker_id) -> dict:
        """Keep a batch, or leave it waiting. Callers are already in a transaction.

        One required copy finishes on the uploads already stored. Two required
        copies still compare period, SDE, depth, and ``max_miss_fraction``,
        and still refuse to pair two devices from the same account (that rule
        runs when the second copy is claimed, not here).
        """
        need = self.replica_count(unit["project"])
        if len(peers) < need:
            waiting = "waiting for a second worker" if need == 2 else f"waiting for {need} copies"
            if announce_wait and worker_id is not None:
                self._event(
                    conn, "info",
                    f"{self._name(conn, worker_id)} submitted unit {unit['id']} ({waiting})",
                )
            return {"status": "accepted", "verdict": "pending", "detail": waiting}
        if need <= 1:
            confirmed = self._finish(conn, unit, peers, "checked once", now)
            return {
                "status": "accepted",
                "verdict": "agreed",
                "detail": "checked once",
                "candidates": len(confirmed),
            }
        handler = get_handler(unit["project"])
        payload = json.loads(unit["payload_json"])
        base_stars = json.loads(peers[0]["stars_json"])
        reason = "accepted"
        for peer in peers[1:]:
            ok, reason = handler.validate(
                payload, base_stars, json.loads(peer["stars_json"]), self.settings,
            )
            if not ok:
                self._reissue(conn, unit, reason)
                return {"status": "accepted", "verdict": "disagreed", "detail": reason}
        confirmed = self._finish(conn, unit, peers, reason, now)
        return {
            "status": "accepted",
            "verdict": "agreed",
            "detail": reason,
            "candidates": len(confirmed),
        }

    def status(self, project: str | None = None) -> dict:
        if project is not None:
            project = self._require_project(project)
        now = self.clock()
        sectors = self._sector_progress(project, now)
        totals = {
            "units": 0,
            "agreed": 0,
            "tics": 0,
            "tics_agreed": 0,
            "units_submitted": 0,
            "tics_submitted": 0,
            "units_in_progress": 0,
            "tics_in_progress": 0,
        }
        for sector in sectors:
            for key in totals:
                totals[key] += int(sector[key] or 0)
        if project:
            leased = self.conn.execute(
                """SELECT COUNT(*) AS n FROM assignments
                   WHERE status='leased' AND expires_at>=? AND project=?""",
                (iso(now), project),
            ).fetchone()["n"]
            new = self.conn.execute(
                """SELECT COUNT(*) AS n FROM candidates c
                   JOIN units u ON u.id = c.unit_id
                   WHERE c.status='NEW' AND u.project=?""",
                (project,),
            ).fetchone()["n"]
        else:
            leased = self.conn.execute(
                "SELECT COUNT(*) AS n FROM assignments WHERE status='leased' AND expires_at>=?",
                (iso(now),),
            ).fetchone()["n"]
            new = self.conn.execute(
                "SELECT COUNT(*) AS n FROM candidates WHERE status='NEW'"
            ).fetchone()["n"]
        workers = self.workers_public(now=now)
        counted = project or DEFAULT_PROJECT
        return {
            "project": project or "all",
            "units_total": totals["units"],
            "units_agreed": totals["agreed"],
            "units_submitted": totals["units_submitted"],
            "units_in_progress": totals["units_in_progress"],
            "assignments_leased": int(leased or 0),
            "tics_total": totals["tics"],
            "tics_agreed": totals["tics_agreed"],
            "tics_submitted": totals["tics_submitted"],
            "tics_in_progress": totals["tics_in_progress"],
            "workers_known": len(workers),
            "workers_online": sum(1 for worker in workers if worker["presence"] == "online"),
            "workers_idle": sum(1 for worker in workers if worker["presence"] == "idle"),
            "workers_offline": sum(1 for worker in workers if worker["presence"] == "offline"),
            "candidates_new": int(new or 0),
            "lease_hours": self.settings.lease_s / 3600,
            "replicas": self.replica_count(counted),
            "copies_required": self.public_copies(),
            "sectors": sectors,
        }

    def workers_public(self, now: datetime | None = None) -> list[dict]:
        moment = now or self.clock()
        rows = [row for row in self._worker_rows(moment, include_revoked=False)]
        rows.sort(key=lambda row: (
            {"online": 0, "idle": 1, "offline": 2}[row["presence"]],
            -int(row["stars_submitted"]),
            row["name"],
        ))
        return rows

    def _sector_progress(self, project: str | None, now: datetime) -> list[dict]:
        """Per sector: verified (agreed), submitted by one worker, and leased but not yet submitted.

        A unit's stars sit in exactly one of those three, or in none when nobody has it.
        Agreement still moves a unit into verified; this split is only for display.
        """
        where = ""
        args: list = [iso(now)]
        if project:
            where = "WHERE u.project=?"
            args.append(project)
        rows = self.conn.execute(
            f"""SELECT u.project AS project, u.sector AS sector,
                       COUNT(*) AS units,
                       SUM(CASE WHEN u.state='agreed' THEN 1 ELSE 0 END) AS agreed,
                       SUM(u.n_tics) AS tics,
                       SUM(CASE WHEN u.state='agreed' THEN u.n_tics ELSE 0 END) AS tics_agreed,
                       SUM(CASE WHEN u.state!='agreed' AND IFNULL(sub.n, 0) > 0 THEN 1 ELSE 0 END) AS units_submitted,
                       SUM(CASE WHEN u.state!='agreed' AND IFNULL(sub.n, 0) > 0 THEN u.n_tics ELSE 0 END) AS tics_submitted,
                       SUM(CASE WHEN u.state!='agreed' AND IFNULL(sub.n, 0) = 0 AND IFNULL(lease.n, 0) > 0
                                THEN 1 ELSE 0 END) AS units_in_progress,
                       SUM(CASE WHEN u.state!='agreed' AND IFNULL(sub.n, 0) = 0 AND IFNULL(lease.n, 0) > 0
                                THEN u.n_tics ELSE 0 END) AS tics_in_progress
                FROM units u
                LEFT JOIN (
                    SELECT unit_id, generation, COUNT(*) AS n
                    FROM assignments
                    WHERE status='submitted'
                    GROUP BY unit_id, generation
                ) sub ON sub.unit_id = u.id AND sub.generation = u.generation
                LEFT JOIN (
                    SELECT unit_id, generation, COUNT(*) AS n
                    FROM assignments
                    WHERE status='leased' AND expires_at >= ?
                    GROUP BY unit_id, generation
                ) lease ON lease.unit_id = u.id AND lease.generation = u.generation
                {where}
                GROUP BY u.project, u.sector
                ORDER BY u.sector DESC""",
            args,
        ).fetchall()
        return [
            {
                "project": row["project"],
                "sector": int(row["sector"]),
                "units": int(row["units"] or 0),
                "agreed": int(row["agreed"] or 0),
                "units_submitted": int(row["units_submitted"] or 0),
                "units_in_progress": int(row["units_in_progress"] or 0),
                "tics": int(row["tics"] or 0),
                "tics_agreed": int(row["tics_agreed"] or 0),
                "tics_submitted": int(row["tics_submitted"] or 0),
                "tics_in_progress": int(row["tics_in_progress"] or 0),
            }
            for row in rows
        ]

    def _worker_rows(self, now: datetime, *, include_revoked: bool) -> list[dict]:
        """Display counts. Submitted includes later-verified work; scoring columns are unchanged."""
        bases = self.conn.execute(
            """SELECT id, name, created_at, revoked_at, last_seen_at,
                      stars_searched, candidates_found, units_agreed
               FROM workers ORDER BY id"""
        ).fetchall()
        stats = {
            int(row["worker_id"]): row
            for row in self.conn.execute(
                """SELECT r.worker_id AS worker_id,
                          SUM(CASE WHEN r.verdict IN ('pending', 'agreed') THEN u.n_tics ELSE 0 END) AS stars_submitted,
                          SUM(CASE WHEN r.verdict='agreed' THEN u.n_tics ELSE 0 END) AS stars_verified,
                          SUM(CASE WHEN r.verdict IN ('pending', 'agreed') THEN 1 ELSE 0 END) AS batches_submitted,
                          SUM(CASE WHEN r.verdict='agreed' THEN 1 ELSE 0 END) AS batches_verified,
                          SUM(CASE WHEN r.verdict IN ('pending', 'agreed') THEN r.n_candidates ELSE 0 END) AS candidates_submitted
                   FROM results r
                   JOIN units u ON u.id = r.unit_id
                   GROUP BY r.worker_id"""
            )
        }
        leases: dict[int, sqlite3.Row] = {}
        for row in self.conn.execute(
            """SELECT a.worker_id, u.sector, u.id AS unit_id,
                      a.progress_phase, a.progress_done, a.progress_total
               FROM assignments a
               JOIN units u ON u.id = a.unit_id
               WHERE a.status='leased' AND a.expires_at >= ?
               ORDER BY a.id ASC""",
            (iso(now),),
        ):
            # Oldest lease: the batch being searched. A newer one is only the prefetch slot.
            leases.setdefault(int(row["worker_id"]), row)
        out = []
        for row in bases:
            if row["revoked_at"] and not include_revoked:
                continue
            worker_id = int(row["id"])
            stat = stats.get(worker_id)
            lease = leases.get(worker_id)
            revoked = bool(row["revoked_at"])
            if revoked:
                presence = "offline"
                activity = "—"
            else:
                presence = _presence(
                    row["last_seen_at"], lease is not None, now, self.settings.online_window_s,
                )
                if lease is not None:
                    activity = activity_line(
                        int(lease["sector"]), int(lease["unit_id"]),
                        lease["progress_phase"], lease["progress_done"], lease["progress_total"],
                    )
                elif presence == "idle":
                    activity = "waiting for work"
                else:
                    activity = "—"
            out.append({
                "id": worker_id,
                "name": row["name"],
                "created_at": row["created_at"],
                "revoked_at": row["revoked_at"],
                "stars_searched": int(row["stars_searched"]),
                "candidates_found": int(row["candidates_found"]),
                "candidates_agreed": int(row["candidates_found"]),
                "units_agreed": int(row["units_agreed"]),
                "stars_submitted": int(stat["stars_submitted"] or 0) if stat else 0,
                "stars_verified": int(stat["stars_verified"] or 0) if stat else 0,
                "batches_submitted": int(stat["batches_submitted"] or 0) if stat else 0,
                "batches_verified": int(stat["batches_verified"] or 0) if stat else 0,
                "candidates_submitted": int(stat["candidates_submitted"] or 0) if stat else 0,
                "last_seen_at": row["last_seen_at"],
                "last_seen": format_ago(row["last_seen_at"], now),
                "online": presence == "online",
                "presence": presence,
                "activity": activity,
            })
        return out

    def candidates_public(self, limit: int = 50, project: str | None = None) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        if project is not None:
            project = self._require_project(project)
            rows = self.conn.execute(
                """SELECT c.sector, c.tic, c.period, c.depth, c.sde, c.snr, c.status, c.score, c.flags
                   FROM candidates c JOIN units u ON u.id = c.unit_id
                   WHERE u.project=?
                   ORDER BY c.score DESC, c.id DESC LIMIT ?""",
                (project, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT sector, tic, period, depth, sde, snr, status, score, flags
                   FROM candidates ORDER BY score DESC, id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def contributions(self, project: str | None = None) -> list[dict]:
        """Append-only credit rows. This process does not delete them or edit outcomes.

        ``account_id`` is who gets the credit. ``verified`` stays 0 until another
        account agrees. ``revoked`` is reserved so Phase 4 can drop pending credit
        without deleting the row. Nothing here sets it.
        """
        if project is not None:
            project = self._require_project(project)
            rows = self.conn.execute(
                """SELECT user_id, account_id, project, unit_id, generation, outcome,
                          verified, revoked, detail, created_at
                   FROM contributions WHERE project=? ORDER BY id""",
                (project,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT user_id, account_id, project, unit_id, generation, outcome,
                          verified, revoked, detail, created_at
                   FROM contributions ORDER BY id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def events_since(self, after_id: int = 0, limit: int = 200) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        rows = self.conn.execute(
            "SELECT id, ts, level, message FROM events WHERE id>? ORDER BY id LIMIT ?",
            (int(after_id), limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def _finish(self, conn, unit, peers, reason: str, now) -> list[dict]:
        conn.execute("UPDATE units SET state='agreed', agreed_at=? WHERE id=?", (iso(now), unit["id"]))
        ids = [int(peer["id"]) for peer in peers]
        marks = ",".join("?" for _ in ids)
        conn.execute(f"UPDATE results SET verdict='agreed' WHERE id IN ({marks})", ids)
        payload = json.loads(unit["payload_json"])
        confirmed = get_handler(unit["project"]).verified_candidates(
            payload, [json.loads(peer["candidates_json"]) for peer in peers],
        )
        for row in confirmed:
            conn.execute(
                """INSERT INTO candidates (
                       unit_id, generation, tic, sector, period, depth, sde, snr, status, score, flags
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    unit["id"], unit["generation"], row["tic"], unit["sector"],
                    row.get("period"), row.get("depth"), row.get("sde"), row.get("snr"),
                    "NEW", row.get("score"), row.get("flags") or "",
                ),
            )
        for peer in peers:
            self._credit(conn, int(peer["worker_id"]), json.loads(peer["stars_json"]), len(confirmed))
            self._contribute(conn, int(peer["worker_id"]), unit, "agreed", 1, reason, now)
        self._event(
            conn, "success",
            f"unit {unit['id']} sector {unit['sector']} agreed ({reason}); {len(confirmed)} new candidates",
        )
        return confirmed

    def _credit(self, conn, worker_id: int, stars: list[dict], new_count: int) -> None:
        conn.execute(
            """UPDATE workers
               SET stars_searched = stars_searched + ?,
                   candidates_found = candidates_found + ?,
                   units_agreed = units_agreed + 1
               WHERE id=?""",
            (measured_count(stars), new_count, worker_id),
        )

    def _reissue(self, conn, unit, reason: str) -> None:
        generation = int(unit["generation"])
        rows = conn.execute(
            """SELECT worker_id FROM results
               WHERE unit_id=? AND generation=? AND verdict='pending'""",
            (unit["id"], generation),
        ).fetchall()
        now = self.clock()
        for row in rows:
            self._contribute(conn, int(row["worker_id"]), unit, "rejected", 0, reason, now)
        conn.execute(
            "UPDATE results SET verdict='disagreed' WHERE unit_id=? AND generation=? AND verdict='pending'",
            (unit["id"], generation),
        )
        conn.execute(
            """UPDATE assignments SET status='rejected'
               WHERE unit_id=? AND generation=? AND status IN ('leased', 'submitted')""",
            (unit["id"], generation),
        )
        nxt = generation + 1
        conn.execute("UPDATE units SET generation=? WHERE id=?", (nxt, unit["id"]))
        self._event(
            conn, "warn",
            f"unit {unit['id']} sector {unit['sector']} disagreed ({reason}); reissued as generation {nxt}",
        )

    def _pick_unit(self, conn, worker_id: int, project: str):
        need = self.replica_count(project)
        units = conn.execute(
            """SELECT id, project, sector, generation, payload_json, n_tics
               FROM units WHERE state='open' AND project=? ORDER BY id""",
            (project,),
        ).fetchall()
        best = None
        for unit in units:
            slots = conn.execute(
                """SELECT worker_id FROM assignments
                   WHERE unit_id=? AND generation=? AND status IN ('leased', 'submitted')""",
                (unit["id"], unit["generation"]),
            ).fetchall()
            holders = {int(slot["worker_id"]) for slot in slots}
            # Two copies of one generation must be two accounts. A second device
            # on the account that already holds this unit is not the other copy.
            if len(slots) >= need or self._blocked_by_account(conn, worker_id, holders):
                continue
            rank = (-len(slots), int(unit["id"]))
            if best is None or rank < best[0]:
                best = (rank, unit, len(slots))
        if best is None:
            return None
        return best[1], best[2]

    def _live_leases(self, conn, worker_id: int) -> list[dict]:
        rows = conn.execute(
            """SELECT a.id, a.unit_id, a.generation, a.leased_at, a.expires_at,
                      u.project, u.sector, u.payload_json, u.n_tics
               FROM assignments a JOIN units u ON u.id = a.unit_id
               WHERE a.worker_id=? AND a.status='leased'
               ORDER BY a.id ASC""",
            (worker_id,),
        ).fetchall()
        return [
            self._assignment_body(
                assignment_id=int(row["id"]),
                unit_id=int(row["unit_id"]),
                project=row["project"],
                generation=int(row["generation"]),
                sector=int(row["sector"]),
                payload_json=row["payload_json"],
                n_tics=int(row["n_tics"]),
                worker_id=worker_id,
                leased_at=row["leased_at"],
                expires_at=row["expires_at"],
            )
            for row in rows
        ]

    def _live_lease(self, conn, worker_id: int) -> dict | None:
        rows = self._live_leases(conn, worker_id)
        return rows[0] if rows else None

    def _assignment_body(
        self, *, assignment_id, unit_id, project, generation, sector,
        payload_json, n_tics, worker_id, leased_at, expires_at,
    ) -> dict:
        payload = json.loads(payload_json)
        tics = payload.get("tics")
        if not isinstance(tics, list):
            tics = []
        return {
            "assignment_id": int(assignment_id),
            "unit_id": int(unit_id),
            "project": project,
            "user_id": int(worker_id),
            "generation": int(generation),
            "sector": int(sector),
            "tics": tics,
            "payload": payload,
            "n_tics": int(n_tics),
            "leased_at": leased_at,
            "expires_at": expires_at,
            "lease_seconds": self.settings.lease_s,
        }

    def _require_project(self, project: str) -> str:
        try:
            return parse_project(project)
        except ValueError as exc:
            raise CoordError(str(exc), 404) from exc

    def _same_project(self, conn, assignment, project: str | None) -> None:
        if project is None:
            return
        project = self._require_project(project)
        row = conn.execute("SELECT project FROM units WHERE id=?", (assignment["unit_id"],)).fetchone()
        if row is None or row["project"] != project:
            raise CoordError("assignment is for a different project", 409)

    def _contribute(self, conn, user_id: int, unit, outcome: str, verified: int, detail: str, now) -> None:
        conn.execute(
            """INSERT INTO contributions
                   (user_id, account_id, project, unit_id, generation, outcome, verified, detail, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(user_id), self._account_id(conn, user_id), unit["project"], int(unit["id"]),
                int(unit["generation"]), outcome, int(verified), detail, iso(now),
            ),
        )

    def _migrate(self) -> None:
        """Add columns on a database created before them. Every change is an ALTER, never a rebuild."""
        names = self._columns("units")
        if not names:
            self._add_columns("assignments", {
                "progress_phase": "TEXT",
                "progress_done": "INTEGER",
                "progress_total": "INTEGER",
            })
            self._migrate_accounts()
            return
        if "project" not in names:
            self.conn.execute("ALTER TABLE units ADD COLUMN project TEXT NOT NULL DEFAULT 'exoplanets'")
        if "payload_json" not in names:
            self.conn.execute("ALTER TABLE units ADD COLUMN payload_json TEXT NOT NULL DEFAULT ''")
            if "tics_json" in names:
                for row in self.conn.execute("SELECT id, sector, tics_json FROM units"):
                    tics = json.loads(row["tics_json"])
                    payload = json.dumps({"sector": int(row["sector"]), "tics": tics})
                    self.conn.execute(
                        "UPDATE units SET payload_json=? WHERE id=?",
                        (payload, row["id"]),
                    )
        for table in ("assignments", "results"):
            cols = self._columns(table)
            if cols and "project" not in cols:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN project TEXT NOT NULL DEFAULT 'exoplanets'"
                )
        # Nullable progress columns. Older workers never send them; a live database keeps its rows.
        self._add_columns("assignments", {
            "progress_phase": "TEXT",
            "progress_done": "INTEGER",
            "progress_total": "INTEGER",
        })
        self._migrate_accounts()

    def _migrate_accounts(self) -> None:
        """Attach legacy invites to accounts. Safe to run twice on a live database."""
        self._add_columns("workers", {
            "account_id": "INTEGER",
            "hostname": "TEXT",
            "kind": "TEXT NOT NULL DEFAULT 'legacy'",
        })
        self._add_columns("contributions", {
            "account_id": "INTEGER",
            "revoked": "INTEGER NOT NULL DEFAULT 0",
        })
        if "account_id" not in self._columns("workers"):
            return
        with self._tx() as conn:
            pending = conn.execute(
                "SELECT id, name, created_at FROM workers WHERE account_id IS NULL ORDER BY id"
            ).fetchall()
            for row in pending:
                account_id = self._insert_legacy_account(
                    conn, row["name"], row["created_at"], suffix_id=int(row["id"]),
                )
                conn.execute(
                    "UPDATE workers SET account_id=?, kind='legacy' WHERE id=?",
                    (account_id, row["id"]),
                )
            if "account_id" in self._columns("contributions"):
                conn.execute(
                    """UPDATE contributions
                       SET account_id = (
                           SELECT account_id FROM workers WHERE workers.id = contributions.user_id
                       )
                       WHERE account_id IS NULL"""
                )
        if "account_id" in self._columns("contributions"):
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_contrib_account ON contributions(account_id, revoked, verified)"
            )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_workers_account ON workers(account_id)"
        )

    def _insert_legacy_account(self, conn, name: str, created_at: str, suffix_id: int | None) -> int:
        display, key = self._legacy_display(conn, name, suffix_id)
        cur = conn.execute(
            """INSERT INTO accounts (
                   email, email_key, password_hash, display_name, display_key, provider,
                   provider_sub, email_verified_at, created_at, legacy, trust_score, tier
               ) VALUES (NULL, NULL, NULL, ?, ?, 'legacy', NULL, NULL, ?, 1, NULL, '')""",
            (display, key, created_at),
        )
        return int(cur.lastrowid)

    def _legacy_display(self, conn, name: str, suffix_id: int | None) -> tuple[str, str]:
        key = name.casefold()
        taken = conn.execute("SELECT 1 FROM accounts WHERE display_key=?", (key,)).fetchone()
        if not taken:
            return name, key
        if suffix_id is None:
            raise ValueError(f"worker {name} already exists")
        display = f"{name}-{suffix_id}"
        return display, display.casefold()

    def _account_id(self, conn, worker_id: int) -> int | None:
        if "account_id" not in self._columns("workers"):
            return None
        row = conn.execute("SELECT account_id FROM workers WHERE id=?", (int(worker_id),)).fetchone()
        if row is None or row["account_id"] is None:
            return None
        return int(row["account_id"])

    def _blocked_by_account(self, conn, worker_id: int, holders: set[int]) -> bool:
        if worker_id in holders:
            return True
        mine = self._account_id(conn, worker_id)
        if mine is None or not holders:
            return False
        marks = ",".join("?" for _ in holders)
        rows = conn.execute(
            f"SELECT account_id FROM workers WHERE id IN ({marks})",
            tuple(holders),
        ).fetchall()
        for row in rows:
            if row["account_id"] is not None and int(row["account_id"]) == mine:
                return True
        return False

    def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}

    def _add_columns(self, table: str, columns: dict[str, str]) -> None:
        have = self._columns(table)
        if not have:
            return
        for name, decl in columns.items():
            if name not in have:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def _owned_lease(self, conn, worker_id: int, assignment_id: int):
        assignment = conn.execute("SELECT * FROM assignments WHERE id=?", (int(assignment_id),)).fetchone()
        if assignment is None or int(assignment["worker_id"]) != int(worker_id):
            raise CoordError("assignment not found", 404)
        if assignment["status"] != "leased":
            raise CoordError(f"assignment is {assignment['status']}", 409)
        return assignment

    def _expire(self, conn, now: datetime) -> None:
        rows = conn.execute(
            """SELECT a.id, a.unit_id, a.worker_id, u.sector
               FROM assignments a JOIN units u ON u.id = a.unit_id
               WHERE a.status='leased' AND a.expires_at < ?""",
            (iso(now),),
        ).fetchall()
        for row in rows:
            conn.execute("UPDATE assignments SET status='expired' WHERE id=?", (row["id"],))
            self._event(
                conn, "warn",
                f"lease expired: unit {row['unit_id']} sector {row['sector']} ({self._name(conn, row['worker_id'])})",
            )

    def _touch(self, conn, worker_id: int, now: datetime) -> None:
        conn.execute("UPDATE workers SET last_seen_at=? WHERE id=?", (iso(now), worker_id))

    def _name(self, conn, worker_id: int) -> str:
        row = conn.execute("SELECT name FROM workers WHERE id=?", (worker_id,)).fetchone()
        return row["name"] if row else f"worker {worker_id}"

    def _event(self, conn, level: str, message: str) -> None:
        conn.execute(
            "INSERT INTO events (ts, level, message) VALUES (?, ?, ?)",
            (iso(self.clock()), level, message),
        )
        max_id = conn.execute("SELECT MAX(id) AS n FROM events").fetchone()["n"] or 0
        if max_id > 2500:
            conn.execute("DELETE FROM events WHERE id < ?", (max_id - 2000,))
