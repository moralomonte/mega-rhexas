"""Download-ahead: the next batch fetches while the current one is searched."""
import json
import threading
import time
from pathlib import Path

from rhexas.cli import build_parser
from rhexas.config import default_config
from rhexas.worker.live import Store
from rhexas.worker.loop import (
    WorkerStopped,
    prefetch_block,
    prefetch_hold,
    resolve_worker,
    run_worker,
)
from rhexas.worker.present import mission_state, next_line


def _batch(assignment_id, tics):
    tics = list(tics)
    return {
        "assignment_id": assignment_id,
        "unit_id": assignment_id,
        "generation": 1,
        "sector": 107,
        "tics": tics,
        "n_tics": len(tics),
        "project": "exoplanets",
        "expires_at": "2026-01-02T00:00:00+00:00",
    }


def _cfg(tmp_path, **kwargs):
    cfg = default_config(tmp_path)
    base = {"token": "rhx_test", "procs": 2, "threads": 2, "poll_s": 0.2, "environ": {}}
    base.update(kwargs)
    return resolve_worker(cfg, **base)


def _write_bls(cfg, tics):
    cfg.sector_results.mkdir(parents=True, exist_ok=True)
    with (cfg.sector_results / "bls_raw.csv").open("w", encoding="utf-8", newline="") as handle:
        handle.write("tic,period,sde,depth,snr,error\n")
        for tic in sorted(tics):
            handle.write(f"{tic},1.5,10,0.01,9,\n")


def _write_candidates(cfg):
    cfg.sector_results.mkdir(parents=True, exist_ok=True)
    (cfg.sector_results / "candidates.csv").write_text("tic,status\n", encoding="utf-8")


class QueueCoord:
    """In-memory coordinator. A prefetch claim may hold a second lease."""

    def __init__(self, batches):
        self._lock = threading.Lock()
        self._queue = list(batches)
        self.held = []
        self.claims = []
        self.submitted = []
        self.released = []
        self.beats = []

    def current(self):
        with self._lock:
            held = list(self.held)
        return {"assignment": held[0] if held else None, "assignments": held}

    def claim(self, prefetch=False):
        with self._lock:
            self.claims.append(bool(prefetch))
            if not prefetch and self.held:
                return {"assignment": self.held[0]}
            if prefetch and len(self.held) >= 2:
                return {"assignment": None}
            if not self._queue:
                return {"assignment": None}
            nxt = self._queue.pop(0)
            self.held.append(nxt)
            return {"assignment": nxt}

    def submit(self, assignment_id, stars, candidates):
        with self._lock:
            self.submitted.append(int(assignment_id))
            self.held = [item for item in self.held if int(item["assignment_id"]) != int(assignment_id)]
        return {"verdict": "pending"}

    def release(self, assignment_id):
        with self._lock:
            self.released.append(int(assignment_id))
            self.held = [item for item in self.held if int(item["assignment_id"]) != int(assignment_id)]
        return {"status": "released"}

    def heartbeat(self, assignment_id, progress=None):
        with self._lock:
            self.beats.append(int(assignment_id))
        return {"expires_at": "2026-01-01T00:00:00+00:00"}

    def snapshot(self):
        with self._lock:
            return {
                "claims": list(self.claims),
                "submitted": list(self.submitted),
                "released": list(self.released),
                "beats": list(self.beats),
            }


def test_prefetch_overlaps_the_next_download_with_search(tmp_path, monkeypatch):
    monkeypatch.setattr("rhexas.worker.loop.apply_priority", lambda priority: 0)
    client = QueueCoord([_batch(1, [1, 2]), _batch(2, [3, 4])])
    search_started = threading.Event()
    download_started = threading.Event()
    quit_event = threading.Event()
    downloads = []
    searches = []
    notes = []

    def download(cfg, tics):
        found = frozenset(tics)
        downloads.append(found)
        if found == frozenset({3, 4}):
            if not search_started.wait(8):
                notes.append("download started before the search was ready to overlap")
            download_started.set()
            from rhexas.worker.live import snapshot
            line = mission_state(snapshot(tmp_path / "worker-cache")).get("next_line") or ""
            notes.append(line)
        cfg.sector_data.mkdir(parents=True, exist_ok=True)

    def search(cfg, tics, procs):
        found = frozenset(tics)
        searches.append(found)
        if found == frozenset({1, 2}):
            search_started.set()
            if not download_started.wait(8):
                notes.append("search finished before the next batch downloaded")
                quit_event.set()
        else:
            if downloads.count(frozenset({3, 4})) != 1:
                notes.append(f"batch 2 downloads={downloads}")
            quit_event.set()
        _write_bls(cfg, tics)

    def filt(cfg, tics):
        _write_candidates(cfg)

    code = run_worker(
        _cfg(tmp_path), once=False, client=client, download=download, search=search, filt=filt,
        heartbeat_s=0, quit_event=quit_event,
    )
    assert code == 0
    assert notes and notes[0].startswith("Next: downloading") and notes[0].endswith("/2")
    assert not any(
        str(item).startswith(("search finished", "batch 2", "download started")) for item in notes
    )
    snap = client.snapshot()
    assert snap["claims"][0] is False
    assert True in snap["claims"]
    assert downloads.count(frozenset({1, 2})) == 1
    assert downloads.count(frozenset({3, 4})) == 1
    assert searches == [frozenset({1, 2}), frozenset({3, 4})]
    assert snap["submitted"] == [1, 2]


def test_prefetch_skips_when_the_cache_cap_would_be_exceeded(tmp_path, monkeypatch):
    monkeypatch.setattr("rhexas.worker.loop.apply_priority", lambda priority: 0)
    active = _batch(1, [1, 2])
    cache = tmp_path / "worker-cache"
    cache.mkdir()
    (cache / "state.json").write_text(json.dumps({**active, "stage": "downloaded"}), encoding="utf-8")
    client = QueueCoord([_batch(2, [3, 4])])
    client.held = [active]
    downloads = []

    def download(cfg, tics):
        downloads.append(set(tics))

    def search(cfg, tics, procs):
        _write_bls(cfg, tics)

    def filt(cfg, tics):
        _write_candidates(cfg)

    code = run_worker(
        _cfg(tmp_path, max_cache_gb=0.001), once=True, client=client,
        download=download, search=search, filt=filt, heartbeat_s=0,
    )
    assert code == 0
    snap = client.snapshot()
    assert snap["claims"] == [True]
    assert snap["released"] == [2]
    assert downloads == []
    assert not (cache / "next.json").exists()
    assert client.submitted == [1]


def test_prefetch_block_respects_free_space_and_the_cache_cap(tmp_path, monkeypatch):
    wide = _cfg(tmp_path, max_cache_gb=20, min_free_gb=5)
    tight = _cfg(tmp_path, max_cache_gb=0.001, min_free_gb=5)

    class Usage:
        def __init__(self, free):
            self.free = free

    monkeypatch.setattr("rhexas.worker.loop.shutil.disk_usage", lambda path: Usage(20_000_000_000))
    assert prefetch_block(tmp_path / "cache-a", wide, 2) is None
    monkeypatch.setattr("rhexas.worker.loop.shutil.disk_usage", lambda path: Usage(5_002_000_000))
    assert prefetch_block(tmp_path / "cache-b", wide, 2) == "free space"
    monkeypatch.setattr("rhexas.worker.loop.shutil.disk_usage", lambda path: Usage(50_000_000_000))
    assert prefetch_block(tmp_path / "cache-c", tight, 2) == "cache cap"


def test_restart_resumes_the_current_batch_and_the_prefetched_one(tmp_path, monkeypatch):
    monkeypatch.setattr("rhexas.worker.loop.apply_priority", lambda priority: 0)
    first = _batch(1, [1])
    second = _batch(2, [2])
    cache = tmp_path / "worker-cache"
    cache.mkdir()
    (cache / "state.json").write_text(json.dumps({**first, "stage": "downloaded"}), encoding="utf-8")
    (cache / "next.json").write_text(json.dumps({**second, "stage": "downloaded"}), encoding="utf-8")
    client = QueueCoord([])
    client.held = [first, second]
    quit_event = threading.Event()
    downloads = []
    searches = []
    notes = []

    def download(cfg, tics):
        downloads.append(set(tics))

    def search(cfg, tics, procs):
        found = frozenset(tics)
        searches.append(found)
        if found == frozenset({1}):
            deadline = time.time() + 3
            while time.time() < deadline:
                if {1, 2} <= set(client.snapshot()["beats"]):
                    break
                time.sleep(0.02)
            else:
                notes.append("both leases were not heartbeated")
                quit_event.set()
            if client.snapshot()["claims"]:
                notes.append(f"claimed before resume search: {client.snapshot()['claims']}")
                quit_event.set()
        else:
            quit_event.set()
        _write_bls(cfg, tics)

    def filt(cfg, tics):
        _write_candidates(cfg)

    code = run_worker(
        _cfg(tmp_path), once=False, client=client, download=download, search=search, filt=filt,
        heartbeat_s=0.05, quit_event=quit_event,
    )
    assert code == 0
    assert notes == []
    assert downloads == []
    assert searches == [frozenset({1}), frozenset({2})]
    assert client.snapshot()["submitted"] == [1, 2]
    assert {1, 2} <= set(client.snapshot()["beats"])


def test_prefetch_zero_waits_until_the_upload_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr("rhexas.worker.loop.apply_priority", lambda priority: 0)
    client = QueueCoord([_batch(1, [7]), _batch(2, [8])])
    downloads = []

    def download(cfg, tics):
        downloads.append(set(tics))

    def search(cfg, tics, procs):
        _write_bls(cfg, tics)

    def filt(cfg, tics):
        _write_candidates(cfg)

    code = run_worker(
        _cfg(tmp_path, prefetch=0), once=True, client=client,
        download=download, search=search, filt=filt, heartbeat_s=0,
    )
    assert code == 0
    assert client.snapshot()["claims"] == [False]
    assert downloads == [{7}]
    assert not (tmp_path / "worker-cache" / "next.json").exists()


def test_eco_lowers_prefetch_download_threads(tmp_path, monkeypatch):
    monkeypatch.setattr("rhexas.worker.loop.apply_priority", lambda priority: 0)
    client = QueueCoord([_batch(1, [1]), _batch(2, [2])])
    recorded = {}
    started = time.monotonic()

    def download(cfg, tics):
        if set(tics) == {1}:
            Store(tmp_path / "worker-cache").request_mode("eco")
        else:
            recorded["threads"] = cfg.download.threads

    def sleeper(seconds):
        if recorded.get("threads") == 1:
            Store(tmp_path / "worker-cache").request_mode("live")
            return
        if time.monotonic() - started > 5:
            recorded["timeout"] = True
            Store(tmp_path / "worker-cache").request_mode("live")
            return
        time.sleep(0.01)

    def search(cfg, tics, procs):
        _write_bls(cfg, tics)

    def filt(cfg, tics):
        _write_candidates(cfg)

    code = run_worker(
        _cfg(tmp_path, threads=4), once=True, client=client,
        download=download, search=search, filt=filt, sleeper=sleeper, heartbeat_s=0,
    )
    assert code == 0
    assert recorded.get("threads") == 1
    assert "timeout" not in recorded


def test_prefetch_hold_honors_tray_pause_and_quit_but_not_eco(tmp_path):
    import rhexas.worker.loop as loop

    cache = tmp_path / "cache"
    cache.mkdir()
    live = Store(cache)
    live.request_user_pause(True)
    calls = {"n": 0}

    def sleeper(seconds):
        calls["n"] += 1
        if calls["n"] >= 2:
            live.request_user_pause(False)

    prefetch_hold(live, sleeper)
    assert calls["n"] >= 2
    event = threading.Event()
    event.set()
    previous = loop._stop_event
    loop._stop_event = event
    try:
        raised = False
        try:
            prefetch_hold(live, sleeper)
        except WorkerStopped:
            raised = True
        assert raised
    finally:
        loop._stop_event = previous
    live.request_mode("eco")
    live.request_user_pause(False)
    calls["n"] = 0
    prefetch_hold(live, sleeper)
    assert calls["n"] == 0


def test_dashboard_shows_the_prefetched_batch(tmp_path):
    assert next_line({"stage": "downloading", "stars_done": 3, "stars_total": 500}) == "Next: downloading 3/500"
    assert next_line({"stage": "ready", "stars_done": 500, "stars_total": 500}) == "Next: ready"
    state = mission_state({
        "batch": {},
        "lifetime": {},
        "resources": {},
        "connection": {},
        "candidates": [],
        "next": {"active": True, "stage": "downloading", "stars_done": 3, "stars_total": 500},
    })
    assert state["next_line"] == "Next: downloading 3/500"
    html = (Path(__file__).resolve().parents[1] / "src/rhexas/worker/static/index.html").read_text(encoding="utf-8")
    assert 'id="next_line"' in html


def test_cli_prefetch_zero_is_accepted(tmp_path):
    args = build_parser().parse_args(["worker", "--prefetch", "0", "--once"])
    assert args.prefetch == 0
    cfg = resolve_worker(default_config(tmp_path), token="rhx_test", prefetch=args.prefetch, environ={})
    assert cfg.worker.prefetch == 0
