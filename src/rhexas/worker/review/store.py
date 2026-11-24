"""Local review queue in the worker dashboard sqlite."""
from __future__ import annotations

import json
import math
import time

from ..vetting.store import dumps, loads, migrate as vetting_migrate

_TABLE = """
CREATE TABLE IF NOT EXISTS local_review (
    tic INTEGER NOT NULL,
    sector INTEGER NOT NULL,
    check_key TEXT NOT NULL DEFAULT '',
    period REAL,
    t0 REAL,
    duration REAL,
    depth REAL,
    snr REAL,
    sde REAL,
    rank INTEGER,
    state TEXT NOT NULL DEFAULT 'queued',
    verdict TEXT,
    result_json TEXT,
    upload_state TEXT NOT NULL DEFAULT 'none',
    upload_detail TEXT NOT NULL DEFAULT '',
    download_bytes INTEGER,
    download_note TEXT NOT NULL DEFAULT '',
    checked_at REAL,
    updated_at REAL,
    cancelled INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tic, sector)
);
CREATE INDEX IF NOT EXISTS local_review_state ON local_review(state, updated_at);
"""


def migrate(conn) -> None:
    vetting_migrate(conn)
    conn.executescript(_TABLE)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)"
    )
    conn.execute("INSERT OR IGNORE INTO schema_migrations (name) VALUES ('local-review-v1')")


def upsert_queue(conn, rows: list[dict], now: float) -> None:
    for row in rows:
        conn.execute(
            """INSERT INTO local_review (tic, sector, check_key, period, depth, snr, sde, rank, state, updated_at)
               VALUES (?, ?, '', ?, ?, ?, ?, ?, 'queued', ?)
               ON CONFLICT(tic, sector) DO UPDATE SET
                   period=excluded.period,
                   depth=excluded.depth,
                   snr=excluded.snr,
                   sde=excluded.sde,
                   rank=COALESCE(excluded.rank, local_review.rank),
                   updated_at=excluded.updated_at
               WHERE local_review.state IN ('queued', 'cancelled')""",
            (
                int(row["tic"]),
                int(row["sector"]),
                row.get("period"),
                row.get("depth"),
                row.get("snr"),
                row.get("sde"),
                row.get("rank"),
                float(now),
            ),
        )


def list_rows(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT tic, sector, period, depth, snr, sde, rank, state, verdict,
                  result_json, upload_state, upload_detail, download_bytes, download_note,
                  checked_at, updated_at, cancelled
           FROM local_review ORDER BY COALESCE(rank, 9999), tic"""
    ).fetchall()
    out = []
    for row in rows:
        out.append({
            "tic": int(row["tic"]),
            "sector": int(row["sector"]),
            "period": row["period"],
            "depth": row["depth"],
            "snr": row["snr"],
            "sde": row["sde"],
            "rank": row["rank"],
            "state": row["state"] or "queued",
            "verdict": row["verdict"],
            "upload_state": row["upload_state"] or "none",
            "upload_detail": row["upload_detail"] or "",
            "download_bytes": row["download_bytes"],
            "download_note": row["download_note"] or "",
            "checked_at": row["checked_at"],
            "updated_at": row["updated_at"],
            "cancelled": bool(int(row["cancelled"] or 0)),
            "has_result": bool(row["result_json"]),
        })
    return out


def get_row(conn, tic: int, sector: int):
    return conn.execute(
        "SELECT * FROM local_review WHERE tic=? AND sector=?",
        (int(tic), int(sector)),
    ).fetchone()


def claim_next(conn, now: float):
    row = conn.execute(
        """SELECT * FROM local_review
           WHERE state='queued' AND cancelled=0
           ORDER BY COALESCE(rank, 9999), updated_at
           LIMIT 1"""
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE local_review SET state='running', updated_at=? WHERE tic=? AND sector=?",
        (float(now), int(row["tic"]), int(row["sector"])),
    )
    return {key: row[key] for key in row.keys()}


def save_result(conn, tic, sector, *, verdict, result, upload_state, upload_detail,
                download_bytes, download_note, check_key, now: float) -> None:
    conn.execute(
        """UPDATE local_review SET state='done', verdict=?, result_json=?, upload_state=?,
               upload_detail=?, download_bytes=?, download_note=?, check_key=?, checked_at=?, updated_at=?
           WHERE tic=? AND sector=?""",
        (
            verdict,
            dumps(result),
            upload_state,
            upload_detail,
            download_bytes,
            download_note,
            check_key,
            float(now),
            float(now),
            int(tic),
            int(sector),
        ),
    )


def mark_cancelled(conn, tic: int, sector: int) -> bool:
    cur = conn.execute(
        """UPDATE local_review SET cancelled=1, state='cancelled', updated_at=?
           WHERE tic=? AND sector=? AND state IN ('queued', 'running')""",
        (time.time(), int(tic), int(sector)),
    )
    return cur.rowcount > 0


def detail_public_row(row) -> dict:
    result = loads(row["result_json"]) if row.get("result_json") else {}
    return {
        "tic": int(row["tic"]),
        "sector": int(row["sector"]),
        "state": row["state"],
        "verdict": row["verdict"],
        "checks": result.get("checks") or [],
        "plot": result.get("plot"),
        "links": result.get("links") or {},
        "upload_state": row["upload_state"],
        "upload_detail": row["upload_detail"],
        "download_note": row["download_note"],
        "disclaimer": result.get("disclaimer"),
    }


def enqueue_selected(conn, items: list[dict], now: float) -> int:
    count = 0
    for item in items:
        tic = int(item["tic"])
        sector = int(item["sector"])
        existing = conn.execute(
            "SELECT state FROM local_review WHERE tic=? AND sector=?",
            (tic, sector),
        ).fetchone()
        if existing and existing["state"] in ("running", "done"):
            continue
        conn.execute(
            """INSERT INTO local_review (tic, sector, period, t0, duration, depth, snr, sde, rank, state, cancelled, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?)
               ON CONFLICT(tic, sector) DO UPDATE SET
                   state='queued', cancelled=0, period=excluded.period, t0=excluded.t0, duration=excluded.duration,
                   depth=excluded.depth, snr=excluded.snr, sde=excluded.sde, rank=excluded.rank, updated_at=excluded.updated_at""",
            (
                tic,
                sector,
                item.get("period"),
                item.get("t0"),
                item.get("duration"),
                item.get("depth"),
                item.get("snr"),
                item.get("sde"),
                item.get("rank"),
                float(now),
            ),
        )
        count += 1
    return count