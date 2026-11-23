"""Local dashboard state for one worker. The worker writes it; the dashboard only reads.

The file is ``dashboard.sqlite`` in the worker cache. Search processes write one
finished star into the ``stars`` table; a separate pointer names the star that
is still running. The parent process publishes batch progress, the log, and
lifetime counters. Nothing in this file is sent to the coordinator, and the
invite token is never stored.

Writes are short and off the BLS hot path (one small transaction per star).
A failure here must not fail the search: callers swallow errors.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from rhexas import __version__

DB_NAME = "dashboard.sqlite"
LOG_NAME = "worker.log"
CANDIDATE_LABEL = "candidates, unverified"
CURVE_POINTS = 280
FOLD_BINS = 72
_CANDIDATE_CAP = 200
_LOG_KEEP = 500_000
_LOG_ROTATE = 2_000_000

_TOKEN = re.compile(r"rhx_[A-Za-z0-9_-]+")
_SECRET_KEYS = {"token", "token_file", "authorization", "password", "secret", "cookie"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    project TEXT NOT NULL,
    sector INTEGER NOT NULL,
    unit_id INTEGER NOT NULL,
    tic INTEGER NOT NULL,
    period REAL,
    depth REAL,
    snr REAL,
    sde REAL,
    phase_json TEXT NOT NULL,
    flux_json TEXT NOT NULL,
    found_at REAL NOT NULL,
    status TEXT,
    PRIMARY KEY (project, sector, unit_id, tic)
);
"""

_EMPTY_LIFE = {
    "stars_searched": 0,
    "batches_submitted": 0,
    "batches_agreed": 0,
    "batches_verified": 0,
    "contributions_pending": 0,
    "contributions_verified": 0,
}

_SAMPLE = {
    "path": "",
    "at": 0.0,
    "walk_at": 0.0,
    "cache_bytes": 0,
    "cpu_percent": None,
    "net_recv": None,
    "net_sent": None,
    "net_recv_bps": None,
    "net_sent_bps": None,
}

PACE_MODES = ("eco", "live", "turbo")


def pace_settings(mode: str, baseline: dict | None) -> dict:
    """Picture speed does not change the worker.

    Eco, live, turbo, and Slow, Normal, Realtime are animation names. The worker
    keeps the procs, threads, and priority it was started with. ``paused`` stays
    false so a picture-speed request cannot freeze the batch. An unknown name is
    stored as live.
    """
    base = baseline if isinstance(baseline, dict) else {}
    try:
        procs = max(1, int(base.get("procs") or 1))
    except (TypeError, ValueError):
        procs = 1
    try:
        threads = max(1, int(base.get("threads") or 1))
    except (TypeError, ValueError):
        threads = 1
    priority = str(base.get("priority") or "low").lower()
    if priority not in ("idle", "low", "normal"):
        priority = "low"
    name = mode if mode in PACE_MODES else "live"
    return {"mode": name, "procs": procs, "threads": threads, "priority": priority, "paused": False}


def pause_view(user_paused: bool, holding: bool) -> dict:
    """What the Running/Paused switch should show.

    A leftover ``holding`` flag must not keep the switch on Paused after resume.
    Pausing means the flag is set and the worker has not reached a wait yet.
    """
    if not user_paused:
        return {"state": "running", "label": "Running", "on": True}
    if holding:
        return {"state": "paused", "label": "Paused", "on": False}
    return {"state": "pausing", "label": "Pausing…", "on": False}


def redact(text: str) -> str:
    """Replace invite tokens. Tokens are ``rhx_`` plus URL-safe characters."""
    return _TOKEN.sub("rhx_redacted", text)


def scrub(value):
    """Drop secret keys and redact tokens anywhere in a JSON-like value."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if str(key).lower() in _SECRET_KEYS:
                continue
            out[str(key)] = scrub(item)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


def public_coordinator_url(url: str) -> str:
    """Coordinator URL safe to show: no userinfo, query, or fragment."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    host = parts.hostname or ""
    if not host:
        return ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


def limits_from(filt) -> dict:
    """Plain numbers the search processes can publish without the config object."""
    return {
        "sde_min": float(filt.sde_min),
        "snr_min": float(filt.snr_min),
        "min_transits": float(filt.min_transits),
        "max_depth": float(filt.max_depth),
        "odd_even_max_sigma": float(filt.odd_even_max_sigma),
        "secondary_max_sigma": float(filt.secondary_max_sigma),
        "max_duration_fraction": float(filt.max_duration_fraction),
    }


def live_spec(cache: Path, filt, assignment: dict) -> dict:
    """Picklable payload for the search pool. No token."""
    spec = {
        "db": str(Path(cache) / DB_NAME),
        "project": str(assignment.get("project") or "exoplanets"),
        "sector": int(assignment.get("sector") or 0),
        "unit_id": int(assignment.get("unit_id") or 0),
    }
    spec.update(limits_from(filt))
    return spec


def numeric_pass(row: dict, limits: dict) -> bool:
    """Same numeric cuts as ``filter.apply_thresholds``. Missing values do not pass."""
    try:
        sde = float(row["sde"])
        snr = float(row["snr"])
        n_transits = float(row["n_transits"])
        depth = float(row["depth"])
        odd = float(row["odd_even_sigma"])
        sec = float(row["sec_sigma"])
        duration = float(row["duration"])
        period = float(row["period"])
    except (KeyError, TypeError, ValueError):
        return False
    nums = (sde, snr, n_transits, depth, odd, sec, duration, period)
    if not all(math.isfinite(n) for n in nums) or period <= 0:
        return False
    try:
        return (
            sde >= float(limits["sde_min"])
            and snr >= float(limits["snr_min"])
            and n_transits >= float(limits["min_transits"])
            and depth > 0
            and depth <= float(limits["max_depth"])
            and odd <= float(limits["odd_even_max_sigma"])
            and abs(sec) <= float(limits["secondary_max_sigma"])
            and duration / period <= float(limits["max_duration_fraction"])
        )
    except (KeyError, TypeError, ValueError):
        return False


def downsample(time_values, flux_values, n: int = CURVE_POINTS) -> tuple[list[float], list[float]]:
    """Evenly pick ``n`` finite samples. Short curves are kept as they are."""
    import numpy as np

    t = np.asarray(time_values, dtype=float)
    y = np.asarray(flux_values, dtype=float)
    if t.shape != y.shape or t.size == 0:
        return [], []
    mask = np.isfinite(t) & np.isfinite(y)
    t, y = t[mask], y[mask]
    if t.size == 0:
        return [], []
    if t.size > n:
        idx = np.linspace(0, t.size - 1, n).astype(int)
        t, y = t[idx], y[idx]
    return [round(float(v), 5) for v in t], [round(float(v), 6) for v in y]


def fold_curve(time_values, flux_values, period, t0, bins: int = FOLD_BINS) -> tuple[list[float], list[float]]:
    """Median flux in phase bins centered on mid-transit. Empty when there is no period."""
    import numpy as np

    try:
        period_f = float(period)
        t0_f = float(t0)
    except (TypeError, ValueError):
        return [], []
    if not math.isfinite(period_f) or not math.isfinite(t0_f) or period_f <= 0:
        return [], []
    t = np.asarray(time_values, dtype=float)
    y = np.asarray(flux_values, dtype=float)
    if t.shape != y.shape or t.size < 4:
        return [], []
    mask = np.isfinite(t) & np.isfinite(y)
    t, y = t[mask], y[mask]
    if t.size < 4:
        return [], []
    phase = np.mod(t - t0_f, period_f) / period_f
    phase = np.where(phase > 0.5, phase - 1.0, phase)
    edges = np.linspace(-0.5, 0.5, bins + 1)
    idx = np.digitize(phase, edges) - 1
    centers: list[float] = []
    medians: list[float] = []
    for i in range(bins):
        sel = y[idx == i]
        if sel.size == 0:
            continue
        centers.append(round(float((edges[i] + edges[i + 1]) / 2), 5))
        medians.append(round(float(np.median(sel)), 6))
    return centers, medians


def _hours_pack(hours, flux, bins: int = 48):
    """Binned hours/flux pairs in the shape the mission-control charts read."""
    import numpy as np

    x = np.asarray(hours, dtype=float)
    y = np.asarray(flux, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    x, y = x[mask], y[mask]
    if x.size < 3:
        return None
    order = np.argsort(x)
    x, y = x[order], y[order]
    edges = np.linspace(float(x[0]), float(x[-1]), bins + 1)
    if edges[-1] == edges[0]:
        return None
    idx = np.digitize(x, edges) - 1
    xb: list[float] = []
    yb: list[float] = []
    for i in range(bins):
        sel = y[idx == i]
        if sel.size == 0:
            continue
        xb.append(round(float((edges[i] + edges[i + 1]) / 2), 3))
        yb.append(round(float(np.median(sel)), 6))
    if len(xb) < 3:
        return None
    return [xb, yb, xb, yb]


def vet_views(time_values, flux_values, period, t0, duration) -> dict:
    """Odd, even, and secondary folds in hours. Cheap; the light curve is already in memory."""
    import numpy as np

    try:
        period_f = float(period)
        t0_f = float(t0)
    except (TypeError, ValueError):
        return {}
    if not math.isfinite(period_f) or not math.isfinite(t0_f) or period_f <= 0:
        return {}
    try:
        dur = float(duration)
    except (TypeError, ValueError):
        dur = 0.1
    if not math.isfinite(dur) or dur <= 0:
        dur = 0.1
    t = np.asarray(time_values, dtype=float)
    y = np.asarray(flux_values, dtype=float)
    if t.shape != y.shape or t.size < 8:
        return {}
    mask = np.isfinite(t) & np.isfinite(y)
    t, y = t[mask], y[mask]
    if t.size < 8:
        return {}
    phase = np.mod(t - t0_f, period_f) / period_f
    phase = np.where(phase > 0.5, phase - 1.0, phase)
    hours = phase * period_f * 24.0
    n_orbit = np.round((t - t0_f) / period_f)
    half = min(0.5, max(4.0 * dur / period_f, 0.02))
    window = np.abs(phase) < half
    odd = _hours_pack(hours[window & (n_orbit % 2 == 1)], y[window & (n_orbit % 2 == 1)])
    even = _hours_pack(hours[window & (n_orbit % 2 == 0)], y[window & (n_orbit % 2 == 0)])
    around_half = np.mod(t - t0_f, period_f) - 0.5 * period_f
    hours2 = around_half * 24.0
    span = min(0.5 * period_f, max(4.0 * dur, 0.02 * period_f))
    sec_m = np.abs(around_half) < span
    secondary = _hours_pack(hours2[sec_m], y[sec_m])
    return {"odd": odd, "even": even, "secondary": secondary}


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _tic(row: dict) -> int | None:
    try:
        return int(row.get("tic"))
    except (TypeError, ValueError):
        return None


def _json_ready(value):
    """Plain JSON types only. Non-finite numbers become null."""
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        try:
            return _json_ready(value.item())
        except Exception:
            return None
    return str(value)


def _dumps(value) -> str:
    return json.dumps(scrub(_json_ready(value)), separators=(",", ":"), allow_nan=False)


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    if write:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        from .records import migrate
        migrate(conn)
    else:
        conn.execute("PRAGMA query_only=ON")
    return conn


class Store:
    """Short transactions against ``dashboard.sqlite``. Safe to call from a heartbeat thread.

    Each call opens its own connection so a forked search process does not inherit one.
    """

    def __init__(self, cache: Path):
        self.cache = Path(cache)
        self.path = self.cache / DB_NAME
        self._lock = threading.Lock()
        with self._locked() as conn:
            conn.execute("SELECT 1 FROM kv").fetchone()

    def _locked(self):
        return _Locked(self)

    def set_worker(self, *, pid: int, procs: int, threads: int, priority: str, max_cache_gb: float,
                   coordinator_url: str) -> None:
        with self._locked() as conn:
            previous = _read_kv(conn, "worker") or {}
            if not isinstance(previous, dict):
                previous = {}
            stored = previous.get("baseline")
            if not isinstance(stored, dict):
                stored = {"procs": int(procs), "threads": int(threads), "priority": str(priority)}
            _write_kv(conn, "worker", {
                "pid": int(pid),
                "procs": int(procs),
                "threads": int(threads),
                "priority": str(priority),
                "max_cache_gb": float(max_cache_gb),
                "coordinator_url": public_coordinator_url(coordinator_url),
                "baseline": {
                    "procs": int(stored.get("procs") or procs),
                    "threads": int(stored.get("threads") or threads),
                    "priority": str(stored.get("priority") or priority),
                },
                "mode": "live",
                "paused": False,
            })

    def request_mode(self, mode: str) -> str:
        """Accept a mode name from an older client. The worker does not read it.

        The tray no longer writes this. ``/api/mode`` still stores eco, live, or
        turbo so an old page does not get an error. Pause is a different field.
        """
        if mode not in PACE_MODES:
            raise ValueError("mode must be eco, live, or turbo")
        with self._locked() as conn:
            row = _read_kv(conn, "control") or {}
            if not isinstance(row, dict):
                row = {}
            row["mode"] = mode
            row["at"] = time.time()
            _write_kv(conn, "control", row)
        return mode

    def requested_mode(self) -> str:
        with self._locked() as conn:
            row = _read_kv(conn, "control")
        if isinstance(row, dict) and row.get("mode") in PACE_MODES:
            return str(row["mode"])
        return "live"

    def request_user_pause(self, paused: bool) -> bool:
        """User pause. This is not an animation speed: it freezes the worker."""
        with self._locked() as conn:
            row = _read_kv(conn, "control") or {}
            if not isinstance(row, dict):
                row = {}
            row["user_paused"] = bool(paused)
            row["pause_at"] = time.time()
            if not paused:
                # Resume must not wait for the worker to notice a stale hold.
                row["holding"] = False
            _write_kv(conn, "control", row)
        return bool(paused)

    def user_paused(self) -> bool:
        with self._locked() as conn:
            row = _read_kv(conn, "control")
        return bool(isinstance(row, dict) and row.get("user_paused"))

    def set_holding(self, holding: bool) -> None:
        """The worker is actually waiting on the user pause, not merely asked to."""
        with self._locked() as conn:
            row = _read_kv(conn, "control") or {}
            if not isinstance(row, dict):
                row = {}
            if bool(row.get("holding")) == bool(holding):
                return
            row["holding"] = bool(holding)
            row["holding_at"] = time.time()
            _write_kv(conn, "control", row)

    def baseline(self) -> dict:
        with self._locked() as conn:
            worker = _read_kv(conn, "worker") or {}
        stored = worker.get("baseline") if isinstance(worker, dict) else None
        return stored if isinstance(stored, dict) else {}

    def publish_pace(self, settings: dict) -> None:
        """Record the load the worker is actually using. The baseline stays put."""
        with self._locked() as conn:
            worker = _read_kv(conn, "worker") or {}
            if not isinstance(worker, dict):
                worker = {}
            worker["procs"] = int(settings["procs"])
            worker["threads"] = int(settings["threads"])
            worker["priority"] = str(settings["priority"])
            worker["mode"] = str(settings["mode"]) if settings.get("mode") in PACE_MODES else "live"
            worker["paused"] = bool(settings.get("paused"))
            _write_kv(conn, "worker", worker)

    def set_pid(self, pid: int) -> None:
        with self._locked() as conn:
            worker = _read_kv(conn, "worker") or {}
            worker["pid"] = int(pid)
            _write_kv(conn, "worker", worker)

    def set_connection(self, ok: bool | None, detail: str, kind: str | None = None) -> None:
        text = redact(str(detail or ""))[:160]
        low = text.lower()
        if not kind:
            if low.startswith("unreachable"):
                kind = "unreachable"
            elif low.startswith("network"):
                kind = "network"
            elif low.startswith("rejected"):
                kind = "rejected"
            elif low.startswith("lease ended"):
                kind = "lease_ended"
            elif ok is True:
                kind = "connected"
            elif ok is False:
                kind = "unreachable"
            else:
                kind = "unknown"
        with self._locked() as conn:
            _write_kv(conn, "connection", {"ok": ok, "detail": text, "kind": str(kind), "at": time.time()})

    def set_next(self, assignment: dict, *, stage: str = "downloading", stars_done: int = 0,
                 stars_total: int | None = None) -> None:
        """The batch downloading ahead of the one on screen. ``ready`` means its files are in the cache."""
        tics = assignment.get("tics") or []
        total = int(assignment.get("n_tics") or len(tics)) if stars_total is None else int(stars_total)
        payload = {
            "active": True,
            "project": str(assignment.get("project") or "exoplanets"),
            "sector": int(assignment.get("sector") or 0),
            "unit_id": int(assignment.get("unit_id") or 0),
            "assignment_id": int(assignment.get("assignment_id") or 0),
            "stage": str(stage),
            "stars_done": int(stars_done),
            "stars_total": total,
        }
        with self._locked() as conn:
            _write_kv(conn, "next", payload)

    def update_next(self, **fields) -> None:
        with self._locked() as conn:
            nxt = _read_kv(conn, "next") or {}
            if not isinstance(nxt, dict):
                nxt = {}
            nxt.update(fields)
            nxt["active"] = True
            nxt["updated"] = time.time()
            _write_kv(conn, "next", nxt)

    def clear_next(self) -> None:
        with self._locked() as conn:
            _write_kv(conn, "next", None)

    def begin_batch(self, assignment: dict, stage: str = "claimed") -> None:
        tics = assignment.get("tics") or []
        batch = {
            "active": True,
            "project": str(assignment.get("project") or "exoplanets"),
            "sector": int(assignment.get("sector") or 0),
            "unit_id": int(assignment.get("unit_id") or 0),
            "assignment_id": int(assignment.get("assignment_id") or 0),
            "stage": stage,
            "progress_kind": "stars",
            "stars_done": 0,
            "stars_total": int(assignment.get("n_tics") or len(tics)),
            "tics": [int(t) for t in tics],
            "eta_min": None,
            "per_s": None,
            "started_at": time.time(),
        }
        with self._locked() as conn:
            _write_kv(conn, "batch", batch)
            _write_kv(conn, "current", None)
            try:
                from .batch_index import register_unit
                register_unit(
                    conn,
                    sector=batch["sector"],
                    unit=batch["unit_id"],
                    project=batch["project"],
                    assignment_id=batch["assignment_id"],
                    tics=[int(t) for t in tics],
                    started_at=batch["started_at"],
                )
            except Exception:
                pass

    def update_batch(self, **fields) -> None:
        with self._locked() as conn:
            batch = _read_kv(conn, "batch") or {}
            batch.update(fields)
            batch["updated"] = time.time()
            _write_kv(conn, "batch", batch)
            stage = str(batch.get("stage") or "").lower()
            if stage in {"uploaded", "waiting", "done"}:
                try:
                    from .batch_index import mark_unit_finished
                    mark_unit_finished(conn, int(batch.get("sector") or 0), int(batch.get("unit_id") or 0))
                except Exception:
                    pass

    def mark_waiting(self) -> None:
        with self._locked() as conn:
            batch = _read_kv(conn, "batch") or {}
            batch["active"] = False
            batch["stage"] = "waiting"
            batch["eta_min"] = None
            batch["per_s"] = None
            batch["updated"] = time.time()
            _write_kv(conn, "batch", batch)

    def note_submit(self, result: dict | None, n_stars: int) -> None:
        """Count one upload. Agreement is whatever the coordinator said in this response."""
        result = result if isinstance(result, dict) else {}
        verdict = str(result.get("verdict") or "").lower()
        verified = verdict == "agreed" or result.get("verified") is True
        with self._locked() as conn:
            life = dict(_EMPTY_LIFE)
            life.update(_read_kv(conn, "lifetime") or {})
            life["batches_submitted"] = int(life.get("batches_submitted") or 0) + 1
            life["stars_searched"] = int(life.get("stars_searched") or 0) + max(int(n_stars), 0)
            if verdict == "agreed":
                life["batches_agreed"] = int(life.get("batches_agreed") or 0) + 1
            if verified:
                life["batches_verified"] = int(life.get("batches_verified") or 0) + 1
                life["contributions_verified"] = int(life.get("contributions_verified") or 0) + 1
            elif verdict == "pending":
                life["contributions_pending"] = int(life.get("contributions_pending") or 0) + 1
            _write_kv(conn, "lifetime", {key: int(life.get(key) or 0) for key in _EMPTY_LIFE})

    def record_candidates(self, assignment: dict, rows: list[dict]) -> None:
        """Keep the official candidate list for this unit, preserving folds already published."""
        from .records import apply_candidate_list
        project = str(assignment.get("project") or "exoplanets")
        sector = int(assignment.get("sector") or 0)
        unit_id = int(assignment.get("unit_id") or 0)
        with self._locked() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                keep: list[int] = []
                for row in rows:
                    tic = _tic(row)
                    if tic is None:
                        continue
                    keep.append(tic)
                    prev = conn.execute(
                        """SELECT phase_json, flux_json FROM candidates
                           WHERE project=? AND sector=? AND unit_id=? AND tic=?""",
                        (project, sector, unit_id, tic),
                    ).fetchone()
                    phase = prev["phase_json"] if prev else "[]"
                    flux = prev["flux_json"] if prev else "[]"
                    status = str(row.get("status") or "NEW")
                    conn.execute(
                        """INSERT INTO candidates (
                               project, sector, unit_id, tic, period, depth, snr, sde,
                               phase_json, flux_json, found_at, status
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(project, sector, unit_id, tic) DO UPDATE SET
                               period=excluded.period, depth=excluded.depth, snr=excluded.snr,
                               sde=excluded.sde, phase_json=excluded.phase_json,
                               flux_json=excluded.flux_json, found_at=excluded.found_at,
                               status=excluded.status""",
                        (
                            project, sector, unit_id, tic,
                            _finite(row.get("period")), _finite(row.get("depth")),
                            _finite(row.get("snr")), _finite(row.get("sde")),
                            phase, flux, time.time(), status,
                        ),
                    )
                if keep:
                    marks = ",".join("?" * len(keep))
                    conn.execute(
                        f"""DELETE FROM candidates
                            WHERE project=? AND sector=? AND unit_id=? AND tic NOT IN ({marks})""",
                        (project, sector, unit_id, *keep),
                    )
                else:
                    conn.execute(
                        "DELETE FROM candidates WHERE project=? AND sector=? AND unit_id=?",
                        (project, sector, unit_id),
                    )
                _cap_candidates(conn)
                apply_candidate_list(conn, sector, unit_id, rows)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise


class _Locked:
    def __init__(self, store: Store):
        self._store = store
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        self._store._lock.acquire()
        try:
            self._conn = _connect(self._store.path, write=True)
        except Exception:
            self._store._lock.release()
            raise
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        finally:
            self._store._lock.release()


def _read_kv(conn: sqlite3.Connection, key: str):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return None


def _write_kv(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, _dumps(value)),
    )


def _cap_candidates(conn: sqlite3.Connection) -> None:
    conn.execute(
        """DELETE FROM candidates WHERE rowid IN (
               SELECT rowid FROM candidates ORDER BY found_at DESC LIMIT -1 OFFSET ?
           )""",
        (_CANDIDATE_CAP,),
    )


_CHILD: dict[str, sqlite3.Connection | None] = {"conn": None, "path": None}


def _child_conn(path: str) -> sqlite3.Connection:
    """One connection per search process. Never the parent's connection."""
    if _CHILD["conn"] is None or _CHILD["path"] != path:
        if _CHILD["conn"] is not None:
            _CHILD["conn"].close()
        _CHILD["conn"] = _connect(Path(path), write=True)
        _CHILD["path"] = path
    return _CHILD["conn"]


_HIST_BINS = 60
# The page list is not the counter. Counters use SQL. This only bounds the card payload.
_CANDIDATE_LIST = 400


def _bump_hist(conn: sqlite3.Connection, sde, snr) -> None:
    hist = _read_kv(conn, "hist")
    if not isinstance(hist, dict):
        hist = {}
    sde_c = [int(v or 0) for v in list(hist.get("sde") or [])]
    snr_c = [int(v or 0) for v in list(hist.get("snr") or [])]
    sde_c = (sde_c + [0] * _HIST_BINS)[:_HIST_BINS]
    snr_c = (snr_c + [0] * _HIST_BINS)[:_HIST_BINS]

    def bump(counts, value, span: float) -> None:
        number = _finite(value)
        if number is None:
            return
        clamped = min(span - 1e-6, max(0.0, number))
        counts[int(clamped / span * _HIST_BINS)] += 1

    bump(sde_c, sde, 30.0)
    bump(snr_c, snr, 60.0)
    _write_kv(conn, "hist", {"sde": sde_c, "snr": snr_c})


def publish_star(spec: dict, row: dict, time_values, flux_values, *, processing: bool) -> None:
    """Record one star. A star that is still in BLS only moves the processing pointer.

    The finished row is written once BLS returns, so a second process cannot replace
    a finished light curve with a half-done one. Called from pool workers. Exceptions
    propagate to the caller, which ignores them.
    """
    from .records import build_record, pointer, retain, upsert_star

    path = spec.get("db")
    if not path:
        return
    tic = _tic(row)
    conn = _child_conn(str(path))
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if processing:
                if tic is not None:
                    _write_kv(conn, "processing", {
                        "tic": int(tic),
                        "sector": int(spec.get("sector") or 0),
                        "unit": int(spec.get("unit_id") or 0),
                        "at": time.time(),
                    })
                conn.execute("COMMIT")
                return
            times: list[float] = []
            flux: list[float] = []
            if time_values is not None and flux_values is not None:
                times, flux = downsample(time_values, flux_values)
            phase: list[float] = []
            folded: list[float] = []
            passed = False
            curves: dict = {}
            if not row.get("error") and times:
                phase, folded = fold_curve(time_values, flux_values, row.get("period"), row.get("t0"))
                passed = numeric_pass(row, spec)
                try:
                    curves = vet_views(
                        time_values, flux_values, row.get("period"), row.get("t0"), row.get("duration"),
                    )
                except Exception:
                    curves = {}
            record = build_record(
                spec, row, times, flux, phase, folded, curves, passed=passed, at=time.time(),
            )
            if record.get("error"):
                record["error"] = redact(str(record["error"]))[:200]
            if tic is None:
                conn.execute("COMMIT")
                return
            record = upsert_star(conn, record, _dumps)
            retain(conn)
            _write_kv(conn, "display", pointer(record))
            _bump_hist(conn, record.get("sde"), record.get("snr"))
            processing_now = _read_kv(conn, "processing")
            if isinstance(processing_now, dict) and processing_now.get("tic") == int(tic):
                _write_kv(conn, "processing", None)
            if passed:
                conn.execute(
                    """INSERT INTO candidates (
                           project, sector, unit_id, tic, period, depth, snr, sde,
                           phase_json, flux_json, found_at, status
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(project, sector, unit_id, tic) DO UPDATE SET
                           period=excluded.period, depth=excluded.depth, snr=excluded.snr,
                           sde=excluded.sde, phase_json=excluded.phase_json,
                           flux_json=excluded.flux_json, found_at=excluded.found_at,
                           status=excluded.status""",
                    (
                        record["project"], record["sector"], record["unit"], int(tic),
                        record["period"], record["depth"], record["snr"], record["sde"],
                        _dumps(phase), _dumps(folded), record["at"], "NEW",
                    ),
                )
                _cap_candidates(conn)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    except sqlite3.Error:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _CHILD["conn"] = None
        _CHILD["path"] = None
        raise


def _batch_stage(batch: dict) -> str:
    stages = {
        "claimed": "Downloading",
        "downloading": "Downloading",
        "downloaded": "Searching",
        "searching": "Searching",
        "searched": "Filtering",
        "filtering": "Filtering",
        "filtered": "Filtering",
        "uploading": "Filtering",
        "uploaded": "Done",
        "waiting": "Done",
    }
    return stages.get(str((batch or {}).get("stage") or "").lower(), "Starting")


def _read_db(path: Path) -> dict:
    from .records import candidate_stats, latest_display, sky_rev, store_stats, table_ready

    empty = {
        "kv": {}, "candidates": [], "current": None, "stats": {"rows": 0, "new": 0, "known": 0},
        "store": {}, "sky_rev": "0:0",
    }
    if not path.is_file():
        return empty
    conn = _connect(path, write=False)
    try:
        kv = {}
        for row in conn.execute("SELECT key, value FROM kv"):
            try:
                kv[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                continue
        names = {row[1] for row in conn.execute("PRAGMA table_info(candidates)")}
        candidates = []
        if names:
            status_sql = "status" if "status" in names else "NULL AS status"
            for row in conn.execute(
                f"""SELECT project, sector, unit_id, tic, period, depth, snr, sde,
                           phase_json, flux_json, found_at, {status_sql} AS status
                    FROM candidates ORDER BY found_at DESC LIMIT ?""",
                (_CANDIDATE_LIST,),
            ):
                try:
                    phase = json.loads(row["phase_json"])
                    folded = json.loads(row["flux_json"])
                except json.JSONDecodeError:
                    phase, folded = [], []
                status = row["status"] or "NEW"
                candidates.append({
                    "tic": int(row["tic"]),
                    "project": row["project"],
                    "sector": int(row["sector"]),
                    "unit_id": int(row["unit_id"]),
                    "unit": int(row["unit_id"]),
                    "period": row["period"],
                    "depth": row["depth"],
                    "snr": row["snr"],
                    "sde": row["sde"],
                    "phase": phase if isinstance(phase, list) else [],
                    "folded": folded if isinstance(folded, list) else [],
                    "found_at": row["found_at"],
                    "status": status,
                    "label": CANDIDATE_LABEL,
                })
        batch = kv.get("batch") if isinstance(kv.get("batch"), dict) else {}
        stage = _batch_stage(batch) if batch else "Starting"
        current = latest_display(conn, stage=stage, now=time.time()) if table_ready(conn) else None
        return {
            "kv": kv,
            "candidates": candidates,
            "current": current,
            "stats": candidate_stats(conn) if names else {"rows": 0, "new": 0, "known": 0},
            "store": store_stats(conn),
            "sky_rev": sky_rev(conn),
        }
    finally:
        conn.close()


def load_star(cache: Path, tic: int, sector: int | None = None, unit: int | None = None) -> dict:
    """One star for ``/api/star``. Missing stars say so instead of inventing curves."""
    from .records import fetch_star, missing_star

    path = Path(cache) / DB_NAME
    if not path.is_file():
        return missing_star(tic)
    try:
        conn = _connect(path, write=False)
    except sqlite3.Error:
        return missing_star(tic)
    try:
        found = fetch_star(conn, tic, sector, unit)
    finally:
        conn.close()
    return found if found is not None else missing_star(tic)


def star_store_stats(cache: Path) -> dict:
    from .records import RECORD_BUDGET_BYTES, SUMMARY_CAP, store_stats

    blank = {
        "stars": 0, "full": 0, "summaries": 0, "candidates": 0,
        "bytes": 0, "limit_bytes": RECORD_BUDGET_BYTES, "summary_limit": SUMMARY_CAP,
    }
    path = Path(cache) / DB_NAME
    if not path.is_file():
        return blank
    try:
        conn = _connect(path, write=False)
    except sqlite3.Error:
        return blank
    try:
        return store_stats(conn)
    finally:
        conn.close()


def sky_for(cache: Path, sector: int | None) -> dict:
    from .records import sky_payload

    path = Path(cache) / DB_NAME
    if not path.is_file():
        return {"scope": "all" if sector is None else "sector", "sector": sector, "points": [], "bounds": None, "count": 0}
    try:
        conn = _connect(path, write=False)
    except sqlite3.Error:
        return {"scope": "all" if sector is None else "sector", "sector": sector, "points": [], "bounds": None, "count": 0}
    try:
        return sky_payload(conn, sector)
    finally:
        conn.close()


def delete_unit_stars(cache: Path, sector: int, unit: int) -> int:
    """Drop this unit's non-candidate star rows. Candidates stay."""
    from .records import clear_unit_rows

    path = Path(cache) / DB_NAME
    if not path.is_file():
        return 0
    conn = _connect(path, write=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            removed = clear_unit_rows(conn, sector, unit)
            conn.execute("COMMIT")
            return removed
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _sample_machine(cache: Path) -> tuple[int, float | None, float | None, float | None]:
    """Cache size, CPU, and network throughput.

    The directory walk is a few times a minute. CPU and byte counters refresh
    about once a second so a 1–2 s dashboard poll can draw a rate.
    """
    now = time.time()
    key = str(cache)
    same = _SAMPLE["path"] == key
    if same and now - float(_SAMPLE["at"]) < 1.0:
        return (
            int(_SAMPLE["cache_bytes"]),
            _SAMPLE["cpu_percent"],
            _SAMPLE["net_recv_bps"],
            _SAMPLE["net_sent_bps"],
        )
    if (not same) or now - float(_SAMPLE.get("walk_at") or 0) >= 5:
        try:
            from rhexas.worker.cache import directory_bytes
            cache_bytes = int(directory_bytes(cache))
        except OSError:
            cache_bytes = 0
        walk_at = now
    else:
        cache_bytes = int(_SAMPLE["cache_bytes"])
        walk_at = float(_SAMPLE.get("walk_at") or now)
    cpu = None
    recv = sent = None
    try:
        import psutil
        cpu = float(psutil.cpu_percent(interval=None))
        counters = psutil.net_io_counters()
        recv = int(counters.bytes_recv)
        sent = int(counters.bytes_sent)
    except Exception:
        cpu = _SAMPLE["cpu_percent"] if same else None
    recv_bps = sent_bps = None
    if recv is not None and same and _SAMPLE["net_recv"] is not None:
        dt = now - float(_SAMPLE["at"])
        if dt >= 0.4:
            recv_bps = max(0.0, (recv - int(_SAMPLE["net_recv"])) / dt)
            sent_bps = max(0.0, (sent - int(_SAMPLE["net_sent"])) / dt)
        else:
            recv_bps = _SAMPLE["net_recv_bps"]
            sent_bps = _SAMPLE["net_sent_bps"]
    _SAMPLE.update(
        path=key, at=now, walk_at=walk_at, cache_bytes=cache_bytes, cpu_percent=cpu,
        net_recv=recv, net_sent=sent, net_recv_bps=recv_bps, net_sent_bps=sent_bps,
    )
    return cache_bytes, cpu, recv_bps, sent_bps


def _worker_alive(pid) -> bool:
    try:
        pid_i = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid_i <= 0:
        return False
    try:
        import psutil
        return bool(psutil.pid_exists(pid_i))
    except Exception:
        return False


def read_log_tail(cache: Path, *, limit: int = 80) -> tuple[list[str], int]:
    path = Path(cache) / LOG_NAME
    if not path.is_file():
        return [], 0
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > 16_000:
                handle.seek(size - 16_000)
                handle.readline()
            data = handle.read()
    except OSError:
        return [], 0
    text = redact(data.decode("utf-8", "replace"))
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) > limit:
        lines = lines[-limit:]
    return lines, int(size)


def snapshot(cache: Path, *, fallback: dict | None = None) -> dict:
    """Public dashboard payload. Secret keys are removed and tokens are redacted."""
    cache = Path(cache)
    fallback = fallback or {}
    try:
        loaded = _read_db(cache / DB_NAME)
    except sqlite3.Error:
        loaded = {}
    kv = loaded.get("kv") if isinstance(loaded.get("kv"), dict) else {}
    candidates = loaded.get("candidates") if isinstance(loaded.get("candidates"), list) else []
    worker = kv.get("worker") if isinstance(kv.get("worker"), dict) else {}
    batch = kv.get("batch") if isinstance(kv.get("batch"), dict) else None
    nxt = kv.get("next") if isinstance(kv.get("next"), dict) else None
    current = loaded.get("current") if isinstance(loaded.get("current"), dict) else None
    processing = kv.get("processing") if isinstance(kv.get("processing"), dict) else None
    from .records import pointer
    display = pointer(current)
    life = dict(_EMPTY_LIFE)
    if isinstance(kv.get("lifetime"), dict):
        for key in _EMPTY_LIFE:
            try:
                life[key] = int(kv["lifetime"].get(key) or 0)
            except (TypeError, ValueError):
                life[key] = 0
    connection = kv.get("connection") if isinstance(kv.get("connection"), dict) else {
        "ok": None, "detail": "no worker state yet", "at": None,
    }
    control = kv.get("control") if isinstance(kv.get("control"), dict) else {}
    user_paused = bool(control.get("user_paused"))
    holding = bool(control.get("holding")) and user_paused
    cache_bytes, cpu, recv_bps, sent_bps = _sample_machine(cache)
    try:
        from .retention import usage as processed_usage
        configured_limit = fallback.get("processed_limit_gb")
        try:
            configured_limit = float(configured_limit) if configured_limit is not None else None
        except (TypeError, ValueError):
            configured_limit = None
        processed = processed_usage(cache, configured_limit)
    except Exception:
        processed = {"bytes": 0, "limit_gb": 20.0, "limit_bytes": 20_000_000_000, "sectors": []}
    log_tail, log_size = read_log_tail(cache)
    max_gb = worker.get("max_cache_gb", fallback.get("max_cache_gb"))
    try:
        max_gb = float(max_gb) if max_gb is not None else None
    except (TypeError, ValueError):
        max_gb = None
    payload = {
        "version": 1,
        "app_version": __version__,
        "label": CANDIDATE_LABEL,
        "updated": time.time(),
        "batch": batch,
        "next": nxt,
        "current_star": current,
        "display": display,
        "processing": processing,
        "candidate_stats": loaded.get("stats") if isinstance(loaded.get("stats"), dict) else {"rows": 0, "new": 0, "known": 0},
        "records": loaded.get("store") if isinstance(loaded.get("store"), dict) else {},
        "sky_rev": loaded.get("sky_rev") or "0:0",
        "candidates": candidates,
        "lifetime": life,
        "connection": {
            "ok": connection.get("ok"),
            "kind": connection.get("kind"),
            "detail": redact(str(connection.get("detail") or ""))[:160],
            "at": connection.get("at"),
            "url": public_coordinator_url(
                str(worker.get("coordinator_url") or fallback.get("coordinator_url") or "")
            ),
        },
        "resources": {
            "procs": worker.get("procs", fallback.get("procs")),
            "threads": worker.get("threads", fallback.get("threads")),
            "priority": worker.get("priority", fallback.get("priority")),
            "max_cache_gb": max_gb,
            "cache_bytes": cache_bytes,
            "cpu_percent": cpu,
            "net_recv_bps": recv_bps,
            "net_sent_bps": sent_bps,
            "processed": processed,
            "pid": worker.get("pid") or 0,
            "worker_alive": _worker_alive(worker.get("pid")),
            "mode": worker.get("mode") if worker.get("mode") in PACE_MODES else "live",
            "paused": False,
            "user_paused": user_paused,
            "holding": holding,
            "pause": pause_view(user_paused, holding),
        },
        "log_tail": log_tail,
        "log_size": log_size,
        "hist": kv.get("hist") if isinstance(kv.get("hist"), dict) else None,
        "sky": [],
        "views": {},
    }
    return scrub(payload)


class WorkerLog:
    """Append-only console log. The copy on disk is redacted; the terminal is unchanged."""

    def __init__(self, cache: Path):
        self.path = Path(cache) / LOG_NAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = self.path.open("a", encoding="utf-8", errors="replace")
        self._bytes = self.path.stat().st_size if self.path.exists() else 0

    def write(self, data: str) -> None:
        if not data:
            return
        text = redact(data)
        with self._lock:
            self._fh.write(text)
            self._fh.flush()
            self._bytes += len(text)
            if self._bytes >= _LOG_ROTATE:
                self._trim()

    def _trim(self) -> None:
        self._fh.flush()
        self._fh.close()
        try:
            raw = self.path.read_bytes()[-_LOG_KEEP:]
        except OSError:
            raw = b""
        cut = raw.find(b"\n")
        if cut != -1:
            raw = raw[cut + 1:]
        self.path.write_bytes(raw)
        self._fh = self.path.open("a", encoding="utf-8", errors="replace")
        self._bytes = len(raw)

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.close()
            except OSError:
                pass


class Tee:
    """Send a text stream to the original destination and to :class:`WorkerLog`."""

    def __init__(self, stream, log: WorkerLog):
        self._stream = stream
        self._log = log
        self.encoding = getattr(stream, "encoding", "utf-8") or "utf-8"

    def write(self, data) -> int:
        text = data if isinstance(data, str) else str(data)
        try:
            self._stream.write(text)
        except Exception:
            pass
        try:
            self._log.write(text)
        except Exception:
            pass
        return len(text)

    def flush(self) -> None:
        try:
            self._stream.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        try:
            return bool(self._stream.isatty())
        except Exception:
            return False

    def fileno(self) -> int:
        return self._stream.fileno()
