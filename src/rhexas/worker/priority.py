"""Run the worker below the interactive priority of the machine."""
from __future__ import annotations

import os


def nice_value(priority: str):
    """OS value passed to ``psutil.Process.nice`` for idle, low, or normal."""
    name = (priority or "").lower()
    if name not in ("idle", "low", "normal"):
        raise ValueError("priority must be idle, low, or normal")
    if os.name == "nt":
        import psutil
        return {
            "idle": psutil.IDLE_PRIORITY_CLASS,
            "low": psutil.BELOW_NORMAL_PRIORITY_CLASS,
            "normal": psutil.NORMAL_PRIORITY_CLASS,
        }[name]
    return {"idle": 19, "low": 10, "normal": 0}[name]


def apply_priority(priority: str) -> int:
    """Set this process's priority. Children (the BLS pool) inherit it."""
    import psutil
    value = nice_value(priority)
    psutil.Process().nice(value)
    return value
