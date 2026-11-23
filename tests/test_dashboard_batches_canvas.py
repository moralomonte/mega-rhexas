"""Batch view: fixed panel heights, hex grid, replay, and a short headless soak."""
import os
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from rhexas.worker.dashboard import bind_server, make_handler
from rhexas.worker.live import Store, publish_star

PANELS = {
    "panel-hex": 360,
    "panel-replay": 300,
    "panel-star": 300,
    "panel-sde": 220,
    "panel-period": 220,
    "panel-scatter": 220,
    "panel-timeline": 220,
}

_MEASURE = """
(ids) => {
  const out = {};
  for (const id of ids) {
    const node = document.getElementById(id);
    if (!node) { out[id] = null; continue; }
    const box = node.getBoundingClientRect();
    const canvas = node.querySelector("canvas");
    let n = 0;
    let bw = 0;
    let bh = 0;
    if (canvas) {
      bw = canvas.width;
      bh = canvas.height;
      const ctx = canvas.getContext("2d");
      const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
      const step = Math.max(1, Math.floor(data.length / 4 / 3000));
      for (let i = 0; i < data.length; i += 4 * step) {
        if (data[i + 3] > 16 && data[i] + data[i + 1] + data[i + 2] > 40) n++;
      }
    }
    out[id] = {h: box.height, w: box.width, n, bw, bh};
  }
  return out;
}
"""


def _serve(cache):
    import threading

    httpd, port = bind_server("127.0.0.1", 0, make_handler(cache, {"max_cache_gb": 20, "processed_limit_gb": 20}))
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return httpd, port, thread


def _stop(httpd, thread):
    httpd.dashboard_stop.set()
    httpd.shutdown()
    thread.join(timeout=3)
    httpd.server_close()


def _passing_row(tic, **extra):
    row = {
        "tic": tic,
        "file": "star.fits",
        "period": 2.5,
        "t0": 100.0,
        "duration": 0.1,
        "depth": 0.01,
        "snr": 12.0,
        "sde": 9.0,
        "n_transits": 4,
        "odd_even_sigma": 0.4,
        "sec_sigma": 0.2,
    }
    row.update(extra)
    return row


def _curve():
    import numpy as np

    time_values = np.linspace(100.0, 127.0, 360)
    return time_values, np.ones_like(time_values)


@pytest.fixture
def served(tmp_path):
    tics = list(range(1, 501))
    store = Store(tmp_path)
    store.begin_batch(
        {
            "project": "exoplanets",
            "sector": 107,
            "unit_id": 4,
            "assignment_id": 9,
            "n_tics": 500,
            "tics": tics,
        },
        "searching",
    )
    store.update_batch(stars_done=80, stars_total=500, per_s=2.5, eta_min=30)
    store.set_pid(os.getpid())
    spec = {"db": str(tmp_path / "dashboard.sqlite"), "project": "exoplanets", "sector": 107, "unit_id": 4}
    spec.update({
        "sde_min": 6, "snr_min": 7, "min_transits": 3, "max_depth": 0.05,
        "odd_even_max_sigma": 3, "secondary_max_sigma": 3, "max_duration_fraction": 0.12,
    })
    times, flux = _curve()
    for tic in range(1, 41):
        publish_star(spec, _passing_row(tic), times, flux, processing=False)
    httpd, port, thread = _serve(tmp_path)
    try:
        yield port
    finally:
        _stop(httpd, thread)


def test_batches_page_panels_and_soak(served):
    seconds = int(os.environ.get("RHEXAS_BATCH_SECONDS", "125"))
    url = f"http://127.0.0.1:{served}/batches"
    shots = Path("/opt/cursor/artifacts")
    shots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            errors = []
            page.on("pageerror", lambda err: errors.append(f"pageerror: {err}"))
            page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_function(
                "() => document.getElementById('progress-text').textContent.includes('stars')",
                timeout=15000,
            )
            page.wait_for_function(
                "() => (document.getElementById('sector-pick').options.length||0) > 0",
                timeout=10000,
            )
            first = page.evaluate(_MEASURE, list(PANELS))
            for name, height in PANELS.items():
                got = first[name]
                assert got is not None, name
                assert abs(got["h"] - height) <= 2, got
            assert first["panel-hex"]["n"] > 20
            heap0 = page.evaluate("() => (performance.memory && performance.memory.usedJSHeapSize) || 0")
            page.screenshot(path=str(shots / "batch-view-live.png"), full_page=True)
            page.click("#play-btn")
            page.wait_for_timeout(800)
            page.screenshot(path=str(shots / "batch-view-replay.png"), full_page=True)
            page.wait_for_timeout(seconds * 1000)
            later = page.evaluate(_MEASURE, list(PANELS))
            for name, height in PANELS.items():
                got = later[name]
                assert abs(got["h"] - height) <= 2, f"{name} drifted to {got['h']}"
                assert got["bw"] <= 5000 and got["bh"] <= 1200, got
            heap1 = page.evaluate("() => (performance.memory && performance.memory.usedJSHeapSize) || 0")
            if heap0 and heap1:
                assert heap1 < heap0 * 1.8 + 8_000_000, (heap0, heap1)
            assert not errors, errors
        finally:
            browser.close()


def test_batches_page_is_not_a_placeholder():
    text = (Path(__file__).resolve().parents[1] / "src" / "rhexas" / "worker" / "static" / "batches.html").read_text(
        encoding="utf-8",
    )
    assert "later phase" not in text.lower()
    assert 'id="hex"' in text
    assert "/api/batches/view" in text
    assert "requestAnimationFrame" not in text