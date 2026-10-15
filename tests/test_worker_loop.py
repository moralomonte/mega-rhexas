"""Worker loop with stand-in download/search/filter. Nothing here contacts MAST."""
import json
import time

from rhexas.config import default_config
from rhexas.worker.cache import delete_lightcurves, enforce_cache_cap
from rhexas.worker.loop import (
    backoff_s,
    load_token,
    read_stars,
    resolve_worker,
    run_worker,
    start_heartbeat,
    unit_config,
)
from rhexas.worker.priority import apply_priority, nice_value


def _fits_name(tic):
    return f"tess2026221103959-s0107-{tic:016d}-0309-s_lc.fits"


class FakeCoord:
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
        return {"verdict": "pending"}

    def heartbeat(self, assignment_id):
        return {"expires_at": "2026-01-01T00:00:00+00:00"}

    def release(self, assignment_id):
        return {"status": "released"}


def _cfg(tmp_path):
    cfg = default_config(tmp_path)
    return resolve_worker(cfg, token="rhx_test", procs=2, threads=2, environ={})


def test_unit_config_keeps_science_defaults_and_uses_worker_limits(tmp_path):
    cfg = resolve_worker(default_config(tmp_path), coordinator="http://127.0.0.1:9/", token="rhx_test", procs=4, threads=1, environ={})
    assert cfg.worker.coordinator_url == "http://127.0.0.1:9"
    assert cfg.search.procs == 28
    assert cfg.search.n_periods == 20000
    assert cfg.filt.sde_min == 6.0
    assert cfg.min_free_gb == 100
    assignment = {"assignment_id": 3, "sector": 107, "tics": [1], "n_tics": 1}
    unit = unit_config(cfg, assignment, tmp_path / "worker-cache")
    assert unit.search.procs == 4
    assert unit.search.n_periods == 20000
    assert unit.filt.sde_min == 6.0
    assert unit.filt.snr_min == 7.0
    assert unit.download.threads == 1
    assert unit.min_free_gb == 5
    assert unit.max_project_gb == 20 + cfg.env_overhead_gb
    assert unit.sector_results == (tmp_path / "worker-cache" / "results" / "a3" / "s0107").resolve()


def test_worker_searches_a_batch_uploads_it_and_deletes_fits(tmp_path, monkeypatch):
    monkeypatch.setattr("rhexas.worker.loop.apply_priority", lambda priority: 0)
    calls = {}
    written = []

    def download(cfg, tics):
        calls["download"] = (cfg.sector, set(tics), cfg.download.threads, cfg.min_free_gb, cfg.filt.sde_min)
        cfg.sector_data.mkdir(parents=True, exist_ok=True)
        for tic in tics:
            path = cfg.sector_data / _fits_name(tic)
            path.write_bytes(b"FITS" * 100)
            written.append(path)

    def search(cfg, tics, procs):
        calls["procs"] = procs
        cfg.sector_results.mkdir(parents=True, exist_ok=True)
        with (cfg.sector_results / "bls_raw.csv").open("w", encoding="utf-8", newline="") as handle:
            handle.write("tic,period,sde,depth,snr,error\n")
            for tic in sorted(tics):
                handle.write(f"{tic},1.5,10,0.01,9,\n")

    def filt(cfg, tics):
        calls["filter"] = set(tics)
        cfg.sector_results.mkdir(parents=True, exist_ok=True)
        (cfg.sector_results / "candidates.csv").write_text(
            "tic,period,depth,snr,sde,status,score,flags\n7,1.5,0.01,9,10,NEW,12,\n",
            encoding="utf-8",
        )

    assignment = {
        "assignment_id": 8,
        "unit_id": 1,
        "generation": 1,
        "sector": 107,
        "tics": [7, 8],
        "n_tics": 2,
        "expires_at": "2026-01-02T00:00:00+00:00",
    }
    client = FakeCoord(assignment)
    code = run_worker(
        _cfg(tmp_path), once=True, client=client, download=download, search=search, filt=filt,
        heartbeat_s=0,
    )
    assert code == 0
    assert calls["download"][0] == 107
    assert calls["download"][1] == {7, 8}
    assert calls["download"][2] == 2
    assert calls["download"][3] == 5
    assert calls["download"][4] == 6.0
    assert calls["procs"] == 2
    assert calls["filter"] == {7, 8}
    uploaded = client.submitted[0]
    by_tic = {row["tic"]: row for row in uploaded["stars"]}
    assert by_tic[7]["period"] == 1.5
    assert by_tic[7]["error"] is None
    assert uploaded["candidates"][0]["status"] == "NEW"
    assert all(not path.exists() for path in written)
    assert not (tmp_path / "worker-cache" / "state.json").exists()


def test_worker_resumes_after_download_without_fetching_again(tmp_path, monkeypatch):
    monkeypatch.setattr("rhexas.worker.loop.apply_priority", lambda priority: 0)
    cache = tmp_path / "worker-cache"
    cache.mkdir()
    assignment = {
        "assignment_id": 4,
        "unit_id": 1,
        "generation": 1,
        "sector": 107,
        "tics": [7],
        "n_tics": 1,
        "expires_at": "2026-01-02T00:00:00+00:00",
    }
    (cache / "state.json").write_text(json.dumps({**assignment, "stage": "downloaded"}), encoding="utf-8")
    calls = {"download": 0, "search": 0}

    def download(cfg, tics):
        calls["download"] += 1

    def search(cfg, tics, procs):
        calls["search"] += 1
        cfg.sector_results.mkdir(parents=True, exist_ok=True)
        (cfg.sector_results / "bls_raw.csv").write_text("tic,period,sde,depth,error\n7,2,8,0.02,\n", encoding="utf-8")

    def filt(cfg, tics):
        (cfg.sector_results / "candidates.csv").write_text("tic,status\n", encoding="utf-8")

    client = FakeCoord(assignment)
    client.claimed = True
    code = run_worker(_cfg(tmp_path), once=True, client=client, download=download, search=search, filt=filt, heartbeat_s=0)
    assert code == 0
    assert calls == {"download": 0, "search": 1}
    assert client.submitted[0]["stars"][0]["tic"] == 7


def test_cache_cap_deletes_oldest_fits_and_leaves_other_tics(tmp_path):
    root = tmp_path / "data" / "s0107"
    root.mkdir(parents=True)
    old = root / _fits_name(1)
    new = root / _fits_name(2)
    old.write_bytes(b"a" * 1000)
    new.write_bytes(b"b" * 1500)
    import os
    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(new, (1_700_000_100, 1_700_000_100))
    deleted = enforce_cache_cap(tmp_path / "data", 1500)
    assert deleted == [old]
    assert not old.exists()
    assert new.exists()
    assert delete_lightcurves(root, {2}) == 1
    assert not new.exists()


def test_backoff_and_priority_and_token_file(tmp_path, monkeypatch):
    assert backoff_s(0) == 15
    assert backoff_s(8) == 300
    assert nice_value("low") == 10
    assert nice_value("idle") == 19
    seen = {}

    class Proc:
        def nice(self, value):
            seen["value"] = value

    monkeypatch.setattr("psutil.Process", lambda: Proc())
    assert apply_priority("low") == 10
    assert seen["value"] == 10
    token = tmp_path / "token.txt"
    token.write_text("\ufeff# invite\nrhx_from_file\n", encoding="utf-8")
    cfg = resolve_worker(default_config(tmp_path), token_file=str(token), environ={})
    assert load_token(cfg) == "rhx_from_file"
    assert cfg.worker.coordinator_url == "https://api.megarhexas.com"


def test_read_stars_fills_in_missing_tics(tmp_path):
    folder = tmp_path / "s0107"
    folder.mkdir()
    (folder / "bls_raw.csv").write_text("tic,period,sde,depth,error\n3,1.0,5,0.01,boom\n", encoding="utf-8")
    rows = {row["tic"]: row for row in read_stars(folder, [3, 4])}
    assert rows[3]["error"] == "boom"
    assert rows[4]["error"] == "no result"


def test_heartbeat_thread_pings_until_stopped():
    beats = []

    class Client:
        def heartbeat(self, assignment_id):
            beats.append(assignment_id)

    stop = start_heartbeat(Client(), 5, 0.01)
    deadline = time.time() + 1
    while not beats and time.time() < deadline:
        time.sleep(0.02)
    stop.set()
    assert beats == [5] or beats[0] == 5
