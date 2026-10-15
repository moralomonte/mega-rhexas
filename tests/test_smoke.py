"""Offline BLS + filter smoke test on a synthetic light curve. No network and no FITS download."""
import sys

import numpy as np
import pandas as pd
import pytest
from astropy.io import fits

from rhexas.config import FilterConfig, SearchConfig, load_config
from rhexas.filter import apply_thresholds, run as filter_run
from rhexas.multisector import fit_ephemeris
from rhexas.pipeline import _is_download_cmd
from rhexas.search import FIELDS, bls_metrics

pytestmark = pytest.mark.filterwarnings("ignore")


def _transit(n_periods: int):
    period = 3.25
    duration = 0.08
    depth = 0.02
    t0 = 0.4
    t = np.arange(0.0, 24.0, 10 / 1440.0)
    phase = ((t - t0 + 0.5 * period) % period) - 0.5 * period
    y = np.ones_like(t)
    y[np.abs(phase) < duration / 2] -= depth
    rng = np.random.default_rng(1)
    y = y + rng.normal(0, 2e-4, size=t.shape)
    dy = np.full_like(t, 2e-4)
    search = SearchConfig(n_periods=n_periods)
    return period, bls_metrics(t, y, dy, search)


def test_search_on_an_empty_sector_writes_the_header_only(tmp_path):
    cfg = load_config(
        search_files=False,
        environ={},
        overrides={
            "root": tmp_path,
            "sector": 42,
            "data_dir": tmp_path / "data",
            "results_root": tmp_path / "results",
            "logs_dir": tmp_path / "logs",
        },
    )
    cfg.sector_data.mkdir(parents=True)
    from rhexas.search import run
    assert run(cfg, procs=1) == 0
    header = (cfg.sector_results / "bls_raw.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header == ",".join(FIELDS)


def test_search_defaults_match_the_historical_grid():
    s = SearchConfig()
    assert s.period_min_days == 0.5
    assert s.period_max_days == 13.0
    assert s.n_periods == 20000
    assert s.bin_minutes == 10
    assert s.flatten_window == 721
    assert FIELDS[0] == "tic" and FIELDS[-1] == "error"
    assert "odd_even_sigma" in FIELDS and "sec_sigma" in FIELDS


def test_analyze_reads_a_local_synthetic_tess_file(tmp_path):
    """Full search path (lightkurve read, flatten, bin, BLS) on a file we write ourselves."""
    from astropy.table import Table
    from rhexas.search import _ACTIVE, _pool_init, analyze

    period, duration, depth, t0 = 2.2, 0.08, 0.02, 0.5
    time = np.arange(0.0, 12.0, 2 / 1440.0)
    phase = ((time - t0 + 0.5 * period) % period) - 0.5 * period
    flux = np.ones_like(time)
    flux[np.abs(phase) < duration / 2] -= depth
    flux = flux + np.random.default_rng(0).normal(0, 3e-4, size=time.shape)
    err = np.full_like(flux, 3e-4)
    table = Table({
        "TIME": time, "PDCSAP_FLUX": flux, "PDCSAP_FLUX_ERR": err,
        "SAP_FLUX": flux, "SAP_FLUX_ERR": err, "QUALITY": np.zeros(len(time), dtype=np.int32),
    })
    primary = fits.PrimaryHDU()
    primary.header["TELESCOP"] = "TESS"
    primary.header["CREATOR"] = "LightCurveExporterPipelineModule"
    primary.header["ORIGIN"] = "NASA/Ames"
    primary.header["OBJECT"] = "TIC 42"
    primary.header["TICID"] = 42
    path = tmp_path / "tess2026000000000-s0042-0000000000000042-0001-s_lc.fits"
    fits.HDUList([primary, fits.BinTableHDU(table, name="LIGHTCURVE")]).writeto(path)
    import rhexas.search as search_mod
    previous = _ACTIVE
    _pool_init(SearchConfig(n_periods=2000))
    try:
        row = analyze(str(path))
    finally:
        search_mod._ACTIVE = previous
    assert "error" not in row
    assert row["tic"] == 42
    assert abs(row["period"] - period) / period < 0.02
    assert row["snr"] >= 7 and row["sde"] >= 6 and row["n_transits"] >= 3
    frame = apply_thresholds(pd.DataFrame([{**row, "error": np.nan}]), FilterConfig())
    assert bool(frame["pass"].iloc[0])


def test_injected_transit_passes_filter_thresholds():
    period, row = _transit(4000)
    assert abs(row["period"] - period) / period < 0.02
    assert row["n_transits"] >= 3
    assert 0 < row["depth"] <= 0.05
    assert row["duration"] / row["period"] <= 0.12
    frame = pd.DataFrame([{
        **row,
        "tic": 1,
        "file": "synthetic_lc.fits",
        "error": np.nan,
    }])
    passed = apply_thresholds(frame, FilterConfig())
    assert bool(passed["pass"].iloc[0])
    assert passed["sde"].iloc[0] >= 6
    assert passed["snr"].iloc[0] >= 7
    flat_t = np.arange(0.0, 24.0, 10 / 1440.0)
    rng = np.random.default_rng(2)
    flat = bls_metrics(flat_t, 1 + rng.normal(0, 2e-4, size=flat_t.shape), np.full_like(flat_t, 2e-4), SearchConfig(n_periods=1500))
    assert row["sde"] > flat["sde"]


def test_filter_writes_the_historical_candidate_columns(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("filter tried to use the network")

    monkeypatch.setattr("requests.get", boom)
    cfg = load_config(
        search_files=False,
        environ={},
        overrides={
            "root": tmp_path,
            "sector": 42,
            "data_dir": tmp_path / "data",
            "results_root": tmp_path / "results",
            "logs_dir": tmp_path / "logs",
            "catalogs_dir": tmp_path / "catalogs",
        },
    )
    assert cfg.sector_results == (tmp_path / "results" / "s0042").resolve()
    cfg.sector_results.mkdir(parents=True)
    cfg.sector_data.mkdir(parents=True)
    cfg.catalogs_dir.mkdir(parents=True)
    (cfg.catalogs_dir / "toi_catalog.csv").write_text(
        "# comment\nTIC ID,TOI,Period (days),Depth (ppm),TFOPWG Disposition\n2,123.01,3.2,4000,PC\n",
        encoding="utf-8",
    )
    (cfg.catalogs_dir / "confirmed_planets.csv").write_text(
        "tic_id,pl_name,pl_orbper\n3,Example b,4.0\n",
        encoding="utf-8",
    )
    (cfg.sector_results / "vetting").mkdir()
    (cfg.sector_results / "vetting" / "vetting.csv").write_text(
        "tic,verdict,flags\n1,promising,clean\n",
        encoding="utf-8",
    )
    fits_name = "tess-s0042-0000000000000002-s_lc.fits"
    ext = fits.ImageHDU()
    ext.header["CROWDSAP"] = 0.5
    fits.HDUList([fits.PrimaryHDU(), ext]).writeto(cfg.sector_data / fits_name)

    def row(tic, file, snr, sde, depth):
        return dict(tic=tic, file=file, period=3.2, t0=1000.0, duration=0.1, depth=depth, depth_err=0.001,
                    snr=snr, sde=sde, n_transits=5, depth_odd=depth, depth_even=depth, odd_even_sigma=0.4,
                    depth_secondary=0.0, sec_sigma=0.2, npts=1000, error="")

    raw = pd.DataFrame([
        row(1, "missing.fits", 20, 10, 0.01),
        row(2, fits_name, 10, 40, 0.04),
        row(3, "also-missing.fits", 8, 9, 0.01),
        row(4, "deep.fits", 30, 20, 0.2),
    ])
    raw_path = cfg.sector_results / "bls_raw.csv"
    raw.to_csv(raw_path, index=False)
    assert filter_run(cfg, plots=0, fetch_catalogs=True) == 0
    out = pd.read_csv(cfg.sector_results / "candidates.csv")
    assert list(out.columns) == FIELDS + [
        "pass", "toi", "toi_period", "tfopwg", "confirmed_planet", "status", "score",
        "crowdsap", "flags", "verdict", "vet_flags",
    ]
    assert list(out.tic) == [2, 1, 3]
    assert list(out.status) == ["known_TOI", "NEW", "known_confirmed"]
    assert out.loc[out.tic == 2, "score"].iloc[0] == pytest.approx(30)
    assert out.loc[out.tic == 2, "flags"].iloc[0] == "likely EB (depth>3%); contaminated aperture (CROWDSAP 0.50)"
    assert out.loc[out.tic == 1, "verdict"].iloc[0] == "promising"
    assert out.loc[out.tic == 1, "flags"].fillna("").iloc[0] == ""


def test_ephemeris_fit_is_stable_offline():
    close = [
        dict(T0_fix=1000.0, dur_h=1.0, snr_fix=10.0),
        dict(T0_fix=1000.5, dur_h=1.0, snr_fix=10.0),
    ]
    assert fit_ephemeris(close, 1000.0, 2.0) is None
    wide = [
        dict(T0_fix=1000.0, dur_h=1.0, snr_fix=10.0),
        dict(T0_fix=1100.0, dur_h=1.0, snr_fix=10.0),
    ]
    eph = fit_ephemeris(wide, 1000.0, 2.0)
    assert eph["P"] == pytest.approx(2.0)
    assert eph["T0"] == pytest.approx(1000.0)
    assert eph["n_cycles"] == 50


def test_download_process_detection_does_not_match_the_queue_command():
    assert _is_download_cmd(["C:\\Mega Rhexas\\venv\\Scripts\\python.exe", "download.py", "--threads", "8"])
    assert _is_download_cmd([sys.executable, "-m", "rhexas", "download", "--sector", "107"])
    assert not _is_download_cmd([sys.executable, "-m", "rhexas", "queue", "--sectors", "104,103"])
    assert not _is_download_cmd([sys.executable, "-m", "rhexas", "search", "--procs", "4"])
