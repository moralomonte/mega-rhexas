"""Phase 6: local review queue, coordinator local-check API, validation."""
from __future__ import annotations

import json
import time

import numpy as np
import pytest

from rhexas.coord.httpapi import App
from rhexas.coord.local_review import LocalReviewError, validate_local_check
from rhexas.config import ReviewConfig
from rhexas.worker.review.gate import (
    clear_pending_reservations,
    reserve_processed_bytes,
    release_processed_bytes,
    search_busy,
    unit_boundary_open,
)
from rhexas.worker.review.runner import ReviewRunner, run_at_boundary, status_payload, stop_all
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


def test_review_runner_defers_when_search_busy_boundary_mode(tmp_path):
    from rhexas.worker.live import DB_NAME, Store

    cache = tmp_path / "cache"
    cache.mkdir()
    live = Store(cache)
    live.begin_batch({"sector": 14, "unit_id": 1, "assignment_id": 1, "n_tics": 2, "tics": [1, 2]}, stage="searching")
    busy, _ = search_busy(cache)
    assert busy is True
    review = ReviewConfig(concurrent=False)
    runner = ReviewRunner(
        cache,
        type("S", (), {"enabled": False, "radius_arcsec": 15.0, "timeout_s": 5.0, "cache_max_mb": 1.0})(),
        review_settings=review,
    )
    assert runner.run_once() is False


def test_review_runner_concurrent_while_search_busy(tmp_path):
    from rhexas.config import VettingConfig
    from rhexas.worker.live import DB_NAME, Store, _connect

    cache = tmp_path / "cache-conc"
    cache.mkdir()
    conn = _connect(cache / DB_NAME, write=True)
    conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()
    live = Store(cache)
    live.begin_batch({"sector": 14, "unit_id": 1, "assignment_id": 1, "n_tics": 2, "tics": [1, 2]}, stage="searching")
    assert search_busy(cache)[0] is True

    seen = []

    def fake_evaluate(cache, row, settings, fetcher=None, **kw):
        seen.append(int(row["tic"]))
        return {
            "verdict": "Needs review",
            "result": {
                "checks": [{"name": "test", "title": "Test", "status": "pass", "reason": "ok", "numbers": {}}],
                "plot": {"phase": [0, 0.5, 1], "flux": [1, 0.99, 1]},
                "links": {},
            },
        }

    runner = ReviewRunner(cache, VettingConfig(), review_settings=ReviewConfig(concurrent=True), evaluate=fake_evaluate)
    runner.queue_items([{"tic": 99, "sector": 14, "period": 1.0}])
    assert runner.run_once() is True
    assert seen == [99]


def test_review_boundary_runs_after_upload_stage(tmp_path):
    from rhexas.config import VettingConfig
    from rhexas.worker.live import DB_NAME, Store, _connect

    cache = tmp_path / "cache-bnd"
    cache.mkdir()
    conn = _connect(cache / DB_NAME, write=True)
    conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()
    live = Store(cache)
    live.begin_batch({"sector": 14, "unit_id": 9, "assignment_id": 9, "n_tics": 1, "tics": [1]}, stage="searching")
    assert unit_boundary_open(cache)[0] is False

    def fake_evaluate(cache, row, settings, fetcher=None, **kw):
        return {
            "verdict": "Needs review",
            "result": {
                "checks": [{"name": "test", "title": "Test", "status": "pass", "reason": "ok", "numbers": {}}],
                "plot": {"phase": [0, 1], "flux": [1, 1]},
                "links": {},
            },
        }

    review = ReviewConfig(concurrent=False)
    runner = ReviewRunner(cache, VettingConfig(), review_settings=review, evaluate=fake_evaluate)
    runner.queue_items([{"tic": 5, "sector": 14, "period": 1.1}])
    live.update_batch(stage="uploaded", active=False, stars_done=1, stars_total=1)
    assert unit_boundary_open(cache)[0] is True
    assert runner.run_once(boundary=True) is True


def test_processed_reserve_is_atomic_under_cap(tmp_path, monkeypatch):
    from rhexas.worker.review import gate

    cache = tmp_path / "cache-cap"
    clear_pending_reservations(cache)
    monkeypatch.setattr(gate, "processed_headroom", lambda _c, configured_gb=None: (900, 1000, ""))
    assert reserve_processed_bytes(cache, 50)[0] is True
    assert reserve_processed_bytes(cache, 50)[0] is True
    assert reserve_processed_bytes(cache, 1)[0] is False
    release_processed_bytes(cache, 50)
    assert reserve_processed_bytes(cache, 50)[0] is True


def test_lightcurve_download_lock_prevents_double_start(tmp_path, monkeypatch):
    import threading

    from rhexas.worker.review.fetch import ensure_lightcurve

    cache = tmp_path / "cache-lock"
    calls = []
    done = threading.Event()
    saved = tmp_path / "lc.fits"

    def find_lc(_c, _tic, _sector):
        if calls and saved.is_file():
            return saved
        return None

    def fake_run(*_a, **_k):
        calls.append(1)
        done.wait(timeout=2)
        saved.write_bytes(b"fits")
        return 0

    monkeypatch.setattr("rhexas.worker.review.fetch.find_lightcurve", find_lc)
    monkeypatch.setattr("rhexas.worker.review.fetch.fits_gate", lambda *_a, **_k: (True, ""))
    monkeypatch.setattr("rhexas.download.run", fake_run)

    def first():
        ensure_lightcurve(cache, 1, 14, sleeper=lambda _s: None)
        done.set()

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=lambda: ensure_lightcurve(cache, 1, 14, sleeper=lambda _s: None))
    t1.start()
    time.sleep(0.05)
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert len(calls) == 1


def test_review_status_shows_boundary_wait_note(tmp_path):
    from rhexas.config import VettingConfig
    from rhexas.worker.live import DB_NAME, Store, _connect
    from rhexas.worker.review.store import enqueue_selected, migrate

    cache = tmp_path / "cache-note"
    cache.mkdir()
    conn = _connect(cache / DB_NAME, write=True)
    migrate(conn)
    enqueue_selected(conn, [{"tic": 1, "sector": 14, "period": 1.0, "rank": 1}], 1.0)
    conn.commit()
    conn.close()
    live = Store(cache)
    live.begin_batch(
        {"sector": 14, "unit_id": 2, "assignment_id": 2, "n_tics": 100, "tics": list(range(100))},
        stage="searching",
    )
    live.update_batch(stars_done=40, stars_total=100, per_s=2.0, eta_min=30.0, active=True)
    body = status_payload(cache, None, VettingConfig(), ReviewConfig(concurrent=False))
    job = next(j for j in body["jobs"] if j["tic"] == 1)
    assert "waits for the current unit to finish" in job["scheduler_note"]
    assert "30" in job["scheduler_note"] or "~30" in job["scheduler_note"]
    assert body["scheduler"]["mode"] == "boundary"


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


def test_review_store_adds_sky_columns_on_an_existing_database(tmp_path):
    import sqlite3

    from rhexas.worker.review.store import enqueue_selected, get_row, migrate

    conn = sqlite3.connect(tmp_path / "old.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE local_review (
            tic INTEGER NOT NULL,
            sector INTEGER NOT NULL,
            check_key TEXT NOT NULL DEFAULT '',
            period REAL,
            t0 REAL,
            duration REAL,
            depth REAL,
            snr REAL,
            sde REAL,
            rank INTEGER,
            state TEXT NOT NULL DEFAULT 'queued',
            verdict TEXT,
            result_json TEXT,
            upload_state TEXT NOT NULL DEFAULT 'none',
            upload_detail TEXT NOT NULL DEFAULT '',
            download_bytes INTEGER,
            download_note TEXT NOT NULL DEFAULT '',
            checked_at REAL,
            updated_at REAL,
            cancelled INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tic, sector)
        )"""
    )
    migrate(conn)
    assert enqueue_selected(conn, [{"tic": 7, "sector": 89, "ra": 113.97, "dec": -65.68, "t0": 1700.5}], 10) == 1
    conn.commit()
    saved = get_row(conn, 7, 89)
    conn.close()
    assert saved["ra"] == pytest.approx(113.97)
    assert saved["dec"] == pytest.approx(-65.68)
    assert saved["t0"] == pytest.approx(1700.5)


def test_review_detail_reads_the_saved_sqlite_row(tmp_path):
    from rhexas.config import VettingConfig
    from rhexas.worker.live import DB_NAME, _connect
    from rhexas.worker.review.runner import detail_payload
    from rhexas.worker.review.store import get_row

    cache = tmp_path / "cache-detail"
    cache.mkdir()
    conn = _connect(cache / DB_NAME, write=True)
    conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()

    def fake_evaluate(cache, row, settings, fetcher=None, **kw):
        return {
            "verdict": "Needs review",
            "result": {
                "checks": [{"name": "test", "title": "Test", "status": "pass", "reason": "ok", "numbers": {}}],
                "plot": {"phase": [0, 0.5, 1], "flux": [1, 0.99, 1]},
                "links": {},
            },
        }

    runner = ReviewRunner(cache, VettingConfig(), evaluate=fake_evaluate)
    runner.queue_items([{
        "tic": 349911034, "sector": 89, "period": 3.2, "t0": 1700.5,
        "duration": 0.12, "ra": 113.97, "dec": -65.68,
    }])
    assert runner.run_once() is True
    payload = detail_payload(cache, 349911034, 89)
    assert payload["available"] is True
    assert payload["checks"][0]["name"] == "test"
    assert payload["checks"][0]["status"] == "pass"
    conn = _connect(cache / DB_NAME, write=False)
    try:
        saved = get_row(conn, 349911034, 89)
    finally:
        conn.close()
    assert saved["t0"] == pytest.approx(1700.5)
    assert saved["ra"] == pytest.approx(113.97)
    assert saved["dec"] == pytest.approx(-65.68)


def test_review_downloads_a_missing_light_curve_and_skips_when_it_does_not_fit(tmp_path, monkeypatch):
    from rhexas.worker.review.fetch import ensure_lightcurve, ensure_tpf
    from rhexas.worker.vetting.evaluate import find_lightcurve

    monkeypatch.setattr("requests.get", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network")))
    tic = 349911034
    name = f"tess2026221103959-s0089-{tic:016d}-0309-s_lc.fits"
    script = f'curl -C - -L -o {name} "https://example.invalid/file?uri=mast:HLSP/tess/{name}"\n'
    urls = []

    class _Resp:
        def __init__(self, status, content=b"", text=""):
            self.status_code = status
            self.content = content
            self.text = text

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    def getter(url, timeout=60):
        urls.append(url)
        if "tesscurl" in url:
            return _Resp(200, text=script)
        return _Resp(200, content=b"FITSBYTES")

    cache = tmp_path / "cache-dl"
    info = ensure_lightcurve(cache, tic, 89, getter=getter, sleeper=lambda _s: None)
    assert info["downloaded"] is True
    assert info["path"] == find_lightcurve(cache, tic, 89)
    assert info["path"].read_bytes() == b"FITSBYTES"
    assert any(url.endswith("tesscurl_sector_89_lc.sh") for url in urls)
    assert all("_tp" not in url for url in urls)

    urls.clear()
    again = ensure_lightcurve(cache, tic, 89, getter=getter, sleeper=lambda _s: None)
    assert again["downloaded"] is False
    assert again["path"] == info["path"]
    assert urls == []

    monkeypatch.setattr("rhexas.worker.review.fetch.estimate_fits_bytes", lambda sector: 2_000_000_000)
    other = tmp_path / "cache-huge"
    skipped = ensure_lightcurve(other, tic, 89, getter=getter, sleeper=lambda _s: None)
    assert skipped["skipped"] is True
    assert skipped["path"] is None
    assert "1 GB" in skipped["note"]
    assert urls == []

    monkeypatch.setattr("rhexas.worker.review.fetch.estimate_fits_bytes", lambda sector: 500_000)
    monkeypatch.setattr(
        "rhexas.worker.review.gate.processed_headroom",
        lambda cache, configured_gb=None: (1_000_000_000, 1_000_000_000, "near the cap"),
    )
    capped = ensure_lightcurve(tmp_path / "cache-cap", tic, 89, getter=getter, sleeper=lambda _s: None)
    assert capped["skipped"] is True
    assert "processed-data cap" in capped["note"]
    assert urls == []

    called = []
    monkeypatch.setattr(
        "rhexas.worker.review.fetch.fits_gate",
        lambda cache, nbytes, **kwargs: (False, "skipped download: it would pass the processed-data cap"),
    )
    tpf = ensure_tpf(tmp_path / "cache-tpf", tic, 89, downloader=lambda *args: called.append(args))
    assert called == []
    assert tpf["skipped"] is True
    assert tpf["path"] is None