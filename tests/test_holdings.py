"""Queue admission, the free-disk floor, and clear-after-acknowledgement."""
import json

from rhexas.config import default_config
from rhexas.worker.dashboard import bind_server, make_handler
from rhexas.worker.holdings import (
    clear_finished,
    clear_unit,
    holdings_view,
    queue_block,
    upsert_unit,
    write_cap_gb,
)
from rhexas.worker.loop import prefetch_block, resolve_worker

import threading
import urllib.error
import urllib.request


def _unit(assignment_id, stage, *, confirmed=False, expected=2, received=2):
    return {
        "assignment_id": assignment_id,
        "unit_id": assignment_id,
        "sector": 84,
        "project": "exoplanets",
        "n_tics": expected,
        "tics": list(range(1, expected + 1)),
        "stage": stage,
        "results_sent": stage == "done",
        "results_confirmed": confirmed,
        "receipt": {
            "complete": confirmed,
            "received": confirmed,
            "n_expected": expected,
            "n_received": received if confirmed else 0,
        },
    }


def test_queue_block_stops_on_count_cap_and_the_ten_gigabyte_floor():
    floor = 10 * 10**9
    assert queue_block(
        queued=3, queue_max=3, used_bytes=0, extra_bytes=100,
        cap_bytes=20 * 10**9, free_bytes=50 * 10**9, floor_bytes=floor,
    ) == "queue"
    assert queue_block(
        queued=0, queue_max=3, used_bytes=19 * 10**9, extra_bytes=2 * 10**9,
        cap_bytes=20 * 10**9, free_bytes=50 * 10**9, floor_bytes=floor,
    ) == "cache cap"
    assert queue_block(
        queued=0, queue_max=3, used_bytes=0, extra_bytes=2 * 10**9,
        cap_bytes=20 * 10**9, free_bytes=11 * 10**9, floor_bytes=floor,
    ) == "free space"
    assert queue_block(
        queued=1, queue_max=3, used_bytes=1 * 10**9, extra_bytes=1 * 10**9,
        cap_bytes=20 * 10**9, free_bytes=40 * 10**9, floor_bytes=floor,
    ) is None


def test_prefetch_block_uses_the_floor_even_when_min_free_is_lower(tmp_path, monkeypatch):
    cfg = resolve_worker(default_config(tmp_path), min_free_gb=0, max_cache_gb=20, environ={})

    class Usage:
        def __init__(self, free):
            self.free = free

    monkeypatch.setattr("rhexas.worker.loop.shutil.disk_usage", lambda path: Usage(11 * 10**9))
    monkeypatch.setattr(
        "rhexas.worker.loop.estimate_batch_bytes", lambda _data, _n: 2 * 10**9,
    )
    assert prefetch_block(tmp_path / "cache", cfg, 500) == "free space"
    monkeypatch.setattr("rhexas.worker.loop.shutil.disk_usage", lambda path: Usage(40 * 10**9))
    assert prefetch_block(tmp_path / "cache", cfg, 500) is None
    write_cap_gb(tmp_path / "cache", 1)
    assert prefetch_block(tmp_path / "cache", cfg, 500) == "cache cap"


def test_clear_requires_a_complete_receipt_and_skips_the_scan(tmp_path):
    cache = tmp_path / "cache"
    data = cache / "data" / "s0084"
    data.mkdir(parents=True)
    fits = data / "tess2026-s0084-0000000000000001-0001-s_lc.fits"
    fits.write_bytes(b"FITS")
    scanning = _unit(1, "searching")
    finished = _unit(2, "done", confirmed=False)
    ready = _unit(3, "done", confirmed=True)
    for row in (scanning, finished, ready):
        upsert_unit(cache, row, **{k: v for k, v in row.items() if k != "assignment_id"})
    (cache / "state.json").write_text(json.dumps({**scanning, "stage": "searching"}), encoding="utf-8")
    (cache / "results" / "a3").mkdir(parents=True)
    (cache / "results" / "a3" / "s0084").mkdir()
    refused = clear_unit(cache, 1)
    assert refused["ok"] is False and refused["error"] == "scanning"
    assert fits.is_file()
    waiting = clear_unit(cache, 2)
    assert waiting["ok"] is False and waiting["error"] == "not confirmed"
    assert clear_unit(cache, 3)["ok"] is True
    assert not (cache / "results" / "a3").exists()
    outcome = clear_finished(cache)
    assert 2 not in outcome["cleared"]
    assert 1 not in outcome["cleared"]
    view = holdings_view(cache, configured_cap_gb=20, queue_max=3, min_free_gb=5)
    assert view["floor_gb"] == 10
    assert view["scanning_id"] == 1


def test_dashboard_clear_is_enforced_on_the_server(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    row = _unit(9, "done", confirmed=False)
    upsert_unit(cache, row, **{k: v for k, v in row.items() if k != "assignment_id"})
    httpd, port = bind_server("127.0.0.1", 0, make_handler(cache, {"max_cache_gb": 20, "queue_max": 3}))
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/sectors", timeout=5)
        assert page.status == 200
        assert b"Show previous sectors" in page.read()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/holdings/clear",
            data=json.dumps({"assignment_id": 9}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            raised = False
        except urllib.error.HTTPError as exc:
            raised = exc.code == 409
            assert b"not confirmed" in exc.read()
        assert raised
        cap = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/holdings/cap",
            data=json.dumps({"max_cache_gb": 12}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        saved = json.loads(urllib.request.urlopen(cap, timeout=5).read())
        assert saved["max_cache_gb"] == 12
    finally:
        httpd.dashboard_stop.set()
        httpd.shutdown()
        thread.join(timeout=3)
        httpd.server_close()
