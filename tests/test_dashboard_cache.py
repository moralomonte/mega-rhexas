"""Dashboard responses must not be cached, and the page must notice a new build."""
import json
import threading
import urllib.request

from rhexas import __version__
from rhexas.worker.dashboard import bind_server, browser_url, make_handler
from rhexas.worker.live import DB_NAME, Store, publish_star, snapshot


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
        return response, response.read()


def test_dashboard_pages_and_apis_are_not_stored(tmp_path):
    httpd, port, thread = _serve(tmp_path)
    try:
        for path in ("/", "/sectors", "/api/status", "/api/star?tic=1", "/api/sky?scope=all", "/img/triskelion.svg", "/events?once=1"):
            response, body = _get(port, path)
            cache = response.headers.get("Cache-Control", "")
            assert "no-store" in cache.lower(), path
            assert body
        response, page = _get(port, "/")
        assert b"__APP_VERSION__" not in page
        assert __version__.encode() in page
        assert f"/img/triskelion.svg?v={__version__}".encode() in page
        assert f'content="{__version__}"'.encode() in page
        assert b"PAGE_VERSION" in page
        response, sectors = _get(port, "/sectors")
        assert b"__APP_VERSION__" not in sectors
        assert f"/?v={__version__}".encode() in sectors
        _response, status_body = _get(port, "/api/status")
        status = json.loads(status_body)
        assert status["app_version"] == __version__
        assert status["version"] == 1
    finally:
        _stop(httpd, thread)
    assert browser_url("127.0.0.1", 8080).endswith(f"/?v={__version__}")


def test_status_points_at_the_sky_endpoint_for_searched_stars(tmp_path):
    """The map is /api/sky keyed by sky_rev. The status blob does not duplicate it."""
    spec = {
        "db": str(tmp_path / DB_NAME), "project": "exoplanets", "sector": 83, "unit_id": 9,
        "sde_min": 6, "snr_min": 7, "min_transits": 3, "max_depth": 0.05,
        "odd_even_max_sigma": 3, "secondary_max_sigma": 3, "max_duration_fraction": 0.12,
    }
    import numpy as np
    time_values = np.linspace(100.0, 127.0, 360)
    flux = np.ones_like(time_values)
    aperture = [[1 if i == j else 0 for i in range(11)] for j in range(13)]
    for index, tic in enumerate((10, 11, 12)):
        publish_star(
            spec,
            {
                "tic": tic, "file": "star.fits", "period": 2.5, "t0": 100.0, "duration": 0.1,
                "depth": 0.01, "snr": 4.0, "sde": 3.0, "n_transits": 4,
                "ra": 291.0 + index, "dec": 50.0 - index * 0.4, "tmag": 9.0,
                "_pgram_p": [1.0, 2.5, 4.0], "_pgram_w": [0.2, 3.0, 0.4],
                "_aperture": aperture,
            },
            time_values, flux, processing=False,
        )
    Store(tmp_path).begin_batch(
        {"project": "exoplanets", "sector": 83, "unit_id": 9, "assignment_id": 1, "n_tics": 3},
        "searching",
    )
    snap = snapshot(tmp_path)
    assert snap["app_version"] == __version__
    assert snap["sky_rev"] not in ("", "0:0")
    assert snap["batch"]["sector"] == 83
    httpd, port, thread = _serve(tmp_path)
    try:
        _response, body = _get(port, "/api/sky?scope=sector&sector=83")
        sky = json.loads(body)
        assert sky["count"] == 3
        assert {point[0] for point in sky["points"]} == {10, 11, 12}
        _response, pixels = _get(port, "/api/pixels?tic=10")
        mask = json.loads(pixels)["aperture"]
        assert len(mask) == 13
        assert {len(row) for row in mask} == {11}
    finally:
        _stop(httpd, thread)
