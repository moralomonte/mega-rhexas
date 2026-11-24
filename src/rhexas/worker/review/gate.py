"""When local review may run, and atomic processed-data reservations."""
from __future__ import annotations

import threading
from pathlib import Path

_BUSY_STAGES = frozenset({"downloading", "searching", "filtering", "uploading"})
_BOUNDARY_STAGES = frozenset({"uploaded", "waiting", "done", "released", "error"})

_CAP_LOCK = threading.Lock()
_PENDING_BYTES: dict[str, int] = {}


def _cache_key(cache: Path) -> str:
    return str(Path(cache).resolve())


def _read_search_state(cache: Path) -> tuple[dict | None, dict | None]:
    from ..live import DB_NAME, _connect, _read_kv

    try:
        conn = _connect(Path(cache) / DB_NAME, write=False)
    except Exception:
        return None, None
    try:
        batch = _read_kv(conn, "batch")
        processing = _read_kv(conn, "processing")
    finally:
        conn.close()
    batch = batch if isinstance(batch, dict) else None
    processing = processing if isinstance(processing, dict) else None
    return batch, processing


def search_busy(cache: Path) -> tuple[bool, str]:
    """True when a running search unit should not share cores with review work."""
    batch, processing = _read_search_state(cache)
    if isinstance(processing, dict) and processing.get("tic"):
        return True, "search is processing a star"
    if isinstance(batch, dict) and batch.get("active"):
        stage = str(batch.get("stage") or "").lower()
        if stage in _BUSY_STAGES:
            return True, f"search unit is {stage}"
    return False, ""


def unit_boundary_open(cache: Path) -> tuple[bool, str]:
    """True in the gap after one unit uploads and before the next is claimed."""
    batch, processing = _read_search_state(cache)
    if isinstance(processing, dict) and processing.get("tic"):
        return False, "search is processing a star"
    if not isinstance(batch, dict):
        return True, "no active search unit"
    if not batch.get("active"):
        stage = str(batch.get("stage") or "").lower()
        if stage in _BOUNDARY_STAGES or not stage:
            return True, "between search units"
        return True, f"search paused ({stage})"
    busy, reason = search_busy(cache)
    if busy:
        return False, reason
    return True, "search idle"


def may_start_review(cache: Path, *, concurrent: bool) -> tuple[bool, str]:
    """Whether the review runner may claim a queued job now."""
    if concurrent:
        return True, "background (low priority)"
    open_now, detail = unit_boundary_open(cache)
    if open_now:
        return True, detail
    busy, reason = search_busy(cache)
    return False, reason or detail


def search_wait_minutes(cache: Path) -> float | None:
    """Rough minutes until the current unit may finish (boundary mode UI)."""
    batch, _ = _read_search_state(cache)
    if not isinstance(batch, dict):
        return None
    eta = batch.get("eta_min")
    try:
        if eta is not None:
            value = float(eta)
            if value >= 0:
                return value
    except (TypeError, ValueError):
        pass
    try:
        done = int(batch.get("stars_done") or 0)
        total = int(batch.get("stars_total") or 0)
        per_s = float(batch.get("per_s") or 0)
    except (TypeError, ValueError):
        return None
    if per_s > 0 and total > done:
        return max(0.0, (total - done) / per_s / 60.0)
    return None


def processed_headroom(cache: Path, configured_gb: float | None = None) -> tuple[int, int, str]:
    """Bytes used, limit, note when near the cap."""
    from ..retention import processed_limit_gb, usage

    limit_gb = processed_limit_gb(Path(cache), configured_gb)
    snap = usage(Path(cache), limit_gb)
    used = int(snap.get("bytes") or 0)
    limit = int(snap.get("limit_bytes") or limit_gb * 1_000_000_000)
    note = ""
    if used > limit * 0.92:
        note = "Processed data is near the retention cap; extra downloads may be skipped."
    return used, limit, note


def reserve_processed_bytes(cache: Path, nbytes: int) -> tuple[bool, str]:
    """Atomically reserve bytes against the processed-data cap (per worker cache)."""
    try:
        nbytes = int(nbytes)
    except (TypeError, ValueError):
        nbytes = 0
    if nbytes <= 0:
        return True, ""
    with _CAP_LOCK:
        used, limit, note = processed_headroom(cache)
        key = _cache_key(cache)
        pending = int(_PENDING_BYTES.get(key) or 0)
        if used + pending + nbytes > limit:
            return False, "skipped download: it would pass the processed-data cap"
        _PENDING_BYTES[key] = pending + nbytes
    return True, note


def release_processed_bytes(cache: Path, nbytes: int) -> None:
    try:
        nbytes = int(nbytes)
    except (TypeError, ValueError):
        return
    if nbytes <= 0:
        return
    with _CAP_LOCK:
        key = _cache_key(cache)
        pending = max(0, int(_PENDING_BYTES.get(key) or 0) - nbytes)
        if pending:
            _PENDING_BYTES[key] = pending
        else:
            _PENDING_BYTES.pop(key, None)


def clear_pending_reservations(cache: Path) -> None:
    """Test helper."""
    with _CAP_LOCK:
        _PENDING_BYTES.pop(_cache_key(cache), None)


def estimate_fits_bytes(sector: int) -> int:
    """Rough size for one TESS light curve when not cached."""
    return 2_500_000


def estimate_tpf_bytes(sector: int) -> int:
    """Rough size for one SPOC target-pixel file. Larger than the light curve."""
    del sector
    return 80_000_000


_ONE_GB = 1_000_000_000


def fits_gate(cache: Path, nbytes: int, *, reserve: bool = False) -> tuple[bool, str]:
    """False when a download is over 1 GB or would pass the processed-data cap."""
    try:
        nbytes = int(nbytes)
    except (TypeError, ValueError):
        nbytes = 0
    if nbytes > _ONE_GB:
        return False, "skipped download: estimated file is over 1 GB"
    if reserve:
        return reserve_processed_bytes(cache, nbytes)
    used, limit, note = processed_headroom(cache)
    if used + nbytes > limit:
        return False, "skipped download: it would pass the processed-data cap"
    return True, note