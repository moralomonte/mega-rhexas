"""Local worker vetting. Catalog HTTP is fixture data; nothing here opens a socket."""
import json
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from rhexas.config import ConfigError, VettingConfig, default_config, load_config
from rhexas.worker.dashboard import bind_server, make_handler
from rhexas.worker.holdings import disk_used_bytes, holdings_view
from rhexas.worker.live import DB_NAME, Store
from rhexas.worker.loop import _notify_vetting
from rhexas.worker.vetting.catalogs import (
    TOI_URL,
    crossmatch,
    gaia_check,
    index_ctoi,
    index_toi,
    load_catalog_list,
    parse_table,
    simbad_check,
    vsx_check,
)
from rhexas.worker.vetting.checks import (
    DISCLAIMER,
    VERDICTS,
    alias_check,
    centroid_check,
    duration_check,
    expected_duration_days,
    light_checks,
    odd_even_check,
    secondary_check,
    shape_check,
    summarize,
    systematics_check,
)
from rhexas.worker.vetting.evaluate import evaluate
from rhexas.worker.vetting.runner import (
    ENABLE_NOTE,
    VettingRunner,
    ensure_runner,
    export_report,
    status_payload,
    stop_all,
)
from rhexas.worker.vetting.store import migrate

FIX = Path(__file__).resolve().parent / "fixtures" / "vetting"


@pytest.fixture(autouse=True)
def _stop_vetting_threads():
    yield
    stop_all()


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def _router(url: str, timeout: float, files: dict):
    if "download_toi" in url:
        return 200, files.get("toi", _read("toi.csv"))
    if "download_ctoi" in url:
        return 200, files.get("ctoi", _read("ctoi.csv"))
    if "tic82" in url:
        return 200, files.get("tic", _read("tic.csv"))
    if "simbad" in url and "ident" in url:
        return 200, files.get("simbad_id", _read("simbad_star.json"))
    if "simbad" in url:
        return 200, files.get("simbad_cone", _read("simbad_empty.json"))
    if "B/vsx" in url or "vsx" in url:
        return 200, files.get("vsx", _read("vsx_empty.csv"))
    if "gaiadr3" in url or "I/355" in url:
        return 200, files.get("gaia", _read("gaia_clean.csv"))
    return 404, "missing"


def _fetcher(files=None, calls=None):
    chosen = files or {}

    def fetch(url, timeout):
        if calls is not None:
            calls.append(url)
        return _router(url, timeout, chosen)

    return fetch


def _curve(kind="planet", *, period=3.2, t0=1.0, depth=0.01, quality_flags=False):
    duration = expected_duration_days(period, 1.0, 1.0)
    step = 2.0 / 1440.0
    time_values = np.arange(0.0, 27.0, step)
    phase = np.mod(time_values - t0, period) / period
    centered = np.where(phase > 0.5, phase - 1.0, phase)
    half = 0.5 * duration / period
    inside = np.abs(centered) <= half
    flux = np.ones(time_values.shape)
    if kind == "planet":
        flux[inside] = 1.0 - depth
    elif kind == "v":
        x = np.abs(centered) / half
        flux[inside] = 1.0 - depth * (1.0 - x[inside])
    elif kind == "eb":
        epoch = np.round((time_values - t0) / period).astype(int)
        flux[inside & (epoch % 2 == 0)] = 1.0 - 0.03
        flux[inside & (epoch % 2 == 1)] = 1.0 - 0.004
        secondary = np.abs(phase - 0.5) <= half
        flux[secondary] = 1.0 - 0.02
    elif kind == "alias":
        # Dips only on even epochs, so twice the period describes the curve.
        epoch = np.round((time_values - t0) / period).astype(int)
        flux[inside & (epoch % 2 == 0)] = 1.0 - depth
    else:
        raise ValueError(kind)
    quality = None
    if quality_flags:
        quality = np.zeros(time_values.shape, dtype=int)
        quality[inside] = 32
    return time_values, flux, duration, quality


def _by_name(checks):
    return {item["name"]: item for item in checks}


def _words(text: str) -> set[str]:
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in text)
    return set(cleaned.split())


def test_planet_like_light_curve_passes():
    time_values, flux, duration, _quality = _curve("planet")
    checks = light_checks(
        time_values, flux, period=3.2, t0=1.0, duration=duration,
        radius=1.0, mass=1.0, centroid_sigma=0.4, centroid_shift=0.2,
    )
    found = _by_name(checks)
    assert found["odd_even"]["status"] == "pass"
    assert found["odd_even"]["numbers"]["sigma"] < 3
    assert found["secondary"]["status"] == "pass"
    assert found["shape"]["status"] == "pass"
    assert found["duration"]["status"] == "pass"
    assert found["systematics"]["status"] == "pass"
    assert found["alias"]["status"] == "pass"
    assert found["centroid"]["status"] == "pass"
    assert summarize(checks) == "Passed automated checks"


def test_eclipsing_binary_fails_odd_even_and_secondary():
    time_values, flux, duration, _quality = _curve("eb")
    checks = light_checks(
        time_values, flux, period=3.2, t0=1.0, duration=duration,
        radius=1.0, mass=1.0, centroid_sigma=0.4, centroid_shift=0.2,
    )
    found = _by_name(checks)
    assert found["odd_even"]["status"] == "fail"
    assert found["odd_even"]["numbers"]["sigma"] > 3
    assert found["secondary"]["status"] == "fail"
    assert found["secondary"]["numbers"]["phase_half_sigma"] > 3
    assert summarize(checks) == "Likely false positive"


def test_v_shape_warns_and_a_long_duration_warns():
    time_values, flux, duration, _quality = _curve("v")
    shape = shape_check(time_values, flux, 3.2, 1.0, duration)
    assert shape["status"] == "warn"
    assert shape["numbers"]["edge_over_center"] < 0.55
    odd = odd_even_check(time_values, flux, 3.2, 1.0, duration)
    sec = secondary_check(time_values, flux, 3.2, 1.0, duration)
    assert odd["status"] == "pass"
    assert sec["status"] == "pass"
    long = duration_check(3.2, 0.8, 1.0, 1.0)
    assert long["status"] == "warn"
    assert long["numbers"]["ratio"] > 2.5
    missing = duration_check(3.2, duration, None, None)
    assert missing["status"] == "unavailable"
    assert summarize([shape, odd, sec]) == "Needs review"


def test_systematics_centroid_and_alias():
    time_values, flux, duration, quality = _curve("planet", quality_flags=True)
    flagged = systematics_check(time_values, flux, 3.2, 1.0, duration, quality)
    assert flagged["status"] == "warn"
    assert flagged["numbers"]["momentum_dump_fraction"] >= 0.25
    harmonic = systematics_check(time_values, flux, 13.7, 1.0, duration, None)
    assert harmonic["status"] == "warn"
    assert harmonic["numbers"]["harmonic_days"] == pytest.approx(13.7, abs=0.1)
    single_t = time_values[time_values < 2.0]
    single_y = flux[: single_t.size]
    single = systematics_check(single_t, single_y, 3.2, 1.0, duration, None)
    assert single["status"] == "warn"
    assert single["numbers"]["n_transits"] < 2

    shift = centroid_check(6.2, 4.0)
    assert shift["status"] == "fail"
    assert "blend" in shift["reason"]
    mild = centroid_check(3.4, 1.0)
    assert mild["status"] == "warn"
    quiet = centroid_check(0.8, 0.1)
    assert quiet["status"] == "pass"
    assert centroid_check(None, None)["status"] == "unavailable"

    alias_t, alias_y, alias_d, _q = _curve("alias")
    alias = alias_check(alias_t, alias_y, 3.2, 1.0, alias_d)
    assert alias["status"] in {"warn", "fail"}
    assert alias["numbers"]["double_rms"] < alias["numbers"]["rms"]


def test_catalog_fixtures_parse_without_a_socket():
    eb = parse_table(_read("simbad_eb.json"))
    assert eb[0]["otype"] == "EB*"
    known = simbad_check(
        [{"main_id": "V* AB And", "otype": "EB*", "dist": 0.4}],
        error="", radius=15,
    )
    assert known["status"] == "fail" and known["known"] is True
    star = simbad_check(
        [{"main_id": "HD 1", "otype": "*", "dist": 0.2}],
        error="", radius=15,
    )
    assert star["status"] == "pass"
    empty = vsx_check([], error="", radius=15, ra=10, dec=20)
    assert empty["status"] == "pass"
    hit = vsx_check(parse_table(_read("vsx_eb.csv")), error="", radius=15, ra=10, dec=20)
    assert hit["status"] == "fail" and hit["known"] is True
    assert index_toi(_read("toi_hit.csv"))[42][0]["toi"] == "123.01"
    assert index_ctoi(_read("ctoi_hit.csv"))[42][0]["ctoi"] == "9.01"
    clean = gaia_check(parse_table(_read("gaia_clean.csv")), error="", ra=10, dec=20, depth=0.01)
    assert clean["status"] == "pass"
    assert clean["numbers"]["ruwe"] == pytest.approx(1.05, abs=0.01)
    ruwe = gaia_check(parse_table(_read("gaia_ruwe.csv")), error="", ra=10, dec=20, depth=0.01)
    assert ruwe["status"] == "warn"
    blend = gaia_check(parse_table(_read("gaia_blend.csv")), error="", ra=10, dec=20, depth=0.01)
    assert blend["status"] == "fail"
    assert blend["numbers"]["blend_arcsec"] < 21
    down = simbad_check([], error="timed out", radius=15)
    assert down["status"] == "unavailable"


def test_crossmatch_uses_fixtures_and_caches_the_daily_lists(tmp_path):
    calls = []
    files = {
        "simbad_id": _read("simbad_eb.json"),
        "simbad_cone": _read("simbad_empty.json"),
        "vsx": _read("vsx_empty.csv"),
        "gaia": _read("gaia_clean.csv"),
    }
    checks, meta = crossmatch(
        tmp_path, 42, None, None, 0.01,
        radius_arcsec=15, timeout_s=5, fetcher=_fetcher(files, calls), cache_max_mb=1, now=1_000_000,
    )
    found = _by_name(checks)
    assert found["simbad"]["status"] == "fail" and found["simbad"]["known"]
    assert found["toi"]["status"] == "pass"
    assert found["ctoi"]["status"] == "pass"
    assert meta["radius"] == pytest.approx(1.0)
    assert meta["ra"] == pytest.approx(10.0)
    assert summarize(checks) == "Known object"
    toi_calls = [url for url in calls if "download_toi" in url]
    assert len(toi_calls) == 1
    import os
    saved = 1_000_000.0
    for name in ("toi.csv", "ctoi.csv"):
        os.utime(tmp_path / "vetting" / "lists" / name, (saved, saved))
    again, _meta = crossmatch(
        tmp_path, 42, 10.0, 20.0, 0.01,
        radius_arcsec=15, timeout_s=5, fetcher=_fetcher(files, calls), cache_max_mb=1, now=saved + 100,
    )
    assert len([url for url in calls if "download_toi" in url]) == 1
    assert _by_name(again)["simbad"]["status"] == "fail"
    stale = saved - 3 * 24 * 3600
    for name in ("toi.csv", "ctoi.csv"):
        os.utime(tmp_path / "vetting" / "lists" / name, (stale, stale))
    crossmatch(
        tmp_path, 7, 10.0, 20.0, 0.01,
        radius_arcsec=15, timeout_s=5, fetcher=_fetcher(files, calls), cache_max_mb=1, now=saved,
    )
    assert len([url for url in calls if "download_toi" in url]) == 2


def test_offline_catalogs_are_unavailable_and_retried(tmp_path):
    def offline(_url, _timeout):
        raise TimeoutError("offline")

    checks, _meta = crossmatch(
        tmp_path, 42, 10.0, 20.0, 0.01,
        radius_arcsec=15, timeout_s=5, fetcher=offline, now=1_000,
    )
    assert all(item["status"] == "unavailable" for item in checks)
    cache = tmp_path / "cache"
    cache.mkdir()
    _seed_candidate(cache, 42, 84)
    _write_fits(cache, 42, 84, *_curve("planet")[:3])
    state = {"up": False, "calls": 0}

    def flaky(url, timeout):
        state["calls"] += 1
        if not state["up"]:
            raise TimeoutError("offline")
        return _fetcher()(url, timeout)

    now = {"t": 10_000.0}
    settings = VettingConfig(enabled=True, timeout_s=5, cache_max_mb=5)
    runner = VettingRunner(cache, settings, fetcher=flaky, clock=lambda: now["t"])
    assert runner.run_once() is True
    row = _vetting_row(cache, 42, 84)
    assert row["state"] == "done"
    assert row["verdict"] == "Needs review"
    assert row["retry_at"] > now["t"]
    result = json.loads(row["result_json"])
    assert any(item["status"] == "unavailable" for item in result["checks"])
    now["t"] = row["retry_at"] + 1
    state["up"] = True
    assert runner.run_once() is True
    done = _vetting_row(cache, 42, 84)
    assert done["verdict"] == "Passed automated checks"
    assert done["retry_at"] is None
    assert state["calls"] > 0


def test_full_candidate_passes_from_disk_and_fixtures(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    _seed_candidate(cache, 42, 84)
    time_values, flux, duration = _curve("planet")[:3]
    _write_fits(cache, 42, 84, time_values, flux, duration)
    settings = VettingConfig(enabled=True, timeout_s=5, cache_max_mb=5)
    outcome = evaluate(
        cache,
        {"tic": 42, "sector": 84, "period": 3.2, "t0": 1.0, "duration": duration, "depth": 0.01, "snr": 12, "sde": 9, "attempts": 0},
        settings,
        fetcher=_fetcher(),
        now=5_000,
    )
    assert outcome["verdict"] == "Passed automated checks"
    assert outcome["verdict"] in VERDICTS
    blob = json.dumps(outcome["result"])
    assert "planet" not in blob.lower()
    assert "new" not in _words(blob)
    assert DISCLAIMER in outcome["result"]["disclaimer"]
    runner = VettingRunner(cache, settings, fetcher=_fetcher(), clock=lambda: 5_000)
    # The row is not pending yet; sync from the candidates table queues it.
    assert runner.run_once() is True
    saved = _vetting_row(cache, 42, 84)
    assert saved["verdict"] == "Passed automated checks"
    exported = export_report(cache, 42, 84, True)
    assert exported["ok"] is True
    assert "Passed automated checks" in exported["markdown"]
    assert "person still needs to review" in exported["markdown"]
    assert Path(exported["markdown_path"]).is_file()
    assert Path(exported["json_path"]).is_file()
    document = json.loads(exported["json"])
    assert document["links"]["exofop"].endswith("id=42")
    assert "simbad" in document["links"]
    assert "planet" not in exported["markdown"].lower()
    assert "new" not in _words(exported["markdown"])


def test_disabled_setting_does_no_network_and_explains_how_to_enable(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("network lookup while vetting is off")

    monkeypatch.setattr("rhexas.worker.vetting.catalogs.urlopen_fetch", boom)
    cache = tmp_path / "cache"
    cache.mkdir()
    _seed_candidate(cache, 42, 84)
    assert ensure_runner(cache, VettingConfig(enabled=False)) is None
    assert all(thread.name != "rhexas-vetting" for thread in threading.enumerate())
    runner = VettingRunner(cache, VettingConfig(enabled=False), fetcher=boom)
    assert runner.run_once() is False
    payload = status_payload(cache, False)
    assert payload["enabled"] is False
    assert "enabled = true" in payload["note"]
    assert "[vetting]" in payload["note"]
    assert payload["candidates"] == []
    assert export_report(cache, 42, 84, False)["ok"] is False

    httpd, port = bind_server("127.0.0.1", 0, make_handler(cache, {"vetting_enabled": False}))
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/vetting", timeout=5)
        body = page.read().decode("utf-8")
        assert page.status == 200
        assert "no-store" in page.headers.get("Cache-Control", "").lower()
        assert "Re-run vetting" in body and "Export report" in body
        assert "__APP_VERSION__" not in body
        assert 'class="cv h280"' in body
        from rhexas import __version__
        assert f"/img/triskelion.svg?v={__version__}" in body
        home = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode("utf-8")
        assert home.find('id="sectors"') < home.find('id="vetting"')
        assert f'href="/vetting?v={__version__}"' in home
        api = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/vetting", timeout=5).read().decode("utf-8"))
        assert api["enabled"] is False
        assert api["note"] == ENABLE_NOTE
    finally:
        httpd.shutdown()
        thread.join(timeout=2)


def test_vetting_never_runs_on_the_pipeline_thread(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    _seed_candidate(cache, 42, 84)
    seen = []

    def spy(_cache, _row, _settings, _fetcher=None, now=None):
        seen.append(threading.current_thread().name)
        return {
            "verdict": "Needs review",
            "result": {
                "checks": [{
                    "name": "simbad", "title": "SIMBAD", "status": "pass",
                    "reason": "No catalog class inside the radius.", "numbers": {},
                }],
                "plot": None, "links": {}, "disclaimer": DISCLAIMER,
            },
            "fields": {},
            "retry_at": None,
            "attempts": 0,
        }

    settings = VettingConfig(enabled=True, timeout_s=5, cache_max_mb=5)
    cfg = default_config(tmp_path)
    from dataclasses import replace
    cfg = replace(cfg, vetting=settings)
    called = []

    def inline(*_args, **_kwargs):
        called.append(threading.current_thread().name)

    # The pipeline hook only wakes a runner. With none started, it does not vet.
    from rhexas.worker import vetting as vetting_pkg
    original = vetting_pkg.runner.default_evaluate
    vetting_pkg.runner.default_evaluate = inline
    try:
        _notify_vetting(cache, cfg)
    finally:
        vetting_pkg.runner.default_evaluate = original
    assert called == []

    ensure_runner(cache, settings, evaluate=spy)
    holder = {}

    def pipeline():
        holder["name"] = threading.current_thread().name
        _notify_vetting(cache, cfg)

    worker = threading.Thread(target=pipeline, name="pipeline-scan")
    worker.start()
    worker.join(timeout=3)
    deadline = time.time() + 5
    while time.time() < deadline and not seen:
        time.sleep(0.02)
    assert holder["name"] == "pipeline-scan"
    assert seen == ["rhexas-vetting"]
    assert "pipeline-scan" not in seen


def test_table_is_keyed_by_tic_and_sector_and_cache_counts(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    Store(cache)
    conn = sqlite3.connect(cache / DB_NAME)
    conn.row_factory = sqlite3.Row
    migrate(conn)
    migrate(conn)
    _insert(conn, 42, 84)
    _insert(conn, 42, 85)
    conn.commit()
    from rhexas.worker.vetting.store import sync_candidates
    assert sync_candidates(conn, 1) == 2
    assert sync_candidates(conn, 2) == 0
    rows = conn.execute("SELECT sector FROM vetting WHERE tic=42 ORDER BY sector").fetchall()
    conn.close()
    assert [row["sector"] for row in rows] == [84, 85]
    folder = cache / "vetting" / "catalogs"
    folder.mkdir(parents=True)
    (folder / "tic-42.json").write_text("{}", encoding="utf-8")
    assert disk_used_bytes(cache) >= 2
    view = holdings_view(cache, configured_cap_gb=20, queue_max=3, min_free_gb=5)
    assert view["vetting_bytes"] >= 2
    assert view["disk_bytes"] >= view["vetting_bytes"]


def test_vetting_config_is_opt_in(tmp_path):
    cfg = default_config(tmp_path)
    assert cfg.vetting.enabled is False
    path = tmp_path / "rhexas.toml"
    path.write_text("[vetting]\nenabled = true\nradius_arcsec = 12\n", encoding="utf-8")
    loaded = load_config(path, environ={}, overrides={"root": tmp_path})
    assert loaded.vetting.enabled is True
    assert loaded.vetting.radius_arcsec == 12.0
    path.write_text('[vetting]\nenabled = "yes"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path, environ={})


def _seed_candidate(cache: Path, tic: int, sector: int) -> None:
    Store(cache)
    conn = sqlite3.connect(cache / DB_NAME)
    _insert(conn, tic, sector)
    duration = expected_duration_days(3.2, 1.0, 1.0)
    record = {
        "tic": tic, "sector": sector, "unit": 1,
        "period": 3.2, "t0": 1.0, "duration": duration,
        "depth": 0.01, "snr": 12.0, "sde": 9.0,
        "ra": 10.0, "dec": 20.0,
        "centroid_sigma": 0.4, "centroid_shift_arcsec": 0.2,
    }
    conn.execute(
        """INSERT INTO stars (
               sector, unit, tic, ra, dec, period, snr, sde, verdict,
               candidate, version, finished_at, record_json
           ) VALUES (?, 1, ?, 10.0, 20.0, 3.2, 12, 9, 'NEW', 1, 1, 0, ?)""",
        (int(sector), int(tic), json.dumps(record)),
    )
    conn.commit()
    conn.close()


def _insert(conn, tic: int, sector: int) -> None:
    conn.execute(
        """INSERT INTO candidates (
               project, sector, unit_id, tic, period, depth, snr, sde,
               phase_json, flux_json, found_at, status
           ) VALUES ('exoplanets', ?, 1, ?, 3.2, 0.01, 12, 9, '[]', '[]', 0, 'NEW')""",
        (int(sector), int(tic)),
    )


def _vetting_row(cache: Path, tic: int, sector: int):
    conn = sqlite3.connect(cache / DB_NAME)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM vetting WHERE tic=? AND sector=?", (tic, sector)).fetchone()
    conn.close()
    return row


def _write_fits(cache: Path, tic: int, sector: int, time_values, flux, _duration) -> None:
    from astropy.io import fits
    folder = cache / "data" / f"s{int(sector):04d}"
    folder.mkdir(parents=True)
    columns = fits.ColDefs([
        fits.Column(name="TIME", format="D", array=np.asarray(time_values, dtype=float)),
        fits.Column(name="PDCSAP_FLUX", format="D", array=np.asarray(flux, dtype=float)),
        fits.Column(name="PDCSAP_FLUX_ERR", format="D", array=np.full(len(time_values), 1e-4)),
        fits.Column(name="QUALITY", format="J", array=np.zeros(len(time_values), dtype=np.int32)),
        fits.Column(name="MOM_CENTR1", format="D", array=np.full(len(time_values), 5.0)),
        fits.Column(name="MOM_CENTR2", format="D", array=np.full(len(time_values), 6.0) + np.linspace(0, 1e-4, len(time_values))),
    ])
    hdul = fits.HDUList([fits.PrimaryHDU(), fits.BinTableHDU.from_columns(columns, name="LIGHTCURVE")])
    hdul[0].header["TICID"] = int(tic)
    hdul[0].header["RA_OBJ"] = 10.0
    hdul[0].header["DEC_OBJ"] = 20.0
    name = f"tess2026-s{int(sector):04d}-{int(tic):016d}-0001-s_lc.fits"
    hdul.writeto(folder / name)


def _offline(_url, _timeout):
    return 404, "no"


def _write_plain_fits(cache: Path, tic: int, sector: int, time_values, flux, *, centroid: bool) -> None:
    from astropy.io import fits

    folder = cache / "data" / f"s{int(sector):04d}"
    folder.mkdir(parents=True, exist_ok=True)
    columns = [
        fits.Column(name="TIME", format="D", array=np.asarray(time_values, dtype=float)),
        fits.Column(name="PDCSAP_FLUX", format="D", array=np.asarray(flux, dtype=float)),
        fits.Column(name="QUALITY", format="J", array=np.zeros(len(time_values), dtype=np.int32)),
    ]
    if centroid:
        columns.append(fits.Column(name="MOM_CENTR1", format="D", array=np.full(len(time_values), 5.0)))
        columns.append(fits.Column(name="MOM_CENTR2", format="D", array=np.full(len(time_values), 6.0)))
    hdul = fits.HDUList([
        fits.PrimaryHDU(),
        fits.BinTableHDU.from_columns(fits.ColDefs(columns), name="LIGHTCURVE"),
    ])
    name = f"tess2026-s{int(sector):04d}-{int(tic):016d}-0001-s_lc.fits"
    hdul.writeto(folder / name, overwrite=True)


def test_bls_fills_a_missing_epoch_from_the_light_curve(tmp_path):
    period, t0, duration = 3.2, 5.0, 0.15
    times = np.linspace(0.0, 20.0, 900)
    phase = np.mod(times - t0, period) / period
    centered = np.where(phase > 0.5, phase - 1.0, phase)
    flux = np.ones(times.shape)
    flux[np.abs(centered) <= 0.5 * duration / period] = 0.98
    cache = tmp_path / "bls"
    cache.mkdir()
    _write_plain_fits(cache, 349911034, 89, times, flux, centroid=True)
    outcome = evaluate(
        cache,
        {"tic": 349911034, "sector": 89, "period": period, "depth": 0.02},
        VettingConfig(enabled=True, timeout_s=5, cache_max_mb=1),
        fetcher=_offline,
        now=1_000.0,
    )
    found = outcome["fields"]["t0"]
    assert found is not None
    delta = abs((found - t0) % period)
    assert min(delta, period - delta) < 0.2
    assert outcome["fields"]["period"] == pytest.approx(period)
    assert outcome["fields"]["duration"] is not None
    assert not any("No sector light curve" in str(item.get("reason") or "") for item in outcome["result"]["checks"])


def test_a_missing_light_curve_stays_unavailable(tmp_path):
    outcome = evaluate(
        tmp_path / "empty",
        {"tic": 1, "sector": 89, "period": 3.2},
        VettingConfig(enabled=True, timeout_s=5, cache_max_mb=1),
        fetcher=_offline,
        now=1_000.0,
    )
    light = next(item for item in outcome["result"]["checks"] if item["name"] == "light_curve")
    assert light["status"] == "unavailable"
    centroid = next(item for item in outcome["result"]["checks"] if item["name"] == "centroid")
    assert centroid["status"] == "unavailable"


def test_tpf_download_runs_only_when_the_centroid_is_missing(tmp_path):
    period = 3.2
    times = np.linspace(0.0, 12.0, 200)
    flux = np.ones(times.shape)
    kinds = []

    def download(_cache, _tic, _sector, kind):
        kinds.append(kind)
        return {"path": None}

    cache = tmp_path / "tpf"
    cache.mkdir()
    settings = VettingConfig(enabled=True, timeout_s=5, cache_max_mb=1)
    _write_plain_fits(cache, 11, 89, times, flux, centroid=False)
    bare = evaluate(
        cache, {"tic": 11, "sector": 89, "period": period, "t0": 1.0, "duration": 0.1},
        settings, fetcher=_offline, now=1.0, download=download,
    )
    assert kinds == ["lc", "tpf"]
    centroid = next(item for item in bare["result"]["checks"] if item["name"] == "centroid")
    assert centroid["status"] == "unavailable"

    kinds.clear()
    _write_plain_fits(cache, 12, 89, times, flux, centroid=True)
    evaluate(
        cache, {"tic": 12, "sector": 89, "period": period, "t0": 1.0, "duration": 0.1},
        settings, fetcher=_offline, now=1.0, download=download,
    )
    assert kinds == ["lc"]


def test_simbad_uses_harvard_and_a_dead_service_stays_unavailable(tmp_path):
    urls = []

    def harvard(url, timeout):
        urls.append(url)
        if "simbad.cfa.harvard.edu" in url and "ident" in url:
            return 200, _read("simbad_star.json")
        if "simbad" in url:
            return 200, _read("simbad_empty.json")
        if "tic82" in url:
            return 200, _read("tic.csv")
        if "download_toi" in url:
            return 200, _read("toi.csv")
        if "download_ctoi" in url:
            return 200, _read("ctoi.csv")
        if "vsx" in url:
            return 200, _read("vsx_empty.csv")
        if "gaiadr3" in url or "I/355" in url:
            return 200, _read("gaia_clean.csv")
        return 404, "missing"

    checks, _meta = crossmatch(
        tmp_path / "harvard", 42, None, None, 0.01,
        radius_arcsec=15, timeout_s=5, fetcher=harvard, cache_max_mb=1, now=1_000.0,
    )
    simbad_urls = [url for url in urls if "simbad" in url]
    assert any("simbad.cfa.harvard.edu" in url for url in simbad_urls)
    assert simbad_urls
    assert not any("unistra.fr" in url or "u-strasbg.fr" in url for url in simbad_urls)
    assert _by_name(checks)["simbad"]["status"] == "pass"

    def down(url, timeout):
        if "simbad" in url:
            return 404, "gone"
        if "tic82" in url:
            return 200, _read("tic.csv")
        if "download_" in url:
            return 200, _read("toi.csv") if "toi" in url else _read("ctoi.csv")
        if "vsx" in url:
            return 200, _read("vsx_empty.csv")
        if "gaia" in url or "I/355" in url:
            return 200, _read("gaia_clean.csv")
        return 404, "missing"

    failed, _meta = crossmatch(
        tmp_path / "down", 42, None, None, 0.01,
        radius_arcsec=15, timeout_s=5, fetcher=down, cache_max_mb=1, now=1_000.0,
    )
    simbad = _by_name(failed)["simbad"]
    assert simbad["status"] == "unavailable"


def test_mast_coordinates_are_used_when_vizier_has_none(tmp_path):
    seen = []

    def fetcher(url, timeout):
        seen.append(url)
        if str(url).startswith("mast-tic://"):
            return 200, json.dumps({
                "data": [{"ra": 113.97, "dec": -65.68, "Tmag": 11.8, "rad": 1.3, "mass": 1.04}],
            })
        if "tic82" in url:
            return 500, "vizier down"
        if "download_toi" in url:
            return 200, _read("toi.csv")
        if "download_ctoi" in url:
            return 200, _read("ctoi.csv")
        if "simbad" in url:
            return 200, _read("simbad_empty.json")
        return 404, "vizier down"

    checks, meta = crossmatch(
        tmp_path / "mast", 349911034, None, None, 0.01,
        radius_arcsec=15, timeout_s=5, fetcher=fetcher, cache_max_mb=1, now=1_000.0,
    )
    assert meta["ra"] == pytest.approx(113.97)
    assert meta["dec"] == pytest.approx(-65.68)
    assert any("113.9700000" in url and "vsx" in url for url in seen)
    assert any("113.9700000" in url and ("gaiadr3" in url or "I/355" in url) for url in seen)
    vsx = _by_name(checks)["vsx"]
    gaia = _by_name(checks)["gaia"]
    assert vsx["status"] == "unavailable"
    assert gaia["status"] == "unavailable"
    assert "no sky position" not in vsx["reason"]
    assert "no sky position" not in gaia["reason"]


def test_toi_list_retries_with_a_long_timeout_and_keeps_a_ttl(tmp_path):
    import os

    calls = []
    stamp = time.time()
    state = {"fails_left": 2}

    def fetcher(url, timeout):
        calls.append((url, timeout))
        if state["fails_left"] > 0:
            state["fails_left"] -= 1
            raise TimeoutError("slow")
        return 200, "TIC,TOI\n42,1.01\n"

    text, error = load_catalog_list(tmp_path, "toi", TOI_URL, fetcher, 120, now=stamp, retries=3)
    assert error == ""
    assert "42,1.01" in text
    assert [timeout for _url, timeout in calls] == [120, 120, 120]
    calls.clear()
    cached, cached_error = load_catalog_list(tmp_path, "toi", TOI_URL, fetcher, 120, now=stamp + 10, retries=3)
    assert cached_error == ""
    assert "42,1.01" in cached
    assert calls == []
    os.utime(tmp_path / "vetting" / "lists" / "toi.csv", (stamp - 3 * 24 * 3600, stamp - 3 * 24 * 3600))
    refreshed, refresh_error = load_catalog_list(tmp_path, "toi", TOI_URL, fetcher, 120, now=stamp, retries=3)
    assert refresh_error == ""
    assert "42,1.01" in refreshed
    assert len(calls) == 1

    def fail(_url, _timeout):
        calls.append(("fail", 0))
        raise TimeoutError("still down")

    missed, miss_error = load_catalog_list(tmp_path / "empty", "toi", TOI_URL, fail, 120, now=stamp, retries=3)
    assert missed == ""
    assert miss_error
    assert sum(1 for url, _timeout in calls if url == "fail") == 3
