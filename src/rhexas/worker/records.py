"""Per-star rows for the local worker dashboard.

The worker sqlite keeps one row per finished star, keyed by sector, unit, and
tic. A candidate keeps the curves the page draws. Every other searched star
keeps a summary (tic, sky position, period, SNR, SDE, verdict). Nothing here
is sent to the coordinator, and a half-finished star is only a pointer.
"""
from __future__ import annotations

import math

# Full curve JSON for non-candidates. Candidates are not counted against this.
RECORD_BUDGET_BYTES = 48 * 1024 * 1024
# Summary rows, not counting candidates. Oldest non-candidates go first.
SUMMARY_CAP = 200_000
_PX = 21.0  # arcsec per TESS pixel

_STARS = """
CREATE TABLE IF NOT EXISTS stars (
    sector INTEGER NOT NULL,
    unit INTEGER NOT NULL,
    tic INTEGER NOT NULL,
    ra REAL,
    dec REAL,
    tmag REAL,
    period REAL,
    snr REAL,
    sde REAL,
    verdict TEXT,
    candidate INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    finished_at REAL NOT NULL,
    record_json TEXT,
    PRIMARY KEY (sector, unit, tic)
);
"""


def migrate(conn) -> None:
    """Create the stars table and any later columns. Safe to run again."""
    conn.executescript(_STARS)
    conn.execute("CREATE INDEX IF NOT EXISTS stars_by_sector ON stars(sector, finished_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS stars_by_candidate ON stars(candidate, finished_at)")
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(candidates)")}
    if columns and "status" not in columns:
        conn.execute("ALTER TABLE candidates ADD COLUMN status TEXT")
    conn.execute("INSERT OR IGNORE INTO schema_migrations (name) VALUES ('stars-v1')")
    from .vetting.store import migrate as migrate_vetting
    from .batch_index import migrate as migrate_batch_index
    migrate_vetting(conn)
    migrate_batch_index(conn)


def table_ready(conn) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stars'"
    ).fetchone()
    return row is not None


def centroid_vs_phase(time_values, col, row, period, t0, duration, bins: int = 24) -> dict | None:
    """Binned centroid shift in arcsec, plus in-transit versus out-of-transit significance.

    Returns None when the arrays or the ephemeris cannot support a curve.
    A missing scatter makes sigma null rather than a made-up zero.
    """
    import numpy as np

    try:
        period_f = float(period)
        t0_f = float(t0)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(period_f) or not math.isfinite(t0_f) or period_f <= 0:
        return None
    try:
        dur = float(duration)
    except (TypeError, ValueError):
        dur = 0.1
    if not math.isfinite(dur) or dur <= 0:
        dur = 0.1
    t = np.asarray(time_values, dtype=float)
    c1 = np.asarray(col, dtype=float)
    c2 = np.asarray(row, dtype=float)
    if t.shape != c1.shape or t.shape != c2.shape or t.size < 8:
        return None
    mask = np.isfinite(t) & np.isfinite(c1) & np.isfinite(c2)
    t, c1, c2 = t[mask], c1[mask], c2[mask]
    if t.size < 8:
        return None
    phase = np.mod(t - t0_f, period_f) / period_f
    phase = np.where(phase > 0.5, phase - 1.0, phase)
    half_in = 0.4 * dur / period_f
    oot_lo = 0.75 * dur / period_f
    oot_hi = min(0.5, max(2.5 * dur / period_f, oot_lo + 0.02))
    intr = np.abs(phase) < half_in
    oot = (np.abs(phase) > oot_lo) & (np.abs(phase) < oot_hi)
    sigma = None
    shift = None
    if int(intr.sum()) >= 2 and int(oot.sum()) >= 4:
        base1 = float(np.nanmedian(c1[oot]))
        base2 = float(np.nanmedian(c2[oot]))
        d1 = float(np.nanmedian(c1[intr]) - base1)
        d2 = float(np.nanmedian(c2[intr]) - base2)
        shift = float(np.hypot(d1, d2) * _PX)
        err = float(np.hypot(np.nanstd(c1[oot]), np.nanstd(c2[oot])) / math.sqrt(max(int(intr.sum()), 1)) * _PX)
        if err > 0 and math.isfinite(err) and math.isfinite(shift):
            sigma = shift / err
            if not math.isfinite(sigma):
                sigma = None
    else:
        base1 = float(np.nanmedian(c1))
        base2 = float(np.nanmedian(c2))
    if not math.isfinite(base1) or not math.isfinite(base2):
        return None
    hours = phase * period_f * 24.0
    span = min(0.5 * period_f, max(4.0 * dur, 0.02 * period_f))
    window = np.abs(phase) < (span / period_f)
    packed_col = _bin_hours(hours[window], (c1[window] - base1) * _PX, bins)
    packed_row = _bin_hours(hours[window], (c2[window] - base2) * _PX, bins)
    if packed_col is None or packed_row is None:
        return None
    return {
        "hours": packed_col[0],
        "column": packed_col[1],
        "row": packed_row[1],
        "sigma": None if sigma is None else round(float(sigma), 3),
        "shift_arcsec": None if shift is None else round(float(shift), 4),
    }


def _bin_hours(hours, values, bins: int):
    import numpy as np

    x = np.asarray(hours, dtype=float)
    y = np.asarray(values, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    x, y = x[mask], y[mask]
    if x.size < 3:
        return None
    order = np.argsort(x)
    x, y = x[order], y[order]
    edges = np.linspace(float(x[0]), float(x[-1]), bins + 1)
    if edges[-1] == edges[0]:
        return None
    idx = np.digitize(x, edges) - 1
    xb: list[float] = []
    yb: list[float] = []
    for i in range(bins):
        sel = y[idx == i]
        if sel.size == 0:
            continue
        xb.append(round(float((edges[i] + edges[i + 1]) / 2), 3))
        yb.append(round(float(np.median(sel)), 4))
    if len(xb) < 3:
        return None
    return xb, yb


def _pair(left, right) -> list | None:
    if not isinstance(left, list) or not isinstance(right, list):
        return None
    if len(left) < 2 or len(left) != len(right):
        return None
    return [left, right]


def _fold_hours(phase, folded, period) -> list | None:
    if not isinstance(phase, list) or not isinstance(folded, list):
        return None
    if len(phase) < 3 or len(phase) != len(folded):
        return None
    try:
        period_f = float(period)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(period_f) or period_f <= 0:
        return None
    hours = [round(float(p) * period_f * 24.0, 3) for p in phase]
    return [hours, folded, hours, folded]


def _centroid_array(payload) -> list | None:
    if not isinstance(payload, dict):
        return None
    hours = payload.get("hours")
    col = payload.get("column")
    row = payload.get("row")
    if not isinstance(hours, list) or not isinstance(col, list) or not isinstance(row, list):
        return None
    if len(hours) < 3 or len(hours) != len(col) or len(hours) != len(row):
        return None
    return [hours, col, row]


def build_record(spec: dict, row: dict, times, flux, phase, folded, curves: dict, *, passed: bool, at: float) -> dict:
    """One display record. Missing pieces stay null and are named in ``missing``."""
    pgram = None
    pgram_p = row.get("_pgram_p")
    pgram_w = row.get("_pgram_w")
    if isinstance(pgram_p, list) and isinstance(pgram_w, list) and len(pgram_p) == len(pgram_w) and len(pgram_p) >= 2:
        pgram = [pgram_p, pgram_w]
    aperture = row.get("_aperture") if isinstance(row.get("_aperture"), list) and row.get("_aperture") else None
    centroid_payload = row.get("_centroid") if isinstance(row.get("_centroid"), dict) else None
    centroid = _centroid_array(centroid_payload)
    error = row.get("error")
    status = "error" if error else "done"
    verdict = "error" if error else ("NEW" if passed else "searched")
    ra = _finite(row.get("ra"))
    dec = _finite(row.get("dec"))
    odd = curves.get("odd") if isinstance(curves, dict) else None
    even = curves.get("even") if isinstance(curves, dict) else None
    secondary = curves.get("secondary") if isinstance(curves, dict) else None
    light = _pair(times, flux)
    fold = _fold_hours(phase, folded, row.get("period"))
    missing = []
    if ra is None or dec is None:
        missing.append("sky")
    if aperture is None:
        missing.append("aperture")
    if light is None:
        missing.append("light_curve")
    if pgram is None:
        missing.append("periodogram")
    if fold is None:
        missing.append("fold")
    if not odd or not even:
        missing.append("odd_even")
    if not secondary:
        missing.append("secondary")
    if centroid is None:
        missing.append("centroid")
    return {
        "tic": _tic(row),
        "sector": int(spec.get("sector") or 0),
        "unit": int(spec.get("unit_id") or 0),
        "project": str(spec.get("project") or "exoplanets"),
        "file": str(row.get("file") or ""),
        "status": status,
        "verdict": verdict,
        "candidate": bool(passed),
        "unverified": True,
        "error": str(error)[:200] if error else None,
        "period": _finite(row.get("period")),
        "t0": _finite(row.get("t0")),
        "duration": _finite(row.get("duration")),
        "depth": _finite(row.get("depth")),
        "snr": _finite(row.get("snr")),
        "sde": _finite(row.get("sde")),
        "n_transits": _finite(row.get("n_transits")),
        "odd_even_sigma": _finite(row.get("odd_even_sigma")),
        "sec_sigma": _finite(row.get("sec_sigma")),
        "ra": ra,
        "dec": dec,
        "tmag": _finite(row.get("tmag")),
        "aperture": aperture,
        "time": times if light else [],
        "flux": flux if light else [],
        "raw": light,
        "phase": phase if fold else [],
        "folded": folded if fold else [],
        "fold": fold,
        "pgram": pgram,
        "odd": odd,
        "even": even,
        "secondary": secondary,
        "centroid": centroid,
        "centroid_sigma": None if not isinstance(centroid_payload, dict) else centroid_payload.get("sigma"),
        "centroid_shift_arcsec": None if not isinstance(centroid_payload, dict) else centroid_payload.get("shift_arcsec"),
        "at": at,
        "missing": missing,
        "available": True,
        "summary": False,
    }


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _tic(row) -> int | None:
    try:
        return int(row.get("tic"))
    except (TypeError, ValueError):
        return None


def upsert_star(conn, record: dict, encode) -> dict:
    """Insert or replace one finished star. The version goes up by one each write."""
    tic = record.get("tic")
    if tic is None:
        return record
    sector = int(record.get("sector") or 0)
    unit = int(record.get("unit") or 0)
    previous = conn.execute(
        "SELECT version FROM stars WHERE sector=? AND unit=? AND tic=?",
        (sector, unit, int(tic)),
    ).fetchone()
    version = int(previous["version"]) + 1 if previous is not None else 1
    record = {**record, "version": version}
    encoded = encode(record)
    conn.execute(
        """INSERT INTO stars (
               sector, unit, tic, ra, dec, tmag, period, snr, sde, verdict,
               candidate, version, finished_at, record_json
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(sector, unit, tic) DO UPDATE SET
               ra=excluded.ra, dec=excluded.dec, tmag=excluded.tmag,
               period=excluded.period, snr=excluded.snr, sde=excluded.sde,
               verdict=excluded.verdict, candidate=excluded.candidate,
               version=excluded.version, finished_at=excluded.finished_at,
               record_json=excluded.record_json""",
        (
            sector, unit, int(tic),
            record.get("ra"), record.get("dec"), record.get("tmag"),
            record.get("period"), record.get("snr"), record.get("sde"),
            record.get("verdict"),
            1 if record.get("candidate") else 0,
            version, float(record.get("at") or 0),
            encoded,
        ),
    )
    return record


def retain(conn, *, budget: int | None = None, summary_cap: int | None = None) -> None:
    """Drop curve JSON, then summary rows, for the oldest non-candidates.

    The newest finished star and every candidate keep their curves.
    """
    if not table_ready(conn):
        return
    limit = RECORD_BUDGET_BYTES if budget is None else int(budget)
    cap = SUMMARY_CAP if summary_cap is None else int(summary_cap)
    newest = conn.execute(
        "SELECT sector, unit, tic FROM stars ORDER BY finished_at DESC LIMIT 1"
    ).fetchone()
    rows = conn.execute(
        """SELECT sector, unit, tic, LENGTH(record_json) AS n
           FROM stars
           WHERE candidate=0 AND record_json IS NOT NULL
           ORDER BY finished_at ASC"""
    ).fetchall()
    total = sum(int(row["n"] or 0) for row in rows)
    for row in rows:
        if total <= limit:
            break
        if newest is not None and (row["sector"], row["unit"], row["tic"]) == (
            newest["sector"], newest["unit"], newest["tic"],
        ):
            continue
        conn.execute(
            "UPDATE stars SET record_json=NULL WHERE sector=? AND unit=? AND tic=?",
            (row["sector"], row["unit"], row["tic"]),
        )
        total -= int(row["n"] or 0)
    extra = conn.execute("SELECT COUNT(*) AS n FROM stars WHERE candidate=0").fetchone()["n"] - cap
    if extra > 0:
        conn.execute(
            """DELETE FROM stars WHERE rowid IN (
                   SELECT rowid FROM stars WHERE candidate=0
                   ORDER BY finished_at ASC LIMIT ?
               )""",
            (int(extra),),
        )


def apply_candidate_list(conn, sector: int, unit: int, rows: list[dict]) -> None:
    """Mark the filter's official candidates. Other stars in the unit stay summaries."""
    if not table_ready(conn):
        return
    keep: list[int] = []
    for row in rows:
        tic = _tic(row)
        if tic is None:
            continue
        keep.append(tic)
        status = str(row.get("status") or "NEW")
        conn.execute(
            """UPDATE stars SET candidate=1, verdict=?
               WHERE sector=? AND unit=? AND tic=?""",
            (status, int(sector), int(unit), tic),
        )
    if keep:
        marks = ",".join("?" * len(keep))
        conn.execute(
            f"""UPDATE stars
                SET candidate=0,
                    verdict=CASE
                        WHEN verdict IN ('NEW', 'known_TOI', 'known_confirmed') THEN 'searched'
                        ELSE verdict
                    END
                WHERE sector=? AND unit=? AND candidate=1 AND tic NOT IN ({marks})""",
            (int(sector), int(unit), *keep),
        )
    else:
        conn.execute(
            """UPDATE stars
               SET candidate=0,
                   verdict=CASE
                       WHEN verdict IN ('NEW', 'known_TOI', 'known_confirmed') THEN 'searched'
                       ELSE verdict
                   END
               WHERE sector=? AND unit=? AND candidate=1""",
            (int(sector), int(unit)),
        )


def clear_unit_rows(conn, sector: int, unit: int) -> int:
    """Remove this unit's searched stars. Candidate rows stay on the machine."""
    if not table_ready(conn):
        return 0
    cur = conn.execute(
        "DELETE FROM stars WHERE sector=? AND unit=? AND candidate=0",
        (int(sector), int(unit)),
    )
    return int(cur.rowcount or 0)


def _decode(raw):
    if not raw:
        return None
    import json
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def fetch_star(conn, tic: int, sector: int | None = None, unit: int | None = None) -> dict | None:
    """The full record when it is still stored, otherwise the summary."""
    if not table_ready(conn):
        return None
    row = None
    if sector is not None and unit is not None:
        row = conn.execute(
            "SELECT * FROM stars WHERE sector=? AND unit=? AND tic=?",
            (int(sector), int(unit), int(tic)),
        ).fetchone()
    if row is None and sector is not None:
        row = conn.execute(
            """SELECT * FROM stars WHERE sector=? AND tic=?
               ORDER BY finished_at DESC LIMIT 1""",
            (int(sector), int(tic)),
        ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM stars WHERE tic=? ORDER BY finished_at DESC LIMIT 1",
            (int(tic),),
        ).fetchone()
    if row is None:
        return None
    return public_from_row(row)


def public_from_row(row) -> dict:
    """API shape. A summary names every curve it does not have."""
    stored = _decode(row["record_json"])
    if isinstance(stored, dict):
        stored = dict(stored)
        stored["version"] = int(row["version"])
        stored["sector"] = int(row["sector"])
        stored["unit"] = int(row["unit"])
        stored["candidate"] = bool(row["candidate"])
        stored["verdict"] = row["verdict"] or stored.get("verdict")
        stored["available"] = True
        stored["summary"] = False
        stored["unverified"] = True
        stored["missing"] = list(stored.get("missing") or [])
        return stored
    missing = ["aperture", "light_curve", "periodogram", "fold", "odd_even", "secondary", "centroid"]
    if row["ra"] is None or row["dec"] is None:
        missing.append("sky")
    return {
        "tic": int(row["tic"]),
        "sector": int(row["sector"]),
        "unit": int(row["unit"]),
        "version": int(row["version"]),
        "ra": row["ra"],
        "dec": row["dec"],
        "tmag": row["tmag"],
        "period": row["period"],
        "snr": row["snr"],
        "sde": row["sde"],
        "verdict": row["verdict"],
        "candidate": bool(row["candidate"]),
        "status": row["verdict"] or "searched",
        "at": row["finished_at"],
        "available": True,
        "summary": True,
        "unverified": True,
        "aperture": None,
        "time": None,
        "flux": None,
        "raw": None,
        "phase": None,
        "folded": None,
        "fold": None,
        "pgram": None,
        "odd": None,
        "even": None,
        "secondary": None,
        "centroid": None,
        "centroid_sigma": None,
        "centroid_shift_arcsec": None,
        "odd_even_sigma": None,
        "sec_sigma": None,
        "depth": None,
        "duration": None,
        "n_transits": None,
        "missing": missing,
    }


def missing_star(tic: int) -> dict:
    return {
        "tic": int(tic),
        "available": False,
        "summary": False,
        "unverified": True,
        "aperture": None,
        "ra": None,
        "dec": None,
        "time": None,
        "flux": None,
        "raw": None,
        "pgram": None,
        "fold": None,
        "odd": None,
        "even": None,
        "secondary": None,
        "centroid": None,
        "odd_even_sigma": None,
        "sec_sigma": None,
        "centroid_sigma": None,
        "missing": ["record"],
    }


def latest_display(conn, *, stage: str, now: float) -> dict | None:
    """Last finished star, or the next candidate while the batch is done."""
    if not table_ready(conn):
        return None
    if stage == "Done":
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM stars WHERE candidate=1 AND record_json IS NOT NULL"
        ).fetchone()["n"]
        if count:
            idx = int(now // 8) % int(count)
            row = conn.execute(
                """SELECT * FROM stars
                   WHERE candidate=1 AND record_json IS NOT NULL
                   ORDER BY finished_at ASC
                   LIMIT 1 OFFSET ?""",
                (idx,),
            ).fetchone()
            if row is not None:
                return public_from_row(row)
    row = conn.execute(
        """SELECT * FROM stars
           WHERE record_json IS NOT NULL
           ORDER BY finished_at DESC LIMIT 1"""
    ).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM stars ORDER BY finished_at DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return public_from_row(row)


def pointer(record: dict | None) -> dict | None:
    if not isinstance(record, dict) or record.get("tic") is None:
        return None
    try:
        tic = int(record["tic"])
        version = int(record.get("version") or 0)
    except (TypeError, ValueError):
        return None
    sector = record.get("sector")
    unit = record.get("unit")
    if unit is None:
        unit = record.get("unit_id")
    return {"tic": tic, "version": version, "sector": sector, "unit": unit}


def candidate_stats(conn) -> dict:
    """Uncapped counts. NEW is the worker's unverified list. Known TOIs are separate."""
    empty = {"rows": 0, "new": 0, "known": 0}
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS rows,
                      SUM(CASE WHEN COALESCE(status, 'NEW') = 'NEW' THEN 1 ELSE 0 END) AS new_n,
                      SUM(CASE WHEN status IN ('known_TOI', 'known_confirmed') THEN 1 ELSE 0 END) AS known_n
               FROM candidates"""
        ).fetchone()
    except Exception:
        return empty
    if row is None:
        return empty
    return {
        "rows": int(row["rows"] or 0),
        "new": int(row["new_n"] or 0),
        "known": int(row["known_n"] or 0),
    }


def store_stats(conn) -> dict:
    if not table_ready(conn):
        return {
            "stars": 0, "full": 0, "summaries": 0, "candidates": 0,
            "bytes": 0, "limit_bytes": RECORD_BUDGET_BYTES, "summary_limit": SUMMARY_CAP,
        }
    row = conn.execute(
        """SELECT COUNT(*) AS stars,
                  SUM(CASE WHEN record_json IS NOT NULL THEN 1 ELSE 0 END) AS full_n,
                  SUM(CASE WHEN record_json IS NULL THEN 1 ELSE 0 END) AS summaries,
                  SUM(CASE WHEN candidate=1 THEN 1 ELSE 0 END) AS candidates,
                  COALESCE(SUM(CASE WHEN candidate=0 THEN LENGTH(record_json) ELSE 0 END), 0) AS bytes
           FROM stars"""
    ).fetchone()
    return {
        "stars": int(row["stars"] or 0),
        "full": int(row["full_n"] or 0),
        "summaries": int(row["summaries"] or 0),
        "candidates": int(row["candidates"] or 0),
        "bytes": int(row["bytes"] or 0),
        "limit_bytes": RECORD_BUDGET_BYTES,
        "summary_limit": SUMMARY_CAP,
    }


def sky_payload(conn, sector: int | None) -> dict:
    """Summaries for one sector, or every stored sector. No JSON blob is rewritten."""
    if not table_ready(conn):
        return {"scope": "sector" if sector else "all", "sector": sector, "points": [], "bounds": None, "count": 0}
    if sector:
        rows = conn.execute(
            """SELECT tic, ra, dec, tmag, candidate, verdict, sector
               FROM stars
               WHERE sector=? AND ra IS NOT NULL AND dec IS NOT NULL
               ORDER BY finished_at ASC""",
            (int(sector),),
        ).fetchall()
        scope = "sector"
    else:
        rows = conn.execute(
            """SELECT tic, ra, dec, tmag, candidate, verdict, sector
               FROM stars
               WHERE ra IS NOT NULL AND dec IS NOT NULL
               ORDER BY finished_at ASC"""
        ).fetchall()
        scope = "all"
    points = []
    for row in rows:
        verdict = str(row["verdict"] or "")
        if verdict in ("known_TOI", "known_confirmed"):
            flag = 3
        elif row["candidate"]:
            flag = 2
        else:
            flag = 1
        mag = row["tmag"]
        points.append([
            int(row["tic"]),
            round(float(row["ra"]), 5),
            round(float(row["dec"]), 5),
            round(float(mag), 2) if mag is not None else None,
            flag,
        ])
    bounds = _bounds(points) if scope == "sector" else None
    return {"scope": scope, "sector": int(sector) if sector else None, "points": points, "bounds": bounds, "count": len(points)}


def sky_rev(conn) -> str:
    if not table_ready(conn):
        return "0:0"
    row = conn.execute(
        """SELECT COUNT(*) AS n, COALESCE(MAX(finished_at), 0) AS at
           FROM stars WHERE ra IS NOT NULL AND dec IS NOT NULL"""
    ).fetchone()
    return f"{int(row['n'] or 0)}:{int(float(row['at'] or 0) * 1000)}"


def _bounds(points: list) -> dict | None:
    if not points:
        return None
    ras = [float(p[1]) for p in points]
    decs = [float(p[2]) for p in points]
    wrapped = False
    if max(ras) - min(ras) > 180:
        wrapped = True
        ras = [r - 360 if r > 180 else r for r in ras]
    span_ra = max(ras) - min(ras)
    span_dec = max(decs) - min(decs)
    pad_ra = max(1.0, span_ra * 0.08 or 2.0)
    pad_dec = max(1.0, span_dec * 0.08 or 2.0)
    return {
        "ra0": round(min(ras) - pad_ra, 4),
        "ra1": round(max(ras) + pad_ra, 4),
        "dec0": round(max(-90.0, min(decs) - pad_dec), 4),
        "dec1": round(min(90.0, max(decs) + pad_dec), 4),
        "wrapped": wrapped,
    }
