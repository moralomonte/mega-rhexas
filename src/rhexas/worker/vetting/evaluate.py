"""One candidate: light curve already on disk, then the public catalogs.

Nothing here runs on the scan thread. ``evaluate`` is what the background
worker calls, and tests call it directly with a fake fetcher.
"""
from __future__ import annotations

import time
from pathlib import Path

from rhexas.tics import tic_from_filename

from .catalogs import catalog_links, crossmatch
from .checks import DISCLAIMER, centroid_check, fold_plot, light_checks, summarize

# Catalog checks that should be tried again after a network failure.
_RETRY_NAMES = {"simbad", "vsx", "toi", "ctoi", "gaia"}
_RETRY_BASE_S = 15 * 60
_RETRY_CAP_S = 6 * 60 * 60
_RETRY_LIMIT = 5


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def find_lightcurve(cache: Path, tic: int, sector: int) -> Path | None:
    folder = Path(cache) / "data" / f"s{int(sector):04d}"
    if not folder.is_dir():
        return None
    matches = []
    try:
        paths = list(folder.glob("*lc.fits"))
    except OSError:
        return None
    for path in paths:
        if tic_from_filename(path.name) == int(tic):
            matches.append(path)
    if not matches:
        return None
    matches.sort()
    return matches[0]


def read_fits(path: Path) -> dict | None:
    """TIME, PDCSAP flux, quality, and any stellar numbers the header happens to carry."""
    try:
        from astropy.io import fits
        with fits.open(path, memmap=False) as hdul:
            chosen = None
            for hdu in hdul:
                names = getattr(getattr(hdu, "columns", None), "names", None) or []
                lookup = {str(name).upper(): name for name in names}
                if "TIME" in lookup and ("PDCSAP_FLUX" in lookup or "SAP_FLUX" in lookup):
                    chosen = (hdu, lookup)
                    break
            if chosen is None:
                return None
            hdu, lookup = chosen
            data = hdu.data
            flux_name = lookup.get("PDCSAP_FLUX") or lookup["SAP_FLUX"]
            quality = None
            if "QUALITY" in lookup:
                quality = [int(v) if v == v else 0 for v in data[lookup["QUALITY"]].tolist()]
            headers = []
            if hdul[0].header is not None:
                headers.append(hdul[0].header)
            if hdu.header is not None:
                headers.append(hdu.header)
            meta = _header_meta(headers)
            col = row = None
            if "MOM_CENTR1" in lookup and "MOM_CENTR2" in lookup:
                col = data[lookup["MOM_CENTR1"]].tolist()
                row = data[lookup["MOM_CENTR2"]].tolist()
            return {
                "time": data[lookup["TIME"]].tolist(),
                "flux": data[flux_name].tolist(),
                "quality": quality,
                "centroid_col": col,
                "centroid_row": row,
                **meta,
            }
    except Exception:
        return None


def _header_value(headers, *keys):
    for header in headers:
        for key in keys:
            if key in header:
                try:
                    number = float(header[key])
                except (TypeError, ValueError):
                    continue
                if number == number:
                    return number
    return None


def _header_meta(headers) -> dict:
    return {
        "ra": _header_value(headers, "RA_OBJ", "RA"),
        "dec": _header_value(headers, "DEC_OBJ", "DEC"),
        "radius": _header_value(headers, "RADIUS", "RAD", "ST_RAD"),
        "mass": _header_value(headers, "MASS", "ST_MASS"),
    }


def _star_record(cache: Path, tic: int, sector: int) -> dict | None:
    try:
        from rhexas.worker.live import DB_NAME, _connect
        from rhexas.worker.records import fetch_star
        conn = _connect(Path(cache) / DB_NAME, write=False)
    except Exception:
        return None
    try:
        return fetch_star(conn, int(tic), int(sector))
    except Exception:
        return None
    finally:
        conn.close()


def _photometry(cache: Path, tic: int, sector: int, record: dict | None) -> dict:
    """Prefer the FITS file. Fall back to the curve stored with the star."""
    photo = {"time": None, "flux": None, "quality": None, "ra": None, "dec": None,
             "radius": None, "mass": None, "centroid_sigma": None, "centroid_shift": None}
    fits_path = find_lightcurve(cache, tic, sector)
    if fits_path is not None:
        loaded = read_fits(fits_path)
        if loaded:
            photo.update(loaded)
    if record:
        for key in ("ra", "dec"):
            if photo.get(key) is None:
                photo[key] = record.get(key)
        if photo.get("centroid_sigma") is None:
            photo["centroid_sigma"] = record.get("centroid_sigma")
        if photo.get("centroid_shift") is None:
            photo["centroid_shift"] = record.get("centroid_shift_arcsec")
        if not photo.get("time") and record.get("time") and record.get("flux"):
            photo["time"] = record.get("time")
            photo["flux"] = record.get("flux")
        if photo.get("centroid_sigma") is None and photo.get("centroid_col") and photo.get("time"):
            photo.update(_centroid_from_columns(photo, record))
    elif photo.get("centroid_col") is not None:
        photo.update(_centroid_from_columns(photo, {}))
    return photo


def _centroid_from_columns(photo: dict, record: dict) -> dict:
    try:
        from rhexas.worker.records import centroid_vs_phase
        packed = centroid_vs_phase(
            photo.get("time"), photo.get("centroid_col"), photo.get("centroid_row"),
            record.get("period"), record.get("t0"), record.get("duration"),
        )
    except Exception:
        return {}
    if not packed:
        return {}
    return {"centroid_sigma": packed.get("sigma"), "centroid_shift": packed.get("shift_arcsec")}


def _signal(row: dict, record: dict | None) -> dict:
    record = record or {}
    def pick(*values):
        for value in values:
            number = _finite(value)
            if number is not None:
                return number
        return None
    return {
        "period": pick(row.get("period"), record.get("period")),
        "t0": pick(row.get("t0"), record.get("t0")),
        "duration": pick(row.get("duration"), record.get("duration")),
        "depth": pick(row.get("depth"), record.get("depth")),
        "snr": pick(row.get("snr"), record.get("snr")),
        "sde": pick(row.get("sde"), record.get("sde")),
        "unit": row.get("unit") if row.get("unit") is not None else record.get("unit"),
        "ra": pick(row.get("ra"), record.get("ra")),
        "dec": pick(row.get("dec"), record.get("dec")),
    }


def retry_delay(attempts: int) -> float | None:
    """Seconds until another catalog try. None once the cap is reached."""
    if attempts >= _RETRY_LIMIT:
        return None
    return min(_RETRY_CAP_S, _RETRY_BASE_S * (2 ** max(attempts - 1, 0)))


def needs_catalog_retry(checks: list[dict]) -> bool:
    return any(item.get("name") in _RETRY_NAMES and item.get("status") == "unavailable" for item in checks)


def evaluate(cache, row: dict, settings, fetcher=None, *, now: float | None = None) -> dict:
    """Vet one candidate. Network errors are recorded; they do not raise."""
    cache = Path(cache)
    tic = int(row["tic"])
    sector = int(row["sector"])
    record = _star_record(cache, tic, sector)
    signal = _signal(row, record)
    if record and signal["period"] is not None:
        record = dict(record)
        record["period"] = signal["period"]
        record["t0"] = signal["t0"]
        record["duration"] = signal["duration"]
    photo = _photometry(cache, tic, sector, record)
    for key in ("ra", "dec"):
        if signal[key] is None and photo.get(key) is not None:
            signal[key] = photo.get(key)
    radius = photo.get("radius")
    mass = photo.get("mass")
    # The centroid stored at search time wins. Recompute only when that column is empty.
    centroid_sigma = photo.get("centroid_sigma")
    centroid_shift = photo.get("centroid_shift")
    if centroid_sigma is None and photo.get("centroid_col") is not None:
        fresh = _centroid_from_columns(photo, signal)
        centroid_sigma = fresh.get("centroid_sigma", centroid_sigma)
        centroid_shift = fresh.get("centroid_shift", centroid_shift)

    checks: list[dict] = []
    try:
        catalog_checks, meta = crossmatch(
            cache, tic, signal["ra"], signal["dec"], signal["depth"],
            radius_arcsec=float(settings.radius_arcsec),
            timeout_s=float(settings.timeout_s),
            fetcher=fetcher,
            cache_max_mb=float(settings.cache_max_mb),
            now=now,
        )
    except Exception as exc:
        catalog_checks = [{
            "name": "simbad", "title": "SIMBAD", "status": "unavailable",
            "reason": f"Catalog checks did not finish ({str(exc)[:160]}).",
            "numbers": {},
        }]
        meta = {}
    if meta.get("ra") is not None:
        signal["ra"] = meta["ra"]
    if meta.get("dec") is not None:
        signal["dec"] = meta["dec"]
    if radius is None:
        radius = meta.get("radius")
    if mass is None:
        mass = meta.get("mass")
    checks.extend(catalog_checks)
    if photo.get("time") is not None and photo.get("flux") is not None and signal["period"] and signal["t0"] and signal["duration"]:
        checks.extend(light_checks(
            photo["time"], photo["flux"],
            period=signal["period"], t0=signal["t0"], duration=signal["duration"],
            quality=photo.get("quality"), radius=radius, mass=mass,
            centroid_sigma=centroid_sigma, centroid_shift=centroid_shift,
        ))
    else:
        checks.append({
            "name": "light_curve",
            "title": "Light curve",
            "status": "unavailable",
            "reason": "No sector light curve and BLS ephemeris are on disk for this candidate yet.",
            "numbers": {},
        })
        checks.append(centroid_check(centroid_sigma, centroid_shift))
    verdict = summarize(checks)
    plot = None
    if photo.get("time") is not None and signal["period"] and signal["t0"]:
        plot = fold_plot(photo["time"], photo["flux"], signal["period"], signal["t0"], signal["duration"] or 0.1)
    links = catalog_links(tic, signal["ra"], signal["dec"], meta.get("simbad_id"), meta.get("vsx_name"))
    attempts = int(row.get("attempts") or 0)
    retry_at = None
    if needs_catalog_retry(checks):
        attempts += 1
        delay = retry_delay(attempts)
        if delay is not None:
            retry_at = (time.time() if now is None else float(now)) + delay
    else:
        attempts = 0
    return {
        "verdict": verdict,
        "result": {
            "checks": checks,
            "plot": plot,
            "links": links,
            "disclaimer": DISCLAIMER,
            "radius": radius,
            "mass": mass,
        },
        "fields": signal,
        "retry_at": retry_at,
        "attempts": attempts,
    }
