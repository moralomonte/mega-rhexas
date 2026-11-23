"""Phase 3: private sector page, mission progress JSON, and migration."""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

from rhexas.coord.httpapi import App
from rhexas.coord.mission import compact_progress, mission_progress
from rhexas.coord.store import Store
from tests.test_coord import Clock, _ids, _star, _store


def _admin_cookie(token: str) -> dict:
    return {"Cookie": f"rhexas_admin={token}"}


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_sectors_page_requires_operator_auth(tmp_path):
    store = _store(tmp_path)
    admin = store.ensure_admin()
    app = App(store)
    try:
        anon = app.handle("GET", "/sectors", "", {}, b"")
        assert anon.status in (303, 401, 403)
        if anon.status == 303:
            assert "/login" in anon.headers[0][1]
        bad = app.handle("GET", "/sectors", "", _bearer("nope"), b"")
        assert bad.status in (303, 401, 403)
        ok = app.handle("GET", "/sectors", "", _admin_cookie(admin), b"")
        assert ok.status == 200
        html = ok.body.decode("utf-8")
        assert "Mission sectors" in html
        assert "Overall progress" in html
    finally:
        store.close()


def test_mission_progress_endpoint_auth_and_math(tmp_path):
    store = _store(tmp_path)
    admin = store.ensure_admin()
    try:
        store.enroll_tics(107, list(range(1, 11)), unit_size=5)
        store.enroll_tics(96, list(range(20, 26)), unit_size=5)
        tokens, ids = _ids(store, "ada")
        app = App(store)
        assert app.handle("GET", "/api/v1/mission/progress", "", {}, b"").status == 401
        worker_headers = _bearer(tokens["ada"])
        before = app.handle("GET", "/api/v1/mission/progress", "", worker_headers, b"")
        assert before.status == 200
        body = compact_progress(store)
        assert body["units_total"] == 4
        assert body["units_verified"] == 0
        assert body["percent_units"] == 0.0
        assert "enrolled" in body["scope_note"].lower()

        lease = store.claim(ids["ada"])
        store.submit(
            ids["ada"], lease["assignment_id"],
            [_star(tic) for tic in lease["tics"]], [],
        )
        after = mission_progress(store)
        assert after["units_verified"] == 1
        assert after["percent_units"] == 25.0
        s96 = next(row for row in after["sectors"] if row["sector"] == 96)
        assert s96["state"] == "pending"
        s107 = next(row for row in after["sectors"] if row["sector"] == 107)
        assert s107["state"] == "working"

        admin_ok = app.handle("GET", "/api/v1/mission/progress", "", _admin_cookie(admin), b"")
        assert admin_ok.status == 200
        overview_denied = app.handle("GET", "/api/v1/mission/overview", "", worker_headers, b"")
        assert overview_denied.status == 401
        overview = app.handle("GET", "/api/v1/mission/overview", "", _admin_cookie(admin), b"")
        assert overview.status == 200
    finally:
        store.close()


def test_timeline_and_agreed_at_index(tmp_path):
    store = _store(tmp_path)
    try:
        store.enroll_tics(55, list(range(1, 5)), unit_size=2)
        _, ids = _ids(store, "ada")
        first = store.claim(ids["ada"])
        store.submit(ids["ada"], first["assignment_id"], [_star(1), _star(2)], [])
        store.clock.now += timedelta(days=1)
        second = store.claim(ids["ada"])
        store.submit(ids["ada"], second["assignment_id"], [_star(3), _star(4)], [])
        snap = mission_progress(store)
        days = [row["day"] for row in snap["timeline"]]
        assert len(days) == 2
        assert days == sorted(days)
        cols = {row[1] for row in store.conn.execute("PRAGMA index_list(units)")}
        # migration creates a partial index for timeline scans
        index_sql = "\n".join(
            row[0] or ""
            for row in store.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='units'"
            )
        )
        assert "idx_units_agreed_at" in index_sql
    finally:
        store.close()


def test_sector_states_done_and_working(tmp_path):
    store = _store(tmp_path)
    try:
        store.enroll_tics(10, [1, 2, 3, 4], unit_size=2)
        _, ids = _ids(store, "ada", "bea")
        one = store.claim(ids["ada"])
        store.submit(ids["ada"], one["assignment_id"], [_star(1), _star(2)], [])
        snap = mission_progress(store)
        row = snap["sectors"][0]
        assert row["state"] == "working"
        assert row["units_done"] == 1
        assert row["units_total"] == 2
        store.claim(ids["bea"])  # second unit in progress
        snap2 = mission_progress(store)
        assert snap2["sectors"][0]["state"] == "working"
        two = store.claim(ids["bea"])
        store.submit(ids["bea"], two["assignment_id"], [_star(3), _star(4)], [])
        done = mission_progress(store)
        assert done["sectors"][0]["state"] == "done"
        assert done["percent_units"] == 100.0
    finally:
        store.close()


def test_tess_pointing_loads(tmp_path):
    from rhexas.coord.tess_pointing import footprint_polygon, spacecraft_pointing
    center = spacecraft_pointing(107)
    assert center is not None
    poly = footprint_polygon(107)
    assert poly and len(poly) == 4