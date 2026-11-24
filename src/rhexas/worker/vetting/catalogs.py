"""Public catalog cross-matches. No API key.

SIMBAD TAP, VizieR (VSX and Gaia DR3), and the ExoFOP TOI and CTOI lists.
A failed request becomes an ``unavailable`` check. Callers pass a fetcher so
tests never open a socket.
"""
from __future__ import annotations

import csv
import io
import json
import math
import time
import urllib.parse
import urllib.request
from pathlib import Path

# Strasbourg CDS is returning 404. Harvard answers, and IDIA is the other live mirror.
SIMBAD_PRIMARY = "https://simbad.cfa.harvard.edu/simbad/sim-tap/sync"
SIMBAD_MIRROR = "https://simbad.idia.ac.za/simbad/sim-tap/sync"
VIZIER_TAP = "https://tapvizier.cds.unistra.fr/TAPVizieR/tap/sync"
TOI_URL = "https://exofop.ipac.caltech.edu/tess/download_toi.php?sort=toi&output=csv"
CTOI_URL = "https://exofop.ipac.caltech.edu/tess/download_ctoi.php?sort=ctoi&output=csv"
MAST_TIC_URL = "https://mast.stsci.edu/portal/Mashup/Mashup.asmx/invoke"

LIST_MAX_AGE_S = 24 * 60 * 60
TOI_LIST_TIMEOUT_S = 120.0
TOI_LIST_RETRIES = 3
NEIGHBOUR_ARCSEC = 42.0
PIXEL_ARCSEC = 21.0
RUWE_WARN = 1.4
TARGET_ARCSEC = 3.0

# SIMBAD classes that mean the object is already an eclipsing binary, a variable, or a classified companion.
_KNOWN_PREFIXES = (
    "EB", "V*", "Puls", "Pl", "pPl", "Ir*", "Er*", "Ro*", "RR", "Ce*", "Mi*",
    "CV", "XB", "DN", "RS*", "BY*", "UV", "Or*", "TT*", "YSO", "a2*", "dS*",
    "SN", "WR*", "Be*", "Ae*",
)
_BINARY_PREFIXES = ("SB",)


def urlopen_fetch(url: str, timeout: float) -> tuple[int, str]:
    """GET a public URL. Raises on a transport failure. No API key is sent.

    ``mast-tic://<tic>`` is a POST to the MAST catalog service. Tests pass a
    fetcher, so this branch only runs against the live service.
    """
    if str(url).startswith("mast-tic://"):
        return _mast_tic_fetch(str(url).split("://", 1)[1], timeout)
    request = urllib.request.Request(url, headers={
        "User-Agent": "MegaRhexasWorker/vetting (local advisory; no api key)",
        "Accept": "application/json, text/csv, text/plain, */*",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = int(getattr(response, "status", 200))
        body = response.read(30_000_000)
    return status, body.decode("utf-8", "replace")


def _call(fetcher, url: str, timeout: float) -> tuple[int, str, str]:
    try:
        status, text = fetcher(url, timeout)
    except Exception as exc:
        return 0, "", _short(exc)
    try:
        status_i = int(status)
    except (TypeError, ValueError):
        status_i = 0
    body = text if isinstance(text, str) else str(text)
    if status_i < 200 or status_i >= 300:
        return status_i, "", f"HTTP {status_i}" if status_i else _short(body)
    if not body.strip():
        return status_i, "", "empty response"
    return status_i, body, ""


def _short(value) -> str:
    text = " ".join(str(value or "").split())
    return text[:180] or "no response"


def tap_url(base: str, query: str) -> str:
    return base + "?" + urllib.parse.urlencode({
        "REQUEST": "doQuery",
        "LANG": "ADQL",
        "FORMAT": "csv",
        "QUERY": query,
    })


def _num(value: float) -> str:
    return f"{float(value):.7f}"


def parse_table(text: str) -> list[dict]:
    """TAP JSON (metadata + data) or CSV. Comment lines are skipped."""
    raw = (text or "").lstrip("\ufeff").strip()
    if not raw:
        return []
    if raw[0] in "{[":
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            names = []
            for column in payload.get("metadata") or []:
                if isinstance(column, dict):
                    names.append(str(column.get("name") or column.get("NAME") or ""))
                else:
                    names.append(str(column))
            rows = []
            for record in payload["data"]:
                if isinstance(record, dict):
                    rows.append({str(key): "" if value is None else str(value).strip() for key, value in record.items()})
                    continue
                if not isinstance(record, (list, tuple)):
                    continue
                item = {}
                for index, value in enumerate(record):
                    key = names[index] if index < len(names) and names[index] else f"c{index}"
                    item[key] = "" if value is None else str(value).strip()
                rows.append(item)
            return rows
        return []
    kept = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("<"):
            continue
        kept.append(stripped)
    if not kept:
        return []
    reader = csv.DictReader(io.StringIO("\n".join(kept)))
    rows = []
    for row in reader:
        if not row:
            continue
        item = {}
        for key, value in row.items():
            if key is None:
                continue
            item[str(key).strip()] = "" if value is None else str(value).strip()
        if any(item.values()):
            rows.append(item)
    return rows


def _column(row: dict, *names: str) -> str:
    folded = {str(key).strip().casefold(): value for key, value in row.items()}
    for name in names:
        if name.casefold() in folded:
            return folded[name.casefold()]
    return ""


def _float(value):
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _tic_id(value) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("TIC", "").replace("tic", "").strip()
    try:
        return int(float(text))
    except ValueError:
        return None


def index_toi(text: str) -> dict[int, list[dict]]:
    found: dict[int, list[dict]] = {}
    for row in parse_table(text):
        tic = _tic_id(_column(row, "TIC ID", "TIC", "tic_id", "tic"))
        if tic is None:
            continue
        found.setdefault(tic, []).append({
            "toi": _column(row, "TOI", "toi"),
            "disposition": _column(row, "TFOPWG Disposition", "Disposition"),
            "period": _column(row, "Period (days)", "Period"),
        })
    return found


def index_ctoi(text: str) -> dict[int, list[dict]]:
    found: dict[int, list[dict]] = {}
    for row in parse_table(text):
        tic = _tic_id(_column(row, "TIC ID", "TIC", "tic_id", "tic"))
        if tic is None:
            continue
        found.setdefault(tic, []).append({
            "ctoi": _column(row, "CTOI", "CTOI ID", "ctoi"),
            "period": _column(row, "Period (days)", "Period"),
        })
    return found


def sep_arcsec(ra1, dec1, ra2, dec2) -> float | None:
    a, b, c, d = _float(ra1), _float(dec1), _float(ra2), _float(dec2)
    if None in (a, b, c, d):
        return None
    scale = math.cos(math.radians((b + d) / 2.0))
    return math.hypot((c - a) * scale, d - b) * 3600.0


def _known_otype(otype: str) -> bool:
    folded = (otype or "").strip().casefold()
    if not folded or folded in {"*", "pm*", "rg*", "sg*", "hs*", "ab*", "wd*", "**"}:
        return False
    return any(folded.startswith(prefix.casefold()) for prefix in _KNOWN_PREFIXES)


def _binary_otype(otype: str) -> bool:
    folded = (otype or "").strip().casefold()
    if folded in {"**", "sb*", "sb"}:
        return True
    return any(folded.startswith(prefix.casefold()) for prefix in _BINARY_PREFIXES)


def _simbad_id_query(tic: int) -> str:
    ident = f"TIC {int(tic)}"
    return (
        "SELECT TOP 5 b.main_id, b.otype, b.oid FROM basic AS b "
        "JOIN ident AS i ON i.oidref = b.oid "
        f"WHERE i.id = '{ident}'"
    )


def _simbad_cone_query(ra: float, dec: float, radius_arcsec: float) -> str:
    radius = _num(radius_arcsec / 3600.0)
    return (
        "SELECT TOP 8 main_id, otype, oid, "
        f"DISTANCE(POINT('ICRS', ra, dec), POINT('ICRS', {_num(ra)}, {_num(dec)})) * 3600 AS dist "
        "FROM basic WHERE "
        f"CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {_num(ra)}, {_num(dec)}, {radius})) = 1"
    )


def _vsx_query(ra: float, dec: float, radius_arcsec: float) -> str:
    radius = _num(radius_arcsec / 3600.0)
    return (
        "SELECT TOP 20 Name, Type, Period, RAJ2000, DEJ2000 "
        'FROM "B/vsx/vsx" WHERE '
        f"CONTAINS(POINT('ICRS', RAJ2000, DEJ2000), CIRCLE('ICRS', {_num(ra)}, {_num(dec)}, {radius})) = 1"
    )


def _gaia_query(ra: float, dec: float) -> str:
    radius = _num(NEIGHBOUR_ARCSEC / 3600.0)
    return (
        "SELECT TOP 40 Source, RA_ICRS, DE_ICRS, RUWE, Gmag "
        'FROM "I/355/gaiadr3" WHERE '
        f"CONTAINS(POINT('ICRS', RA_ICRS, DE_ICRS), CIRCLE('ICRS', {_num(ra)}, {_num(dec)}, {radius})) = 1"
    )


def _tic_query(tic: int) -> str:
    return (
        "SELECT TOP 1 TIC, RAJ2000, DEJ2000, Tmag, rad, mass "
        'FROM "IV/39/tic82" WHERE '
        f"TIC = {int(tic)}"
    )


def _check(name, title, status, reason, numbers=None, **extra) -> dict:
    from .checks import check
    return check(name, title, status, reason, numbers, **extra)


def _load_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def list_path(cache: Path, name: str) -> Path:
    return Path(cache) / "vetting" / "lists" / f"{name}.csv"


def tic_cache_path(cache: Path, tic: int) -> Path:
    return Path(cache) / "vetting" / "catalogs" / f"tic-{int(tic)}.json"


def _list_fresh(path: Path, now: float) -> bool:
    try:
        return path.is_file() and (now - path.stat().st_mtime) < LIST_MAX_AGE_S
    except OSError:
        return False


def load_catalog_list(
    cache: Path, name: str, url: str, fetcher, timeout: float, now: float | None = None,
    *, retries: int = 1, pause_s: float = 0.0, sleeper=None,
) -> tuple[str, str]:
    """Return (csv text, error). A file newer than a day is reused.

    A failed fetch is tried again ``retries`` times. The last file on disk is
    still returned when every try fails, including after its TTL. With no file
    and no successful fetch, the error is returned and the check stays unavailable.
    """
    now = time.time() if now is None else float(now)
    path = list_path(cache, name)
    if _list_fresh(path, now):
        try:
            return path.read_text(encoding="utf-8"), ""
        except OSError as exc:
            return "", _short(exc)
    attempts = max(1, int(retries))
    last_error = ""
    for attempt in range(attempts):
        _status, text, error = _call(fetcher, url, timeout)
        if not error and parse_table(text):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            except OSError:
                pass
            return text, ""
        last_error = error or "the list had no rows"
        if attempt + 1 < attempts and pause_s > 0 and callable(sleeper):
            sleeper(pause_s)
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8"), ""
        except OSError:
            pass
    return "", last_error or "the list had no rows"


def bound_catalog_cache(cache: Path, max_bytes: int) -> None:
    """Drop the oldest per-TIC responses until the catalog cache fits."""
    root = Path(cache) / "vetting" / "catalogs"
    if max_bytes <= 0 or not root.is_dir():
        return
    files = [path for path in root.glob("tic-*.json") if path.is_file()]
    files.sort(key=lambda path: (path.stat().st_mtime, path.name))
    total = sum(path.stat().st_size for path in files)
    for path in files:
        if total <= max_bytes:
            break
        size = path.stat().st_size
        try:
            path.unlink()
        except OSError:
            continue
        total -= size


def catalog_cache_bytes(cache: Path) -> int:
    root = Path(cache) / "vetting"
    if not root.is_dir():
        return 0
    total = 0
    for path in root.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def _cached_section(blob: dict, name: str):
    section = blob.get(name)
    if isinstance(section, dict) and section.get("ok"):
        return section
    return None


def _store_section(blob: dict, name: str, text: str) -> None:
    blob[name] = {"ok": 1, "text": text}


def _is_table(text: str) -> bool:
    """A TAP JSON table or a CSV. HTML and error objects are not tables."""
    raw = (text or "").lstrip()
    if not raw or raw.startswith("<"):
        return False
    if raw[0] in "{[":
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return False
        return isinstance(payload, dict) and isinstance(payload.get("data"), list)
    head = raw.splitlines()[0]
    return "," in head or "\t" in head


def _fetch_tap(fetcher, timeout: float, primary: str, mirror: str | None = None) -> tuple[str, str]:
    _status, text, error = _call(fetcher, primary, timeout)
    if not error and _is_table(text):
        return text, ""
    if not error:
        error = "the service returned a page instead of a table"
    if mirror:
        _status_m, text_m, error_m = _call(fetcher, mirror, timeout)
        if not error_m and _is_table(text_m):
            return text_m, ""
        return "", error_m or error or "no response"
    return "", error or "no response"


def _simbad_rows(text: str, ra, dec) -> list[dict]:
    rows = []
    for row in parse_table(text):
        dist = _float(_column(row, "dist", "DIST", "d"))
        if dist is None and ra is not None and dec is not None:
            dist = sep_arcsec(ra, dec, _column(row, "ra", "RA_ICRS"), _column(row, "dec", "DE_ICRS"))
        rows.append({
            "main_id": _column(row, "main_id", "MAIN_ID"),
            "otype": _column(row, "otype", "OTYPE"),
            "dist": dist,
            "oid": _column(row, "oid", "OID"),
        })
    return rows


def simbad_check(rows: list[dict], *, error: str, radius: float) -> dict:
    if error and not rows:
        return _check("simbad", "SIMBAD", "unavailable", f"SIMBAD did not answer ({error}).", {"error": error})
    kept = []
    for row in rows:
        dist = row.get("dist")
        if dist is not None and dist > radius + 1.0:
            continue
        kept.append(row)
    known = [row for row in kept if _known_otype(row.get("otype") or "")]
    if known:
        row = known[0]
        where = "" if row.get("dist") is None else f", {row['dist']:.1f} arcsec away"
        return _check(
            "simbad", "SIMBAD", "fail",
            f"SIMBAD lists {row.get('main_id') or 'an object'} as {row.get('otype') or 'a known class'}{where}.",
            {"main_id": row.get("main_id"), "otype": row.get("otype"), "dist_arcsec": row.get("dist"), "matches": len(kept)},
            known=True,
        )
    binaries = [row for row in kept if _binary_otype(row.get("otype") or "")]
    if binaries:
        row = binaries[0]
        return _check(
            "simbad", "SIMBAD", "warn",
            f"SIMBAD lists {row.get('main_id') or 'a neighbour'} as {row.get('otype')}, a double or spectroscopic binary.",
            {"main_id": row.get("main_id"), "otype": row.get("otype"), "dist_arcsec": row.get("dist")},
        )
    if kept:
        row = kept[0]
        return _check(
            "simbad", "SIMBAD", "pass",
            f"SIMBAD lists {row.get('main_id') or 'a star'} as {row.get('otype') or 'a star'}, with no eclipsing or variable class inside {radius:.0f} arcsec.",
            {"main_id": row.get("main_id"), "otype": row.get("otype"), "dist_arcsec": row.get("dist"), "matches": len(kept)},
        )
    return _check(
        "simbad", "SIMBAD", "pass",
        f"No SIMBAD object inside {radius:.0f} arcsec, and the TIC identifier is not a known class.",
        {"matches": 0},
    )


def vsx_check(rows: list[dict], *, error: str, radius: float, ra, dec) -> dict:
    if error and not rows:
        return _check("vsx", "VSX", "unavailable", f"VSX did not answer ({error}).", {"error": error})
    kept = []
    for row in rows:
        dist = sep_arcsec(ra, dec, _column(row, "RAJ2000", "ra"), _column(row, "DEJ2000", "dec"))
        if dist is None:
            dist = _float(_column(row, "_r", "dist"))
        if dist is not None and dist > radius + 1.0:
            continue
        kept.append({
            "name": _column(row, "Name", "name"),
            "type": _column(row, "Type", "type"),
            "period": _column(row, "Period", "period"),
            "dist": dist,
        })
    if not kept:
        return _check("vsx", "VSX", "pass", f"No VSX entry inside {radius:.0f} arcsec.", {"matches": 0})
    row = kept[0]
    where = "" if row["dist"] is None else f", {row['dist']:.1f} arcsec away"
    kind = row["type"] or "variable"
    return _check(
        "vsx", "VSX", "fail",
        f"VSX lists {row['name'] or 'a variable'} ({kind}){where}.",
        {"name": row["name"], "type": row["type"], "period": row["period"], "dist_arcsec": row["dist"], "matches": len(kept)},
        known=True,
    )


def _list_check(name: str, title: str, rows: list[dict], *, error: str, label: str) -> dict:
    if error and not rows:
        return _check(name, title, "unavailable", f"The {label} list could not be read ({error}).", {"error": error})
    if not rows:
        return _check(name, title, "pass", f"This TIC is not on the {label} list.", {"matches": 0})
    ident = rows[0].get("toi") or rows[0].get("ctoi") or label
    extra = f" ({rows[0]['disposition']})" if rows[0].get("disposition") else ""
    more = f" {len(rows)} entries." if len(rows) > 1 else ""
    return _check(
        name, title, "fail",
        f"ExoFOP already has {label} {ident}{extra} for this TIC.{more}",
        {"id": ident, "matches": len(rows), "period": rows[0].get("period"), "disposition": rows[0].get("disposition")},
        known=True,
    )


def _diluted_depth(delta_g: float) -> float:
    ratio = 10 ** (-0.4 * delta_g)
    return ratio / (1.0 + ratio)


def gaia_check(rows: list[dict], *, error: str, ra, dec, depth) -> dict:
    if error and not rows:
        return _check("gaia", "Gaia neighbours and RUWE", "unavailable", f"Gaia DR3 did not answer ({error}).", {"error": error})
    parsed = []
    for row in rows:
        src_ra = _column(row, "RA_ICRS", "ra")
        src_dec = _column(row, "DE_ICRS", "dec")
        dist = sep_arcsec(ra, dec, src_ra, src_dec)
        if dist is None or dist > NEIGHBOUR_ARCSEC + 1.0:
            continue
        parsed.append({
            "source": _column(row, "Source", "source_id"),
            "dist": dist,
            "ruwe": _float(_column(row, "RUWE", "ruwe")),
            "gmag": _float(_column(row, "Gmag", "gmag", "phot_g_mean_mag")),
        })
    parsed.sort(key=lambda item: item["dist"])
    if not parsed:
        return _check(
            "gaia", "Gaia neighbours and RUWE", "pass",
            "No Gaia DR3 source inside 42 arcsec.",
            {"matches": 0, "ruwe": None},
        )
    target = parsed[0] if parsed[0]["dist"] <= TARGET_ARCSEC else None
    neighbours = [item for item in parsed if item is not target]
    ruwe = None if target is None else target["ruwe"]
    depth_f = _float(depth)
    worst = None
    for item in neighbours:
        if target is None or target["gmag"] is None or item["gmag"] is None or depth_f is None or depth_f <= 0:
            continue
        delta = item["gmag"] - target["gmag"]
        diluted = _diluted_depth(delta)
        if worst is None or diluted > worst["diluted"]:
            worst = {"dist": item["dist"], "delta_g": delta, "diluted": diluted, "gmag": item["gmag"]}
    numbers = {
        "ruwe": None if ruwe is None else round(ruwe, 3),
        "target_gmag": None if target is None or target["gmag"] is None else round(target["gmag"], 3),
        "neighbours": len(neighbours),
        "nearest_arcsec": None if not neighbours else round(neighbours[0]["dist"], 2),
        "blend_depth": None if worst is None else round(worst["diluted"], 6),
        "blend_delta_g": None if worst is None else round(worst["delta_g"], 2),
        "blend_arcsec": None if worst is None else round(worst["dist"], 2),
    }
    notes = []
    status = "pass"
    if target is None:
        notes.append("No Gaia source sits on the target, so RUWE was not measured.")
    elif ruwe is None:
        notes.append("The Gaia source on the target has no RUWE.")
    elif ruwe >= RUWE_WARN:
        notes.append(f"RUWE is {ruwe:.2f}, above {RUWE_WARN:.1f}, which can mean an unresolved companion.")
        status = "warn"
    else:
        notes.append(f"RUWE is {ruwe:.2f}.")
    if worst is not None and depth_f is not None and worst["diluted"] >= depth_f * 0.9:
        where = f"{worst['dist']:.1f} arcsec"
        sentence = (
            f"A neighbour {where} away (delta G {worst['delta_g']:.1f}) could produce "
            f"a depth of {worst['diluted'] * 1e6:.0f} ppm if it were fully eclipsed."
        )
        notes.append(sentence)
        if worst["dist"] <= PIXEL_ARCSEC:
            status = "fail"
        elif status != "fail":
            status = "warn"
    elif not neighbours:
        notes.append("No other Gaia source inside 42 arcsec.")
    elif status == "pass":
        notes.append("No neighbour inside 42 arcsec can produce this depth on its own.")
    return _check("gaia", "Gaia neighbours and RUWE", status, " ".join(notes), numbers)


def _mast_tic_fetch(tic_text: str, timeout: float) -> tuple[int, str]:
    """POST one TIC id to MAST's catalog service. Raises on a transport failure."""
    try:
        tic = int(str(tic_text).strip())
    except (TypeError, ValueError):
        return 400, ""
    payload = json.dumps({
        "service": "Mast.Catalogs.Filtered.Tic",
        "params": {
            "columns": "ID,ra,dec,Tmag,rad,mass",
            "filters": [{"paramName": "ID", "values": [tic]}],
        },
        "format": "json",
    })
    data = urllib.parse.urlencode({"request": payload}).encode("utf-8")
    request = urllib.request.Request(
        MAST_TIC_URL,
        data=data,
        headers={
            "User-Agent": "MegaRhexasWorker/vetting (local advisory; no api key)",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = int(getattr(response, "status", 200))
        body = response.read(2_000_000)
    return status, body.decode("utf-8", "replace")


def _parse_mast(text: str) -> dict:
    raw = (text or "").strip()
    if not raw:
        return {}
    if raw[0] == "{":
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            return {}
        row = rows[0]
        return {
            "ra": _float(row.get("ra") if row.get("ra") is not None else row.get("RA")),
            "dec": _float(row.get("dec") if row.get("dec") is not None else row.get("DEC")),
            "tmag": _float(row.get("Tmag") if row.get("Tmag") is not None else row.get("tmag")),
            "rad": _float(row.get("rad")),
            "mass": _float(row.get("mass")),
        }
    return _parse_tic(text)


def _parse_tic(text: str) -> dict:
    rows = parse_table(text)
    if not rows:
        return {}
    row = rows[0]
    return {
        "ra": _float(_column(row, "RAJ2000", "ra")),
        "dec": _float(_column(row, "DEJ2000", "dec")),
        "tmag": _float(_column(row, "Tmag", "tmag")),
        "rad": _float(_column(row, "rad", "Rstar")),
        "mass": _float(_column(row, "mass", "st_mass")),
    }


def crossmatch(cache, tic: int, ra, dec, depth, *, radius_arcsec: float, timeout_s: float,
               fetcher=None, cache_max_mb: float = 200.0, now: float | None = None) -> tuple[list[dict], dict]:
    """Run the catalog checks for one TIC. Cached sections are not fetched again."""
    fetcher = fetcher or urlopen_fetch
    now = time.time() if now is None else float(now)
    cache = Path(cache)
    blob = _load_json(tic_cache_path(cache, tic))
    stellar = dict(blob.get("stellar") or {}) if isinstance(blob.get("stellar"), dict) else {}

    list_timeout = max(float(timeout_s), TOI_LIST_TIMEOUT_S)
    toi_text, toi_error = load_catalog_list(
        cache, "toi", TOI_URL, fetcher, list_timeout, now, retries=TOI_LIST_RETRIES,
    )
    ctoi_text, ctoi_error = load_catalog_list(
        cache, "ctoi", CTOI_URL, fetcher, list_timeout, now, retries=TOI_LIST_RETRIES,
    )
    toi_rows = [] if toi_error and not toi_text else index_toi(toi_text).get(int(tic), [])
    ctoi_rows = [] if ctoi_error and not ctoi_text else index_ctoi(ctoi_text).get(int(tic), [])
    if toi_error and not toi_text:
        toi_rows = []
    if ctoi_error and not ctoi_text:
        ctoi_rows = []

    tic_section = _cached_section(blob, "tic")
    if tic_section is None:
        text, error = _fetch_tap(fetcher, timeout_s, tap_url(VIZIER_TAP, _tic_query(tic)))
        if error:
            blob["tic"] = {"ok": 0, "error": error}
        else:
            _store_section(blob, "tic", text)
            tic_section = blob["tic"]
    if tic_section and tic_section.get("ok"):
        stellar.update(_parse_tic(tic_section.get("text") or ""))
        blob["stellar"] = {key: stellar.get(key) for key in ("ra", "dec", "tmag", "rad", "mass")}

    if stellar.get("ra") is None or stellar.get("dec") is None:
        mast = _cached_section(blob, "mast_tic")
        if mast is None:
            _status, text, error = _call(fetcher, f"mast-tic://{int(tic)}", timeout_s)
            parsed = _parse_mast(text) if not error else {}
            if error or parsed.get("ra") is None or parsed.get("dec") is None:
                blob["mast_tic"] = {"ok": 0, "error": error or "no TIC row"}
            else:
                _store_section(blob, "mast_tic", text)
                mast = blob["mast_tic"]
        if mast and mast.get("ok"):
            for key, value in _parse_mast(mast.get("text") or "").items():
                if value is not None:
                    stellar[key] = value
            blob["stellar"] = {key: stellar.get(key) for key in ("ra", "dec", "tmag", "rad", "mass")}

    if ra is None:
        ra = stellar.get("ra")
    if dec is None:
        dec = stellar.get("dec")

    simbad_rows: list[dict] = []
    simbad_error = ""
    id_section = _cached_section(blob, "simbad_id")
    if id_section is None:
        text, error = _fetch_tap(
            fetcher, timeout_s,
            tap_url(SIMBAD_PRIMARY, _simbad_id_query(tic)),
            tap_url(SIMBAD_MIRROR, _simbad_id_query(tic)),
        )
        if error:
            blob["simbad_id"] = {"ok": 0, "error": error}
            simbad_error = error
        else:
            _store_section(blob, "simbad_id", text)
            id_section = blob["simbad_id"]
    if id_section and id_section.get("ok"):
        simbad_rows.extend(_simbad_rows(id_section.get("text") or "", ra, dec))
    elif isinstance(blob.get("simbad_id"), dict) and not blob["simbad_id"].get("ok"):
        simbad_error = str(blob["simbad_id"].get("error") or "no response")

    if ra is not None and dec is not None:
        cone = _cached_section(blob, "simbad_cone")
        if cone is None:
            query = _simbad_cone_query(float(ra), float(dec), radius_arcsec)
            text, error = _fetch_tap(
                fetcher, timeout_s,
                tap_url(SIMBAD_PRIMARY, query),
                tap_url(SIMBAD_MIRROR, query),
            )
            if error:
                blob["simbad_cone"] = {"ok": 0, "error": error}
                simbad_error = simbad_error or error
            else:
                _store_section(blob, "simbad_cone", text)
                cone = blob["simbad_cone"]
        if cone and cone.get("ok"):
            simbad_rows.extend(_simbad_rows(cone.get("text") or "", ra, dec))
            simbad_error = ""
    elif not simbad_rows:
        simbad_error = simbad_error or "no sky position for a cone search"

    vsx_rows: list[dict] = []
    vsx_error = ""
    if ra is None or dec is None:
        vsx_error = "no sky position"
    else:
        section = _cached_section(blob, "vsx")
        if section is None:
            text, error = _fetch_tap(
                fetcher, timeout_s, tap_url(VIZIER_TAP, _vsx_query(float(ra), float(dec), radius_arcsec)),
            )
            if error:
                blob["vsx"] = {"ok": 0, "error": error}
                vsx_error = error
            else:
                _store_section(blob, "vsx", text)
                section = blob["vsx"]
        if section and section.get("ok"):
            vsx_rows = parse_table(section.get("text") or "")

    gaia_rows: list[dict] = []
    gaia_error = ""
    if ra is None or dec is None:
        gaia_error = "no sky position"
    else:
        section = _cached_section(blob, "gaia")
        if section is None:
            text, error = _fetch_tap(
                fetcher, timeout_s, tap_url(VIZIER_TAP, _gaia_query(float(ra), float(dec))),
            )
            if error:
                blob["gaia"] = {"ok": 0, "error": error}
                gaia_error = error
            else:
                _store_section(blob, "gaia", text)
                section = blob["gaia"]
        if section and section.get("ok"):
            gaia_rows = parse_table(section.get("text") or "")

    try:
        _save_json(tic_cache_path(cache, tic), blob)
        bound_catalog_cache(cache, int(float(cache_max_mb) * 1_000_000))
    except OSError:
        pass

    checks = [
        simbad_check(simbad_rows, error=simbad_error if not simbad_rows else "", radius=radius_arcsec),
        vsx_check(vsx_rows, error=vsx_error, radius=radius_arcsec, ra=ra, dec=dec),
        _list_check("toi", "TOI list", toi_rows, error=toi_error if not toi_text else "", label="TOI"),
        _list_check("ctoi", "CTOI list", ctoi_rows, error=ctoi_error if not ctoi_text else "", label="CTOI"),
        gaia_check(gaia_rows, error=gaia_error, ra=ra, dec=dec, depth=depth),
    ]
    meta = {
        "ra": ra,
        "dec": dec,
        "radius": stellar.get("rad"),
        "mass": stellar.get("mass"),
        "tmag": stellar.get("tmag"),
        "simbad_id": next((row.get("main_id") for row in simbad_rows if row.get("main_id")), None),
        "vsx_name": _column(vsx_rows[0], "Name", "name") if vsx_rows else None,
    }
    return checks, meta


def catalog_links(tic: int, ra, dec, simbad_id, vsx_name) -> dict:
    ident = simbad_id or f"TIC {int(tic)}"
    links = {
        "simbad": "https://simbad.cfa.harvard.edu/simbad/sim-id?Ident=" + urllib.parse.quote(ident),
        "exofop": f"https://exofop.ipac.caltech.edu/tess/target.php?id={int(tic)}",
    }
    if ra is not None and dec is not None:
        coord = f"{float(ra):.5f}+{float(dec):.5f}"
        links["vsx"] = (
            "https://vizier.cds.unistra.fr/viz-bin/VizieR-3?-source=B/vsx/vsx&-c="
            + urllib.parse.quote(f"{coord},eq=J2000,rs=20")
        )
        links["gaia"] = (
            "https://vizier.cds.unistra.fr/viz-bin/VizieR-3?-source=I/355/gaiadr3&-c="
            + urllib.parse.quote(f"{coord},eq=J2000,rs=42")
        )
    elif vsx_name:
        links["vsx"] = "https://www.aavso.org/vsx/index.php?view=search.top&ident=" + urllib.parse.quote(vsx_name)
    else:
        links["vsx"] = "https://www.aavso.org/vsx/index.php?view=search.top"
    return links
