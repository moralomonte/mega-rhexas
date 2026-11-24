"""Background refresh for Phase 5 science sector watch and re-check flags."""
from __future__ import annotations

import threading
import traceback

from .store import iso


class ScienceRefresher:
    def __init__(self, store, *, lock=None, idle_s: float = 6 * 3600):
        self.store = store
        self.lock = lock or threading.Lock()
        self.idle_s = idle_s
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._pending = False
        self._meta_lock = threading.Lock()
        self._last_completed_at: str | None = None
        self._last_error: str | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="rhexas-science-refresh", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)

    def trigger(self) -> None:
        self._pending = True
        self._kick.set()

    def status(self) -> dict:
        with self._meta_lock:
            return {
                "refreshing": self._running.is_set(),
                "last_completed_at": self._last_completed_at,
                "last_error": self._last_error,
                "stale": self._last_completed_at is None,
            }

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._run_once()
            if self._stop.is_set():
                break
            if self._kick.wait(timeout=self.idle_s):
                self._kick.clear()

    def _run_once(self) -> None:
        if self._running.is_set():
            self._pending = True
            return
        self._running.set()
        self._pending = False
        try:
            from .science import refresh_candidate_recheck

            with self.lock:
                refresh_candidate_recheck(self.store)
            with self._meta_lock:
                self._last_completed_at = iso(self.store.clock())
                self._last_error = None
        except Exception as exc:
            traceback.print_exc()
            with self._meta_lock:
                self._last_error = str(exc)
        finally:
            self._running.clear()
        if self._pending and not self._stop.is_set():
            self._run_once()