"""Move or delete processed products. Downloaded light curves stay put.

``reset-results`` archives (the default) or deletes BLS tables, candidate
tables, plots, vetting output, and pipeline logs. It does not touch
``data/sNNNN/*lc.fits``, ``.download.done``, or the shared catalog CSVs.
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

from .config import Config

# Files that used to sit in results/ itself when sector 107 was special-cased.
_LEGACY_FILES = (
    "bls_raw.csv",
    "candidates.csv",
    "smoke_raw.csv",
    "smoke_candidates.csv",
)
_LEGACY_DIRS = ("plots", "vetting")
_CATALOGS = {"toi_catalog.csv", "confirmed_planets.csv"}
_SHARED_LOGS = (
    "full_run.log",
    "full_run.pid",
    "multi_run.pid",
    "pipeline_queue.log",
    "pipeline_queue.pid",
    "pipeline_queue2.pid",
    "pipeline.pid",
)
_SECTOR_DIR = re.compile(r"s(\d{4})$")
_SECTOR_LOG = re.compile(r"(?:run|download)_s(\d{4})\.(?:log|pid)$")


def _contained(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _sector_dirs(cfg: Config) -> dict[int, Path]:
    found = {}
    root = cfg.results_root
    if not root.is_dir():
        return found
    for child in root.iterdir():
        match = _SECTOR_DIR.fullmatch(child.name)
        if match and child.is_dir():
            found[int(match.group(1))] = child
    return found


def _sectors_from_logs(cfg: Config) -> set[int]:
    found = set()
    if not cfg.logs_dir.is_dir():
        return found
    for child in cfg.logs_dir.iterdir():
        match = _SECTOR_LOG.fullmatch(child.name)
        if match:
            found.add(int(match.group(1)))
    return found


def _legacy_root_present(cfg: Config) -> bool:
    root = cfg.results_root
    if any((root / name).exists() for name in _LEGACY_FILES):
        return True
    return any((root / name).exists() for name in _LEGACY_DIRS)


def discover_sectors(cfg: Config) -> list[int]:
    """Sectors that still have processed output, including the old sector-107 root."""
    sectors = set(_sector_dirs(cfg)) | _sectors_from_logs(cfg)
    if _legacy_root_present(cfg):
        sectors.add(107)
    return sorted(sectors)


def _sector_log_names(sector: int) -> tuple[str, ...]:
    tag = f"s{int(sector):04d}"
    return (
        f"run_{tag}.log",
        f"download_{tag}.log",
        f"run_{tag}.pid",
        f"batch_{tag}.tics",
    )


def plan(cfg: Config, sectors: list[int] | None) -> list[tuple[Path, Path]]:
    """``(source, archive-relative)`` pairs. Archive-relative is under the stamp directory."""
    chosen = discover_sectors(cfg) if sectors is None else [int(s) for s in sectors]
    chosen_set = set(chosen)
    pairs: list[tuple[Path, Path]] = []
    seen: set[Path] = set()

    def add(src: Path, rel: Path) -> None:
        src = Path(src)
        if not src.exists() or src in seen:
            return
        if src.name in _CATALOGS:
            return
        if _contained(src, cfg.data_dir):
            raise RuntimeError(f"refusing to reset a path inside the data directory: {src}")
        seen.add(src)
        pairs.append((src, rel))

    dirs = _sector_dirs(cfg)
    for sector in chosen:
        folder = dirs.get(sector)
        if folder is not None:
            add(folder, Path("results") / folder.name)
        for name in _sector_log_names(sector):
            add(cfg.logs_dir / name, Path("logs") / name)

    # Sector 107 used to write straight into results/ and logs/full_run.log.
    if sectors is None or 107 in chosen_set:
        for name in _LEGACY_FILES:
            add(cfg.results_root / name, Path("results") / "sector-107-root" / name)
        for name in _LEGACY_DIRS:
            add(cfg.results_root / name, Path("results") / "sector-107-root" / name)
        add(cfg.logs_dir / "full_run.log", Path("logs") / "full_run.log")
        add(cfg.logs_dir / "full_run.pid", Path("logs") / "full_run.pid")

    if sectors is None:
        for name in _SHARED_LOGS:
            add(cfg.logs_dir / name, Path("logs") / name)
    return pairs


def _stamp_dir(cfg: Config) -> Path:
    base = cfg.archive_dir / time.strftime("%Y%m%d-%H%M%S")
    dest = base
    n = 2
    while dest.exists():
        dest = Path(f"{base}-{n}")
        n += 1
    return dest


def _relocate(src: Path, dest: Path, delete: bool) -> None:
    if delete:
        if src.is_dir():
            shutil.rmtree(src)
        else:
            src.unlink()
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))


def reset_results(cfg: Config, sectors: list[int] | None = None, *, delete: bool = False) -> int:
    """Archive (default) or delete processed results. Returns 0. Never touches downloads."""
    if _contained(cfg.archive_dir, cfg.data_dir):
        raise RuntimeError(f"archive_dir is inside the data directory: {cfg.archive_dir}")
    pairs = plan(cfg, sectors)
    if not pairs:
        print("nothing to reset")
        return 0
    stamp = None if delete else _stamp_dir(cfg)
    for src, rel in pairs:
        dest = (stamp / rel) if stamp is not None else src
        _relocate(src, dest, delete)
        action = "deleted" if delete else "archived"
        print(f"{action} {src}")
    if stamp is not None:
        print(f"archive: {stamp}")
    print("downloaded light curves and .download.done markers were left in place")
    return 0
