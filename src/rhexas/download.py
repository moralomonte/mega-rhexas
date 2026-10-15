"""Download SPOC 2-minute light curves for one sector, or for a TIC batch inside it.

Whole-sector behavior: MAST curl script, per-sector
lock, skip existing files, HTTP 429/503 backoff, stop when free space or the
project cap is exceeded (exit code 2).
"""
from __future__ import annotations

import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from .config import Config
from .disk import disk_limit_hit, free_gb, project_gb
from .tics import filename_from_url, tic_from_filename

CURL_URL = "https://archive.stsci.edu/missions/tess/download_scripts/sector/tesscurl_sector_{sector}_lc.sh"


def parse_curl_script(text: str) -> list[str]:
    return re.findall(r"(https\S+lc\.fits)", text)


def select_urls(urls: list[str], *, tics: set[int] | None, limit: int) -> list[str]:
    if tics is not None:
        urls = [u for u in urls if tic_from_filename(filename_from_url(u)) in tics]
    if limit:
        urls = urls[:limit]
    return urls


def consider_disk_limit(state: dict, cfg: Config, base: float) -> None:
    """Historical check: every 200 attempted files, stop if the disk guard trips."""
    if state["n"] % 200 != 0:
        return
    fg = free_gb(cfg.data_dir)
    pg = base + state["added"]
    print(f"{time.strftime('%H:%M:%S')} downloaded {state['n']}  free={fg:.1f}GB project~{pg:.1f}GB", flush=True)
    if disk_limit_hit(fg, pg, cfg.min_free_gb, cfg.max_project_gb):
        print("DISK LIMIT REACHED - stopping download", flush=True)
        state["stop"] = True


def _acquire_lock(out: Path, sector: int, sleeper) -> Path:
    import psutil
    lock = out / ".download.lock"
    waited = False
    while lock.exists():
        try:
            other = int(lock.read_text().strip() or 0)
        except Exception:
            other = 0
        if not other or other == os.getpid() or not psutil.pid_exists(other):
            break
        if not waited:
            print(f"sector {sector}: another downloader (pid {other}) is active, waiting for it", flush=True)
            waited = True
        sleeper(30)
    if waited:
        print(f"sector {sector}: other downloader finished, resuming (existing files are skipped)", flush=True)
    lock.write_text(str(os.getpid()))
    return lock


def retry_delay(base: float, factor: float, attempt: int, *, jitter: float = 0.0, rng=None) -> float:
    """Exponential backoff with equal jitter.

    The midpoint (``rng`` returns 0.5, or ``jitter`` is 0) is ``base * factor**attempt``.
    Otherwise the delay is that value scaled by a uniform draw from ``1-jitter`` to ``1+jitter``.
    """
    attempt = max(0, int(attempt))
    raw = float(base) * (float(factor) ** attempt)
    span = float(jitter or 0.0)
    if span <= 0 or raw == 0:
        return raw
    if span > 1:
        span = 1.0
    roll = float((rng or random.random)())
    if roll < 0:
        roll = 0.0
    elif roll > 1:
        roll = 1.0
    scale = (1.0 - span) + (2.0 * span) * roll
    return raw * scale


def error_kind(exc: BaseException) -> str:
    """Short label for a transfer failure. The exception text is not returned."""
    text = f"{type(exc).__name__} {exc}".lower()
    if "eof" in text or "ssl" in text or "unexpected_eof" in text:
        return "SSL EOF"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    return type(exc).__name__


def _format_delay(delay: float) -> str:
    if abs(delay - round(delay)) < 1e-9:
        return str(int(round(delay)))
    return f"{delay:.1f}"


def file_ready(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _close_response(response) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _call_getter(getter, url: str, timeout: float, headers: dict | None):
    """Call ``getter`` the way the offline tests do, and stream when it is requests."""
    kwargs = {"timeout": timeout}
    if headers:
        kwargs["headers"] = dict(headers)
    if getter is requests.get:
        kwargs["stream"] = True
    try:
        return getter(url, **kwargs)
    except TypeError:
        if headers:
            try:
                return getter(url, timeout=timeout, headers=headers)
            except TypeError:
                return getter(url, timeout=timeout)
        try:
            return getter(url, timeout=timeout)
        except TypeError:
            return getter(url)


def _write_body(response, dest: Path, *, append: bool) -> None:
    """Write the body to ``dest``. A ``chunks`` sequence of ``None`` is an SSL EOF mid-file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    mode = "ab" if append else "wb"
    pieces = getattr(response, "chunks", None)
    if pieces is None:
        iterator = getattr(response, "iter_content", None)
        if callable(iterator):
            pieces = iterator(chunk_size=256 * 1024)
        else:
            pieces = (getattr(response, "content", None) or b"",)
    with dest.open(mode) as handle:
        for chunk in pieces:
            if chunk is None:
                raise ConnectionError("SSL EOF")
            if chunk:
                handle.write(chunk)
        handle.flush()


def fetch_one(url: str, dest: Path, *, getter, sleeper, dl, rng=None, hold=None, stop=None) -> bool:
    """Download one light curve. A partial ``.part`` file is resumed with HTTP Range.

    Returns True when ``dest`` has bytes. Retries use exponential backoff and jitter.
    """
    dest = Path(dest)
    if file_ready(dest):
        return True
    part = dest.with_suffix(".part")
    attempts = max(1, int(dl.retries))
    for attempt in range(attempts):
        if stop is not None and stop():
            return file_ready(dest)
        if hold is not None:
            hold()
        offset = part.stat().st_size if part.is_file() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else None
        try:
            response = _call_getter(getter, url, dl.timeout_s, headers)
        except Exception as exc:
            delay = retry_delay(dl.retry_base_s, dl.throttle_factor, attempt, jitter=dl.jitter, rng=rng)
            print(f"retry {dest.name} {error_kind(exc)} - waiting {_format_delay(delay)}s", flush=True)
            sleeper(delay)
            continue
        status = int(getattr(response, "status_code", 0) or 0)
        if status in (429, 503):
            _close_response(response)
            delay = retry_delay(dl.throttle_base_s, dl.throttle_factor, attempt, jitter=dl.jitter, rng=rng)
            print(f"THROTTLED {status} {dest.name} - backing off {_format_delay(delay)}s", flush=True)
            sleeper(delay)
            continue
        if status == 416 and offset:
            _close_response(response)
            part.unlink(missing_ok=True)
            continue
        try:
            if status >= 400 or status == 0:
                _close_response(response)
                raise RuntimeError(f"HTTP {status}")
            append = status == 206 and offset > 0
            _write_body(response, part, append=append)
        except Exception as exc:
            _close_response(response)
            delay = retry_delay(dl.retry_base_s, dl.throttle_factor, attempt, jitter=dl.jitter, rng=rng)
            print(f"retry {dest.name} {error_kind(exc)} - waiting {_format_delay(delay)}s", flush=True)
            sleeper(delay)
            continue
        else:
            _close_response(response)
        if file_ready(part):
            part.replace(dest)
            return True
    return file_ready(dest)


def missing_urls(urls: list[str], out: Path) -> list[str]:
    """URLs whose light curve is not a non-empty file yet."""
    missing = []
    for url in urls:
        if not file_ready(Path(out) / filename_from_url(url)):
            missing.append(url)
    return missing


def run(cfg: Config, *, limit: int = 0, threads: int | None = None, tics: set[int] | None = None,
        getter=None, sleeper=None, rng=None, hold=None) -> int:
    """Download into ``<data_dir>/sNNNN``. Return 0, or 2 if the disk guard stops the run.

    Each file is retried with exponential backoff and jitter. A partial download is
    resumed. After the main pass, stars that still have no file get one more pass
    before this function returns.
    """
    getter = getter or requests.get
    sleeper = sleeper or time.sleep
    rng = rng or random.random
    dl = cfg.download
    threads = dl.threads if threads is None else threads
    out = cfg.sector_data
    out.mkdir(parents=True, exist_ok=True)
    lock = None
    try:
        lock = _acquire_lock(out, cfg.sector, sleeper)
        url = CURL_URL.format(sector=cfg.sector)
        urls = select_urls(parse_curl_script(getter(url, timeout=dl.curl_timeout_s).text), tics=tics, limit=limit)
        print(f"sector {cfg.sector}: {len(urls)} files", flush=True)
        if tics is not None:
            found = {tic_from_filename(filename_from_url(u)) for u in urls}
            absent = tics - found
            if absent:
                print(f"sector {cfg.sector}: {len(absent)} TIC IDs not in the MAST file list", flush=True)
        base = project_gb(cfg.data_dir, cfg.env_overhead_gb)
        state = {"n": 0, "stop": False, "added": 0.0}

        def get(one_url: str) -> None:
            if state["stop"]:
                return
            dest = out / filename_from_url(one_url)
            if file_ready(dest):
                return
            before = dest.stat().st_size if dest.is_file() else 0
            fetch_one(
                one_url, dest, getter=getter, sleeper=sleeper, dl=dl, rng=rng, hold=hold,
                stop=lambda: bool(state["stop"]),
            )
            if file_ready(dest):
                state["added"] += max(0, dest.stat().st_size - before) / 1e9
            state["n"] += 1
            consider_disk_limit(state, cfg, base)

        with ThreadPoolExecutor(threads) as ex:
            list(ex.map(get, urls))
        if not state["stop"]:
            leftover = missing_urls(urls, out)
            if leftover:
                print(
                    f"sector {cfg.sector}: {len(leftover)} stars still missing, retrying them",
                    flush=True,
                )
                for one_url in leftover:
                    if state["stop"]:
                        break
                    get(one_url)
        print("download finished" + (" (stopped on disk limit)" if state["stop"] else ""), flush=True)
        if not state["stop"]:
            (out / ".download.done").write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
        return 2 if state["stop"] else 0
    finally:
        if lock is not None:
            try:
                lock.unlink()
            except Exception:
                pass
