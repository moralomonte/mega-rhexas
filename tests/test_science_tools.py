"""Phase 5 science tools: ranking, re-check, export, auth, migrations."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from rhexas.coord.httpapi import App
from rhexas.coord.science import (
    progress_summary,
    refresh_candidate_recheck,
    shortlist_rows,
)
from rhexas.coord.store import Store
from rhexas.coord.vetting import build_groups

from test_candidate_checks import _detection, _files, _one, _pass_cache
from test_coord import Clock, _cand, _star, _store


def _science(app: App, headers=None):
    return app.handle("GET", "/api/v1/candidates/science", "limit=50", headers or {}, b"")


def _export(app: App, query: str, headers=None):
    return app.handle("GET", "/api/v1/candidates/export", query, headers or {}, b"")


def test_progress_summary_buckets():
    rows = [
        {"status_code": "new"},
        {"status_code": "pending"},
        {"status_code": "known"},
        {"status_code": "rejected", "review_manual": True},
    ]
    summary = progress_summary(rows)
    assert summary["total"] == 4
    assert summary["new"] == 1
    assert summary["under_review"] == 1
    assert summary["known"] == 1
    assert summary["false_positive"] == 1
    assert summary["reviewed"] == 2


def test_shortlist_demotes_known_and_explains():
    known = _one([_detection(snr=30, sde=20)], _pass_cache(), _files())
    known["review_priority"] = 50.0
    known["priority_parts"] = [{"label": "signal", "value": 50.0}]
    known["status_code"] = "known"
    known["checks"]["exofop"] = {"state": "fail", "summary": "TOI 1.01", "detail": ""}
    normal = _one([_detection(tic=111, snr=28, sde=18)], _pass_cache(), _files())
    normal["review_priority"] = 40.0
    normal["priority_parts"] = [{"label": "signal", "value": 40.0}]
    normal["status_code"] = "passed"
    ranked = shortlist_rows([known, normal], limit=2)
    assert ranked[0]["tic"] == 111
    assert ranked[1]["shortlist_excluded"] is True
    assert any("Known" in line for line in ranked[1]["rank_explanation"])


def test_recheck_flags_new_sector_enrollment(tmp_path):
    from test_candidate_checks import _agree

    store = _store(tmp_path)
    try:
        _agree(store, tic=1001, sector=14)
        refresh_candidate_recheck(store)
        before = store.conn.execute(
            "SELECT needs_recheck FROM candidate_recheck WHERE tic=1001"
        ).fetchone()
        assert before is not None
        assert int(before["needs_recheck"]) == 0
        store.enroll_tics(15, [1001, 2002], unit_size=2)
        refresh_candidate_recheck(store)
        row = store.conn.execute(
            "SELECT needs_recheck, recheck_sectors FROM candidate_recheck WHERE tic=1001"
        ).fetchone()
        assert row is not None
        assert int(row["needs_recheck"]) == 1
        sectors = json.loads(row["recheck_sectors"])
        assert 15 in sectors
    finally:
        store.close()


def test_science_migration_idempotent(tmp_path):
    path = tmp_path / "coord.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE candidates (
            id INTEGER PRIMARY KEY, unit_id INTEGER, generation INTEGER, tic INTEGER,
            sector INTEGER, period REAL, depth REAL, sde REAL, snr REAL,
            status TEXT, score REAL, flags TEXT)"""
    )
    conn.execute(
        "INSERT INTO candidates VALUES (1,1,1,555,14,1.2,0.01,10,9,'NEW',1,'')"
    )
    conn.commit()
    conn.close()
    store = Store(path, clock=Clock())
    try:
        store.init_schema()
        store.init_schema()
        for name in ("science_sector_watch", "candidate_recheck"):
            assert store.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
        row = store.conn.execute("SELECT status FROM candidates WHERE id=1").fetchone()
        assert row["status"] == "NEW"
    finally:
        store.close()


def test_science_api_requires_operator_auth(tmp_path):
    store = _store(tmp_path)
    try:
        app = App(store)
        denied = _science(app)
        assert denied.status == 401
        admin = store.ensure_admin()
        ok = _science(app, {"Authorization": f"Bearer {admin}"})
        assert ok.status == 200
        body = json.loads(ok.body)
        assert "progress" in body
        assert "shortlist" in body
        assert "candidates" in body
    finally:
        store.close()


def test_export_markdown_contains_draft_banner(tmp_path):
    store = _store(tmp_path)
    try:
        store.enroll_tics(14, [9001], unit_size=500)
        token = store.create_worker("w")
        wid = store.authenticate(token)["id"]
        a = store.claim(wid)
        store.submit(wid, a["assignment_id"], [_star(9001)], [_cand(9001)])
        app = App(store)
        admin = store.ensure_admin()
        res = _export(app, "tic=9001&format=md", {"Authorization": f"Bearer {admin}"})
        assert res.status == 200
        text = res.body.decode("utf-8")
        assert "DRAFT" in text
        assert "not submitted" in text.lower()
        assert "not a confirmed" in text.lower() or "not confirmed" in text.lower()
    finally:
        store.close()


def test_review_accepts_operator_session(tmp_path):
    from test_accounts import _login, _open, _seed, _session

    store = _store(tmp_path)
    try:
        store.enroll_tics(14, [8001], unit_size=500)
        token = store.create_worker("w")
        wid = store.authenticate(token)["id"]
        a = store.claim(wid)
        store.submit(wid, a["assignment_id"], [_star(8001)], [_cand(8001)])
        _seed(store, email="ops@example.com", name="Ops", password="correct horse")
        app = _open(store)
        logged = _login(app, email="ops@example.com", password="correct horse")
        session = _session(logged)
        res = app.handle(
            "POST",
            "/api/v1/candidates/review",
            "",
            {"Cookie": f"rhexas_session={session}", "Content-Type": "application/json"},
            json.dumps({"tic": 8001, "status": "passed", "note": "ok"}).encode(),
        )
        assert res.status == 200
    finally:
        store.close()