"""``rhexas`` command line: download, search, filter, vet, multisector, run, rerun, queue, dashboard, worker, worker-dashboard, coord."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import Config, ConfigError, load_config
from .tics import parse_sector_list, parse_tics


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None, help="TOML config (default: discover rhexas.toml)")
    parser.add_argument("--root", default=None, help="Project root containing data/, results/, logs/")
    parser.add_argument("--sector", type=int, default=None, help="TESS sector (default: config, else 107)")
    parser.add_argument("--data-dir", default=None, help="Light-curve root (sector folders live under it)")
    parser.add_argument("--results-dir", default=None, help="Results root. Each sector writes sNNNN/ under it")
    parser.add_argument("--logs-dir", default=None)
    parser.add_argument("--catalogs-dir", default=None, help="Where toi_catalog.csv and confirmed_planets.csv live")
    parser.add_argument("--multisector-dir", default=None, help="Download directory for multisector light curves")
    parser.add_argument("--min-free-gb", type=float, default=None, help="Stop downloading below this much free space (default 100)")
    parser.add_argument("--max-project-gb", type=float, default=None, help="Project size cap in GB (default 400)")


def _tics(parser: argparse.ArgumentParser, *, positional: bool = False) -> None:
    if positional:
        parser.add_argument("tics", nargs="*", type=int, help="TIC IDs")
    parser.add_argument("--tics", default="", dest="tics_text", help="Comma-separated TIC IDs. Omit for the whole sector.")
    parser.add_argument("--tics-file", default="", help="Text file of TIC IDs (whitespace, commas, or one per line; # comments)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rhexas",
        description="Mega Rhexas TESS transit search. Whole-sector runs and TIC batches share one config file.",
    )
    parser.add_argument("--version", action="version", version=f"mega-rhexas {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("download", help="Download SPOC 2-min light curves from MAST")
    _common(p)
    _tics(p)
    p.add_argument("--limit", type=int, default=0, help="Only the first N files after the TIC filter")
    p.add_argument("--threads", type=int, default=None, help="MAST download concurrency (default 8)")

    p = sub.add_parser("search", help="Detrend + Box Least Squares into bls_raw.csv")
    _common(p)
    _tics(p)
    p.add_argument("--procs", type=int, default=None, help="Worker processes (default 28)")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--out", default="bls_raw.csv")

    p = sub.add_parser("filter", help="Threshold, cross-match, rank, plot")
    _common(p)
    _tics(p)
    p.add_argument("--raw", default="bls_raw.csv")
    p.add_argument("--out", default="candidates.csv")
    p.add_argument("--plots", type=int, default=None, help="How many NEW candidates to plot (default 50, 0 to skip)")

    p = sub.add_parser("vet", help="Centroid / neighbour / catalog vetting for specific TICs")
    _common(p)
    _tics(p, positional=True)
    p.add_argument("--new", action="store_true", help="Vet every NEW row in candidates.csv")

    p = sub.add_parser("multisector", help="BLS every available light curve of one or more TICs")
    _common(p)
    _tics(p, positional=True)
    p.add_argument("--period", type=float, default=None, help="Seed period in days (else bls_raw, else free BLS)")
    p.add_argument("--t0", type=float, default=None, help="Seed mid-transit time")

    p = sub.add_parser("check-known", help="Print BLS rows that match the TOI catalog")
    _common(p)
    p.add_argument("raw", nargs="?", default="bls_raw.csv")

    p = sub.add_parser("run", help="Download, search, and filter, one sector after another")
    _common(p)
    _tics(p)
    p.add_argument("--sectors", default="", help="Comma-separated sectors. Default: --sector or the config sector.")
    p.add_argument("--threads", type=int, default=None)
    p.add_argument("--procs", type=int, default=None)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--plots", type=int, default=None)

    p = sub.add_parser("rerun", help="Search, filter, and vet sectors that are already downloaded")
    _common(p)
    _tics(p)
    p.add_argument("--sectors", default="", help="Comma-separated sectors. Default: --sector or the config sector.")
    p.add_argument("--queue", action="store_true", help="Overlap download of incomplete sectors with search")
    p.add_argument("--predownload", default="", help="With --queue: sectors to download first and not search here")
    p.add_argument("--wait-pid", type=int, default=0)
    p.add_argument("--threads", type=int, default=None)
    p.add_argument("--procs", type=int, default=None)
    p.add_argument("--plots", type=int, default=None)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--poll-seconds", type=float, default=60.0)

    p = sub.add_parser("queue", help="Overlap one download with one search across sectors")
    _common(p)
    _tics(p)
    p.add_argument("--sectors", required=True, help="Sectors to search once each download finishes, e.g. 104,103,102")
    p.add_argument("--predownload", default="", help="Sectors to download first and not search in this process")
    p.add_argument("--wait-pid", type=int, default=0, help="Wait until this pid exits before searching")
    p.add_argument("--threads", type=int, default=None)
    p.add_argument("--procs", type=int, default=None)
    p.add_argument("--plots", type=int, default=None)
    p.add_argument("--poll-seconds", type=float, default=60.0)
    p.add_argument("--rerun", action="store_true", help="Skip sectors with .download.done, then search, filter, and vet")

    p = sub.add_parser("reset-results", help="Archive or delete processed results; keep downloaded light curves")
    _common(p)
    p.add_argument("--sectors", default="", help="Comma-separated sectors. Default: every sector that has results or logs.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--archive", action="store_true", help="Move results into archive/ (this is the default)")
    mode.add_argument("--delete", action="store_true", help="Delete results instead of archiving them")

    p = sub.add_parser("dashboard", help="Start or stop the read-only live dashboard")
    _common(p)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--host", default=None)
    p.add_argument("--mode", default=None, choices=["eco", "live", "turbo"])
    p.add_argument("--demo", action="store_true")
    p.add_argument("--all", action="store_true", help="One dashboard per row of dashboard.sectors in rhexas.toml")
    p.add_argument("--detach", action="store_true", help="Leave the server running in the background")
    p.add_argument("--stop", action="store_true", help="Stop the dashboard pid file for this sector")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--local-only", action="store_true", help="Bind 127.0.0.1")

    p = sub.add_parser("worker", help="Friends beta: claim batches from the coordinator and search them")
    p.add_argument("--config", default=None, help="TOML config (default: discover rhexas.toml)")
    p.add_argument("--root", default=None, help="Project root. The worker cache is created under it.")
    p.add_argument("--min-free-gb", type=float, default=None, help="Stop if the drive has less than this much free space (worker default 5)")
    p.add_argument("--coordinator", default=None, help="Coordinator URL (default https://api.megarhexas.com)")
    p.add_argument("--login", action="store_true", help="Link this computer to an account and save a device token (no paste)")
    p.add_argument("--token", default=None, help="Invite or device token. Prefer --token-file or --login so it stays out of shell history.")
    p.add_argument("--token-file", default=None, help="File containing the worker token")
    p.add_argument("--procs", type=int, default=None, help="BLS processes (default 2, low priority)")
    p.add_argument("--threads", type=int, default=None, help="MAST download threads (default 2)")
    p.add_argument("--priority", default=None, choices=["idle", "low", "normal"], help="Process priority (default low)")
    p.add_argument("--max-cache-gb", type=float, default=None, dest="max_cache_gb", help="Delete raw light curves to stay under this (default 20)")
    p.add_argument("--poll-seconds", type=float, default=None, help="Wait between empty claims (default 60)")
    p.add_argument("--cache-dir", default=None, help="Where this worker keeps temporary light curves")
    p.add_argument("--once", action="store_true", help="Process one batch (or exit if the queue is empty) and stop")
    p.add_argument("--dashboard", action="store_true", help="Also serve the local live dashboard and open it in a browser")
    _dashboard_flags(p)

    p = sub.add_parser("worker-dashboard", help="Open a local dashboard for a worker that is already running")
    p.add_argument("--config", default=None, help="TOML config (default: discover rhexas.toml)")
    p.add_argument("--root", default=None, help="Project root. The worker cache is created under it.")
    p.add_argument("--coordinator", default=None, help="Shown on the page until the worker writes its own URL. This command does not connect.")
    p.add_argument("--procs", type=int, default=None, help="Shown until the worker publishes its own process count")
    p.add_argument("--threads", type=int, default=None, help="Shown until the worker publishes its own download threads")
    p.add_argument("--priority", default=None, choices=["idle", "low", "normal"])
    p.add_argument("--max-cache-gb", type=float, default=None, dest="max_cache_gb", help="Cache cap drawn on the page until the worker publishes its own")
    p.add_argument("--cache-dir", default=None, help="Worker cache to attach to (default worker-cache)")
    _dashboard_flags(p)

    from .coord.cli import add_subcommands, config_parent
    parent = config_parent()
    p = sub.add_parser("coord", parents=[parent], help="Friends-beta coordinator: init, enroll, tokens, serve")
    add_subcommands(p.add_subparsers(dest="coord_cmd", required=True), parent)
    return parser


def _dashboard_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dashboard-port", type=int, default=None, help="Dashboard port (default 8080). If it is busy, the next free port is used.")
    parser.add_argument(
        "--dashboard-host", default=None,
        help="Dashboard bind address (default 127.0.0.1, this machine only). Set an address such as 0.0.0.0 to allow your LAN.",
    )
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser")


def _overrides(args) -> dict:
    return {
        "root": getattr(args, "root", None),
        "sector": getattr(args, "sector", None),
        "data_dir": getattr(args, "data_dir", None),
        "results_root": getattr(args, "results_dir", None),
        "logs_dir": getattr(args, "logs_dir", None),
        "catalogs_dir": getattr(args, "catalogs_dir", None),
        "multisector_dir": getattr(args, "multisector_dir", None),
        "min_free_gb": getattr(args, "min_free_gb", None),
        "max_project_gb": getattr(args, "max_project_gb", None),
        "download_threads": getattr(args, "threads", None) if args.cmd == "download" else None,
        "search_procs": getattr(args, "procs", None) if args.cmd == "search" else None,
        "dashboard_host": "127.0.0.1" if getattr(args, "local_only", False) else getattr(args, "host", None),
        "dashboard_port": getattr(args, "port", None),
        "dashboard_mode": getattr(args, "mode", None),
    }


def _cfg(args) -> Config:
    return load_config(path=args.config, overrides=_overrides(args))


def _tic_set(args) -> set[int] | None:
    positional = getattr(args, "tics", ()) or ()
    return parse_tics(text=getattr(args, "tics_text", "") or "", path=getattr(args, "tics_file", "") or "", positional=positional)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "coord":
        from .coord.cli import execute
        return execute(args)
    try:
        cfg = _cfg(args)
        return _dispatch(args, cfg)
    except ConfigError as exc:
        print(f"rhexas: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"rhexas: {exc}", file=sys.stderr)
        return 1


def _dispatch(args, cfg: Config) -> int:
    cmd = args.cmd
    if cmd == "download":
        from .download import run
        return run(cfg, limit=args.limit, threads=args.threads, tics=_tic_set(args))
    if cmd == "search":
        from .search import run
        return run(cfg, procs=args.procs, limit=args.limit, out=args.out, tics=_tic_set(args))
    if cmd == "filter":
        from .filter import run
        return run(cfg, raw=args.raw, out=args.out, plots=args.plots, tics=_tic_set(args))
    if cmd == "vet":
        from .vet import run, run_new
        if args.new and not (args.tics or args.tics_text or args.tics_file):
            return run_new(cfg)
        tics = _tic_set(args)
        if args.new:
            from .vet import new_tics
            tics = set(new_tics(cfg)) | (tics or set())
        if not tics:
            print("rhexas vet: give TIC IDs, --tics-file, or --new", file=sys.stderr)
            return 2
        return run(cfg, sorted(tics))
    if cmd == "multisector":
        from .multisector import run
        tics = _tic_set(args)
        if not tics:
            print("rhexas multisector: give at least one TIC ID", file=sys.stderr)
            return 2
        return run(cfg, sorted(tics), period=args.period, t0=args.t0)
    if cmd == "check-known":
        from .check_known import run
        return run(cfg, args.raw)
    if cmd == "run":
        from .pipeline import run_sectors
        sectors = parse_sector_list(args.sectors) or [cfg.sector]
        return run_sectors(
            cfg, sectors, tics=_tic_set(args), limit=args.limit,
            threads=args.threads, procs=args.procs, plots=args.plots,
        )
    if cmd == "rerun":
        from .pipeline import queue, run_sectors
        sectors = parse_sector_list(args.sectors) or [cfg.sector]
        if args.queue:
            return queue(
                cfg, sectors, predownload=parse_sector_list(args.predownload), wait_pid=args.wait_pid,
                tics=_tic_set(args), threads=args.threads, procs=args.procs, plots=args.plots,
                poll_s=args.poll_seconds, skip_done_downloads=True, vet=True,
            )
        return run_sectors(
            cfg, sectors, tics=_tic_set(args), limit=args.limit,
            threads=args.threads, procs=args.procs, plots=args.plots,
            skip_done_downloads=True, vet=True,
        )
    if cmd == "queue":
        from .pipeline import queue
        sectors = parse_sector_list(args.sectors)
        if not sectors:
            print("rhexas queue: --sectors is required", file=sys.stderr)
            return 2
        return queue(
            cfg, sectors, predownload=parse_sector_list(args.predownload), wait_pid=args.wait_pid,
            tics=_tic_set(args), threads=args.threads, procs=args.procs, plots=args.plots,
            poll_s=args.poll_seconds, skip_done_downloads=args.rerun, vet=args.rerun,
        )
    if cmd == "reset-results":
        from .reset import reset_results
        sectors = parse_sector_list(args.sectors)
        return reset_results(cfg, sectors or None, delete=args.delete)
    if cmd == "worker":
        from .worker.loop import run_from_args
        return run_from_args(cfg, args)
    if cmd == "worker-dashboard":
        from .worker.dashboard import run_attached
        return run_attached(cfg, args)
    if cmd == "dashboard":
        from .dashboard_cmd import launch, launch_all, stop
        if args.stop:
            return stop(cfg, sector=cfg.sector, demo=args.demo)
        host = cfg.dashboard.host
        mode = cfg.dashboard.mode
        if args.all:
            return launch_all(cfg, host=host, mode=mode, detach=True)
        port = cfg.dashboard.port
        if args.demo and args.port is None:
            port = cfg.dashboard.demo_port
        return launch(
            cfg, sector=cfg.sector, port=port, host=host, mode=mode, demo=args.demo,
            detach=args.detach, open_browser=not args.no_browser,
        )
    print(f"rhexas: unknown command {cmd}", file=sys.stderr)
    return 2
