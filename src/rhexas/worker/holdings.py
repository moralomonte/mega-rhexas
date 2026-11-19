"""Local sector units: what this computer is holding, and when it may delete them.

The coordinator's receipt is the only thing that marks a unit confirmed.
The page can ask to clear; this module refuses unless that receipt says
every star was stored, and it never deletes the unit being scanned.
"""
from __future__ import annotations

import json
from pathlib import Path

# A download must leave at least this much free, even if min_free_gb is lower.
FREE_DISK_FLOOR_GB = 10.0
# Background fetches use one thread so the search keeps the machine.
BACKGROUND_DOWNLOAD_THREADS = 1

_STATUS = {
    "queued": "Queued",
    "claimed": "Queued",
    "downloading": "Downloading",
    "downloaded": "Ready",
    "ready": "Ready",
    "searching": "Scanning",
    "searched": "Scanning",
    "filtering": "Scanning",
    "filtered": "Scanning",
    "scanning": "Scanning",
    "uploading": "Uploading",
    "uploaded": "Done",
    "done": "Done",
}

# The unit in front is being scanned while it is in one of these stages.
_SCANNING = {"searching", "searched", "filtering", "filtered", "scanning"}


def display_status(stage) -> str:
    return _STATUS.get(str(stage or "").strip().lower(), "Queued")


def free_floor_gb(min_free_gb: float) -> float:
    """Free space a new unit must leave. Never under 10 GB."""
    try:
        configured = float(min_free_gb)
    except (TypeError, ValueError):
        configured = 0.0
    if configured < 0:
        configured = 0.0
    return max(FREE_DISK_FLOOR_GB, configured)


def units_path(cache: Path) -> Path:
    return Path(cache) / "units.json"


def prefs_path(cache: Path) -> Path:
    return Path(cache) / "prefs.json"


def _read_json(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def load_units(cache: Path) -> list[dict]:
    data = _read_json(units_path(cache))
    rows = data.get("units") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict) and row.get("assignment_id") is not None]


def save_units(cache: Path, units: list[dict]) -> None:
    _write_json(units_path(cache), {"units": units})


def runtime_cap_gb(cache: Path, configured_gb: float) -> float:
    """Storage cap. The Sectors page writes prefs.json over the config value."""
    try:
        cap = float(configured_gb)
    except (TypeError, ValueError):
        cap = 20.0
    data = _read_json(prefs_path(cache))
    if isinstance(data, dict) and data.get("max_cache_gb") is not None:
        try:
            cap = float(data["max_cache_gb"])
        except (TypeError, ValueError):
            pass
    if cap <= 0:
        cap = 0.001
    return cap


def write_cap_gb(cache: Path, max_cache_gb: float) -> float:
    cap = float(max_cache_gb)
    if cap <= 0:
        raise ValueError("storage cap must be greater than 0")
    _write_json(prefs_path(cache), {"max_cache_gb": cap})
    return cap


def upsert_unit(cache: Path, assignment: dict, **fields) -> dict:
    """Insert or update one local unit. ``fields`` win over the assignment."""
    units = load_units(cache)
    try:
        aid = int(assignment["assignment_id"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("assignment_id is required") from None
    current = None
    for row in units:
        try:
            if int(row.get("assignment_id")) == aid:
                current = row
                break
        except (TypeError, ValueError):
            continue
    if current is None:
        current = {"assignment_id": aid}
        units.append(current)
    for key in ("unit_id", "sector", "project", "generation", "n_tics", "tics"):
        if assignment.get(key) is not None and key not in fields:
            current[key] = assignment.get(key)
    for key, value in fields.items():
        current[key] = value
    if "project" not in current or not current["project"]:
        current["project"] = "exoplanets"
    save_units(cache, units)
    return current


def apply_receipt(submit_body) -> dict:
    """What the worker is allowed to remember from a coordinator reply.

    A missing or incomplete receipt stays unconfirmed. The page cannot set this.
    """
    receipt = submit_body.get("receipt") if isinstance(submit_body, dict) else None
    if not isinstance(receipt, dict):
        receipt = submit_body if isinstance(submit_body, dict) and "complete" in submit_body else {}
    try:
        expected = int(receipt.get("n_expected") or 0)
    except (TypeError, ValueError):
        expected = 0
    try:
        received = int(receipt.get("n_received") or 0)
    except (TypeError, ValueError):
        received = 0
    complete = bool(receipt.get("complete")) and bool(receipt.get("received", True)) and received >= expected
    if expected <= 0:
        complete = False
    stored = {
        "complete": complete,
        "received": bool(receipt.get("received")),
        "n_expected": expected,
        "n_received": received,
    }
    return {"results_sent": True, "results_confirmed": complete, "receipt": stored}


def queue_block(*, queued: int, queue_max: int, used_bytes: int, extra_bytes: int,
                cap_bytes: int, free_bytes: int, floor_bytes: int) -> str | None:
    """Why a new unit must not be reserved. None when it fits."""
    if queue_max >= 0 and queued >= queue_max:
        return "queue"
    if used_bytes + extra_bytes > cap_bytes:
        return "cache cap"
    if free_bytes - extra_bytes < floor_bytes:
        return "free space"
    return None


def _assignment_id(row) -> int | None:
    try:
        return int(row.get("assignment_id"))
    except (TypeError, ValueError, AttributeError):
        return None


def scanning_assignment_id(cache: Path) -> int | None:
    """The unit in front, while its stage is a scan. Ready and queued units are not this."""
    state = _read_json(Path(cache) / "state.json")
    if not isinstance(state, dict):
        return None
    if str(state.get("stage") or "").lower() not in _SCANNING:
        return None
    return _assignment_id(state)


def clear_refusal(cache: Path, unit: dict) -> str | None:
    """None when this finished unit may be deleted. Otherwise a short reason."""
    aid = _assignment_id(unit)
    if aid is None:
        return "missing"
    scanning = scanning_assignment_id(cache)
    if scanning is not None and scanning == aid:
        return "scanning"
    state = _read_json(Path(cache) / "state.json")
    if isinstance(state, dict) and _assignment_id(state) == aid:
        if str(state.get("stage") or "").lower() not in {"uploaded", "done"}:
            return "scanning"
    if display_status(unit.get("stage")) != "Done":
        return "not finished"
    if not unit.get("results_confirmed"):
        return "not confirmed"
    receipt = unit.get("receipt") if isinstance(unit.get("receipt"), dict) else {}
    try:
        expected = int(receipt.get("n_expected") or 0)
        received = int(receipt.get("n_received") or 0)
    except (TypeError, ValueError):
        return "not confirmed"
    if not receipt.get("complete") or expected <= 0 or received < expected:
        return "not confirmed"
    return None


def unit_disk_bytes(cache: Path, unit: dict) -> int:
    """Bytes of this unit's light curves still on disk."""
    from rhexas.tics import tic_from_filename
    try:
        sector = int(unit.get("sector") or 0)
    except (TypeError, ValueError):
        return 0
    tics = unit.get("tics") or []
    try:
        wanted = {int(tic) for tic in tics}
    except (TypeError, ValueError):
        wanted = set()
    folder = Path(cache) / "data" / f"s{sector:04d}"
    if not folder.is_dir() or not wanted:
        return 0
    total = 0
    try:
        paths = list(folder.glob("*"))
    except OSError:
        return 0
    for path in paths:
        if not path.is_file():
            continue
        tic = tic_from_filename(path.name)
        if tic in wanted:
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def disk_used_bytes(cache: Path) -> int:
    from .cache import directory_bytes
    return directory_bytes(Path(cache) / "data")


def delete_unit_files(cache: Path, unit: dict) -> None:
    """Delete this unit's light curves and its result folder. Nothing else."""
    import shutil
    from .cache import delete_lightcurves
    cache = Path(cache)
    aid = _assignment_id(unit)
    if aid is not None:
        shutil.rmtree(cache / "results" / f"a{aid}", ignore_errors=True)
    try:
        sector = int(unit.get("sector") or 0)
        tics = {int(tic) for tic in (unit.get("tics") or [])}
    except (TypeError, ValueError):
        return
    if sector and tics:
        delete_lightcurves(cache / "data" / f"s{sector:04d}", tics)


def clear_unit(cache: Path, assignment_id: int) -> dict:
    """Delete one finished unit. Refuses when the coordinator has not confirmed it."""
    aid = int(assignment_id)
    units = load_units(cache)
    found = None
    for row in units:
        if _assignment_id(row) == aid:
            found = row
            break
    if found is None:
        return {"ok": False, "error": "not found"}
    reason = clear_refusal(cache, found)
    if reason:
        return {"ok": False, "error": reason, "assignment_id": aid}
    delete_unit_files(cache, found)
    try:
        from .live import delete_unit_stars
        delete_unit_stars(cache, int(found.get("sector") or 0), int(found.get("unit_id") or 0))
    except Exception:
        pass
    save_units(cache, [row for row in units if _assignment_id(row) != aid])
    return {"ok": True, "assignment_id": aid, "cleared": 1}


def clear_finished(cache: Path) -> dict:
    """Delete every finished unit the coordinator has confirmed. Skip the rest."""
    cleared = []
    skipped = []
    for row in list(load_units(cache)):
        aid = _assignment_id(row)
        if aid is None:
            continue
        result = clear_unit(cache, aid)
        if result.get("ok"):
            cleared.append(aid)
        else:
            skipped.append({"assignment_id": aid, "error": result.get("error")})
    return {"ok": True, "cleared": cleared, "skipped": skipped}


def _live_rows(cache: Path) -> list[dict]:
    rows = []
    for name in ("state.json", "next.json"):
        data = _read_json(Path(cache) / name)
        if isinstance(data, dict) and data.get("assignment_id") is not None:
            rows.append(data)
    queued = _read_json(Path(cache) / "queue.json")
    items = queued.get("units") if isinstance(queued, dict) else queued
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("assignment_id") is not None:
                rows.append(item)
    return rows


def collect_units(cache: Path) -> list[dict]:
    """Ledger plus anything state/queue files still describe. Finished rows stay."""
    merged: dict[int, dict] = {}
    order: list[int] = []

    def take(row: dict) -> None:
        aid = _assignment_id(row)
        if aid is None:
            return
        if aid not in merged:
            merged[aid] = {"assignment_id": aid, "project": row.get("project") or "exoplanets"}
            order.append(aid)
        current = merged[aid]
        for key, value in row.items():
            if value is not None:
                current[key] = value

    for row in load_units(cache):
        take(row)
    for row in _live_rows(cache):
        take(row)
    out = []
    for aid in order:
        row = merged[aid]
        row["status"] = display_status(row.get("stage"))
        row["bytes"] = unit_disk_bytes(cache, row)
        row["clear_ok"] = clear_refusal(cache, row) is None
        out.append(row)
    return out


def holdings_view(cache: Path, *, configured_cap_gb: float, queue_max: int, min_free_gb: float) -> dict:
    units = collect_units(cache)
    cap = runtime_cap_gb(cache, configured_cap_gb)
    used = disk_used_bytes(cache)
    try:
        from .live import star_store_stats
        records = star_store_stats(cache)
    except Exception:
        records = {
            "stars": 0, "full": 0, "summaries": 0, "candidates": 0,
            "bytes": 0, "limit_bytes": 0, "summary_limit": 0,
        }
    return {
        "units": units,
        "disk_bytes": used,
        "cap_gb": cap,
        "cap_bytes": int(cap * 1e9),
        "floor_gb": free_floor_gb(min_free_gb),
        "queue_max": int(queue_max),
        "scanning_id": scanning_assignment_id(cache),
        "records": records,
    }
