"""Download selection, 429 backoff, and the disk guard. The HTTP client is a fake."""
from pathlib import Path

import pytest

from rhexas.config import load_config
from rhexas.disk import disk_limit_hit, project_gb
from rhexas.download import consider_disk_limit, parse_curl_script, run, select_urls
from rhexas.tics import filename_from_url, tic_from_filename


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


def _cfg(tmp_path: Path):
    return load_config(
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


def test_curl_parse_and_tic_filter():
    text = _curl(7, 8, 9)
    urls = parse_curl_script(text)
    assert [tic_from_filename(filename_from_url(u)) for u in urls] == [7, 8, 9]
    picked = select_urls(urls, tics={9, 7}, limit=0)
    assert [tic_from_filename(filename_from_url(u)) for u in picked] == [7, 9]
    assert len(select_urls(urls, tics=None, limit=2)) == 2
    assert select_urls(urls, tics=set(), limit=0) == []


def test_download_retries_429_and_writes_a_file(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("real HTTP is disabled")

    monkeypatch.setattr("requests.get", boom)
    sleeps = []
    calls = {"file": 0}

    def getter(url, timeout=60):
        if "tesscurl" in url:
            return _Resp(200, text=_curl(7, 8))
        calls["file"] += 1
        if calls["file"] == 1:
            return _Resp(429, content=b"slow")
        return _Resp(200, content=b"FITSBYTES")

    cfg = _cfg(tmp_path)
    # rng 0.5 is the jitter midpoint, so the 429 wait stays exactly throttle_base_s.
    code = run(cfg, threads=1, tics={7}, getter=getter, sleeper=sleeps.append, rng=lambda: 0.5)
    assert code == 0
    dest = cfg.sector_data / _name(7)
    assert dest.read_bytes() == b"FITSBYTES"
    assert not dest.with_suffix(".part").exists()
    assert sleeps == [15]
    assert (cfg.sector_data / ".download.done").is_file()
    assert not (cfg.sector_data / ".download.lock").exists()
    assert not (cfg.sector_data / _name(8)).exists()


def test_existing_files_are_not_fetched(tmp_path, monkeypatch):
    monkeypatch.setattr("requests.get", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network")))
    cfg = _cfg(tmp_path)
    cfg.sector_data.mkdir(parents=True)
    (cfg.sector_data / _name(7)).write_bytes(b"already")
    file_gets = []

    def getter(url, timeout=60):
        if "tesscurl" in url:
            return _Resp(200, text=_curl(7))
        file_gets.append(url)
        return _Resp(200, content=b"nope")

    assert run(cfg, threads=1, getter=getter, sleeper=lambda _s: None) == 0
    assert file_gets == []
    assert (cfg.sector_data / _name(7)).read_bytes() == b"already"


def test_disk_guard_trips_every_200_files(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg = load_config(search_files=False, environ={}, overrides={
        "root": tmp_path, "sector": 107, "data_dir": tmp_path / "data",
        "results_root": tmp_path / "results", "min_free_gb": 100, "max_project_gb": 400,
    })
    monkeypatch.setattr("rhexas.download.free_gb", lambda _data: 10.0)
    state = {"n": 199, "stop": False, "added": 0.0}
    consider_disk_limit(state, cfg, base=0.0)
    assert state["stop"] is False
    state["n"] = 200
    consider_disk_limit(state, cfg, base=0.0)
    assert state["stop"] is True

    monkeypatch.setattr("rhexas.download.free_gb", lambda _data: 1000.0)
    state = {"n": 200, "stop": False, "added": 50.0}
    consider_disk_limit(state, cfg, base=360.0)
    assert state["stop"] is True
    assert disk_limit_hit(150, 50, 100, 400) is False
    assert disk_limit_hit(150, 401, 100, 400) is True


def test_project_gb_counts_only_top_level_sector_files(tmp_path):
    sector = tmp_path / "s0007"
    sector.mkdir()
    (sector / "a.fits").write_bytes(b"x" * 1000)
    (sector / "nested").mkdir()
    (sector / "nested" / "nope.fits").write_bytes(b"y" * 5000)
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "nope.fits").write_bytes(b"z" * 5000)
    assert project_gb(tmp_path, overhead_gb=4.5) == pytest.approx(1000 / 1e9 + 4.5)
