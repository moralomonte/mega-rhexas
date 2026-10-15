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
    code = run(cfg, threads=cfg.download.threads, tics=set(tics))
    if code == 2:
        raise RuntimeError("disk limit stopped the download")
    return code


def default_search(cfg: Config, tics, procs: int) -> int:
    from rhexas.search import run
    return run(cfg, procs=procs, tics=set(tics))


def default_filter(cfg: Config, tics) -> int:
    from rhexas.filter import run
    return run(cfg, plots=0, tics=set(tics))


def start_heartbeat(client, assignment_id: int, interval: float):
    """Ping the coordinator until the returned event is set. ``interval <= 0`` disables it."""
    if interval <= 0:
        return None

    stop = threading.Event()

    def loop():
        while not stop.wait(interval):
            try:
                client.heartbeat(assignment_id)
            except Exception as exc:
                print(f"heartbeat failed: {exc}", flush=True)

    threading.Thread(target=loop, name="rhexas-heartbeat", daemon=True).start()
    return stop


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


def run_unit(cfg: Config, cache: Path, client, assignment: dict, *, download, search, filt) -> None:
    """Download, search, and filter one leased batch, then upload and delete it."""
    path = _state_path(cache)
    state = _read_state(path) or {**assignment, "stage": "claimed"}
    if state.get("assignment_id") != assignment["assignment_id"]:
        state = {**assignment, "stage": "claimed"}
    ucfg = unit_config(cfg, assignment, cache)
    tics = [int(tic) for tic in assignment["tics"]]
    tic_set = set(tics)
    stage = state.get("stage") or "claimed"
    if stage == "claimed":
        ensure_space(cache, cfg)
        download(ucfg, tic_set)
        state = _advance(path, state, "downloaded")
        stage = "downloaded"
    if stage == "downloaded":
        search(ucfg, tic_set, ucfg.search.procs)
        state = _advance(path, state, "searched")
        stage = "searched"
    if stage == "searched":
        filt(ucfg, tic_set)
        state = _advance(path, state, "filtered")
        stage = "filtered"
    if stage == "filtered":
        stars = read_stars(ucfg.sector_results, tics)
        candidates = read_candidates(ucfg.sector_results)
        client.submit(assignment["assignment_id"], stars, candidates)
        delete_lightcurves(ucfg.sector_data, tic_set)
        shutil.rmtree(ucfg.results_root, ignore_errors=True)
        _clear_state(path)
        enforce_cache_cap(cache / "data", int(cfg.worker.max_cache_gb * 1e9))


def run_worker(cfg: Config, *, once: bool = False, client=None, download=None, search=None, filt=None,
               sleeper=time.sleep, heartbeat_s: float = 300) -> int:
    token = load_token(cfg)
    if client is None:
        if not cfg.worker.coordinator_url or not token:
            print("rhexas worker: set the coordinator URL and a token", file=sys.stderr)
            return 2
        client = CoordinatorClient(cfg.worker.coordinator_url, token)
    download = download or default_download
    search = search or default_search
    filt = filt or default_filter
    try:
        apply_priority(cfg.worker.priority)
        print(f"process priority: {cfg.worker.priority}", flush=True)
    except Exception as exc:
        print(f"could not set process priority ({exc}); continuing", flush=True)
    cache = cache_dir(cfg)
    cache.mkdir(parents=True, exist_ok=True)
    print(
        f"coordinator {cfg.worker.coordinator_url}  procs={cfg.worker.procs}  "
        f"threads={cfg.worker.threads}  cache={cache}",
        flush=True,
    )
    attempt = 0
    while True:
        try:
            assignment = _next_assignment(client, cache)
            attempt = 0
        except ProtocolError as exc:
            print(f"coordinator rejected the request ({exc.status}): {exc}", flush=True)
            if once:
                return 1
            sleeper(backoff_s(attempt))
            attempt += 1
            continue
        except Exception as exc:
            print(f"coordinator unreachable: {exc}", flush=True)
            if once:
                return 1
            sleeper(backoff_s(attempt))
            attempt += 1
            continue
        if assignment is None:
            print("no work right now", flush=True)
            if once:
                return 0
            sleeper(cfg.worker.poll_s)
            continue
        print(
            f"unit {assignment['assignment_id']} sector {assignment['sector']} "
            f"({assignment['n_tics']} stars) stage {assignment.get('stage')}",
            flush=True,
        )
        stop = start_heartbeat(client, assignment["assignment_id"], heartbeat_s)
        try:
            run_unit(cfg, cache, client, assignment, download=download, search=search, filt=filt)
        except ProtocolError as exc:
            if stop is not None:
                stop.set()
            if exc.status == 409:
                print(f"unit {assignment['assignment_id']} is no longer ours ({exc}); dropping local files", flush=True)
                discard_local(cache, assignment)
                _clear_state(_state_path(cache))
                if once:
                    return 0
                continue
            print(f"upload failed ({exc.status}): {exc}", flush=True)
            if once:
                return 1
            sleeper(cfg.worker.poll_s)
            continue
        except Exception as exc:
            if stop is not None:
                stop.set()
            print(f"unit {assignment['assignment_id']} failed: {exc}", flush=True)
            if once:
                return 1
            sleeper(cfg.worker.poll_s)
            continue
        if stop is not None:
            stop.set()
        print(f"unit {assignment['assignment_id']} uploaded; light curves deleted", flush=True)
        if once:
            return 0


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
    if not cfg.worker.token_file.strip():
        return ""
    path = Path(cfg.worker.token_file).expanduser()
    if not path.is_absolute():
        path = cfg.root / path
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    raise ValueError(f"no token in {path}")


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
    if not load_token(cfg):
        print("rhexas worker: set a token with --token-file, --token, or RHEXAS_WORKER_TOKEN", file=sys.stderr)
        return 2
    try:
        return run_worker(cfg, once=bool(getattr(args, "once", False)))
    except KeyboardInterrupt:
        print("\nstopped. The lease is still yours until it expires; run the same command to resume.", flush=True)
        return 0
    except ValueError as exc:
        print(f"rhexas worker: {exc}", file=sys.stderr)
        return 2
