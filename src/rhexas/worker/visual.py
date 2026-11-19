"""Dashboard picture speed. It never changes how hard the worker searches.

Slow, Normal, and Realtime only time the drawings on the local page.
The older Eco, Live, and Turbo labels were the same buttons, so a saved
value under one of those names maps onto the new three. Reduced motion
uses Slow and the page keeps its loops off.
"""
from __future__ import annotations

CYCLE_SECONDS = {
    "slow": 5.0,
    "normal": 3.0,
    "realtime": 0.0,
}

LEGACY_VISUAL = {
    "eco": "slow",
    "live": "normal",
    "turbo": "realtime",
}


def resolve_visual(stored, *, reduced_motion: bool = False) -> str:
    """The picture speed to draw. Reduced motion is always Slow."""
    if reduced_motion:
        return "slow"
    name = str(stored or "").strip().lower()
    name = LEGACY_VISUAL.get(name, name)
    if name not in CYCLE_SECONDS:
        return "normal"
    return name


def cycle_seconds(stored, *, reduced_motion: bool = False) -> float:
    """Seconds for one drawn cycle. Zero means no artificial wait."""
    return CYCLE_SECONDS[resolve_visual(stored, reduced_motion=reduced_motion)]


def loops_enabled(stored, *, reduced_motion: bool = False) -> bool:
    """Scripted replays. Off for reduced motion and for Realtime."""
    if reduced_motion:
        return False
    return resolve_visual(stored) != "realtime"
