"""Download SPOC 2-minute light curves for one sector, or for a TIC batch inside it.

Whole-sector behavior: MAST curl script, per-sector
lock, skip existing files, HTTP 429/503 backoff, stop when free space or the
project cap is exceeded (exit code 2).
"""
from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

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


def run(cfg: Config, *, limit: int = 0, threads: int | None = None, tics: set[int] | None = None,
        getter=None, sleeper=None) -> int:
    """Download into ``<data_dir>/sNNNN``. Return 0, or 2 if the disk guard stops the run."""
    getter = getter or requests.get
    sleeper = sleeper or time.sleep
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
            missing = tics - found
            if missing:
                print(f"sector {cfg.sector}: {len(missing)} TIC IDs not in the MAST file list", flush=True)
        base = project_gb(cfg.data_dir, cfg.env_overhead_gb)
        state = {"n": 0, "stop": False, "added": 0.0}

        def get(u):
            if state["stop"]:
                return
            name = filename_from_url(u)
            f = out / name
            if f.exists() and f.stat().st_size > 0:
                return
            for t in range(dl.retries):
                try:
                    r = getter(u, timeout=dl.timeout_s)
                    if r.status_code in (429, 503):
                        delay = dl.throttle_base_s * dl.throttle_factor ** t
                        print(f"THROTTLED {r.status_code} {f.name} - backing off {delay}s", flush=True)
                        sleeper(delay)
                        continue
                    r.raise_for_status()
                    tmp = f.with_suffix(".part")
                    tmp.write_bytes(r.content)
                    tmp.replace(f)
                    state["added"] += len(r.content) / 1e9
                    break
                except Exception as e:
                    print("retry", f.name, e, flush=True)
                    sleeper(dl.retry_base_s * dl.throttle_factor ** t)
            state["n"] += 1
            consider_disk_limit(state, cfg, base)

        with ThreadPoolExecutor(threads) as ex:
            list(ex.map(get, urls))
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
