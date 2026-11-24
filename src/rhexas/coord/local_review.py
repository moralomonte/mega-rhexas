"""Untrusted local review payloads from worker desktops.

Nothing here is treated as confirmed vetting. Records are additive and size-limited.
"""
from __future__ import annotations

import json
import re

_MAX_BODY = 96_000
_MAX_CHECKS = 48
_MAX_STRING = 2_000
_MAX_PLOT_POINTS = 512
_CHECK_STATUSES = frozenset({"pass", "warn", "fail", "unavailable"})
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$", re.IGNORECASE)

DISCLAIMER = (
    "Untrusted local check from a worker computer. "
    "Not a confirmed planet and not submitted to ExoFOP."
)


class LocalReviewError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _trim(text: str, limit: int) -> str:
    if not isinstance(text, str):
        raise LocalReviewError("text fields must be strings")
    text = text.strip()
    if len(text) > limit:
        raise LocalReviewError(f"text field exceeds {limit} characters", 413)
    return text


def _finite_list(values, *, limit: int, name: str) -> list[float]:
    if not isinstance(values, list):
        raise LocalReviewError(f"{name} must be a list")
    if len(values) > limit:
        raise LocalReviewError(f"{name} has too many points", 413)
    out: list[float] = []
    for item in values:
        try:
            number = float(item)
        except (TypeError, ValueError):
            continue
        if number == number and number not in (float("inf"), float("-inf")):
            out.append(number)
    return out


def _check_item(raw) -> dict:
    if not isinstance(raw, dict):
        raise LocalReviewError("each check must be an object")
    name = _trim(str(raw.get("name") or "check"), 80)
    title = _trim(str(raw.get("title") or name), 120)
    status = str(raw.get("status") or "unavailable").strip().lower()
    if status not in _CHECK_STATUSES:
        raise LocalReviewError("invalid check status")
    reason = _trim(str(raw.get("reason") or ""), _MAX_STRING)
    numbers = raw.get("numbers")
    clean_numbers: dict = {}
    if numbers is not None:
        if not isinstance(numbers, dict):
            raise LocalReviewError("check numbers must be an object")
        for key, value in list(numbers.items())[:24]:
            key_s = _trim(str(key), 40)
            if isinstance(value, (int, float)) and float(value) == float(value):
                clean_numbers[key_s] = float(value)
            elif value is None:
                clean_numbers[key_s] = None
            else:
                clean_numbers[key_s] = _trim(str(value), 120)
    item = {"name": name, "title": title, "status": status, "reason": reason, "numbers": clean_numbers}
    if raw.get("known"):
        item["known"] = True
    return item


def validate_local_check(body: bytes | str | dict, *, tic: int) -> dict:
    """Parse and sanitize one upload. Raises LocalReviewError on failure."""
    if isinstance(body, (bytes, bytearray)):
        if len(body) > _MAX_BODY:
            raise LocalReviewError("request body is too large", 413)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LocalReviewError("body must be JSON") from exc
    elif isinstance(body, dict):
        payload = body
    else:
        raise LocalReviewError("body must be JSON")
    if not isinstance(payload, dict):
        raise LocalReviewError("body must be a JSON object")
    body_tic = payload.get("tic")
    try:
        body_tic_i = int(body_tic)
    except (TypeError, ValueError) as exc:
        raise LocalReviewError("tic is required") from exc
    if body_tic_i != int(tic):
        raise LocalReviewError("tic in body must match the URL")

    check_key = _trim(str(payload.get("check_key") or payload.get("idempotency_key") or ""), 64)
    if not check_key or not _KEY_RE.match(check_key):
        raise LocalReviewError("check_key must be 1–64 characters (letters, digits, . _ -)")

    sector_raw = payload.get("sector")
    sector = None
    if sector_raw is not None and sector_raw != "":
        try:
            sector = int(sector_raw)
        except (TypeError, ValueError) as exc:
            raise LocalReviewError("sector must be an integer") from exc

    checks_raw = payload.get("checks")
    if not isinstance(checks_raw, list) or not checks_raw:
        raise LocalReviewError("checks must be a non-empty list")
    if len(checks_raw) > _MAX_CHECKS:
        raise LocalReviewError("too many checks", 413)
    checks = [_check_item(item) for item in checks_raw]

    verdict = _trim(str(payload.get("verdict") or "Needs review"), 120)
    note = _trim(str(payload.get("note") or ""), 500)
    plot = payload.get("plot") if isinstance(payload.get("plot"), dict) else {}
    fold_phase = _finite_list(plot.get("phase") or [], limit=_MAX_PLOT_POINTS, name="plot.phase")
    fold_flux = _finite_list(plot.get("flux") or [], limit=_MAX_PLOT_POINTS, name="plot.flux")
    if fold_phase and fold_flux and len(fold_phase) != len(fold_flux):
        fold_flux = fold_flux[: len(fold_phase)]
        fold_phase = fold_phase[: len(fold_flux)]

    client_version = _trim(str(payload.get("app_version") or ""), 40)
    return {
        "tic": int(tic),
        "sector": sector,
        "check_key": check_key,
        "verdict": verdict,
        "note": note,
        "checks": checks,
        "plot": {"phase": fold_phase, "flux": fold_flux} if fold_phase else None,
        "disclaimer": DISCLAIMER,
        "app_version": client_version,
    }