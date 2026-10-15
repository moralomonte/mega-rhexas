"""Sequential ``run`` / ``rerun`` and the overlapping ``queue``.

``run_sectors`` downloads, then searches, then filters, one sector after another.
``queue`` overlaps one download with one search. Both write a pid file the
dashboard reads. ``rerun`` is the same path with downloads skipped when
``data/sNNNN/.download.done`` already exists, plus vetting of NEW candidates.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .config import Config


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            stream.write(data)
            stream.flush()

    def flush(self):
        for stream in self.streams:
            stream.flush()


def default_run_log(cfg: Config) -> Path:
    """Per-sector log the dashboard tails: ``logs/run_sNNNN.log``."""
    return cfg.logs_dir / f"run_s{cfg.sector:04d}.log"


def download_log(cfg: Config) -> Path:
    return cfg.logs_dir / f"download_s{cfg.sector:04d}.log"


def pipeline_pid_files(logs_dir: Path, sector: int) -> list[Path]:
    """Pid files the dashboard treats as 'the pipeline is running' for this sector."""
    logs_dir = Path(logs_dir)
    return [
        logs_dir / "pipeline.pid",
        logs_dir / "pipeline_queue.pid",
        logs_dir / f"run_s{int(sector):04d}.pid",
    ]


def download_done(cfg: Config) -> Path:
    return cfg.sector_data / ".download.done"


@contextmanager
def holding_pid(path: Path):
    """Write our pid, and remove the file on the way out if it is still ours."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        yield path
    finally:
        try:
            current = path.read_text(encoding="utf-8").strip().split()[0]
        except Exception:
            current = ""
        if current == str(os.getpid()):
            path.unlink(missing_ok=True)


def _vet_new(cfg: Config) -> None:
    from .vet import run_new
    run_new(cfg)


def run_sectors(cfg: Config, sectors: list[int], *, tics: set[int] | None = None, limit: int = 0,
                threads: int | None = None, procs: int | None = None, plots: int | None = None,
                skip_done_downloads: bool = False, vet: bool = False) -> int:
    from . import download, filter as filt, search
    code = 0
    with holding_pid(cfg.logs_dir / "pipeline.pid"):
        for sector in sectors:
            c = cfg.for_sector(sector)
            log_path = default_run_log(c)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with holding_pid(c.logs_dir / f"run_s{sector:04d}.pid"):
                with open(log_path, "a", encoding="utf-8") as fh:
                    old = sys.stdout
                    sys.stdout = _Tee(old, fh)
                    try:
                        print(f"=== SECTOR {sector} START {time.strftime('%Y-%m-%d %H:%M:%S')} data={c.data_dir} ===", flush=True)
                        done = download_done(c)
                        if skip_done_downloads and done.is_file():
                            print(f"sector {sector}: download already complete (.download.done), skipping", flush=True)
                        else:
                            rc = download.run(c, limit=limit, threads=threads, tics=tics)
                            if rc == 2:
                                print("DISK LIMIT - stopping multi-sector run", flush=True)
                                code = 2
                                break
                        search.run(c, procs=procs, limit=limit, tics=tics)
                        filt.run(c, plots=plots, tics=tics)
                        if vet:
                            _vet_new(c)
                        print(f"=== SECTOR {sector} ALL DONE {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)
                    finally:
                        sys.stdout = old
    return code


def _is_download_cmd(argv: list[str]) -> bool:
    if any("download.py" in part for part in argv):
        return True
    has_rhexas = any(part == "-m" or part.endswith("rhexas") or part.endswith("rhexas.exe") for part in argv)
    return has_rhexas and "download" in argv


def other_download_running() -> bool:
    """True when some other process is already running a sector download."""
    import psutil
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if proc.info["pid"] == me:
                continue
            cmd = list(proc.info.get("cmdline") or [])
        except Exception:
            continue
        if _is_download_cmd(cmd):
            return True
    return False


def _child(cfg: Config, sub: str, extra: list[str]) -> list[str]:
    cmd = [
        sys.executable, "-m", "rhexas", sub,
        "--root", str(cfg.root),
        "--sector", str(cfg.sector),
        "--data-dir", str(cfg.data_dir),
        "--results-dir", str(cfg.results_root),
        "--logs-dir", str(cfg.logs_dir),
        "--catalogs-dir", str(cfg.catalogs_dir),
        "--min-free-gb", str(cfg.min_free_gb),
        "--max-project-gb", str(cfg.max_project_gb),
    ]
    if cfg.multisector_dir is not None:
        cmd += ["--multisector-dir", str(cfg.multisector_dir)]
    return cmd + extra


def _tics_args(cfg: Config, tics: set[int] | None) -> list[str]:
    if tics is None:
        return []
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.logs_dir / f"batch_s{cfg.sector:04d}.tics"
    path.write_text("\n".join(str(t) for t in sorted(tics)) + "\n", encoding="utf-8")
    return ["--tics-file", str(path)]


def _run_logged(cmd: list[str], log_path: Path, cwd: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=str(cwd))
    return proc.returncode


def queue(cfg: Config, sectors: list[int], *, predownload: list[int] | None = None, wait_pid: int = 0,
          tics: set[int] | None = None, threads: int | None = None, procs: int | None = None,
          plots: int | None = None, poll_s: float = 60.0, skip_done_downloads: bool = False,
          vet: bool = False) -> int:
    """Overlap one download with one search. ``predownload`` sectors are fetched but not searched here.

    With ``skip_done_downloads``, a sector that already has ``.download.done`` is not
    downloaded again. ``vet`` runs ``vet --new`` after filter.
    """
    import psutil
    predownload = list(predownload or [])
    log_path = cfg.logs_dir / "pipeline_queue.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def L(msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(line, flush=True)

    download_sectors = predownload + list(sectors)
    holder: dict = {}

    def download_worker() -> None:
        try:
            for s in download_sectors:
                c = cfg.for_sector(s)
                if skip_done_downloads and download_done(c).is_file():
                    L(f"download worker SKIP sector {s} (.download.done)")
                    continue
                while other_download_running():
                    time.sleep(min(30.0, poll_s))
                dest = download_log(c)
                L(f"download worker START sector {s}")
                extra = ["--threads", str(threads or c.download.threads)] + _tics_args(c, tics)
                rc = _run_logged(_child(c, "download", extra), dest, c.root)
                L(f"download worker DONE sector {s} (exit {rc})")
                if rc == 2:
                    L("DISK LIMIT - download worker stopping")
                    holder["code"] = 2
                    return
            holder["code"] = holder.get("code", 0)
        except Exception as exc:
            holder["error"] = exc
            L(f"download worker ERROR {exc}")

    with holding_pid(cfg.logs_dir / "pipeline_queue.pid"):
        L(
            "queue start: predownload="
            + ",".join(map(str, predownload))
            + " queue="
            + ",".join(map(str, sectors))
            + f" waitPid={wait_pid} limit={cfg.max_project_gb:g}GB minFree={cfg.min_free_gb:g}GB"
            + (" rerun=1" if skip_done_downloads else "")
        )
        thread = threading.Thread(target=download_worker, name="rhexas-download", daemon=False)
        thread.start()
        if wait_pid:
            while psutil.pid_exists(wait_pid):
                time.sleep(poll_s)
            L(f"wait pid {wait_pid} finished; search worker starting")
        code = 0
        for s in sectors:
            c = cfg.for_sector(s)
            done = download_done(c)
            while not done.exists():
                if not thread.is_alive() and not done.exists():
                    L(f"download worker ended before sector {s} completed - stopping")
                    break
                time.sleep(poll_s)
            if not done.exists():
                code = holder.get("code") or 1
                break
            lpath = default_run_log(c)
            with holding_pid(c.logs_dir / f"run_s{s:04d}.pid"):
                with open(lpath, "a", encoding="utf-8") as fh:
                    fh.write(f"=== SECTOR {s} START {time.strftime('%Y-%m-%d %H:%M:%S')} (pipelined) ===\n")
                    nfits = len(list(c.sector_data.glob("*lc.fits")))
                    fh.write(f"sector {s}: {nfits} files\n")
                L(f"search START sector {s}")
                extra_s = ["--procs", str(procs or c.search.procs)] + _tics_args(c, tics)
                _run_logged(_child(c, "search", extra_s), lpath, c.root)
                extra_f = _tics_args(c, tics)
                if plots is not None:
                    extra_f += ["--plots", str(plots)]
                _run_logged(_child(c, "filter", extra_f), lpath, c.root)
                if vet:
                    L(f"vet START sector {s}")
                    _run_logged(_child(c, "vet", ["--new"]), lpath, c.root)
                with open(lpath, "a", encoding="utf-8") as fh:
                    fh.write(f"=== SECTOR {s} ALL DONE {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                L(f"search+filter DONE sector {s}")
        thread.join()
        if holder.get("error"):
            raise holder["error"]
        L("queue finished")
    return holder.get("code", code)
