"""When the search unit is busy, local review waits."""
from __future__ import annotations

from pathlib import Path

_BUSY_STAGES = frozenset({"downloading", "searching", "filtering", "uploading"})


def search_busy(cache: Path) -> tuple[bool, str]:
    """True when a running search unit should not be slowed."""
    from ..live import DB_NAME, _connect, _read_kv

    try:
        conn = _connect(Path(cache) / DB_NAME, write=False)
    except Exception:
        return False, ""
    try:
        batch = _read_kv(conn, "batch")
        processing = _read_kv(conn, "processing")
    finally:
        conn.close()
    if isinstance(processing, dict) and processing.get("tic"):
        return True, "search is processing a star"
    if isinstance(batch, dict) and batch.get("active"):
        stage = str(batch.get("stage") or "").lower()
        if stage in _BUSY_STAGES:
            return True, f"search unit is {stage}"
    return False, ""


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


def estimate_fits_bytes(sector: int) -> int:
    """Rough size for one TESS light curve when not cached."""
    return 2_500_000


def estimate_tpf_bytes(sector: int) -> int:
    """Rough size for one SPOC target-pixel file. Larger than the light curve."""
    del sector
    return 80_000_000


_ONE_GB = 1_000_000_000


def fits_gate(cache: Path, nbytes: int) -> tuple[bool, str]:
    """False when a download is over 1 GB or would pass the processed-data cap."""
    try:
        nbytes = int(nbytes)
    except (TypeError, ValueError):
        nbytes = 0
    if nbytes > _ONE_GB:
        return False, "skipped download: estimated file is over 1 GB"
    used, limit, note = processed_headroom(cache)
    if used + nbytes > limit:
        return False, "skipped download: it would pass the processed-data cap"
    return True, note