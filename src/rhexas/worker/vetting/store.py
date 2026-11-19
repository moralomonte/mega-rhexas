"""Vetting rows in the worker's dashboard sqlite, keyed by TIC and sector.

``migrate`` is safe to run on every connection. The scan pipeline does not
read or write this table; the background vetting thread does.
"""
from __future__ import annotations

import json
import math
import time

_TABLE = """
CREATE TABLE IF NOT EXISTS vetting (
    tic INTEGER NOT NULL,
    sector INTEGER NOT NULL,
    unit INTEGER,
    period REAL,
    t0 REAL,
    duration REAL,
    depth REAL,
    snr REAL,
    sde REAL,
    ra REAL,
    dec REAL,
    verdict TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    result_json TEXT,
    vetted_at REAL,
    updated_at REAL,
    retry_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tic, sector)
);
"""


def migrate(conn) -> None:
    conn.executescript(_TABLE)
    conn.execute("CREATE INDEX IF NOT EXISTS vetting_by_state ON vetting(state, updated_at)")
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
    conn.execute("INSERT OR IGNORE INTO schema_migrations (name) VALUES ('vetting-v1')")


def _clean(value):
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        try:
            return _clean(value.item())
        except Exception:
            return None
    return str(value)


def dumps(value) -> str:
    return json.dumps(_clean(value), separators=(",", ":"), allow_nan=False)


def loads(raw):
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload


def row_public(row) -> dict:
    """One table row for the Vetting page. The heavy JSON stays out of the list."""
    result = loads(row["result_json"]) if "result_json" in row.keys() else None
    return {
        "tic": int(row["tic"]),
        "sector": int(row["sector"]),
        "unit": row["unit"],
        "period": row["period"],
        "depth": row["depth"],
        "snr": row["snr"],
        "sde": row["sde"],
        "verdict": row["verdict"],
        "state": row["state"] or "pending",
        "vetted_at": row["vetted_at"],
        "updated_at": row["updated_at"],
        "retry_at": row["retry_at"],
        "attempts": int(row["attempts"] or 0),
        "has_result": bool(result),
    }


def detail_public(row) -> dict:
    item = row_public(row)
    result = loads(row["result_json"]) or {}
    if not isinstance(result, dict):
        result = {}
    item["t0"] = row["t0"]
    item["duration"] = row["duration"]
    item["ra"] = row["ra"]
    item["dec"] = row["dec"]
    item["checks"] = result.get("checks") or []
    item["plot"] = result.get("plot")
    item["links"] = result.get("links") or {}
    item["disclaimer"] = result.get("disclaimer")
    item["radius"] = result.get("radius")
    item["mass"] = result.get("mass")
    return item


def list_rows(conn, limit: int = 500) -> list[dict]:
    migrate(conn)
    rows = conn.execute(
        """SELECT * FROM vetting
           ORDER BY COALESCE(vetted_at, updated_at) DESC
           LIMIT ?""",
        (int(limit),),
    ).fetchall()
    return [row_public(row) for row in rows]


def get_row(conn, tic: int, sector: int):
    migrate(conn)
    return conn.execute(
        "SELECT * FROM vetting WHERE tic=? AND sector=?",
        (int(tic), int(sector)),
    ).fetchone()


def sync_candidates(conn, now: float | None = None) -> int:
    """Queue candidates that have never been vetted. Does not reset a finished row."""
    migrate(conn)
    now = time.time() if now is None else float(now)
    before = conn.execute("SELECT COUNT(*) AS n FROM vetting").fetchone()["n"]
    conn.execute(
        """INSERT OR IGNORE INTO vetting (
               tic, sector, unit, period, depth, snr, sde, state, updated_at, attempts
           )
           SELECT tic, sector, unit_id, period, depth, snr, sde, 'pending', ?, 0
           FROM candidates"""
        ,
        (now,),
    )
    after = conn.execute("SELECT COUNT(*) AS n FROM vetting").fetchone()["n"]
    return int(after or 0) - int(before or 0)


def mark_pending(conn, tic: int, sector: int, now: float | None = None) -> bool:
    """Ask for another pass. Clears a cached failure by leaving state pending."""
    migrate(conn)
    now = time.time() if now is None else float(now)
    existing = get_row(conn, tic, sector)
    if existing is None:
        conn.execute(
            """INSERT INTO vetting (tic, sector, state, updated_at, attempts, retry_at, vetted_at)
               VALUES (?, ?, 'pending', ?, 0, NULL, NULL)""",
            (int(tic), int(sector), now),
        )
        return True
    conn.execute(
        """UPDATE vetting
           SET state='pending', retry_at=NULL, attempts=0, updated_at=?
           WHERE tic=? AND sector=?""",
        (now, int(tic), int(sector)),
    )
    return True


def recover_stale(conn, now: float, stale_s: float = 900.0) -> None:
    """A crash mid-check leaves a row running. Put an old one back on the queue."""
    migrate(conn)
    conn.execute(
        """UPDATE vetting SET state='pending'
           WHERE state='running' AND COALESCE(updated_at, 0) < ?""",
        (float(now) - float(stale_s),),
    )


def claim_next(conn, now: float):
    """Take one pending row, or one whose catalog retry is due. None if the queue is empty."""
    migrate(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """SELECT tic, sector FROM vetting
               WHERE state='pending'
                  OR (state='done' AND retry_at IS NOT NULL AND retry_at <= ?)
               ORDER BY CASE WHEN state='pending' THEN 0 ELSE 1 END, COALESCE(updated_at, 0)
               LIMIT 1""",
            (float(now),),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        cur = conn.execute(
            """UPDATE vetting SET state='running', updated_at=?
               WHERE tic=? AND sector=?
                 AND (state='pending' OR (state='done' AND retry_at IS NOT NULL AND retry_at <= ?))""",
            (float(now), int(row["tic"]), int(row["sector"]), float(now)),
        )
        if cur.rowcount != 1:
            conn.execute("COMMIT")
            return None
        claimed = conn.execute(
            "SELECT * FROM vetting WHERE tic=? AND sector=?",
            (int(row["tic"]), int(row["sector"])),
        ).fetchone()
        conn.execute("COMMIT")
        return claimed
    except Exception:
        conn.execute("ROLLBACK")
        raise


def save_result(conn, tic: int, sector: int, *, verdict: str, result: dict, fields: dict,
                retry_at, attempts: int, now: float) -> None:
    migrate(conn)
    conn.execute(
        """UPDATE vetting SET
               unit=COALESCE(?, unit),
               period=COALESCE(?, period),
               t0=COALESCE(?, t0),
               duration=COALESCE(?, duration),
               depth=COALESCE(?, depth),
               snr=COALESCE(?, snr),
               sde=COALESCE(?, sde),
               ra=COALESCE(?, ra),
               dec=COALESCE(?, dec),
               verdict=?,
               state='done',
               result_json=?,
               vetted_at=?,
               updated_at=?,
               retry_at=?,
               attempts=?
           WHERE tic=? AND sector=?""",
        (
            fields.get("unit"), fields.get("period"), fields.get("t0"), fields.get("duration"),
            fields.get("depth"), fields.get("snr"), fields.get("sde"), fields.get("ra"), fields.get("dec"),
            verdict, dumps(result), float(now), float(now), retry_at, int(attempts),
            int(tic), int(sector),
        ),
    )
