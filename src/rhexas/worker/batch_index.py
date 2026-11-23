"""Local index for batch (unit) replay and the dashboard hex grid.

Each work unit is up to 500 TIC IDs from one sector (see coordinator ``unit_size``).
Star rows and timestamps live in ``dashboard.sqlite``; this module adds a stable
TIC order per unit and builds API payloads without touching the coordinator.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path

# Coordinator default; see ``deploy/coord.example.toml`` ``unit_size``.
UNIT_SIZE = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS unit_catalog (
    sector INTEGER NOT NULL,
    unit INTEGER NOT NULL,
    project TEXT NOT NULL DEFAULT 'exoplanets',
    assignment_id INTEGER,
    n_tics INTEGER NOT NULL DEFAULT 0,
    tics_json TEXT NOT NULL,
    started_at REAL,
    finished_at REAL,
    PRIMARY KEY (sector, unit)
);
CREATE INDEX IF NOT EXISTS unit_catalog_sector ON unit_catalog(sector, unit);
"""


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name) VALUES ('unit-catalog-v1')"
    )


def _read_json(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _tics_from_sources(cache: Path, sector: int, unit: int) -> list[int]:
    cache = Path(cache)
    for row in _rows_from_holdings(cache):
        try:
            if int(row.get("sector") or 0) == sector and int(row.get("unit_id") or 0) == unit:
                raw = row.get("tics")
                if isinstance(raw, list) and raw:
                    return [int(t) for t in raw]
        except (TypeError, ValueError):
            continue
    state = _read_json(cache / "state.json")
    if isinstance(state, dict):
        try:
            if int(state.get("sector") or 0) == sector and int(state.get("unit_id") or 0) == unit:
                raw = state.get("tics")
                if isinstance(raw, list) and raw:
                    return [int(t) for t in raw]
        except (TypeError, ValueError):
            pass
    queue = _read_json(cache / "queue.json")
    rows = queue.get("units") if isinstance(queue, dict) else None
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                if int(row.get("sector") or 0) == sector and int(row.get("unit_id") or 0) == unit:
                    raw = row.get("tics")
                    if isinstance(raw, list) and raw:
                        return [int(t) for t in raw]
            except (TypeError, ValueError):
                continue
    return []


def register_unit(
    conn: sqlite3.Connection,
    *,
    sector: int,
    unit: int,
    project: str,
    assignment_id: int | None,
    tics: list[int],
    started_at: float | None = None,
) -> None:
    """Remember TIC order for a unit. Safe to call again with the same list."""
    cleaned = []
    seen: set[int] = set()
    for item in tics:
        try:
            tic = int(item)
        except (TypeError, ValueError):
            continue
        if tic > 0 and tic not in seen:
            seen.add(tic)
            cleaned.append(tic)
    if not cleaned:
        return
    row = conn.execute(
        "SELECT tics_json, n_tics FROM unit_catalog WHERE sector=? AND unit=?",
        (int(sector), int(unit)),
    ).fetchone()
    if row is not None and int(row["n_tics"] or 0) >= len(cleaned):
        return
    conn.execute(
        """INSERT INTO unit_catalog (
               sector, unit, project, assignment_id, n_tics, tics_json, started_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(sector, unit) DO UPDATE SET
               project=excluded.project,
               assignment_id=COALESCE(excluded.assignment_id, unit_catalog.assignment_id),
               n_tics=excluded.n_tics,
               tics_json=excluded.tics_json,
               started_at=COALESCE(unit_catalog.started_at, excluded.started_at)""",
        (
            int(sector),
            int(unit),
            str(project or "exoplanets"),
            int(assignment_id) if assignment_id is not None else None,
            len(cleaned),
            json.dumps(cleaned, separators=(",", ":")),
            float(started_at if started_at is not None else time.time()),
        ),
    )


def mark_unit_finished(conn: sqlite3.Connection, sector: int, unit: int) -> None:
    conn.execute(
        "UPDATE unit_catalog SET finished_at=? WHERE sector=? AND unit=? AND finished_at IS NULL",
        (time.time(), int(sector), int(unit)),
    )


def _rows_from_holdings(cache: Path) -> list[dict]:
    from .holdings import load_units

    return load_units(cache)


def catalog(cache: Path, snap: dict | None = None) -> dict:
    """Sectors and units this computer has seen, newest sector first."""
    cache = Path(cache)
    snap = snap or {}
    batch = snap.get("batch") if isinstance(snap.get("batch"), dict) else {}
    live_sector = int(batch.get("sector") or 0)
    live_unit = int(batch.get("unit_id") or 0)
    sectors: dict[int, dict] = {}

    path = cache / "dashboard.sqlite"
    if path.is_file():
        conn = sqlite3.connect(str(path), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            migrate(conn)
            for row in conn.execute(
                """SELECT sector, unit, project, n_tics, started_at, finished_at
                   FROM unit_catalog ORDER BY sector DESC, unit ASC"""
            ):
                sec = int(row["sector"])
                bucket = sectors.setdefault(sec, {"sector": sec, "units": []})
                bucket["units"].append({
                    "unit": int(row["unit"]),
                    "project": row["project"],
                    "n_tics": int(row["n_tics"] or 0),
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "live": sec == live_sector and int(row["unit"]) == live_unit,
                })
            for row in conn.execute(
                """SELECT DISTINCT sector, unit FROM stars ORDER BY sector DESC, unit ASC"""
            ):
                sec = int(row["sector"])
                unit = int(row["unit"])
                bucket = sectors.setdefault(sec, {"sector": sec, "units": []})
                if not any(int(u["unit"]) == unit for u in bucket["units"]):
                    bucket["units"].append({
                        "unit": unit,
                        "project": "exoplanets",
                        "n_tics": 0,
                        "started_at": None,
                        "finished_at": None,
                        "live": sec == live_sector and unit == live_unit,
                    })
        finally:
            conn.close()

    for row in _rows_from_holdings(cache):
        try:
            sec = int(row.get("sector") or 0)
            unit = int(row.get("unit_id") or 0)
        except (TypeError, ValueError):
            continue
        if sec < 1 or unit < 1:
            continue
        bucket = sectors.setdefault(sec, {"sector": sec, "units": []})
        if not any(int(u["unit"]) == unit for u in bucket["units"]):
            try:
                n = int(row.get("n_tics") or len(row.get("tics") or []))
            except (TypeError, ValueError):
                n = 0
            bucket["units"].append({
                "unit": unit,
                "project": str(row.get("project") or "exoplanets"),
                "n_tics": n,
                "started_at": None,
                "finished_at": None,
                "live": sec == live_sector and unit == live_unit,
            })

    if live_sector and live_unit:
        bucket = sectors.setdefault(live_sector, {"sector": live_sector, "units": []})
        if not any(int(u["unit"]) == live_unit for u in bucket["units"]):
            try:
                n = int(batch.get("stars_total") or 0)
            except (TypeError, ValueError):
                n = 0
            bucket["units"].append({
                "unit": live_unit,
                "project": str(batch.get("project") or "exoplanets"),
                "n_tics": n,
                "started_at": batch.get("started_at"),
                "finished_at": None,
                "live": True,
            })
        else:
            for u in bucket["units"]:
                if int(u["unit"]) == live_unit:
                    u["live"] = True

    out = list(sectors.values())
    for item in out:
        item["units"].sort(key=lambda u: int(u["unit"]))
    out.sort(key=lambda s: int(s["sector"]), reverse=True)
    default = {"sector": live_sector, "unit": live_unit} if live_sector and live_unit else None
    return {"sectors": out, "default": default, "unit_size": UNIT_SIZE}


def _unit_tics(conn: sqlite3.Connection, cache: Path, sector: int, unit: int) -> list[int]:
    row = conn.execute(
        "SELECT tics_json FROM unit_catalog WHERE sector=? AND unit=?",
        (int(sector), int(unit)),
    ).fetchone()
    if row is not None:
        try:
            payload = json.loads(row["tics_json"])
            if isinstance(payload, list) and payload:
                return [int(t) for t in payload]
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    found = _tics_from_sources(cache, sector, unit)
    if found:
        register_unit(
            conn,
            sector=sector,
            unit=unit,
            project="exoplanets",
            assignment_id=None,
            tics=found,
        )
        return found
    rows = conn.execute(
        "SELECT tic FROM stars WHERE sector=? AND unit=? ORDER BY finished_at ASC, tic ASC",
        (int(sector), int(unit)),
    ).fetchall()
    if rows:
        return [int(r["tic"]) for r in rows]
    return []


def _star_row_state(row: sqlite3.Row) -> str:
    verdict = str(row["verdict"] or "").lower()
    if verdict == "error" or (row["record_json"] and '"error"' in str(row["record_json"])[:120]):
        return "error"
    if int(row["candidate"] or 0):
        return "candidate"
    sde = row["sde"]
    snr = row["snr"]
    try:
        sde_f = float(sde) if sde is not None else None
        snr_f = float(snr) if snr is not None else None
    except (TypeError, ValueError):
        sde_f = snr_f = None
    if sde_f is not None and math.isfinite(sde_f) and sde_f >= 6:
        return "signal"
    if snr_f is not None and math.isfinite(snr_f) and snr_f >= 7:
        return "signal"
    return "quiet"


def _live_overlay(batch: dict | None, processing: dict | None) -> dict:
    stage = str((batch or {}).get("stage") or "").lower()
    kind = str((batch or {}).get("progress_kind") or "stars")
    done = int((batch or {}).get("stars_done") or 0)
    return {
        "stage": stage,
        "progress_kind": kind,
        "stars_done": done,
        "processing_tic": int(processing["tic"]) if isinstance(processing, dict) and processing.get("tic") else None,
        "downloading": stage in {"claimed", "downloading"},
        "searching": stage in {"downloaded", "searching", "searched", "filtering", "filtered"},
    }


def _tic_state(
    tic: int,
    star: sqlite3.Row | None,
    *,
    live: bool,
    overlay: dict,
    ord_index: int,
    total: int,
) -> dict:
    if star is not None:
        state = _star_row_state(star)
        sde = star["sde"]
        snr = star["snr"]
        try:
            sde_v = float(sde) if sde is not None else None
        except (TypeError, ValueError):
            sde_v = None
        if not math.isfinite(sde_v or float("nan")):
            sde_v = None
        depth = None
        raw_json = star["record_json"]
        if raw_json:
            try:
                payload = json.loads(raw_json)
                if isinstance(payload, dict):
                    depth = payload.get("depth")
            except json.JSONDecodeError:
                depth = None
        return {
            "tic": int(tic),
            "state": state,
            "period": star["period"],
            "depth": depth,
            "sde": sde_v,
            "snr": star["snr"],
            "at": star["finished_at"],
            "candidate": bool(star["candidate"]),
        }
    if not live:
        return {"tic": int(tic), "state": "queued", "period": None, "sde": None, "snr": None, "at": None, "candidate": False}
    proc = overlay.get("processing_tic")
    if overlay.get("searching") and proc == tic:
        return {"tic": int(tic), "state": "searching", "period": None, "sde": None, "snr": None, "at": None, "candidate": False}
    if overlay.get("downloading") and overlay.get("progress_kind") == "files":
        if ord_index < int(overlay.get("stars_done") or 0):
            return {"tic": int(tic), "state": "queued", "period": None, "sde": None, "snr": None, "at": None, "candidate": False}
        return {"tic": int(tic), "state": "downloading", "period": None, "sde": None, "snr": None, "at": None, "candidate": False}
    if overlay.get("downloading"):
        return {"tic": int(tic), "state": "downloading", "period": None, "sde": None, "snr": None, "at": None, "candidate": False}
    if overlay.get("searching") and ord_index >= int(overlay.get("stars_done") or 0):
        return {"tic": int(tic), "state": "queued", "period": None, "sde": None, "snr": None, "at": None, "candidate": False}
    return {"tic": int(tic), "state": "queued", "period": None, "sde": None, "snr": None, "at": None, "candidate": False}


def _graphs(stars: list[dict], started_at: float | None) -> dict:
    sde_vals = []
    periods = []
    depth_period: list[list[float]] = []
    timeline: list[list[float]] = []
    t0 = started_at if started_at else (stars[0]["at"] if stars and stars[0].get("at") else time.time())
    count = 0
    for row in sorted(stars, key=lambda r: float(r.get("at") or 0)):
        if row.get("state") == "error":
            continue
        count += 1
        at = row.get("at")
        if at is not None:
            timeline.append([float(at) - t0, count])
        try:
            sde = float(row.get("sde")) if row.get("sde") is not None else None
        except (TypeError, ValueError):
            sde = None
        if sde is not None and math.isfinite(sde):
            sde_vals.append(sde)
        try:
            period = float(row.get("period")) if row.get("period") is not None else None
        except (TypeError, ValueError):
            period = None
        if period is not None and math.isfinite(period) and period > 0:
            periods.append(period)
            try:
                depth = float(row.get("depth")) if row.get("depth") is not None else None
            except (TypeError, ValueError):
                depth = None
            if depth is not None and math.isfinite(depth):
                depth_period.append([period, depth])
    return {
        "sde_hist": _histogram(sde_vals, 0.0, 30.0, 24),
        "period_hist": _histogram(periods, 0.5, 20.0, 24, log=True),
        "depth_period": depth_period[:800],
        "processed_timeline": timeline,
        "t0": t0,
    }


def _histogram(values: list[float], lo: float, hi: float, bins: int, *, log: bool = False) -> dict:
    if not values:
        return {"edges": [], "counts": []}
    counts = [0] * bins
    if log:
        edges = [lo * ((hi / lo) ** (i / bins)) for i in range(bins + 1)]
    else:
        edges = [lo + (hi - lo) * i / bins for i in range(bins + 1)]
    for v in values:
        if log:
            if v <= lo or v >= hi:
                continue
            x = math.log(v / lo) / math.log(hi / lo)
        else:
            if v < lo or v >= hi:
                continue
            x = (v - lo) / (hi - lo)
        idx = min(bins - 1, max(0, int(x * bins)))
        counts[idx] += 1
    return {"edges": [round(e, 4) for e in edges], "counts": counts}


def replay_events(stars: list[dict]) -> list[dict]:
    rows = [r for r in stars if r.get("at") is not None and r.get("state") not in {"queued", "downloading", "searching"}]
    rows.sort(key=lambda r: float(r["at"]))
    return [
        {
            "tic": int(r["tic"]),
            "state": r["state"],
            "at": float(r["at"]),
            "period": r.get("period"),
            "sde": r.get("sde"),
        }
        for r in rows
    ]


def unit_view(cache: Path, sector: int, unit: int, *, snap: dict | None = None) -> dict:
    """Hex grid, graphs, and replay timeline for one unit."""
    cache = Path(cache)
    snap = snap or {}
    batch = snap.get("batch") if isinstance(snap.get("batch"), dict) else {}
    processing = snap.get("processing") if isinstance(snap.get("processing"), dict) else None
    live = (
        int(batch.get("sector") or 0) == int(sector)
        and int(batch.get("unit_id") or 0) == int(unit)
        and bool(batch.get("active"))
    )
    path = cache / "dashboard.sqlite"
    if not path.is_file():
        return {"sector": sector, "unit": unit, "tics": [], "live": live, "graphs": {}, "replay": [], "n_tics": 0}
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        migrate(conn)
        tics = _unit_tics(conn, cache, sector, unit)
        star_rows = conn.execute(
            "SELECT * FROM stars WHERE sector=? AND unit=?",
            (int(sector), int(unit)),
        ).fetchall()
        by_tic = {int(r["tic"]): r for r in star_rows}
        overlay = _live_overlay(batch if live else None, processing if live else None)
        cells: list[dict] = []
        for i, tic in enumerate(tics):
            cells.append(_tic_state(tic, by_tic.get(tic), live=live, overlay=overlay, ord_index=i, total=len(tics)))
        started = conn.execute(
            "SELECT started_at FROM unit_catalog WHERE sector=? AND unit=?",
            (int(sector), int(unit)),
        ).fetchone()
        started_at = None
        if started is not None and started["started_at"] is not None:
            started_at = float(started["started_at"])
        elif batch.get("started_at") and live:
            started_at = float(batch["started_at"])
        graphs = _graphs(cells, started_at)
        replay = replay_events(cells)
        stars_total = max(len(tics), int(batch.get("stars_total") or 0) if live else 0, 1)
        stored_done = sum(
            1 for c in cells if c.get("at") is not None or c.get("state") in {"quiet", "signal", "candidate", "error"}
        )
        if live:
            try:
                worker_done = int(batch.get("stars_done") or 0)
            except (TypeError, ValueError):
                worker_done = 0
            stars_done = max(stored_done, worker_done)
            finished = False
        else:
            stars_done = stars_total if stars_total > 1 else max(stored_done, len(replay))
            finished = True
        percent = min(100.0, round(100.0 * stars_done / stars_total, 1))
        return {
            "sector": int(sector),
            "unit": int(unit),
            "project": str(batch.get("project") or "exoplanets"),
            "n_tics": len(tics),
            "unit_size": UNIT_SIZE,
            "tics": cells,
            "live": live,
            "batch_stage": str(batch.get("stage") or "") if live else None,
            "graphs": graphs,
            "replay": replay,
            "progress": {
                "stars_done": int(stars_done),
                "stars_total": int(stars_total),
                "percent": percent,
                "finished": finished,
            },
            "updated": time.time(),
        }
    finally:
        conn.close()