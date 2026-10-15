"""Repeat the BLS check on every MAST light curve of one or more TICs and fit an ephemeris.

The per-sector math matches the historical script (free BLS, a narrow search around
the seed period, weighted linear ephemeris). The seed period comes from, in order:
``--period`` / ``--t0``, this sector's ``bls_raw.csv``, or the strongest free-BLS
peak if the star has not been searched here yet. There is no built-in TIC.
"""
from __future__ import annotations

import json
import warnings

import numpy as np
import pandas as pd

from .config import Config


def lookup_bls(cfg: Config, tic: int):
    path = cfg.sector_results / "bls_raw.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    tic_num = pd.to_numeric(df["tic"], errors="coerce")
    m = df[tic_num == tic]
    if m.empty:
        return None
    row = m.iloc[0]
    if pd.isna(row.get("period")) or pd.isna(row.get("t0")):
        return None
    return row


def fit_ephemeris(good: list[dict], t00: float, p0: float):
    """Weighted linear ephemeris. Returns None when every epoch falls in one ~50 d bin."""
    if not good:
        return None
    E, T, W = [], [], []
    for d in good:
        n = round((d["T0_fix"] - t00) / p0)
        E.append(n)
        T.append(d["T0_fix"])
        W.append(1 / max(d["dur_h"] / 24 / d["snr_fix"], 1e-4) ** 2)
    if len(set(np.round(np.array(T) / 50))) <= 1:
        return None
    A = np.vstack([np.ones(len(E)), E]).T
    w = np.sqrt(W)
    c, *_ = np.linalg.lstsq(A * w[:, None], np.array(T) * w, rcond=None)
    cov = np.linalg.inv((A * w[:, None]).T @ (A * w[:, None]))
    return dict(
        T0=float(c[0]), P=float(c[1]),
        T0_err=float(np.sqrt(cov[0, 0])), P_err=float(np.sqrt(cov[1, 1])),
        O_C_min=[float((t - (c[0] + c[1] * e)) * 1440) for e, t in zip(E, T)],
        n_cycles=int(max(E) - min(E)),
    )


def _prepare(lc, row):
    cad = float(row["exptime"]) / 86400
    win = int(round(1.0 / cad)) | 1
    flat = lc.flatten(window_length=max(win, 11)).remove_outliers(sigma_upper=4, sigma_lower=20)
    t = flat.time.value
    y = flat.flux.value
    if np.isfinite(flat.flux_err.value).all() and (flat.flux_err.value > 0).all():
        dy = flat.flux_err.value
    else:
        dy = np.full(len(t), np.nanstd(y))
    if len(t) < 100:
        raise ValueError("too few pts")
    return t, y, dy, cad


def _search_one(t, y, dy, cad, period, multi):
    from astropy.timeseries import BoxLeastSquares
    bls = BoxLeastSquares(t, y, dy)
    base = t.max() - t.min()
    durs = [d for d in multi.durations_days if d > cad * 1.5] or [cad * 2]
    per = np.exp(np.linspace(np.log(multi.period_min_days), np.log(min(multi.period_max_days, base / 2)), multi.n_periods))
    pw = bls.power(per, durs, objective="snr")
    k = int(np.argmax(pw.power))
    sde = (pw.power[k] - pw.power.mean()) / pw.power.std()
    pp = np.linspace(period * (1 - multi.fixed_period_frac), period * (1 + multi.fixed_period_frac), multi.fixed_period_steps)
    fx = bls.power(pp, durs, objective="snr")
    j = int(np.argmax(fx.power))
    st = bls.compute_stats(fx.period[j], fx.duration[j], fx.transit_time[j])
    rank = int((pw.power > np.max(fx.power)).sum())
    return dict(
        npts=len(t), tspan=[float(t.min()), float(t.max())],
        best_P=float(pw.period[k]), best_sde=float(sde),
        P_fix=float(fx.period[j]), T0_fix=float(fx.transit_time[j]),
        dur_h=float(fx.duration[j] * 24), depth_ppm=float(st["depth"][0] * 1e6),
        depth_err_ppm=float(st["depth"][1] * 1e6),
        snr_fix=float(st["depth"][0] / st["depth"][1]),
        odd=float(st["depth_odd"][0] * 1e6), even=float(st["depth_even"][0] * 1e6),
        periods_stronger_than_P0=rank, alias=float(pw.period[k] / period),
    ), (t, y, cad)


def run_star(cfg: Config, tic: int, *, period: float | None = None, t0: float | None = None) -> dict:
    import lightkurve as lk
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    warnings.filterwarnings("ignore")
    multi = cfg.multisector
    out = cfg.sector_results / "vetting"
    out.mkdir(parents=True, exist_ok=True)
    dl = cfg.multisector_download_dir
    dl.mkdir(parents=True, exist_ok=True)
    r0 = lookup_bls(cfg, tic)
    if period is None and r0 is not None:
        period = float(r0.period)
        t0 = float(r0.t0)
    depth_ref = float(r0.depth) if r0 is not None and pd.notna(r0.depth) else None
    sr = lk.search_lightcurve(f"TIC {tic}")
    prepared = []
    for i, row in enumerate(sr.table):
        lab = f"S{int(row['mission'].split()[-1]):02d} {row['author']} {int(row['exptime'])}s"
        try:
            lc = sr[i].download(download_dir=str(dl))
            lc = lc.remove_nans().normalize()
            if "quality" in lc.colnames:
                lc = lc[lc.quality == 0] if row["author"] == "SPOC" else lc
            prepared.append((lab, *_prepare(lc, row)))
        except Exception as e:
            print(lab, "FAIL", str(e)[:120])
    if period is None:
        if not prepared:
            raise RuntimeError(f"TIC {tic}: no light curves and no seed period")
        # Seed from the strongest free-BLS peak, then run the fixed-period search.
        from astropy.timeseries import BoxLeastSquares
        best = None
        for lab, t, y, dy, cad in prepared:
            base = float(t.max() - t.min())
            durs = [d for d in multi.durations_days if d > cad * 1.5] or [cad * 2]
            per = np.exp(np.linspace(
                np.log(multi.period_min_days),
                np.log(min(multi.period_max_days, base / 2)),
                multi.n_periods,
            ))
            pw = BoxLeastSquares(t, y, dy).power(per, durs, objective="snr")
            k = int(np.argmax(pw.power))
            sde = float((pw.power[k] - pw.power.mean()) / pw.power.std())
            if best is None or sde > best[0]:
                best = (sde, float(pw.period[k]), float(pw.transit_time[k]))
        period, t0 = best[1], best[2]
        print(f"TIC {tic}: no bls_raw seed; using free-BLS P={period:.6f} d", flush=True)
    res = []
    lcs = []
    for lab, t, y, dy, cad in prepared:
        try:
            d, series = _search_one(t, y, dy, cad, period, multi)
            d = {"lc": lab, **d}
            print(json.dumps({a: (round(b, 4) if isinstance(b, float) else b) for a, b in d.items()}), flush=True)
            res.append(d)
            lcs.append((lab, series[0], series[1], series[2]))
        except Exception as e:
            print(lab, "FAIL", str(e)[:120])
    t00 = float(t0 if t0 is not None else period)
    good = [d for d in res if d["snr_fix"] > multi.snr_min]
    eph = fit_ephemeris(good, t00, float(period))
    print("EPHEMERIS", json.dumps(eph))
    pf = eph["P"] if eph else float(period)
    tf = eph["T0"] if eph else t00
    n = len(lcs)
    if n:
        fig, ax = plt.subplots(int(np.ceil(n / 3)), 3, figsize=(15, 3.2 * np.ceil(n / 3)), squeeze=False)
        for a, (lab, t, y, cad) in zip(ax.flat, lcs):
            ph = ((t - tf + 0.5 * pf) % pf - 0.5 * pf) * 24
            a.plot(ph, y, ".", ms=1.5, alpha=.35, color="k")
            b = np.linspace(-8.4, 8.4, 60)
            idx = np.digitize(ph, b)
            yb = [np.nanmedian(y[idx == i]) if (idx == i).sum() > 2 else np.nan for i in range(1, len(b))]
            a.plot((b[1:] + b[:-1]) / 2, yb, "o-", color="crimson", ms=3)
            if depth_ref is not None:
                a.axhline(1 - depth_ref, color="orange", ls="--", lw=.8)
            d = next(x for x in res if x["lc"] == lab)
            a.set_title(f"{lab}: depth {d['depth_ppm']:.0f}±{d['depth_err_ppm']:.0f} ppm, SNR {d['snr_fix']:.1f}", fontsize=9)
            s = np.nanstd(y)
            a.set_ylim(1 - 0.06, 1 + max(0.03, 2 * s))
            a.set_xlim(-8.4, 8.4)
        for a in ax.flat[n:]:
            a.axis("off")
        title = f"TIC {tic} folded at P={pf:.6f} d"
        if depth_ref is not None:
            title += f" (dashed = S{cfg.sector} depth {depth_ref * 100:.1f}%)"
        fig.suptitle(title)
        fig.tight_layout()
        fig.savefig(out / f"TIC{tic}_multisector.png", dpi=80)
        plt.close(fig)
    payload = dict(tic=int(tic), sectors=res, ephemeris=eph)
    with open(out / f"TIC{tic}_multisector.json", "w") as fh:
        json.dump(payload, fh, indent=1)
    return payload


def run(cfg: Config, tics: list[int], *, period: float | None = None, t0: float | None = None) -> int:
    if not tics:
        raise SystemExit("rhexas multisector needs at least one TIC ID")
    # A single period/t0 applies only when one star was requested.
    if len(tics) > 1 and (period is not None or t0 is not None):
        raise SystemExit("--period/--t0 apply to a single TIC; omit them when passing several stars")
    for tic in tics:
        if len(tics) > 1:
            print(f"=== TIC {tic} ===", flush=True)
        run_star(cfg, int(tic), period=period, t0=t0)
    return 0
