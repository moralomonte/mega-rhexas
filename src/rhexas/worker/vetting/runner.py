"""One background thread, one candidate at a time.

The scan pipeline only calls ``notify``, which wakes this thread. The checks
themselves run here, so a slow catalog cannot stall a BLS worker.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from .catalogs import catalog_cache_bytes
from .checks import DISCLAIMER
from .evaluate import evaluate as default_evaluate
from .report import write_reports
from .store import (
    claim_next,
    detail_public,
    get_row,
    list_rows,
    mark_pending,
    recover_stale,
    save_result,
    sync_candidates,
)

ENABLE_NOTE = (
    "Vetting is turned off on this computer. To turn it on, set enabled = true "
    "under [vetting] in rhexas.toml and restart the worker. While it is off, "
    "this page does not contact SIMBAD, VizieR, or ExoFOP."
)

_RUNNERS: dict[str, "VettingRunner"] = {}
_REGISTRY = threading.Lock()


def _lower_priority() -> None:
    """Keep this thread behind the search. On Linux, nice is per thread."""
    try:
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.GetCurrentThread()
            ctypes.windll.kernel32.SetThreadPriority(handle, -1)
        else:
            os.nice(10)
    except Exception:
        return


def _connect(cache: Path):
    from rhexas.worker.live import DB_NAME, _connect as open_db
    return open_db(Path(cache) / DB_NAME, write=True)


class VettingRunner:
    def __init__(self, cache, settings, fetcher=None, evaluate=None, clock=None):
        self.cache = Path(cache)
        self.settings = settings
        self.fetcher = fetcher
        self.evaluate = evaluate or default_evaluate
        self.clock = clock or time.time
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> "VettingRunner":
        if self.alive or not getattr(self.settings, "enabled", False):
            return self
        self._thread = threading.Thread(target=self._loop, name="rhexas-vetting", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def wake(self) -> None:
        self._wake.set()

    def run_once(self) -> bool:
        """Process at most one candidate. Used by tests and by the thread."""
        if not getattr(self.settings, "enabled", False):
            return False
        now = float(self.clock())
        conn = _connect(self.cache)
        try:
            recover_stale(conn, now)
            sync_candidates(conn, now)
            claimed = claim_next(conn, now)
            job = None if claimed is None else {key: claimed[key] for key in claimed.keys()}
        finally:
            conn.close()
        if job is None:
            return False
        self._finish(job, now)
        return True

    def _finish(self, job, now: float) -> None:
        row = {key: job[key] for key in job.keys()}
        try:
            outcome = self.evaluate(self.cache, row, self.settings, self.fetcher, now=now)
        except Exception as exc:
            outcome = {
                "verdict": "Needs review",
                "result": {
                    "checks": [{
                        "name": "simbad",
                        "title": "SIMBAD",
                        "status": "unavailable",
                        "reason": f"Vetting stopped early ({str(exc)[:160]}).",
                        "numbers": {},
                    }],
                    "plot": None,
                    "links": {},
                    "disclaimer": DISCLAIMER,
                },
                "fields": {},
                "retry_at": now + 900,
                "attempts": int(row.get("attempts") or 0) + 1,
            }
        conn = _connect(self.cache)
        try:
            save_result(
                conn, int(row["tic"]), int(row["sector"]),
                verdict=outcome["verdict"],
                result=outcome["result"],
                fields=outcome.get("fields") or {},
                retry_at=outcome.get("retry_at"),
                attempts=int(outcome.get("attempts") or 0),
                now=now,
            )
            conn.commit()
        finally:
            conn.close()

    def _loop(self) -> None:
        _lower_priority()
        while not self._stop.is_set():
            worked = False
            try:
                worked = self.run_once()
            except Exception:
                worked = False
            if self._stop.is_set():
                return
            if not worked:
                self._wake.wait(2.0)
                self._wake.clear()
            else:
                # One candidate at a time, and yield so the search keeps the cores.
                self._stop.wait(0.25)


def _key(cache: Path) -> str:
    return str(Path(cache).resolve())


def ensure_runner(cache, settings, fetcher=None, evaluate=None) -> VettingRunner | None:
    """Start the background thread when vetting is enabled. A second call reuses it."""
    if not getattr(settings, "enabled", False):
        return None
    key = _key(cache)
    with _REGISTRY:
        current = _RUNNERS.get(key)
        if current is not None and current.alive:
            return current
        runner = VettingRunner(cache, settings, fetcher=fetcher, evaluate=evaluate)
        runner.start()
        _RUNNERS[key] = runner
        return runner


def notify(cache) -> None:
    """Wake a runner in this process. Does not vet anything itself."""
    with _REGISTRY:
        runner = _RUNNERS.get(_key(cache))
    if runner is not None:
        runner.wake()


def stop_all() -> None:
    with _REGISTRY:
        runners = list(_RUNNERS.values())
        _RUNNERS.clear()
    for runner in runners:
        runner.stop()


def _enabled(flag) -> bool:
    return bool(flag)


def _versioned(payload: dict) -> dict:
    from rhexas import __version__
    out = dict(payload)
    out["app_version"] = __version__
    return out


def status_payload(cache, enabled: bool) -> dict:
    """List for the page. Disabled means no catalog client is constructed."""
    if not _enabled(enabled):
        return _versioned({
            "enabled": False,
            "note": ENABLE_NOTE,
            "disclaimer": DISCLAIMER,
            "candidates": [],
            "cache_bytes": 0,
        })
    conn = _connect(cache)
    try:
        rows = list_rows(conn)
    finally:
        conn.close()
    return _versioned({
        "enabled": True,
        "note": "",
        "disclaimer": DISCLAIMER,
        "candidates": rows,
        "cache_bytes": catalog_cache_bytes(cache),
    })


def detail_payload(cache, tic: int, sector: int, enabled: bool) -> dict:
    if not _enabled(enabled):
        return _versioned({"enabled": False, "note": ENABLE_NOTE, "disclaimer": DISCLAIMER, "available": False})
    conn = _connect(cache)
    try:
        row = get_row(conn, tic, sector)
    finally:
        conn.close()
    if row is None:
        return _versioned({"enabled": True, "available": False, "tic": int(tic), "sector": int(sector),
                "disclaimer": DISCLAIMER, "note": "This candidate is not in the vetting table yet."})
    detail = detail_public(row)
    detail["enabled"] = True
    detail["available"] = True
    detail["disclaimer"] = detail.get("disclaimer") or DISCLAIMER
    return _versioned(detail)


def request_rerun(cache, tic: int, sector: int, enabled: bool) -> dict:
    if not _enabled(enabled):
        return _versioned({"ok": False, "enabled": False, "note": ENABLE_NOTE})
    conn = _connect(cache)
    try:
        mark_pending(conn, tic, sector)
        conn.commit()
    finally:
        conn.close()
    # Drop the cached catalog response so a re-run actually asks again.
    path = Path(cache) / "vetting" / "catalogs" / f"tic-{int(tic)}.json"
    try:
        path.unlink()
    except OSError:
        pass
    notify(cache)
    return _versioned({"ok": True, "enabled": True, "tic": int(tic), "sector": int(sector), "state": "pending"})


def export_report(cache, tic: int, sector: int, enabled: bool) -> dict:
    if not _enabled(enabled):
        return _versioned({"ok": False, "enabled": False, "note": ENABLE_NOTE})
    conn = _connect(cache)
    try:
        row = get_row(conn, tic, sector)
    finally:
        conn.close()
    if row is None or not row["result_json"]:
        return _versioned({"ok": False, "enabled": True, "note": "Nothing to export until vetting has finished for this candidate."})
    detail = detail_public(row)
    written = write_reports(cache, detail)
    return _versioned({"ok": True, "enabled": True, **written})
