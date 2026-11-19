"""Small sky and pixel images for the worker dashboard.

The worker does not keep target-pixel files. The pipeline aperture is read from
the light curve when that file is still on disk. A missing mask can be filled
from a 5×5 TESScut, cached under the worker cache. The sky panel is a DSS2
JPEG from the hips2fits service, also cached. Neither path is allowed to grow
without a cap, and a timeout or an unreachable host becomes a short note.
"""
from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .present import pixels_payload

HIPS_URL = "https://alasky.cds.unistra.fr/hips-image-services/hips2fits"
# hips2fits is the color plate. When that host is down, SkyView still serves DSS2.
SKYVIEW_URL = "https://skyview.gsfc.nasa.gov/current/cgi/runquery.pl"
TESSCUT_URL = "https://mast.stsci.edu/tesscut/api/v0.1/astrocut"
_IMAGE_CAP = 80
_IMAGE_BYTES = 20_000_000
# One 5×5 sector cutout is a few hundred KB. Every sector at once is many MB, so that is rejected.
_BODY_CAP = 2_000_000
_FAIL_S = 90
_TIMEOUT_S = 12


def _folder(cache: Path) -> Path:
    path = Path(cache) / "images"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _prune(folder: Path) -> None:
    files = [path for path in folder.glob("*") if path.is_file()]
    files.sort(key=lambda path: path.stat().st_mtime)
    total = 0
    sized = []
    for path in files:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        sized.append((path, size))
        total += size
    while sized and (len(sized) > _IMAGE_CAP or total > _IMAGE_BYTES):
        path, size = sized.pop(0)
        total -= size
        try:
            path.unlink()
        except OSError:
            pass


def _finite(value, lo: float, hi: float):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if number < lo or number > hi:
        return None
    return number


def _read_capped(response, limit: int) -> bytes:
    chunks = []
    total = 0
    while total <= limit:
        part = response.read(min(65536, limit - total + 1))
        if not part:
            break
        total += len(part)
        if total > limit:
            raise ValueError("response too large")
        chunks.append(part)
    return b"".join(chunks)


def default_get(url: str, *, timeout: float = _TIMEOUT_S, params: dict | None = None) -> tuple[int, bytes]:
    """GET one small body. Raises on timeout, DNS failure, or an oversized reply."""
    if params:
        url = url + ("&" if "?" in url else "?") + urlencode(params)
    request = Request(url, headers={"User-Agent": "mega-rhexas-dashboard"})
    with urlopen(request, timeout=timeout) as response:
        status = int(getattr(response, "status", 200) or 200)
        return status, _read_capped(response, _BODY_CAP)


def _sky_sources(ra: float, dec: float) -> list[tuple[str, dict, float]]:
    """Color DSS2 first, then the DSS2 red plate. Both are a 6 arcmin field."""
    return [
        (
            HIPS_URL,
            {
                "hips": "CDS/P/DSS2/color",
                "width": 300,
                "height": 300,
                "fov": 6 / 60,
                "projection": "TAN",
                "coordsys": "icrs",
                "ra": f"{ra:.6f}",
                "dec": f"{dec:.6f}",
                "format": "jpg",
            },
            8,
        ),
        (
            SKYVIEW_URL,
            {
                "Survey": "DSS2 Red",
                "position": f"{ra:.6f},{dec:.6f}",
                "Size": 0.1,
                "Pixels": 300,
                "Return": "JPG",
            },
            _TIMEOUT_S,
        ),
    ]


def cutout_jpeg(cache: Path, tic: int, ra: float, dec: float, *, getter=None) -> bytes | None:
    """DSS2 JPEG, 6 arcmin on a side. None when every source times out or is down."""
    ra_v = _finite(ra, -360, 360)
    dec_v = _finite(dec, -90, 90)
    if ra_v is None or dec_v is None:
        return None
    if ra_v < 0:
        ra_v = ra_v % 360
    folder = _folder(cache)
    path = folder / f"dss-{int(tic)}.jpg"
    try:
        if path.is_file() and path.stat().st_size > 32 and path.read_bytes()[:2] == b"\xff\xd8":
            return path.read_bytes()
    except OSError:
        pass
    getter = getter or default_get
    body = None
    for url, params, timeout in _sky_sources(ra_v, dec_v):
        try:
            status, payload = getter(url, timeout=timeout, params=params)
        except (OSError, URLError, TimeoutError, ValueError):
            continue
        if int(status) == 200 and payload and payload[:2] == b"\xff\xd8":
            body = payload
            break
    if not body:
        return None
    try:
        path.write_bytes(body)
        _prune(folder)
    except OSError:
        return body
    return body


def _lc_path(cache: Path, tic: int) -> Path | None:
    root = Path(cache) / "data"
    if not root.is_dir():
        return None
    needle = f"{int(tic):016d}"
    try:
        matches = [path for path in root.glob(f"s*/*{needle}*lc.fits") if path.is_file()]
    except OSError:
        return None
    return matches[0] if matches else None


def _mask_array(data) -> list | None:
    import numpy as np
    arr = np.asarray(data)
    if arr.ndim != 2 or arr.size == 0 or arr.shape[0] > 15 or arr.shape[1] > 15:
        return None
    mask = [[1 if int(v) & 2 else 0 for v in row] for row in arr.tolist()]
    if not any(bit for row in mask for bit in row):
        return None
    return mask


def _picture(data) -> list | None:
    import numpy as np
    arr = np.asarray(data, dtype=float)
    if arr.ndim == 3:
        arr = np.nanmedian(arr[:8], axis=0)
    if arr.ndim != 2 or arr.shape[0] > 15 or arr.shape[1] > 15 or arr.size == 0:
        return None
    if not np.isfinite(arr).any():
        return None
    picture = []
    for row in arr:
        picture.append([None if not np.isfinite(value) else round(float(value), 3) for value in row])
    return picture


def _center_mask(height: int, width: int) -> list:
    mask = [[0 for _ in range(width)] for _ in range(height)]
    mask[height // 2][width // 2] = 1
    return mask


def _from_fits(blob: bytes) -> tuple[list | None, list | None]:
    import numpy as np
    from astropy.io import fits
    image = None
    mask = None
    with fits.open(io.BytesIO(blob)) as hdul:
        for hdu in hdul:
            name = str(getattr(hdu, "name", "") or "").upper()
            data = getattr(hdu, "data", None)
            if data is None:
                continue
            if name == "APERTURE":
                mask = _mask_array(data) or mask
                continue
            columns = getattr(getattr(hdu, "columns", None), "names", None) or []
            if "FLUX" in columns:
                flux = hdu.data["FLUX"]
                arr = np.asarray(flux, dtype=float)
                if arr.ndim == 1 and len(arr) and hasattr(arr[0], "__len__"):
                    arr = np.stack([np.asarray(frame, dtype=float) for frame in list(arr)[:8]])
                image = image or _picture(arr)
                continue
            arr = np.asarray(data)
            if getattr(arr, "dtype", None) is not None and arr.dtype.kind in "fiu" and arr.ndim >= 2:
                image = image or _picture(arr)
    return image, mask


def _parse_cutout(raw: bytes) -> tuple[list | None, list | None]:
    blob = raw
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            name = next((item for item in archive.namelist() if item.lower().endswith((".fits", ".fit"))), None)
            if name is None:
                return None, None
            blob = archive.read(name)
    return _from_fits(blob)


def _coords(snap: dict, tic: int) -> tuple[float | None, float | None, int | None]:
    star = snap.get("current_star") if isinstance(snap.get("current_star"), dict) else {}
    views = snap.get("views") if isinstance(snap.get("views"), dict) else {}
    view = views.get(str(int(tic))) if isinstance(views.get(str(int(tic))), dict) else None
    source = star if int(star.get("tic") or 0) == int(tic) else (view or {})
    if not source and isinstance(star, dict):
        source = {}
    ra = source.get("ra") if isinstance(source, dict) else None
    dec = source.get("dec") if isinstance(source, dict) else None
    sector = source.get("sector") if isinstance(source, dict) else None
    batch = snap.get("batch") if isinstance(snap.get("batch"), dict) else {}
    if sector is None:
        sector = batch.get("sector")
    try:
        sector_i = int(sector) if sector else None
    except (TypeError, ValueError):
        sector_i = None
    ra_v = _finite(ra, -360, 360)
    if ra_v is not None:
        ra_v = ra_v % 360
    return ra_v, _finite(dec, -90, 90), sector_i


def _offline_note() -> str:
    return "No aperture in the saved light curve, and the small TESS cutout could not be downloaded (offline or timed out)."


def resolve_pixels(cache: Path, snap: dict, tic: int, *, getter=None) -> dict:
    """Aperture for one star: the saved record, the light curve on disk, or a 5×5 cutout."""
    try:
        tic_i = int(tic)
    except (TypeError, ValueError):
        tic_i = 0
    try:
        from .live import load_star
        stored = load_star(cache, tic_i)
    except Exception:
        stored = None
    if isinstance(stored, dict) and stored.get("aperture"):
        return {
            "kind": "aperture",
            "aperture": stored["aperture"],
            "note": "Pipeline aperture saved with this star.",
        }
    if isinstance(stored, dict) and stored.get("available") and stored.get("ra") is not None:
        snap = {**snap, "current_star": stored}
    payload = pixels_payload(snap, tic)
    if payload.get("aperture"):
        return payload
    from rhexas.search import read_aperture
    path = _lc_path(cache, tic_i)
    if path is not None:
        mask = read_aperture(path)
        if mask:
            return {
                "kind": "aperture",
                "aperture": mask,
                "note": "Pipeline aperture from this star's light curve.",
            }
    folder = _folder(cache)
    cached = folder / f"pix-{tic_i}.json"
    try:
        if cached.is_file():
            saved = json.loads(cached.read_text(encoding="utf-8"))
            if isinstance(saved, dict) and (saved.get("aperture") or saved.get("image")):
                return saved
    except (OSError, json.JSONDecodeError):
        pass
    fail = folder / f"pix-{tic_i}.fail"
    try:
        if fail.is_file() and time.time() - fail.stat().st_mtime < _FAIL_S:
            return {"kind": "aperture", "aperture": None, "offline": True, "note": _offline_note()}
    except OSError:
        pass
    ra, dec, sector = _coords(snap, tic_i)
    if ra is None or dec is None or not sector:
        return {
            "kind": "aperture",
            "aperture": None,
            "note": "No aperture stored for this star yet.",
        }
    getter = getter or default_get
    params = {
        "ra": f"{ra:.6f}",
        "dec": f"{dec:.6f}",
        "x": 5,
        "y": 5,
        "units": "px",
        "sector": int(sector),
    }
    try:
        status, body = getter(TESSCUT_URL, timeout=_TIMEOUT_S, params=params)
        if int(status) != 200 or not body or body[:1] in (b"{", b"<"):
            raise ValueError("empty cutout")
        image, mask = _parse_cutout(body)
    except (OSError, URLError, TimeoutError, ValueError, zipfile.BadZipFile):
        try:
            fail.write_text("offline\n", encoding="utf-8")
            _prune(folder)
        except OSError:
            pass
        return {"kind": "aperture", "aperture": None, "offline": True, "note": _offline_note()}
    if image is None and mask is None:
        return {"kind": "aperture", "aperture": None, "note": "The TESS cutout did not include pixels for this star."}
    if mask is None and image:
        mask = _center_mask(len(image), len(image[0]))
        note = "Small TESS cutout, 5×5 pixels. The center is marked because the pipeline aperture was not in the file."
    elif image:
        note = "Small TESS cutout, 5×5 pixels, with the pipeline aperture outlined."
    else:
        note = "Pipeline aperture from a small TESS cutout."
    result = {
        "kind": "cutout" if image else "aperture",
        "aperture": mask,
        "image": image,
        "note": note,
    }
    try:
        cached.write_text(json.dumps(result), encoding="utf-8")
        fail.unlink(missing_ok=True)
        _prune(folder)
    except OSError:
        pass
    return result
