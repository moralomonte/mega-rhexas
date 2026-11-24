"""Transit depth units for storage and display.

Stored and uploaded depth fields (e.g. check ``numbers`` odd_depth, primary_depth,
and BLS ``depth`` on candidate rows) are **flux fractions**: (out-of-transit
baseline − in-transit level) / baseline, dimensionless. Values like 0.0047 mean
a 0.47% dip. Convert to human-readable percent or ppm only at display time.
"""
from __future__ import annotations

import math
import re

# Documented unit for persisted depth numbers across worker vetting and coordinator uploads.
TRANSIT_DEPTH_STORED_UNIT = "flux_fraction"

_DEPTH_KEY = re.compile(r"(^|_)depth($|_)", re.IGNORECASE)


def format_transit_depth(depth_fraction: float | None) -> str:
    """Format a stored flux-fraction depth once, as percent or ppm."""
    if depth_fraction is None:
        return "an unknown depth"
    try:
        number = float(depth_fraction)
    except (TypeError, ValueError):
        return "an unknown depth"
    if not math.isfinite(number):
        return "an unknown depth"
    if abs(number) >= 0.001:
        return f"{number * 100:.2f}%"
    return f"{number * 1e6:.0f} ppm"


def is_depth_number_key(key: str) -> bool:
    lowered = (key or "").strip().lower()
    if lowered == "depth":
        return True
    return bool(_DEPTH_KEY.search(lowered))


def format_check_number(key: str, value) -> str:
    """Display one numeric field from a vetting check ``numbers`` map."""
    if value is None:
        return "—"
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        if is_depth_number_key(key):
            return format_transit_depth(float(value))
        number = float(value)
        if abs(number) >= 1000 or (abs(number) < 0.001 and number != 0):
            return f"{number:.4g}"
        return f"{number:.4f}".rstrip("0").rstrip(".")
    return str(value)