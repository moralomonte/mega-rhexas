"""Keep processed light curves and results, under a size limit.

Candidates are never removed by the limit or by a sector delete. The files
that stay are the ones a later replay can draw. The coordinator is not called.
"""
from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

from rhexas.tics import tic_from_filename

from .cache import directory_bytes

DEFAULT_PROCESSED_LIMIT_GB = 20.0
_USAGE_TTL_S = 5.0
_ENFORCE_GAP_S = 30.0
_usage_cache: dict[str, tuple[float, dict]] = {}
_enforced_at: dict[str, float] = {}


def processed_limit_gb(cache: Path, configured: float | None = None) -> float:
    """Limit in GB. A saved preference wins over the config value, then 20."""
    from .holdings import _read_json, prefs_path

    try:
        limit = float(DEFAULT_PROCESSED_LIMIT_GB if configured is None else configured)
    except (TypeError, ValueError):
        limit = DEFAULT_PROCESSED_LIMIT_GB
    data = _read_json(prefs_path(cache))
    if isinstance(data, dict) and data.get("processed_limit_gb") is not None:
        try:
            limit = float(data["processed_limit_gb"])
        except (TypeError, ValueError):
            pass
    if limit <= 0:
        limit = 0.001
    return limit


def write_processed_limit_gb(cache: Path, limit_gb: float) -> float:
    from .holdings import _update_prefs

    limit = float(limit_gb)
    if limit <= 0:
        raise ValueError("processed limit must be greater than 0")
    _update_prefs(cache, processed_limit_gb=limit)
    _usage_cache.pop(str(Path(cache)), None)
    return limit


def _sector_from_name(name: str) -> int | None:
    if len(name) == 5 and name.startswith("s") and name[1:].isdigit():
        sector = int(name[1:])
        return sector if sector > 0 else None
    return None


def _candidate_tics(cache: Path) -> dict[int, set[int]]:
    """TIC ids marked as candidates, by sector. Missing tables yield nothing."""
    path = Path(cache) / "dashboard.sqlite"
    found: dict[int, set[int]] = {}
    if not path.is_file():
        return found
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return found
    try:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "stars" in names:
            for sector, tic in conn.execute("SELECT sector, tic FROM stars WHERE candidate=1"):
                found.setdefault(int(sector), set()).add(int(tic))
        if "candidates" in names:
            for sector, tic in conn.execute("SELECT sector, tic FROM candidates"):
                found.setdefault(int(sector), set()).add(int(tic))
    except sqlite3.Error:
        return found
    finally:
        conn.close()
    return found


def _folder_bytes(folder: Path) -> int:
    if not folder.is_dir():
        return 0
    total = 0
    try:
        paths = list(folder.rglob("*"))
    except OSError:
        return 0
    for path in paths:
        if not path.is_file():
            continue
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _mtime(folder: Path) -> float:
    latest = 0.0
    if not folder.exists():
        return latest
    try:
        latest = folder.stat().st_mtime
    except OSError:
        latest = 0.0
    try:
        for path in folder.rglob("*"):
            if path.is_file():
                latest = max(latest, path.stat().st_mtime)
    except OSError:
        return latest
    return latest


def sector_rows(cache: Path) -> list[dict]:
    """One row per sector that still has light curves or result files."""
    cache = Path(cache)
    protected = _candidate_tics(cache)
    rows: dict[int, dict] = {}

    def slot(sector: int) -> dict:
        row = rows.get(sector)
        if row is None:
            row = {
                "sector": sector,
                "bytes": 0,
                "deletable_bytes": 0,
                "candidates": len(protected.get(sector, ())),
                "oldest": 0.0,
            }
            rows[sector] = row
        return row

    data_root = cache / "data"
    if data_root.is_dir():
        try:
            children = list(data_root.iterdir())
        except OSError:
            children = []
        for child in children:
            sector = _sector_from_name(child.name)
            if sector is None or not child.is_dir():
                continue
            row = slot(sector)
            keep = protected.get(sector, set())
            try:
                files = [path for path in child.iterdir() if path.is_file()]
            except OSError:
                files = []
            for path in files:
                try:
                    size = path.stat().st_size
                    stamp = path.stat().st_mtime
                except OSError:
                    continue
                row["bytes"] += size
                row["oldest"] = stamp if row["oldest"] == 0 else min(row["oldest"], stamp)
                tic = tic_from_filename(path.name)
                if tic is None or tic not in keep:
                    row["deletable_bytes"] += size
    results = cache / "results"
    if results.is_dir():
        try:
            assignments = list(results.iterdir())
        except OSError:
            assignments = []
        for assignment in assignments:
            if not assignment.is_dir():
                continue
            try:
                nested = list(assignment.iterdir())
            except OSError:
                continue
            for child in nested:
                sector = _sector_from_name(child.name)
                if sector is None or not child.is_dir():
                    continue
                row = slot(sector)
                size = _folder_bytes(child)
                stamp = _mtime(child)
                row["bytes"] += size
                if stamp:
                    row["oldest"] = stamp if row["oldest"] == 0 else min(row["oldest"], stamp)
                if not protected.get(sector):
                    row["deletable_bytes"] += size
    ordered = list(rows.values())
    ordered.sort(key=lambda row: (row["oldest"] or 0, row["sector"]))
    return ordered


def usage(cache: Path, configured_gb: float | None = None) -> dict:
    """Processed bytes and the limit. Cached for a few seconds."""
    cache = Path(cache)
    key = str(cache)
    now = time.time()
    cached = _usage_cache.get(key)
    limit = processed_limit_gb(cache, configured_gb)
    if cached and now - cached[0] < _USAGE_TTL_S and cached[1].get("limit_gb") == limit:
        return cached[1]
    sectors = sector_rows(cache)
    total = sum(int(row["bytes"]) for row in sectors)
    payload = {
        "bytes": total,
        "limit_gb": limit,
        "limit_bytes": int(limit * 1e9),
        "sectors": [
            {
                "sector": int(row["sector"]),
                "bytes": int(row["bytes"]),
                "deletable_bytes": int(row["deletable_bytes"]),
                "candidates": int(row["candidates"]),
            }
            for row in sectors
        ],
    }
    _usage_cache[key] = (now, payload)
    return payload


def _drop_sector_files(cache: Path, sector: int, keep: set[int]) -> int:
    """Delete this sector's non-candidate files. Returns bytes removed."""
    removed = 0
    data = Path(cache) / "data" / f"s{sector:04d}"
    if data.is_dir():
        try:
            files = [path for path in data.iterdir() if path.is_file()]
        except OSError:
            files = []
        for path in files:
            tic = tic_from_filename(path.name)
            if tic is not None and tic in keep:
                continue
            try:
                size = path.stat().st_size
                path.unlink()
                removed += size
            except OSError:
                continue
    if keep:
        return removed
    results = Path(cache) / "results"
    if not results.is_dir():
        return removed
    try:
        assignments = list(results.iterdir())
    except OSError:
        return removed
    folder_name = f"s{sector:04d}"
    for assignment in assignments:
        target = assignment / folder_name
        if not target.is_dir():
            continue
        size = _folder_bytes(target)
        shutil.rmtree(target, ignore_errors=True)
        if not target.exists():
            removed += size
    return removed


def delete_sector(cache: Path, sector: int, *, active: set[int] | None = None) -> dict:
    """Delete one sector's processed files. Candidate light curves and rows stay."""
    sector = int(sector)
    if sector < 1:
        return {"ok": False, "error": "sector must be >= 1"}
    active = active or set()
    if sector in active:
        return {"ok": False, "error": "scanning", "sector": sector}
    keep = _candidate_tics(cache).get(sector, set())
    removed = _drop_sector_files(cache, sector, keep)
    _usage_cache.pop(str(Path(cache)), None)
    return {
        "ok": True,
        "sector": sector,
        "deleted_bytes": removed,
        "kept_candidates": len(keep),
    }


def enforce(cache: Path, limit_gb: float | None = None, *, active: set[int] | None = None) -> dict:
    """Drop oldest non-candidate sector data until the tree fits.

    Sectors being scanned are left alone. Candidate files are left alone.
    When only those remain, the total may stay above the limit.
    """
    cache = Path(cache)
    limit = processed_limit_gb(cache, limit_gb)
    cap = int(limit * 1e9)
    active = set(active or ())
    protected = _candidate_tics(cache)
    deleted = []
    rows = sector_rows(cache)
    total = sum(int(row["bytes"]) for row in rows)
    for row in rows:
        if total <= cap:
            break
        sector = int(row["sector"])
        if sector in active:
            continue
        if int(row["deletable_bytes"]) <= 0:
            continue
        removed = _drop_sector_files(cache, sector, protected.get(sector, set()))
        if removed:
            deleted.append({"sector": sector, "bytes": removed})
            total = max(0, total - removed)
    _usage_cache.pop(str(cache), None)
    return {"ok": True, "bytes": max(0, total), "limit_bytes": cap, "deleted": deleted}


def maybe_enforce(cache: Path, limit_gb: float | None = None, *, active: set[int] | None = None,
                  min_interval: float = _ENFORCE_GAP_S) -> dict | None:
    """Enforce at most once per interval. A dashboard poll can call this."""
    cache = Path(cache)
    key = str(cache)
    now = time.time()
    if now - _enforced_at.get(key, 0.0) < min_interval:
        return None
    _enforced_at[key] = now
    return enforce(cache, limit_gb, active=active)


def active_sectors(cache: Path) -> set[int]:
    """Sectors with a batch that is not finished. Those files stay."""
    from .holdings import _read_json

    found: set[int] = set()
    cache = Path(cache)
    for name in ("state.json", "next.json"):
        data = _read_json(cache / name)
        if isinstance(data, dict):
            _take_sector(found, data)
    queued = _read_json(cache / "queue.json")
    items = queued.get("units") if isinstance(queued, dict) else queued
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                _take_sector(found, item)
    return found


def _take_sector(found: set[int], row: dict) -> None:
    stage = str(row.get("stage") or "").strip().lower()
    if stage in {"done", "uploaded"}:
        return
    try:
        sector = int(row.get("sector") or 0)
    except (TypeError, ValueError):
        return
    if sector > 0:
        found.add(sector)


def processed_bytes(cache: Path) -> int:
    cache = Path(cache)
    return directory_bytes(cache / "data") + directory_bytes(cache / "results")
