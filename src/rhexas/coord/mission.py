"""Mission-wide sector progress, totals, and timeline for Phase 3."""
from __future__ import annotations

from datetime import datetime

from .tess_pointing import sky_footprints


def _sector_state(row: dict) -> str:
    units = int(row.get("units") or 0)
    agreed = int(row.get("agreed") or 0)
    if units <= 0:
        return "pending"
    if agreed >= units:
        return "done"
    in_progress = int(row.get("units_in_progress") or 0)
    submitted = int(row.get("units_submitted") or 0)
    if in_progress > 0 or submitted > 0 or agreed > 0:
        return "working"
    return "pending"


def mission_progress(store, project: str | None = None) -> dict:
    """Overall and per-sector progress. Denominator is enrolled units in the database."""
    if project is not None:
        project = store._require_project(project)
    snap = store.status(project)
    sectors_raw = snap.get("sectors") or []
    sectors = []
    for row in sectors_raw:
        units = int(row.get("units") or 0)
        agreed = int(row.get("agreed") or 0)
        sectors.append({
            "project": row.get("project"),
            "sector": int(row["sector"]),
            "state": _sector_state(row),
            "units_done": agreed,
            "units_total": units,
            "stars_done": int(row.get("tics_agreed") or 0),
            "stars_total": int(row.get("tics") or 0),
            "units_in_progress": int(row.get("units_in_progress") or 0),
            "units_submitted": int(row.get("units_submitted") or 0),
        })
    units_total = int(snap.get("units_total") or 0)
    units_agreed = int(snap.get("units_agreed") or 0)
    pct = (100.0 * units_agreed / units_total) if units_total else 0.0
    totals = _totals(store, project)
    timeline = _timeline(store, project)
    candidates = _candidate_dots(store, project)
    return {
        "project": snap.get("project") or (project or "all"),
        "scope": "enrolled_sectors",
        "scope_note": (
            "Percent and totals count only TESS sectors enrolled in the coordinator database. "
            "Unenrolled sectors are not in the denominator."
        ),
        "units_total": units_total,
        "units_verified": units_agreed,
        "stars_total": int(snap.get("tics_total") or 0),
        "stars_verified": int(snap.get("tics_agreed") or 0),
        "percent_units": round(pct, 2),
        "sectors": sectors,
        "totals": totals,
        "timeline": timeline,
        "sky": {
            "projection": "aitoff",
            "footprints": sky_footprints([row["sector"] for row in sectors]),
            "candidates": candidates,
        },
        "copies_required": snap.get("copies_required"),
    }


def compact_progress(store, project: str | None = None) -> dict:
    body = mission_progress(store, project)
    return {
        "project": body["project"],
        "scope": body["scope"],
        "scope_note": body["scope_note"],
        "units_total": body["units_total"],
        "units_verified": body["units_verified"],
        "stars_total": body["stars_total"],
        "stars_verified": body["stars_verified"],
        "percent_units": body["percent_units"],
        "candidates_new": body["totals"]["candidates_new"],
        "updated": store.clock().isoformat(timespec="seconds"),
    }


def _totals(store, project: str | None) -> dict:
    conn = store.conn
    where = ""
    args: list = []
    if project:
        where = "WHERE u.project=?"
        args.append(project)
    row = conn.execute(
        f"""SELECT
               SUM(CASE WHEN u.state='agreed' THEN u.n_tics ELSE 0 END) AS stars_searched,
               SUM(CASE WHEN u.state='agreed' THEN 1 ELSE 0 END) AS units_verified,
               COUNT(DISTINCT c.tic) AS candidates
            FROM units u
            LEFT JOIN candidates c ON c.unit_id = u.id
            {where}""",
        args,
    ).fetchone()
    signals_row = conn.execute(
        f"""SELECT COALESCE(SUM(r.n_candidates), 0) AS signals
            FROM results r
            JOIN units u ON u.id = r.unit_id
            WHERE r.verdict='agreed' {('AND u.project=?' if project else '')}""",
        args if project else [],
    ).fetchone()
    if project:
        new = conn.execute(
            """SELECT COUNT(*) AS n FROM candidates c
               JOIN units u ON u.id = c.unit_id
               WHERE c.status='NEW' AND u.project=?""",
            (project,),
        ).fetchone()["n"]
    else:
        new = conn.execute(
            "SELECT COUNT(*) AS n FROM candidates WHERE status='NEW'"
        ).fetchone()["n"]
    return {
        "stars_searched": int(row["stars_searched"] or 0),
        "units_verified": int(row["units_verified"] or 0),
        "signals": int(signals_row["signals"] or 0),
        "candidates": int(row["candidates"] or 0),
        "candidates_new": int(new or 0),
    }


def _timeline(store, project: str | None) -> list[dict]:
    where = "WHERE u.state='agreed' AND u.agreed_at IS NOT NULL"
    args: list = []
    if project:
        where += " AND u.project=?"
        args.append(project)
    rows = store.conn.execute(
        f"""SELECT substr(u.agreed_at, 1, 10) AS day, COUNT(*) AS units
            FROM units u
            {where}
            GROUP BY day
            ORDER BY day""",
        args,
    ).fetchall()
    return [{"day": row["day"], "units_verified": int(row["units"] or 0)} for row in rows]


def _candidate_dots(store, project: str | None) -> list[dict]:
    where = ""
    args: list = []
    if project:
        where = "WHERE u.project=?"
        args.append(project)
    rows = store.conn.execute(
        f"""SELECT c.tic, c.sector, c.period, c.snr
            FROM candidates c
            JOIN units u ON u.id = c.unit_id
            {where}
            ORDER BY c.id DESC
            LIMIT 500""",
        args,
    ).fetchall()
    dots = []
    from .tess_pointing import spacecraft_pointing
    for row in rows:
        tic = int(row["tic"])
        sector = int(row["sector"])
        center = spacecraft_pointing(sector)
        if center is None:
            continue
        ra, dec = center
        # Spread dots inside the sector field so overlaps are visible.
        jitter = ((tic % 97) - 48) * 0.08
        dec_j = ((tic % 53) - 26) * 0.05
        dots.append({
            "tic": tic,
            "sector": sector,
            "ra": ra + jitter,
            "dec": dec + dec_j,
            "period": row["period"],
            "snr": row["snr"],
        })
    return dots