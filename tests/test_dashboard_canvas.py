"""The worker dashboard shell keeps fixed panels and a quiet console."""
import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from rhexas.worker.dashboard import bind_server, make_handler
from rhexas.worker.live import Store

PANELS = {
    "panel-cpu": 220,
    "panel-net": 220,
    "panel-state": 340,
    "panel-disk": 280,
}

_MEASURE = """
(ids) => {
  const out = {};
  for (const id of ids) {
    const node = document.getElementById(id);
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
      const step = Math.max(1, Math.floor(data.length / 4 / 4000));
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


@pytest.fixture
def served(tmp_path):
    store = Store(tmp_path)
    store.begin_batch(
        {"project": "exoplanets", "sector": 107, "unit_id": 4, "assignment_id": 9, "n_tics": 500, "tics": [1]},
        "searching",
    )
    store.update_batch(stars_done=120, stars_total=500, per_s=2.5, eta_min=3)
    store.set_pid(os.getpid())
    httpd, port, thread = _serve(tmp_path)
    try:
        yield port
    finally:
        _stop(httpd, thread)


def test_panels_hold_their_size(served):
    seconds = int(os.environ.get("RHEXAS_DASH_SECONDS", "125"))
    url = f"http://127.0.0.1:{served}/"
    shots = Path("/opt/cursor/artifacts")
    shots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda err: errors.append(f"pageerror: {err}"))
            page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_function("() => document.getElementById('sector').textContent === '107'", timeout=10000)
            first = page.evaluate(_MEASURE, list(PANELS))
            for name, height in PANELS.items():
                got = first[name]
                assert abs(got["h"] - height) <= 2, got
                assert got["w"] > 200, got
            assert first["panel-cpu"]["n"] > 8
            assert first["panel-net"]["n"] > 8
            page.screenshot(path=str(shots / "dashboard-shell.png"), full_page=True)
            page.wait_for_timeout(seconds * 1000)
            later = page.evaluate(_MEASURE, list(PANELS))
            for name, height in PANELS.items():
                got = later[name]
                assert abs(got["h"] - height) <= 2, f"{name} drifted to {got['h']}"
                assert abs(got["w"] - first[name]["w"]) <= 2, name
                assert got["bw"] <= 4000 and got["bh"] <= 800, got
            page.screenshot(path=str(shots / "dashboard-shell-later.png"), full_page=True)
            assert not errors, errors
            page.click("#sectors")
            page.wait_for_function("() => document.body.innerText.includes('Download ahead')", timeout=10000)
            page.screenshot(path=str(shots / "dashboard-sectors.png"), full_page=True)
            assert not errors, errors
        finally:
            browser.close()


def test_version_mismatch_reloads_the_page(served):
    url = f"http://127.0.0.1:{served}/"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.route(
                "**/api/status",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    headers={"Cache-Control": "no-store"},
                    body=json.dumps({"app_version": "9.9.9", "resources": {"pause": {"state": "running"}}, "version": 1}),
                ),
            )
            navigated = []
            page.on("framenavigated", lambda frame: navigated.append(frame.url) if frame == page.main_frame else None)
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            assert sum(1 for item in navigated if item.startswith(url)) >= 2
        finally:
            browser.close()
