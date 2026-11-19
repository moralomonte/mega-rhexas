"""Claim a batch, run the existing download/search/filter path, upload, delete.

The scientific settings (period grid, SDE, SNR, durations) are the pipeline's.
This module only changes where the files go, how many processes run, and the
worker cache limits.

With prefetch on, the next batch's light curves download while this batch is
searching. ``state.json`` is the batch in front. ``next.json`` is the one
already leased and downloading. A restart reads both.
"""
from __future__ import annotations

import csv
import inspect
import json
import os
import shutil
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from rhexas.config import Config

from .cache import delete_lightcurves, directory_bytes
from .client import CoordinatorClient, ProtocolError
from .priority import apply_priority

# One worker process. Download threads read these; they are not thread-locals.
_stop_event: threading.Event | None = None
_hold_fn = None
_hold_local = threading.local()
# Used only when this cache has no light curves yet to average. A SPOC 2-min file is about this size.
PREFETCH_CURVE_BYTES = 2_000_000


class WorkerStopped(Exception):
    """The tray asked this process to quit."""


def _check_stop() -> None:
    event = _stop_event
    if event is not None and event.is_set():
        raise WorkerStopped()


def _set_hold(fn) -> None:
    global _hold_fn
    _hold_fn = fn


def _user_paused(live) -> bool:
    return bool(_live_call(live, "user_paused"))


def _mark_holding(live, memory: dict, holding: bool) -> None:
    """Publish that the worker is actually waiting. Repeat calls do not rewrite the row."""
    if memory.get("holding") is bool(holding):
        return
    memory["holding"] = bool(holding)
    _live_call(live, "set_holding", bool(holding))

_FLOATS = (
    "period", "t0", "duration", "depth", "depth_err", "snr", "sde",
    "odd_even_sigma", "sec_sigma", "score",
)
_INTS = ("n_transits", "npts")


def backoff_s(attempt: int, base: float = 15, cap: float = 300) -> float:
    """Wait after a coordinator or network failure. MAST 429s are handled in download.py."""
    return min(cap, base * 2 ** min(max(attempt, 0), 5))


def cache_dir(cfg: Config) -> Path:
    raw = Path(cfg.worker.cache_dir).expanduser()
    if raw.is_absolute():
        return raw
    return (cfg.root / raw).resolve()


def unit_config(base: Config, assignment: dict, cache: Path) -> Config:
    """A Config pointed at this batch's cache. Science thresholds stay as they are."""
    sector = int(assignment["sector"])
    cache = Path(cache)
    data_dir = (cache / "data").resolve()
    results_root = (cache / "results" / f"a{int(assignment['assignment_id'])}").resolve()
    worker = base.worker
    return replace(
        base,
        sector=sector,
        data_dir=data_dir,
        results_root=results_root,
        sector_results=results_root / f"s{sector:04d}",
        logs_dir=(cache / "logs").resolve(),
        catalogs_dir=(cache / "catalogs").resolve(),
        min_free_gb=worker.min_free_gb,
        max_project_gb=worker.max_cache_gb + base.env_overhead_gb,
        download=replace(base.download, threads=worker.threads),
        search=replace(base.search, procs=worker.procs),
    )


def _blank(value):
    if value is None:
        return None
    text = str(value).strip()
    if text in ("", "nan", "NaN", "None"):
        return None
    return text


def _float(value):
    text = _blank(value)
    if text is None:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _row_numbers(row: dict) -> dict:
    item = {}
    for key in _FLOATS:
        if key in row:
            item[key] = _float(row.get(key))
    for key in _INTS:
        number = _float(row.get(key))
        if key in row:
            item[key] = None if number is None else int(number)
    return item


def read_stars(results_dir: Path, tics: list[int]) -> list[dict]:
    """One compact row per TIC. Missing stars are reported as errors, not omitted."""
    path = Path(results_dir) / "bls_raw.csv"
    found: dict[int, dict] = {}
    if path.is_file():
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                tic_text = _blank(row.get("tic"))
                if tic_text is None:
                    continue
                try:
                    tic = int(float(tic_text))
                except ValueError:
                    continue
                item = {"tic": tic, "error": _blank(row.get("error"))}
                item.update(_row_numbers(row))
                found[tic] = item
    out = []
    for tic in tics:
        if tic in found:
            out.append(found[tic])
        else:
            out.append({"tic": int(tic), "period": None, "sde": None, "depth": None, "error": "no result"})
    return out


def read_candidates(results_dir: Path) -> list[dict]:
    path = Path(results_dir) / "candidates.csv"
    if not path.is_file():
        return []
    out = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            tic_text = _blank(row.get("tic"))
            if tic_text is None:
                continue
            try:
                tic = int(float(tic_text))
            except ValueError:
                continue
            item = {"tic": tic, "status": row.get("status") or "", "flags": row.get("flags") or ""}
            item.update(_row_numbers(row))
            out.append(item)
    return out


def ensure_space(cache: Path, cfg: Config) -> None:
    """Refuse a download that would leave under 10 GB free. Do not delete sector files."""
    from .holdings import free_floor_gb
    data = Path(cache) / "data"
    data.mkdir(parents=True, exist_ok=True)
    need = free_floor_gb(cfg.worker.min_free_gb) * 1e9
    free = shutil.disk_usage(data).free
    if free >= need:
        return
    raise RuntimeError(
        f"only {free / 1e9:.1f} GB free; a download must leave at least "
        f"{free_floor_gb(cfg.worker.min_free_gb):.0f} GB"
    )


def _state_path(cache: Path) -> Path:
    return Path(cache) / "state.json"


def _next_path(cache: Path) -> Path:
    return Path(cache) / "next.json"


def _queue_path(cache: Path) -> Path:
    return Path(cache) / "queue.json"


def _read_queue(cache: Path) -> list[dict]:
    """Units downloaded ahead of the one being scanned, oldest first."""
    data = _read_state(_queue_path(cache))
    rows = data.get("units") if isinstance(data, dict) else data
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict) and row.get("assignment_id") is not None]
    nxt = _read_state(_next_path(cache))
    if isinstance(nxt, dict) and nxt.get("assignment_id") is not None:
        return [nxt]
    return []


def _write_queue(cache: Path, rows: list[dict]) -> None:
    path = _queue_path(cache)
    if rows:
        _write_state(path, {"units": rows})
    else:
        _clear_state(path)
    if rows:
        _write_state(_next_path(cache), rows[0])
    else:
        _clear_state(_next_path(cache))


def _read_state(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(path)


def _clear_state(path: Path) -> None:
    path.unlink(missing_ok=True)


def discard_local(cache: Path, state: dict) -> None:
    cache = Path(cache)
    assignment_id = state.get("assignment_id")
    if assignment_id:
        shutil.rmtree(cache / "results" / f"a{int(assignment_id)}", ignore_errors=True)
    tics = {int(tic) for tic in state.get("tics") or []}
    sector = state.get("sector")
    if sector and tics:
        delete_lightcurves(cache / "data" / f"s{int(sector):04d}", tics)


def default_download(cfg: Config, tics) -> int:
    from rhexas.download import run
    hold = getattr(_hold_local, "fn", None)
    if hold is None:
        hold = _hold_fn
    code = run(cfg, threads=cfg.download.threads, tics=set(tics), hold=hold)
    if code == 2:
        raise RuntimeError("disk limit stopped the download")
    return code


def default_filter(cfg: Config, tics) -> int:
    from rhexas.filter import run
    return run(cfg, plots=0, tics=set(tics))


class LeaseSet:
    """Assignment ids the heartbeat thread should renew. Safe to add from the prefetch thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[int, HeartbeatProgress | None] = {}

    def add(self, assignment_id: int, progress: HeartbeatProgress | None = None) -> None:
        with self._lock:
            self._items[int(assignment_id)] = progress

    def discard(self, assignment_id: int) -> None:
        with self._lock:
            self._items.pop(int(assignment_id), None)

    def pairs(self) -> list[tuple[int, HeartbeatProgress | None]]:
        with self._lock:
            return list(self._items.items())


# One refused renewal is not "the coordinator is down". A network failure has to
# repeat before the page says the coordinator is unreachable.
_UNREACHABLE_AFTER = 3


def _heartbeat_failure(exc) -> str:
    """lease_ended, rejected, or network. A 409 means the assignment is already finished."""
    if isinstance(exc, ProtocolError):
        if int(getattr(exc, "status", 0) or 0) == 409:
            return "lease_ended"
        return "rejected"
    return "network"


def start_heartbeat(client, assignment_id: int, interval: float, *, on_result=None, progress: HeartbeatProgress | None = None,
                    leases: LeaseSet | None = None):
    """Ping the coordinator until the returned event is set. ``interval <= 0`` disables it.

    ``progress`` is sent only after the worker has a fraction. A call with no progress
    matches workers that predate the field. ``leases`` renews every id it holds, so a
    prefetched batch does not expire while the current one is searching.

    A 409 drops that lease and is not a failure. Other coordinator rejections are
    reported on their own. Only repeated network errors are "unreachable".
    """
    if interval <= 0:
        return None

    stop = threading.Event()
    streak = {"network": 0}

    def loop():
        while not stop.wait(interval):
            if leases is not None:
                targets = []
                for aid, prog in leases.pairs():
                    snap = prog.get() if prog is not None else None
                    targets.append((aid, snap))
            else:
                snap = progress.get() if progress is not None else None
                targets = [(assignment_id, snap)]
            network = False
            rejected = []
            ended = False
            for aid, snap in targets:
                try:
                    if snap:
                        client.heartbeat(aid, progress=snap)
                    else:
                        client.heartbeat(aid)
                except Exception as exc:
                    kind = _heartbeat_failure(exc)
                    if kind == "lease_ended":
                        ended = True
                        if leases is not None:
                            leases.discard(aid)
                        print(f"heartbeat: lease ended for assignment {aid}", flush=True)
                    elif kind == "rejected":
                        rejected.append(str(exc))
                        print(f"heartbeat rejected: {exc}", flush=True)
                    else:
                        network = True
                        print(f"heartbeat network failure: {exc}", flush=True)
            if network:
                streak["network"] += 1
            else:
                streak["network"] = 0
            if on_result is None:
                continue
            try:
                if streak["network"] >= _UNREACHABLE_AFTER:
                    on_result(False, "unreachable")
                elif network:
                    on_result(None, f"network trouble ({streak['network']})")
                elif rejected:
                    on_result(True, "rejected " + "; ".join(rejected)[:120])
                elif ended:
                    on_result(True, "lease ended")
                else:
                    on_result(True, "connected")
            except Exception:
                pass

    threading.Thread(target=loop, name="rhexas-heartbeat", daemon=True).start()
    return stop


class _Quiet:
    """Stand-in when the local dashboard file cannot be opened. Every call is a no-op."""

    def __getattr__(self, name):
        def _noop(*args, **kwargs):
            return None
        return _noop


def _live_call(live, name: str, *args, **kwargs):
    """Dashboard writes must not fail a batch."""
    fn = getattr(live, name, None)
    if fn is None:
        return None
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


class _SearchHooks:
    """Parent-only. The pool receives a plain dict, not this closure."""

    current = None


def default_search(cfg: Config, tics, procs: int) -> int:
    from rhexas.search import run
    hooks = _SearchHooks.current or {}
    return run(
        cfg, procs=procs, tics=set(tics), live=hooks.get("live"),
        on_row=hooks.get("on_row"), pace=hooks.get("pace"),
    )


def current_pace(cfg: Config, live, memory: dict) -> dict:
    """Publish the load the worker was started with.

    Slow, Normal, Realtime, and an old eco/live/turbo value are not read.
    Procs, threads, priority, and the poll wait stay on the config.
    """
    from .live import pace_settings
    baseline = {
        "procs": cfg.worker.procs,
        "threads": cfg.worker.threads,
        "priority": cfg.worker.priority,
    }
    stored = _live_call(live, "baseline")
    if isinstance(stored, dict) and stored.get("procs"):
        baseline = stored
    settings = pace_settings("live", baseline)
    _live_call(live, "publish_pace", settings)
    signature = (
        settings["procs"], settings["threads"], settings["priority"],
    )
    if signature != memory.get("sig"):
        memory["sig"] = signature
        print(
            f"pace {settings['mode']}: procs={settings['procs']}  "
            f"threads={settings['threads']}  priority={settings['priority']}",
            flush=True,
        )
    if settings["priority"] != memory.get("priority"):
        try:
            apply_priority(settings["priority"])
        except Exception as exc:
            print(f"could not set process priority ({exc}); continuing", flush=True)
        memory["priority"] = settings["priority"]
    return settings


def wait_until_running(cfg: Config, live, memory: dict, sleeper) -> dict:
    """Hold here while the user has paused the worker. The lease stays.

    Picture speed does not pause. Only the user pause flag does.
    """
    while True:
        _check_stop()
        settings = current_pace(cfg, live, memory)
        if not _user_paused(live):
            _mark_holding(live, memory, False)
            return settings
        _mark_holding(live, memory, True)
        sleeper(0.4)


def _with_pace(ucfg: Config, settings: dict) -> Config:
    return replace(
        ucfg,
        download=replace(ucfg.download, threads=int(settings["threads"])),
        search=replace(ucfg.search, procs=int(settings["procs"])),
    )


def _paced_sleep(cfg: Config, live, memory: dict, sleeper, seconds: float) -> None:
    deadline = time.monotonic() + float(seconds)
    while True:
        _check_stop()
        current_pace(cfg, live, memory)
        left = deadline - time.monotonic()
        if left <= 0:
            return
        sleeper(left if left < 0.5 else 0.5)


def estimate_batch_bytes(data_dir: Path, n_tics: int) -> int:
    """Bytes the next batch is likely to add. Curves already cached set the average."""
    root = Path(data_dir)
    sizes: list[int] = []
    if root.exists():
        for path in root.glob("s*/*lc.fits"):
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > 0:
                sizes.append(size)
            if len(sizes) >= 32:
                break
    each = int(sum(sizes) / len(sizes)) if sizes else PREFETCH_CURVE_BYTES
    return each * max(int(n_tics), 0)


def prefetch_block(cache: Path, cfg: Config, n_tics: int, *, queued: int = 0, queue_max: int | None = None) -> str | None:
    """None when the next batch fits. Otherwise ``queue``, ``cache cap``, or ``free space``.

    This does not delete files. The batch already being searched stays on disk.
    Free space left behind is at least 10 GB, and at least ``min_free_gb`` when
    that is higher. The cap is the Sectors-page value when one has been saved.
    """
    from .holdings import free_floor_gb, queue_block, runtime_cap_gb
    data = Path(cache) / "data"
    try:
        data.mkdir(parents=True, exist_ok=True)
        used = directory_bytes(data)
        extra = estimate_batch_bytes(data, n_tics)
        free = shutil.disk_usage(data).free
    except OSError:
        return "free space"
    cap_gb = runtime_cap_gb(cache, cfg.worker.max_cache_gb)
    limit = cfg.worker.queue_max if queue_max is None else queue_max
    if int(cfg.worker.prefetch) <= 0:
        limit = 0
    return queue_block(
        queued=int(queued),
        queue_max=int(limit),
        used_bytes=used,
        extra_bytes=extra,
        cap_bytes=int(cap_gb * 1e9),
        free_bytes=free,
        floor_bytes=int(free_floor_gb(cfg.worker.min_free_gb) * 1e9),
    )


def _call_claim(client, *, prefetch: bool) -> dict:
    """Claim one batch. Clients that predate the prefetch argument are left on one lease."""
    claim = client.claim
    if prefetch:
        try:
            params = inspect.signature(claim).parameters
        except (TypeError, ValueError):
            return {"assignment": None}
        accepts = "prefetch" in params or any(
            item.kind is inspect.Parameter.VAR_KEYWORD for item in params.values()
        )
        if not accepts:
            return {"assignment": None}
        body = claim(prefetch=True)
    else:
        body = claim()
    return body if isinstance(body, dict) else {"assignment": None}


def _release_lease(client, assignment_id: int) -> None:
    fn = getattr(client, "release", None)
    if fn is None:
        return
    try:
        fn(int(assignment_id))
    except Exception as exc:
        print(f"prefetch release failed: {exc}", flush=True)


def _remote_leases(client) -> list[dict]:
    body = client.current()
    if not isinstance(body, dict):
        return []
    many = body.get("assignments")
    if isinstance(many, list):
        return [
            item for item in many
            if isinstance(item, dict) and item.get("assignment_id") is not None
        ]
    one = body.get("assignment")
    if isinstance(one, dict) and one.get("assignment_id") is not None:
        return [one]
    return []


def _merge_saved(cache: Path, saved: dict | None, by_id: dict[int, dict]) -> dict | None:
    if not isinstance(saved, dict) or saved.get("assignment_id") is None:
        return None
    try:
        aid = int(saved["assignment_id"])
    except (TypeError, ValueError):
        return None
    live = by_id.get(aid)
    if live is None:
        discard_local(cache, saved)
        return None
    return {**live, "stage": saved.get("stage") or "claimed"}


def _resume_or_claim(client, cache: Path) -> tuple[dict | None, dict | None]:
    """Match ``state.json`` and ``next.json`` to live leases, then claim if none are leased."""
    local = _read_state(_state_path(cache))
    nxt = _read_state(_next_path(cache))
    by_id: dict[int, dict] = {}
    for item in _remote_leases(client):
        try:
            by_id[int(item["assignment_id"])] = item
        except (TypeError, ValueError, KeyError):
            continue
    active = _merge_saved(cache, local, by_id)
    pre = _merge_saved(cache, nxt, by_id)
    if (
        active is not None and pre is not None
        and int(active["assignment_id"]) == int(pre["assignment_id"])
    ):
        pre = None
    known: set[int] = set()
    if active is not None:
        known.add(int(active["assignment_id"]))
    if pre is not None:
        known.add(int(pre["assignment_id"]))
    orphans = [item for aid, item in sorted(by_id.items()) if aid not in known]
    if active is None and orphans:
        active = {**orphans.pop(0), "stage": "claimed"}
    if pre is None and orphans:
        pre = {**orphans.pop(0), "stage": "claimed"}
    if active is None and pre is not None:
        active = pre
        pre = None
    if active is None:
        _clear_state(_state_path(cache))
    else:
        _write_state(_state_path(cache), active)
    if pre is None:
        _clear_state(_next_path(cache))
    else:
        _write_state(_next_path(cache), pre)
    if active is not None:
        return active, pre
    body = _call_claim(client, prefetch=False)
    claimed = body.get("assignment")
    if not isinstance(claimed, dict) or claimed.get("assignment_id") is None:
        return None, None
    active = {**claimed, "stage": "claimed"}
    _write_state(_state_path(cache), active)
    return active, pre


def _replace_queued(cache: Path, state: dict) -> None:
    rows = _read_queue(cache)
    aid = int(state.get("assignment_id") or 0)
    found = False
    for index, row in enumerate(rows):
        if int(row.get("assignment_id") or 0) == aid:
            rows[index] = state
            found = True
            break
    if not found:
        rows.append(state)
    _write_queue(cache, rows)


def _promote_next(cache: Path) -> dict | None:
    """Make the oldest queued batch the current one. Its stage is kept, so a finished download is not repeated."""
    rows = _read_queue(cache)
    if not rows:
        pre = _read_state(_next_path(cache))
        if not pre:
            return None
        rows = [pre]
    nxt = rows.pop(0)
    _write_state(_state_path(cache), nxt)
    _write_queue(cache, rows)
    return nxt


def _remember(cache: Path, assignment: dict, **fields) -> None:
    """Record a unit for the Sectors page. A failure here must not stop the search."""
    try:
        from .holdings import upsert_unit
        upsert_unit(cache, assignment, **fields)
    except Exception:
        return


def _confirm_receipt(client, assignment_id: int, submit_body) -> dict:
    """The coordinator's receipt. A body without one is asked for again."""
    from .holdings import apply_receipt
    body = submit_body if isinstance(submit_body, dict) else {}
    receipt = body.get("receipt") if isinstance(body.get("receipt"), dict) else None
    if receipt is None or "complete" not in receipt:
        fn = getattr(client, "receipt", None)
        if fn is not None:
            try:
                fresh = fn(int(assignment_id))
            except Exception:
                fresh = None
            if isinstance(fresh, dict):
                body = fresh if isinstance(fresh.get("receipt"), dict) else {"receipt": fresh}
    return apply_receipt(body)


def release_queued(client, cache: Path, *, keep: int | None = None) -> None:
    """Hand queued units back. The unit being scanned is left leased."""
    rows = _read_queue(cache)
    kept = []
    for state in rows:
        try:
            aid = int(state.get("assignment_id"))
        except (TypeError, ValueError):
            continue
        if keep is not None and aid == int(keep):
            kept.append(state)
            continue
        _release_lease(client, aid)
        discard_local(cache, state)
    _write_queue(cache, kept)


def _publish_saved_next(live, state: dict) -> None:
    stage = str(state.get("stage") or "claimed")
    n = int(state.get("n_tics") or len(state.get("tics") or []))
    if stage in ("downloaded", "searched", "filtered"):
        _live_call(live, "set_next", state, stage="ready", stars_done=n, stars_total=n)
    else:
        _live_call(live, "set_next", state, stage="downloading", stars_done=0, stars_total=n)


def prefetch_hold(live, sleeper) -> None:
    """User pause and Quit apply to the prefetch download. Animation speed does not."""
    while True:
        _check_stop()
        if not _user_paused(live):
            return
        sleeper(0.4)


def _prefetch_threads(cfg: Config, live) -> int:
    """Background downloads stay on one thread so the scan is not crowded out.

    Picture speed does not change this number.
    """
    del cfg, live
    from .holdings import BACKGROUND_DOWNLOAD_THREADS
    return BACKGROUND_DOWNLOAD_THREADS


def _invoke_download(download, ucfg: Config, tics: set[int], hold) -> None:
    previous = getattr(_hold_local, "fn", None)
    _hold_local.fn = hold
    try:
        download(ucfg, tics)
    finally:
        _hold_local.fn = previous


class _PrefetchJob:
    """Claim the next batch and download it while the current one is searched."""

    def __init__(self, cfg: Config, cache: Path, client, live, download, sleeper, watch, watch_drop=None):
        self.cfg = cfg
        self.cache = cache
        self.client = client
        self.live = live
        self.download = download
        self.sleeper = sleeper
        self.watch = watch
        self.watch_drop = watch_drop or (lambda _assignment_id: None)
        self._started = False
        self._thread: threading.Thread | None = None

    def start(self, active: dict, existing: dict | None, depth: int) -> None:
        if self._started:
            return
        self._started = True
        ahead = _read_queue(self.cache)
        if existing and not any(
            int(row.get("assignment_id") or 0) == int(existing.get("assignment_id") or -1) for row in ahead
        ):
            ahead = [existing, *ahead]
            _write_queue(self.cache, ahead)
        if depth < 1 and not ahead:
            return
        self._thread = threading.Thread(
            target=self._fill, args=(active, depth), name="rhexas-prefetch", daemon=True,
        )
        self._thread.start()

    def wait(self) -> None:
        thread = self._thread
        if thread is not None:
            thread.join()

    def _fill(self, active: dict, depth: int) -> None:
        """Download queued units, in order, then reserve more while the cap allows."""
        try:
            _check_stop()
            self._release_overflow(active, depth)
            for state in list(_read_queue(self.cache)):
                _check_stop()
                if self._download(state) is False:
                    self._drop(state)
                    return
            while depth >= 1:
                _check_stop()
                prefetch_hold(self.live, self.sleeper)
                self._release_overflow(active, depth)
                ahead = _read_queue(self.cache)
                held = {int(active["assignment_id"])}
                held.update(int(row["assignment_id"]) for row in ahead)
                if len(ahead) >= depth:
                    return
                estimate_n = self._estimate_n(active, ahead)
                reason = prefetch_block(
                    self.cache, self.cfg, estimate_n, queued=len(ahead), queue_max=depth,
                )
                if reason:
                    print(f"prefetch skipped: {reason}", flush=True)
                    return
                body = _call_claim(self.client, prefetch=True)
                assignment = body.get("assignment") if isinstance(body, dict) else None
                if not isinstance(assignment, dict) or assignment.get("assignment_id") is None:
                    return
                if int(assignment["assignment_id"]) in held:
                    return
                n = int(assignment.get("n_tics") or len(assignment.get("tics") or []))
                reason = prefetch_block(
                    self.cache, self.cfg, n, queued=len(ahead), queue_max=depth,
                )
                if reason:
                    print(f"prefetch skipped: {reason}", flush=True)
                    _release_lease(self.client, int(assignment["assignment_id"]))
                    return
                state = {**assignment, "stage": "queued"}
                ahead.append(state)
                _write_queue(self.cache, ahead)
                _remember(self.cache, state, stage="queued")
                print(
                    f"prefetch unit {state['assignment_id']} sector {state.get('sector')} ({n} stars)",
                    flush=True,
                )
                self.watch(state, HeartbeatProgress())
                if self._download(state) is False:
                    self._drop(state)
                    return
        except WorkerStopped:
            return
        except Exception as exc:
            print(f"prefetch failed: {exc}", flush=True)

    def _estimate_n(self, active: dict, ahead: list[dict]) -> int:
        """Star count used before a lease. Peek when the coordinator offers it."""
        fn = getattr(self.client, "peek", None)
        if fn is not None:
            try:
                body = fn()
            except Exception:
                body = None
            preview = body.get("preview") if isinstance(body, dict) else None
            if isinstance(preview, dict) and preview.get("n_tics"):
                try:
                    return max(1, int(preview["n_tics"]))
                except (TypeError, ValueError):
                    pass
        sample = ahead[-1] if ahead else active
        try:
            return max(1, int(sample.get("n_tics") or len(sample.get("tics") or []) or 1))
        except (TypeError, ValueError):
            return 1

    def _drop(self, state: dict) -> None:
        """Return a queued unit we will not download."""
        try:
            aid = int(state["assignment_id"])
        except (KeyError, TypeError, ValueError):
            return
        _release_lease(self.client, aid)
        self.watch_drop(aid)
        discard_local(self.cache, state)
        _write_queue(self.cache, [
            row for row in _read_queue(self.cache)
            if int(row.get("assignment_id") or 0) != aid
        ])

    def _release_overflow(self, active: dict, depth: int) -> None:
        """Give back queued units that no longer fit the cap, the free-disk floor, or the queue length."""
        from .holdings import free_floor_gb, runtime_cap_gb
        ahead = [
            row for row in _read_queue(self.cache)
            if int(row.get("assignment_id") or 0) != int(active.get("assignment_id") or -1)
        ]
        data = Path(self.cache) / "data"
        cap = int(runtime_cap_gb(self.cache, self.cfg.worker.max_cache_gb) * 1e9)
        floor = int(free_floor_gb(self.cfg.worker.min_free_gb) * 1e9)

        def too_much() -> str | None:
            if len(ahead) > max(depth, 0):
                return "queue"
            try:
                used = directory_bytes(data)
                free = shutil.disk_usage(data if data.exists() else self.cache).free
            except OSError:
                return None
            if used > cap:
                return "cache cap"
            if free < floor:
                return "free space"
            return None

        changed = False
        while ahead and (reason := too_much()):
            state = ahead.pop()
            print(f"releasing queued unit {state.get('assignment_id')}: {reason}", flush=True)
            _release_lease(self.client, int(state["assignment_id"]))
            self.watch_drop(int(state["assignment_id"]))
            discard_local(self.cache, state)
            changed = True
        if changed or len(_read_queue(self.cache)) != len(ahead):
            _write_queue(self.cache, ahead)

    def _download(self, state: dict) -> None:
        stage = str(state.get("stage") or "claimed")
        if stage in ("downloaded", "searched", "filtered", "ready"):
            _publish_saved_next(self.live, state)
            return True
        n = int(state.get("n_tics") or len(state.get("tics") or []))
        ahead = _read_queue(self.cache)
        others = [row for row in ahead if int(row.get("assignment_id") or 0) != int(state.get("assignment_id") or -1)]
        reason = prefetch_block(
            self.cache, self.cfg, n, queued=len(others), queue_max=max(len(ahead), 1),
        )
        if reason:
            print(f"prefetch paused: {reason}", flush=True)
            _publish_saved_next(self.live, state)
            return False
        progress = HeartbeatProgress()
        progress.set("download", 0, n)
        self.watch(state, progress)
        state = {**state, "stage": "downloading"}
        _replace_queued(self.cache, state)
        _remember(self.cache, state, stage="downloading", download_done=0, download_total=n)
        _live_call(self.live, "set_next", state, stage="downloading", stars_done=0, stars_total=n)
        ucfg = unit_config(self.cfg, state, self.cache)
        threads = _prefetch_threads(self.cfg, self.live)
        ucfg = replace(ucfg, download=replace(ucfg.download, threads=threads))
        tics = {int(tic) for tic in state.get("tics") or []}
        stop_watch = threading.Event()

        def on_progress(done, total):
            _live_call(self.live, "update_next", stage="downloading", stars_done=done, stars_total=total)
            _remember(self.cache, state, stage="downloading", download_done=done, download_total=total)

        watcher = threading.Thread(
            target=_watch_downloads,
            args=(self.live, ucfg.sector_data, n, stop_watch, progress),
            kwargs={"tics": tics, "on_progress": on_progress},
            name="rhexas-prefetch-watch",
            daemon=True,
        )
        watcher.start()
        try:
            attempt = 0
            while True:
                prefetch_hold(self.live, self.sleeper)
                try:
                    _invoke_download(self.download, ucfg, tics, lambda: prefetch_hold(self.live, self.sleeper))
                    break
                except WorkerStopped:
                    raise
                except Exception as exc:
                    attempt += 1
                    print(f"prefetch download retry {attempt}: {exc}", flush=True)
                    if attempt >= 3:
                        raise
                    self.sleeper(min(5.0, 0.2 * attempt))
        finally:
            stop_watch.set()
            watcher.join(timeout=2)
        state = {**state, "stage": "downloaded"}
        _replace_queued(self.cache, state)
        _remember(
            self.cache, state, stage="ready", download_done=n, download_total=n,
            ready_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        progress.set("download", n, n)
        _live_call(self.live, "update_next", stage="ready", stars_done=n, stars_total=n)
        print(f"prefetch unit {state['assignment_id']} ready", flush=True)
        return True


def _advance(path: Path, state: dict, stage: str) -> dict:
    state = {**state, "stage": stage}
    _write_state(path, state)
    return state


class HeartbeatProgress:
    """Latest fraction the heartbeat thread can attach. Safe to update from the download watcher."""

    def __init__(self):
        self._lock = threading.Lock()
        self._value: dict | None = None

    def set(self, phase: str, done: int, total: int) -> None:
        with self._lock:
            self._value = {"phase": str(phase), "done": int(done), "total": int(total)}

    def get(self) -> dict | None:
        with self._lock:
            return None if self._value is None else dict(self._value)


def _ready_curves(folder: Path, tics: set[int] | None) -> int:
    """Count light curves on disk. ``tics`` limits the count to one batch when two share a sector."""
    folder = Path(folder)
    try:
        paths = [path for path in folder.glob("*lc.fits") if path.is_file()]
    except OSError:
        return 0
    if tics is None:
        return len(paths)
    from rhexas.tics import tic_from_filename
    found = 0
    for path in paths:
        tic = tic_from_filename(path.name)
        if tic in tics:
            found += 1
    return found


def _watch_downloads(live, folder: Path, total: int, stop: threading.Event, progress: HeartbeatProgress | None = None,
                     *, tics: set[int] | None = None, on_progress=None) -> None:
    """Count FITS files already on disk so the dashboard can move during a download."""
    started = time.time()
    while not stop.is_set():
        found = _ready_curves(folder, tics)
        if stop.is_set():
            return
        done = min(found, total)
        if progress is not None:
            progress.set("download", done, total)
        elapsed = time.time() - started
        rate = (found / elapsed) if elapsed >= 1 and found else None
        eta = ((total - found) / rate / 60.0) if rate else None
        if on_progress is not None:
            try:
                on_progress(done, total)
            except Exception:
                pass
        else:
            _live_call(
                live, "update_batch", stage="downloading", progress_kind="files",
                stars_done=done, stars_total=total,
                per_s=round(rate, 3) if rate else None,
                eta_min=round(eta, 1) if eta is not None else None,
            )
        if stop.wait(2.0):
            return


def stars_still_missing(results_dir: Path, tics: list[int]) -> list[int]:
    """TICs with no measurement. A star the search actually rejected is not in this list."""
    missing = []
    for row in read_stars(results_dir, tics):
        if row.get("error") == "no result":
            missing.append(int(row["tic"]))
    return missing


def retry_missing_before_submit(ucfg: Config, tics: list[int], download, search, filt) -> list[int]:
    """One more download and search for stars that never produced a row.

    Replica comparison treats a star only one worker measured as a miss. A home
    connection that dropped the MAST transfer should not become that miss if the
    file can still be fetched.
    """
    missing = stars_still_missing(ucfg.sector_results, tics)
    if not missing:
        return []
    print(f"retrying {len(missing)} stars still missing before upload", flush=True)
    download(ucfg, set(missing))
    _check_stop()
    search(ucfg, set(missing), int(ucfg.search.procs))
    _check_stop()
    filt(ucfg, set(tics))
    return missing


def run_unit(cfg: Config, cache: Path, client, assignment: dict, *, download, search, filt, live=None,
             progress: HeartbeatProgress | None = None, pace_memory=None, sleeper=None,
             before_search=None, on_submitted=None) -> None:
    """Download, search, and filter one leased batch, then upload and delete it."""
    live = live or _Quiet()
    path = _state_path(cache)
    state = _read_state(path) or {**assignment, "stage": "claimed"}
    if state.get("assignment_id") != assignment["assignment_id"]:
        state = {**assignment, "stage": "claimed"}
    ucfg = unit_config(cfg, assignment, cache)
    tics = [int(tic) for tic in assignment["tics"]]
    tic_set = set(tics)
    n_tics = int(assignment.get("n_tics") or len(tics))
    stage = state.get("stage") or "claimed"
    memory = pace_memory if isinstance(pace_memory, dict) else {}
    pause_sleep = sleeper or time.sleep
    armed = {"on": False}

    def arm():
        if armed["on"]:
            return
        armed["on"] = True
        if before_search is not None:
            before_search()

    _live_call(live, "begin_batch", assignment, stage)
    _remember(cache, assignment, stage=stage, claimed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    if stage != "claimed":
        arm()
    if stage == "claimed":
        settings = wait_until_running(cfg, live, memory, pause_sleep)
        ucfg = _with_pace(ucfg, settings)
        _live_call(
            live, "update_batch", stage="downloading", progress_kind="files",
            stars_done=0, stars_total=n_tics, per_s=None, eta_min=None,
        )
        _remember(cache, assignment, stage="downloading", download_done=0, download_total=n_tics,
                  claimed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        if progress is not None:
            progress.set("download", 0, n_tics)
        stop_watch = threading.Event()
        watch = threading.Thread(
            target=_watch_downloads, args=(live, ucfg.sector_data, n_tics, stop_watch, progress),
            name="rhexas-download-watch", daemon=True,
        )
        watch.start()

        def _hold():
            wait_until_running(cfg, live, memory, pause_sleep)

        _set_hold(_hold)
        try:
            ensure_space(cache, cfg)
            download(ucfg, tic_set)
        finally:
            _set_hold(None)
            stop_watch.set()
            watch.join(timeout=2.0)
        state = _advance(path, state, "downloaded")
        stage = "downloaded"
        arm()
    if stage == "downloaded":
        _live_call(
            live, "update_batch", stage="searching", progress_kind="stars",
            stars_total=n_tics, per_s=None, eta_min=None,
        )
        _remember(
            cache, assignment, stage="searching", stars_done=0, stars_total=n_tics,
            scan_started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        if progress is not None:
            progress.set("search", 0, n_tics)
        spec = None
        try:
            from .live import live_spec
            spec = live_spec(cache, cfg.filt, assignment)
        except Exception:
            spec = None

        def on_row(k, todo, row, elapsed):
            done = (n_tics - todo) + k if todo <= n_tics else k
            rate = (k / elapsed) if elapsed > 0 else 0.0
            eta = ((todo - k) / rate / 60.0) if rate > 0 else None
            if progress is not None:
                progress.set("search", done, n_tics)
            _live_call(
                live, "update_batch", stage="searching", progress_kind="stars",
                stars_done=done, stars_total=n_tics,
                per_s=round(rate, 3) if rate else None,
                eta_min=round(eta, 1) if eta is not None else None,
            )

        def pace():
            settings = dict(current_pace(cfg, live, memory))
            # User pause is honored between stars and, via still_paused, during a BLS call.
            # Picture speed does not change procs.
            settings["paused"] = bool(_user_paused(live))
            settings["still_paused"] = lambda: bool(_user_paused(live))
            settings["mark_holding"] = lambda holding: _mark_holding(live, memory, bool(holding))
            return settings

        settings = wait_until_running(cfg, live, memory, pause_sleep)
        ucfg = _with_pace(ucfg, settings)
        _SearchHooks.current = {"live": spec, "on_row": on_row, "pace": pace}
        try:
            search(ucfg, tic_set, int(settings["procs"]))
        finally:
            _SearchHooks.current = None
        state = _advance(path, state, "searched")
        stage = "searched"
        _live_call(
            live, "update_batch", stage="searched", progress_kind="stars",
            stars_done=n_tics, stars_total=n_tics, eta_min=0,
        )
        if progress is not None:
            progress.set("search", n_tics, n_tics)
    if stage == "searched":
        _live_call(live, "update_batch", stage="filtering", progress_kind="stars", eta_min=0, per_s=None)
        if progress is not None:
            progress.set("filter", n_tics, n_tics)
        filt(ucfg, tic_set)
        state = _advance(path, state, "filtered")
        stage = "filtered"
    if stage == "filtered":
        def _hold():
            wait_until_running(cfg, live, memory, pause_sleep)

        _set_hold(_hold)
        try:
            retry_missing_before_submit(ucfg, tics, download, search, filt)
        except WorkerStopped:
            raise
        except Exception as exc:
            print(f"final retry for missing stars did not finish: {exc}", flush=True)
        finally:
            _set_hold(None)
        stars = read_stars(ucfg.sector_results, tics)
        candidates = read_candidates(ucfg.sector_results)
        _live_call(live, "record_candidates", assignment, candidates)
        _notify_vetting(cache, cfg)
        _live_call(live, "update_batch", stage="uploading", progress_kind="stars", eta_min=0, per_s=None)
        _remember(cache, assignment, stage="uploading")
        if progress is not None:
            progress.set("upload", n_tics, n_tics)
        result = client.submit(assignment["assignment_id"], stars, candidates)
        # The lease ends when the coordinator accepts the upload. Drop it before
        # receipt checks and before the prefetch thread is joined, so the heartbeat
        # does not renew an assignment that is already submitted.
        if on_submitted is not None:
            try:
                on_submitted(int(assignment["assignment_id"]))
            except Exception:
                pass
        _live_call(live, "note_submit", result if isinstance(result, dict) else {}, len(stars))
        confirmed = _confirm_receipt(client, int(assignment["assignment_id"]), result if isinstance(result, dict) else {})
        _clear_state(path)
        _remember(
            cache, assignment, stage="done", results_sent=True,
            results_confirmed=confirmed["results_confirmed"], receipt=confirmed["receipt"],
            stars_done=n_tics, stars_total=n_tics,
            uploaded_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        _live_call(
            live, "update_batch", stage="uploaded", active=False, progress_kind="stars",
            stars_done=n_tics, stars_total=n_tics, eta_min=0, per_s=None,
        )


def _notify_vetting(cache: Path, cfg: Config) -> None:
    """Wake the vetting thread, if this computer opted in.

    This returns immediately. The checks run on ``rhexas-vetting``, not here.
    """
    try:
        if not cfg.vetting.enabled:
            return
        from .vetting.runner import notify
        notify(cache)
    except Exception:
        return


def _open_local(cfg: Config, cache: Path):
    """Dashboard state and a redacted log. The worker keeps going if either cannot be opened."""
    old_out, old_err = sys.stdout, sys.stderr
    try:
        from .live import Store, Tee, WorkerLog
        live = Store(cache)
    except Exception as exc:
        print(f"local dashboard state disabled: {exc}", flush=True)
        return _Quiet(), None, old_out, old_err
    log = None
    try:
        log = WorkerLog(cache)
        sys.stdout = Tee(old_out, log)
        sys.stderr = Tee(old_err, log)
    except Exception:
        log = None
    _live_call(
        live, "set_worker",
        pid=os.getpid(), procs=cfg.worker.procs, threads=cfg.worker.threads,
        priority=cfg.worker.priority, max_cache_gb=cfg.worker.max_cache_gb,
        coordinator_url=cfg.worker.coordinator_url,
    )
    _live_call(live, "set_connection", None, "connecting")
    return live, log, old_out, old_err


def _close_local(live, log, old_out, old_err) -> None:
    _live_call(live, "set_pid", 0)
    sys.stdout = old_out
    sys.stderr = old_err
    if log is not None:
        try:
            log.close()
        except Exception:
            pass


def run_worker(cfg: Config, *, once: bool = False, client=None, download=None, search=None, filt=None,
               sleeper=time.sleep, heartbeat_s: float = 300, quit_event: threading.Event | None = None) -> int:
    global _stop_event
    previous_stop = _stop_event
    if quit_event is not None:
        _stop_event = quit_event
    token = load_token(cfg)
    if client is None:
        if not cfg.worker.coordinator_url or not token:
            _stop_event = previous_stop
            print("rhexas worker: set the coordinator URL and a token", file=sys.stderr)
            return 2
        client = CoordinatorClient(cfg.worker.coordinator_url, token)
    download = download or default_download
    search = search or default_search
    filt = filt or default_filter
    cache = cache_dir(cfg)
    cache.mkdir(parents=True, exist_ok=True)
    try:
        live, log, old_out, old_err = _open_local(cfg, cache)
    except BaseException:
        _stop_event = previous_stop
        raise
    beat_stop = None
    prefetcher = None
    vetting_runner = None
    try:
        try:
            apply_priority(cfg.worker.priority)
            print(f"process priority: {cfg.worker.priority}", flush=True)
        except Exception as exc:
            print(f"could not set process priority ({exc}); continuing", flush=True)
        depth = int(cfg.worker.queue_max) if int(cfg.worker.prefetch) >= 1 else 0
        print(
            f"coordinator {cfg.worker.coordinator_url}  procs={cfg.worker.procs}  "
            f"threads={cfg.worker.threads}  cache={cache}  prefetch={depth}",
            flush=True,
        )
        try:
            from .vetting.runner import ensure_runner
            vetting_runner = ensure_runner(cache, cfg.vetting)
            if vetting_runner is not None:
                print("vetting on: one candidate at a time, local only", flush=True)
        except Exception as exc:
            vetting_runner = None
            print(f"vetting did not start ({exc}); the search continues", flush=True)
        pace_memory: dict = {}
        current_pace(cfg, live, pace_memory)
        attempt = 0
        leases = LeaseSet()
        beat_stop = None
        beat_lock = threading.Lock()
        promoted = None
        prefetcher = None

        def watch(assignment, progress):
            nonlocal beat_stop
            leases.add(int(assignment["assignment_id"]), progress)
            with beat_lock:
                if beat_stop is None:
                    beat_stop = start_heartbeat(
                        client, int(assignment["assignment_id"]), heartbeat_s,
                        on_result=lambda ok, detail: _live_call(live, "set_connection", ok, detail),
                        leases=leases,
                    )

        while True:
            _check_stop()
            if _user_paused(live) and not pace_memory.get("announced_pause"):
                print("Paused. Turn the worker back on from the dashboard to continue.", flush=True)
                pace_memory["announced_pause"] = True
            while _user_paused(live):
                _check_stop()
                _mark_holding(live, pace_memory, True)
                sleeper(0.4)
            _mark_holding(live, pace_memory, False)
            pace_memory["announced_pause"] = False
            try:
                if promoted is not None:
                    assignment = promoted
                    resumed_next = None
                    promoted = None
                else:
                    assignment, resumed_next = _resume_or_claim(client, cache)
                attempt = 0
            except WorkerStopped:
                raise
            except ProtocolError as exc:
                print(f"coordinator rejected the request ({exc.status}): {exc}", flush=True)
                _live_call(live, "set_connection", False, f"rejected ({exc.status})")
                if once:
                    return 1
                _paced_sleep(cfg, live, pace_memory, sleeper, backoff_s(attempt))
                attempt += 1
                continue
            except Exception as exc:
                print(f"coordinator unreachable: {exc}", flush=True)
                _live_call(live, "set_connection", False, "unreachable")
                if once:
                    return 1
                _paced_sleep(cfg, live, pace_memory, sleeper, backoff_s(attempt))
                attempt += 1
                continue
            _live_call(live, "set_connection", True, "connected")
            if assignment is None:
                print("no work right now", flush=True)
                _live_call(live, "mark_waiting")
                if once:
                    return 0
                _paced_sleep(cfg, live, pace_memory, sleeper, cfg.worker.poll_s)
                continue
            print(
                f"unit {assignment['assignment_id']} sector {assignment['sector']} "
                f"({assignment['n_tics']} stars) stage {assignment.get('stage')}",
                flush=True,
            )
            progress = HeartbeatProgress()
            watch(assignment, progress)
            if resumed_next:
                nxt_progress = HeartbeatProgress()
                n_next = int(resumed_next.get("n_tics") or len(resumed_next.get("tics") or []))
                if str(resumed_next.get("stage") or "") in ("downloaded", "searched", "filtered"):
                    nxt_progress.set("download", n_next, n_next)
                watch(resumed_next, nxt_progress)
                _publish_saved_next(live, resumed_next)
            def watch_drop(assignment_id, _leases=leases):
                _leases.discard(int(assignment_id))

            prefetcher = _PrefetchJob(
                cfg, cache, client, live, download, sleeper, watch, watch_drop,
            )

            def begin_prefetch(active=assignment, nxt=resumed_next, job=prefetcher, slots=depth):
                job.start(active, nxt, slots)

            def drop_submitted(aid, _leases=leases):
                _leases.discard(int(aid))

            try:
                run_unit(
                    cfg, cache, client, assignment,
                    download=download, search=search, filt=filt, live=live, progress=progress,
                    pace_memory=pace_memory, sleeper=sleeper, before_search=begin_prefetch,
                    on_submitted=drop_submitted,
                )
            except WorkerStopped:
                prefetcher.wait()
                release_queued(client, cache)
                return 0
            except ProtocolError as exc:
                leases.discard(int(assignment["assignment_id"]))
                prefetcher.wait()
                if exc.status == 409:
                    print(f"unit {assignment['assignment_id']} is no longer ours ({exc}); dropping local files", flush=True)
                    discard_local(cache, assignment)
                    _clear_state(_state_path(cache))
                    _live_call(live, "update_batch", stage="released", active=False)
                    if once:
                        return 0
                    continue
                print(f"upload failed ({exc.status}): {exc}", flush=True)
                _live_call(live, "set_connection", True, f"rejected upload ({exc.status})")
                if once:
                    return 1
                _paced_sleep(cfg, live, pace_memory, sleeper, cfg.worker.poll_s)
                continue
            except Exception as exc:
                prefetcher.wait()
                print(f"unit {assignment['assignment_id']} failed: {exc}", flush=True)
                _live_call(live, "update_batch", stage="error", active=True)
                if once:
                    return 1
                _paced_sleep(cfg, live, pace_memory, sleeper, cfg.worker.poll_s)
                continue
            prefetcher.wait()
            finished = int(assignment["assignment_id"])
            print(f"unit {assignment['assignment_id']} uploaded; light curves kept until cleared", flush=True)
            if once:
                return 0
            nxt = _promote_next(cache)
            leases.discard(finished)
            if nxt:
                _live_call(live, "clear_next")
                promoted = nxt
    except WorkerStopped:
        release_queued(client, cache)
        return 0
    finally:
        if vetting_runner is not None:
            vetting_runner.stop()
        if beat_stop is not None:
            beat_stop.set()
        if prefetcher is not None:
            prefetcher.wait()
        _stop_event = previous_stop
        _close_local(live, log, old_out, old_err)


def resolve_worker(cfg: Config, *, coordinator=None, token=None, token_file=None, procs=None, threads=None,
                   priority=None, max_cache_gb=None, min_free_gb=None, poll_s=None, cache_dir_value=None,
                   prefetch=None, queue_max=None, environ=None) -> Config:
    env = os.environ if environ is None else environ
    worker = cfg.worker
    updates = {}
    url = (coordinator or "").strip() or (env.get("RHEXAS_COORDINATOR_URL") or "").strip() or worker.coordinator_url
    updates["coordinator_url"] = url.rstrip("/")
    inline = (token or "").strip() or (env.get("RHEXAS_WORKER_TOKEN") or "").strip()
    file_name = (token_file or "").strip() or (env.get("RHEXAS_WORKER_TOKEN_FILE") or "").strip()
    if inline:
        updates["token"] = inline
        updates["token_file"] = ""
    elif file_name:
        updates["token"] = ""
        updates["token_file"] = file_name
    if procs is not None:
        updates["procs"] = int(procs)
    if threads is not None:
        updates["threads"] = int(threads)
    if priority:
        updates["priority"] = str(priority)
    if max_cache_gb is not None:
        updates["max_cache_gb"] = float(max_cache_gb)
    if min_free_gb is not None:
        updates["min_free_gb"] = float(min_free_gb)
    if poll_s is not None:
        updates["poll_s"] = float(poll_s)
    if cache_dir_value:
        updates["cache_dir"] = str(cache_dir_value)
    if prefetch is not None:
        updates["prefetch"] = 1 if int(prefetch) >= 1 else 0
    if queue_max is not None:
        updates["queue_max"] = max(0, int(queue_max))
    return replace(cfg, worker=replace(worker, **updates))


def load_token(cfg: Config) -> str:
    if cfg.worker.token.strip():
        return cfg.worker.token.strip()
    if cfg.worker.token_file.strip():
        path = Path(cfg.worker.token_file).expanduser()
        if not path.is_absolute():
            path = cfg.root / path
        if path.is_file():
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
            raise ValueError(f"no token in {path}")
    from .login import load_saved_token
    return load_saved_token()


def run_from_args(cfg: Config, args) -> int:
    cfg = resolve_worker(
        cfg,
        coordinator=getattr(args, "coordinator", None),
        token=getattr(args, "token", None),
        token_file=getattr(args, "token_file", None),
        procs=getattr(args, "procs", None),
        threads=getattr(args, "threads", None),
        priority=getattr(args, "priority", None),
        max_cache_gb=getattr(args, "max_cache_gb", None),
        min_free_gb=getattr(args, "min_free_gb", None),
        poll_s=getattr(args, "poll_seconds", None),
        cache_dir_value=getattr(args, "cache_dir", None),
        prefetch=getattr(args, "prefetch", None),
    )
    try:
        if getattr(args, "login", False):
            from .login import DeviceLoginError, link_this_computer, token_path
            try:
                token = link_this_computer(
                    cfg.worker.coordinator_url,
                    open_browser=not getattr(args, "no_browser", False),
                    on_verification_url=lambda url: print(f"Approve this computer: {url}", flush=True),
                )
            except (DeviceLoginError, OSError, TimeoutError) as exc:
                print(f"rhexas worker: {exc}", file=sys.stderr)
                return 2
            print(f"Saved this computer's token to {token_path()}", flush=True)
            cfg = replace(cfg, worker=replace(cfg.worker, token=token, token_file=""))
        if not load_token(cfg):
            print(
                "rhexas worker: set a token with --login, --token-file, --token, or RHEXAS_WORKER_TOKEN",
                file=sys.stderr,
            )
            return 2
        if getattr(args, "dashboard", False):
            from .dashboard import fallback_resources, launch_in_thread
            try:
                launch_in_thread(
                    cache_dir(cfg),
                    host=getattr(args, "dashboard_host", None),
                    port=getattr(args, "dashboard_port", None),
                    open_browser=not getattr(args, "no_browser", False),
                    fallback=fallback_resources(cfg),
                )
            except (OSError, ValueError) as exc:
                print(f"rhexas worker: dashboard did not start ({exc})", file=sys.stderr)
        return run_worker(cfg, once=bool(getattr(args, "once", False)))
    except KeyboardInterrupt:
        print("\nstopped. The lease is still yours until it expires; run the same command to resume.", flush=True)
        return 0
    except ValueError as exc:
        print(f"rhexas worker: {exc}", file=sys.stderr)
        return 2
