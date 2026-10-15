"""Threshold BLS hits, cross-match TOI + confirmed planets, rank, plot the top new signals.

Defaults match filter.py v2: SDE >= 6, SNR >= 7, >= 3 transits, depth <= 5%,
odd/even <= 3 sigma, |secondary| <= 3 sigma, duration/period <= 0.12, depth > 3%
flagged "likely EB", CROWDSAP < 0.8 flagged. Every sector writes
``results/sNNNN/candidates.csv``.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .config import Config, FilterConfig

TOI_URL = "https://exofop.ipac.caltech.edu/tess/download_toi.php?sort=toi&output=csv"
CP_URL = ("https://exoplanetarchive.ipac.caltech.edu/TAP/sync?query=select+distinct+tic_id,pl_name,pl_orbper"
          "+from+pscomppars+where+tic_id+is+not+null&format=csv")


def load_catalogs(cfg: Config, *, fetch: bool = True, getter=None):
    """Read (and, if missing, download) the ExoFOP TOI list and NASA confirmed-planet table."""
    import requests
    getter = getter or requests.get
    cfg.catalogs_dir.mkdir(parents=True, exist_ok=True)
    toi_f, cp_f = cfg.catalogs_dir / "toi_catalog.csv", cfg.catalogs_dir / "confirmed_planets.csv"
    if not toi_f.exists():
        if not fetch:
            raise FileNotFoundError(toi_f)
        toi_f.write_bytes(getter(TOI_URL, timeout=120).content)
    if not cp_f.exists():
        if not fetch:
            raise FileNotFoundError(cp_f)
        cp_f.write_bytes(getter(CP_URL, timeout=120).content)
    toi = pd.read_csv(toi_f, comment="#")
    cp = pd.read_csv(cp_f)
    cp["tic"] = cp["tic_id"].astype(str).str.replace("TIC ", "", regex=False).astype(int)
    return toi, cp


def apply_thresholds(d: pd.DataFrame, filt: FilterConfig | None = None) -> pd.DataFrame:
    """Add the boolean ``pass`` column. ``d`` must already have dropped error rows."""
    f = filt or FilterConfig()
    out = d.copy()
    out["pass"] = (
        (out.sde >= f.sde_min) & (out.snr >= f.snr_min) & (out.n_transits >= f.min_transits) & (out.depth > 0)
        & (out.depth <= f.max_depth) & (out.odd_even_sigma <= f.odd_even_max_sigma)
        & (out.sec_sigma.abs() <= f.secondary_max_sigma)
        & (out.duration / out.period <= f.max_duration_fraction)
    )
    return out


def annotate_known(d: pd.DataFrame, toi: pd.DataFrame, cp: pd.DataFrame) -> pd.DataFrame:
    """Join TOI / confirmed-planet catalogs and set status + score. Same columns as filter.py v2."""
    toi_by_tic = toi.groupby("TIC ID").agg(
        toi=("TOI", lambda s: ";".join(map(str, s))),
        toi_period=("Period (days)", "first"),
        tfopwg=("TFOPWG Disposition", "first"),
    )
    out = d.join(toi_by_tic, on="tic")
    confirmed = cp.groupby("tic").pl_name.agg(";".join).rename("confirmed_planet")
    out = out.merge(confirmed, left_on="tic", right_index=True, how="left")
    out["status"] = np.where(out.confirmed_planet.notna(), "known_confirmed", np.where(out.toi.notna(), "known_TOI", "NEW"))
    out["score"] = out.snr * np.minimum(out.sde, 30) / 10
    return out


def flag_row(row, filt: FilterConfig) -> str:
    eb = ("likely EB (depth>%.0f%%)" % (filt.eb_depth * 100)) if row.depth > filt.eb_depth else ""
    crowd = ("contaminated aperture (CROWDSAP %.2f)" % row.crowdsap) if row.crowdsap < filt.crowdsap_min else ""
    return "; ".join(x for x in (eb, crowd) if x)


def plot_candidate(row, path, cfg: Config):
    import lightkurve as lk
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from astropy.timeseries import BoxLeastSquares
    f = cfg.filt
    lc = lk.read(cfg.sector_data / row.file, flux_column="pdcsap_flux").remove_nans().normalize()
    fl = lc.flatten(window_length=cfg.search.flatten_window).remove_outliers(
        sigma_upper=cfg.search.outlier_sigma_upper, sigma_lower=cfg.search.outlier_sigma_lower)
    b = fl.bin(time_bin_size=cfg.search.bin_minutes / 1440).remove_nans()
    per = np.exp(np.linspace(np.log(f.plot_period_min_days), np.log(f.plot_period_max_days), f.plot_n_periods))
    r = BoxLeastSquares(b.time.value, b.flux.value, b.flux_err.value).power(
        per, list(f.plot_durations_days), objective="snr")
    fig, ax = plt.subplots(3, 1, figsize=(10, 10))
    ax[0].plot(lc.time.value, lc.flux.value, "k.", ms=1)
    ax[0].set_title(f"TIC {row.tic}  raw PDCSAP")
    ax[1].plot(r.period, r.power, "k-", lw=.5)
    ax[1].axvline(row.period, c="r", alpha=.4)
    ax[1].set_xscale("log")
    ax[1].set_xlabel("period [d]")
    folded = fl.fold(period=row.period, epoch_time=row.t0)
    ax[2].plot(folded.time.value * 24, folded.flux.value, "k.", ms=1, alpha=.3)
    fb = folded.bin(time_bin_size=row.duration / 6)
    ax[2].plot(fb.time.value * 24, fb.flux.value, "r.")
    ax[2].set_xlim(-3 * row.duration * 24, 3 * row.duration * 24)
    ax[2].set_xlabel("hours from mid-transit")
    ax[2].set_title(f"P={row.period:.4f} d  depth={row.depth*1e6:.0f} ppm  SNR={row.snr:.1f}  SDE={row.sde:.1f}")
    fig.tight_layout()
    fig.savefig(path, dpi=90)
    plt.close(fig)


def run(cfg: Config, *, raw: str = "bls_raw.csv", out: str = "candidates.csv", plots: int | None = None,
        tics: set[int] | None = None, fetch_catalogs: bool = True, getter=None) -> int:
    filt = cfg.filt
    n_plots = filt.n_plots if plots is None else plots
    raw_path = Path(raw) if Path(raw).is_absolute() else cfg.sector_results / raw
    out_path = Path(out) if Path(out).is_absolute() else cfg.sector_results / out
    d = pd.read_csv(raw_path)
    if tics is not None:
        d = d[d.tic.isin(list(tics))]
    d = d[d.error.isna()].copy()
    d = apply_thresholds(d, filt)
    toi, cp = load_catalogs(cfg, fetch=fetch_catalogs, getter=getter)
    d = annotate_known(d, toi, cp)
    c = d[d["pass"]].sort_values("score", ascending=False).copy()
    from astropy.io import fits

    def crowd(fn):
        try:
            return fits.getheader(cfg.sector_data / fn, 1).get("CROWDSAP")
        except Exception:
            return np.nan

    c["crowdsap"] = c.file.map(crowd)
    c["flags"] = c.apply(lambda r: flag_row(r, filt), axis=1)
    vf = cfg.sector_results / "vetting" / "vetting.csv"
    if vf.exists():
        v = pd.read_csv(vf)[["tic", "verdict", "flags"]].rename(columns={"flags": "vet_flags"})
        c = c.merge(v, on="tic", how="left")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    c.to_csv(out_path, index=False)
    print(f"stars searched={len(d)}  passed vetting={len(c)}  ->", c.status.value_counts().to_dict())
    k = d[d.status != "NEW"]
    rec = k[k["pass"]]
    print(f"known TOI/planet hosts in sample={len(k)}  recovered (passed)={len(rec)}")
    if len(rec):
        rp = rec.toi_period.fillna(0)
        ratio = rec.period / rp.replace(0, np.nan)
        n_match = int(np.isclose(ratio, 1, atol=.02).sum() + np.isclose(ratio, .5, atol=.01).sum() + np.isclose(ratio, 2, atol=.04).sum())
        print(f"  of those, BLS period matched catalog (incl 1/2x,2x) = {n_match}")
        print(rec[["tic", "toi", "confirmed_planet", "period", "toi_period", "depth", "snr", "sde"]].head(20).to_string())
    if n_plots:
        pdir = cfg.sector_results / "plots"
        pdir.mkdir(parents=True, exist_ok=True)
        for _, r in c[c.status == "NEW"].head(n_plots).iterrows():
            try:
                plot_candidate(r, pdir / f"TIC{r.tic}.png", cfg)
            except Exception as e:
                print("plot fail", r.tic, e)
    return 0
