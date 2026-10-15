"""Config loading, path layout, and TIC-list parsing. No network."""
from pathlib import Path

import pytest

from rhexas.config import ConfigError, default_config, load_config, sector_results_dir
from rhexas.dashboard_cmd import sectors_table
from rhexas.tics import filename_from_url, parse_sector_list, parse_tics, tic_from_filename

ROOT = Path(__file__).resolve().parents[1]


def test_builtin_defaults_are_the_operational_disk_limits(tmp_path):
    cfg = default_config(tmp_path)
    assert cfg.sector == 107
    assert cfg.min_free_gb == 100
    assert cfg.max_project_gb == 400
    assert cfg.download.threads == 8
    assert cfg.search.procs == 28
    assert cfg.search.n_periods == 20000
    assert cfg.search.durations_days == (0.04, 0.06, 0.08, 0.12, 0.16, 0.24)
    assert cfg.search.flatten_window == 721
    assert cfg.filt.sde_min == 6.0
    assert cfg.filt.snr_min == 7.0
    assert cfg.filt.min_transits == 3
    assert cfg.filt.max_depth == 0.05
    assert cfg.filt.odd_even_max_sigma == 3.0
    assert cfg.filt.secondary_max_sigma == 3.0
    assert cfg.filt.max_duration_fraction == 0.12
    assert cfg.filt.eb_depth == 0.03
    assert cfg.filt.crowdsap_min == 0.8
    assert cfg.dashboard.port == 8080
    assert cfg.dashboard.demo_port == 8081
    assert cfg.dashboard.mode == "live"
    assert cfg.archive_dir == (tmp_path / "archive").resolve()
    assert cfg.sector_results == (tmp_path / "results" / "s0107").resolve()
    assert cfg.for_sector(106).sector_results == (tmp_path / "results" / "s0106").resolve()
    assert cfg.multisector_download_dir == cfg.data_dir / "multisector"
    assert cfg.worker.coordinator_url == "https://api.megarhexas.com"
    assert cfg.worker.procs == 2
    assert cfg.search.procs == 28


def test_committed_toml_matches_builtin_defaults():
    cfg = load_config(ROOT / "rhexas.toml", environ={})
    plain = default_config(ROOT)
    assert cfg.sector == plain.sector == 107
    assert cfg.min_free_gb == plain.min_free_gb == 100
    assert cfg.max_project_gb == plain.max_project_gb == 400
    assert cfg.download == plain.download
    assert cfg.search == plain.search
    assert cfg.filt == plain.filt
    assert cfg.dashboard == plain.dashboard
    assert cfg.multisector == plain.multisector
    assert cfg.worker == plain.worker
    assert cfg.worker.procs == 2
    assert cfg.worker.max_cache_gb == 20.0
    assert cfg.worker.coordinator_url == "https://api.megarhexas.com"
    assert cfg.data_dir == (ROOT / "data").resolve()
    assert cfg.results_root == (ROOT / "results").resolve()
    assert cfg.catalogs_dir == (ROOT / "results").resolve()
    assert cfg.archive_dir == (ROOT / "archive").resolve()
    assert cfg.sector_results == (ROOT / "results" / "s0107").resolve()
    assert cfg.for_sector(96).sector_results == (ROOT / "results" / "s0096").resolve()
    assert cfg.multisector_dir is None
    assert cfg.multisector_download_dir == cfg.data_dir / "multisector"


def test_env_overrides_file_and_cli_overrides_env(tmp_path):
    toml = tmp_path / "rhexas.toml"
    toml.write_text("[project]\nsector = 10\n[disk]\nmin_free_gb = 3\nmax_project_gb = 9\n", encoding="utf-8")
    env = {"MR_SECTOR": "42", "MR_MIN_FREE_GB": "12", "MR_DATA_DIR": "from-env"}
    cfg = load_config(toml, environ=env)
    assert cfg.sector == 42
    assert cfg.min_free_gb == 12
    assert cfg.max_project_gb == 9
    assert cfg.data_dir == (tmp_path / "from-env").resolve()
    cli = load_config(toml, environ=env, overrides={"sector": 7, "data_dir": "from-cli"})
    assert cli.sector == 7
    assert cli.data_dir == (tmp_path / "from-cli").resolve()


def test_relative_root_override_moves_default_dirs(tmp_path):
    toml = tmp_path / "rhexas.toml"
    toml.write_text("[paths]\ndata_dir = \"data\"\n", encoding="utf-8")
    other = tmp_path / "elsewhere"
    cfg = load_config(toml, environ={}, overrides={"root": other})
    assert cfg.root == other.resolve()
    assert cfg.data_dir == (other / "data").resolve()
    assert cfg.logs_dir == (other / "logs").resolve()


def test_unknown_config_keys_are_errors(tmp_path):
    bad = tmp_path / "rhexas.toml"
    bad.write_text("[filter]\nnot_a_real_threshold = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(bad, environ={})
    bad.write_text("[nope]\nx = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown section"):
        load_config(bad, environ={})


def test_missing_explicit_config_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.toml", environ={})


def test_parse_tics_and_sectors(tmp_path):
    assert parse_tics() is None
    assert parse_tics(text="1, 2\n# skip\n3") == {1, 2, 3}
    blob = tmp_path / "batch.txt"
    blob.write_text("# worker batch\n10 11\n12,\n", encoding="utf-8")
    assert parse_tics(path=str(blob), positional=(10,)) == {10, 11, 12}
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing\n\n", encoding="utf-8")
    assert parse_tics(path=str(empty)) == set()
    assert parse_sector_list("104, 103 102") == [104, 103, 102]
    name = "tess2026221103959-s0107-0000000170634116-0309-s_lc.fits"
    assert tic_from_filename(name) == 170634116
    url = "https://example.invalid/file?uri=mast:HLSP/tess/" + name
    assert filename_from_url(url) == name


def test_utf8_bom_toml_loads(tmp_path):
    # PowerShell `Set-Content -Encoding utf8` writes this BOM. tomllib rejects it.
    toml = tmp_path / "rhexas.toml"
    toml.write_bytes(b"\xef\xbb\xbf[project]\nsector = 96\n[worker]\nprocs = 3\npriority = \"low\"\n")
    cfg = load_config(toml, environ={})
    assert cfg.sector == 96
    assert cfg.worker.procs == 3
    assert cfg.search.n_periods == 20000
    assert cfg.filt.sde_min == 6.0


def test_sector_results_helper():
    root = Path("/data/results")
    assert sector_results_dir(root, 107) == root / "s0107"
    assert sector_results_dir(root, 42) == root / "s0042"


def test_dashboard_sector_table_comes_from_the_toml():
    cfg = load_config(ROOT / "rhexas.toml", environ={})
    jobs = sectors_table(cfg)
    by_port = {j["port"]: j for j in jobs}
    assert by_port[8080]["sector"] == 107
    assert by_port[8080]["label"] == ""
    assert by_port[8080]["demo"] is False
    assert 8081 not in by_port
    assert by_port[8092]["sector"] == 96
    assert len(cfg.dashboard.sectors) == 12
    sectors = [row.sector for row in cfg.dashboard.sectors]
    assert len(sectors) == len(set(sectors))
    assert all(row.label != "done" for row in cfg.dashboard.sectors)
    assert all(row.label.lower() != "demo" for row in cfg.dashboard.sectors)
    assert cfg.dashboard.demo_port == 8081
