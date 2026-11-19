"""Map a worker snapshot onto the original mission-control events.

The page in ``dashboard/static/index.html`` listens for ``state``, ``log``,
``lc``, ``cands``, ``sky``, ``hist``, and ``mode``. This module builds those
payloads from the worker cache. It does not read the coordinator or the token.
"""
from __future__ import annotations

import math
import time

from .live import CANDIDATE_LABEL, pause_view

_STAGES = {
    "claimed": "Downloading",
    "downloading": "Downloading",
    "downloaded": "Searching",
    "searching": "Searching",
    "searched": "Filtering",
    "filtering": "Filtering",
    "filtered": "Filtering",
    "uploading": "Filtering",
    "uploaded": "Done",
    "waiting": "Done",
}

MODES = {
    "eco": (2.0, 6.0),
    "live": (0.5, 2.0),
    "turbo": (0.25, 1.0),
}


def mode_event(name: str) -> dict:
    mode = name if name in MODES else "live"
    fast, plot = MODES[mode]
    return {"fast": fast, "plot": plot, "mode": mode}


def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _stage(batch: dict) -> str:
    return _STAGES.get(str(batch.get("stage") or "").lower(), "Starting")


def next_line(nxt) -> str:
    """One line for the batch downloading ahead. Empty when nothing is prefetched."""
    if not isinstance(nxt, dict) or nxt.get("active") is False:
        return ""
    stage = str(nxt.get("stage") or "").lower()
    try:
        total = int(nxt.get("stars_total") or 0)
    except (TypeError, ValueError):
        total = 0
    try:
        done = int(nxt.get("stars_done") or 0)
    except (TypeError, ValueError):
        done = 0
    if stage in {"ready", "downloaded"}:
        return "Next: ready"
    if stage in {"downloading", "claimed", "download"}:
        if total:
            return f"Next: downloading {done}/{total}"
        return "Next: downloading"
    return ""


def mission_state(snap: dict) -> dict:
    """Counters and stage names the original header, rings, and steps already draw."""
    batch = snap.get("batch") if isinstance(snap.get("batch"), dict) else {}
    life = snap.get("lifetime") if isinstance(snap.get("lifetime"), dict) else {}
    res = snap.get("resources") if isinstance(snap.get("resources"), dict) else {}
    conn = snap.get("connection") if isinstance(snap.get("connection"), dict) else {}
    stage = _stage(batch)
    done = int(batch.get("stars_done") or 0)
    total = int(batch.get("stars_total") or 0)
    per_s = _num(batch.get("per_s"))
    if stage == "Downloading":
        downloaded, searched, rate, dl_rate = done, 0, 0.0, per_s
    elif stage == "Searching":
        downloaded, searched, rate, dl_rate = total, done, per_s or 0.0, None
    else:
        downloaded, searched, rate, dl_rate = total, done, per_s or 0.0, None
    eta = _num(batch.get("eta_min"))
    cache_bytes = _num(res.get("cache_bytes")) or 0.0
    data_gb = cache_bytes / 1e9
    cap = _num(res.get("max_cache_gb"))
    free_gb = round(max(0.0, cap - data_gb), 2) if cap is not None else None
    candidates = snap.get("candidates") if isinstance(snap.get("candidates"), list) else []
    stats = snap.get("candidate_stats") if isinstance(snap.get("candidate_stats"), dict) else {}
    if "new" in stats:
        new_count = int(stats.get("new") or 0)
        known_count = int(stats.get("known") or 0)
    else:
        new_count = 0
        known_count = 0
        for row in candidates:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "NEW")
            if status in ("known_TOI", "known_confirmed"):
                known_count += 1
            elif status == "NEW":
                new_count += 1
    alive = bool(res.get("worker_alive"))
    ok = conn.get("ok")
    kind = str(conn.get("kind") or "")
    detail = str(conn.get("detail") or "")
    low = detail.lower()
    if kind == "unreachable" or low.startswith("unreachable") or (ok is False and kind in ("", "unknown")):
        link = "coordinator unreachable"
    elif kind == "network" or low.startswith("network"):
        link = "coordinator network trouble"
    elif kind == "rejected" or low.startswith("rejected"):
        link = "coordinator rejected a request"
    elif ok is True or kind in ("connected", "lease_ended") or low.startswith("lease ended"):
        link = "coordinator connected"
    else:
        link = "coordinator not checked yet"
    bits = [f"worker running (pid {int(res.get('pid') or 0)})" if alive else "worker process not detected", link]
    if conn.get("url"):
        bits.append(str(conn["url"]))
    if conn.get("detail"):
        bits.append(str(conn["detail"]))
    if batch.get("unit_id"):
        bits.append(f"unit {int(batch['unit_id'])}")
    bits.append(
        f"pending {int(life.get('contributions_pending') or 0)} · verified {int(life.get('contributions_verified') or 0)}"
    )
    user_paused = bool(res.get("user_paused"))
    holding = bool(res.get("holding")) and user_paused
    switch = res.get("pause") if isinstance(res.get("pause"), dict) and res.get("pause", {}).get("state") else pause_view(user_paused, holding)
    banner = ""
    if not alive and stage != "Done":
        banner = "The worker process is not attached to this cache. Leave rhexas worker running and this page will follow it."
    elif switch.get("state") == "paused":
        banner = "Paused. Turn the worker back on from this page. This batch stays leased."
    elif switch.get("state") == "pausing":
        banner = "Pausing… the worker will stop at the next safe point. This batch stays leased."
    pace_mode = res.get("mode") if res.get("mode") in ("eco", "live", "turbo") else "live"
    return {
        "stage": stage,
        "demo": False,
        "sector": int(batch.get("sector") or 0),
        "project": batch.get("project") or "",
        "downloaded": downloaded,
        "searched": searched,
        "total": total,
        "errors": 0,
        "passed": new_count,
        "rate": rate,
        "dl_rate": dl_rate,
        "eta_min": None if eta is None else int(round(eta)),
        "data_gb": round(data_gb, 3),
        "free_gb": free_gb,
        "cpu": _num(res.get("cpu_percent")),
        "pipeline_alive": alive,
        "pid": int(res.get("pid") or 0),
        "coord_line": " · ".join(bits),
        "banner": banner,
        "pace_mode": pace_mode,
        "user_paused": user_paused,
        "holding": holding,
        "pause": switch,
        "next_line": next_line(snap.get("next")),
        "worker_panel": {
            "stars_searched": int(life.get("stars_searched") or 0),
            "batches_submitted": int(life.get("batches_submitted") or 0),
            "batches_agreed": int(life.get("batches_agreed") or 0),
            "batches_verified": int(life.get("batches_verified") or 0),
            "contributions_pending": int(life.get("contributions_pending") or 0),
            "contributions_verified": int(life.get("contributions_verified") or 0),
            "procs": res.get("procs"),
            "threads": res.get("threads"),
            "priority": res.get("priority") or "",
            "pid": int(res.get("pid") or 0),
            "alive": alive,
            "mode": pace_mode,
            "paused": user_paused,
            "user_paused": user_paused,
            "holding": holding,
            "pause": switch,
            "cache_bytes": int(cache_bytes),
            "max_cache_gb": cap,
            "cpu": _num(res.get("cpu_percent")),
            "ok": ok,
            "kind": kind,
            "conn_label": link,
            "url": str(conn.get("url") or ""),
            "detail": str(conn.get("detail") or ""),
        },
        "display": snap.get("display") if isinstance(snap.get("display"), dict) else None,
        "processing": snap.get("processing") if isinstance(snap.get("processing"), dict) else None,
        "app_version": str(snap.get("app_version") or ""),
        "sky_rev": snap.get("sky_rev") or "0:0",
        "new_counts": {
            # total is candidates, unverified: the worker's own NEW rows, not a display cap.
            "total": new_count,
            "verified": int(life.get("contributions_verified") or 0),
            "pending": int(life.get("contributions_pending") or 0),
            "submitted": int(life.get("batches_submitted") or 0),
            "known_ctoi": known_count,
            "promising": new_count,
            "unchecked": int(life.get("contributions_pending") or 0),
            "fp": int(life.get("batches_submitted") or 0),
        },
    }


def _pgram(star: dict) -> list | None:
    """The periodogram the search stored. Missing stays missing."""
    pgram = star.get("pgram")
    if (
        isinstance(pgram, list)
        and len(pgram) == 2
        and isinstance(pgram[0], list)
        and isinstance(pgram[1], list)
        and len(pgram[0]) >= 2
        and len(pgram[0]) == len(pgram[1])
    ):
        return pgram
    return None


def _fold(star: dict) -> list | None:
    phase = star.get("phase") if isinstance(star.get("phase"), list) else []
    folded = star.get("folded") if isinstance(star.get("folded"), list) else []
    if len(phase) < 3 or len(phase) != len(folded):
        return None
    period = _num(star.get("period")) or 1.0
    hours = [round(float(p) * period * 24.0, 3) for p in phase]
    return [hours, folded, hours, folded]


def mission_lc(star: dict | None, *, passed: bool | None = None) -> dict | None:
    """One light-curve payload. Missing curves stay null. No stand-in series."""
    if not isinstance(star, dict) or star.get("tic") is None:
        return None
    period = _num(star.get("period"))
    depth = _num(star.get("depth"))
    duration = _num(star.get("duration"))
    sde = _num(star.get("sde"))
    snr = _num(star.get("snr"))
    raw_t = star.get("time") if isinstance(star.get("time"), list) else []
    raw_y = star.get("flux") if isinstance(star.get("flux"), list) else []
    raw = [raw_t, raw_y] if len(raw_t) == len(raw_y) and len(raw_t) >= 2 else None
    if raw is None and isinstance(star.get("raw"), list):
        raw = star.get("raw")
    status = star.get("status") or ""
    is_hit = bool(star.get("candidate")) if passed is None else bool(passed)
    if passed is None and status == "done" and star.get("candidate") is None and period is not None and not star.get("error"):
        is_hit = True
    stamp = star.get("at")
    try:
        clock = time.strftime("%H:%M:%S", time.localtime(float(stamp)))
    except (TypeError, ValueError, OSError):
        clock = None
    return {
        "tic": int(star["tic"]),
        "version": star.get("version"),
        "sector": star.get("sector"),
        "unit": star.get("unit") if star.get("unit") is not None else star.get("unit_id"),
        "ra": _num(star.get("ra")),
        "dec": _num(star.get("dec")),
        "tmag": _num(star.get("tmag")),
        "period": period,
        "duration": duration,
        "depth": depth,
        "sde": sde,
        "snr": snr,
        "n_transits": star.get("n_transits"),
        "status": CANDIDATE_LABEL if is_hit else (status or None),
        "passed": bool(is_hit),
        "raw": raw,
        "pgram": _pgram(star),
        "fold": _fold(star) or star.get("fold"),
        "missing": list(star.get("missing") or []),
        "t": clock,
    }


def _score(row: dict) -> float:
    snr = _num(row.get("snr")) or 0.0
    sde = _num(row.get("sde")) or 0.0
    return round(snr * min(sde, 30.0) / 10.0, 2)


def mission_cands(snap: dict) -> list:
    """Showcase rows. ``status`` stays NEW so they use the gold card, not the known-planet column."""
    rows = []
    for row in snap.get("candidates") or []:
        if not isinstance(row, dict) or row.get("tic") is None:
            continue
        view = _view(snap, row["tic"]) or {}
        status = str(row.get("status") or "NEW")
        rows.append({
            "tic": int(row["tic"]),
            "period": _num(row.get("period")),
            "depth": _num(row.get("depth")),
            "duration": _num(view.get("duration")) or _num(row.get("duration")),
            "snr": _num(row.get("snr")),
            "sde": _num(row.get("sde")),
            "n_transits": view.get("n_transits") if view.get("n_transits") is not None else row.get("n_transits"),
            "status": status,
            "score": _score(row),
            "prelim": True,
            "label": CANDIDATE_LABEL if status == "NEW" else status,
        })
    rows.sort(key=lambda item: -item["score"])
    return rows


def _edges(stop: float, n_bins: int) -> list[float]:
    return [round(stop * i / n_bins, 4) for i in range(n_bins + 1)]


def mission_hist(snap: dict) -> dict:
    stored = snap.get("hist") if isinstance(snap.get("hist"), dict) else {}
    sde = [int(v or 0) for v in list(stored.get("sde") or [])]
    snr = [int(v or 0) for v in list(stored.get("snr") or [])]
    if not any(sde) and not any(snr):
        sde = [0] * 60
        snr = [0] * 60
        for row in list(snap.get("candidates") or []):
            if not isinstance(row, dict):
                continue
            _bump(sde, _num(row.get("sde")), 30.0)
            _bump(snr, _num(row.get("snr")), 60.0)
        star = snap.get("current_star") if isinstance(snap.get("current_star"), dict) else {}
        _bump(sde, _num(star.get("sde")), 30.0)
        _bump(snr, _num(star.get("snr")), 60.0)
    sde = (sde + [0] * 60)[:60]
    snr = (snr + [0] * 60)[:60]
    return {
        "sde": [sde, _edges(30.0, 60), 6],
        "snr": [snr, _edges(60.0, 60), 7],
    }


def _bump(counts, value, span: float) -> None:
    if value is None:
        return
    clamped = min(span - 1e-6, max(0.0, value))
    counts[int(clamped / span * len(counts))] += 1


def mission_sky(snap: dict) -> list:
    points = []
    for item in snap.get("sky") or []:
        if not isinstance(item, list) or len(item) < 4 or item[1] is None or item[2] is None:
            continue
        flag = int(item[4]) if len(item) > 4 and item[4] is not None else 1
        points.append([int(item[0]), float(item[1]), float(item[2]), item[3] if item[3] is not None else 12, flag])
    return points


def _lvl(text: str) -> str:
    low = text.lower()
    if any(word in low for word in ("error", "traceback", "fail", "exception")):
        return "error"
    if any(word in low for word in ("warn", "stop", "low space", "limit")):
        return "warn"
    if any(word in low for word in ("finished", "uploaded", "agreed", "candidate")):
        return "success"
    return "info"


def mission_logs(lines) -> list:
    out = []
    for line in lines or []:
        text = str(line).strip()
        if not text:
            continue
        clock, msg = "", text
        if len(text) >= 8 and text[2] == ":" and text[5] == ":":
            clock, msg = text[:8], text[8:].strip()
        out.append({
            "t": clock or time.strftime("%H:%M:%S"),
            "src": "worker",
            "lvl": _lvl(msg),
            "msg": msg,
        })
    return out


def fresh_logs(previous: list, lines: list) -> list:
    """Lines the console has not shown yet. A rotated log sends the new tail."""
    if not previous:
        return list(lines)
    if lines[: len(previous)] == previous:
        return lines[len(previous):]
    last = previous[-1]
    if last in lines:
        return lines[lines.index(last) + 1 :]
    return list(lines)


def _view(snap: dict, tic) -> dict | None:
    star = snap.get("current_star") if isinstance(snap.get("current_star"), dict) else None
    try:
        wanted = int(tic)
    except (TypeError, ValueError):
        return None
    if isinstance(star, dict) and star.get("tic") is not None and int(star.get("tic")) == wanted:
        return star
    return None


def cycle_star(snap: dict, now: float | None = None) -> dict | None:
    """The star on screen: last finished, or the candidate the Done state is walking.

    ``snapshot`` already picks that row from the stars table. ``now`` is accepted
    so callers can ask again; the choice itself is made when the snapshot is read.
    """
    del now
    star = snap.get("current_star")
    return star if isinstance(star, dict) else None


def detail_payload(snap: dict, tic: int) -> dict:
    """Modal payload from a stored record. Missing numbers stay null."""
    view = _view(snap, tic)
    if not isinstance(view, dict):
        return {"tic": int(tic), "available": False, "missing": ["record"], "error": "not available"}
    raw_t = view.get("time") if isinstance(view.get("time"), list) else []
    raw_y = view.get("flux") if isinstance(view.get("flux"), list) else []
    raw = [raw_t, raw_y] if len(raw_t) == len(raw_y) and len(raw_t) >= 2 else None
    fold = _fold(view) or view.get("fold")
    is_hit = bool(view.get("candidate")) or str(view.get("verdict") or "") == "NEW"
    missing = list(view.get("missing") or [])
    return {
        "tic": int(tic),
        "version": view.get("version"),
        "available": True,
        "unverified": True,
        "period": _num(view.get("period")),
        "t0": _num(view.get("t0")),
        "duration": _num(view.get("duration")),
        "depth": _num(view.get("depth")),
        "snr": _num(view.get("snr")),
        "sde": _num(view.get("sde")),
        "n_transits": view.get("n_transits"),
        "odd_even_sigma": _num(view.get("odd_even_sigma")),
        "sec_sigma": _num(view.get("sec_sigma")),
        "star": {
            "ra": _num(view.get("ra")),
            "dec": _num(view.get("dec")),
            "tmag": _num(view.get("tmag")),
        },
        "raw": raw,
        "flat": raw,
        "transits": [],
        "pgram": _pgram(view),
        "fold": fold,
        "odd": view.get("odd"),
        "even": view.get("even"),
        "secondary": view.get("secondary"),
        "centroid": view.get("centroid"),
        "centroid_sigma": view.get("centroid_sigma"),
        "aperture": view.get("aperture"),
        "img": None,
        "missing": missing,
        "vet": {"verdict": CANDIDATE_LABEL if is_hit else (view.get("verdict") or None)},
    }


def pixels_payload(snap: dict, tic: int) -> dict:
    """Aperture view for one star. Pixel movies need a target-pixel file, which workers do not keep."""
    view = None
    star = snap.get("current_star")
    if isinstance(star, dict) and int(star.get("tic") or 0) == int(tic):
        view = star
    if view is None:
        view = _view(snap, tic)
    aperture = view.get("aperture") if isinstance(view, dict) else None
    if not isinstance(aperture, list) or not aperture:
        aperture = None
    return {
        "kind": "aperture",
        "aperture": aperture,
        "note": (
            "Pipeline aperture from this star's light curve."
            if aperture else "No aperture stored for this star yet."
        ),
    }


def sectors_payload(snap: dict, port: int) -> dict:
    batch = snap.get("batch") if isinstance(snap.get("batch"), dict) else {}
    sector = int(batch.get("sector") or 0)
    project = str(batch.get("project") or "exoplanets")
    unit = batch.get("unit_id")
    label = project if not unit else f"unit {int(unit)} · {project}"
    return {"list": [{"sector": sector, "port": int(port), "label": label}], "current": sector}
