"""The worker dashboard keeps every panel drawn, at a fixed size, in every picture speed.

The curves come from the real search on short SPOC-style light curves. Picture
speed only changes how the page draws. It does not post a new processing mode.
"""
import json
import os
import threading
import urllib.request
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astropy.table import Table
from playwright.sync_api import sync_playwright

from rhexas.config import SearchConfig
from rhexas.search import _pool_init, analyze
from rhexas.worker.dashboard import bind_server, make_handler
from rhexas.worker.live import DB_NAME, Store

PANELS = {
    "cpuc": 70,
    "sky": 330,
    "pixc": 200,
    "cutc": 200,
    "lcc": 150,
    "pgc": 110,
    "fdc": 282,
    "hist": 250,
    "vc_oe": 230,
    "vc_sec": 230,
    "vc_cen": 230,
    "wcpu": 70,
}

_MEASURE = """
(ids) => {
  const out = {};
  for (const id of ids) {
    const cv = document.getElementById(id);
    const box = cv.parentElement.getBoundingClientRect();
    let n = 0;
    const ctx = cv.getContext('2d');
    const data = ctx.getImageData(0, 0, cv.width, cv.height).data;
    const step = Math.max(1, Math.floor(data.length / 4 / 5000));
    for (let i = 0; i < data.length; i += 4 * step) {
      if (data[i + 3] > 16 && data[i] + data[i + 1] + data[i + 2] > 60) n++;
    }
    out[id] = {h: box.height, w: box.width, n, bw: cv.width, bh: cv.height};
  }
  return out;
}
"""


def _jpeg(path: Path) -> None:
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (96, 96), (18, 20, 36))
    draw = ImageDraw.Draw(image)
    for x, y in ((20, 30), (50, 18), (70, 55), (34, 68), (58, 40)):
        draw.ellipse((x, y, x + 4, y + 4), fill=(235, 235, 245))
    image.save(path, "JPEG", quality=85)


def _fits(path: Path, tic: int, ra: float, dec: float, rows: int, cols: int) -> None:
    period, duration, depth, t0 = 2.2, 0.08, 0.02, 0.5
    time_values = np.arange(0.0, 12.0, 2 / 1440.0)
    phase = ((time_values - t0 + 0.5 * period) % period) - 0.5 * period
    flux = np.ones_like(time_values)
    flux[np.abs(phase) < duration / 2] -= depth
    rng = np.random.default_rng(tic)
    flux = flux + rng.normal(0, 3e-4, size=time_values.shape)
    err = np.full_like(flux, 3e-4)
    in_transit = np.abs(phase) < duration / 2
    col = np.where(in_transit, 5.15, 5.0) + rng.normal(0, 0.002, size=time_values.shape)
    row = np.where(in_transit, 6.08, 6.0) + rng.normal(0, 0.002, size=time_values.shape)
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
    primary.header["OBJECT"] = f"TIC {tic}"
    primary.header["TICID"] = tic
    primary.header["RA_OBJ"] = ra
    primary.header["DEC_OBJ"] = dec
    primary.header["TESSMAG"] = 9.1
    lightcurve = fits.BinTableHDU(table, name="LIGHTCURVE")
    lightcurve.header["RA_OBJ"] = ra
    lightcurve.header["DEC_OBJ"] = dec
    lightcurve.header["TESSMAG"] = 9.1
    mask = np.zeros((rows, cols), dtype=np.int32)
    mask[rows // 2 - 1:rows // 2 + 2, cols // 2 - 1:cols // 2 + 2] = 2
    aperture = fits.ImageHDU(mask, name="APERTURE")
    fits.HDUList([primary, lightcurve, aperture]).writeto(path)


def _search(cache: Path) -> None:
    import rhexas.search as search_mod
    stars = (
        (26474636, 291.59, 50.99, 11, 11),
        (27454154, 295.33, 51.14, 13, 11),
        (28444784, 288.10, 49.40, 11, 11),
    )
    spec = {
        "db": str(cache / DB_NAME), "cache": str(cache), "project": "exoplanets",
        "sector": 83, "unit_id": 625,
    }
    previous = search_mod._ACTIVE
    previous_live = search_mod._LIVE
    _pool_init(SearchConfig(n_periods=800), spec)
    try:
        for tic, ra, dec, rows, cols in stars:
            path = cache / f"tess2026000000000-s0083-{tic:016d}-0001-s_lc.fits"
            _fits(path, tic, ra, dec, rows, cols)
            row = analyze(str(path))
            assert "error" not in row, row.get("error")
            assert row["tic"] == tic
    finally:
        search_mod._ACTIVE = previous
        search_mod._LIVE = previous_live
    import sqlite3
    conn = sqlite3.connect(cache / DB_NAME)
    try:
        conn.execute("UPDATE stars SET candidate=1")
        conn.commit()
        shapes = {
            int(tic): (len(json.loads(raw)["aperture"]), len(json.loads(raw)["aperture"][0]))
            for tic, raw in conn.execute("SELECT tic, record_json FROM stars")
        }
    finally:
        conn.close()
    assert shapes[26474636] == (11, 11)
    assert shapes[27454154] == (13, 11)
    store = Store(cache)
    store.begin_batch(
        {"project": "exoplanets", "sector": 83, "unit_id": 625, "assignment_id": 9, "n_tics": 3, "tics": [s[0] for s in stars]},
        "uploaded",
    )
    store.update_batch(stars_done=3, stars_total=3, per_s=0.4, eta_min=1)
    store.publish_pace({"mode": "live", "procs": 2, "threads": 2, "priority": "low", "paused": False})
    store.set_pid(os.getpid())
    images = cache / "images"
    images.mkdir(parents=True, exist_ok=True)
    for tic, *_rest in stars:
        _jpeg(images / f"dss-{tic}.jpg")


def _serve(cache: Path):
    httpd, port = bind_server("127.0.0.1", 0, make_handler(cache, {"max_cache_gb": 20}))
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return httpd, port, thread


def _stop(httpd, thread):
    httpd.dashboard_stop.set()
    httpd.shutdown()
    thread.join(timeout=3)
    httpd.server_close()


def _sample(page):
    return page.evaluate(_MEASURE, list(PANELS))


def _assert_sizes(sample, where):
    for name, height in PANELS.items():
        got = sample[name]
        assert abs(got["h"] - height) <= 2, f"{where} {name} height {got['h']} expected {height}"
        assert got["w"] > 80, f"{where} {name} width collapsed to {got['w']}"
        assert got["n"] > 8, f"{where} {name} is blank ({got['n']} lit pixels)"
        assert got["bh"] <= height * 3, f"{where} {name} backing store grew to {got['bh']}"


def _hold(page, seconds, label):
    tics = set()
    deadline = page.evaluate("() => performance.now()") + seconds * 1000
    step = 0
    while page.evaluate("() => performance.now()") < deadline:
        if step == 2:
            page.set_viewport_size({"width": 1180, "height": 820})
        elif step == 4:
            page.set_viewport_size({"width": 1500, "height": 980})
        page.wait_for_timeout(4000)
        sample = _sample(page)
        _assert_sizes(sample, f"{label} t={step * 4}s")
        tics.add(page.locator("#lc_tic").inner_text())
        label_text = page.locator("#cut_lbl").inner_text()
        assert not label_text.startswith("Loading"), f"{label} survey label stuck: {label_text}"
        assert "Pipeline aperture" in page.locator("#pix_note").inner_text()
        step += 1
    return tics


def _drive(port, seconds):
    url = f"http://127.0.0.1:{port}/"
    modes = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            for dpr in (1.25, 1.5):
                context = browser.new_context(
                    viewport={"width": 1440, "height": 1000},
                    device_scale_factor=dpr,
                )
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda err: errors.append(f"pageerror: {err}"))
                page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
                page.on("request", lambda req: modes.append(req.url) if "/api/mode" in req.url else None)
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_function(
                    "() => document.getElementById('stage').textContent && !/connect/i.test(document.getElementById('stage').textContent)",
                    timeout=20000,
                )
                page.wait_for_function(
                    "() => document.getElementById('cut_lbl').textContent.startsWith('Sky survey (DSS2')",
                    timeout=15000,
                )
                page.wait_for_function(
                    "() => (document.getElementById('bls_scan').textContent || '').includes('best P=')",
                    timeout=15000,
                )
                seen = set()
                for name in ("slow", "normal", "realtime"):
                    page.click(f"#speed button[data-m='{name}']")
                    page.wait_for_function(
                        "(name) => document.querySelector(`#speed button[data-m='${name}']`).classList.contains('on')",
                        arg=name,
                    )
                    seen |= _hold(page, seconds, f"dpr {dpr} {name}")
                assert len(seen) >= 2, f"dpr {dpr} did not change stars: {seen}"
                if dpr == 1.25:
                    page.screenshot(path="/opt/cursor/artifacts/dashboard-after.png", full_page=True)
                    page.locator("#cpuc").screenshot(path="/opt/cursor/artifacts/cpu-after.png")
                context.close()
                assert not errors, errors
        finally:
            browser.close()
    assert modes == []


@pytest.fixture(scope="module")
def searched(tmp_path_factory):
    cache = tmp_path_factory.mktemp("dashboard-live")
    _search(cache)
    httpd, port, thread = _serve(cache)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/sky?scope=sector&sector=83", timeout=5) as response:
            sky = json.loads(response.read())
        assert sky["count"] == 3
        yield port
    finally:
        _stop(httpd, thread)


def test_version_mismatch_reloads_the_page(searched):
    url = f"http://127.0.0.1:{searched}/"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.route(
                "**/api/status",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    headers={"Cache-Control": "no-store"},
                    body=json.dumps({"app_version": "9.9.9", "resources": {}, "version": 1}),
                ),
            )
            navigated = []
            page.on("framenavigated", lambda frame: navigated.append(frame.url) if frame == page.main_frame else None)
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_function("() => performance.getEntriesByType('navigation').length >= 1", timeout=10000)
            page.wait_for_timeout(2500)
            assert sum(1 for item in navigated if item.startswith(url)) >= 2
        finally:
            browser.close()


def test_panels_hold_size_across_speeds_and_pixel_ratios(searched):
    seconds = int(os.environ.get("RHEXAS_CANVAS_SECONDS", "60"))
    assert seconds >= 60 or os.environ.get("RHEXAS_CANVAS_SECONDS")
    _drive(searched, seconds)
