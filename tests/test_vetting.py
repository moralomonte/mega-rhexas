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
    crossmatch,
    gaia_check,
    index_ctoi,
    index_toi,
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
