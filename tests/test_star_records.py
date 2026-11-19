"""Dashboard records: one real star, served as itself, with no stand-in curves."""
import json
import threading
import urllib.request
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astropy.table import Table

from rhexas.config import SearchConfig
from rhexas.worker.dashboard import bind_server, make_handler
from rhexas.worker.holdings import clear_unit, holdings_view, upsert_unit
from rhexas.worker.live import DB_NAME, Store, publish_star, snapshot
from rhexas.worker.present import mission_hist, mission_lc, mission_state
from rhexas.worker.records import retain
from rhexas.search import _pool_init, analyze

pytestmark = pytest.mark.filterwarnings("ignore")


def _limits():
    return {
        "sde_min": 6, "snr_min": 7, "min_transits": 3, "max_depth": 0.05,
        "odd_even_max_sigma": 3, "secondary_max_sigma": 3, "max_duration_fraction": 0.12,
    }


def _spec(path, sector=107, unit=3):
    spec = {"db": str(path / DB_NAME), "project": "exoplanets", "sector": sector, "unit_id": unit}
    spec.update(_limits())
    return spec


def _row(**extra):
    row = {
        "tic": 42, "file": "star.fits", "period": 2.5, "t0": 100.0, "duration": 0.1,
        "depth": 0.01, "snr": 12.0, "sde": 9.0, "n_transits": 4,
        "odd_even_sigma": 0.4, "sec_sigma": 0.2, "ra": 82.5, "dec": -12.25, "tmag": 9.4,
        "_pgram_p": [1.0, 2.5, 4.0], "_pgram_w": [0.2, 9.0, 0.4],
        "_aperture": [[0, 1], [1, 1]],
    }
    row.update(extra)
    return row


def _curve():
    time_values = np.linspace(100.0, 127.0, 360)
    phase = np.mod(time_values - 100.0, 2.5) / 2.5
    phase = np.where(phase > 0.5, phase - 1.0, phase)
    flux = np.where(np.abs(phase) < 0.02, 0.99, 1.0)
    return time_values, flux


def _serve(cache):
    httpd, port = bind_server("127.0.0.1", 0, make_handler(cache, {"max_cache_gb": 20}))
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return httpd, port, thread


def _stop(httpd, thread):
    httpd.dashboard_stop.set()
    httpd.shutdown()
    thread.join(timeout=3)
    httpd.server_close()


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
        return response.headers.get("Content-Type", ""), response.read()


def test_each_panel_reads_a_stored_field_and_not_a_stand_in(tmp_path):
    spec = _spec(tmp_path)
    time_values, flux = _curve()
    publish_star(spec, _row(), time_values, flux, processing=False)
    snap = snapshot(tmp_path)
    star = snap["current_star"]
    assert star["aperture"] == [[0, 1], [1, 1]]
    assert star["ra"] == pytest.approx(82.5)
    assert star["dec"] == pytest.approx(-12.25)
    assert star["time"] and star["flux"] and len(star["time"]) == len(star["flux"])
    assert star["pgram"][0][1] == pytest.approx(2.5)
    assert star["pgram"][1][1] == pytest.approx(9.0)
    assert star["fold"][0] and star["fold"][1]
    assert star["odd"] and star["even"]
    assert star["secondary"]
    assert star["odd_even_sigma"] == pytest.approx(0.4)
    assert star["sec_sigma"] == pytest.approx(0.2)
    assert "centroid" in star["missing"]
    assert star["centroid"] is None
    assert star["centroid_sigma"] is None

    bare = mission_lc({"tic": 7, "period": 2.0, "sde": 8, "status": "done"})
    assert bare["pgram"] is None
    assert bare["raw"] is None
    assert bare["t"] is None
    assert bare["depth"] is None
    assert 0.0 not in (bare["sde"], bare["snr"], bare["depth"])

    state = mission_state(snap)
    assert state["new_counts"]["total"] == 1
    assert state["new_counts"]["verified"] == 0
    assert state["passed"] == 1
    hist = mission_hist(snap)
    assert sum(hist["sde"][0]) >= 1
    assert sum(hist["snr"][0]) >= 1


def test_counters_count_new_candidates_and_leave_known_tois_out(tmp_path):
    spec = _spec(tmp_path)
    time_values, flux = _curve()
    for tic in range(1, 62):
        publish_star(spec, _row(tic=tic, snr=12 if tic < 61 else 1), time_values, flux, processing=False)
    store = Store(tmp_path)
    store.record_candidates(
        {"project": "exoplanets", "sector": 107, "unit_id": 3},
        [{"tic": 1, "period": 2.5, "depth": 0.01, "snr": 12, "sde": 9, "status": "known_TOI"}]
        + [
            {"tic": tic, "period": 2.5, "depth": 0.01, "snr": 12, "sde": 9, "status": "NEW"}
            for tic in range(2, 61)
        ],
    )
    store.note_submit({"verdict": "agreed"}, 10)
    store.note_submit({"verdict": "pending"}, 4)
    snap = snapshot(tmp_path)
    state = mission_state(snap)
    counts = state["new_counts"]
    assert counts["total"] == 59
    assert counts["total"] != 60
    assert counts["known_ctoi"] == 1
    assert counts["verified"] == 1
    assert counts["pending"] == 1
    assert counts["submitted"] == 2
    assert state["passed"] == 59
    assert state["worker_panel"]["contributions_verified"] == 1
    assert state["worker_panel"]["batches_submitted"] == 2


def test_sky_uses_every_stored_summary_and_does_not_rewrite_a_blob(tmp_path):
    spec = _spec(tmp_path, sector=14, unit=1)
    time_values, flux = _curve()
    for index, tic in enumerate((10, 11, 12)):
        publish_star(
            spec,
            _row(tic=tic, ra=20.0 + index, dec=-30.0 - index, snr=1),
            time_values, flux, processing=False,
        )
    httpd, port, thread = _serve(tmp_path)
    try:
        _ctype, body = _get(port, "/api/sky?scope=sector&sector=14")
        sky = json.loads(body)
        assert sky["count"] == 3
        assert len(sky["points"]) == 3
        assert sky["bounds"]["ra0"] < 20
        assert sky["bounds"]["ra1"] > 22
        _ctype, whole = _get(port, "/api/sky?scope=all")
        everywhere = json.loads(whole)
        assert everywhere["count"] == 3
        assert everywhere["bounds"] is None
    finally:
        _stop(httpd, thread)
    import sqlite3
    conn = sqlite3.connect(tmp_path / DB_NAME)
    sky_row = conn.execute("SELECT value FROM kv WHERE key='sky'").fetchone()
    conn.close()
    assert sky_row is None


def test_clear_removes_summaries_and_keeps_candidates(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    spec = _spec(cache, sector=84, unit=5)
    time_values, flux = _curve()
    publish_star(spec, _row(tic=1), time_values, flux, processing=False)
    publish_star(spec, _row(tic=2, snr=1), time_values, flux, processing=False)
    upsert_unit(
        cache,
        {
            "assignment_id": 5, "unit_id": 5, "sector": 84, "project": "exoplanets",
            "tics": [1, 2], "n_tics": 2,
        },
        stage="done", results_sent=True, results_confirmed=True,
        receipt={"complete": True, "received": True, "n_expected": 2, "n_received": 2},
    )
    view = holdings_view(cache, configured_cap_gb=20, queue_max=3, min_free_gb=5)
    assert view["records"]["stars"] == 2
    assert view["records"]["candidates"] == 1
    assert view["records"]["limit_bytes"] > 0
    result = clear_unit(cache, 5)
    assert result["ok"] is True
    snap = snapshot(cache)
    tics = []
    import sqlite3
    conn = sqlite3.connect(cache / DB_NAME)
    tics = [row[0] for row in conn.execute("SELECT tic FROM stars ORDER BY tic")]
    conn.close()
    assert tics == [1]
    assert snap["records"]["candidates"] == 1


def test_retention_drops_old_curves_and_keeps_the_candidate(tmp_path):
    spec = _spec(tmp_path)
    time_values, flux = _curve()
    publish_star(spec, _row(tic=1), time_values, flux, processing=False)
    for tic in (2, 3, 4):
        publish_star(spec, _row(tic=tic, snr=1), time_values, flux, processing=False)
    from rhexas.worker.live import _connect
    conn = _connect(tmp_path / DB_NAME, write=True)
    try:
        retain(conn, budget=1)
        kept = {
            row[0]: bool(row[1])
            for row in conn.execute("SELECT tic, record_json IS NOT NULL FROM stars ORDER BY tic")
        }
    finally:
        conn.close()
    assert kept[1] is True
    assert kept[4] is True
    assert kept[2] is False
    assert kept[3] is False


def test_done_state_cycles_full_candidate_records(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    time_values, flux = _curve()
    publish_star(spec, _row(tic=1, ra=1.0, dec=1.0), time_values, flux, processing=False)
    publish_star(spec, _row(tic=2, ra=2.0, dec=2.0), time_values, flux, processing=False)
    publish_star(spec, _row(tic=3, ra=3.0, dec=3.0, snr=1), time_values, flux, processing=False)
    Store(tmp_path).begin_batch(
        {"project": "exoplanets", "sector": 107, "unit_id": 3, "assignment_id": 1, "n_tics": 3, "tics": [1, 2, 3]},
        "uploaded",
    )
    monkeypatch.setattr("rhexas.worker.live.time.time", lambda: 0)
    first = snapshot(tmp_path)["current_star"]
    monkeypatch.setattr("rhexas.worker.live.time.time", lambda: 8)
    second = snapshot(tmp_path)["current_star"]
    assert first["tic"] == 1 and first["fold"] and first["pgram"]
    assert second["tic"] == 2 and second["raw"]
    assert {first["tic"], second["tic"]} == {1, 2}


def test_missing_star_names_the_gap(tmp_path):
    httpd, port, thread = _serve(tmp_path)
    try:
        _ctype, body = _get(port, "/api/star?tic=404")
        payload = json.loads(body)
    finally:
        _stop(httpd, thread)
    assert payload["available"] is False
    assert payload["missing"] == ["record"]
    assert payload["pgram"] is None
    assert payload["raw"] is None
    assert payload["odd_even_sigma"] is None
    assert payload["centroid"] is None


def test_star_event_is_only_a_pointer(tmp_path):
    spec = _spec(tmp_path)
    time_values, flux = _curve()
    publish_star(spec, _row(), time_values, flux, processing=False)
    httpd, port, thread = _serve(tmp_path)
    try:
        _ctype, body = _get(port, "/events?once=1")
    finally:
        _stop(httpd, thread)
    text = body.decode("utf-8")
    assert "event: star" in text
    assert "event: lc" not in text
    pointer = None
    for block in text.split("\n\n"):
        if block.startswith("event: star"):
            pointer = json.loads(block.split("data: ", 1)[1])
    assert pointer["tic"] == 42
    assert pointer["version"] >= 1
    assert "pgram" not in pointer
    assert "raw" not in pointer
    assert "t" not in pointer


def test_picture_speed_does_not_touch_a_stored_star_or_the_batch(tmp_path):
    spec = _spec(tmp_path)
    time_values, flux = _curve()
    publish_star(spec, _row(), time_values, flux, processing=False)
    store = Store(tmp_path)
    store.begin_batch(
        {"project": "exoplanets", "sector": 107, "unit_id": 3, "assignment_id": 1, "n_tics": 10, "tics": [42]},
        "searching",
    )
    store.update_batch(stars_done=4, stars_total=10)
    store.publish_pace({"mode": "live", "procs": 4, "threads": 2, "priority": "low", "paused": False})
    httpd, port, thread = _serve(tmp_path)
    try:
        for name in ("slow", "normal", "realtime"):
            data = json.dumps({"mode": name}).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/mode", data=data, method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                body = json.loads(response.read())
            assert body["effort"] == "unchanged"
            assert body["requested"] == name
    finally:
        _stop(httpd, thread)
    snap = snapshot(tmp_path)
    assert snap["resources"]["procs"] == 4
    assert snap["resources"]["threads"] == 2
    assert snap["resources"]["paused"] is False
    assert snap["batch"]["stars_done"] == 4
    assert snap["batch"]["stage"] == "searching"
    assert snap["current_star"]["period"] == pytest.approx(2.5)
    assert snap["current_star"]["tic"] == 42


def _synthetic_curve(path: Path):
    period, duration, depth, t0 = 2.2, 0.08, 0.02, 0.5
    time_values = np.arange(0.0, 12.0, 2 / 1440.0)
    phase = ((time_values - t0 + 0.5 * period) % period) - 0.5 * period
    flux = np.ones_like(time_values)
    flux[np.abs(phase) < duration / 2] -= depth
    flux = flux + np.random.default_rng(0).normal(0, 3e-4, size=time_values.shape)
    err = np.full_like(flux, 3e-4)
    in_transit = np.abs(phase) < duration / 2
    col = np.where(in_transit, 5.15, 5.0) + np.random.default_rng(1).normal(0, 0.002, size=time_values.shape)
    row = np.where(in_transit, 6.08, 6.0) + np.random.default_rng(2).normal(0, 0.002, size=time_values.shape)
    table = Table({
        "TIME": time_values,
        "PDCSAP_FLUX": flux,
        "PDCSAP_FLUX_ERR": err,
        "SAP_FLUX": flux,
        "SAP_FLUX_ERR": err,
        "QUALITY": np.zeros(len(time_values), dtype=np.int32),
        "MOM_CENTR1": col,
        "MOM_CENTR2": row,
    })
    primary = fits.PrimaryHDU()
    primary.header["TELESCOP"] = "TESS"
    primary.header["CREATOR"] = "LightCurveExporterPipelineModule"
    primary.header["ORIGIN"] = "NASA/Ames"
    primary.header["OBJECT"] = "TIC 42"
    primary.header["TICID"] = 42
    primary.header["RA_OBJ"] = 201.25
    primary.header["DEC_OBJ"] = -15.5
    primary.header["TESSMAG"] = 9.1
    lightcurve = fits.BinTableHDU(table, name="LIGHTCURVE")
    lightcurve.header["RA_OBJ"] = 201.25
    lightcurve.header["DEC_OBJ"] = -15.5
    lightcurve.header["TESSMAG"] = 9.1
    aperture = fits.ImageHDU(np.array([[1, 2], [3, 6]], dtype=np.int32), name="APERTURE")
    fits.HDUList([primary, lightcurve, aperture]).writeto(path)


def test_pipeline_serves_a_real_star_after_the_light_curve_is_gone(tmp_path):
    """analyze() writes the record the page will fetch. Deleting the FITS does not erase it."""
    import rhexas.search as search_mod

    fits_path = tmp_path / "tess2026000000000-s0042-0000000000000042-0001-s_lc.fits"
    _synthetic_curve(fits_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    spec = _spec(cache, sector=42, unit=1)
    previous = search_mod._ACTIVE
    previous_live = search_mod._LIVE
    _pool_init(SearchConfig(n_periods=1200), spec)
    try:
        row = analyze(str(fits_path))
    finally:
        search_mod._ACTIVE = previous
        search_mod._LIVE = previous_live
    assert "error" not in row
    assert row["tic"] == 42
    fits_path.unlink()
    httpd, port, thread = _serve(cache)
    try:
        _ctype, body = _get(port, "/api/star?tic=42&sector=42&unit=1")
        _ctype, pixels = _get(port, "/api/pixels?tic=42")
    finally:
        _stop(httpd, thread)
    payload = json.loads(body)
    assert payload["available"] is True
    assert payload["summary"] is False
    assert payload["unverified"] is True
    assert payload["ra"] == pytest.approx(201.25)
    assert payload["dec"] == pytest.approx(-15.5)
    assert payload["aperture"] == [[0, 1], [1, 1]]
    assert payload["raw"][0] and len(payload["raw"][0]) == len(payload["raw"][1])
    assert payload["raw"] != [[0, 1], [1, 1]]
    assert payload["pgram"] and len(payload["pgram"][0]) >= 2
    peak = int(np.argmax(payload["pgram"][1]))
    assert payload["pgram"][0][peak] == pytest.approx(2.2, rel=0.05)
    assert payload["fold"] and len(payload["fold"][0]) >= 3
    assert payload["odd"] and payload["even"]
    assert payload["secondary"]
    assert payload["odd_even_sigma"] is not None
    assert payload["sec_sigma"] is not None
    assert payload["centroid"] and len(payload["centroid"]) == 3
    assert len(payload["centroid"][0]) == len(payload["centroid"][1]) == len(payload["centroid"][2])
    assert payload["centroid_sigma"] is not None and payload["centroid_sigma"] > 0
    assert "centroid" not in payload["missing"]
    assert "light_curve" not in payload["missing"]
    assert "periodogram" not in payload["missing"]
    pix = json.loads(pixels)
    assert pix["aperture"] == [[0, 1], [1, 1]]
    again = snapshot(cache)
    assert again["current_star"]["tic"] == 42
    assert again["current_star"]["aperture"] == [[0, 1], [1, 1]]
