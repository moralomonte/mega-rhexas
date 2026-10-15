"""Local live dashboard for one worker. Reads the cache; never calls the coordinator.

``rhexas worker --dashboard`` serves this in a daemon thread. ``rhexas worker-dashboard``
serves it in its own process and attaches to a worker that is already running.
The bind address is 127.0.0.1 unless ``--dashboard-host`` says otherwise.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .live import LOG_NAME, PACE_MODES, Store, redact, read_log_tail, snapshot
from .present import (
    cycle_star,
    detail_payload,
    fresh_logs,
    mission_cands,
    mission_hist,
    mission_lc,
    mission_logs,
    mission_sky,
    mission_state,
    mode_event,
    sectors_payload,
)

_STATIC = Path(__file__).resolve().parent / "static"
_PORT_SPAN = 50
_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/img/triskelion.svg": ("img/triskelion.svg", "image/svg+xml"),
    "/img/favicon.svg": ("img/favicon.svg", "image/svg+xml"),
    "/img/favicon-32.png": ("img/favicon.svg", "image/svg+xml"),
    "/img/favicon-192.png": ("img/favicon.svg", "image/svg+xml"),
    "/favicon.ico": ("img/favicon.svg", "image/svg+xml"),
}


def dumps_safe(payload) -> bytes:
    """JSON for the browser. Tokens are redacted again on the way out."""
    try:
        text = json.dumps(payload, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        text = '{"version":1,"error":"state unreadable","candidates":[],"lifetime":{}}'
    return redact(text).encode("utf-8")


def fallback_resources(cfg) -> dict:
    """Numbers from config, used until the worker has written its own. No token."""
    from .live import public_coordinator_url
    worker = cfg.worker
    return {
        "procs": worker.procs,
        "threads": worker.threads,
        "priority": worker.priority,
        "max_cache_gb": worker.max_cache_gb,
        "coordinator_url": public_coordinator_url(worker.coordinator_url),
    }


def resolve_bind(host: str | None, port: int | None) -> tuple[str, int]:
    """Default 127.0.0.1:8080. ``port`` 0 asks the OS for a free port."""
    chosen_host = (host or "").strip() or "127.0.0.1"
    chosen_port = 8080 if port is None else int(port)
    if not (0 <= chosen_port <= 65535):
        raise ValueError(f"dashboard port out of range: {chosen_port}")
    return chosen_host, chosen_port


def page_host(host: str) -> str:
    """Address a browser on this machine can open."""
    if host in {"0.0.0.0", "::", ""}:
        return "127.0.0.1"
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _loopback(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    # Leave the default False so a busy port stays busy and we can pick the next one.
    allow_reuse_address = False

    def __init__(self, server_address, handler):
        super().__init__(server_address, handler)
        self.dashboard_stop = threading.Event()


def bind_server(host: str, port: int, handler) -> tuple[DashboardServer, int]:
    """Bind ``port``, or the next free port in a short range. ``0`` is ephemeral."""
    if port == 0:
        httpd = DashboardServer((host, 0), handler)
        return httpd, int(httpd.server_address[1])
    last: Exception | None = None
    end = min(65535, port + _PORT_SPAN - 1)
    for candidate in range(port, end + 1):
        try:
            return DashboardServer((host, candidate), handler), candidate
        except OSError as exc:
            last = exc
    raise OSError(f"no free dashboard port from {port} to {end}") from last


def _static_bytes(name: str) -> bytes:
    root = _STATIC.resolve()
    path = (root / name).resolve()
    if path != root and root not in path.parents:
        raise FileNotFoundError(name)
    if not path.is_file():
        raise FileNotFoundError(name)
    return path.read_bytes()


def make_handler(cache: Path, fallback: dict | None):
    cache = Path(cache)
    fallback = dict(fallback or {})

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            try:
                self._get()
            except Exception:
                self._try_send(500, b"error", "text/plain; charset=utf-8")

        def do_POST(self):
            try:
                self._post()
            except Exception:
                self._try_send(500, b"error", "text/plain; charset=utf-8")

        def do_PUT(self):
            self._try_send(405, b"read only", "text/plain; charset=utf-8")

        do_DELETE = do_PUT

        def _post(self):
            parts = urlsplit(self.path)
            if (parts.path or "/") != "/api/mode":
                self._drain()
                self._try_send(405, b"read only", "text/plain; charset=utf-8")
                return
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            raw = self._drain(limit=256)
            if raw is None or ctype != "application/json":
                self._try_send(400, b"mode must be eco, live, or turbo", "text/plain; charset=utf-8")
                return
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._try_send(400, b"mode must be eco, live, or turbo", "text/plain; charset=utf-8")
                return
            mode = data.get("mode") if isinstance(data, dict) else None
            if mode not in PACE_MODES:
                self._try_send(400, b"mode must be eco, live, or turbo", "text/plain; charset=utf-8")
                return
            try:
                Store(cache).request_mode(mode)
            except Exception:
                self._try_send(500, b"error", "text/plain; charset=utf-8")
                return
            self._send(200, dumps_safe({"ok": True, "requested": mode}), "application/json; charset=utf-8")

        def _drain(self, limit: int = 4096):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length < 0:
                return None
            if length > limit:
                self.rfile.read(length)
                return None
            if length == 0:
                return b""
            return self.rfile.read(length)

        def _get(self):
            parts = urlsplit(self.path)
            path = parts.path or "/"
            if path == "/events":
                query = parse_qs(parts.query)
                mode = (query.get("mode") or ["live"])[0]
                self._events(once="once" in query and "1" in query["once"], mode=mode)
                return
            if path == "/api/state":
                body = dumps_safe(snapshot(cache, fallback=fallback))
                self._send(200, body, "application/json; charset=utf-8")
                return
            if path == "/api/log":
                lines, size = read_log_tail(cache, limit=200)
                body = dumps_safe({"lines": lines, "size": size, "path": LOG_NAME})
                self._send(200, body, "application/json; charset=utf-8")
                return
            if path == "/api/sectors":
                body = dumps_safe(sectors_payload(snapshot(cache, fallback=fallback), self.server.server_address[1]))
                self._send(200, body, "application/json; charset=utf-8")
                return
            if path == "/api/detail":
                tic = (parse_qs(parts.query).get("tic") or ["0"])[0]
                try:
                    tic_i = int(tic)
                except ValueError:
                    tic_i = 0
                body = dumps_safe(detail_payload(snapshot(cache, fallback=fallback), tic_i))
                self._send(200, body, "application/json; charset=utf-8")
                return
            if path == "/api/pixels":
                body = dumps_safe({
                    "kind": "aperture",
                    "aperture": None,
                    "note": "Pixel movies stay on the sector dashboard. This page follows the worker cache.",
                })
                self._send(200, body, "application/json; charset=utf-8")
                return
            if path == "/api/cutout":
                self._send(404, b"no cutout in the worker cache", "text/plain; charset=utf-8")
                return
            spec = _FILES.get(path)
            if spec is None:
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            try:
                body = _static_bytes(spec[0])
            except OSError:
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            self._send(200, body, spec[1])

        def _ev(self, name: str, payload) -> None:
            body = dumps_safe(payload)
            if name:
                self.wfile.write(b"event: " + name.encode("ascii") + b"\n")
            self.wfile.write(b"data: " + body + b"\n\n")

        def _events(self, *, once: bool, mode: str):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close" if once else "keep-alive")
            self.end_headers()
            sent_snapshot = False
            seen_logs: list = []
            seen = {}
            while True:
                try:
                    snap = snapshot(cache, fallback=fallback)
                    if not sent_snapshot:
                        # The snapshot stays the first event so existing clients can poll one JSON object.
                        self.wfile.write(b"data: " + dumps_safe(snap) + b"\n\n")
                        sent_snapshot = True
                    resources = snap.get("resources") if isinstance(snap.get("resources"), dict) else {}
                    applied = str(resources.get("mode") or mode or "live")
                    if applied not in PACE_MODES:
                        applied = "live"
                    if applied != seen.get("mode"):
                        self._ev("mode", mode_event(applied))
                        seen["mode"] = applied
                    self._ev("state", mission_state(snap))
                    lines, _size = read_log_tail(cache, limit=400)
                    fresh = fresh_logs(seen_logs, lines)
                    if fresh:
                        self._ev("log", mission_logs(fresh))
                        seen_logs = list(lines)
                    cands = mission_cands(snap)
                    cand_key = tuple((row["tic"], row["score"]) for row in cands)
                    if cand_key != seen.get("cands"):
                        self._ev("cands", cands)
                        seen["cands"] = cand_key
                    hist = mission_hist(snap)
                    hist_key = (tuple(hist["sde"][0]), tuple(hist["snr"][0]))
                    if hist_key != seen.get("hist"):
                        self._ev("hist", hist)
                        seen["hist"] = hist_key
                    sky = mission_sky(snap)
                    sky_key = tuple(tuple(point) for point in sky)
                    if sky_key != seen.get("sky"):
                        self._ev("sky", sky)
                        seen["sky"] = sky_key
                    star = cycle_star(snap)
                    lc = mission_lc(star)
                    lc_key = None if lc is None else (lc["tic"], lc["status"], lc["t"])
                    if lc is not None and lc_key != seen.get("lc"):
                        self._ev("lc", lc)
                        seen["lc"] = lc_key
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    return
                except Exception:
                    return
                if once or self.server.dashboard_stop.wait(1.0):
                    self.close_connection = True
                    return

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _try_send(self, code: int, body: bytes, content_type: str) -> None:
            try:
                self._send(code, body, content_type)
            except OSError:
                return

    return Handler


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception as exc:
        print(f"could not open a browser for {url}: {exc}", flush=True)


def _announce(host: str, requested: int, chosen: int, url: str) -> None:
    print(f"worker dashboard {url}  (bind {host}:{chosen})", flush=True)
    if requested and chosen != requested:
        print(f"port {requested} was busy; using {chosen}", flush=True)
    if not _loopback(host):
        print("this dashboard is reachable from other machines on that address", flush=True)


def _polite() -> None:
    try:
        import psutil
        psutil.Process().nice(psutil.IDLE_PRIORITY_CLASS if os.name == "nt" else 19)
    except Exception:
        pass


def launch_in_thread(cache: Path, *, host: str | None, port: int | None, open_browser: bool,
                     fallback: dict | None = None) -> str:
    """Start the dashboard beside the worker. Returns the URL opened in the browser."""
    bind_host, requested = resolve_bind(host, port)
    httpd, chosen = bind_server(bind_host, requested, make_handler(cache, fallback))
    url = f"http://{page_host(bind_host)}:{chosen}/"

    def _run() -> None:
        try:
            httpd.serve_forever(poll_interval=0.5)
        except Exception:
            return

    threading.Thread(target=_run, name="rhexas-worker-dashboard", daemon=True).start()
    _announce(bind_host, requested, chosen, url)
    if open_browser:
        _open_browser(url)
    return url


def run_attached(cfg, args) -> int:
    """``rhexas worker-dashboard``. Reads the cache only; the token is cleared before that."""
    import os
    from dataclasses import replace

    from .loop import cache_dir, resolve_worker

    env = {
        key: value for key, value in os.environ.items()
        if key not in {"RHEXAS_WORKER_TOKEN", "RHEXAS_WORKER_TOKEN_FILE"}
    }
    cfg = resolve_worker(
        cfg,
        coordinator=getattr(args, "coordinator", None),
        token="",
        token_file="",
        procs=getattr(args, "procs", None),
        threads=getattr(args, "threads", None),
        priority=getattr(args, "priority", None),
        max_cache_gb=getattr(args, "max_cache_gb", None),
        cache_dir_value=getattr(args, "cache_dir", None),
        environ=env,
    )
    cfg = replace(cfg, worker=replace(cfg.worker, token="", token_file=""))
    return serve(
        cache_dir(cfg),
        host=getattr(args, "dashboard_host", None),
        port=getattr(args, "dashboard_port", None),
        open_browser=not getattr(args, "no_browser", False),
        fallback=fallback_resources(cfg),
    )


def serve(cache: Path, *, host: str | None, port: int | None, open_browser: bool,
          fallback: dict | None = None) -> int:
    """Block on the dashboard until Ctrl+C. Used by ``rhexas worker-dashboard``."""
    _polite()
    bind_host, requested = resolve_bind(host, port)
    try:
        httpd, chosen = bind_server(bind_host, requested, make_handler(cache, fallback))
    except OSError as exc:
        print(f"rhexas worker-dashboard: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"rhexas worker-dashboard: {exc}", file=sys.stderr)
        return 2
    url = f"http://{page_host(bind_host)}:{chosen}/"
    _announce(bind_host, requested, chosen, url)
    if open_browser:
        _open_browser(url)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nworker dashboard stopped", flush=True)
        return 0
    finally:
        httpd.dashboard_stop.set()
        httpd.server_close()
    return 0
