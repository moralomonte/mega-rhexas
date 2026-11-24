"""Background local review: one coordinator shortlist candidate at a time."""
from __future__ import annotations

import contextlib
import os
import threading
import time
from pathlib import Path

from rhexas.config import ReviewConfig, VettingConfig

from ..vetting.checks import DISCLAIMER
from ..vetting.evaluate import evaluate as default_evaluate
from ..vetting.runner import _lower_priority
from .gate import (
    estimate_fits_bytes,
    may_start_review,
    processed_headroom,
    search_busy,
    search_wait_minutes,
    unit_boundary_open,
)
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

_REVIEW_JOB_MIN = 4.0


def _connect(cache: Path):
    from ..live import DB_NAME, _connect as open_db

    return open_db(Path(cache) / DB_NAME, write=True)


@contextlib.contextmanager
def _limited_compute_threads(n: int):
    """Cap BLAS/OpenMP while review runs so search keeps most cores."""
    n = max(1, int(n))
    keys = (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    )
    saved = {key: os.environ.get(key) for key in keys}
    for key in keys:
        os.environ[key] = str(n)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class ReviewRunner:
    def __init__(
        self,
        cache,
        vetting_settings,
        review_settings: ReviewConfig | None = None,
        coordinator_factory=None,
        evaluate=None,
        clock=None,
    ):
        self.cache = Path(cache)
        self.settings = vetting_settings
        self.review = review_settings or ReviewConfig()
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
        if not self.review.concurrent:
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
        if self.review.concurrent:
            self.wake()
        else:
            self.run_once(boundary=True)
        return count

    def run_once(self, *, boundary: bool = False) -> bool:
        """Process at most one queued job when scheduling allows."""
        concurrent = bool(self.review.concurrent)
        if boundary and concurrent:
            return False
        if not boundary:
            allowed, _ = may_start_review(self.cache, concurrent=concurrent)
            if not allowed:
                return False
        elif not unit_boundary_open(self.cache)[0]:
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

        allowed, _ = may_start_review(self.cache, concurrent=concurrent)
        if not allowed and not concurrent:
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
            return False

        with _limited_compute_threads(self.review.blas_threads):
            self._finish(claimed, now)
        return True

    def _finish(self, job, now: float) -> None:
        tic = int(job["tic"])
        sector = int(job["sector"])
        if (tic, sector) in self._cancel:
            self._cancel.discard((tic, sector))
            return

        est_bytes = estimate_fits_bytes(sector)
        used, limit, cap_note = processed_headroom(self.cache)
        download_note = f"~{est_bytes / 1_000_000:.1f} MB light curve if not cached"
        if cap_note:
            download_note = f"{cap_note} {download_note}"
        lc_download = None
        if self.evaluate is default_evaluate:
            from .fetch import ensure_lightcurve, ensure_tpf

            lc_info = ensure_lightcurve(self.cache, tic, sector)
            if lc_info.get("note"):
                download_note = str(lc_info["note"])
                if cap_note and cap_note not in download_note:
                    download_note = f"{cap_note} {download_note}"
            try:
                est_bytes = int(lc_info.get("bytes") or 0)
            except (TypeError, ValueError):
                est_bytes = 0

            def lc_download(cache, one_tic, one_sector, kind, _info=lc_info):
                if kind == "tpf":
                    return ensure_tpf(cache, one_tic, one_sector)
                return _info

        row = {
            "tic": tic,
            "sector": sector,
            "period": job.get("period"),
            "t0": job.get("t0"),
            "duration": job.get("duration"),
            "depth": job.get("depth"),
            "snr": job.get("snr"),
            "sde": job.get("sde"),
            "ra": job.get("ra"),
            "dec": job.get("dec"),
            "attempts": 0,
        }
        from dataclasses import replace

        eval_settings = self.settings
        if not getattr(eval_settings, "enabled", False):
            eval_settings = replace(eval_settings, enabled=True)
        try:
            extra = {"now": now}
            if lc_download is not None:
                extra["download"] = lc_download
            outcome = self.evaluate(self.cache, row, eval_settings, fetcher=None, **extra)
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
                self._wake.wait(1.5 if self.review.concurrent else 3.0)
                self._wake.clear()
            else:
                self._stop.wait(0.5 if self.review.concurrent else 1.0)


def _key(cache: Path) -> str:
    return str(Path(cache).resolve())


def ensure_runner(
    cache,
    vetting_settings,
    review_settings: ReviewConfig | None = None,
    coordinator_factory=None,
    evaluate=None,
) -> ReviewRunner:
    review_settings = review_settings or ReviewConfig()
    key = _key(cache)
    with _REGISTRY:
        current = _RUNNERS.get(key)
        if current is not None and current.alive:
            if coordinator_factory and current.coordinator_factory is None:
                current.coordinator_factory = coordinator_factory
            current.review = review_settings
            current.settings = vetting_settings
            return current
        runner = ReviewRunner(
            cache,
            vetting_settings,
            review_settings=review_settings,
            coordinator_factory=coordinator_factory,
            evaluate=evaluate,
        )
        runner.start()
        _RUNNERS[key] = runner
        return runner


def stop_all() -> None:
    with _REGISTRY:
        runners = list(_RUNNERS.values())
        _RUNNERS.clear()
    for runner in runners:
        runner.stop()


def run_at_boundary(
    cache,
    vetting_settings,
    review_settings: ReviewConfig | None = None,
    coordinator_factory=None,
    evaluate=None,
) -> bool:
    """At most one review job between search units (``review.concurrent = false``)."""
    review_settings = review_settings or ReviewConfig()
    if review_settings.concurrent:
        return False
    runner = ensure_runner(
        cache,
        vetting_settings,
        review_settings=review_settings,
        coordinator_factory=coordinator_factory,
        evaluate=evaluate,
    )
    return runner.run_once(boundary=True)


def notify_boundary(cache, vetting_settings, review_settings: ReviewConfig | None = None, coordinator_factory=None) -> None:
    """Worker loop hook after a unit uploads."""
    review_settings = review_settings or ReviewConfig()
    if review_settings.concurrent:
        ensure_runner(cache, vetting_settings, review_settings=review_settings, coordinator_factory=coordinator_factory).wake()
        return
    run_at_boundary(cache, vetting_settings, review_settings, coordinator_factory=coordinator_factory)


def _queue_positions(rows: list[dict]) -> dict[tuple[int, int], int]:
    queued = [
        row for row in rows
        if (row.get("state") or "queued") == "queued" and not row.get("cancelled")
    ]
    queued.sort(key=lambda row: (row.get("rank") if row.get("rank") is not None else 9999, row.get("updated_at") or 0, row["tic"]))
    return {(int(row["tic"]), int(row["sector"])): i + 1 for i, row in enumerate(queued)}


def _job_scheduler_note(row: dict, *, concurrent: bool, position: int | None, wait_min: float | None) -> str:
    state = str(row.get("state") or "queued")
    if state == "running":
        return "checks running (low priority)" if concurrent else "checks running"
    if state in ("done", "cancelled"):
        return ""
    if state != "queued":
        return state
    if concurrent:
        if position and position > 1:
            est = max(1, int(round((position - 1) * _REVIEW_JOB_MIN)))
            return f"queued · position {position} · ~{est} min"
        return "queued · runs in the background while search continues (low priority)"
    unit_wait = wait_min
    if unit_wait is not None:
        unit_part = f"waits for the current unit to finish, ~{max(1, int(round(unit_wait)))} min"
    else:
        unit_part = "waits for the current unit to finish"
    if position and position > 1:
        job_part = f"position {position} · ~{max(1, int(round((position - 1) * _REVIEW_JOB_MIN)))} min after that"
        return f"{unit_part}; {job_part}"
    return unit_part


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


def status_payload(cache, fetch_shortlist, vetting_settings, review_settings: ReviewConfig | None = None) -> dict:
    from rhexas import __version__

    review_settings = review_settings or ReviewConfig()
    conn = _connect(cache)
    try:
        migrate(conn)
        rows = list_rows(conn)
    finally:
        conn.close()
    coord = shortlist_payload(fetch_shortlist)
    positions = _queue_positions(rows)
    wait_min = search_wait_minutes(cache)
    busy, busy_reason = search_busy(cache)
    concurrent = bool(review_settings.concurrent)
    jobs = []
    for row in rows:
        key = (int(row["tic"]), int(row["sector"]))
        position = positions.get(key)
        enriched = dict(row)
        enriched["queue_position"] = position
        enriched["scheduler_note"] = _job_scheduler_note(
            row, concurrent=concurrent, position=position, wait_min=wait_min,
        )
        jobs.append(enriched)
    boundary_open, boundary_detail = unit_boundary_open(cache)
    return {
        "app_version": __version__,
        "disclaimer": DISCLAIMER_NOTE,
        "catalog_enabled": bool(getattr(vetting_settings, "enabled", False)),
        "coordinator": coord,
        "jobs": jobs,
        "scheduler": {
            "mode": "concurrent" if concurrent else "boundary",
            "concurrent": concurrent,
            "search_busy": busy,
            "search_busy_reason": busy_reason or "",
            "boundary_open": boundary_open,
            "boundary_detail": boundary_detail,
            "unit_wait_min": wait_min,
            "blas_threads": int(review_settings.blas_threads),
        },
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


def queue_from_request(
    cache,
    items: list[dict],
    vetting_settings,
    review_settings: ReviewConfig | None = None,
    coordinator_factory=None,
) -> dict:
    review_settings = review_settings or ReviewConfig()
    runner = ensure_runner(
        cache,
        vetting_settings,
        review_settings=review_settings,
        coordinator_factory=coordinator_factory,
    )
    count = runner.queue_items(items)
    return {"ok": True, "queued": count, "scheduler_mode": "concurrent" if review_settings.concurrent else "boundary"}