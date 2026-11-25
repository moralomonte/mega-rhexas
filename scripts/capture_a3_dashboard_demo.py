#!/usr/bin/env python3
"""Capture a PR screenshot of the A3 dashboard with realistic mid-run sample data."""
from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

from rhexas import __version__
from rhexas.worker.dashboard import bind_server, make_handler

DEMO = {
    "version": 1,
    "app_version": __version__,
    "label": "candidates, unverified",
    "batch": {
        "project": "exoplanets",
        "sector": 83,
        "unit_id": 630,
        "stars_done": 210,
        "stars_total": 500,
        "per_s": 48.0 / 3600.0,
        "eta_min": 360,
        "stage": "searching",
    },
    "next": {
        "active": True,
        "stage": "downloading",
        "stars_done": 140,
        "stars_total": 500,
        "sector": 83,
        "unit_id": 631,
    },
    "display": {"tic": 384120915},
    "lifetime": {
        "stars_searched": 14280,
        "batches_verified": 26,
        "batches_submitted": 28,
        "contributions_pending": 2,
    },
    "connection": {
        "ok": True,
        "kind": "connected",
        "url": "https://api.megarhexas.com/v1",
        "detail": "connected",
    },
    "candidate_stats": {"rows": 1, "new": 1, "known": 0},
    "candidates": [],
    "resources": {
        "cpu_percent": 37.0,
        "net_recv_bps": 1.24e6,
        "net_sent_bps": 18400.0,
        "processed": {"bytes": int(6.42e9), "limit_gb": 20.0, "limit_bytes": int(20e9)},
        "cache_bytes": int(4.1e9),
        "max_cache_gb": 20.0,
        "pause": {"state": "running", "label": "Running", "on": True},
        "pid": 4821,
        "procs": 4,
        "threads": 1,
        "priority": "below_normal",
        "mode": "live",
        "worker_alive": True,
    },
}


def main() -> None:
    import tempfile

    cache = Path(tempfile.mkdtemp(prefix="rhexas-a3-shot-"))
    fallback = {"max_cache_gb": 20, "processed_limit_gb": 20}
    httpd, port = bind_server("127.0.0.1", 0, make_handler(cache, fallback))
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/"
    out = Path(os.environ.get("RHEXAS_DASH_SHOT", "/opt/cursor/artifacts/dashboard-a3-shell.png"))
    out.parent.mkdir(parents=True, exist_ok=True)
    mission = {"ok": True, "percent_units": 3.8, "units_verified": 126, "units_total": 3312, "scope_note": "Enrolled sectors only."}
    catalog = {
        "sectors": [
            {
                "sector": 83,
                "units": [{"unit": n, "finished_at": 1.0 if n <= 18 else None} for n in range(1, 85)],
            }
        ]
    }
    history = [
        {"cpu": 28 + 12 * math.sin(i / 7), "dn": 1.02e6 + 0.35e6 * math.sin(i / 5), "up": 12000 + 8000 * math.cos(i / 4)}
        for i in range(72)
    ]
    tick = {"i": 0}

    def status(route):
        i = min(tick["i"], len(history) - 1)
        tick["i"] += 1
        row = history[i]
        payload = json.loads(json.dumps(DEMO))
        payload["resources"]["cpu_percent"] = round(row["cpu"], 1)
        payload["resources"]["net_recv_bps"] = row["dn"]
        payload["resources"]["net_sent_bps"] = row["up"]
        route.fulfill(
            status=200,
            content_type="application/json",
            headers={"Cache-Control": "no-store"},
            body=json.dumps(payload),
        )

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.route("**/api/status", status)
            page.route(
                "**/api/mission/progress",
                lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(mission)),
            )
            page.route(
                "**/api/batches/catalog",
                lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(catalog)),
            )
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_function("() => document.getElementById('sector').textContent === '83'", timeout=10000)
            page.evaluate("clearInterval(window.__pollTimer)")
            for i in range(72):
                page.evaluate("poll()")
                if i % 8 == 0:
                    page.wait_for_timeout(20)
            page.screenshot(path=str(out), full_page=True)
            browser.close()
        print(out)
    finally:
        httpd.dashboard_stop.set()
        httpd.shutdown()
        thread.join(timeout=3)
        httpd.server_close()


if __name__ == "__main__":
    main()