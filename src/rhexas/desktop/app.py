"""Launch the friends-beta app: worker, local dashboard, tray.

``rhexas desktop`` is the command the Windows shortcut runs. A macOS bundle
should exec the same command. Defaults are the ones a non-technical person
should get without editing a config file: cache in app-data, low priority,
Live pace, a 20 GB cache cap, and the worker disk-space guard.
"""
from __future__ import annotations

import sys
import threading
import webbrowser

from rhexas import __version__
from rhexas.config import load_config
from rhexas.worker.dashboard import DashboardHooks, fallback_resources, launch_in_thread
from rhexas.worker.live import Store
from rhexas.worker.loop import cache_dir, resolve_worker, run_worker

from .paths import AppPaths
from .setup import SetupGate
from .tray import start_tray
from .updates import UpdateBoard, check_for_update


def ensure_stdio(log_path) -> None:
    """Give pythonw.exe somewhere to print. A console Python already has streams."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    path = log_path
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.is_file() and path.stat().st_size > 1_000_000:
            path.write_text("", encoding="utf-8")
    except OSError:
        pass
    handle = path.open("a", encoding="utf-8", errors="replace")
    if sys.stdout is None:
        sys.stdout = handle
    if sys.stderr is None:
        sys.stderr = handle


def apply_desktop_defaults(cfg, paths: AppPaths):
    """Force the friendly worker settings. Science thresholds stay as they are."""
    return resolve_worker(
        cfg,
        token_file=str(paths.token_file),
        priority="low",
        max_cache_gb=20,
        min_free_gb=5,
        cache_dir_value=str(paths.cache),
        environ={},
    )


def run_desktop(cfg, *, open_browser: bool = True, tray: bool = True, port: int | None = None,
                once: bool = False, quit_event: threading.Event | None = None,
                paths: AppPaths | None = None) -> int:
    paths = paths or AppPaths.discover()
    paths.ensure()
    ensure_stdio(paths.root / "desktop.log")
    cfg = apply_desktop_defaults(cfg, paths)
    cache = cache_dir(cfg)
    cache.mkdir(parents=True, exist_ok=True)
    gate = SetupGate(paths.root, cfg.worker.coordinator_url)
    board = UpdateBoard(__version__)
    url = launch_in_thread(
        cache,
        host="127.0.0.1",
        port=port,
        open_browser=open_browser,
        fallback=fallback_resources(cfg),
        services=DashboardHooks(setup=gate, updates=board),
    )
    stop = quit_event or threading.Event()

    def _check() -> None:
        board.set_offer(check_for_update(__version__))

    threading.Thread(target=_check, name="rhexas-update", daemon=True).start()
    icon = _start_tray(cache, url, stop) if tray else None
    print(f"Mega Rhexas  {url}", flush=True)
    print("cache in app data, low priority, Live pace, 20 GB cache cap, disk-space guard on", flush=True)
    try:
        if not gate.ready.is_set():
            print(
                "Sign in with megarhexas.com on the dashboard, or paste a token. It stays on this computer and is not shown again.",
                flush=True,
            )
            while not gate.ready.wait(0.4):
                if stop.is_set():
                    return 0
        return run_worker(cfg, once=once, quit_event=stop)
    finally:
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass


def _start_tray(cache, url: str, stop: threading.Event):
    def on_quit(icon) -> None:
        stop.set()
        try:
            icon.stop()
        except Exception:
            pass

    def toggle() -> None:
        store = Store(cache)
        store.request_user_pause(not store.user_paused())

    return start_tray(
        on_open=lambda: webbrowser.open(url),
        on_mode=lambda mode: Store(cache).request_mode(mode),
        on_toggle_pause=toggle,
        on_quit=on_quit,
        mode=lambda: Store(cache).requested_mode(),
        paused=lambda: Store(cache).user_paused(),
    )


def run_from_args(cfg, args) -> int:
    """Entry from ``rhexas desktop``. Ignores a checkout config so the app is self-contained."""
    del cfg
    paths = AppPaths.discover()
    fresh = load_config(search_files=False, environ={}, overrides={"root": paths.root})
    coordinator = getattr(args, "coordinator", None)
    if coordinator:
        fresh = resolve_worker(fresh, coordinator=coordinator, environ={})
    try:
        return run_desktop(
            fresh,
            open_browser=not getattr(args, "no_browser", False),
            tray=not getattr(args, "no_tray", False),
            port=getattr(args, "dashboard_port", None),
            once=bool(getattr(args, "once", False)),
        )
    except KeyboardInterrupt:
        print("\nstopped. The lease is still yours until it expires; open Mega Rhexas again to resume.", flush=True)
        return 0


def main(argv: list[str] | None = None) -> int:
    from rhexas.cli import main as cli_main
    return cli_main(["desktop", *(argv or [])])
