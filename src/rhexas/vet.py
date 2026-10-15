"""Vet candidates: TPF difference-image centroid, MOM_CENTR, TIC neighbours, odd/even, catalogs.

Writes ``<sector results>/vetting/vetting.csv``, ``vetting.json``, and ``TIC*_vet.png``.
Pass an explicit TIC list, or vet every ``NEW`` row in ``candidates.csv``.
"""
from __future__ import annotations

import json
import warnings

import numpy as np
import pandas as pd

from .config import Config

PX = 21.0  # arcsec / TESS pixel


def vetting_dir(cfg: Config):
    out = cfg.sector_results / "vetting"
    out.mkdir(parents=True, exist_ok=True)
    cfg.tpf_dir.mkdir(parents=True, exist_ok=True)
    return out


def load_raw(cfg: Config) -> pd.DataFrame:
    return pd.read_csv(cfg.sector_results / "bls_raw.csv").set_index("tic")


def tic_neighbours(ra, dec, tic):
    from astroquery.mast import Catalogs
    t = Catalogs.query_region(f"{ra} {dec}", radius=60 / 3600, catalog="TIC").to_pandas()
    t["sep"] = t["dstArcSec"]
    me = t[t.ID.astype(str) == str(tic)]
    return (me.iloc[0] if len(me) else None), t[t.ID.astype(str) != str(tic)]


def exofop(tic):
    import requests
    try:
        j = requests.get(f"https://exofop.ipac.caltech.edu/tess/target.php?id={tic}&json", timeout=30).json()
        b = j.get("basic_info", {})
        tois = [t.get("toi") for t in j.get("tois", [])] if isinstance(j.get("tois"), list) else []
        ctois = [t.get("ctoi") for t in j.get("ctois", [])] if isinstance(j.get("ctois"), list) else []
        nn = len(j.get("notes", []) or []) if isinstance(j.get("notes"), list) else 0
        return dict(exofop_names=b.get("star_names", ""), exofop_tois=";".join(map(str, tois)),
                    exofop_ctois=";".join(map(str, ctois)),
                    exofop_planets=b.get("confirmed_planets", ""), exofop_notes=nn)
    except Exception as e:
        return dict(exofop_err=str(e)[:80])


def simbad(ra, dec):
    import requests
    q = ("SELECT TOP 5 main_id, otype, DISTANCE(POINT('ICRS',ra,dec),POINT('ICRS',%f,%f))*3600 AS d FROM basic "
         "WHERE CONTAINS(POINT('ICRS',ra,dec),CIRCLE('ICRS',%f,%f,0.0042))=1 ORDER BY d") % (ra, dec, ra, dec)
    try:
        r = requests.get("https://simbad.cds.unistra.fr/simbad/sim-tap/sync",
                         params=dict(request="doQuery", lang="adql", format="json", query=q), timeout=30).json()
        return "; ".join(f"{a} [{b}] {c:.0f}\"" for a, b, c in r["data"])
    except Exception as e:
        return f"err {e}"[:60]


def vsx(ra, dec):
    try:
        from astroquery.vizier import Vizier
        r = Vizier(columns=["Name", "Type", "Period", "_r"]).query_region(f"{ra} {dec}", radius="60s", catalog="B/vsx/vsx")
        if not r:
            return ""
        t = r[0].to_pandas()
        return "; ".join(f"{a} {b} P={c} ({d:.0f}\")" for a, b, c, d in t[["Name", "Type", "Period", "_r"]].values)
    except Exception as e:
        return f"err {e}"[:60]


def centroid(img):
    img = np.nan_to_num(img)
    img = np.clip(img, 0, None)
    y, x = np.indices(img.shape)
    s = img.sum()
    return (x * img).sum() / s, (y * img).sum() / s


def vet_star(cfg: Config, tic: int, raw: pd.DataFrame | None = None) -> dict:
    """Vet one TIC. ``raw`` is ``bls_raw.csv`` indexed by tic (loaded if omitted)."""
    warnings.filterwarnings("ignore")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from astropy.io import fits
    if raw is None:
        raw = load_raw(cfg)
    out = vetting_dir(cfg)
    r = raw.loc[tic]
    P, T0, D = r.period, r.t0, r.duration
    lcf = cfg.sector_data / r.file
    with fits.open(lcf) as h:
        H0, H1 = h[0].header, h[1].header
        d = h[1].data
        ra, dec, crowd = H0["RA_OBJ"], H0["DEC_OBJ"], H1.get("CROWDSAP")
        t = d["TIME"]
        c1, c2 = d["MOM_CENTR1"], d["MOM_CENTR2"]
        q = d["QUALITY"]
    ph = ((t - T0 + 0.5 * P) % P) - 0.5 * P
    intr = (np.abs(ph) < 0.4 * D) & (q == 0) & np.isfinite(c1)
    oot = (np.abs(ph) > 0.75 * D) & (np.abs(ph) < 2.5 * D) & (q == 0) & np.isfinite(c1)
    mom_shift = np.hypot(np.nanmedian(c1[intr]) - np.nanmedian(c1[oot]), np.nanmedian(c2[intr]) - np.nanmedian(c2[oot])) * PX
    mom_err = np.hypot(np.nanstd(c1[oot]), np.nanstd(c2[oot])) / np.sqrt(max(intr.sum(), 1)) * PX
    res = dict(tic=tic, period=P, t0=T0, dur_h=D * 24, depth_ppm=r.depth * 1e6, snr=r.snr, sde=r.sde, n_transits=r.n_transits,
               odd_even_sigma=r.odd_even_sigma, sec_sigma=r.sec_sigma, dur_frac=D / P, ra=ra, dec=dec, crowdsap=crowd,
               mom_shift_arcsec=mom_shift, mom_shift_sigma=mom_shift / mom_err if mom_err > 0 else np.nan)
    me, nb = tic_neighbours(ra, dec, tic)
    tm = float(me["Tmag"]) if me is not None else np.nan
    rs = float(me["rad"]) if me is not None and np.isfinite(me["rad"]) else np.nan
    res.update(tmag=tm, rstar=rs, teff=float(me["Teff"]) if me is not None else np.nan,
               lumclass=str(me.get("lumclass", "")) if me is not None else "")
    res["rp_earth"] = np.sqrt(r.depth) * rs * 109.1 if np.isfinite(rs) else np.nan
    res["rp_jup"] = res["rp_earth"] / 11.21 if np.isfinite(rs) else np.nan
    dm_max = -2.5 * np.log10(r.depth)
    cand = nb[(nb.Tmag - tm) < dm_max]
    res["n_nb_60"] = len(nb)
    res["n_nb_could_mimic"] = len(cand)
    res["brightest_nb"] = (f"TIC{int(nb.sort_values('Tmag').iloc[0].ID)} Tmag {nb.Tmag.min():.1f} at {nb.sort_values('Tmag').iloc[0].sep:.0f}\"" if len(nb) else "")
    import lightkurve as lk
    diff_off = np.nan
    fig, ax = plt.subplots(1, 3, figsize=(15, 4.8))
    try:
        sr = lk.search_targetpixelfile(f"TIC {tic}", sector=cfg.sector, author="SPOC", exptime=120)
        tpf = sr[0].download(download_dir=str(cfg.tpf_dir), quality_bitmask="default")
        tt = tpf.time.value
        fl = tpf.flux.value
        ph2 = ((tt - T0 + 0.5 * P) % P) - 0.5 * P
        a = np.abs(ph2) < 0.4 * D
        b = (np.abs(ph2) > 0.75 * D) & (np.abs(ph2) < 2.5 * D)
        oimg = np.nanmedian(fl[b], 0)
        iimg = np.nanmedian(fl[a], 0)
        dimg = oimg - iimg
        cxo, cyo = centroid(oimg)
        cxd, cyd = centroid(np.where(dimg > 0.3 * np.nanmax(dimg), dimg, 0))
        diff_off = np.hypot(cxd - cxo, cyd - cyo) * PX
        px, py = tpf.wcs.all_world2pix([[ra, dec]], 0)[0]
        diff_tgt = np.hypot(cxd - px, cyd - py) * PX
        res.update(diff_centroid_offset_arcsec=diff_off, diff_vs_target_arcsec=diff_tgt)
        ax[0].imshow(oimg, origin="lower", cmap="magma")
        ax[0].set_title("out-of-transit")
        ax[1].imshow(dimg, origin="lower", cmap="viridis")
        ax[1].set_title(f"difference (oot-in)  offset {diff_tgt:.0f}\" from target")
        for k in (0, 1):
            ax[k].plot(px, py, "c+", ms=16, mew=2)
            for _, n in nb[nb.Tmag < tm + 4].iterrows():
                nx, ny = tpf.wcs.all_world2pix([[n.ra, n.dec]], 0)[0]
                ax[k].plot(nx, ny, "wx" if k == 0 else "rx", ms=8)
            ax[k].plot(cxd, cyd, "o", mfc="none", mec="lime", ms=14, mew=2)
    except Exception as e:
        res["tpf_err"] = str(e)[:100]
    m = np.abs(ph) < 3 * D
    ax[2].plot(ph[m] * 24, (c1[m] - np.nanmedian(c1[oot])) * PX, ".", ms=2, alpha=.4, label="col")
    ax[2].plot(ph[m] * 24, (c2[m] - np.nanmedian(c2[oot])) * PX, ".", ms=2, alpha=.4, label="row")
    ax[2].axvspan(-D * 12, D * 12, color="y", alpha=.15)
    ax[2].set_ylim(-6, 6)
    ax[2].legend()
    ax[2].set_xlabel("hours from mid-transit")
    ax[2].set_ylabel("centroid shift [arcsec]")
    ax[2].set_title(f"MOM_CENTR shift {mom_shift:.2f}\" ({res['mom_shift_sigma']:.1f}σ)")
    fig.suptitle(f"TIC {tic}  P={P:.4f} d  depth={r.depth*1e6:.0f} ppm  Rp≈{res['rp_earth']:.1f} R⊕ (R*={rs:.2f})  Tmag {tm:.1f}  crowdsap={crowd}")
    fig.tight_layout()
    fig.savefig(out / f"TIC{tic}_vet.png", dpi=80)
    plt.close(fig)
    res.update(exofop(tic))
    res["simbad"] = simbad(ra, dec)
    res["vsx"] = vsx(ra, dec)
    why = []
    if r.depth > 0.03:
        why.append("depth>3% (EB-like)")
    if D / P > 0.15:
        why.append("duration/period>15%")
    if abs(r.sec_sigma) > 3:
        why.append("secondary/phase signal")
    if r.odd_even_sigma > 3:
        why.append("odd/even mismatch")
    if np.isfinite(diff_off) and res.get("diff_vs_target_arcsec", 0) > 15:
        why.append(f"difference-image offset {res['diff_vs_target_arcsec']:.0f}\"")
    if res["mom_shift_sigma"] > 4 and mom_shift > 1:
        why.append(f"centroid shift {res['mom_shift_sigma']:.0f}σ")
    if np.isfinite(res["rp_earth"]) and res["rp_earth"] > 25:
        why.append(f"Rp≈{res['rp_earth']:.0f} R⊕ too big for a planet")
    if any(f" {k} " in (res["vsx"] or "") for k in ("E", "EA", "EB", "EW", "ELL")):
        why.append("VSX eclipsing binary nearby/at target")
    if "EB*" in (res["simbad"] or ""):
        why.append("SIMBAD lists an eclipsing binary")
    if res.get("exofop_tois") or res.get("exofop_planets"):
        why.append("already a TOI/planet on ExoFOP")
    if res.get("exofop_ctois"):
        why.append("already a CTOI")
    res["flags"] = "; ".join(why)
    res["verdict"] = "promising" if not why else ("known" if all("ExoFOP" in w or "CTOI" in w for w in why) else "likely false positive")
    print(json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in res.items()}, default=str), flush=True)
    return res


def write_table(cfg: Config, rows: list[dict]) -> None:
    out = vetting_dir(cfg)
    df = pd.DataFrame(rows)
    f = out / "vetting.csv"
    if f.exists():
        old = pd.read_csv(f)
        df = pd.concat([old[~old.tic.isin(df.tic)], df])
    df.to_csv(f, index=False)
    df.to_json(out / "vetting.json", orient="records", indent=1)


def new_tics(cfg: Config) -> list[int]:
    c = pd.read_csv(cfg.sector_results / "candidates.csv")
    return [int(t) for t in c[c.status == "NEW"].tic]


def run(cfg: Config, tics: list[int], *, raw: pd.DataFrame | None = None) -> int:
    if raw is None:
        raw = load_raw(cfg)
    rows = [vet_star(cfg, int(tic), raw=raw) for tic in tics]
    write_table(cfg, rows)
    return 0


def run_new(cfg: Config) -> int:
    """Vet every NEW candidate. Per-star failures are recorded and do not stop the rest."""
    cand = cfg.sector_results / "candidates.csv"
    if not cand.is_file():
        print(f"sector {cfg.sector}: no candidates.csv, nothing to vet", flush=True)
        return 0
    tics = new_tics(cfg)
    print(f"sector {cfg.sector}: vetting {len(tics)} NEW candidates", flush=True)
    if not tics:
        return 0
    raw = load_raw(cfg)
    rows = []
    for t in tics:
        try:
            rows.append(vet_star(cfg, t, raw=raw))
        except Exception as e:
            print(f"TIC {t} ERROR {e}", flush=True)
            rows.append(dict(tic=t, verdict="error", flags=str(e)[:120]))
    write_table(cfg, rows)
    print(f"sector {cfg.sector}: done", flush=True)
    return 0
