"""Phase 5 science tools: progress, shortlist ranking, re-check flags, and exports."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime

from .discoveries_gallery import FILTER_LABELS, filter_bucket
from .vetting import PRIORITY_HELP, STATUS_LABELS

_DRAFT_BANNER = (
    "DRAFT — not submitted to ExoFOP or any outside service. "
    "For private review and CTOI prep only."
)
_CANDIDATE_DISCLAIMER = (
    "Transiting signal candidate — not a confirmed or newly discovered planet. "
    "A candidate is not treated as novel until cross-matched against SIMBAD, VSX, "
    "and ExoFOP TOI/CTOI lists and vetted for binaries, pulsators, blends, "
    "and instrument effects."
)

_PART_LABELS = {
    "signal": "SNR/SDE signal strength",
    "sectors": "multi-sector agreement",
    "radius": "small companion radius",
    "known": "known catalog object (demoted)",
    "catalog": "SIMBAD/VSX warning",
    "binary": "likely binary or false positive",
    "blend": "blend/contamination concern",
    "gaia": "Gaia non-single warning",
    "pending": "catalog checks still pending",
    "rejected": "marked rejected",
    "ctoi": "CTOI submitted",
}

_CHECK_ORDER = (
    ("simbad", "SIMBAD"),
    ("vsx", "VSX"),
    ("exofop", "ExoFOP TOI/CTOI"),
    ("nea", "NASA archive planet"),
    ("gaia", "Gaia non-single"),
    ("blend", "Blend/contamination"),
    ("planet", "Companion size/duration"),
    ("odd_even", "Odd vs even transits"),
    ("secondary", "Secondary eclipse"),
)


def progress_summary(groups: list[dict]) -> dict:
    """Bucket counts for the candidate list header."""
    counts = {key: 0 for key in FILTER_LABELS}
    reviewed = 0
    for row in groups:
        code = str(row.get("status_code") or "new")
        bucket = filter_bucket(code)
        counts[bucket] = counts.get(bucket, 0) + 1
        if code not in ("new", "pending"):
            reviewed += 1
        elif row.get("review_manual"):
            reviewed += 1
    total = len(groups)
    pct = round(100.0 * reviewed / total, 1) if total else 0.0
    return {
        "total": total,
        "new": counts.get("new", 0),
        "under_review": counts.get("under_review", 0),
        "false_positive": counts.get("false_positive", 0),
        "known": counts.get("known", 0),
        "reviewed": reviewed,
        "reviewed_pct": pct,
        "buckets": {key: {"label": FILTER_LABELS[key], "count": counts.get(key, 0)} for key in FILTER_LABELS},
    }


def _is_known(row: dict) -> bool:
    code = str(row.get("status_code") or "")
    if code == "known":
        return True
    checks = row.get("checks") or {}
    for name in ("simbad", "vsx", "exofop", "nea"):
        if checks.get(name, {}).get("state") == "fail":
            return True
    return False


def rank_explanation(row: dict) -> list[str]:
    """Human-readable bullets for why a candidate ranks where it does."""
    lines = []
    for part in row.get("priority_parts") or []:
        label = _PART_LABELS.get(part.get("label"), str(part.get("label")))
        value = float(part.get("value") or 0)
        sign = "+" if value >= 0 else ""
        lines.append(f"{sign}{value:.1f} {label}")
    checks = row.get("checks") or {}
    for key, title in _CHECK_ORDER:
        item = checks.get(key) or {}
        state = item.get("state") or "unknown"
        summary = item.get("summary") or state
        if state == "unknown":
            lines.append(f"Missing: {title} ({summary})")
        elif state == "warn":
            lines.append(f"Warning: {title} — {summary}")
        elif state == "fail":
            lines.append(f"Fail: {title} — {summary}")
    if row.get("needs_recheck"):
        reason = row.get("recheck_reason") or "New sector coverage"
        lines.append(f"Needs re-check: {reason}")
    if _is_known(row):
        lines.append("Known catalog match — excluded from shortlist lead positions")
    return lines


def shortlist_rows(groups: list[dict], *, limit: int = 25) -> list[dict]:
    """Strongest candidates with explainable ranking. Known objects are demoted."""
    limit = max(1, min(int(limit), 100))
    ranked: list[dict] = []
    for row in groups:
        copy = dict(row)
        base = float(row.get("review_priority") or 0)
        if _is_known(row):
            copy["shortlist_score"] = base - 500.0
            copy["shortlist_excluded"] = True
        elif str(row.get("status_code")) in ("rejected", "binary"):
            copy["shortlist_score"] = base - 300.0
            copy["shortlist_excluded"] = True
        else:
            copy["shortlist_score"] = base
            copy["shortlist_excluded"] = False
        copy["rank_explanation"] = rank_explanation(row)
        ranked.append(copy)
    ranked.sort(key=lambda item: (-float(item["shortlist_score"]), int(item["tic"])))
    for index, row in enumerate(ranked[:limit], start=1):
        row["rank"] = index
    return ranked[:limit]


def attach_recheck(groups: list[dict], recheck: dict[int, dict]) -> None:
    for row in groups:
        meta = recheck.get(int(row["tic"])) or {}
        row["detection_sectors"] = meta.get("detection_sectors") or row.get("sectors") or []
        row["observed_sectors"] = meta.get("observed_sectors") or []
        row["needs_recheck"] = bool(meta.get("needs_recheck"))
        row["recheck_reason"] = meta.get("recheck_reason") or ""
        row["recheck_sectors"] = meta.get("recheck_sectors") or []


def checks_complete(checks: dict) -> dict:
    done, missing = [], []
    for key, title in _CHECK_ORDER:
        state = (checks.get(key) or {}).get("state")
        if state in ("pass", "warn", "fail"):
            done.append(title)
        else:
            missing.append(title)
    return {"done": done, "missing": missing}


def export_markdown(row: dict, *, generated_at: str) -> str:
    lines = [
        f"# TIC {row['tic']} — candidate report",
        "",
        f"> {_DRAFT_BANNER}",
        "",
        _CANDIDATE_DISCLAIMER,
        "",
        f"Generated: {generated_at}",
        "",
        "## Signal",
        f"- Period: {row.get('period')} d",
        f"- Depth: {row.get('depth')}",
        f"- SDE: {row.get('sde')}",
        f"- SNR: {row.get('snr')}",
        f"- Sectors with detections: {row.get('sectors_label') or '—'}",
        f"- Review priority: {row.get('review_priority')}",
        f"- Status: {row.get('status_label')} ({row.get('status_detail')})",
        "",
    ]
    if row.get("needs_recheck"):
        lines.extend([
            "## Re-check",
            f"- **Needs re-check** — {row.get('recheck_reason') or 'new sector coverage'}",
            f"- Observed in sectors: {', '.join(str(s) for s in row.get('observed_sectors') or []) or '—'}",
            f"- Detections in sectors: {', '.join(str(s) for s in row.get('detection_sectors') or []) or '—'}",
            "",
        ])
    lines.append("## Ranking breakdown")
    for bullet in row.get("rank_explanation") or rank_explanation(row):
        lines.append(f"- {bullet}")
    lines.append("")
    complete = checks_complete(row.get("checks") or {})
    lines.append("## Vetting checks")
    lines.append(f"- Done: {', '.join(complete['done']) or 'none yet'}")
    lines.append(f"- Missing or pending: {', '.join(complete['missing']) or 'none'}")
    lines.append("")
    lines.append("## Per-sector detections")
    lines.append("| Sector | Period | Depth | SDE | SNR | Odd/even σ | Secondary σ |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for hit in row.get("detections") or []:
        lines.append(
            f"| {hit.get('sector')} | {hit.get('period')} | {hit.get('depth')} | "
            f"{hit.get('sde')} | {hit.get('snr')} | {hit.get('odd_even_sigma')} | {hit.get('sec_sigma')} |"
        )
    if row.get("review_note"):
        lines.append("")
        lines.append(f"Reviewer note: {row['review_note']}")
    lines.extend(["", "---", PRIORITY_HELP, ""])
    return "\n".join(lines)


def export_csv_row(row: dict) -> dict:
    complete = checks_complete(row.get("checks") or {})
    return {
        "tic": row.get("tic"),
        "period_d": row.get("period"),
        "depth": row.get("depth"),
        "sde": row.get("sde"),
        "snr": row.get("snr"),
        "review_priority": row.get("review_priority"),
        "shortlist_score": row.get("shortlist_score"),
        "rank": row.get("rank"),
        "status_code": row.get("status_code"),
        "status_label": row.get("status_label"),
        "needs_recheck": 1 if row.get("needs_recheck") else 0,
        "recheck_reason": row.get("recheck_reason") or "",
        "detection_sectors": ",".join(str(s) for s in row.get("detection_sectors") or []),
        "observed_sectors": ",".join(str(s) for s in row.get("observed_sectors") or []),
        "checks_done": ";".join(complete["done"]),
        "checks_missing": ";".join(complete["missing"]),
        "draft_banner": _DRAFT_BANNER,
    }


def export_csv(rows: list[dict]) -> str:
    if not rows:
        return "tic,draft_banner\n"
    keys = list(export_csv_row(rows[0]).keys())
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=keys, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(export_csv_row(row))
    return buf.getvalue()


def export_json_payload(rows: list[dict], *, kind: str, generated_at: str) -> dict:
    return {
        "draft": True,
        "draft_banner": _DRAFT_BANNER,
        "disclaimer": _CANDIDATE_DISCLAIMER,
        "kind": kind,
        "generated_at": generated_at,
        "rows": [
            {
                **export_csv_row(row),
                "rank_explanation": row.get("rank_explanation") or rank_explanation(row),
                "checks": row.get("checks"),
            }
            for row in rows
        ],
    }


def iso_now(clock) -> str:
    now = clock() if callable(clock) else datetime.utcnow()
    if hasattr(now, "isoformat"):
        return now.isoformat(timespec="seconds")
    return str(now)


def _aggregate_units(unit_rows) -> tuple[list[dict], dict[int, set[int]]]:
    """One pass over units: sector watch rows and TIC→observed-sector map."""
    from collections import defaultdict

    buckets: dict[tuple[str, int], dict] = {}
    sector_tics: dict[tuple[str, int], set[int]] = defaultdict(set)
    tic_observed: dict[int, set[int]] = defaultdict(set)
    for row in unit_rows:
        project = row["project"]
        sector = int(row["sector"])
        key = (project, sector)
        if key not in buckets:
            buckets[key] = {
                "project": project,
                "sector": sector,
                "units_total": 0,
                "units_agreed": 0,
            }
        buckets[key]["units_total"] += 1
        if row["state"] == "agreed":
            buckets[key]["units_agreed"] += 1
        tics = _tics_from_payload(row["payload_json"])
        sector_tics[key] |= tics
        for tic in tics:
            tic_observed[tic].add(sector)
    watch = [{**buckets[key], "tics": sorted(sector_tics[key])} for key in sorted(buckets)]
    return watch, tic_observed


def _tics_from_payload(payload_json: str) -> set[int]:
    try:
        payload = json.loads(payload_json or "")
    except json.JSONDecodeError:
        return set()
    raw = payload.get("tics") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return set()
    found: set[int] = set()
    for item in raw:
        try:
            found.add(int(item))
        except (TypeError, ValueError):
            continue
    return found


def refresh_candidate_recheck(store, *, project: str | None = None) -> None:
    """Update per-TIC sector coverage and needs_recheck flags (idempotent)."""
    from collections import defaultdict

    from .store import iso

    where = ""
    args: list = []
    if project:
        where = "WHERE project=?"
        args.append(project)
    unit_rows = store.conn.execute(
        f"SELECT project, sector, state, payload_json FROM units {where}",
        args,
    ).fetchall()
    watch, tic_observed = _aggregate_units(unit_rows)

    previous_rows = store.conn.execute(
        "SELECT project, sector, units_total, units_agreed, tics_json FROM science_sector_watch"
    ).fetchall()
    prev_map: dict[tuple[str, int], dict] = {}
    for row in previous_rows:
        prev_map[(row["project"], int(row["sector"]))] = {
            "units_total": int(row["units_total"]),
            "units_agreed": int(row["units_agreed"]),
            "tics": json.loads(row["tics_json"] or "[]"),
        }
    sector_by_key: dict[int, dict] = {}
    triggered: set[int] = set()
    for row in watch:
        key = (row["project"], row["sector"])
        sector_by_key[int(row["sector"])] = row
        prev = prev_map.get(key, {"units_total": 0, "units_agreed": 0, "tics": []})
        prev_total = int(prev.get("units_total") or 0)
        cur_total = int(row.get("units_total") or 0)
        prev_agreed = int(prev.get("units_agreed") or 0)
        cur_agreed = int(row.get("units_agreed") or 0)
        if cur_total > 0 and prev_total == 0:
            triggered.add(int(row["sector"]))
        elif cur_total > 0 and cur_agreed >= cur_total and prev_agreed < cur_total:
            triggered.add(int(row["sector"]))

    detection_by_tic: dict[int, list[int]] = defaultdict(list)
    for row in store.conn.execute("SELECT DISTINCT tic, sector FROM candidates ORDER BY tic, sector"):
        detection_by_tic[int(row["tic"])].append(int(row["sector"]))

    prev_recheck: dict[int, set[int]] = {}
    for row in store.conn.execute("SELECT tic, recheck_sectors FROM candidate_recheck"):
        try:
            prev_recheck[int(row["tic"])] = {int(x) for x in json.loads(row["recheck_sectors"] or "[]")}
        except json.JSONDecodeError:
            prev_recheck[int(row["tic"])] = set()

    now = iso(store.clock())
    watch_rows = [
        (row["project"], row["sector"], row["units_total"], row["units_agreed"], json.dumps(row["tics"]), now)
        for row in watch
    ]
    recheck_rows = []
    for tic in sorted(detection_by_tic):
        detection = detection_by_tic[tic]
        observed = sorted(tic_observed.get(tic, set()))
        old_recheck = prev_recheck.get(tic, set())
        new_from_trigger = set()
        for sector in triggered:
            sector_tics = sector_by_key.get(sector, {}).get("tics") or []
            if tic in sector_tics:
                new_from_trigger.add(sector)
        recheck_sectors = sorted((old_recheck | new_from_trigger) - set(detection))
        needs = 1 if recheck_sectors else 0
        reason = ""
        if recheck_sectors:
            reason = "Star now covered by sector(s) " + ", ".join(str(s) for s in recheck_sectors)
        recheck_rows.append((
            tic,
            json.dumps(detection),
            json.dumps(observed),
            json.dumps(recheck_sectors),
            needs,
            reason,
            now,
        ))

    with store._tx() as conn:
        conn.execute("DELETE FROM science_sector_watch")
        if watch_rows:
            conn.executemany(
                """INSERT INTO science_sector_watch
                   (project, sector, units_total, units_agreed, tics_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                watch_rows,
            )
        if recheck_rows:
            conn.executemany(
                """INSERT INTO candidate_recheck
                   (tic, detection_sectors, observed_sectors, recheck_sectors,
                    needs_recheck, recheck_reason, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(tic) DO UPDATE SET
                       detection_sectors=excluded.detection_sectors,
                       observed_sectors=excluded.observed_sectors,
                       recheck_sectors=excluded.recheck_sectors,
                       needs_recheck=excluded.needs_recheck,
                       recheck_reason=excluded.recheck_reason,
                       updated_at=excluded.updated_at""",
                recheck_rows,
            )


def recheck_index(store) -> dict[int, dict]:
    rows = store.conn.execute(
        """SELECT tic, detection_sectors, observed_sectors, recheck_sectors,
                  needs_recheck, recheck_reason FROM candidate_recheck"""
    ).fetchall()
    found: dict[int, dict] = {}
    for row in rows:
        tic = int(row["tic"])
        def _loads(text):
            try:
                return json.loads(text or "[]")
            except json.JSONDecodeError:
                return []
        found[tic] = {
            "detection_sectors": _loads(row["detection_sectors"]),
            "observed_sectors": _loads(row["observed_sectors"]),
            "recheck_sectors": _loads(row["recheck_sectors"]),
            "needs_recheck": bool(int(row["needs_recheck"] or 0)),
            "recheck_reason": row["recheck_reason"] or "",
        }
    return found