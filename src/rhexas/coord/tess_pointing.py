"""TESS sector camera footprints for the coordinator sky map.

Spacecraft boresight (RA, Dec, J2000 degrees) per sector. Values were fetched
from NASA HEASARC sector summary pages, e.g.
https://heasarc.gsfc.nasa.gov/docs/tess/sectorNNN_summary.html
(the Spacecraft row under "Spacecraft Pointing"). They are checked in as static
JSON so the droplet does not call HEASARC at runtime.
"""
from __future__ import annotations

import json
from pathlib import Path

_DATA = Path(__file__).with_name("tess_pointing.json")
_FOV_RA_DEG = 24.0
_FOV_DEC_DEG = 96.0


def _load() -> dict[int, tuple[float, float]]:
    try:
        raw = json.loads(_DATA.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[int, tuple[float, float]] = {}
    for key, pair in raw.items():
        try:
            sector = int(key)
            ra, dec = float(pair[0]), float(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        out[sector] = (ra, dec)
    return out


_POINTING = _load()


def spacecraft_pointing(sector: int) -> tuple[float, float] | None:
    return _POINTING.get(int(sector))


def footprint_polygon(sector: int) -> list[list[float]] | None:
    """Axis-aligned RA/Dec box around the spacecraft boresight (approximate stripe)."""
    center = spacecraft_pointing(sector)
    if center is None:
        return None
    ra, dec = center
    half_ra = _FOV_RA_DEG * 0.5
    half_dec = _FOV_DEC_DEG * 0.5
    # wrap RA edges naively for display only
    return [
        [ra - half_ra, dec - half_dec],
        [ra + half_ra, dec - half_dec],
        [ra + half_ra, dec + half_dec],
        [ra - half_ra, dec + half_dec],
    ]


def sky_footprints(sectors: list[int]) -> list[dict]:
    rows = []
    for sector in sorted({int(sector) for sector in sectors}):
        center = spacecraft_pointing(sector)
        poly = footprint_polygon(sector)
        if center is None or poly is None:
            continue
        rows.append({
            "sector": sector,
            "ra": center[0],
            "dec": center[1],
            "polygon": poly,
        })
    return rows