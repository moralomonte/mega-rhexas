"""Speed buttons change the worker's load. Nothing here contacts a coordinator."""
import json
import os
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


def test_mode_post_changes_effective_settings_and_hides_the_token(tmp_path, monkeypatch):
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
        status, body = _post(port, "/api/mode", {"mode": "turbo", "token": "rhx_secretvalue"})
        assert status == 200
        assert b"rhx_" not in body
        assert json.loads(body) == {"ok": True, "requested": "turbo"}
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
    cfg = resolve_worker(default_config(tmp_path), procs=2, threads=2, priority="low", environ={})
    current_pace(cfg, store, {})
    snap = snapshot(tmp_path)
    assert snap["resources"]["mode"] == "turbo"
    assert snap["resources"]["procs"] == 4
    assert snap["resources"]["threads"] == 4
    assert snap["resources"]["priority"] == "normal"
    assert snap["resources"]["paused"] is False
    store.request_mode("eco")
    current_pace(cfg, store, {})
    snap = snapshot(tmp_path)
    assert snap["resources"]["mode"] == "eco"
    assert snap["resources"]["procs"] == 1
    assert snap["resources"]["priority"] == "idle"
    assert snap["resources"]["paused"] is True
    from rhexas.worker.present import mission_state
    state = mission_state(snap)
    assert state["pace_mode"] == "eco"
    assert "paused between stars" in state["banner"]
    assert "rhx_" not in json.dumps(snap)


def test_turbo_reaches_the_search_and_eco_keeps_the_lease(tmp_path, monkeypatch):
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
    cfg = resolve_worker(default_config(tmp_path), token="rhx_test", procs=2, threads=2, environ={})
    client = _Coord(assignment)
    code = run_worker(cfg, once=True, client=client, download=download, search=search, filt=filt, heartbeat_s=0)
    assert code == 0
    assert seen["procs"] == 4
    assert seen["threads"] == 4
    assert client.submitted == [8]
    assert snapshot(cache)["resources"]["mode"] == "turbo"
    assert b"rhx_" not in (cache / "worker.log").read_bytes()

    Store(cache).request_mode("eco")
    held = {}

    def download_later(cfg, tics):
        cfg.sector_data.mkdir(parents=True, exist_ok=True)

    def search_later(cfg, tics, procs):
        held["procs"] = procs
        cfg.sector_results.mkdir(parents=True, exist_ok=True)
        (cfg.sector_results / "bls_raw.csv").write_text(
            "tic,period,sde,depth,snr,error\n7,1.5,10,0.01,9,\n", encoding="utf-8",
        )

    def sleeper(_seconds):
        state = json.loads((cache / "state.json").read_text(encoding="utf-8"))
        resources = snapshot(cache)["resources"]
        held["during"] = {
            "assignment_id": state["assignment_id"],
            "stage": state["stage"],
            "mode": resources["mode"],
            "procs": resources["procs"],
            "paused": resources["paused"],
            "submitted": list(client.submitted),
        }
        Store(cache).request_mode("live")

    client = _Coord(dict(assignment))
    code = run_worker(
        cfg, once=True, client=client, download=download_later, search=search_later,
        filt=filt, heartbeat_s=0, sleeper=sleeper,
    )
    assert code == 0
    assert held["during"]["assignment_id"] == 8
    assert held["during"]["stage"] == "claimed"
    assert held["during"]["mode"] == "eco"
    assert held["during"]["procs"] == 1
    assert held["during"]["paused"] is True
    assert held["during"]["submitted"] == []
    assert held["procs"] == 2
    assert client.submitted == [8]
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
