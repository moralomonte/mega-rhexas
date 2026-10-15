"""Map a worker snapshot onto the original mission-control events.

The page in ``dashboard/static/index.html`` listens for ``state``, ``log``,
``lc``, ``cands``, ``sky``, ``hist``, and ``mode``. This module builds those
payloads from the worker cache. It does not read the coordinator or the token.
"""
from __future__ import annotations

import math
import time

from .live import CANDIDATE_LABEL

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
    alive = bool(res.get("worker_alive"))
    ok = conn.get("ok")
    if ok is True:
        link = "coordinator connected"
    elif ok is False:
        link = "coordinator unreachable"
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
    banner = ""
    if not alive and stage != "Done":
        banner = "The worker process is not attached to this cache. Leave rhexas worker running and this page will follow it."
    return {
        "stage": stage,
        "demo": False,
        "sector": int(batch.get("sector") or 0),
        "project": batch.get("project") or "",
        "downloaded": downloaded,
        "searched": searched,
        "total": total,
        "errors": 0,
        "passed": len(candidates),
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
            "cache_bytes": int(cache_bytes),
            "max_cache_gb": cap,
            "cpu": _num(res.get("cpu_percent")),
            "ok": ok,
            "url": str(conn.get("url") or ""),
            "detail": str(conn.get("detail") or ""),
        },
        "new_counts": {
            "total": len(candidates),
            "promising": int(life.get("contributions_verified") or 0),
            "unchecked": int(life.get("contributions_pending") or 0),
            "fp": int(life.get("batches_submitted") or 0),
            "known_ctoi": 0,
        },
    }


def _synth_pgram(period, sde) -> list:
    center = period if period and period > 0 else 3.0
    height = sde if sde and sde > 0 else 1.0
    n = 160
    lo, hi = math.log(0.5), math.log(13.0)
    periods = []
    power = []
    for i in range(n):
        p = math.exp(lo + (hi - lo) * i / (n - 1))
        z = math.log(p / center) / 0.06
        periods.append(round(p, 5))
        power.append(round(height * math.exp(-0.5 * z * z) + 0.08 * height, 3))
    return [periods, power]


def _pgram(star: dict) -> list:
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
    return _synth_pgram(_num(star.get("period")), _num(star.get("sde")))


def _fold(star: dict) -> list | None:
    phase = star.get("phase") if isinstance(star.get("phase"), list) else []
    folded = star.get("folded") if isinstance(star.get("folded"), list) else []
    if len(phase) < 3 or len(phase) != len(folded):
        return None
    period = _num(star.get("period")) or 1.0
    hours = [round(float(p) * period * 24.0, 3) for p in phase]
    return [hours, folded, hours, folded]


def mission_lc(star: dict | None, *, passed: bool | None = None) -> dict | None:
    """One light-curve payload. The page loops the scan animation on its own."""
    if not isinstance(star, dict) or star.get("tic") is None:
        return None
    period = _num(star.get("period"))
    depth = _num(star.get("depth"))
    duration = _num(star.get("duration")) or 0.1
    sde = _num(star.get("sde"))
    snr = _num(star.get("snr"))
    raw_t = star.get("time") if isinstance(star.get("time"), list) else []
    raw_y = star.get("flux") if isinstance(star.get("flux"), list) else []
    if len(raw_t) != len(raw_y) or len(raw_t) < 2:
        fold = _fold(star)
        raw_t = fold[0] if fold else [0.0, 1.0]
        raw_y = fold[1] if fold else [1.0, 1.0]
    status = star.get("status") or ""
    is_hit = bool(passed) or (status == "done" and period is not None and not star.get("error"))
    stamp = star.get("at")
    try:
        clock = time.strftime("%H:%M:%S", time.localtime(float(stamp)))
    except (TypeError, ValueError, OSError):
        clock = time.strftime("%H:%M:%S")
    return {
        "tic": int(star["tic"]),
        "ra": _num(star.get("ra")),
        "dec": _num(star.get("dec")),
        "tmag": _num(star.get("tmag")),
        "period": period,
        "duration": duration,
        "depth": depth if depth is not None else 0.0,
        "sde": sde if sde is not None else 0.0,
        "snr": snr if snr is not None else 0.0,
        "n_transits": star.get("n_transits"),
        "status": CANDIDATE_LABEL if is_hit else (status or "searching"),
        "passed": bool(is_hit),
        "raw": [raw_t, raw_y],
        "pgram": _pgram(star),
        "fold": _fold(star),
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
        rows.append({
            "tic": int(row["tic"]),
            "period": _num(row.get("period")) or 0.0,
            "depth": _num(row.get("depth")) or 0.0,
            "duration": _num(view.get("duration")) or 0.1,
            "snr": _num(row.get("snr")) or 0.0,
            "sde": _num(row.get("sde")) or 0.0,
            "n_transits": view.get("n_transits"),
            "status": "NEW",
            "score": _score(row),
            "prelim": True,
            "label": CANDIDATE_LABEL,
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
    views = snap.get("views") if isinstance(snap.get("views"), dict) else {}
    view = views.get(str(tic))
    if isinstance(view, dict):
        return view
    star = snap.get("current_star") if isinstance(snap.get("current_star"), dict) else None
    if isinstance(star, dict) and star.get("tic") == tic:
        return star
    return None


def cycle_star(snap: dict, now: float | None = None) -> dict | None:
    """While the worker is idle, walk the candidate list the way the sector page walks its hits."""
    batch = snap.get("batch") if isinstance(snap.get("batch"), dict) else {}
    stage = _stage(batch)
    candidates = [row for row in (snap.get("candidates") or []) if isinstance(row, dict) and row.get("tic") is not None]
    if stage == "Done" and candidates:
        idx = int((now if now is not None else time.time()) // 8) % len(candidates)
        view = _view(snap, candidates[idx]["tic"]) or candidates[idx]
        return view
    star = snap.get("current_star")
    return star if isinstance(star, dict) else None


def detail_payload(snap: dict, tic: int) -> dict:
    """Modal and vetting-chart payload. Missing pixel data is left out on purpose."""
    view = _view(snap, tic)
    if not isinstance(view, dict):
        return {"error": "no stored curves for this star yet"}
    period = _num(view.get("period")) or 0.0
    depth = _num(view.get("depth")) or 0.0
    duration = _num(view.get("duration")) or 0.1
    raw_t = view.get("time") if isinstance(view.get("time"), list) else []
    raw_y = view.get("flux") if isinstance(view.get("flux"), list) else []
    fold = _fold(view)
    return {
        "tic": int(tic),
        "period": period,
        "t0": _num(view.get("t0")) or 0.0,
        "duration": duration,
        "depth": depth,
        "snr": _num(view.get("snr")) or 0.0,
        "sde": _num(view.get("sde")) or 0.0,
        "n_transits": view.get("n_transits") or 0,
        "odd_even_sigma": _num(view.get("odd_even_sigma")) or 0.0,
        "sec_sigma": _num(view.get("sec_sigma")) or 0.0,
        "star": {
            "ra": _num(view.get("ra")),
            "dec": _num(view.get("dec")),
            "tmag": _num(view.get("tmag")),
        },
        "raw": [raw_t, raw_y] if len(raw_t) == len(raw_y) and raw_t else [[0, 1], [1, 1]],
        "flat": [raw_t, raw_y] if len(raw_t) == len(raw_y) and raw_t else [[0, 1], [1, 1]],
        "transits": [],
        "pgram": _pgram(view),
        "fold": fold,
        "odd": view.get("odd"),
        "even": view.get("even"),
        "secondary": view.get("secondary"),
        "centroid": None,
        "img": None,
        "vet": {"verdict": CANDIDATE_LABEL},
    }


def sectors_payload(snap: dict, port: int) -> dict:
    batch = snap.get("batch") if isinstance(snap.get("batch"), dict) else {}
    sector = int(batch.get("sector") or 0)
    project = str(batch.get("project") or "exoplanets")
    unit = batch.get("unit_id")
    label = project if not unit else f"unit {int(unit)} · {project}"
    return {"list": [{"sector": sector, "port": int(port), "label": label}], "current": sector}
