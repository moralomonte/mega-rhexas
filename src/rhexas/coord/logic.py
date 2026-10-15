"""Pure helpers: chunk TIC lists and decide whether two BLS summaries agree.

No database and no network. Tolerances are the friends-beta defaults; the same
code on the same light curve should land well inside them. A made-up period,
SDE, or depth should not.
"""
from __future__ import annotations

import math
import re

from rhexas.tics import filename_from_url, tic_from_filename

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")

_STAR_FLOATS = (
    "period", "t0", "duration", "depth", "depth_err", "snr", "sde",
    "odd_even_sigma", "sec_sigma",
)
_STAR_INTS = ("n_transits", "npts")


def check_worker_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME.fullmatch(name):
        raise ValueError("worker name must be 1–40 characters: letters, digits, dot, underscore, hyphen")
    return name


_CURL_URL = re.compile(r"https\S+lc\.fits")


def tics_from_curl_script(text: str) -> list[int]:
    """TIC IDs from a MAST ``tesscurl_sector_*_lc.sh`` script, order kept."""
    found: list[int] = []
    for url in _CURL_URL.findall(text):
        tic = tic_from_filename(filename_from_url(url))
        if tic:
            found.append(tic)
    return found


def chunk_tics(tics: list[int], unit_size: int) -> list[list[int]]:
    """Split TIC IDs into ordered chunks. Duplicates are dropped, order kept."""
    if unit_size < 1:
        raise ValueError("unit_size must be >= 1")
    cleaned: list[int] = []
    seen: set[int] = set()
    for raw in tics:
        tic = int(raw)
        if tic <= 0:
            raise ValueError(f"TIC id must be positive, got {tic}")
        if tic in seen:
            continue
        seen.add(tic)
        cleaned.append(tic)
    if not cleaned:
        raise ValueError("no TIC ids to enroll")
    return [cleaned[i:i + unit_size] for i in range(0, len(cleaned), unit_size)]


def _finite(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def star_failed(star: dict | None) -> bool:
    """True when this worker has no usable top hit for the star."""
    if not star:
        return True
    err = star.get("error")
    if err is not None and str(err).strip() not in ("", "nan", "None"):
        return True
    for key in ("period", "sde", "depth"):
        if _finite(star.get(key)) is None:
            return True
    return False


def _blank_error(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text in ("", "nan", "None", "NaN"):
        return None
    return text[:200]


def normalize_stars(stars) -> list[dict]:
    if not isinstance(stars, list):
        raise ValueError("stars must be a list")
    out = []
    for star in stars:
        if not isinstance(star, dict) or "tic" not in star:
            raise ValueError("each star needs a tic")
        item: dict = {"tic": int(star["tic"])}
        for key in _STAR_FLOATS:
            item[key] = _finite(star.get(key))
        for key in _STAR_INTS:
            number = _finite(star.get(key))
            item[key] = None if number is None else int(number)
        item["error"] = _blank_error(star.get("error"))
        out.append(item)
    return out


def normalize_candidates(candidates) -> list[dict]:
    if not isinstance(candidates, list):
        raise ValueError("candidates must be a list")
    out = []
    for row in candidates:
        if not isinstance(row, dict) or "tic" not in row:
            raise ValueError("each candidate needs a tic")
        item = {
            "tic": int(row["tic"]),
            "status": str(row.get("status") or ""),
            "flags": str(row.get("flags") or "")[:300],
        }
        for key in ("period", "t0", "duration", "depth", "snr", "sde", "score", "odd_even_sigma", "sec_sigma"):
            item[key] = _finite(row.get(key))
        n_transits = _finite(row.get("n_transits"))
        item["n_transits"] = None if n_transits is None else int(n_transits)
        out.append(item)
    return out


def index_stars(stars: list[dict]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for star in stars:
        try:
            tic = int(star["tic"])
        except (KeyError, TypeError, ValueError):
            continue
        out[tic] = star
    return out


def _relative(a: float, b: float) -> float:
    return abs(a - b) / max(abs(a), abs(b), 1e-12)


def hits_close(a: dict, b: dict, settings) -> bool:
    """Top-hit period, SDE, and depth within the coordinator tolerances."""
    if _relative(float(a["period"]), float(b["period"])) > settings.period_rtol:
        return False
    if abs(float(a["sde"]) - float(b["sde"])) > settings.sde_atol:
        return False
    depth_a, depth_b = float(a["depth"]), float(b["depth"])
    allowed = max(settings.depth_atol, settings.depth_rtol * max(abs(depth_a), abs(depth_b), 1e-12))
    return abs(depth_a - depth_b) <= allowed


def compare_star_lists(unit_tics: list[int], stars_a: list[dict], stars_b: list[dict], settings) -> tuple[bool, str]:
    """Return whether two workers' top hits agree on this unit.

    A star both workers failed to measure is a shared gap, not a fight.
    A star only one worker measured counts toward ``max_miss_fraction``.
    Every star both workers measured must match period, SDE, and depth.
    """
    left, right = index_stars(stars_a), index_stars(stars_b)
    misses = 0
    mismatches: list[int] = []
    compared = 0
    for tic in unit_tics:
        fail_a = star_failed(left.get(int(tic)))
        fail_b = star_failed(right.get(int(tic)))
        if fail_a and fail_b:
            continue
        if fail_a or fail_b:
            misses += 1
            continue
        compared += 1
        if not hits_close(left[int(tic)], right[int(tic)], settings):
            mismatches.append(int(tic))
    n = max(len(unit_tics), 1)
    if misses / n > settings.max_miss_fraction:
        return False, f"coverage: {misses} of {len(unit_tics)} stars measured by only one worker"
    if mismatches:
        shown = ", ".join(str(tic) for tic in mismatches[:5])
        return False, f"{len(mismatches)} stars outside tolerance (TIC {shown})"
    return True, f"{compared} stars matched"


def new_tic_ids(candidates: list[dict]) -> set[int]:
    return {int(row["tic"]) for row in candidates if row.get("status") == "NEW"}


def measured_count(stars: list[dict]) -> int:
    return sum(1 for star in stars if not star_failed(star))
