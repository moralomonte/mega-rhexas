"""Worker light-curve cache: delete a finished batch, then stay under the cap."""
from __future__ import annotations

from pathlib import Path

from rhexas.tics import tic_from_filename


def directory_bytes(root: Path) -> int:
    root = Path(root)
    if not root.exists():
        return 0
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def enforce_cache_cap(root: Path, max_bytes: int) -> list[Path]:
    """Delete oldest files under ``root`` until the tree fits in ``max_bytes``."""
    root = Path(root)
    if max_bytes < 0 or not root.exists():
        return []
    files = [path for path in root.rglob("*") if path.is_file()]
    files.sort(key=lambda path: (path.stat().st_mtime, path.name))
    total = sum(path.stat().st_size for path in files)
    deleted: list[Path] = []
    for path in files:
        if total <= max_bytes:
            break
        size = path.stat().st_size
        path.unlink()
        total -= size
        deleted.append(path)
    return deleted


def delete_lightcurves(sector_data: Path, tics: set[int]) -> int:
    """Remove FITS (and ``.part``) files for these TIC IDs. Leave locks and catalogs alone."""
    sector_data = Path(sector_data)
    if not sector_data.exists():
        return 0
    removed = 0
    for path in sector_data.glob("*"):
        if not path.is_file():
            continue
        tic = tic_from_filename(path.name)
        if tic is None or tic not in tics:
            continue
        if path.suffix in {".fits", ".part"} or path.name.endswith(".fits"):
            path.unlink()
            removed += 1
    return removed
