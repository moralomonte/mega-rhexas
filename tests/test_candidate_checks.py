"""Candidate grouping, catalog badges, and review priority. Catalog HTTP is mocked."""
import json
import sqlite3
import urllib.request
from datetime import timedelta
from pathlib import Path

from rhexas.coord.catalogs import CatalogClient, index_ctoi, index_nea, index_toi
from rhexas.coord.checker import CatalogChecker
from rhexas.coord.httpapi import App
from rhexas.coord.settings import Settings
from rhexas.coord.store import Store
from rhexas.coord.vetting import (
    PRIORITY_HELP,
    build_groups,
    periods_compatible,
    review_priority,
)

from test_coord import Clock, _cand, _star, _store


def _detection(tic=594723, sector=14, period=1.5, depth=0.01, sde=15, snr=20, **extra):
    row = {
        "tic": tic, "sector": sector, "period": period, "depth": depth,
        "sde": sde, "snr": snr, "score": 12.0, "flags": "", "status": "NEW",
        "odd_even_sigma": None, "sec_sigma": None, "duration": 0.1,
    }
    row.update(extra)
    return row


def _pass_cache(stamp="2026-01-01T00:00:00+00:00"):
    def entry(payload):
        return {"ok": 1, "checked_at": stamp, "payload": payload}
    return {
        "tic": entry({
            "found": True, "ra": 1.0, "dec": 2.0, "tmag": 10.0,
            "rad": 1.0, "mass": 1.0, "contratio": 0.01, "neighbors": [],
        }),
        "simbad": entry({"objects": [{"main_id": "TIC 594723", "otype": "*", "sep": 0.2, "ids": []}]}),
        "vsx": entry({"rows": []}),
        "gaia": entry({"rows": [{
            "source_id": "111", "ruwe": 1.05, "non_single_star": 0,
            "phot_variable_flag": "NOT_AVAILABLE", "sep": 0.1,
        }]}),
    }


def _files(tic_rows=None):
    empty = {"ok": 1, "fetched_at": "2026-01-01T00:00:00+00:00", "error": "", "by_tic": {}}
    files = {"exofop_toi": dict(empty), "exofop_ctoi": dict(empty), "nea_planets": dict(empty)}
    if tic_rows:
        for name, rows in tic_rows.items():
            files[name] = dict(empty, by_tic=rows)
    return files


def _one(detections, cache=None, files=None, review=None):
    tic = int(detections[0]["tic"])
    caches = {} if cache is None else {tic: cache}
    reviews = {} if review is None else {tic: review}
    groups = build_groups(detections, caches, reviews, files if files is not None else _files())
    assert len(groups) == 1
    return groups[0]


def test_shortlist_keeps_the_epoch_and_the_sky_position():
    row = _detection(t0=2459000.5, duration=0.12)
    group = _one([row], _pass_cache())
    hit = group["detections"][0]
    assert hit["t0"] == 2459000.5
    assert hit["period"] == 1.5
    assert hit["duration"] == 0.12
    assert group["t0"] == 2459000.5
    assert group["duration"] == 0.12
    assert group["ra"] == 1.0
    assert group["dec"] == 2.0


def test_periods_merge_on_a_ratio_and_ignore_a_mismatch():
    assert periods_compatible(3.0, 3.02) == (True, "")
    assert periods_compatible(6.02, 3.0) == (True, "2×")
    assert periods_compatible(3.0, 6.02) == (True, "1/2×")
    assert periods_compatible(2.0, 4.0)[1] == "1/2×"
    assert periods_compatible(4.0, 2.0)[1] == "2×"
    assert periods_compatible(3.0, 3.2)[0] is False
    rows = [
        _detection(sector=14, period=1.5, snr=20),
        _detection(sector=15, period=3.0, snr=9),
        _detection(sector=16, period=8.0, snr=30),
    ]
    group = _one(rows, _pass_cache(), _files())
    assert group["n_sectors"] == 2
    assert group["sectors"] == [14, 15]
    assert "2×" in group["period_note"]
    assert "does not match" in group["period_note"]
    by_sector = {hit["sector"]: hit for hit in group["detections"]}
    assert by_sector[14]["grouped"] is True
    assert by_sector[15]["grouped"] is True
    assert by_sector[16]["grouped"] is False
    assert group["snr"] == 20


def _three_sectors():
    return [
        _detection(sector=14, period=1.5, snr=20, sde=15),
        _detection(sector=15, period=1.5, snr=11, sde=10),
        _detection(sector=16, period=1.51, snr=9, sde=8),
    ]


def test_review_priority_puts_a_clean_star_ahead_of_a_known_one():
    # SNR 20 and SDE 15 contribute 16, two extra sectors contribute 12, a planetary radius contributes 18.
    clean = _one(_three_sectors(), _pass_cache(), _files())
    assert clean["status_code"] == "passed"
    assert clean["n_sectors"] == 3
    assert clean["review_priority"] == 46.0
    labels = [part["label"] for part in clean["priority_parts"]]
    assert labels[:3] == ["signal", "sectors", "radius"]
    assert "0.5×SNR" in PRIORITY_HELP and "Subtract 80" in PRIORITY_HELP
    assert "odd_even_sigma" in PRIORITY_HELP

    known_cache = _pass_cache()
    known_cache["simbad"] = {"ok": 1, "checked_at": "2026-01-01T00:00:00+00:00", "payload": {"objects": [{
        "main_id": "V* HATS550-016", "otype": "EB*", "sep": 0.4,
        "ids": ["HATS550-016", "TIC 594723"],
    }]}}
    known = _one(_three_sectors(), known_cache, _files())
    assert known["status_code"] == "known"
    assert known["checks"]["simbad"]["state"] == "fail"
    assert "HATS550-016" in known["checks"]["simbad"]["detail"]
    assert known["review_priority"] == 46.0 - 80
    assert known["review_priority"] < clean["review_priority"]


def test_radius_duration_odd_even_and_blend_badges():
    huge = _one([_detection(depth=0.05)], _pass_cache(), _files())
    assert huge["checks"]["planet"]["state"] == "fail"
    assert huge["status_code"] == "binary"
    assert "Rjup" in huge["checks"]["planet"]["summary"]

    short_cache = _pass_cache()
    long = _one([_detection(period=3.0, duration=0.5)], short_cache, _files())
    assert long["checks"]["planet"]["state"] == "fail"
    assert "does not fit" in long["checks"]["planet"]["detail"]

    missing = _one([_detection(odd_even_sigma=None, sec_sigma=None, duration=None)], _pass_cache(), _files())
    assert missing["checks"]["odd_even"]["state"] == "unknown"
    assert missing["checks"]["secondary"]["state"] == "unknown"
    assert "odd_even_sigma" in missing["checks"]["odd_even"]["detail"]
    assert "sec_sigma" in missing["checks"]["secondary"]["detail"]
    assert missing["checks"]["planet"]["state"] == "warn"
    assert "not checked yet" in missing["checks"]["planet"]["detail"].lower()

    clean_vet = _one([_detection(odd_even_sigma=0.4, sec_sigma=-0.2)], _pass_cache(), _files())
    assert clean_vet["checks"]["odd_even"]["state"] == "pass"
    assert clean_vet["checks"]["secondary"]["state"] == "pass"

    mismatch = _one([_detection(odd_even_sigma=4.2, sec_sigma=3.4)], _pass_cache(), _files())
    assert mismatch["checks"]["odd_even"]["state"] == "fail"
    assert mismatch["checks"]["secondary"]["state"] == "fail"
    assert mismatch["status_code"] == "binary"

    crowded = _pass_cache()
    crowded["tic"]["payload"] = dict(crowded["tic"]["payload"], contratio=0.4)
    blend = _one([_detection()], crowded, _files())
    assert blend["checks"]["blend"]["state"] == "fail"
    assert blend["status_code"] == "binary"
    assert blend["review_priority"] < clean_vet["review_priority"]

    mild = _pass_cache()
    mild["tic"]["payload"] = dict(mild["tic"]["payload"], contratio=0.1, neighbors=[{
        "tic": 8, "tmag": 14.0, "sep": 30,
    }])
    warned = _one([_detection()], mild, _files())
    assert warned["checks"]["blend"]["state"] == "warn"

    flagged = _one([_detection(flags="contaminated aperture (CROWDSAP 0.50)")], _pass_cache(), _files())
    assert flagged["checks"]["blend"]["state"] == "fail"


def test_catalog_badges_for_vsx_toi_archive_and_gaia():
    vsx_hit = _pass_cache()
    vsx_hit["vsx"] = {"ok": 1, "checked_at": "2026-01-01", "payload": {"rows": [
        {"name": "XX Cet", "type": "EA", "period": "1.5", "sep": 40},
    ]}}
    assert _one([_detection()], vsx_hit, _files())["checks"]["vsx"]["state"] == "fail"

    nearby = _pass_cache()
    nearby["vsx"] = {"ok": 1, "checked_at": "2026-01-01", "payload": {"rows": [
        {"name": "YY Cet", "type": "M", "period": "80", "sep": 40},
    ]}}
    assert _one([_detection()], nearby, _files())["checks"]["vsx"]["state"] == "warn"

    none = _one([_detection()], _pass_cache(), _files())
    assert none["checks"]["vsx"]["state"] == "pass"
    assert none["checks"]["exofop"]["state"] == "pass"
    assert none["checks"]["nea"]["state"] == "pass"

    files = _files({
        "exofop_toi": {594723: [{"toi": "123.01", "disposition": "KP", "period": "1.5"}]},
    })
    toi = _one([_detection()], _pass_cache(), files)
    assert toi["checks"]["exofop"]["state"] == "fail"
    assert "123.01" in toi["checks"]["exofop"]["detail"]
    assert toi["status_code"] == "known"

    files = _files({"exofop_ctoi": {594723: [{"ctoi": "123.01", "period": "1.5"}]}})
    assert _one([_detection()], _pass_cache(), files)["checks"]["exofop"]["summary"] == "Already a CTOI"

    files = _files({"nea_planets": {594723: [{"name": "HATS-550 b", "period": "1.5"}]}})
    archive = _one([_detection()], _pass_cache(), files)
    assert archive["checks"]["nea"]["state"] == "fail"
    assert "HATS-550 b" in archive["checks"]["nea"]["summary"]

    gaia = _pass_cache()
    gaia["gaia"]["payload"] = {"rows": [{
        "source_id": "1", "ruwe": 1.8, "non_single_star": 0,
        "phot_variable_flag": "VARIABLE", "sep": 0.2,
    }]}
    warned = _one([_detection()], gaia, _files())
    assert warned["checks"]["gaia"]["state"] == "warn"
    assert "RUWE" in warned["checks"]["gaia"]["summary"]

    eclipsing = _pass_cache()
    eclipsing["gaia"]["payload"] = {"rows": [{
        "source_id": "1", "ruwe": 1.1, "non_single_star": 3,
        "phot_variable_flag": "NOT_AVAILABLE", "sep": 0.2,
    }]}
    assert _one([_detection()], eclipsing, _files())["checks"]["gaia"]["state"] == "fail"

    down = _pass_cache()
    down["simbad"] = {"ok": 0, "checked_at": "2026-01-01", "payload": {"error": "timed out"}}
    pending = _one([_detection()], down, _files())
    assert pending["checks"]["simbad"]["state"] == "unknown"
    assert pending["status_code"] == "pending"
    assert pending["checks"]["vsx"]["state"] == "pass"

    fresh = _one([_detection()], None, {})
    assert fresh["status_code"] == "new"


def test_hand_status_overrides_the_badge_and_the_score():
    review = {"status": "rejected", "note": "published EB", "updated_at": "2026-01-02"}
    row = _one([_detection()], _pass_cache(), _files(), review)
    assert row["status_code"] == "rejected"
    assert row["review_manual"] is True
    assert "published EB" in row["status_detail"]
    assert row["automatic_status"] == "passed"
    assert any(part["label"] == "rejected" and part["value"] == -200 for part in row["priority_parts"])

    submitted = _one(
        _three_sectors(), _pass_cache(), _files(),
        {"status": "ctoi", "note": "", "updated_at": "2026-01-02"},
    )
    assert submitted["status_label"] == "CTOI submitted"
    assert submitted["review_priority"] == 46.0 + 10

    parts_only = review_priority(20, 15, 3, row["checks"], None)
    assert parts_only[0] == 46.0


def test_csv_indexes_match_tic_ids():
    toi = index_toi("TIC ID,TOI,TFOPWG Disposition,Period (days)\n594723,123.01,KP,1.5\n")
    assert toi[594723][0]["toi"] == "123.01"
    ctoi = index_ctoi("TIC,CTOI,Period (days)\n594723,9.01,2.0\n")
    assert ctoi[594723][0]["ctoi"] == "9.01"
    planets = index_nea("tic_id,pl_name,pl_orbper\nTIC 594723,HATS-550 b,1.5\n")
    assert planets[594723][0]["name"] == "HATS-550 b"


def test_simbad_falls_back_to_the_harvard_mirror():
    calls = []

    def fetcher(url, timeout, limit):
        calls.append(url)
        if "simbad.cds.unistra.fr" in url:
            raise TimeoutError("simbad down")
        if "harvard" in url and "ident" not in url:
            return 200, 'main_id,otype,ra,dec,oid\n"V* HATS550-016","EB*",1.0,2.0,99\n'
        if "ident" in url:
            return 200, "id\nHATS550-016\nTIC 594723\n"
        if "CIRCLE" in url and "tic82" in url:
            return 200, "TIC,RAJ2000,DEJ2000,Tmag,rad,mass,contratio\n"
        if "tic82" in url:
            return 200, "TIC,RAJ2000,DEJ2000,Tmag,rad,mass,contratio\n594723,1.0,2.0,10,1,1,0.01\n"
        if "vsx" in url:
            return 200, "Name,Type,Period,RAJ2000,DEJ2000\n"
        if "gaia" in url or "gea.esac" in url:
            return 200, "source_id,ra,dec,ruwe,non_single_star,phot_variable_flag\n111,1.0,2.0,1.0,0,NOT_AVAILABLE\n"
        return 500, "unexpected " + url

    client = CatalogClient(fetcher, gap_s=0, sleep=lambda _seconds: None)
    result = client.check_star(594723, ["tic", "simbad", "vsx", "gaia"], None)
    assert result["simbad"]["ok"] == 1
    assert result["simbad"]["payload"]["mirror"] is True
    assert result["simbad"]["payload"]["objects"][0]["main_id"] == "V* HATS550-016"
    assert "HATS550-016" in result["simbad"]["payload"]["objects"][0]["ids"]
    assert any("harvard" in url for url in calls)
    assert result["tic"]["ok"] == 1
    assert result["vsx"]["payload"]["rows"] == []
    assert result["gaia"]["ok"] == 1


def test_a_down_service_does_not_drop_the_other_checks():
    def fetcher(url, timeout, limit):
        if "simbad" in url:
            raise TimeoutError("down")
        if "vsx" in url:
            return 200, "Name,Type,Period,RAJ2000,DEJ2000\n"
        if "gaia" in url or "gea.esac" in url:
            return 200, "source_id,ra,dec,ruwe,non_single_star,phot_variable_flag\n"
        return 200, ""

    client = CatalogClient(fetcher, gap_s=0, sleep=lambda _seconds: None)
    result = client.check_star(1, ["simbad", "vsx", "gaia"], {"ra": 10.0, "dec": -20.0, "found": True})
    assert result["simbad"]["ok"] == 0
    assert "Harvard" in result["simbad"]["payload"]["error"] or "mirror" in result["simbad"]["payload"]["error"]
    assert result["vsx"]["ok"] == 1
    assert result["gaia"]["ok"] == 1


def _catalog_fetcher(calls, sleeps, simbad="ok"):
    def fetcher(url, timeout, limit):
        calls.append(url)
        if "download_toi" in url:
            return 200, "TIC ID,TOI,TFOPWG Disposition,Period (days)\n"
        if "download_ctoi" in url:
            return 200, "TIC ID,CTOI,Period (days)\n"
        if "exoplanetarchive" in url:
            return 200, "tic_id,pl_name,pl_orbper\n"
        if "simbad" in url:
            if simbad == "down":
                raise TimeoutError("simbad down")
            if "ident" in url:
                return 200, "id\nTIC 7\n"
            return 200, "main_id,otype,ra,dec,oid\n*,*,1.0,2.0,5\n"
        if "CIRCLE" in url and "tic82" in url:
            return 200, "TIC,RAJ2000,DEJ2000,Tmag,rad,mass,contratio\n7,1.0,2.0,10,1,1,0.01\n"
        if "tic82" in url or "IV/39/tic" in url:
            return 200, "TIC,RAJ2000,DEJ2000,Tmag,rad,mass,contratio\n7,1.0,2.0,10,1,1,0.01\n"
        if "vsx" in url:
            return 200, "Name,Type,Period,RAJ2000,DEJ2000\n"
        if "gaia" in url or "gea.esac" in url:
            return 200, "source_id,ra,dec,ruwe,non_single_star,phot_variable_flag\n9,1.0,2.0,1.0,0,NOT_AVAILABLE\n"
        return 500, "unexpected"
    return fetcher


def test_checks_are_cached_and_rate_limited(tmp_path):
    store = _store(tmp_path)
    calls, sleeps = [], []
    try:
        _agree(store, tic=7, odd_even=0.2, secondary=0.1)
        checker = CatalogChecker(
            store,
            fetcher=_catalog_fetcher(calls, sleeps),
            sleep=sleeps.append,
            gap_s=1.5,
        )
        assert checker.run_once() is True
        assert sleeps, "calls should be spaced out"
        assert store.conn.execute("SELECT COUNT(*) AS n FROM catalog_cache").fetchone()["n"] >= 4
        calls.clear()
        sleeps.clear()
        assert checker.run_once() is False
        assert calls == []

        store.clock.now += timedelta(days=31)
        assert checker.run_once() is True
        assert calls

        calls.clear()
        store.clock.now += timedelta(hours=1)
        assert checker.run_once() is False
        assert calls == []
    finally:
        store.close()


def test_a_failed_lookup_retries_after_a_few_hours_not_a_month(tmp_path):
    store = _store(tmp_path)
    calls = []
    try:
        _agree(store, tic=7)
        checker = CatalogChecker(
            store, fetcher=_catalog_fetcher(calls, [], simbad="down"), gap_s=0, sleep=lambda _s: None,
        )
        assert checker.run_once() is True
        row = store.conn.execute(
            "SELECT ok FROM catalog_cache WHERE tic=7 AND source='simbad'"
        ).fetchone()
        assert row["ok"] == 0
        calls.clear()
        assert checker.run_once() is False
        assert calls == []
        store.clock.now += timedelta(hours=7)
        assert checker.run_once() is True
        assert any("simbad" in url for url in calls)
        assert not any("tic82" in url for url in calls)
    finally:
        store.close()


def test_listing_candidates_does_not_touch_the_network(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("the candidate page must not call a catalog")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    store = _store(tmp_path)
    try:
        _agree(store, tic=7, odd_even=0.3, secondary=0.2, duration=0.08)
        rows = store.candidates_public()
        assert rows[0]["tic"] == 7
        assert rows[0]["checks"]["odd_even"]["state"] == "pass"
        assert rows[0]["checks"]["simbad"]["state"] == "unknown"
        kept = store.conn.execute("SELECT status FROM candidates").fetchone()["status"]
        assert kept == "NEW"
    finally:
        store.close()


def test_two_sectors_group_and_a_review_does_not_rewrite_rows(tmp_path):
    store = _store(tmp_path)
    try:
        _agree(store, tic=594723, sector=14, period=1.5, snr=20, odd_even=0.4, secondary=0.2)
        _agree(store, tic=594723, sector=15, period=3.0, snr=9, odd_even=1.0, secondary=0.3)
        before = [
            tuple(row) for row in store.conn.execute(
                "SELECT id, tic, sector, status, score, period FROM candidates ORDER BY id"
            )
        ]
        result_before = store.conn.execute("SELECT id, candidates_json FROM results ORDER BY id").fetchall()
        grouped = store.candidates_public()
        assert len(grouped) == 1
        assert grouped[0]["n_sectors"] == 2
        assert grouped[0]["sectors"] == [14, 15]
        assert grouped[0]["checks"]["odd_even"]["state"] == "pass"

        admin = store.ensure_admin()
        app = App(store)
        denied = app.handle("POST", "/api/v1/candidates/review", "", {}, b'{"tic":594723,"status":"rejected"}')
        assert denied.status == 401
        saved = app.handle(
            "POST", "/api/v1/candidates/review", "",
            {"Authorization": f"Bearer {admin}"},
            json.dumps({"tic": 594723, "status": "ctoi", "note": "sent to ExoFOP"}).encode(),
        )
        assert saved.status == 200
        body = json.loads(saved.body)
        assert body["status"] == "ctoi"
        assert body["note"] == "sent to ExoFOP"
        after = [
            tuple(row) for row in store.conn.execute(
                "SELECT id, tic, sector, status, score, period FROM candidates ORDER BY id"
            )
        ]
        assert after == before
        result_after = store.conn.execute("SELECT id, candidates_json FROM results ORDER BY id").fetchall()
        assert [tuple(row) for row in result_after] == [tuple(row) for row in result_before]
        shown = store.candidates_public()[0]
        assert shown["status_label"] == "CTOI submitted"
        assert shown["review_note"] == "sent to ExoFOP"
        assert store.conn.execute("SELECT status FROM candidates WHERE tic=594723").fetchone()["status"] == "NEW"

        cleared = app.handle(
            "POST", "/api/v1/candidates/review", "",
            {"Authorization": f"Bearer {admin}"},
            json.dumps({"tic": 594723, "status": "auto", "note": ""}).encode(),
        )
        assert cleared.status == 200
        assert store.candidates_public()[0]["review_manual"] is False
    finally:
        store.close()


def test_migration_adds_tables_without_touching_candidate_or_result_rows(tmp_path):
    path = tmp_path / "coord.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE candidates (
            id INTEGER PRIMARY KEY, unit_id INTEGER NOT NULL, generation INTEGER NOT NULL,
            tic INTEGER NOT NULL, sector INTEGER NOT NULL, period REAL, depth REAL,
            sde REAL, snr REAL, status TEXT, score REAL, flags TEXT,
            UNIQUE (unit_id, tic))"""
    )
    conn.execute(
        """CREATE TABLE results (
            id INTEGER PRIMARY KEY, assignment_id INTEGER, unit_id INTEGER, project TEXT,
            worker_id INTEGER, generation INTEGER, stars_json TEXT, candidates_json TEXT,
            n_stars INTEGER, n_candidates INTEGER, n_errors INTEGER, created_at TEXT, verdict TEXT)"""
    )
    blob = '[{"tic": 594723, "status": "NEW", "period": 2.5}]'
    conn.execute(
        "INSERT INTO candidates VALUES (7, 3, 1, 594723, 14, 2.5, 0.01, 12, 11, 'NEW', 9.5, '')"
    )
    conn.execute(
        "INSERT INTO results VALUES (4, 1, 3, 'exoplanets', 1, 1, '[]', ?, 1, 1, 0, '2026-01-01T00:00:00+00:00', 'agreed')",
        (blob,),
    )
    conn.commit()
    conn.close()

    store = Store(path, Settings(), clock=Clock())
    try:
        store.init_schema()
        row = store.conn.execute("SELECT * FROM candidates WHERE id=7").fetchone()
        assert row["tic"] == 594723
        assert row["status"] == "NEW"
        assert row["score"] == 9.5
        assert row["sector"] == 14
        kept = store.conn.execute("SELECT candidates_json FROM results WHERE id=4").fetchone()[0]
        assert kept == blob
        for name in ("catalog_cache", "catalog_files", "candidate_reviews"):
            assert store.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
    finally:
        store.close()


def test_status_page_links_science_tools():
    root = Path(__file__).resolve().parents[1].joinpath("src/rhexas/coord")
    status = (root / "status.html").read_text(encoding="utf-8")
    assert "/candidates" in status
    assert "/vetting" in status
    vetting = (root / "vetting.html").read_text(encoding="utf-8")
    assert "priority_help" in vetting
    assert "Odd/even" in vetting
    assert "/api/v1/candidates/review" in vetting


def _agree(store, tic, sector=14, period=1.5, snr=20.0, odd_even=None, secondary=None, duration=0.1):
    store.enroll_tics(sector, [tic], unit_size=500)
    token = store.create_worker("ada" + str(sector))
    worker = store.authenticate(token)["id"]
    first = store.claim(worker)
    stars = [_star(tic, period=period)]
    cand = _cand(tic)
    cand["period"] = period
    cand["snr"] = snr
    cand["duration"] = duration
    cand["odd_even_sigma"] = odd_even
    cand["sec_sigma"] = secondary
    done = store.submit(worker, first["assignment_id"], stars, [cand])
    assert done["verdict"] == "agreed"
