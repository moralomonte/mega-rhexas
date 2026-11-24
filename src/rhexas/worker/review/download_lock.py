"""One in-flight download per TIC+sector across search and review."""
from __future__ import annotations

import threading

_REGISTRY = threading.Lock()
_LOCKS: dict[tuple[int, int], threading.Lock] = {}


def lightcurve_lock(tic: int, sector: int) -> threading.Lock:
    key = (int(tic), int(sector))
    with _REGISTRY:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock