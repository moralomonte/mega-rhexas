"""Threaded HTTP server. Bind it to localhost and put Caddy in front for HTTPS."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .httpapi import App, _MAX_BODY, _error
from .store import Store


def make_server(store: Store, host: str, port: int, *, accounts=None, smtp=None, mailer=None) -> ThreadingHTTPServer:
    app = App(store, accounts=accounts, smtp=smtp, mailer=mailer)
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self._handle()

        def do_POST(self):
            self._handle()

        def do_OPTIONS(self):
            self._handle()

        def log_message(self, fmt, *args):
            return

        def _handle(self):
            split = urlsplit(self.path)
            length = self.headers.get("Content-Length")
            try:
                size = int(length) if length else 0
            except ValueError:
                size = _MAX_BODY + 1
            if size > _MAX_BODY:
                response = _error(413, "body too large", "too_large")
            else:
                body = self.rfile.read(size) if size else b""
                with lock:
                    response = app.handle(self.command, split.path, split.query, self.headers, body)
            payload = response.body
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            for key, value in response.headers:
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)

    ThreadingHTTPServer.daemon_threads = True
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.store = store
    return httpd


def serve(store: Store, host: str, port: int, *, accounts=None, smtp=None, mailer=None) -> None:
    summary = store.reverify()
    if summary["promoted"] or summary["reissued"]:
        print(
            f"reverify: {summary['promoted']} verified, {summary['reissued']} reissued "
            f"(copies_required {summary['copies_required']})",
            flush=True,
        )
    httpd = make_server(store, host, port, accounts=accounts, smtp=smtp, mailer=mailer)
    bound_host, bound_port = httpd.server_address[:2]
    print(f"Mega Rhexas coordinator on http://{bound_host}:{bound_port}", flush=True)
    print(f"database {store.path}", flush=True)
    print("The status page asks for the admin token. Worker tokens cannot open it.", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    finally:
        httpd.server_close()
