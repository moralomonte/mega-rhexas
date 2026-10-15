"""Per-star detrend + Box Least Squares. Appends ``bls_raw.csv`` and skips stars already in it.

The numeric path (flatten window, binning, period grid, durations, SDE/SNR/odd-even/
secondary) matches the historical ``src/search.py`` when :class:`SearchConfig` is
left at its defaults.
"""
from __future__ import annotations

import csv
import time
import warnings
from glob import glob
from multiprocessing import Pool, get_context
from pathlib import Path

import numpy as np

from .config import Config, SearchConfig
from .tics import tic_from_filename

FIELDS = ["tic", "file", "period", "t0", "duration", "depth", "depth_err", "snr", "sde", "n_transits",
          "depth_odd", "depth_even", "odd_even_sigma", "depth_secondary", "sec_sigma", "npts", "error"]

# Workers inherit this via the pool initializer (spawn-safe on Windows).
_ACTIVE: SearchConfig | None = None
# Optional local-dashboard payload. None for a normal ``rhexas search`` (no extra I/O).
_LIVE: dict | None = None


def _pool_init(search: SearchConfig, live: dict | None = None) -> None:
    global _ACTIVE, _LIVE
    _ACTIVE = search
    _LIVE = live


def _installed_live():
    """Dashboard payload installed in this process by the pool initializer."""
    return _LIVE


def _publish_live(row: dict, t, y, *, processing: bool) -> None:
    """Tell a worker dashboard which star this process is on. Never fails the search."""
    if not _LIVE:
        return
    try:
        from rhexas.worker.live import publish_star
        publish_star(_LIVE, row, t, y, processing=processing)
    except Exception:
        return


def bls_metrics(t, y, dy, search: SearchConfig | None = None) -> dict:
    """BLS on an already-cleaned, binned light curve. Raises if there are too few points."""
    from astropy.timeseries import BoxLeastSquares
    s = search or _ACTIVE or SearchConfig()
    if len(t) < s.min_points:
        raise ValueError("too few points")
    row = {"npts": len(t)}
    bls = BoxLeastSquares(t, y, dy)
    durs = np.array(s.durations_days)
    base = float(np.max(t) - np.min(t))
    periods = np.exp(np.linspace(np.log(s.period_min_days), np.log(min(s.period_max_days, base / 1.5)), s.n_periods))
    res = bls.power(periods, durs, objective="snr")
    i = int(np.argmax(res.power))
    p = res.power
    row["sde"] = float((p[i] - p.mean()) / p.std())
    P, T0, D = res.period[i], res.transit_time[i], res.duration[i]
    st = bls.compute_stats(P, D, T0)
    row.update(
        period=P, t0=T0, duration=D, depth=st["depth"][0], depth_err=st["depth"][1],
        snr=st["depth"][0] / st["depth"][1] if st["depth"][1] > 0 else 0,
        n_transits=int(np.sum(st["per_transit_count"] > 0)),
        depth_odd=st["depth_odd"][0], depth_even=st["depth_even"][0],
        odd_even_sigma=abs(st["depth_odd"][0] - st["depth_even"][0]) / np.hypot(st["depth_odd"][1], st["depth_even"][1]),
        depth_secondary=st["depth_phased"][0],
        sec_sigma=st["depth_phased"][0] / st["depth_phased"][1] if st["depth_phased"][1] > 0 else 0,
    )
    return row


def analyze(path):
    """Read one SPOC ``lc.fits``, detrend, bin, and run BLS. One row dict (same columns as before)."""
    import lightkurve as lk
    s = _ACTIVE or SearchConfig()
    row = {"file": Path(path).name}
    t = y = None
    try:
        lc = lk.read(path, flux_column="pdcsap_flux", quality_bitmask="default")
        row["tic"] = int(lc.meta.get("TICID") or lc.meta.get("OBJECT", "0").split()[-1])
        lc = lc.remove_nans().normalize()
        lc = lc.flatten(window_length=s.flatten_window).remove_outliers(
            sigma_upper=s.outlier_sigma_upper, sigma_lower=s.outlier_sigma_lower)
        lc = lc.bin(time_bin_size=s.bin_minutes / 1440).remove_nans()
        t = lc.time.value
        y = lc.flux.value
        dy = lc.flux_err.value
        _publish_live(row, t, y, processing=True)
        row.update(bls_metrics(t, y, dy, s))
    except Exception as e:
        row["error"] = str(e)[:200]
        _publish_live(row, t, y, processing=False)
        return row
    _publish_live(row, t, y, processing=False)
    return row


def list_lightcurves(cfg: Config, *, tics: set[int] | None, limit: int) -> list[str]:
    files = sorted(glob(str(cfg.sector_data / "*lc.fits")))
    if tics is not None:
        files = [f for f in files if tic_from_filename(f) in tics]
        have = {tic_from_filename(f) for f in files}
        missing = tics - have
        if missing:
            print(f"sector {cfg.sector}: {len(missing)} TIC IDs have no lc.fits in {cfg.sector_data}", flush=True)
    if limit:
        files = files[:limit]
    return files


def run(cfg: Config, *, procs: int | None = None, limit: int = 0, out: str = "bls_raw.csv",
        tics: set[int] | None = None, live: dict | None = None, on_row=None) -> int:
    warnings.filterwarnings("ignore")
    procs = cfg.search.procs if procs is None else procs
    cfg.sector_results.mkdir(parents=True, exist_ok=True)
    dest = Path(out) if Path(out).is_absolute() else cfg.sector_results / out
    done = set()
    if dest.exists():
        with open(dest, newline="") as f:
            done = {r["file"] for r in csv.DictReader(f)}
    files = list_lightcurves(cfg, tics=tics, limit=limit)
    todo = [f for f in files if Path(f).name not in done]
    print(f"{len(files)} files, {len(done)} done, {len(todo)} to do", flush=True)
    new = not dest.exists()
    if not todo:
        if new:
            with open(dest, "w", newline="") as f:
                csv.DictWriter(f, FIELDS).writeheader()
        print("search finished", flush=True)
        return 0
    t0 = time.time()
    # A normal search keeps the platform default (fork on Linux). The worker
    # dashboard path uses spawn so this process's open log and sqlite handles
    # are not inherited; forking with a sqlite connection open can corrupt it.
    if live is None:
        pool = Pool(procs, initializer=_pool_init, initargs=(cfg.search,))
    else:
        pool = get_context("spawn").Pool(procs, initializer=_pool_init, initargs=(cfg.search, live))
    with open(dest, "a", newline="") as f, pool:
        w = csv.DictWriter(f, FIELDS)
        if new:
            w.writeheader()
        for k, row in enumerate(pool.imap_unordered(analyze, todo, chunksize=4), 1):
            w.writerow(row)
            if on_row is not None:
                try:
                    on_row(k, len(todo), row, time.time() - t0)
                except Exception:
                    pass
            if k % 100 == 0 or k == len(todo):
                f.flush()
                el = time.time() - t0
                print(f"{time.strftime('%H:%M:%S')} {k}/{len(todo)} stars  {k/el:.2f}/s  ETA {(len(todo)-k)/(k/el)/60:.0f} min", flush=True)
    print("search finished", flush=True)
    return 0
