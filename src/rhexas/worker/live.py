"""Local dashboard state for one worker. The worker writes it; the dashboard only reads.

The file is ``dashboard.sqlite`` in the worker cache. Search processes publish the
star they are on; the parent process publishes batch progress, the log, and
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

_SAMPLE = {"path": "", "at": 0.0, "cache_bytes": 0, "cpu_percent": None}


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
            _write_kv(conn, "worker", {
                "pid": int(pid),
                "procs": int(procs),
                "threads": int(threads),
                "priority": str(priority),
                "max_cache_gb": float(max_cache_gb),
                "coordinator_url": public_coordinator_url(coordinator_url),
            })

    def set_pid(self, pid: int) -> None:
        with self._locked() as conn:
            worker = _read_kv(conn, "worker") or {}
            worker["pid"] = int(pid)
            _write_kv(conn, "worker", worker)

    def set_connection(self, ok: bool | None, detail: str) -> None:
        text = redact(str(detail or ""))[:160]
        with self._locked() as conn:
            _write_kv(conn, "connection", {"ok": ok, "detail": text, "at": time.time()})

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
            "eta_min": None,
            "per_s": None,
            "started_at": time.time(),
        }
        with self._locked() as conn:
            _write_kv(conn, "batch", batch)
            _write_kv(conn, "current", None)

    def update_batch(self, **fields) -> None:
        with self._locked() as conn:
            batch = _read_kv(conn, "batch") or {}
            batch.update(fields)
            batch["updated"] = time.time()
            _write_kv(conn, "batch", batch)

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
                    conn.execute(
                        """INSERT INTO candidates (
                               project, sector, unit_id, tic, period, depth, snr, sde,
                               phase_json, flux_json, found_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(project, sector, unit_id, tic) DO UPDATE SET
                               period=excluded.period, depth=excluded.depth, snr=excluded.snr,
                               sde=excluded.sde, phase_json=excluded.phase_json,
                               flux_json=excluded.flux_json, found_at=excluded.found_at""",
                        (
                            project, sector, unit_id, tic,
                            _finite(row.get("period")), _finite(row.get("depth")),
                            _finite(row.get("snr")), _finite(row.get("sde")),
                            phase, flux, time.time(),
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


def publish_star(spec: dict, row: dict, time_values, flux_values, *, processing: bool) -> None:
    """Write the star a search process is on, and keep a fold if the numeric cuts pass.

    Called from pool workers. Opens the database itself so it does not share a connection
    with the parent. Exceptions propagate to the caller, which ignores them.
    """
    path = spec.get("db")
    if not path:
        return
    tic = _tic(row)
    times, flux = ([], [])
    if time_values is not None and flux_values is not None:
        times, flux = downsample(time_values, flux_values)
    phase: list[float] = []
    folded: list[float] = []
    passed = False
    if not processing and not row.get("error") and times:
        phase, folded = fold_curve(time_values, flux_values, row.get("period"), row.get("t0"))
        passed = numeric_pass(row, spec)
    current = {
        "tic": tic,
        "file": str(row.get("file") or ""),
        "status": "processing" if processing else ("error" if row.get("error") else "done"),
        "error": redact(str(row.get("error")))[:200] if row.get("error") else None,
        "period": _finite(row.get("period")),
        "t0": _finite(row.get("t0")),
        "depth": _finite(row.get("depth")),
        "snr": _finite(row.get("snr")),
        "sde": _finite(row.get("sde")),
        "time": times,
        "flux": flux,
        "phase": phase,
        "folded": folded,
        "project": spec.get("project") or "exoplanets",
        "sector": int(spec.get("sector") or 0),
        "unit_id": int(spec.get("unit_id") or 0),
        "at": time.time(),
    }
    conn = _child_conn(str(path))
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _write_kv(conn, "current", current)
            if passed and tic is not None and phase:
                conn.execute(
                    """INSERT INTO candidates (
                           project, sector, unit_id, tic, period, depth, snr, sde,
                           phase_json, flux_json, found_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(project, sector, unit_id, tic) DO UPDATE SET
                           period=excluded.period, depth=excluded.depth, snr=excluded.snr,
                           sde=excluded.sde, phase_json=excluded.phase_json,
                           flux_json=excluded.flux_json, found_at=excluded.found_at""",
                    (
                        current["project"], current["sector"], current["unit_id"], tic,
                        current["period"], current["depth"], current["snr"], current["sde"],
                        _dumps(phase), _dumps(folded), current["at"],
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


def _read_db(path: Path) -> tuple[dict, list[dict]]:
    if not path.is_file():
        return {}, []
    conn = _connect(path, write=False)
    try:
        kv = {}
        for row in conn.execute("SELECT key, value FROM kv"):
            try:
                kv[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                continue
        candidates = []
        for row in conn.execute(
            "SELECT * FROM candidates ORDER BY found_at DESC LIMIT 60"
        ):
            try:
                phase = json.loads(row["phase_json"])
                folded = json.loads(row["flux_json"])
            except json.JSONDecodeError:
                phase, folded = [], []
            candidates.append({
                "tic": int(row["tic"]),
                "project": row["project"],
                "sector": int(row["sector"]),
                "unit_id": int(row["unit_id"]),
                "period": row["period"],
                "depth": row["depth"],
                "snr": row["snr"],
                "sde": row["sde"],
                "phase": phase if isinstance(phase, list) else [],
                "folded": folded if isinstance(folded, list) else [],
                "found_at": row["found_at"],
                "label": CANDIDATE_LABEL,
            })
        return kv, candidates
    finally:
        conn.close()


def _sample_machine(cache: Path) -> tuple[int, float | None]:
    """Cache size and system CPU, refreshed a few times a minute so the walk stays cheap."""
    now = time.time()
    key = str(cache)
    if _SAMPLE["path"] == key and now - _SAMPLE["at"] < 5:
        return int(_SAMPLE["cache_bytes"]), _SAMPLE["cpu_percent"]
    cache_bytes = 0
    cpu = None
    try:
        from rhexas.worker.cache import directory_bytes
        cache_bytes = int(directory_bytes(cache))
    except OSError:
        cache_bytes = 0
    try:
        import psutil
        cpu = float(psutil.cpu_percent(interval=None))
    except Exception:
        cpu = None
    _SAMPLE.update(path=key, at=now, cache_bytes=cache_bytes, cpu_percent=cpu)
    return cache_bytes, cpu


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
        kv, candidates = _read_db(cache / DB_NAME)
    except sqlite3.Error:
        kv, candidates = {}, []
    worker = kv.get("worker") if isinstance(kv.get("worker"), dict) else {}
    batch = kv.get("batch") if isinstance(kv.get("batch"), dict) else None
    current = kv.get("current") if isinstance(kv.get("current"), dict) else None
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
    cache_bytes, cpu = _sample_machine(cache)
    log_tail, log_size = read_log_tail(cache)
    max_gb = worker.get("max_cache_gb", fallback.get("max_cache_gb"))
    try:
        max_gb = float(max_gb) if max_gb is not None else None
    except (TypeError, ValueError):
        max_gb = None
    payload = {
        "version": 1,
        "label": CANDIDATE_LABEL,
        "updated": time.time(),
        "batch": batch,
        "current_star": current,
        "candidates": candidates,
        "lifetime": life,
        "connection": {
            "ok": connection.get("ok"),
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
            "pid": worker.get("pid") or 0,
            "worker_alive": _worker_alive(worker.get("pid")),
        },
        "log_tail": log_tail,
        "log_size": log_size,
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
