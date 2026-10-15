"""Local worker dashboard. Nothing here contacts MAST or a coordinator."""
import json
import socket
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from rhexas.cli import build_parser
from rhexas.config import FilterConfig, default_config
from rhexas.worker.dashboard import bind_server, make_handler, resolve_bind
from rhexas.worker.live import (
    CANDIDATE_LABEL,
    DB_NAME,
    Store,
    fold_curve,
    numeric_pass,
    public_coordinator_url,
    publish_star,
    snapshot,
)
from rhexas.worker.loop import resolve_worker, run_worker


@pytest.fixture(autouse=True)
def _close_child_connection():
    yield
    from rhexas.worker import live as live_mod
    conn = live_mod._CHILD.get("conn")
    if conn is not None:
        conn.close()
        live_mod._CHILD["conn"] = None
        live_mod._CHILD["path"] = None


def _keys(value, found):
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(str(key).lower())
            _keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _keys(item, found)


def _passing_row(**extra):
    row = {
        "tic": 42,
        "file": "star.fits",
        "period": 2.5,
        "t0": 100.0,
        "duration": 0.1,
        "depth": 0.01,
        "snr": 12.0,
        "sde": 9.0,
        "n_transits": 4,
        "odd_even_sigma": 0.4,
        "sec_sigma": 0.2,
    }
    row.update(extra)
    return row


def _curve():
    import numpy as np
    time_values = np.linspace(100.0, 127.0, 360)
    phase = np.mod(time_values - 100.0, 2.5) / 2.5
    phase = np.where(phase > 0.5, phase - 1.0, phase)
    flux = np.where(np.abs(phase) < 0.02, 0.99, 1.0)
    return time_values, flux


class _Coord:
    def __init__(self, assignment):
        self.assignment = assignment
        self.submitted = []
        self.claimed = False

    def current(self):
        if self.claimed and self.assignment is not None:
            return {"assignment": self.assignment}
        return {"assignment": None}

    def claim(self):
        self.claimed = True
        return {"assignment": self.assignment}

    def submit(self, assignment_id, stars, candidates):
        self.submitted.append({"assignment_id": assignment_id, "stars": stars, "candidates": candidates})
        self.assignment = None
        self.claimed = False
        return {"verdict": "pending", "token": "rhx_test"}

    def heartbeat(self, assignment_id):
        return {"expires_at": "2026-01-01T00:00:00+00:00"}


def test_numeric_pass_and_fold_keep_a_dip_at_phase_zero():
    limits = {
        "sde_min": 6, "snr_min": 7, "min_transits": 3, "max_depth": 0.05,
        "odd_even_max_sigma": 3, "secondary_max_sigma": 3, "max_duration_fraction": 0.12,
    }
    assert numeric_pass(_passing_row(), limits)
    assert not numeric_pass(_passing_row(snr=1), limits)
    assert not numeric_pass({"tic": 1, "period": 1.0}, limits)
    time_values, flux = _curve()
    phase, folded = fold_curve(time_values, flux, 2.5, 100.0)
    assert phase and folded
    assert min(phase) >= -0.5 and max(phase) <= 0.5
    center = folded[min(range(len(phase)), key=lambda i: abs(phase[i]))]
    assert center < 0.995
    assert public_coordinator_url("https://user:rhx_secret@api.example/v1?x=1") == "https://api.example/v1"


def test_publish_star_writes_a_curve_and_an_unverified_candidate(tmp_path):
    spec = {"db": str(tmp_path / DB_NAME), "project": "exoplanets", "sector": 107, "unit_id": 3}
    spec.update({
        "sde_min": 6, "snr_min": 7, "min_transits": 3, "max_depth": 0.05,
        "odd_even_max_sigma": 3, "secondary_max_sigma": 3, "max_duration_fraction": 0.12,
    })
    time_values, flux = _curve()
    publish_star(spec, _passing_row(), time_values, flux, processing=True)
    mid = snapshot(tmp_path)
    assert mid["current_star"]["status"] == "processing"
    assert mid["current_star"]["time"]
    assert mid["candidates"] == []
    publish_star(spec, _passing_row(), time_values, flux, processing=False)
    publish_star(spec, _passing_row(tic=99, snr=1), time_values, flux, processing=False)
    snap = snapshot(tmp_path)
    assert snap["current_star"]["tic"] == 99
    assert snap["current_star"]["flux"]
    assert [row["tic"] for row in snap["candidates"]] == [42]
    hit = snap["candidates"][0]
    assert hit["label"] == CANDIDATE_LABEL
    assert hit["period"] == pytest.approx(2.5)
    assert hit["depth"] == pytest.approx(0.01)
    assert hit["snr"] == pytest.approx(12)
    assert hit["phase"] and hit["folded"]
    assert b"rhx_" not in (tmp_path / DB_NAME).read_bytes()


def test_lifetime_counters_and_redaction(tmp_path):
    store = Store(tmp_path)
    store.set_worker(
        pid=0, procs=2, threads=2, priority="low", max_cache_gb=20,
        coordinator_url="https://api.megarhexas.com",
    )
    store.set_connection(True, "connected rhx_secretvalue")
    store.note_submit({"verdict": "agreed", "token": "rhx_secretvalue"}, 4)
    store.note_submit({"verdict": "pending"}, 1)
    store.note_submit({"verdict": "disagreed"}, 2)
    blob = b"".join(path.read_bytes() for path in tmp_path.glob("dashboard.sqlite*"))
    assert b"rhx_secretvalue" not in blob
    snap = snapshot(tmp_path)
    life = snap["lifetime"]
    assert life == {
        "stars_searched": 7,
        "batches_submitted": 3,
        "batches_agreed": 1,
        "batches_verified": 1,
        "contributions_pending": 1,
        "contributions_verified": 1,
    }
    assert "rhx_secretvalue" not in snap["connection"]["detail"]
    assert "rhx_redacted" in snap["connection"]["detail"]
    assert snap["resources"]["max_cache_gb"] == 20
    assert snap["resources"]["procs"] == 2
    keys = set()
    _keys(snap, keys)
    assert "token" not in keys


def test_worker_writes_the_state_file_and_never_the_token(tmp_path, monkeypatch):
    monkeypatch.setattr("rhexas.worker.loop.apply_priority", lambda priority: 0)

    def download(cfg, tics):
        cfg.sector_data.mkdir(parents=True, exist_ok=True)

    def search(cfg, tics, procs):
        cfg.sector_results.mkdir(parents=True, exist_ok=True)
        lines = ["tic,period,sde,depth,snr,error"]
        for tic in sorted(tics):
            lines.append(f"{tic},1.5,10,0.01,9,")
        (cfg.sector_results / "bls_raw.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def filt(cfg, tics):
        (cfg.sector_results / "candidates.csv").write_text(
            "tic,period,depth,snr,sde,status,score,flags\n7,1.5,0.01,9,10,NEW,12,\n",
            encoding="utf-8",
        )

    assignment = {
        "assignment_id": 8,
        "unit_id": 1,
        "generation": 1,
        "sector": 107,
        "project": "exoplanets",
        "tics": [7, 8],
        "n_tics": 2,
        "expires_at": "2026-01-02T00:00:00+00:00",
    }
    cfg = resolve_worker(default_config(tmp_path), token="rhx_test", procs=2, threads=2, environ={})
    code = run_worker(
        cfg, once=True, client=_Coord(assignment), download=download, search=search, filt=filt, heartbeat_s=0,
    )
    assert code == 0
    cache = tmp_path / "worker-cache"
    assert (cache / DB_NAME).is_file()
    assert not (cache / "state.json").exists()
    blob = b"".join(path.read_bytes() for path in cache.glob("dashboard.sqlite*"))
    assert b"rhx_test" not in blob
    log = (cache / "worker.log").read_text(encoding="utf-8")
    assert "rhx_test" not in log
    assert "coordinator" in log
    snap = snapshot(cache)
    assert "rhx_test" not in json.dumps(snap)
    batch = snap["batch"]
    assert batch["project"] == "exoplanets"
    assert batch["sector"] == 107
    assert batch["unit_id"] == 1
    assert batch["stars_done"] == 2
    assert batch["stars_total"] == 2
    assert batch["stage"] == "uploaded"
    life = snap["lifetime"]
    assert life["stars_searched"] == 2
    assert life["batches_submitted"] == 1
    assert life["batches_agreed"] == 0
    assert life["batches_verified"] == 0
    assert life["contributions_pending"] == 1
    assert life["contributions_verified"] == 0
    assert snap["candidates"][0]["tic"] == 7
    assert snap["candidates"][0]["period"] == pytest.approx(1.5)
    assert snap["candidates"][0]["depth"] == pytest.approx(0.01)
    assert snap["candidates"][0]["snr"] == pytest.approx(9)
    assert snap["candidates"][0]["label"] == CANDIDATE_LABEL
    assert snap["resources"]["procs"] == 2
    assert snap["resources"]["threads"] == 2
    assert snap["resources"]["max_cache_gb"] == 20
    assert snap["connection"]["ok"] is True
    keys = set()
    _keys(snap, keys)
    assert "token" not in keys


def _start(cache):
    httpd, port = bind_server("127.0.0.1", 0, make_handler(cache, {"max_cache_gb": 20, "procs": 2, "threads": 2}))
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    return httpd, port, thread


def _stop(httpd, thread):
    httpd.dashboard_stop.set()
    httpd.shutdown()
    thread.join(timeout=3)
    httpd.server_close()


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
        body = response.read()
        ctype = response.headers.get("Content-Type", "")
    return ctype, body


def test_dashboard_endpoints_return_json_and_hide_the_token(tmp_path):
    store = Store(tmp_path)
    store.begin_batch({
        "project": "exoplanets", "sector": 96, "unit_id": 4, "assignment_id": 9,
        "n_tics": 10, "tics": [1],
    }, "searching")
    store.update_batch(stars_done=3, stars_total=10, per_s=0.5, eta_min=12.0, progress_kind="stars")
    store.set_worker(pid=0, procs=2, threads=2, priority="low", max_cache_gb=20, coordinator_url="https://api.megarhexas.com")
    store.record_candidates(
        {"project": "exoplanets", "sector": 96, "unit_id": 4},
        [{"tic": 15, "period": 3.25, "depth": 0.002, "snr": 11.5, "sde": 8}],
    )
    conn = sqlite3.connect(tmp_path / DB_NAME)
    conn.execute(
        """INSERT INTO kv (key, value) VALUES ('connection', ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        ('{"ok": true, "detail": "rhx_secretvalue", "token": "rhx_secretvalue"}',),
    )
    conn.commit()
    conn.close()
    (tmp_path / "worker.log").write_text("searching TIC 15\nrhx_secretvalue\n", encoding="utf-8")
    secret = b"rhx_secretvalue"
    httpd, port, thread = _start(tmp_path)
    try:
        ctype, body = _get(port, "/api/state")
        assert ctype.startswith("application/json")
        assert secret not in body
        payload = json.loads(body)
        assert payload["batch"]["project"] == "exoplanets"
        assert payload["batch"]["sector"] == 96
        assert payload["batch"]["unit_id"] == 4
        assert payload["batch"]["stars_done"] == 3
        assert payload["batch"]["stars_total"] == 10
        assert payload["batch"]["eta_min"] == pytest.approx(12)
        assert payload["batch"]["per_s"] == pytest.approx(0.5)
        hit = payload["candidates"][0]
        assert hit["tic"] == 15
        assert hit["period"] == pytest.approx(3.25)
        assert hit["depth"] == pytest.approx(0.002)
        assert hit["snr"] == pytest.approx(11.5)
        assert hit["label"] == CANDIDATE_LABEL
        assert payload["resources"]["max_cache_gb"] == 20
        assert payload["resources"]["procs"] == 2
        assert "rhx_redacted" in payload["connection"]["detail"]
        assert any("searching TIC 15" in line for line in payload["log_tail"])
        keys = set()
        _keys(payload, keys)
        assert "token" not in keys

        ctype, log_body = _get(port, "/api/log")
        assert ctype.startswith("application/json")
        assert secret not in log_body
        log_payload = json.loads(log_body)
        assert any("searching TIC 15" in line for line in log_payload["lines"])
        assert any("rhx_redacted" in line for line in log_payload["lines"])

        ctype, page = _get(port, "/")
        assert ctype.startswith("text/html")
        text = page.decode("utf-8")
        assert "candidates, unverified" in text
        assert "EventSource" in text
        assert "triskelion" in text
        assert "discovery" not in text.lower()
        assert secret not in page

        ctype, events = _get(port, "/events?once=1")
        assert "text/event-stream" in ctype
        assert events.startswith(b"data: ")
        assert secret not in events
        event = json.loads(events.split(b"\n", 1)[0][len(b"data: "):])
        assert event["batch"]["unit_id"] == 4
        assert event["candidates"][0]["label"] == CANDIDATE_LABEL
    finally:
        _stop(httpd, thread)


def test_busy_port_is_skipped_and_the_default_bind_is_loopback():
    host, port = resolve_bind(None, None)
    assert (host, port) == ("127.0.0.1", 8080)
    assert resolve_bind("  ", None) == ("127.0.0.1", 8080)
    assert resolve_bind("0.0.0.0", 9090) == ("0.0.0.0", 9090)
    with pytest.raises(ValueError):
        resolve_bind("127.0.0.1", 70000)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    busy = sock.getsockname()[1]
    try:
        httpd, chosen = bind_server("127.0.0.1", busy, make_handler(".", {}))
    finally:
        sock.close()
    try:
        assert chosen != busy
        assert busy < chosen <= busy + 49
    finally:
        httpd.server_close()


def test_cli_accepts_dashboard_flags_without_a_token_flag_on_attach():
    worker = build_parser().parse_args([
        "worker", "--dashboard", "--dashboard-port", "9091", "--dashboard-host", "0.0.0.0", "--no-browser", "--once",
    ])
    assert worker.dashboard is True
    assert worker.dashboard_port == 9091
    assert worker.dashboard_host == "0.0.0.0"
    assert worker.no_browser is True
    attached = build_parser().parse_args([
        "worker-dashboard", "--cache-dir", "worker-cache", "--dashboard-port", "9092", "--no-browser",
    ])
    assert attached.cmd == "worker-dashboard"
    assert attached.dashboard_port == 9092
    assert attached.cache_dir == "worker-cache"
    assert not hasattr(attached, "token")
    plain = build_parser().parse_args(["worker", "--once"])
    assert plain.dashboard is False
    assert plain.dashboard_port is None
    assert plain.dashboard_host is None


def test_spawn_pool_installs_the_live_spec_in_the_child():
    """The dashboard search path must reach the child without inheriting parent handles."""
    from multiprocessing import get_context

    from rhexas.config import SearchConfig
    from rhexas.search import _installed_live, _pool_init

    spec = {"cache": str(Path("/tmp/rhexas-live-spec")), "project": "tess", "sector": 1, "unit_id": 7}
    ctx = get_context("spawn")
    with ctx.Pool(1, initializer=_pool_init, initargs=(SearchConfig(), spec)) as pool:
        installed = pool.apply(_installed_live)
    assert installed == spec
    assert _installed_live() is None


def test_download_watcher_reports_file_rate_and_eta(tmp_path):
    from rhexas.worker.loop import _watch_downloads

    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "a_lc.fits").write_bytes(b"x")
    (folder / "b_lc.fits").write_bytes(b"x")
    store = Store(tmp_path)
    stop = threading.Event()
    thread = threading.Thread(target=_watch_downloads, args=(store, folder, 4, stop), daemon=True)
    thread.start()
    deadline = time.time() + 5
    batch = {}
    while time.time() < deadline:
        batch = snapshot(tmp_path)["batch"] or {}
        if batch.get("stars_done") == 2 and batch.get("per_s"):
            break
        time.sleep(0.05)
    stop.set()
    thread.join(timeout=3)
    assert batch["stage"] == "downloading"
    assert batch["progress_kind"] == "files"
    assert batch["stars_done"] == 2
    assert batch["stars_total"] == 4
    assert batch["per_s"] > 0
    assert batch["eta_min"] is not None and batch["eta_min"] >= 0


def test_page_reuses_the_mission_control_screen():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "src" / "rhexas" / "worker" / "static" / "index.html").read_text(encoding="utf-8")
    original = (Path(__file__).resolve().parents[1] / "dashboard" / "static" / "index.html").read_text(encoding="utf-8")
    for marker in ('id="sky"', 'id="pgc"', 'id="hist"', 'id="lcc"', 'id="fdc"', "trspin", "Eco", "auto-scroll", "EventSource"):
        assert marker in text
        assert marker in original
    assert "candidates, unverified" in text
    assert "No coins" in text
    assert "discovery" not in text.lower()
    assert "passes vetting" not in text
    assert "★ NEW" not in text
    assert "getImageData" not in text
    assert "/api/cutout" not in text


def test_mission_events_follow_the_worker_snapshot(tmp_path):
    from rhexas.worker.present import detail_payload, mission_cands, mission_lc, mission_state

    spec = {"db": str(tmp_path / DB_NAME), "project": "exoplanets", "sector": 82, "unit_id": 14}
    spec.update({
        "sde_min": 6, "snr_min": 7, "min_transits": 3, "max_depth": 0.05,
        "odd_even_max_sigma": 3, "secondary_max_sigma": 3, "max_duration_fraction": 0.12,
    })
    time_values, flux = _curve()
    row = _passing_row(ra=82.5, dec=-12.25, tmag=9.4, _pgram_p=[1.0, 2.5, 4.0], _pgram_w=[0.2, 9.0, 0.4])
    publish_star(spec, row, time_values, flux, processing=False)
    snap = snapshot(tmp_path)
    state = mission_state(snap)
    assert state["stage"] == "Starting"
    assert state["new_counts"]["total"] == 1
    assert "coordinator" in state["coord_line"]
    assert "discovery" not in json.dumps(state).lower()
    lc = mission_lc(snap["current_star"])
    assert lc["passed"] is True
    assert lc["status"] == CANDIDATE_LABEL
    assert lc["pgram"][0][1] == pytest.approx(2.5)
    assert lc["fold"][0]
    assert lc["ra"] == pytest.approx(82.5)
    cands = mission_cands(snap)
    assert cands[0]["label"] == CANDIDATE_LABEL
    assert cands[0]["status"] == "NEW"
    detail = detail_payload(snap, 42)
    assert detail["vet"]["verdict"] == CANDIDATE_LABEL
    assert detail["odd"] and detail["even"]
    assert detail["img"] is None
    blob = json.dumps(detail)
    assert "rhx_" not in blob
    assert "token" not in blob
    limits = {name: getattr(FilterConfig(), name) for name in (
        "sde_min", "snr_min", "min_transits", "max_depth", "odd_even_max_sigma",
        "secondary_max_sigma", "max_duration_fraction",
    )}
    assert numeric_pass(_passing_row(), limits)
