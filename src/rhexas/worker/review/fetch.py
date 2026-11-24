"""Fetch one missing sector light curve, or a target-pixel file for the centroid check.

The light curve uses the same MAST curl script as the pipeline downloader.
A target-pixel file is only fetched when the caller asks, and only when it
fits the same size gate. A skip or a failure leaves the check unavailable.
"""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from rhexas.config import default_config

from ..vetting.evaluate import find_lightcurve
from .download_lock import lightcurve_lock
from .gate import (
    estimate_fits_bytes,
    estimate_tpf_bytes,
    fits_gate,
    release_processed_bytes,
)


def ensure_lightcurve(cache: Path, tic: int, sector: int, *, getter=None, sleeper=None) -> dict:
    """Return the on-disk SPOC light curve, downloading it when it is missing.

    ``downloaded`` is true only when this call wrote the file. ``skipped`` is
    true when the size gate refused the download. The path is None when there
    is still no file.
    """
    cache = Path(cache)
    existing = find_lightcurve(cache, tic, sector)
    if existing is not None:
        return {
            "path": existing,
            "downloaded": False,
            "skipped": False,
            "note": "light curve already on disk",
            "bytes": _size(existing),
        }
    estimate = int(estimate_fits_bytes(sector))
    lock = lightcurve_lock(tic, sector)
    with lock:
        existing = find_lightcurve(cache, tic, sector)
        if existing is not None:
            return {
                "path": existing,
                "downloaded": False,
                "skipped": False,
                "note": "light curve already on disk",
                "bytes": _size(existing),
            }
        allowed, why = fits_gate(cache, estimate, reserve=True)
        if not allowed:
            return {"path": None, "downloaded": False, "skipped": True, "note": why, "bytes": 0}
        from rhexas.download import run

        cfg = replace(
            default_config(cache),
            sector=int(sector),
            data_dir=(cache / "data").resolve(),
            min_free_gb=0,
            max_project_gb=10_000,
        )
        code = 1
        try:
            code = run(
                cfg,
                threads=1,
                tics={int(tic)},
                getter=getter,
                sleeper=sleeper or time.sleep,
            )
        except Exception as exc:
            return {
                "path": None,
                "downloaded": False,
                "skipped": False,
                "note": f"light curve download did not finish ({str(exc)[:140]})",
                "bytes": 0,
            }
        finally:
            release_processed_bytes(cache, estimate)
        path = find_lightcurve(cache, tic, sector)
        if path is not None:
            return {
                "path": path,
                "downloaded": True,
                "skipped": False,
                "note": "downloaded the SPOC 2-minute light curve",
                "bytes": _size(path) or estimate,
            }
        if code == 2:
            note = "download stopped before the light curve was saved"
        else:
            note = "this TIC is not in the MAST sector light-curve list"
        return {"path": None, "downloaded": False, "skipped": False, "note": note, "bytes": 0}


def ensure_tpf(cache: Path, tic: int, sector: int, *, downloader=None) -> dict:
    """Download one SPOC target-pixel file when the centroid check needs it."""
    cache = Path(cache)
    estimate = int(estimate_tpf_bytes(sector))
    allowed, why = fits_gate(cache, estimate, reserve=True)
    if not allowed:
        return {"path": None, "downloaded": False, "skipped": True, "note": why, "bytes": 0}
    downloader = downloader or _lightkurve_tpf
    try:
        try:
            path = downloader(cache, int(tic), int(sector))
        except Exception as exc:
            return {
                "path": None,
                "downloaded": False,
                "skipped": False,
                "note": f"target pixel file was not downloaded ({str(exc)[:140]})",
                "bytes": 0,
            }
    finally:
        release_processed_bytes(cache, estimate)
    if not path:
        return {
            "path": None,
            "downloaded": False,
            "skipped": False,
            "note": "no SPOC target pixel file for this TIC",
            "bytes": 0,
        }
    saved = Path(path)
    return {
        "path": saved,
        "downloaded": True,
        "skipped": False,
        "note": "downloaded the target pixel file for the centroid check",
        "bytes": _size(saved) or estimate,
    }


def _lightkurve_tpf(cache: Path, tic: int, sector: int):
    import lightkurve as lk

    out = Path(cache) / "data" / "tpf" / f"s{int(sector):04d}"
    out.mkdir(parents=True, exist_ok=True)
    found = lk.search_targetpixelfile(
        f"TIC {int(tic)}", sector=int(sector), author="SPOC", exptime=120,
    )
    if len(found) == 0:
        return None
    product = found[0].download(download_dir=str(out))
    path = getattr(product, "path", None)
    if path:
        return Path(path)
    matches = sorted(out.glob("*tp.fits"))
    return matches[-1] if matches else None


def _size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0
