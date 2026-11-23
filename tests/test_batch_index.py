"""Local batch index and dashboard batch APIs."""
import json

import pytest

from rhexas.worker.batch_index import UNIT_SIZE, catalog, unit_view
from rhexas.worker.live import Store


def test_register_unit_and_progress_in_view(tmp_path):
    store = Store(tmp_path)
    tics = list(range(1, 11))
    store.begin_batch(
        {
            "project": "exoplanets",
            "sector": 107,
            "unit_id": 2,
            "assignment_id": 5,
            "n_tics": len(tics),
            "tics": tics,
        },
        "searching",
    )
    store.update_batch(stars_done=4, stars_total=10, progress_kind="stars")
    from rhexas.worker.live import snapshot
    snap = snapshot(tmp_path)
    view = unit_view(tmp_path, 107, 2, snap=snap)
    assert view["n_tics"] == 10
    assert view["unit_size"] == UNIT_SIZE
    assert view["live"] is True
    assert view["progress"]["stars_total"] == 10
    assert view["progress"]["stars_done"] >= 4
    assert view["progress"]["finished"] is False

    store.update_batch(stage="uploaded", active=False)
    snap = snapshot(tmp_path)
    done = unit_view(tmp_path, 107, 2, snap=snap)
    assert done["live"] is False
    assert done["progress"]["stars_done"] == 10
    assert done["progress"]["percent"] == 100.0
    assert done["progress"]["finished"] is True


def test_catalog_lists_live_default(tmp_path):
    store = Store(tmp_path)
    store.begin_batch(
        {"project": "exoplanets", "sector": 96, "unit_id": 1, "assignment_id": 1, "n_tics": 3, "tics": [1, 2, 3]},
        "claimed",
    )
    from rhexas.worker.live import snapshot
    snap = snapshot(tmp_path)
    payload = catalog(tmp_path, snap)
    assert payload["default"] == {"sector": 96, "unit": 1}
    assert any(s["sector"] == 96 for s in payload["sectors"])


def test_batches_view_api(tmp_path):
    import threading
    import urllib.request

    from rhexas.worker.dashboard import bind_server, make_handler

    store = Store(tmp_path)
    store.begin_batch(
        {"project": "exoplanets", "sector": 107, "unit_id": 4, "n_tics": 500, "tics": list(range(1, 501)), "assignment_id": 9},
        "searching",
    )
    store.update_batch(stars_done=312, stars_total=500)
    httpd, port = bind_server("127.0.0.1", 0, make_handler(tmp_path, {}))
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/batches/view?sector=107&unit=4", timeout=5) as res:
            body = json.loads(res.read().decode())
        assert body["progress"]["stars_done"] >= 312
        assert body["progress"]["stars_total"] == 500
        assert body["progress"]["percent"] == pytest.approx(62.4, abs=0.2)
    finally:
        httpd.dashboard_stop.set()
        httpd.shutdown()
        thread.join(timeout=3)
        httpd.server_close()