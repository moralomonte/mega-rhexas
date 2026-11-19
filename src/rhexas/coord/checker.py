"""Background catalog checks. One star at a time, never on the page request.

The HTTP server holds ``lock`` around each request. This thread takes the same
lock only while it reads or writes SQLite, and talks to the catalogs outside
that lock. A failed service is stored and tried again later; it does not stop
the coordinator.
"""
from __future__ import annotations

import threading
import traceback

from .catalogs import CatalogClient


class CatalogChecker:
    def __init__(self, store, *, fetcher=None, sleep=None, gap_s: float = 1.5, lock=None,
                 idle_s: float = 60, busy_s: float = 5):
        self.store = store
        self.client = CatalogClient(fetcher, gap_s=gap_s, sleep=sleep)
        self.lock = lock or threading.Lock()
        self.idle_s = idle_s
        self.busy_s = busy_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="rhexas-catalog-checks", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                traceback.print_exc()
                worked = False
            self._stop.wait(self.busy_s if worked else self.idle_s)

    def run_once(self) -> bool:
        """Refresh any stale list, then one star. False when there is nothing due."""
        with self.lock:
            job = self.store.next_catalog_job()
        if not job:
            return False
        refreshed = {}
        if job["files"]:
            refreshed = self.client.refresh_files(job["files"])
            with self.lock:
                for name, result in refreshed.items():
                    self.store.save_catalog_file(name, result)
        if job["tic"] is not None and job["need"]:
            outcome = self.client.check_star(job["tic"], job["need"], job.get("tic_payload"))
            with self.lock:
                self.store.save_catalog(job["tic"], outcome)
                self.store.log_event("info", _summary(job["tic"], outcome))
            return True
        if refreshed:
            with self.lock:
                bits = []
                for name, result in refreshed.items():
                    bits.append(f"{name} {'ok' if result.get('ok') else 'down'}")
                self.store.log_event("info", "catalog lists: " + ", ".join(bits))
        return bool(refreshed)


def _summary(tic: int, outcome: dict) -> str:
    bits = []
    for source, row in outcome.items():
        bits.append(f"{source} {'ok' if row.get('ok') else 'down'}")
    return f"catalog check TIC {tic}: " + ", ".join(bits)
