"""Per-star detrend + Box Least Squares. Appends ``bls_raw.csv`` and skips stars already in it.

The numeric path (flatten window, binning, period grid, durations, SDE/SNR/odd-even/
secondary) matches the historical ``src/search.py`` when :class:`SearchConfig` is
left at its defaults.
"""
from __future__ import annotations

import csv
import threading
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


_LIVE_KEYS = ("ra", "dec", "tmag", "_pgram_p", "_pgram_w", "_aperture", "_centroid")


def _meta_float(meta, *names):
    for name in names:
        try:
            value = float(meta.get(name))
        except (TypeError, ValueError):
            continue
        if np.isfinite(value):
            return float(value)
    return None


def _attach_coords(row: dict, meta) -> None:
    """Sky position already in the FITS header. Stripped before the CSV is written."""
    row["ra"] = _meta_float(meta, "RA_OBJ", "RA")
    row["dec"] = _meta_float(meta, "DEC_OBJ", "DEC")
    row["tmag"] = _meta_float(meta, "TESSMAG", "TMAG")


def _attach_pgram(row: dict, periods, power) -> None:
    """Downsample the periodogram the search just computed. Not a second BLS."""
    n = int(len(periods))
    if n == 0:
        return
    step = max(1, n // 160)
    idx = list(range(0, n, step))
    imax = int(np.argmax(power))
    if imax not in idx:
        idx.append(imax)
        idx.sort()
    row["_pgram_p"] = [round(float(periods[i]), 5) for i in idx]
    row["_pgram_w"] = [round(float(power[i]), 3) for i in idx]


def read_aperture(path) -> list | None:
    """Pipeline aperture (bit 2) from a SPOC light-curve file. None when it is missing."""
    try:
        from astropy.io import fits
        with fits.open(path) as hdul:
            data = hdul["APERTURE"].data if "APERTURE" in hdul else None
        if data is None:
            return None
        mask = [[1 if int(v) & 2 else 0 for v in row] for row in np.asarray(data).tolist()]
    except Exception:
        return None
    if not mask or not any(bit for row in mask for bit in row):
        return None
    return mask


def _attach_aperture(row: dict, path) -> None:
    """Pipeline aperture (bit 2) from the SPOC light-curve file, for the dashboard."""
    mask = read_aperture(path)
    if mask:
        row["_aperture"] = mask


def read_mom_centroid(path):
    """MOM_CENTR1/MOM_CENTR2 from a SPOC light curve. None when the columns are absent."""
    try:
        from astropy.io import fits
        with fits.open(path, memmap=False) as hdul:
            chosen = None
            for hdu in hdul:
                names = getattr(getattr(hdu, "columns", None), "names", None) or []
                lookup = {str(name).upper(): name for name in names}
                if "TIME" in lookup:
                    chosen = (hdu, lookup)
                    break
            if chosen is None:
                return None
            hdu, lookup = chosen
            if "MOM_CENTR1" not in lookup or "MOM_CENTR2" not in lookup:
                return None
            data = hdu.data
            return (
                np.asarray(data[lookup["TIME"]], dtype=float),
                np.asarray(data[lookup["MOM_CENTR1"]], dtype=float),
                np.asarray(data[lookup["MOM_CENTR2"]], dtype=float),
            )
    except Exception:
        return None


def _attach_centroid(row: dict, path) -> None:
    """Binned centroid shift. A missing column leaves the row without a centroid."""
    if row.get("error") or row.get("period") is None:
        return
    series = read_mom_centroid(path)
    if not series:
        return
    try:
        from rhexas.worker.records import centroid_vs_phase
        packed = centroid_vs_phase(series[0], series[1], series[2], row.get("period"), row.get("t0"), row.get("duration"))
    except Exception:
        return
    if packed:
        row["_centroid"] = packed


def _strip_live_keys(row: dict) -> None:
    for key in _LIVE_KEYS:
        row.pop(key, None)


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
    _attach_pgram(row, res.period, res.power)
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
        _attach_coords(row, lc.meta)
        _attach_aperture(row, path)
        _publish_live(row, t, y, processing=True)
        row.update(bls_metrics(t, y, dy, s))
        _attach_centroid(row, path)
    except Exception as e:
        row["error"] = str(e)[:200]
        _publish_live(row, t, y, processing=False)
    else:
        _publish_live(row, t, y, processing=False)
    _strip_live_keys(row)
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


def _open_pool(procs: int, cfg: Config, live):
    """One pool. Spawn when a dashboard payload is installed, so sqlite is not forked."""
    procs = max(1, int(procs))
    if live is None:
        return Pool(procs, initializer=_pool_init, initargs=(cfg.search,))
    return get_context("spawn").Pool(procs, initializer=_pool_init, initargs=(cfg.search, live))


def _close_pool(pool) -> None:
    if pool is None:
        return
    try:
        pool.close()
        pool.join()
    except Exception:
        try:
            pool.terminate()
            pool.join()
        except Exception:
            pass


def _kill_pool(pool) -> None:
    """Stop workers that are inside a BLS call. close() would wait for that call."""
    if pool is None:
        return
    try:
        pool.terminate()
    except Exception:
        pass
    try:
        pool.join()
    except Exception:
        pass


def _directive(pace) -> dict:
    if pace is None:
        return {}
    try:
        directive = pace() or {}
    except Exception:
        return {}
    return directive if isinstance(directive, dict) else {}


def _mark_holding(directive: dict, holding: bool) -> None:
    fn = directive.get("mark_holding")
    if not callable(fn):
        return
    try:
        fn(bool(holding))
    except Exception:
        pass


def _still_paused(directive: dict) -> bool:
    fn = directive.get("still_paused")
    if callable(fn):
        try:
            return bool(fn())
        except Exception:
            return False
    return bool(directive.get("paused"))


def _next_result(iterator, pool, directive: dict):
    """One row from the pool. ('paused', None) means the pool was stopped."""
    box: dict = {}
    ready = threading.Event()

    def pull() -> None:
        try:
            box["row"] = next(iterator)
        except StopIteration:
            box["end"] = True
        except Exception as exc:
            box["err"] = exc
        finally:
            ready.set()

    threading.Thread(target=pull, name="rhexas-bls-wait", daemon=True).start()
    while not ready.wait(0.05):
        if _still_paused(directive):
            _kill_pool(pool)
            return "paused", None
    if box.get("end"):
        return "end", None
    if box.get("err"):
        if _still_paused(directive):
            _kill_pool(pool)
            return "paused", None
        raise box["err"]
    return "row", box.get("row")


def _apply_pool_priority(priority, current):
    if not priority or priority == current:
        return current
    try:
        from rhexas.worker.priority import apply_priority
        apply_priority(str(priority))
    except Exception:
        return current
    return str(priority)


def _write_star(handle, writer, row, on_row, done: int, total: int, started: float) -> int:
    writer.writerow(row)
    done += 1
    if on_row is not None:
        try:
            on_row(done, total, row, time.time() - started)
        except Exception:
            pass
    if done % 100 == 0 or done == total:
        handle.flush()
        elapsed = time.time() - started
        rate = done / elapsed if elapsed else 0.0
        left = (total - done) / rate / 60.0 if rate else 0.0
        print(
            f"{time.strftime('%H:%M:%S')} {done}/{total} stars  {rate:.2f}/s  ETA {left:.0f} min",
            flush=True,
        )
    return done


def run(cfg: Config, *, procs: int | None = None, limit: int = 0, out: str = "bls_raw.csv",
        tics: set[int] | None = None, live: dict | None = None, on_row=None, pace=None) -> int:
    warnings.filterwarnings("ignore")
    procs = cfg.search.procs if procs is None else procs
    cfg.sector_results.mkdir(parents=True, exist_ok=True)
    dest = Path(out) if Path(out).is_absolute() else cfg.sector_results / out
    done_names = set()
    if dest.exists():
        with open(dest, newline="") as handle:
            done_names = {row["file"] for row in csv.DictReader(handle)}
    files = list_lightcurves(cfg, tics=tics, limit=limit)
    todo = [path for path in files if Path(path).name not in done_names]
    print(f"{len(files)} files, {len(done_names)} done, {len(todo)} to do", flush=True)
    new = not dest.exists()
    if not todo:
        if new:
            with open(dest, "w", newline="") as handle:
                csv.DictWriter(handle, FIELDS).writeheader()
        print("search finished", flush=True)
        return 0
    started = time.time()
    total = len(todo)
    # A normal search keeps one pool for the whole batch. A worker pace callback
    # is checked between waves, and a new pool is opened only when the load changed.
    if pace is None:
        pool = _open_pool(procs, cfg, live)
        with open(dest, "a", newline="") as handle, pool:
            writer = csv.DictWriter(handle, FIELDS)
            if new:
                writer.writeheader()
            finished = 0
            for row in pool.imap_unordered(analyze, todo, chunksize=4):
                finished = _write_star(handle, writer, row, on_row, finished, total, started)
        print("search finished", flush=True)
        return 0
    pending = list(todo)
    pool = None
    pool_procs = None
    pool_priority = None
    finished = 0
    try:
        with open(dest, "a", newline="") as handle:
            writer = csv.DictWriter(handle, FIELDS)
            if new:
                writer.writeheader()
            while pending:
                directive = _directive(pace)
                if directive.get("paused") or _still_paused(directive):
                    if pool is not None:
                        _close_pool(pool)
                        pool = None
                        pool_procs = None
                    _mark_holding(directive, True)
                    time.sleep(0.4)
                    continue
                _mark_holding(directive, False)
                try:
                    want = max(1, int(directive.get("procs") or procs))
                except (TypeError, ValueError):
                    want = max(1, int(procs))
                priority = directive.get("priority")
                if pool is None or want != pool_procs or (priority and priority != pool_priority):
                    _close_pool(pool)
                    pool_priority = _apply_pool_priority(priority, pool_priority)
                    pool = _open_pool(want, cfg, live)
                    pool_procs = want
                wave = pending[:pool_procs]
                pending = pending[pool_procs:]
                iterator = pool.imap_unordered(analyze, wave, chunksize=1)
                written: set[str] = set()
                aborted = False
                while True:
                    kind, row = _next_result(iterator, pool, directive)
                    if kind == "paused":
                        aborted = True
                        _kill_pool(pool)
                        pool = None
                        pool_procs = None
                        _mark_holding(directive, True)
                        break
                    if kind == "end":
                        break
                    if isinstance(row, dict):
                        finished = _write_star(handle, writer, row, on_row, finished, total, started)
                        if row.get("file"):
                            written.add(str(row["file"]))
                if aborted:
                    leftover = [path for path in wave if Path(path).name not in written]
                    pending = leftover + pending
                    time.sleep(0.4)
    finally:
        _close_pool(pool)
    print("search finished", flush=True)
    return 0
