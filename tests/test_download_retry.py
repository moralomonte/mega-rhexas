"""MAST retries: jitter, resume, and a last pass for stars that never landed."""
from dataclasses import replace
from pathlib import Path

import pytest

from rhexas.config import default_config, load_config
from rhexas.download import error_kind, retry_delay, run
from rhexas.worker.loop import resolve_worker, retry_missing_before_submit, run_worker


def _name(tic: int) -> str:
    return f"tess2026221103959-s0107-{tic:016d}-0309-s_lc.fits"


def _curl(*tics: int) -> str:
    lines = []
    for tic in tics:
        name = _name(tic)
        lines.append(f'curl -C - -L -o {name} "https://example.invalid/file?uri=mast:HLSP/tess/{name}"')
    return "\n".join(lines)


class _Resp:
    def __init__(self, status, content=b"", text=""):
        self.status_code = status
        self.content = content
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Chunks:
    def __init__(self, status, chunks):
        self.status_code = status
        self.chunks = chunks

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _cfg(tmp_path: Path, **download):
    cfg = load_config(
        search_files=False,
        environ={},
        overrides={
            "root": tmp_path,
            "sector": 107,
            "data_dir": tmp_path / "data",
            "results_root": tmp_path / "results",
            "logs_dir": tmp_path / "logs",
            "min_free_gb": 0,
            "max_project_gb": 1e9,
        },
    )
    if download:
        cfg = replace(cfg, download=replace(cfg.download, **download))
    return cfg


def test_retry_delay_is_exponential_and_stays_in_the_jitter_band():
    assert retry_delay(5, 2, 0, jitter=0) == 5
    assert retry_delay(5, 2, 3, jitter=0) == 40
    assert retry_delay(15, 2, 0, jitter=0.2, rng=lambda: 0.5) == 15
    assert retry_delay(10, 2, 2, jitter=0.2, rng=lambda: 0) == pytest.approx(32)
    assert retry_delay(10, 2, 2, jitter=0.2, rng=lambda: 1) == pytest.approx(48)
    assert error_kind(ConnectionError("EOF occurred in violation of protocol")) == "SSL EOF"
    assert error_kind(TimeoutError("timed out")) == "timeout"


def test_ssl_eof_retries_with_backoff_and_resumes_a_partial_file(tmp_path, monkeypatch):
    monkeypatch.setattr("requests.get", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network")))
    calls = []

    def getter(url, timeout=60, headers=None):
        if "tesscurl" in url:
            return _Resp(200, text=_curl(7))
        calls.append(dict(headers or {}))
        if len(calls) == 1:
            return _Chunks(200, [b"FITS", None])
        return _Resp(206, content=b"BYTES")

    sleeps = []
    cfg = _cfg(tmp_path, jitter=0, retry_base_s=5)
    assert run(cfg, threads=1, tics={7}, getter=getter, sleeper=sleeps.append) == 0
    dest = cfg.sector_data / _name(7)
    assert dest.read_bytes() == b"FITSBYTES"
    assert not dest.with_suffix(".part").exists()
    assert calls[0] == {}
    assert calls[1]["Range"] == "bytes=4-"
    assert sleeps == [5]


def test_missing_stars_get_a_final_download_pass(tmp_path, monkeypatch):
    monkeypatch.setattr("requests.get", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network")))
    attempts = {"n": 0}

    def getter(url, timeout=60, headers=None):
        if "tesscurl" in url:
            return _Resp(200, text=_curl(7, 8))
        attempts["n"] += 1
        name = url.rsplit("/", 1)[-1]
        if name.startswith(_name(7)[:20]) and attempts["n"] == 1:
            raise OSError("EOF occurred in violation of protocol (_ssl.c)")
        tic = 7 if f"{7:016d}" in url else 8
        return _Resp(200, content=f"FITS{tic}".encode())

    cfg = _cfg(tmp_path, retries=1, jitter=0, retry_base_s=1)
    assert run(cfg, threads=1, tics={7, 8}, getter=getter, sleeper=lambda _s: None) == 0
    assert (cfg.sector_data / _name(7)).read_bytes() == b"FITS7"
    assert (cfg.sector_data / _name(8)).read_bytes() == b"FITS8"
    # Star 7 failed its only in-pass attempt and succeeded on the final pass.
    assert attempts["n"] >= 3


def _worker(tmp_path):
    return resolve_worker(default_config(tmp_path), token="rhx_test", procs=2, threads=2, environ={})


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
        self.submitted.append({"stars": stars, "candidates": candidates})
        self.assignment = None
        self.claimed = False
        return {"verdict": "pending"}

    def heartbeat(self, assignment_id):
        return {}


def test_worker_retries_missing_stars_before_submit(tmp_path, monkeypatch):
    monkeypatch.setattr("rhexas.worker.loop.apply_priority", lambda priority: 0)
    downloads = []
    searches = []

    def download(cfg, tics):
        downloads.append(set(tics))
        cfg.sector_data.mkdir(parents=True, exist_ok=True)
        for tic in tics:
            (cfg.sector_data / _name(tic)).write_bytes(b"FITS")

    def search(cfg, tics, procs):
        searches.append(set(tics))
        cfg.sector_results.mkdir(parents=True, exist_ok=True)
        path = cfg.sector_results / "bls_raw.csv"
        new = not path.exists()
        with path.open("a", encoding="utf-8", newline="") as handle:
            if new:
                handle.write("tic,period,sde,depth,snr,error\n")
            for tic in sorted(tics):
                if tic == 8 and len(searches) == 1:
                    continue
                handle.write(f"{tic},1.5,10,0.01,9,\n")

    def filt(cfg, tics):
        cfg.sector_results.mkdir(parents=True, exist_ok=True)
        (cfg.sector_results / "candidates.csv").write_text("tic,status\n", encoding="utf-8")

    assignment = {
        "assignment_id": 8, "unit_id": 1, "generation": 1, "sector": 107,
        "tics": [7, 8], "n_tics": 2, "expires_at": "2026-01-02T00:00:00+00:00",
    }
    client = _Coord(assignment)
    code = run_worker(
        _worker(tmp_path), once=True, client=client, download=download, search=search, filt=filt,
        heartbeat_s=0,
    )
    assert code == 0
    assert downloads == [{7, 8}, {8}]
    assert searches == [{7, 8}, {8}]
    by_tic = {row["tic"]: row for row in client.submitted[0]["stars"]}
    assert by_tic[7]["period"] == 1.5
    assert by_tic[8]["period"] == 1.5
    assert by_tic[8]["error"] is None


def test_a_real_search_error_is_not_downloaded_again(tmp_path):
    cfg = _worker(tmp_path)
    assignment = {"assignment_id": 3, "sector": 107, "tics": [7], "n_tics": 1}
    from rhexas.worker.loop import unit_config
    ucfg = unit_config(cfg, assignment, tmp_path / "cache")
    ucfg.sector_results.mkdir(parents=True)
    (ucfg.sector_results / "bls_raw.csv").write_text(
        "tic,period,sde,depth,error\n7,,,,boom\n", encoding="utf-8",
    )
    calls = []
    assert retry_missing_before_submit(
        ucfg, [7],
        download=lambda *_a, **_k: calls.append("download"),
        search=lambda *_a, **_k: calls.append("search"),
        filt=lambda *_a, **_k: calls.append("filter"),
    ) == []
    assert calls == []
