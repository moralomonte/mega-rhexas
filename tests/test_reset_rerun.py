"""reset-results and rerun stay offline and leave downloaded light curves alone."""
from pathlib import Path

from rhexas.config import default_config
from rhexas.pipeline import queue, run_sectors
from rhexas.reset import reset_results


def _touch(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed(tmp_path: Path):
    cfg = default_config(tmp_path)
    data = cfg.data_dir / "s0107"
    _touch(data / "tess-s0107-0000000000000001-s_lc.fits", "fits")
    _touch(data / ".download.done", "ok")
    _touch(cfg.data_dir / "s0106" / "tess-s0106-0000000000000002-s_lc.fits", "fits")
    _touch(cfg.data_dir / "s0106" / ".download.done", "ok")
    _touch(cfg.results_root / "bls_raw.csv", "tic\n")
    _touch(cfg.results_root / "candidates.csv", "tic\n")
    _touch(cfg.results_root / "plots" / "TIC1.png", "png")
    _touch(cfg.results_root / "vetting" / "vetting.csv", "tic\n")
    _touch(cfg.results_root / "toi_catalog.csv", "tic\n")
    _touch(cfg.results_root / "confirmed_planets.csv", "tic\n")
    _touch(cfg.results_root / "s0107" / "candidates.csv", "new\n")
    _touch(cfg.results_root / "s0106" / "bls_raw.csv", "old\n")
    _touch(cfg.logs_dir / "run_s0107.log", "run\n")
    _touch(cfg.logs_dir / "download_s0106.log", "dl\n")
    _touch(cfg.logs_dir / "full_run.log", "full\n")
    _touch(cfg.logs_dir / "pipeline_queue.log", "queue\n")
    return cfg


def test_reset_archives_products_and_keeps_downloads(tmp_path, capsys):
    cfg = _seed(tmp_path)
    assert reset_results(cfg) == 0
    assert (cfg.data_dir / "s0107" / "tess-s0107-0000000000000001-s_lc.fits").is_file()
    assert (cfg.data_dir / "s0107" / ".download.done").is_file()
    assert (cfg.data_dir / "s0106" / ".download.done").is_file()
    assert (cfg.results_root / "toi_catalog.csv").is_file()
    assert (cfg.results_root / "confirmed_planets.csv").is_file()
    assert not (cfg.results_root / "bls_raw.csv").exists()
    assert not (cfg.results_root / "s0107").exists()
    assert not (cfg.results_root / "s0106").exists()
    assert not (cfg.logs_dir / "full_run.log").exists()
    assert not (cfg.logs_dir / "pipeline_queue.log").exists()
    stamps = list(cfg.archive_dir.iterdir())
    assert len(stamps) == 1
    stamp = stamps[0]
    assert (stamp / "results" / "sector-107-root" / "bls_raw.csv").is_file()
    assert (stamp / "results" / "sector-107-root" / "plots" / "TIC1.png").is_file()
    assert (stamp / "results" / "sector-107-root" / "vetting" / "vetting.csv").is_file()
    assert (stamp / "results" / "s0107" / "candidates.csv").is_file()
    assert (stamp / "results" / "s0106" / "bls_raw.csv").is_file()
    assert (stamp / "logs" / "run_s0107.log").is_file()
    assert (stamp / "logs" / "pipeline_queue.log").is_file()
    out = capsys.readouterr().out
    assert "downloaded light curves and .download.done markers were left in place" in out


def test_reset_one_sector_leaves_the_others(tmp_path):
    cfg = _seed(tmp_path)
    assert reset_results(cfg, [106]) == 0
    assert (cfg.results_root / "s0107" / "candidates.csv").is_file()
    assert (cfg.results_root / "bls_raw.csv").is_file()
    assert (cfg.logs_dir / "run_s0107.log").is_file()
    assert (cfg.logs_dir / "full_run.log").is_file()
    assert (cfg.logs_dir / "pipeline_queue.log").is_file()
    assert not (cfg.results_root / "s0106").exists()
    assert not (cfg.logs_dir / "download_s0106.log").exists()
    assert (cfg.data_dir / "s0106" / "tess-s0106-0000000000000002-s_lc.fits").is_file()
    stamp = next(cfg.archive_dir.iterdir())
    assert (stamp / "results" / "s0106" / "bls_raw.csv").is_file()
    assert not (stamp / "results" / "sector-107-root").exists()


def test_reset_delete_removes_without_an_archive(tmp_path):
    cfg = _seed(tmp_path)
    assert reset_results(cfg, [106], delete=True) == 0
    assert not cfg.archive_dir.exists()
    assert not (cfg.results_root / "s0106").exists()
    assert (cfg.results_root / "s0107" / "candidates.csv").is_file()
    assert (cfg.data_dir / "s0106" / ".download.done").is_file()


def test_reset_with_nothing_processed(tmp_path, capsys):
    cfg = default_config(tmp_path)
    assert reset_results(cfg) == 0
    assert "nothing to reset" in capsys.readouterr().out


def test_rerun_skips_download_when_the_marker_exists(tmp_path, monkeypatch, capsys):
    cfg = default_config(tmp_path).for_sector(107)
    _touch(cfg.sector_data / "star_lc.fits", "fits")
    _touch(cfg.sector_data / ".download.done", "ok")
    calls = []

    def refuse_download(*_a, **_k):
        raise AssertionError("download must not run when .download.done exists")

    def search_run(c, **_k):
        assert (c.logs_dir / "pipeline.pid").is_file()
        assert (c.logs_dir / "run_s0107.pid").is_file()
        calls.append(("search", c.sector))
        return 0

    monkeypatch.setattr("rhexas.download.run", refuse_download)
    monkeypatch.setattr("rhexas.search.run", search_run)
    monkeypatch.setattr("rhexas.filter.run", lambda c, **_k: calls.append(("filter", c.sector)) or 0)
    monkeypatch.setattr("rhexas.vet.run_new", lambda c: calls.append(("vet", c.sector)) or 0)
    assert run_sectors(cfg, [107], skip_done_downloads=True, vet=True) == 0
    assert calls == [("search", 107), ("filter", 107), ("vet", 107)]
    assert not (cfg.logs_dir / "pipeline.pid").exists()
    assert not (cfg.logs_dir / "run_s0107.pid").exists()
    assert (cfg.sector_data / "star_lc.fits").is_file()
    assert "sector 107: download already complete (.download.done), skipping" in capsys.readouterr().out


def test_rerun_downloads_when_the_marker_is_missing(tmp_path, monkeypatch):
    cfg = default_config(tmp_path).for_sector(96)
    calls = []
    monkeypatch.setattr("rhexas.download.run", lambda c, **_k: calls.append(("download", c.sector)) or 0)
    monkeypatch.setattr("rhexas.search.run", lambda c, **_k: calls.append(("search", c.sector)) or 0)
    monkeypatch.setattr("rhexas.filter.run", lambda c, **_k: calls.append(("filter", c.sector)) or 0)
    monkeypatch.setattr("rhexas.vet.run_new", lambda *_a, **_k: calls.append("vet") or 0)
    assert run_sectors(cfg, [96], skip_done_downloads=True, vet=True) == 0
    assert calls[0] == ("download", 96)
    assert "vet" in calls


def test_queue_rerun_does_not_spawn_a_finished_download(tmp_path, monkeypatch, capsys):
    cfg = default_config(tmp_path)
    sector = cfg.for_sector(96)
    _touch(sector.sector_data / ".download.done", "ok")
    logged = []

    def fake_logged(cmd, _log_path, _cwd):
        logged.append(cmd)
        assert (cfg.logs_dir / "run_s0096.pid").is_file()
        return 0

    monkeypatch.setattr("rhexas.pipeline._run_logged", fake_logged)
    assert queue(cfg, [96], poll_s=0.01, skip_done_downloads=True, vet=True) == 0
    subs = [cmd[3] for cmd in logged]
    assert subs == ["search", "filter", "vet"]
    assert logged[-1][logged[-1].index("--new")]
    assert "download worker SKIP sector 96 (.download.done)" in capsys.readouterr().out
    assert not (cfg.logs_dir / "pipeline_queue.pid").exists()
