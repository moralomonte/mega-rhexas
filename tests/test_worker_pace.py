"""Animation speed does not change the worker. Pause is a separate switch."""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from rhexas.config import default_config
from rhexas.search import FIELDS, run
from rhexas.worker.dashboard import bind_server, make_handler
from rhexas.worker.live import Store, snapshot
from rhexas.worker.loop import current_pace, resolve_worker, run_worker

import threading


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
        self.submitted.append(assignment_id)
        self.assignment = None
        self.claimed = False
        return {"verdict": "pending"}

    def heartbeat(self, assignment_id, progress=None):
        return {"expires_at": "2026-01-01T00:00:00+00:00"}


def _post(port, path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_mode_post_leaves_effort_unchanged_and_hides_the_token(tmp_path, monkeypatch):
    monkeypatch.setattr("rhexas.worker.loop.apply_priority", lambda priority: 0)
    store = Store(tmp_path)
    store.set_worker(
        pid=os.getpid(), procs=2, threads=2, priority="low", max_cache_gb=20,
        coordinator_url="https://user:rhx_secretvalue@coord.example/v1",
    )
    httpd, port = bind_server("127.0.0.1", 0, make_handler(tmp_path, {}))
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        before = snapshot(tmp_path)["resources"]
        status, body = _post(port, "/api/mode", {"mode": "turbo", "token": "rhx_secretvalue"})
        assert status == 200
        assert b"rhx_" not in body
        assert json.loads(body)["effort"] == "unchanged"
        assert json.loads(body)["requested"] == "realtime"
        status, body = _post(port, "/api/mode", {"mode": "slow", "token": "rhx_secretvalue"})
        assert status == 200
        assert json.loads(body)["requested"] == "slow"
        status, body = _post(port, "/api/state", {"mode": "eco", "token": "rhx_secretvalue"})
        assert status == 405
        assert b"rhx_" not in body
        status, _body = _post(port, "/api/mode", {"mode": "halt", "token": "rhx_secretvalue"})
        assert status == 400
    finally:
        httpd.dashboard_stop.set()
        httpd.shutdown()
        thread.join(timeout=3)
        httpd.server_close()
    blob = b"".join(path.read_bytes() for path in tmp_path.glob("dashboard.sqlite*"))
    assert b"rhx_secretvalue" not in blob
    cfg = resolve_worker(
        default_config(tmp_path), procs=2, threads=2, priority="low", poll_s=60, environ={},
    )
    current_pace(cfg, store, {})
    snap = snapshot(tmp_path)
    # The post is stored for an old client. The worker does not apply it.
    assert store.requested_mode() == "turbo"
    assert snap["resources"]["mode"] == "live"
    assert snap["resources"]["procs"] == 2
    assert snap["resources"]["threads"] == 2
    assert snap["resources"]["priority"] == "low"
    assert snap["resources"]["paused"] is False
    assert snap["resources"]["user_paused"] is False
    assert cfg.worker.poll_s == 60
    store.request_mode("eco")
    current_pace(cfg, store, {})
    snap = snapshot(tmp_path)
    assert store.requested_mode() == "eco"
    assert snap["resources"]["mode"] == "live"
    assert snap["resources"]["procs"] == 2
    assert snap["resources"]["threads"] == 2
    assert snap["resources"]["priority"] == "low"
    assert snap["resources"]["paused"] is False
    from rhexas.worker.present import mission_state
    state = mission_state(snap)
    assert state["pace_mode"] == "live"
    assert "paused between stars" not in state["banner"]
    assert "rhx_" not in json.dumps(snap)


def test_picture_speed_does_not_change_search_effort(tmp_path, monkeypatch):
    monkeypatch.setattr("rhexas.worker.loop.apply_priority", lambda priority: 0)
    cache = tmp_path / "worker-cache"
    cache.mkdir()
    Store(cache).request_mode("turbo")
    seen = {}

    def download(cfg, tics):
        seen["threads"] = cfg.download.threads
        cfg.sector_data.mkdir(parents=True, exist_ok=True)

    def search(cfg, tics, procs):
        seen["procs"] = procs
        cfg.sector_results.mkdir(parents=True, exist_ok=True)
        lines = ["tic,period,sde,depth,snr,error"]
        for tic in sorted(tics):
            lines.append(f"{tic},1.5,10,0.01,9,")
        (cfg.sector_results / "bls_raw.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def filt(cfg, tics):
        (cfg.sector_results / "candidates.csv").write_text(
            "tic,period,depth,snr,sde,status,score,flags\n", encoding="utf-8",
        )

    assignment = {
        "assignment_id": 8, "unit_id": 1, "generation": 1, "sector": 107,
        "project": "exoplanets", "tics": [7], "n_tics": 1,
        "expires_at": "2026-01-02T00:00:00+00:00",
    }
    cfg = resolve_worker(
        default_config(tmp_path), token="rhx_test", procs=2, threads=2, poll_s=60, environ={},
    )
    client = _Coord(assignment)
    code = run_worker(cfg, once=True, client=client, download=download, search=search, filt=filt, heartbeat_s=0)
    assert code == 0
    assert seen["procs"] == 2
    assert seen["threads"] == 2
    assert client.submitted == [8]
    assert snapshot(cache)["resources"]["mode"] == "live"
    assert snapshot(cache)["resources"]["procs"] == 2
    assert snapshot(cache)["resources"]["paused"] is False
    assert cfg.worker.poll_s == 60
    assert b"rhx_" not in (cache / "worker.log").read_bytes()

    Store(cache).request_mode("eco")
    seen.clear()
    client = _Coord(dict(assignment))
    code = run_worker(
        cfg, once=True, client=client, download=download, search=search,
        filt=filt, heartbeat_s=0,
    )
    assert code == 0
    assert seen["procs"] == 2
    assert seen["threads"] == 2
    assert client.submitted == [8]
    assert Store(cache).requested_mode() == "eco"
    assert snapshot(cache)["resources"]["mode"] == "live"
    assert snapshot(cache)["resources"]["priority"] == "low"
    assert snapshot(cache)["resources"]["paused"] is False
    assert not (cache / "state.json").exists()


def test_pool_size_changes_at_the_next_wave(tmp_path, monkeypatch):
    cfg = default_config(tmp_path)
    folder = cfg.sector_data
    folder.mkdir(parents=True)
    for index in range(4):
        (folder / f"tess-000000000000000{index}-s_lc.fits").write_bytes(b"")
    opened = []

    class FakePool:
        def __init__(self, procs, initializer=None, initargs=()):
            opened.append(int(procs))

        def imap_unordered(self, fn, items, chunksize=1):
            for item in items:
                row = {name: "" for name in FIELDS}
                row["file"] = Path(item).name
                row["tic"] = "1"
                yield row

        def close(self):
            return None

        def join(self):
            return None

        def terminate(self):
            return None

    monkeypatch.setattr("rhexas.search.Pool", FakePool)
    monkeypatch.setattr("rhexas.search.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("rhexas.worker.priority.apply_priority", lambda priority: 0)
    calls = {"n": 0}

    def pace():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"paused": True, "procs": 1, "priority": "idle"}
        if calls["n"] == 2:
            return {"paused": False, "procs": 1, "priority": "low"}
        return {"paused": False, "procs": 3, "priority": "normal"}

    assert run(cfg, procs=2, pace=pace) == 0
    assert opened == [1, 3]
    text = (cfg.sector_results / "bls_raw.csv").read_text(encoding="utf-8")
    assert text.count("tess-") == 4


def test_pause_button_posts_and_the_pause_endpoint_toggles(tmp_path):
    page = (Path(__file__).resolve().parents[1] / "src" / "rhexas" / "worker" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="run-switch"' in page
    assert 'role="switch"' in page
    assert "/api/pause" in page
    assert "/api/status" in page
    assert "Pausing…" in page
    assert ">freeze log<" in page
    store = Store(tmp_path)
    httpd, port = bind_server("127.0.0.1", 0, make_handler(tmp_path, {}))
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        status, body = _post(port, "/api/pause", {"paused": True})
        assert status == 200
        assert json.loads(body) == {"ok": True, "paused": True}
        assert store.user_paused() is True
        status, body = _post(port, "/api/pause", {"paused": False})
        assert status == 200
        assert json.loads(body) == {"ok": True, "paused": False}
        assert store.user_paused() is False
        status, _body = _post(port, "/api/pause", {"paused": "yes"})
        assert status == 400
    finally:
        httpd.dashboard_stop.set()
        httpd.shutdown()
        thread.join(timeout=3)
        httpd.server_close()


def test_pause_switch_sits_in_the_top_bar_after_the_speed_buttons():
    page = (Path(__file__).resolve().parents[1] / "src" / "rhexas" / "worker" / "static" / "index.html").read_text(encoding="utf-8")
    line = next(l for l in page.splitlines() if 'id="speed"' in l)
    assert line.index('data-m="realtime"') < line.index('id="run-switch"')
    assert page.count('id="run-switch"') == 1
    assert "rhexas-anim" in page
    assert "rhexas.visual" in page
    assert "prefers-reduced-motion" in page
    assert "function pauseView(user,holding)" in page
    assert page.count("/api/mode") == 0


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
        return response.status, response.read()


def test_status_maps_pause_and_a_stale_hold_does_not_stick(tmp_path):
    from rhexas.worker.live import pause_view
    assert pause_view(False, False) == {"state": "running", "label": "Running", "on": True}
    assert pause_view(False, True)["state"] == "running"
    assert pause_view(True, False) == {"state": "pausing", "label": "Pausing…", "on": False}
    assert pause_view(True, True) == {"state": "paused", "label": "Paused", "on": False}
    store = Store(tmp_path)
    httpd, port = bind_server("127.0.0.1", 0, make_handler(tmp_path, {}))
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        status, body = _post(port, "/api/pause", {"paused": True})
        assert status == 200
        status, raw = _get(port, "/api/status")
        assert status == 200
        resources = json.loads(raw)["resources"]
        assert resources["user_paused"] is True
        assert resources["holding"] is False
        assert resources["pause"]["state"] == "pausing"
        store.set_holding(True)
        resources = json.loads(_get(port, "/api/status")[1])["resources"]
        assert resources["pause"]["state"] == "paused"
        _post(port, "/api/pause", {"paused": False})
        resources = json.loads(_get(port, "/api/status")[1])["resources"]
        assert resources["user_paused"] is False
        assert resources["pause"]["state"] == "running"
        assert resources["holding"] is False
    finally:
        httpd.dashboard_stop.set()
        httpd.shutdown()
        thread.join(timeout=3)
        httpd.server_close()


def test_pause_and_resume_during_download_and_search(tmp_path, monkeypatch):
    monkeypatch.setattr("rhexas.worker.loop.apply_priority", lambda priority: 0)
    cache = tmp_path / "worker-cache"
    cache.mkdir()
    store = Store(cache)
    download_entered = threading.Event()
    release_download = threading.Event()
    search_entered = threading.Event()
    search_saw_pause = threading.Event()
    release_search = threading.Event()

    def download(cfg, tics):
        from rhexas.worker.loop import _hold_fn
        download_entered.set()
        while not release_download.is_set():
            if _hold_fn:
                _hold_fn()
            time.sleep(0.02)
        cfg.sector_data.mkdir(parents=True, exist_ok=True)

    def search(cfg, tics, procs):
        from rhexas.worker.loop import _SearchHooks
        search_entered.set()
        pace = (_SearchHooks.current or {}).get("pace")
        while not release_search.is_set():
            directive = pace() if pace else {}
            if directive.get("paused"):
                mark = directive.get("mark_holding")
                if callable(mark):
                    mark(True)
                search_saw_pause.set()
            time.sleep(0.02)
        cfg.sector_results.mkdir(parents=True, exist_ok=True)
        (cfg.sector_results / "bls_raw.csv").write_text(
            "tic,period,sde,depth,snr,error\n7,1.5,10,0.01,9,\n", encoding="utf-8",
        )

    def filt(cfg, tics):
        (cfg.sector_results / "candidates.csv").write_text(
            "tic,period,depth,snr,sde,status,score,flags\n", encoding="utf-8",
        )

    assignment = {
        "assignment_id": 8, "unit_id": 1, "generation": 1, "sector": 107,
        "project": "exoplanets", "tics": [7], "n_tics": 1,
        "expires_at": "2026-01-02T00:00:00+00:00",
    }
    cfg = resolve_worker(default_config(tmp_path), token="rhx_test", procs=2, threads=2, environ={})
    client = _Coord(assignment)
    box = {}

    def runner():
        box["code"] = run_worker(
            cfg, once=True, client=client, download=download, search=search, filt=filt, heartbeat_s=0,
        )

    thread = threading.Thread(target=runner)
    thread.start()
    assert download_entered.wait(3)
    store.request_user_pause(True)
    deadline = time.time() + 3
    while time.time() < deadline and snapshot(cache)["resources"]["pause"]["state"] != "paused":
        time.sleep(0.02)
    assert snapshot(cache)["resources"]["holding"] is True
    assert snapshot(cache)["resources"]["user_paused"] is True
    assert not search_entered.is_set()
    store.request_user_pause(False)
    release_download.set()
    assert search_entered.wait(3)
    store.request_user_pause(True)
    assert search_saw_pause.wait(3)
    deadline = time.time() + 3
    while time.time() < deadline and snapshot(cache)["resources"]["holding"] is not True:
        time.sleep(0.02)
    assert snapshot(cache)["resources"]["pause"]["state"] == "paused"
    store.request_user_pause(False)
    release_search.set()
    thread.join(5)
    assert box["code"] == 0
    assert client.submitted == [8]
    assert snapshot(cache)["resources"]["pause"]["state"] == "running"


def test_search_pause_interrupts_a_running_star_and_resume_finishes(tmp_path, monkeypatch):
    import time as time_mod
    cfg = default_config(tmp_path)
    folder = cfg.sector_data
    folder.mkdir(parents=True)
    for index in range(3):
        (folder / f"tess-000000000000000{index}-s_lc.fits").write_bytes(b"")
    flag = {"paused": False}
    held = []
    entered = threading.Event()
    release = threading.Event()

    class BlockingPool:
        def __init__(self, procs, initializer=None, initargs=()):
            self.killed = threading.Event()

        def imap_unordered(self, fn, items, chunksize=1):
            killed = self.killed
            items = list(items)

            def gen():
                for item in items:
                    entered.set()
                    while not release.is_set():
                        if killed.wait(0.01):
                            raise RuntimeError("terminated")
                    row = {name: "" for name in FIELDS}
                    row["file"] = Path(item).name
                    row["tic"] = "1"
                    yield row
            return gen()

        def terminate(self):
            self.killed.set()

        def close(self):
            return None

        def join(self):
            return None

    monkeypatch.setattr("rhexas.search.Pool", BlockingPool)

    def pace():
        return {
            "paused": flag["paused"],
            "procs": 1,
            "priority": "low",
            "still_paused": lambda: flag["paused"],
            "mark_holding": lambda holding: held.append(bool(holding)),
        }

    box = {}

    def runner():
        box["code"] = run(cfg, procs=1, pace=pace)

    thread = threading.Thread(target=runner)
    thread.start()
    assert entered.wait(3)
    flag["paused"] = True
    deadline = time_mod.time() + 3
    while True not in held and time_mod.time() < deadline:
        time_mod.sleep(0.02)
    assert True in held
    text_mid = (cfg.sector_results / "bls_raw.csv").read_text(encoding="utf-8") if (cfg.sector_results / "bls_raw.csv").exists() else ""
    assert text_mid.count("tess-") == 0
    flag["paused"] = False
    release.set()
    thread.join(4)
    assert box["code"] == 0
    text = (cfg.sector_results / "bls_raw.csv").read_text(encoding="utf-8")
    assert text.count("tess-") == 3


def test_tray_icon_ships_as_package_data_and_has_a_fallback(tmp_path, monkeypatch):
    import tomllib
    from rhexas.desktop import tray
    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert "icons/*" in data["tool"]["setuptools"]["package-data"]["rhexas.desktop"]
    assert tray.icon_path() == tray._ICON
    fallback = tmp_path / "triskelion.ico"
    fallback.write_bytes(tray._ICON.read_bytes())
    monkeypatch.setattr(tray, "_ICON", tmp_path / "missing.ico")
    monkeypatch.setattr(tray, "_FALLBACK_ICON", fallback)
    assert tray.icon_path() == fallback
