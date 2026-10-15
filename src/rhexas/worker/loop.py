"""Claim a batch, run the existing download/search/filter path, upload, delete.

The scientific settings (period grid, SDE, SNR, durations) are the pipeline's.
This module only changes where the files go, how many processes run, and the
worker cache limits.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from rhexas.config import Config

from .cache import delete_lightcurves, enforce_cache_cap
from .client import CoordinatorClient, ProtocolError
from .priority import apply_priority

# One worker process. Download threads read these; they are not thread-locals.
_stop_event: threading.Event | None = None
_hold_fn = None


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
    data = Path(cache) / "data"
    data.mkdir(parents=True, exist_ok=True)
    enforce_cache_cap(data, int(cfg.worker.max_cache_gb * 1e9))
    need = cfg.worker.min_free_gb * 1e9
    free = shutil.disk_usage(data).free
    if free >= need:
        return
    enforce_cache_cap(data, 0)
    free = shutil.disk_usage(data).free
    if free < need:
        raise RuntimeError(
            f"only {free / 1e9:.1f} GB free after emptying the worker cache; "
            f"worker.min_free_gb is {cfg.worker.min_free_gb}"
        )


def _state_path(cache: Path) -> Path:
    return Path(cache) / "state.json"


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
    path.write_text(json.dumps(state), encoding="utf-8")


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
    code = run(cfg, threads=cfg.download.threads, tics=set(tics), hold=_hold_fn)
    if code == 2:
        raise RuntimeError("disk limit stopped the download")
    return code


def default_filter(cfg: Config, tics) -> int:
    from rhexas.filter import run
    return run(cfg, plots=0, tics=set(tics))


def start_heartbeat(client, assignment_id: int, interval: float, *, on_result=None, progress: HeartbeatProgress | None = None):
    """Ping the coordinator until the returned event is set. ``interval <= 0`` disables it.

    ``progress`` is sent only after the worker has a fraction. A call with no progress
    matches workers that predate the field.
    """
    if interval <= 0:
        return None

    stop = threading.Event()

    def loop():
        while not stop.wait(interval):
            try:
                snap = progress.get() if progress is not None else None
                if snap:
                    client.heartbeat(assignment_id, progress=snap)
                else:
                    client.heartbeat(assignment_id)
            except Exception as exc:
                print(f"heartbeat failed: {exc}", flush=True)
                if on_result is not None:
                    try:
                        on_result(False, "heartbeat failed")
                    except Exception:
                        pass
                continue
            if on_result is not None:
                try:
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
    """Read the dashboard's requested speed and publish the load now in effect."""
    from .live import pace_settings
    baseline = {
        "procs": cfg.worker.procs,
        "threads": cfg.worker.threads,
        "priority": cfg.worker.priority,
    }
    stored = _live_call(live, "baseline")
    if isinstance(stored, dict) and stored.get("procs"):
        baseline = stored
    requested = _live_call(live, "requested_mode") or "live"
    settings = pace_settings(str(requested), baseline)
    _live_call(live, "publish_pace", settings)
    signature = (
        settings["mode"], settings["procs"], settings["threads"],
        settings["priority"], settings["paused"],
    )
    if signature != memory.get("sig"):
        memory["sig"] = signature
        if settings["paused"]:
            print("pace eco: paused between stars; this batch stays leased", flush=True)
        else:
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
    """Hold here while eco is selected, or the tray Pause is on. The lease stays."""
    while True:
        _check_stop()
        settings = current_pace(cfg, live, memory)
        if not settings["paused"] and not _user_paused(live):
            return settings
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


def _next_assignment(client, cache: Path):
    path = _state_path(cache)
    local = _read_state(path)
    current = client.current().get("assignment")
    if current is None:
        if local:
            discard_local(cache, local)
            _clear_state(path)
        current = client.claim().get("assignment")
        if current is None:
            return None
        state = {**current, "stage": "claimed"}
        _write_state(path, state)
        return state
    if local and local.get("assignment_id") == current["assignment_id"]:
        state = {**current, "stage": local.get("stage") or "claimed"}
    else:
        if local:
            discard_local(cache, local)
        state = {**current, "stage": "claimed"}
    _write_state(path, state)
    return state


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


def _watch_downloads(live, folder: Path, total: int, stop: threading.Event, progress: HeartbeatProgress | None = None) -> None:
    """Count FITS files already on disk so the dashboard can move during a download."""
    started = time.time()
    while not stop.is_set():
        try:
            found = sum(1 for path in Path(folder).glob("*lc.fits") if path.is_file())
        except OSError:
            found = 0
        if stop.is_set():
            return
        done = min(found, total)
        if progress is not None:
            progress.set("download", done, total)
        elapsed = time.time() - started
        rate = (found / elapsed) if elapsed >= 1 and found else None
        eta = ((total - found) / rate / 60.0) if rate else None
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
             progress: HeartbeatProgress | None = None, pace_memory=None, sleeper=None) -> None:
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
    _live_call(live, "begin_batch", assignment, stage)
    if stage == "claimed":
        settings = wait_until_running(cfg, live, memory, pause_sleep)
        ucfg = _with_pace(ucfg, settings)
        _live_call(
            live, "update_batch", stage="downloading", progress_kind="files",
            stars_done=0, stars_total=n_tics, per_s=None, eta_min=None,
        )
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
    if stage == "downloaded":
        _live_call(
            live, "update_batch", stage="searching", progress_kind="stars",
            stars_total=n_tics, per_s=None, eta_min=None,
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
            return current_pace(cfg, live, memory)

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
        _live_call(live, "update_batch", stage="uploading", progress_kind="stars", eta_min=0, per_s=None)
        if progress is not None:
            progress.set("upload", n_tics, n_tics)
        result = client.submit(assignment["assignment_id"], stars, candidates)
        _live_call(live, "note_submit", result if isinstance(result, dict) else {}, len(stars))
        delete_lightcurves(ucfg.sector_data, tic_set)
        shutil.rmtree(ucfg.results_root, ignore_errors=True)
        _clear_state(path)
        enforce_cache_cap(cache / "data", int(cfg.worker.max_cache_gb * 1e9))
        _live_call(
            live, "update_batch", stage="uploaded", active=False, progress_kind="stars",
            stars_done=n_tics, stars_total=n_tics, eta_min=0, per_s=None,
        )


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
    try:
        try:
            apply_priority(cfg.worker.priority)
            print(f"process priority: {cfg.worker.priority}", flush=True)
        except Exception as exc:
            print(f"could not set process priority ({exc}); continuing", flush=True)
        print(
            f"coordinator {cfg.worker.coordinator_url}  procs={cfg.worker.procs}  "
            f"threads={cfg.worker.threads}  cache={cache}",
            flush=True,
        )
        pace_memory: dict = {}
        current_pace(cfg, live, pace_memory)
        attempt = 0
        while True:
            _check_stop()
            if _user_paused(live) and not pace_memory.get("announced_pause"):
                print("paused. Resume from the Mega Rhexas menu to continue.", flush=True)
                pace_memory["announced_pause"] = True
            while _user_paused(live):
                _check_stop()
                sleeper(0.4)
            pace_memory["announced_pause"] = False
            try:
                assignment = _next_assignment(client, cache)
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
            beat = HeartbeatProgress()
            stop = start_heartbeat(
                client, assignment["assignment_id"], heartbeat_s,
                on_result=lambda ok, detail: _live_call(live, "set_connection", ok, detail),
                progress=beat,
            )
            try:
                run_unit(
                    cfg, cache, client, assignment,
                    download=download, search=search, filt=filt, live=live, progress=beat,
                    pace_memory=pace_memory, sleeper=sleeper,
                )
            except WorkerStopped:
                if stop is not None:
                    stop.set()
                return 0
            except ProtocolError as exc:
                if stop is not None:
                    stop.set()
                if exc.status == 409:
                    print(f"unit {assignment['assignment_id']} is no longer ours ({exc}); dropping local files", flush=True)
                    discard_local(cache, assignment)
                    _clear_state(_state_path(cache))
                    _live_call(live, "update_batch", stage="released", active=False)
                    if once:
                        return 0
                    continue
                print(f"upload failed ({exc.status}): {exc}", flush=True)
                _live_call(live, "set_connection", False, f"upload rejected ({exc.status})")
                if once:
                    return 1
                _paced_sleep(cfg, live, pace_memory, sleeper, cfg.worker.poll_s)
                continue
            except Exception as exc:
                if stop is not None:
                    stop.set()
                print(f"unit {assignment['assignment_id']} failed: {exc}", flush=True)
                _live_call(live, "update_batch", stage="error", active=True)
                if once:
                    return 1
                _paced_sleep(cfg, live, pace_memory, sleeper, cfg.worker.poll_s)
                continue
            if stop is not None:
                stop.set()
            print(f"unit {assignment['assignment_id']} uploaded; light curves deleted", flush=True)
            if once:
                return 0
    except WorkerStopped:
        return 0
    finally:
        _stop_event = previous_stop
        _close_local(live, log, old_out, old_err)


def resolve_worker(cfg: Config, *, coordinator=None, token=None, token_file=None, procs=None, threads=None,
                   priority=None, max_cache_gb=None, min_free_gb=None, poll_s=None, cache_dir_value=None,
                   environ=None) -> Config:
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
