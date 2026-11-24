"""Background local review: one coordinator shortlist candidate at a time."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from ..vetting.checks import DISCLAIMER
from ..vetting.evaluate import evaluate as default_evaluate
from ..vetting.runner import _lower_priority
from .gate import estimate_fits_bytes, processed_headroom, search_busy
from .store import (
    claim_next,
    detail_public_row,
    enqueue_selected,
    get_row,
    list_rows,
    mark_cancelled,
    migrate,
    save_result,
)

DISCLAIMER_NOTE = (
    "Local review checks transiting signal candidates only. "
    "They do not confirm a planet and are not submitted to ExoFOP."
)

_RUNNERS: dict[str, "ReviewRunner"] = {}
_REGISTRY = threading.Lock()


def _connect(cache: Path):
    from ..live import DB_NAME, _connect as open_db

    return open_db(Path(cache) / DB_NAME, write=True)


class ReviewRunner:
    def __init__(self, cache, vetting_settings, coordinator_factory=None, evaluate=None, clock=None):
        self.cache = Path(cache)
        self.settings = vetting_settings
        self.coordinator_factory = coordinator_factory
        self.evaluate = evaluate or default_evaluate
        self.clock = clock or time.time
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._cancel: set[tuple[int, int]] = set()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> "ReviewRunner":
        if self.alive:
            return self
        self._thread = threading.Thread(target=self._loop, name="rhexas-review", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def wake(self) -> None:
        self._wake.set()

    def request_cancel(self, tic: int, sector: int) -> None:
        key = (int(tic), int(sector))
        self._cancel.add(key)
        conn = _connect(self.cache)
        try:
            migrate(conn)
            mark_cancelled(conn, tic, sector)
            conn.commit()
        finally:
            conn.close()
        self.wake()

    def queue_items(self, items: list[dict]) -> int:
        now = float(self.clock())
        conn = _connect(self.cache)
        try:
            migrate(conn)
            count = enqueue_selected(conn, items, now)
            conn.commit()
        finally:
            conn.close()
        self.wake()
        return count

    def run_once(self) -> bool:
        busy, _ = search_busy(self.cache)
        if busy:
            return False
        now = float(self.clock())
        conn = _connect(self.cache)
        try:
            migrate(conn)
            claimed = claim_next(conn, now)
            conn.commit()
        finally:
            conn.close()
        if claimed is None:
            return False
        tic, sector = int(claimed["tic"]), int(claimed["sector"])
        if (tic, sector) in self._cancel:
            self._cancel.discard((tic, sector))
            return True
        self._finish(claimed, now)
        return True

    def _finish(self, job, now: float) -> None:
        tic = int(job["tic"])
        sector = int(job["sector"])
        if (tic, sector) in self._cancel:
            self._cancel.discard((tic, sector))
            return
        busy, reason = search_busy(self.cache)
        if busy:
            conn = _connect(self.cache)
            try:
                migrate(conn)
                conn.execute(
                    "UPDATE local_review SET state='queued', updated_at=? WHERE tic=? AND sector=?",
                    (float(now), tic, sector),
                )
                conn.commit()
            finally:
                conn.close()
            return

        est_bytes = estimate_fits_bytes(sector)
        used, limit, cap_note = processed_headroom(self.cache)
        download_note = f"~{est_bytes / 1_000_000:.1f} MB light curve if not cached"
        if cap_note:
            download_note = f"{cap_note} {download_note}"

        row = {
            "tic": tic,
            "sector": sector,
            "period": job.get("period"),
            "t0": job.get("t0"),
            "duration": job.get("duration"),
            "depth": job.get("depth"),
            "snr": job.get("snr"),
            "sde": job.get("sde"),
            "attempts": 0,
        }
        from dataclasses import replace

        eval_settings = self.settings
        if not getattr(eval_settings, "enabled", False):
            eval_settings = replace(eval_settings, enabled=True)
        try:
            outcome = self.evaluate(self.cache, row, eval_settings, fetcher=None, now=now)
        except Exception as exc:
            outcome = {
                "verdict": "Needs review",
                "result": {
                    "checks": [{
                        "name": "runner",
                        "title": "Local review",
                        "status": "unavailable",
                        "reason": f"Checks stopped early ({str(exc)[:120]}).",
                        "numbers": {},
                    }],
                    "plot": None,
                    "links": {},
                    "disclaimer": DISCLAIMER,
                },
            }

        upload_state = "skipped"
        upload_detail = "coordinator upload not configured"
        client = self.coordinator_factory() if callable(self.coordinator_factory) else None
        check_key = f"s{sector}-local-{int(now)}"
        if client is not None:
            checks = (outcome.get("result") or {}).get("checks") or []
            plot = (outcome.get("result") or {}).get("plot")
            body = {
                "tic": tic,
                "sector": sector,
                "check_key": check_key,
                "verdict": outcome.get("verdict") or "Needs review",
                "note": "Worker local review (untrusted)",
                "checks": checks,
                "plot": plot or {},
                "app_version": "",
            }
            try:
                from rhexas import __version__

                body["app_version"] = __version__
                uploaded = client.post_local_check(body)
                upload_state = "ok" if uploaded.get("stored") else "warn"
                upload_detail = "stored on coordinator" if uploaded.get("stored") else str(uploaded)[:120]
            except Exception as exc:
                upload_state = "fail"
                upload_detail = str(exc)[:160]

        conn = _connect(self.cache)
        try:
            migrate(conn)
            save_result(
                conn,
                tic,
                sector,
                verdict=outcome.get("verdict"),
                result=outcome.get("result") or {},
                upload_state=upload_state,
                upload_detail=upload_detail,
                download_bytes=est_bytes,
                download_note=download_note,
                check_key=check_key,
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
                self._wake.wait(3.0)
                self._wake.clear()
            else:
                self._stop.wait(0.5)


def _key(cache: Path) -> str:
    return str(Path(cache).resolve())


def ensure_runner(cache, vetting_settings, coordinator_factory=None, evaluate=None) -> ReviewRunner:
    key = _key(cache)
    with _REGISTRY:
        current = _RUNNERS.get(key)
        if current is not None and current.alive:
            if coordinator_factory and current.coordinator_factory is None:
                current.coordinator_factory = coordinator_factory
            return current
        runner = ReviewRunner(cache, vetting_settings, coordinator_factory=coordinator_factory, evaluate=evaluate)
        runner.start()
        _RUNNERS[key] = runner
        return runner


def stop_all() -> None:
    with _REGISTRY:
        runners = list(_RUNNERS.values())
        _RUNNERS.clear()
    for runner in runners:
        runner.stop()


def shortlist_payload(fetch_shortlist) -> dict:
    """Coordinator shortlist for the Review page."""
    from rhexas import __version__

    if not callable(fetch_shortlist):
        return {
            "ok": False,
            "disclaimer": DISCLAIMER_NOTE,
            "detail": "Coordinator access is not configured on this dashboard. Run the desktop worker.",
            "shortlist": [],
            "progress": {},
        }
    try:
        body = fetch_shortlist()
    except Exception as exc:
        return {
            "ok": False,
            "disclaimer": DISCLAIMER_NOTE,
            "detail": str(exc)[:200],
            "shortlist": [],
            "progress": {},
        }
    if not isinstance(body, dict):
        return {"ok": False, "detail": "unexpected coordinator response", "shortlist": [], "progress": {}}
    return {
        "ok": True,
        "disclaimer": body.get("disclaimer") or DISCLAIMER_NOTE,
        "shortlist": body.get("shortlist") or [],
        "progress": body.get("progress") or {},
        "candidates": body.get("candidates") or [],
        "app_version": __version__,
    }


def status_payload(cache, fetch_shortlist, vetting_settings) -> dict:
    from rhexas import __version__

    conn = _connect(cache)
    try:
        migrate(conn)
        rows = list_rows(conn)
    finally:
        conn.close()
    coord = shortlist_payload(fetch_shortlist)
    return {
        "app_version": __version__,
        "disclaimer": DISCLAIMER_NOTE,
        "catalog_enabled": bool(getattr(vetting_settings, "enabled", False)),
        "coordinator": coord,
        "jobs": rows,
    }


def detail_payload(cache, tic: int, sector: int) -> dict:
    from rhexas import __version__

    conn = _connect(cache)
    try:
        migrate(conn)
        row = get_row(conn, tic, sector)
    finally:
        conn.close()
    if row is None:
        return {"available": False, "tic": tic, "sector": sector, "app_version": __version__}
    detail = detail_public_row(row)
    detail["available"] = True
    detail["app_version"] = __version__
    return detail


def queue_from_request(cache, items: list[dict], vetting_settings) -> dict:
    runner = ensure_runner(cache, vetting_settings)
    count = runner.queue_items(items)
    return {"ok": True, "queued": count}