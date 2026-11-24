"""Discoveries gallery: list cards, detail, to-scale orbits, and thumbnails.

The coordinator does not store worker light curves. Fold thumbnails are a
placeholder until folded flux is uploaded. Generated SVGs may be cached on disk
under the same directory as coord.sqlite.
"""
from __future__ import annotations

import re
from pathlib import Path

from .vetting import (
    R_JUP_EARTH,
    R_SUN_EARTH,
    STATUS_LABELS,
    as_float,
    companion_radius,
    build_groups,
)

# UI filter buckets (not the same strings as automatic status codes).
FILTER_LABELS = {
    "new": "New",
    "under_review": "Under review",
    "false_positive": "Likely false positive",
    "known": "Known catalog match",
}

_STATUS_BUCKET = {
    "new": "new",
    "pending": "under_review",
    "passed": "under_review",
    "ctoi": "under_review",
    "binary": "false_positive",
    "rejected": "false_positive",
    "known": "known",
}

EARTH_R = 1.0
NEPTUNE_R = 3.886
JUPITER_R = R_JUP_EARTH
SUN_R = R_SUN_EARTH

_REF_BODIES = (
    ("Earth", EARTH_R, "#4ade80"),
    ("Neptune", NEPTUNE_R, "#38bdf8"),
    ("Jupiter", JUPITER_R, "#f97316"),
)


def filter_bucket(status_code: str) -> str:
    return _STATUS_BUCKET.get(str(status_code or "").strip().lower(), "under_review")


def known_catalog_match(checks: dict) -> dict | None:
    """Cross-match from vetting checks — TOI, CTOI, or NASA archive planet only."""
    exofop = checks.get("exofop") or {}
    if exofop.get("state") == "fail":
        summary = str(exofop.get("summary") or "")
        if "CTOI" in summary.upper():
            name = _first_token(summary, r"CTOI\s*[\d.]+") or summary.split(";")[0].strip()
            return {"kind": "known CTOI", "name": name, "source": "ExoFOP CTOI list"}
        name = _first_token(summary, r"TOI\s*[\d.]+") or summary.split(";")[0].strip()
        return {"kind": "known TOI", "name": name, "source": "ExoFOP TOI list"}
    nea = checks.get("nea") or {}
    if nea.get("state") == "fail":
        summary = str(nea.get("summary") or "")
        name = summary.replace("Known planet:", "").strip()
        if not name:
            name = _first_token(str(nea.get("detail") or ""), r"[A-Za-z0-9.\-]+\s+[A-Za-z]+") or "Archive planet"
        return {"kind": "known planet", "name": name.split(",")[0].strip(), "source": "NASA Exoplanet Archive"}
    return None


def _first_token(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(0).strip() if match else None


def orbit_au(period_days: float, mass_sun: float | None) -> tuple[float | None, str]:
    """Semi-major axis in AU from Kepler's third law. P in days, M in solar masses."""
    period = as_float(period_days)
    if period is None or period <= 0:
        return None, "Period unknown"
    mass = as_float(mass_sun)
    assume = False
    if mass is None or mass <= 0:
        mass = 1.0
        assume = True
    years = period / 365.25
    axis = (years * years * mass) ** (1.0 / 3.0)
    note = "assumes Sun-like star" if assume else "from TIC stellar mass"
    return axis, note


def planet_radius_fields(
    depth: float | None, tic_payload: dict | None,
) -> dict:
    """Estimated Rp from transit depth and TIC stellar radius."""
    rad = None
    if tic_payload and tic_payload.get("found"):
        rad = as_float(tic_payload.get("rad"))
    depth_val = as_float(depth)
    if depth_val is None or depth_val <= 0 or rad is None or rad <= 0:
        return {
            "radius_earth": None,
            "radius_jupiter": None,
            "radius_label": "radius unknown",
        }
    rp_earth, rp_jup = companion_radius(depth_val, rad)
    return {
        "radius_earth": round(rp_earth, 2),
        "radius_jupiter": round(rp_jup, 3),
        "radius_label": f"{rp_earth:.1f} R⊕ ({rp_jup:.2f} Rjup)",
    }


def to_scale_payload(
    *,
    period: float | None,
    depth: float | None,
    tic_payload: dict | None,
) -> dict:
    """Numbers for the detail canvas: true relative radii and orbit when possible."""
    star_sun = as_float((tic_payload or {}).get("rad")) if tic_payload and tic_payload.get("found") else None
    mass = as_float((tic_payload or {}).get("mass")) if tic_payload and tic_payload.get("found") else None
    star_earth = star_sun * R_SUN_EARTH if star_sun and star_sun > 0 else None
    radius = planet_radius_fields(depth, tic_payload)
    candidate_r = radius.get("radius_earth")
    axis, axis_note = orbit_au(period, mass)
    return {
        "star_radius_earth": round(star_earth, 1) if star_earth else None,
        "candidate_radius_earth": candidate_r,
        "reference_bodies": [{"name": n, "radius_earth": r, "color": c} for n, r, c in _REF_BODIES],
        "orbit_au": round(axis, 4) if axis is not None else None,
        "orbit_note": axis_note,
        "radius_label": radius["radius_label"],
    }


def placeholder_fold_svg(*, width: int = 160, height: int = 72) -> str:
    """Light-curve placeholder when no folded flux is stored on the coordinator."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="No folded light curve stored">'
        f'<rect width="100%" height="100%" fill="#0b0d1c" rx="6"/>'
        f'<line x1="8" y1="{height - 12}" x2="{width - 8}" y2="{height - 12}" stroke="#334155" stroke-width="1"/>'
        f'<text x="{width // 2}" y="{height // 2 - 4}" fill="#64748b" font-size="10" '
        f'text-anchor="middle" font-family="system-ui,sans-serif">No LC on coordinator</text>'
        f'<text x="{width // 2}" y="{height // 2 + 10}" fill="#475569" font-size="9" '
        f'text-anchor="middle" font-family="system-ui,sans-serif">phase-fold pending</text>'
        f"</svg>"
    )


def fold_thumbnail_svg(phase: list, flux: list, *, width: int = 160, height: int = 72) -> str | None:
    """Minimal phase-fold polyline when phase/flux pairs exist."""
    if not isinstance(phase, list) or not isinstance(flux, list) or len(phase) < 3:
        return None
    if len(phase) != len(flux):
        return None
    pairs = sorted(zip(phase, flux), key=lambda item: float(item[0]))
    xs = [float(p[0]) for p in pairs]
    ys = [float(p[1]) for p in pairs]
    if not xs:
        return None
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    if xmax <= xmin:
        xmax = xmin + 1.0
    if ymax <= ymin:
        ymax = ymin + 1e-6
    pad_x, pad_y = 10, 8
    plot_w = width - 2 * pad_x
    plot_h = height - 2 * pad_y

    def map_x(x):
        return pad_x + (x - xmin) / (xmax - xmin) * plot_w

    def map_y(y):
        return pad_y + (1.0 - (y - ymin) / (ymax - ymin)) * plot_h

    points = " ".join(f"{map_x(x):.1f},{map_y(y):.1f}" for x, y in zip(xs, ys))
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="#0b0d1c" rx="6"/>'
        f'<polyline fill="none" stroke="#22d3ee" stroke-width="1.2" points="{points}"/></svg>'
    )


def thumb_cache_dir(db_path: Path) -> Path:
    """Thumbnail directory beside the coordinator sqlite file (e.g. ``…/rhexas/thumbs``)."""
    return Path(db_path).resolve().parent / "thumbs"


def read_or_write_thumbnail(cache_dir: Path, tic: int, fold_phase=None, fold_flux=None, store=None) -> bytes:
    """Disk cache under /var/lib/rhexas/thumbs (or next to the sqlite file)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"tic-{int(tic)}.svg"
    if path.is_file():
        return path.read_bytes()
    if store is not None and fold_phase is None:
        try:
            loaded = store.local_check_fold_for_tic(int(tic))
        except Exception:
            loaded = None
        if loaded is not None:
            fold_phase, fold_flux = loaded
    svg = fold_thumbnail_svg(fold_phase or [], fold_flux or [])
    if svg is None:
        svg = placeholder_fold_svg()
    path.write_text(svg, encoding="utf-8")
    return svg.encode("utf-8")


def card_from_group(group: dict, *, agreed_at: str | None, tic_payload: dict | None) -> dict:
    """One gallery card — always described as a candidate, never confirmed."""
    checks = group.get("checks") or {}
    known = known_catalog_match(checks)
    depth = group.get("depth")
    radius = planet_radius_fields(depth, tic_payload)
    code = group.get("status_code") or "new"
    bucket = filter_bucket(code)
    flags = str(group.get("flags") or "").strip()
    vet_summary = _vet_flags_summary(checks)
    return {
        "tic": int(group["tic"]),
        "sector": group.get("sector"),
        "sectors_label": group.get("sectors_label") or "",
        "period": group.get("period"),
        "depth": depth,
        "sde": group.get("sde"),
        "snr": group.get("snr"),
        "radius_earth": radius.get("radius_earth"),
        "radius_jupiter": radius.get("radius_jupiter"),
        "radius_label": radius["radius_label"],
        "review_priority": group.get("review_priority"),
        "status_code": code,
        "status_label": STATUS_LABELS.get(code, code),
        "status_detail": group.get("status_detail") or "",
        "filter_bucket": bucket,
        "filter_label": FILTER_LABELS.get(bucket, bucket),
        "vetting_flags": vet_summary,
        "flags": flags,
        "known_match": known,
        "agreed_at": agreed_at or "",
        "thumb_url": f"/api/v1/discoveries/{int(group['tic'])}/thumb.svg",
        "detail_url": f"/discoveries/{int(group['tic'])}",
        "wording": "candidate",
    }


def _vet_flags_summary(checks: dict) -> list[str]:
    out = []
    for key, row in sorted(checks.items()):
        if not isinstance(row, dict):
            continue
        state = row.get("state")
        if state in ("fail", "warn") and row.get("summary"):
            out.append(f"{key}: {row['summary']}")
    return out[:8]


def sort_groups(groups: list[dict], mode: str) -> list[dict]:
    mode = (mode or "priority").strip().lower()
    if mode == "sde":
        return sorted(groups, key=lambda row: (-(as_float(row.get("sde")) or -1.0), int(row["tic"])))
    if mode == "date":
        return sorted(
            groups,
            key=lambda row: (str(row.get("agreed_at") or ""), int(row["tic"])),
            reverse=True,
        )
    return sorted(
        groups,
        key=lambda row: (-float(row.get("review_priority") or 0.0), int(row["tic"])),
    )


def filter_groups(groups: list[dict], status_filter: str | None) -> list[dict]:
    if not status_filter or status_filter.strip().lower() in ("", "all"):
        return groups
    want = status_filter.strip().lower()
    return [row for row in groups if row.get("filter_bucket") == want]


def build_gallery_payload(
    store,
    *,
    page: int = 1,
    per_page: int = 24,
    status: str | None = None,
    sort: str = "priority",
    project: str | None = None,
) -> dict:
    """Paginated discovery cards."""
    page = max(1, int(page))
    per_page = max(1, min(int(per_page), 60))
    detections = store._candidate_detections(project)
    if not detections:
        return {"page": page, "per_page": per_page, "total": 0, "items": [], "sort": sort, "status": status or "all"}
    tics = sorted({int(row["tic"]) for row in detections})
    groups = build_groups(
        detections,
        store._catalog_cache_for(tics),
        store._reviews_for(tics),
        store._catalog_file_indexes(),
    )
    agreed = store._agreed_at_by_tic(tics)
    cards = []
    for group in groups:
        tic = int(group["tic"])
        payload = store._tic_payload(tic)
        item = card_from_group(group, agreed_at=agreed.get(tic), tic_payload=payload)
        cards.append({**group, **item, "agreed_at": agreed.get(tic) or ""})
    cards = filter_groups(cards, status)
    cards = sort_groups(cards, sort)
    total = len(cards)
    start = (page - 1) * per_page
    slice_ = cards[start : start + per_page]
    return {
        "page": page,
        "per_page": per_page,
        "total": total,
        "sort": sort,
        "status": status or "all",
        "items": slice_,
    }


def build_detail_payload(store, tic: int, project: str | None = None) -> dict | None:
    tic = int(tic)
    detections = [row for row in store._candidate_detections(project) if int(row["tic"]) == tic]
    if not detections:
        return None
    groups = build_groups(
        detections,
        store._catalog_cache_for([tic]),
        store._reviews_for([tic]),
        store._catalog_file_indexes(),
    )
    if not groups:
        return None
    group = groups[0]
    payload = store._tic_payload(tic)
    agreed = store._agreed_at_by_tic([tic]).get(tic)
    card = card_from_group(group, agreed_at=agreed, tic_payload=payload)
    scale = to_scale_payload(period=group.get("period"), depth=group.get("depth"), tic_payload=payload)
    return {
        **card,
        "period_note": group.get("period_note"),
        "priority_parts": group.get("priority_parts") or [],
        "checks": group.get("checks") or {},
        "detections": group.get("detections") or [],
        "review_manual": group.get("review_manual"),
        "review_note": group.get("review_note") or "",
        "to_scale": scale,
    }