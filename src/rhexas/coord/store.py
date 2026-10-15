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
    result_id INTEGER
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


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return "rhx_" + secrets.token_urlsafe(32)


class Store:
    def __init__(self, path: Path | str, settings: Settings | None = None, clock=None):
        self.path = Path(path)
        self.settings = settings or Settings()
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
        """How many distinct workers must finish a unit. A toml override wins."""
        project = self._require_project(project)
        if self.settings.replicas is not None:
            return int(self.settings.replicas)
        return get_handler(project).replicas

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
            conn.execute(
                "INSERT INTO workers (name, token_hash, created_at) VALUES (?, ?, ?)",
                (name, hash_token(token), iso(self.clock())),
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
        rows = self.conn.execute(
            """SELECT name, created_at, revoked_at, last_seen_at, stars_searched, candidates_found, units_agreed
               FROM workers ORDER BY id"""
        ).fetchall()
        return [dict(row) for row in rows]

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

    def claim(self, worker_id: int, project: str = DEFAULT_PROJECT) -> dict | None:
        project = self._require_project(project)
        with self._tx() as conn:
            now = self.clock()
            self._expire(conn, now)
            self._touch(conn, worker_id, now)
            live = self._live_lease(conn, worker_id)
            if live is not None:
                return live
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
        with self._tx() as conn:
            now = self.clock()
            self._expire(conn, now)
            self._touch(conn, worker_id, now)
            return self._live_lease(conn, worker_id)

    def heartbeat(self, worker_id: int, assignment_id: int, project: str | None = None) -> dict:
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
            conn.execute("UPDATE assignments SET expires_at=? WHERE id=?", (iso(expires), assignment_id))
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
            need = self.replica_count(unit["project"])
            peers = conn.execute(
                """SELECT r.* FROM results r
                   JOIN assignments a ON a.id = r.assignment_id
                   WHERE r.unit_id=? AND r.generation=? AND r.verdict='pending'
                     AND a.status='submitted'
                   ORDER BY r.id""",
                (unit["id"], assignment["generation"]),
            ).fetchall()
            if len(peers) < need:
                waiting = "waiting for a second worker" if need == 2 else f"waiting for {need} copies"
                self._event(
                    conn, "info",
                    f"{self._name(conn, worker_id)} submitted unit {unit['id']} ({waiting})",
                )
                return {"status": "accepted", "verdict": "pending", "detail": waiting}
            if need <= 1:
                confirmed = self._finish(conn, unit, peers, "accepted", now)
                return {
                    "status": "accepted",
                    "verdict": "agreed",
                    "detail": "accepted",
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
        where = "WHERE project=?" if project else ""
        args = (project,) if project else ()
        sectors = [
            {
                "project": row["project"],
                "sector": int(row["sector"]),
                "units": int(row["units"]),
                "agreed": int(row["agreed"] or 0),
                "tics": int(row["tics"] or 0),
                "tics_agreed": int(row["tics_agreed"] or 0),
            }
            for row in self.conn.execute(
                f"""SELECT project, sector,
                          COUNT(*) AS units,
                          SUM(CASE WHEN state='agreed' THEN 1 ELSE 0 END) AS agreed,
                          SUM(n_tics) AS tics,
                          SUM(CASE WHEN state='agreed' THEN n_tics ELSE 0 END) AS tics_agreed
                   FROM units {where} GROUP BY project, sector ORDER BY sector DESC""",
                args,
            )
        ]
        totals = self.conn.execute(
            f"""SELECT COUNT(*) AS units,
                      SUM(CASE WHEN state='agreed' THEN 1 ELSE 0 END) AS agreed,
                      SUM(n_tics) AS tics,
                      SUM(CASE WHEN state='agreed' THEN n_tics ELSE 0 END) AS tics_agreed
               FROM units {where}""",
            args,
        ).fetchone()
        if project:
            leased = self.conn.execute(
                "SELECT COUNT(*) AS n FROM assignments WHERE status='leased' AND project=?",
                (project,),
            ).fetchone()["n"]
            new = self.conn.execute(
                """SELECT COUNT(*) AS n FROM candidates c
                   JOIN units u ON u.id = c.unit_id
                   WHERE c.status='NEW' AND u.project=?""",
                (project,),
            ).fetchone()["n"]
        else:
            leased = self.conn.execute(
                "SELECT COUNT(*) AS n FROM assignments WHERE status='leased'"
            ).fetchone()["n"]
            new = self.conn.execute(
                "SELECT COUNT(*) AS n FROM candidates WHERE status='NEW'"
            ).fetchone()["n"]
        workers = self.workers_public(now=now)
        online = sum(1 for worker in workers if worker["online"])
        counted = project or DEFAULT_PROJECT
        return {
            "project": project or "all",
            "units_total": int(totals["units"] or 0),
            "units_agreed": int(totals["agreed"] or 0),
            "assignments_leased": int(leased or 0),
            "tics_total": int(totals["tics"] or 0),
            "tics_agreed": int(totals["tics_agreed"] or 0),
            "workers_known": len(workers),
            "workers_online": online,
            "candidates_new": int(new or 0),
            "lease_hours": self.settings.lease_s / 3600,
            "replicas": self.replica_count(counted),
            "sectors": sectors,
        }

    def workers_public(self, now: datetime | None = None) -> list[dict]:
        moment = now or self.clock()
        cutoff = iso(moment - timedelta(seconds=self.settings.online_window_s))
        rows = self.conn.execute(
            """SELECT id, name, last_seen_at, stars_searched, candidates_found, units_agreed, revoked_at
               FROM workers ORDER BY candidates_found DESC, stars_searched DESC, name"""
        ).fetchall()
        out = []
        for row in rows:
            if row["revoked_at"]:
                continue
            seen = row["last_seen_at"]
            out.append({
                "id": int(row["id"]),
                "name": row["name"],
                "stars_searched": int(row["stars_searched"]),
                "candidates_found": int(row["candidates_found"]),
                "units_agreed": int(row["units_agreed"]),
                "last_seen_at": seen,
                "online": bool(seen and seen >= cutoff),
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
        """Append-only credit rows. Nothing in this process updates or deletes them."""
        if project is not None:
            project = self._require_project(project)
            rows = self.conn.execute(
                """SELECT user_id, project, unit_id, generation, outcome, verified, detail, created_at
                   FROM contributions WHERE project=? ORDER BY id""",
                (project,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT user_id, project, unit_id, generation, outcome, verified, detail, created_at
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
            if worker_id in holders or len(slots) >= need:
                continue
            rank = (-len(slots), int(unit["id"]))
            if best is None or rank < best[0]:
                best = (rank, unit, len(slots))
        if best is None:
            return None
        return best[1], best[2]

    def _live_lease(self, conn, worker_id: int) -> dict | None:
        row = conn.execute(
            """SELECT a.id, a.unit_id, a.generation, a.leased_at, a.expires_at,
                      u.project, u.sector, u.payload_json, u.n_tics
               FROM assignments a JOIN units u ON u.id = a.unit_id
               WHERE a.worker_id=? AND a.status='leased'""",
            (worker_id,),
        ).fetchone()
        if row is None:
            return None
        return self._assignment_body(
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
                   (user_id, project, unit_id, generation, outcome, verified, detail, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(user_id), unit["project"], int(unit["id"]), int(unit["generation"]),
                outcome, int(verified), detail, iso(now),
            ),
        )

    def _migrate(self) -> None:
        """Add project and payload columns on a database created before them."""
        names = self._columns("units")
        if not names:
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

    def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}

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
