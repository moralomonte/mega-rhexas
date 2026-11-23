"""Phase 4: private discoveries gallery, filters, to-scale math, known marking."""
from __future__ import annotations

import json
from pathlib import Path

from rhexas.coord.discoveries_gallery import (
    build_detail_payload,
    filter_bucket,
    filter_groups,
    known_catalog_match,
    orbit_au,
    planet_radius_fields,
    to_scale_payload,
    read_or_write_thumbnail,
    thumb_cache_dir,
)
from rhexas.coord.httpapi import App
from rhexas.coord.vetting import companion_radius
from tests.test_coord import _cand, _star, _store


def _admin_cookie(token: str) -> dict:
    return {"Cookie": f"rhexas_admin={token}"}


def _enroll_candidate(store, tic=594723, sector=14, **cand_kw):
    store.enroll_tics(sector, [tic], unit_size=500)
    from tests.test_coord import _ids

    _, ids = _ids(store, "ada")
    lease = store.claim(ids["ada"])
    cand = _cand(tic)
    cand.update(cand_kw)
    store.submit(ids["ada"], lease["assignment_id"], [_star(tic)], [cand])


def test_discoveries_pages_require_operator_auth(tmp_path):
    store = _store(tmp_path)
    admin = store.ensure_admin()
    app = App(store)
    try:
        assert app.handle("GET", "/discoveries", "", {}, b"").status in (303, 401, 403)
        ok = app.handle("GET", "/discoveries", "", _admin_cookie(admin), b"")
        assert ok.status == 200
        assert "Discoveries gallery" in ok.body.decode("utf-8")
        detail = app.handle("GET", "/discoveries/1", "", _admin_cookie(admin), b"")
        assert detail.status == 200
        assert b"const TIC=1" in detail.body
    finally:
        store.close()


def test_gallery_api_filters_and_sort(tmp_path):
    store = _store(tmp_path)
    admin = store.ensure_admin()
    app = App(store)
    try:
        _enroll_candidate(store, tic=101, sector=10, period=2.0, sde=30, snr=25)
        store.enroll_tics(11, [102], unit_size=500)
        from tests.test_coord import _ids

        _, ids = _ids(store, "bea")
        lease = store.claim(ids["bea"])
        cand = _cand(102)
        cand.update({"period": 1.0, "sde": 5, "snr": 8})
        store.submit(ids["bea"], lease["assignment_id"], [_star(102)], [cand])
        denied = app.handle("GET", "/api/v1/discoveries", "", {}, b"")
        assert denied.status == 401
        body = json.loads(
            app.handle("GET", "/api/v1/discoveries", "sort=sde", _admin_cookie(admin), b"").body.decode()
        )
        assert body["total"] == 2
        assert body["items"][0]["tic"] == 101
        assert body["items"][0]["wording"] == "candidate"
        assert "thumb.svg" in body["items"][0]["thumb_url"]
        new_only = json.loads(
            app.handle("GET", "/api/v1/discoveries", "status=new", _admin_cookie(admin), b"").body.decode()
        )
        assert all(row["filter_bucket"] == "new" for row in new_only["items"])
    finally:
        store.close()


def test_to_scale_and_orbit_math():
    depth = 0.01
    rstar = 1.0
    rp_earth, _ = companion_radius(depth, rstar)
    fields = planet_radius_fields(depth, {"found": True, "rad": rstar, "mass": 1.0})
    assert fields["radius_label"].startswith(f"{rp_earth:.1f}")
    axis, note = orbit_au(365.25, 1.0)
    assert abs(axis - 1.0) < 0.05
    assert note == "from TIC stellar mass"
    axis2, note2 = orbit_au(10.0, None)
    assert axis2 is not None and note2 == "assumes Sun-like star"
    scale = to_scale_payload(period=10.0, depth=depth, tic_payload={"found": True, "rad": 1.0, "mass": 1.0})
    assert scale["candidate_radius_earth"] == fields["radius_earth"]
    assert scale["orbit_au"] is not None


def test_known_planet_marking_from_checks():
    checks = {
        "exofop": {"state": "fail", "summary": "Already a TOI", "detail": "TOI 1234.01"},
    }
    match = known_catalog_match(checks)
    assert match and match["kind"] == "known TOI"
    nea_checks = {
        "nea": {"state": "fail", "summary": "Known planet: Kepler-10 b", "detail": "Kepler-10 b P=0.8 d"},
    }
    match2 = known_catalog_match(nea_checks)
    assert match2 and match2["kind"] == "known planet"


def test_filter_buckets():
    assert filter_bucket("binary") == "false_positive"
    assert filter_bucket("known") == "known"
    assert filter_bucket("pending") == "under_review"


def test_detail_endpoint_and_thumbnail_cache(tmp_path):
    store = _store(tmp_path)
    admin = store.ensure_admin()
    app = App(store)
    tic = 594723
    try:
        _enroll_candidate(store, tic=tic, sector=14)
        store.save_catalog(tic, {
            "tic": {"ok": True, "payload": {"found": True, "rad": 0.9, "mass": 1.05}},
        })
        detail = json.loads(
            app.handle("GET", f"/api/v1/discoveries/{tic}", "", _admin_cookie(admin), b"").body.decode()
        )
        assert detail["tic"] == tic
        assert detail["to_scale"]["orbit_au"] is not None
        thumb = app.handle("GET", f"/api/v1/discoveries/{tic}/thumb.svg", "", _admin_cookie(admin), b"")
        assert thumb.status == 200
        assert thumb.content_type == "image/svg+xml"
        cache = thumb_cache_dir(store.path)
        assert (cache / f"tic-{tic}.svg").is_file()
        # second read hits disk
        body = read_or_write_thumbnail(cache, tic)
        assert b"<svg" in body
    finally:
        store.close()


def test_build_groups_integration_with_gallery(tmp_path):
    store = _store(tmp_path)
    try:
        _enroll_candidate(store, tic=777, sector=55, depth=0.008, sde=18)
        store.save_catalog(777, {
            "tic": {"ok": True, "payload": {"found": True, "rad": 1.0, "mass": 1.0}},
        })
        payload = store.discoveries_gallery(per_page=10)
        assert payload["total"] == 1
        row = payload["items"][0]
        assert row["tic"] == 777
        detail = build_detail_payload(store, 777)
        assert detail is not None
        assert detail["radius_label"] != "radius unknown"
    finally:
        store.close()