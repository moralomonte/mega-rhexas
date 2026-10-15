"""Coordinator assignment, agreement, and auth. No MAST and no listening on a public port."""
import http.client
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rhexas.config import ConfigError
from rhexas.coord.cli import main as coord_main
from rhexas.coord.httpapi import App
from rhexas.coord.logic import chunk_tics, compare_star_lists, tics_from_curl_script
from rhexas.coord.server import make_server
from rhexas.coord.settings import Settings, load_coord_file
from rhexas.coord.store import CoordError, Store
from rhexas.worker.client import CoordinatorClient


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def _store(tmp_path, **kwargs):
    settings = Settings(**kwargs) if kwargs else Settings()
    store = Store(tmp_path / "coord.sqlite", settings, clock=Clock())
    store.init_schema()
    return store


def _star(tic, period=1.5, sde=10.0, depth=0.01, error=None):
    return {"tic": tic, "period": period, "sde": sde, "depth": depth, "error": error, "snr": 9.0, "n_transits": 4}


def _cand(tic, status="NEW"):
    return {"tic": tic, "period": 1.5, "depth": 0.01, "sde": 10.0, "snr": 9.0, "status": status, "score": 12.0, "flags": ""}


def _ids(store, *names):
    tokens = {name: store.create_worker(name) for name in names}
    return tokens, {name: store.authenticate(token)["id"] for name, token in tokens.items()}


def test_chunks_drop_duplicates_and_keep_order():
    chunks = chunk_tics([5, 5, 6] + list(range(7, 1205)), 500)
    assert [len(part) for part in chunks] == [500, 500, 200]
    assert chunks[0][0] == 5
    assert chunks[0][1] == 6
    assert 5 not in chunks[0][2:]


def test_curl_script_tics_stay_in_file_order():
    script = "\n".join(
        f'curl -L -o x "https://example.invalid/f?uri=mast/tess2026221103959-s0107-{tic:016d}-0309-s_lc.fits"'
        for tic in (9, 8, 7)
    )
    assert tics_from_curl_script(script) == [9, 8, 7]


def test_agreement_tolerances_are_the_documented_windows():
    settings = Settings()
    base = [_star(1, period=3.0, sde=10.0, depth=0.01)]
    ok, _ = compare_star_lists([1], base, [_star(1, period=3.005, sde=10.2, depth=0.01005)], settings)
    assert ok
    bad_period, _ = compare_star_lists([1], base, [_star(1, period=3.02)], settings)
    bad_sde, _ = compare_star_lists([1], base, [_star(1, sde=10.3)], settings)
    bad_depth, _ = compare_star_lists([1], base, [_star(1, depth=0.012)], settings)
    assert not bad_period and not bad_sde and not bad_depth
    tics = list(range(1, 101))
    left = [_star(tic) for tic in tics]
    right = [_star(tic) for tic in tics if tic > 2]
    assert compare_star_lists(tics, left, right, settings)[0]
    right = [_star(tic) for tic in tics if tic > 3]
    assert not compare_star_lists(tics, left, right, settings)[0]


def test_two_workers_share_a_unit_and_a_third_does_not(tmp_path):
    store = _store(tmp_path)
    try:
        store.enroll_tics(107, [1, 2, 3, 4], unit_size=2)
        tokens, ids = _ids(store, "ada", "bea", "cam")
        assert tokens["ada"].encode() not in (tmp_path / "coord.sqlite").read_bytes()
        first = store.claim(ids["ada"])
        second = store.claim(ids["bea"])
        third = store.claim(ids["cam"])
        assert first["tics"] == second["tics"] == [1, 2]
        assert first["assignment_id"] != second["assignment_id"]
        assert third["tics"] == [3, 4]
        assert store.claim(ids["ada"])["assignment_id"] == first["assignment_id"]
        assert store.authenticate("nope") is None
        assert store.authenticate(tokens["ada"])["name"] == "ada"
        store.revoke_worker("cam")
        assert store.authenticate(tokens["cam"]) is None
        public = store.workers_public()
        assert "cam" not in {row["name"] for row in public}
        assert "token" not in public[0]
    finally:
        store.close()


def test_agreeing_results_credit_both_workers_and_store_the_candidate(tmp_path):
    store = _store(tmp_path)
    try:
        store.enroll_tics(107, [1, 2], unit_size=500)
        _, ids = _ids(store, "ada", "bea")
        ada = store.claim(ids["ada"])
        bea = store.claim(ids["bea"])
        assert store.submit(ids["ada"], ada["assignment_id"], [_star(1), _star(2)], [_cand(1)])["verdict"] == "pending"
        done = store.submit(ids["bea"], bea["assignment_id"], [_star(1), _star(2)], [_cand(1), _cand(2, status="known_TOI")])
        assert done["verdict"] == "agreed"
        assert done["candidates"] == 1
        snap = store.status()
        assert snap["units_agreed"] == 1
        assert snap["tics_agreed"] == 2
        assert snap["candidates_new"] == 1
        by_name = {row["name"]: row for row in store.workers_public()}
        assert by_name["ada"]["stars_searched"] == 2
        assert by_name["bea"]["stars_searched"] == 2
        assert by_name["ada"]["candidates_found"] == 1
        assert by_name["bea"]["candidates_found"] == 1
        assert store.claim(ids["ada"]) is None
        assert store.candidates_public()[0]["tic"] == 1
    finally:
        store.close()


def test_disagreement_reissues_without_credit(tmp_path):
    store = _store(tmp_path)
    try:
        store.enroll_tics(96, [7], unit_size=500)
        _, ids = _ids(store, "ada", "bea", "cam")
        ada = store.claim(ids["ada"])
        bea = store.claim(ids["bea"])
        store.submit(ids["ada"], ada["assignment_id"], [_star(7, period=1.0)], [])
        result = store.submit(ids["bea"], bea["assignment_id"], [_star(7, period=9.0)], [])
        assert result["verdict"] == "disagreed"
        by_name = {row["name"]: row for row in store.workers_public()}
        assert by_name["ada"]["stars_searched"] == 0
        assert by_name["bea"]["candidates_found"] == 0
        again = store.claim(ids["cam"])
        assert again["generation"] == 2
        assert again["tics"] == [7]
        assert store.status()["units_agreed"] == 0
        rejected = [row for row in store.contributions() if row["outcome"] == "rejected"]
        assert len(rejected) == 2
        assert all(row["verified"] == 0 and row["project"] == "exoplanets" for row in rejected)
    finally:
        store.close()


def test_expired_lease_is_reissued_and_a_late_upload_is_rejected(tmp_path):
    store = _store(tmp_path, lease_s=3600, max_lease_s=7200)
    try:
        store.enroll_tics(105, [4, 5], unit_size=500)
        _, ids = _ids(store, "ada", "bea")
        ada = store.claim(ids["ada"])
        store.clock.now += timedelta(seconds=3601)
        bea = store.claim(ids["bea"])
        assert bea["tics"] == [4, 5]
        assert bea["assignment_id"] != ada["assignment_id"]
        try:
            store.submit(ids["ada"], ada["assignment_id"], [_star(4), _star(5)], [])
            raised = False
        except Exception as exc:
            raised = True
            assert getattr(exc, "status", None) == 409
        assert raised
        from rhexas.coord.store import parse_iso
        store.clock.now = parse_iso(bea["leased_at"]) + timedelta(seconds=7200)
        try:
            store.heartbeat(ids["bea"], bea["assignment_id"])
            raised = False
        except Exception as exc:
            raised = True
            assert getattr(exc, "status", None) == 409
        assert raised
    finally:
        store.close()


def test_http_auth_claim_and_admin_page(tmp_path):
    store = _store(tmp_path)
    httpd = None
    try:
        admin = store.ensure_admin()
        token = store.create_worker("ada")
        other = store.create_worker("bea")
        store.enroll_tics(107, [1, 2], unit_size=500)
        httpd = make_server(store, "127.0.0.1", 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]

        health, _, _ = _call(port, "GET", "/healthz")
        assert health == 200
        hidden, _, _ = _call(port, "GET", "/api/v1/status")
        assert hidden == 401
        denied, _, _ = _call(port, "GET", "/api/v1/workers", headers={"Authorization": f"Bearer {token}"})
        assert denied == 401
        bad, _, _ = _call(port, "POST", "/api/v1/work/claim", {}, headers={"Authorization": "Bearer rhx_nope"})
        assert bad == 401

        page, _, html = _call(port, "GET", "/")
        assert page == 200 and b"Mega Rhexas" in html
        logged, headers, _ = _call(port, "POST", "/login", {"token": admin})
        assert logged == 204
        cookie = next(value for key, value in headers.items() if key.lower() == "set-cookie").split(";", 1)[0]
        status, _, body = _call(port, "GET", "/api/v1/status", headers={"Cookie": cookie})
        assert status == 200
        assert json.loads(body)["units_total"] == 1

        listed, _, body = _call(port, "GET", "/api/v1/projects")
        assert listed == 200
        catalog = json.loads(body)["projects"]
        assert catalog[0]["name"] == "exoplanets"
        assert catalog[0]["replicas"] == 2
        missing, _, _ = _call(
            port, "POST", "/api/v1/projects/asteroids/tasks/claim", {},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert missing == 404
        scoped, _, body = _call(
            port, "GET", "/api/v1/projects/exoplanets/status", headers={"Cookie": cookie},
        )
        assert scoped == 200 and json.loads(body)["replicas"] == 2

        client = CoordinatorClient(f"http://127.0.0.1:{port}", token, timeout=10)
        assignment = client.claim()["assignment"]
        assert assignment["tics"] == [1, 2]
        assert assignment["project"] == "exoplanets"
        assert assignment["user_id"] == store.authenticate(token)["id"]
        legacy, _, raw = _call(
            port, "GET", "/api/v1/work/current", headers={"Authorization": f"Bearer {token}"},
        )
        assert legacy == 200
        assert json.loads(raw)["assignment"]["assignment_id"] == assignment["assignment_id"]
        second = CoordinatorClient(f"http://127.0.0.1:{port}", other, timeout=10).claim()["assignment"]
        stars = [_star(1), _star(2)]
        assert client.submit(assignment["assignment_id"], stars, [_cand(1)])["verdict"] == "pending"
        assert CoordinatorClient(f"http://127.0.0.1:{port}", other, timeout=10).submit(
            second["assignment_id"], stars, [_cand(1)]
        )["verdict"] == "agreed"
        status, _, body = _call(port, "GET", "/api/v1/candidates", headers={"Cookie": cookie})
        assert json.loads(body)["candidates"][0]["tic"] == 1
        store.revoke_worker("ada")
        try:
            client.current()
            assert False, "revoked token should fail"
        except Exception as exc:
            assert exc.status == 401
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        store.close()


def test_coord_toml_bom_and_cli_round_trip(tmp_path, capsys):
    path = tmp_path / "coord.toml"
    path.write_bytes(b"\xef\xbb\xbf" + b'[coord]\nport = 8799\ndb = "coord.sqlite"\n')
    loaded = load_coord_file(path)
    assert loaded.port == 8799
    assert coord_main(["init", "--config", str(path)]) == 0
    token_file = tmp_path / "admin.token"
    assert token_file.read_text(encoding="utf-8").startswith("rhx_")
    tics = tmp_path / "batch.txt"
    tics.write_text("# batch\n10, 11\n12\n", encoding="utf-8")
    assert coord_main(["--config", str(path), "enroll", "--sector", "42", "--tics-file", str(tics), "--unit-size", "2"]) == 0
    capsys.readouterr()
    assert coord_main(["--config", str(path), "token", "create", "ada"]) == 0
    printed = capsys.readouterr().out
    assert "rhx_" in printed
    assert "ada" in printed
    assert coord_main(["--config", str(path), "status"]) == 0
    summary = capsys.readouterr().out
    assert "3" in summary or "stars 0/3" in summary


def test_units_carry_project_and_verified_credit(tmp_path):
    store = _store(tmp_path)
    try:
        store.enroll_tics(107, [1, 2], unit_size=500)
        _, ids = _ids(store, "ada", "bea")
        ada = store.claim(ids["ada"])
        assert ada["project"] == "exoplanets"
        assert ada["user_id"] == ids["ada"]
        assert ada["payload"]["tics"] == [1, 2]
        assert store.status()["replicas"] == 2
        bea = store.claim(ids["bea"])
        stars = [_star(1), _star(2)]
        store.submit(ids["ada"], ada["assignment_id"], stars, [_cand(1)])
        store.submit(ids["bea"], bea["assignment_id"], stars, [_cand(1)])
        agreed = [row for row in store.contributions() if row["outcome"] == "agreed"]
        assert {row["user_id"] for row in agreed} == {ids["ada"], ids["bea"]}
        assert all(row["verified"] == 1 and row["project"] == "exoplanets" for row in agreed)
        assert any(row["outcome"] == "submitted" and row["verified"] == 0 for row in store.contributions())
    finally:
        store.close()


def test_one_replica_accepts_a_single_upload(tmp_path):
    store = _store(tmp_path, replicas=1)
    try:
        store.enroll_tics(107, [1], unit_size=500)
        _, ids = _ids(store, "ada", "bea")
        ada = store.claim(ids["ada"])
        done = store.submit(ids["ada"], ada["assignment_id"], [_star(1)], [_cand(1)])
        assert done["verdict"] == "agreed"
        assert done["candidates"] == 1
        assert store.claim(ids["bea"]) is None
        verified = [row for row in store.contributions() if row["verified"] == 1]
        assert len(verified) == 1
        assert verified[0]["user_id"] == ids["ada"]
        assert verified[0]["outcome"] == "agreed"
        assert store.status()["replicas"] == 1
        assert store.status()["units_agreed"] == 1
    finally:
        store.close()


def test_unknown_project_is_rejected(tmp_path):
    store = _store(tmp_path)
    path = tmp_path / "coord.toml"
    try:
        _, ids = _ids(store, "ada")
        try:
            store.claim(ids["ada"], project="asteroids")
            assert False, "unknown project should fail"
        except CoordError as exc:
            assert exc.status == 404
        path.write_text("[coord]\nreplicas = 0\n", encoding="utf-8")
        try:
            load_coord_file(path)
            assert False, "replicas = 0 should fail"
        except ConfigError:
            pass
        path.write_text("[coord]\nreplicas = 1\n", encoding="utf-8")
        assert load_coord_file(path).settings.replicas == 1
    finally:
        store.close()


def test_status_page_contains_triskelion(tmp_path):
    """Status page inlines the dashboard triskelion and the coordinator serves its favicon."""
    root = Path(__file__).resolve().parents[1]
    mark = (root / "dashboard" / "static" / "img" / "triskelion.svg").read_text(encoding="utf-8")
    start = mark.index('d="') + 3
    glyph = mark[start:mark.index('"', start)]
    favicon = (root / "dashboard" / "static" / "img" / "favicon.svg").read_bytes()
    store = _store(tmp_path)
    try:
        app = App(store)
        page = app.handle("GET", "/", "", {}, b"")
        assert page.status == 200
        html = page.body.decode("utf-8")
        assert glyph in html
        assert "animation:trspin 12s linear infinite" in html
        assert "@keyframes trspin{to{transform:rotate(360deg)}}" in html
        assert "drop-shadow(0 0 8px #818cf866)" in html
        assert "drop-shadow(0 0 14px #a5b4fcaa)" in html
        assert "prefers-reduced-motion:reduce" in html
        assert ".trisk{animation:none!important}" in html
        assert 'rel="icon"' in html and 'href="/favicon.svg"' in html
        icon = app.handle("GET", "/favicon.svg", "", {}, b"")
        assert icon.status == 200
        assert icon.content_type == "image/svg+xml"
        assert icon.body == favicon
        assert glyph in icon.body.decode("utf-8")
    finally:
        store.close()


def _call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = None if body is None else json.dumps(body).encode()
    hdrs = {"Accept": "application/json"}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    conn.request(method, path, body=data, headers=hdrs)
    response = conn.getresponse()
    raw = response.read()
    found = {key: value for key, value in response.getheaders()}
    conn.close()
    return response.status, found, raw
