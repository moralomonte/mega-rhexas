"""Phase 6: local review queue, coordinator local-check API, validation."""
from __future__ import annotations

import json
import time

import numpy as np
import pytest

from rhexas.coord.httpapi import App
from rhexas.coord.local_review import LocalReviewError, validate_local_check
from rhexas.worker.review.gate import search_busy
from rhexas.worker.review.runner import ReviewRunner, stop_all
from rhexas.worker.vetting.checks import light_checks, odd_even_check

from test_coord import _cand, _star, _store
from test_science_tools import _science


@pytest.fixture(autouse=True)
def _stop_review_threads():
    yield
    stop_all()


def test_local_check_validation_rejects_oversized_checks():
    checks = [{"name": f"c{i}", "title": "t", "status": "pass", "reason": "ok", "numbers": {}} for i in range(60)]
    with pytest.raises(LocalReviewError):
        validate_local_check({"tic": 1, "check_key": "run-1", "checks": checks}, tic=1)


def test_local_check_validation_accepts_plot():
    phase = [i / 50 for i in range(50)]
    flux = [1.0 - 0.01 * (0.5 - abs(p - 0.5)) for p in phase]
    body = validate_local_check({
        "tic": 42,
        "check_key": "desktop-1",
        "checks": [{"name": "odd_even", "title": "Odd/even", "status": "pass", "reason": "ok", "numbers": {}}],
        "plot": {"phase": phase, "flux": flux},
    }, tic=42)
    assert body["plot"]["phase"]
    assert len(body["plot"]["phase"]) == len(body["plot"]["flux"])


def test_science_api_allows_worker_token(tmp_path):
    store = _store(tmp_path)
    try:
        store.enroll_tics(14, [7001], unit_size=500)
        worker_token = store.create_worker("steve")
        app = App(store)
        denied = _science(app)
        assert denied.status == 401
        ok = _science(app, {"Authorization": f"Bearer {worker_token}"})
        assert ok.status == 200
    finally:
        store.close()


def test_local_check_post_auth_and_idempotent(tmp_path):
    store = _store(tmp_path)
    try:
        store.enroll_tics(14, [9001], unit_size=500)
        wid_token = store.create_worker("w")
        wid = store.authenticate(wid_token)["id"]
        assignment = store.claim(wid)
        store.submit(wid, assignment["assignment_id"], [_star(9001)], [_cand(9001)])
        app = App(store)
        payload = {
            "tic": 9001,
            "check_key": "run-a",
            "checks": [{"name": "simbad", "title": "SIMBAD", "status": "pass", "reason": "clear", "numbers": {}}],
            "plot": {"phase": [0, 0.25, 0.5, 0.75, 1.0], "flux": [1, 0.995, 0.99, 0.995, 1]},
        }
        headers = {"Authorization": f"Bearer {wid_token}", "Content-Type": "application/json"}
        res = app.handle("POST", "/api/v1/candidates/local-check", "tic=9001", headers, json.dumps(payload).encode())
        assert res.status == 200
        again = app.handle("POST", "/api/v1/candidates/local-check", "tic=9001", headers, json.dumps(payload).encode())
        assert again.status == 200
        row = store.conn.execute(
            "SELECT COUNT(*) AS c FROM candidate_local_checks WHERE tic=9001",
        ).fetchone()
        assert int(row["c"]) == 1
        fold = store.local_check_fold_for_tic(9001)
        assert fold is not None
    finally:
        store.close()


def test_odd_even_math_on_synthetic_curve():
    period, t0, dur = 2.5, 100.0, 0.08
    times = np.linspace(t0 - 0.2, t0 + period * 7, 500)
    y = np.ones(len(times))
    for i, t in enumerate(times):
        n = int(round((t - t0) / period))
        phase = (t - (t0 + n * period))
        if phase > period * 0.5:
            phase -= period
        if abs(phase) < dur / 2:
            y[i] = 0.99 if n % 2 == 0 else 0.994
    checks = light_checks(times.tolist(), y.tolist(), period=period, t0=t0, duration=dur)
    odd = next(c for c in checks if c["name"] == "odd_even")
    assert odd["status"] in {"warn", "fail", "pass"}
    assert odd_even_check(times.tolist(), y.tolist(), period, t0, dur)["status"] in {"warn", "fail", "pass"}


def test_review_runner_defers_when_search_busy(tmp_path):
    from rhexas.worker.live import DB_NAME, Store

    cache = tmp_path / "cache"
    cache.mkdir()
    live = Store(cache)
    live.begin_batch({"sector": 14, "unit_id": 1, "assignment_id": 1, "n_tics": 2, "tics": [1, 2]}, stage="searching")
    busy, _ = search_busy(cache)
    assert busy is True
    runner = ReviewRunner(cache, type("S", (), {"enabled": False, "radius_arcsec": 15.0, "timeout_s": 5.0, "cache_max_mb": 1.0})())
    assert runner.run_once() is False


def test_review_queue_serial(tmp_path):
    from rhexas.config import VettingConfig
    from rhexas.worker.live import DB_NAME, _connect

    cache = tmp_path / "cache2"
    cache.mkdir()
    conn = _connect(cache / DB_NAME, write=True)
    conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()

    outcomes = []

    def fake_evaluate(cache, row, settings, fetcher=None, **kw):
        outcomes.append(int(row["tic"]))
        return {
            "verdict": "Needs review",
            "result": {
                "checks": [{"name": "test", "title": "Test", "status": "pass", "reason": "ok", "numbers": {}}],
                "plot": {"phase": [0, 0.5, 1], "flux": [1, 0.99, 1]},
                "links": {},
            },
        }

    runner = ReviewRunner(cache, VettingConfig(), evaluate=fake_evaluate)
    runner.queue_items([{"tic": 1, "sector": 14, "period": 1.2}, {"tic": 2, "sector": 14, "period": 1.3}])
    assert runner.run_once() is True
    assert len(outcomes) == 1
    assert runner.run_once() is True
    assert len(outcomes) == 2
    assert set(outcomes) == {1, 2}