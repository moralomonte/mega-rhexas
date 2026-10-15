"""CLI parsing and --help. Nothing here contacts MAST."""
import subprocess
import sys
from pathlib import Path

from rhexas.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]


def test_parser_accepts_batch_and_sector_flags():
    args = build_parser().parse_args([
        "download", "--sector", "105", "--tics", "1,2", "--threads", "4", "--limit", "10",
        "--min-free-gb", "100", "--max-project-gb", "400",
    ])
    assert args.cmd == "download"
    assert args.sector == 105
    assert args.tics_text == "1,2"
    assert args.threads == 4
    multi = build_parser().parse_args(["multisector", "13345688", "170634116", "--period", "0.7"])
    assert multi.tics == [13345688, 170634116]
    assert multi.period == 0.7
    run = build_parser().parse_args(["run", "--sectors", "104,103", "--tics-file", "batch.txt"])
    assert run.sectors == "104,103"
    queue = build_parser().parse_args(["queue", "--predownload", "105", "--sectors", "104,103", "--wait-pid", "9", "--rerun"])
    assert queue.predownload == "105"
    assert queue.wait_pid == 9
    assert queue.rerun is True
    rerun = build_parser().parse_args(["rerun", "--sectors", "107,96", "--queue"])
    assert rerun.queue is True
    assert rerun.sectors == "107,96"
    reset = build_parser().parse_args(["reset-results", "--sectors", "107,106", "--delete"])
    assert reset.delete is True
    assert reset.archive is False


def _help(args):
    proc = subprocess.run([sys.executable, *args, "--help"], cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout


def test_module_commands_print_help():
    _help(["-m", "rhexas"])
    _help(["-m", "rhexas", "download"])
    _help(["-m", "rhexas", "search"])
    _help(["-m", "rhexas", "filter"])
    _help(["-m", "rhexas", "vet"])
    _help(["-m", "rhexas", "run"])
    _help(["-m", "rhexas", "rerun"])
    _help(["-m", "rhexas", "queue"])
    _help(["-m", "rhexas", "reset-results"])
    _help(["-m", "rhexas", "dashboard"])
    _help(["-m", "rhexas", "worker"])
    _help(["-m", "rhexas", "desktop"])
    _help(["-m", "rhexas", "worker-dashboard"])
    _help(["-m", "rhexas", "coord"])
    _help(["-m", "rhexas.coord"])
    _help([str(ROOT / "dashboard" / "server.py")])


def test_dashboard_server_reads_the_toml_and_pipeline_pids():
    text = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
    assert "load_config" in text
    assert "pipeline_pid_files" in text
    assert "S107" not in text
    assert "sectors.json" not in text
    assert "full_run" not in text


def test_powershell_wrappers_call_the_cli():
    for name in ("run_full.ps1", "run_multi.ps1", "pipeline_queue.ps1"):
        text = (ROOT / "src" / name).read_text(encoding="utf-8")
        assert "rhexas" in text
        assert "36280" not in text
        assert "45" not in text
