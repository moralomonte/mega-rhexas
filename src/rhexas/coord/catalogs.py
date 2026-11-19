"""Public catalog lookups for agreed candidates.

No API key is sent. SIMBAD, VizieR, ExoFOP, the NASA Exoplanet Archive, and
Gaia are anonymous HTTP endpoints. SIMBAD's main server is sometimes down, so
a failed call is repeated against the Harvard mirror. A down service returns
an error payload; it does not raise.

The coordinator process is plain Python on a small machine. This module uses
the standard library only.
"""
from __future__ import annotations

import csv
import io
import urllib.parse
import urllib.request

USER_AGENT = "MegaRhexasCoordinator/1.0 (+https://megarhexas.com; catalog cross-check)"

SIMBAD_PRIMARY = "https://simbad.cds.unistra.fr/simbad/sim-tap/sync"
SIMBAD_MIRROR = "https://simbad.cfa.harvard.edu/simbad/sim-tap/sync"
VIZIER_TAP = "https://tapvizier.cds.unistra.fr/TAPVizieR/tap/sync"
GAIA_TAP = "https://gea.esac.esa.int/tap-server/tap/sync"
NEA_TAP = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync"
TOI_URL = "https://exofop.ipac.caltech.edu/tess/download_toi.php?sort=toi&output=csv"
CTOI_URL = "https://exofop.ipac.caltech.edu/tess/download_ctoi.php?sort=ctoi&output=csv"

TIC_TABLES = ("IV/39/tic82", "IV/39/tic")
FILE_URLS = {
    "exofop_toi": TOI_URL,
    "exofop_ctoi": CTOI_URL,
    "nea_planets": None,
}
STAR_SOURCES = ("tic", "simbad", "vsx", "gaia")
FRESH_DAYS = 30
FAIL_HOURS = 6

TAP_LIMIT = 1_000_000
FILE_LIMIT = 25_000_000
TAP_TIMEOUT = 25
FILE_TIMEOUT = 60


def urllib_fetch(url: str, timeout: float, limit: int) -> tuple[int, str]:
    """GET ``url`` and return (status, body). Raises on a transport failure."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/csv, text/plain;q=0.9, */*;q=0.1"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError(f"response larger than {limit} bytes")
    return int(status), body.decode("utf-8", errors="replace")


def tap_url(base: str, query: str) -> str:
    return base + "?" + urllib.parse.urlencode({
        "REQUEST": "doQuery",
        "LANG": "ADQL",
        "FORMAT": "csv",
        "QUERY": query,
    })


def nea_url() -> str:
    query = "SELECT tic_id, pl_name, pl_orbper FROM pscomppars WHERE tic_id IS NOT NULL"
    return NEA_TAP + "?" + urllib.parse.urlencode({"query": query, "format": "csv"})


def _num(value: float) -> str:
    return f"{float(value):.8f}"


def _csv_ok(status: int, text: str) -> bool:
    if status < 200 or status >= 300:
        return False
    head = (text or "").lstrip()[:400].casefold()
    if not head:
        return False
    if head.startswith("<") or head.startswith("{"):
        return False
    if "query_status" in head and "error" in head:
        return False
    return True


def short_error(text: str) -> str:
    flat = " ".join(str(text or "").split())
    folded = flat.casefold()
    if not flat:
        return "no response"
    if "<html" in folded or flat.startswith("<"):
        return "the service returned a page instead of a table"
    return flat[:180]


def parse_csv(text: str) -> list[dict]:
    """TAP or ExoFOP CSV. Comment lines and blank lines are skipped."""
    if text and text.startswith("\ufeff"):
        text = text[1:]
    kept = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
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
        if item and all(set(value) <= set("- ") and value for value in item.values()):
            continue
        rows.append(item)
    return rows


def _column(row: dict, *names: str) -> str:
    folded = {str(key).casefold(): value for key, value in row.items()}
    for name in names:
        if name.casefold() in folded:
            return folded[name.casefold()]
    return ""


def _float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _tic_id(value) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.upper().startswith("TIC"):
        text = text[3:].strip()
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def angular_sep_arcsec(ra1, dec1, ra2, dec2) -> float | None:
    import math
    first_ra, first_dec = _float(ra1), _float(dec1)
    second_ra, second_dec = _float(ra2), _float(dec2)
    if None in (first_ra, first_dec, second_ra, second_dec):
        return None
    dra = math.radians(first_ra - second_ra)
    dec_a = math.radians(first_dec)
    dec_b = math.radians(second_dec)
    step = math.sin((dec_b - dec_a) / 2.0) ** 2 + math.cos(dec_a) * math.cos(dec_b) * math.sin(dra / 2.0) ** 2
    return math.degrees(2.0 * math.asin(min(1.0, math.sqrt(step)))) * 3600.0


def index_toi(text: str) -> dict[int, list[dict]]:
    found: dict[int, list[dict]] = {}
    for row in parse_csv(text):
        tic = _tic_id(_column(row, "TIC ID", "TIC", "tic_id", "tic"))
        if tic is None:
            continue
        found.setdefault(tic, []).append({
            "toi": _column(row, "TOI", "Full TOI ID"),
            "disposition": _column(row, "TFOPWG Disposition", "Disposition"),
            "period": _column(row, "Period (days)", "Period"),
        })
    return found


def index_ctoi(text: str) -> dict[int, list[dict]]:
    found: dict[int, list[dict]] = {}
    for row in parse_csv(text):
        tic = _tic_id(_column(row, "TIC ID", "TIC", "tic_id", "tic"))
        if tic is None:
            continue
        found.setdefault(tic, []).append({
            "ctoi": _column(row, "CTOI", "CTOI ID"),
            "period": _column(row, "Period (days)", "Period"),
        })
    return found


def index_nea(text: str) -> dict[int, list[dict]]:
    found: dict[int, list[dict]] = {}
    for row in parse_csv(text):
        tic = _tic_id(_column(row, "tic_id", "TIC ID", "TIC"))
        if tic is None:
            continue
        found.setdefault(tic, []).append({
            "name": _column(row, "pl_name", "Planet Name"),
            "period": _column(row, "pl_orbper", "Period"),
        })
    return found


def index_catalog_file(name: str, text: str) -> dict[int, list[dict]]:
    if name == "exofop_toi":
        return index_toi(text)
    if name == "exofop_ctoi":
        return index_ctoi(text)
    if name == "nea_planets":
        return index_nea(text)
    return {}


class CatalogClient:
    """Rate-limited catalog calls. ``fetcher`` is ``(url, timeout, limit) -> (status, text)``."""

    def __init__(self, fetcher=None, *, gap_s: float = 1.5, sleep=None):
        self._fetcher = fetcher or urllib_fetch
        self.gap_s = gap_s
        self._sleep = sleep
        self._calls = 0

    def _pace(self) -> None:
        if self._calls and self.gap_s and self._sleep is not None:
            self._sleep(self.gap_s)
        elif self._calls and self.gap_s and self._sleep is None:
            import time
            time.sleep(self.gap_s)
        self._calls += 1

    def fetch(self, url: str, *, timeout: float, limit: int) -> tuple[int, str]:
        self._pace()
        try:
            status, text = self._fetcher(url, timeout, limit)
        except Exception as exc:
            return 0, short_error(str(exc))
        return int(status), text if isinstance(text, str) else str(text)

    def refresh_files(self, names: list[str]) -> dict[str, dict]:
        out = {}
        for name in names:
            url = nea_url() if name == "nea_planets" else FILE_URLS.get(name)
            if not url:
                out[name] = {"ok": 0, "body": "", "error": f"unknown list {name}"}
                continue
            status, text = self.fetch(url, timeout=FILE_TIMEOUT, limit=FILE_LIMIT)
            if _csv_ok(status, text) and _has_header(text):
                out[name] = {"ok": 1, "body": text, "error": ""}
            else:
                out[name] = {"ok": 0, "body": "", "error": short_error(text or f"HTTP {status}")}
        return out

    def check_star(self, tic: int, need: list[str], tic_payload: dict | None) -> dict[str, dict]:
        """Look up the sources in ``need``. Network stays inside this method."""
        out: dict[str, dict] = {}
        payload = tic_payload
        if "tic" in need:
            out["tic"] = self.lookup_tic(int(tic))
            record = out["tic"]
            if record["ok"] and record["payload"].get("found"):
                payload = record["payload"]
            else:
                skipped = record["ok"] and not record["payload"].get("found")
                for source in ("simbad", "vsx", "gaia"):
                    if source not in need:
                        continue
                    if skipped:
                        out[source] = {"ok": 1, "payload": {"skipped": "No coordinates in the TIC, so this cone search was not run."}}
                    else:
                        out[source] = {"ok": 0, "payload": {"error": "Skipped because the TIC lookup failed."}}
                return out
        ra = _float((payload or {}).get("ra"))
        dec = _float((payload or {}).get("dec"))
        if ("simbad" in need or "vsx" in need or "gaia" in need) and (ra is None or dec is None):
            for source in ("simbad", "vsx", "gaia"):
                if source in need:
                    out[source] = {"ok": 0, "payload": {"error": "No TIC coordinates yet."}}
            return out
        if "simbad" in need:
            out["simbad"] = self.lookup_simbad(ra, dec)
        if "vsx" in need:
            out["vsx"] = self.lookup_vsx(ra, dec)
        if "gaia" in need:
            out["gaia"] = self.lookup_gaia(ra, dec)
        return out

    def lookup_tic(self, tic: int) -> dict:
        star = None
        table_used = ""
        last_error = "VizieR returned no TIC row"
        for table in TIC_TABLES:
            status, text = self.fetch(
                tap_url(VIZIER_TAP, _tic_by_id(table, tic)), timeout=TAP_TIMEOUT, limit=TAP_LIMIT,
            )
            if not _csv_ok(status, text):
                last_error = short_error(text or f"HTTP {status}")
                continue
            rows = parse_csv(text)
            if not rows:
                last_error = f"No row in {table}"
                continue
            star = _tic_star(rows[0])
            table_used = table
            break
        if star is None or star.get("ra") is None or star.get("dec") is None:
            return {"ok": 0, "payload": {"found": False, "error": last_error}}
        status, text = self.fetch(
            tap_url(VIZIER_TAP, _tic_cone(table_used, star["ra"], star["dec"])),
            timeout=TAP_TIMEOUT, limit=TAP_LIMIT,
        )
        neighbors = []
        neighbor_error = ""
        if not _csv_ok(status, text):
            neighbor_error = short_error(text or f"HTTP {status}")
        else:
            for row in parse_csv(text):
                other = _tic_id(_column(row, "TIC", "tic"))
                if other is None or other == tic:
                    continue
                sep = angular_sep_arcsec(star["ra"], star["dec"], _column(row, "RAJ2000", "ra"), _column(row, "DEJ2000", "dec"))
                if sep is None or sep > 60:
                    continue
                neighbors.append({
                    "tic": other,
                    "tmag": _float(_column(row, "Tmag", "tmag")),
                    "sep": round(sep, 2),
                })
            neighbors.sort(key=lambda item: item["sep"])
        star.update({
            "found": True,
            "table": table_used,
            "neighbors": neighbors[:25],
            "neighbor_error": neighbor_error,
        })
        return {"ok": 1, "payload": star}

    def lookup_simbad(self, ra: float, dec: float) -> dict:
        status, text = self.fetch(tap_url(SIMBAD_PRIMARY, _simbad_query(ra, dec)), timeout=TAP_TIMEOUT, limit=TAP_LIMIT)
        mirror = False
        if not _csv_ok(status, text):
            mirror = True
            status, text = self.fetch(tap_url(SIMBAD_MIRROR, _simbad_query(ra, dec)), timeout=TAP_TIMEOUT, limit=TAP_LIMIT)
        if not _csv_ok(status, text):
            where = "the main SIMBAD server and the Harvard mirror" if mirror else "SIMBAD"
            return {"ok": 0, "payload": {"error": f"{where} failed ({short_error(text or 'HTTP ' + str(status))})", "mirror": mirror}}
        objects = []
        for row in parse_csv(text):
            sep = angular_sep_arcsec(ra, dec, _column(row, "ra"), _column(row, "dec"))
            if sep is None:
                continue
            objects.append({
                "main_id": _column(row, "main_id"),
                "otype": _column(row, "otype"),
                "sep": round(sep, 2),
                "oid": _column(row, "oid"),
                "ids": [],
            })
        objects.sort(key=lambda item: item["sep"])
        objects = [item for item in objects if item["sep"] <= 10][:5]
        if objects and objects[0].get("oid"):
            names = self._simbad_ids(objects[0]["oid"], mirror)
            if names is not None:
                objects[0]["ids"] = names
        for item in objects:
            item.pop("oid", None)
        return {"ok": 1, "payload": {"mirror": mirror, "objects": objects}}

    def _simbad_ids(self, oid: str, mirror: bool) -> list[str] | None:
        try:
            oid_int = int(float(oid))
        except (TypeError, ValueError):
            return None
        base = SIMBAD_MIRROR if mirror else SIMBAD_PRIMARY
        status, text = self.fetch(
            tap_url(base, f"SELECT TOP 15 id FROM ident WHERE oidref = {oid_int}"),
            timeout=TAP_TIMEOUT, limit=TAP_LIMIT,
        )
        if not _csv_ok(status, text):
            return None
        names = []
        for row in parse_csv(text):
            name = _column(row, "id")
            if name and name not in names:
                names.append(name)
        return names[:12]

    def lookup_vsx(self, ra: float, dec: float) -> dict:
        status, text = self.fetch(tap_url(VIZIER_TAP, _vsx_query(ra, dec)), timeout=TAP_TIMEOUT, limit=TAP_LIMIT)
        if not _csv_ok(status, text):
            return {"ok": 0, "payload": {"error": short_error(text or f"HTTP {status}")}}
        rows = []
        for row in parse_csv(text):
            sep = angular_sep_arcsec(ra, dec, _column(row, "RAJ2000", "ra"), _column(row, "DEJ2000", "dec"))
            if sep is None or sep > 60:
                continue
            period = _column(row, "Period", "period")
            rows.append({
                "name": _column(row, "Name", "name"),
                "type": _column(row, "Type", "VType", "VarType"),
                "period": period,
                "sep": round(sep, 2),
            })
        rows.sort(key=lambda item: item["sep"])
        return {"ok": 1, "payload": {"rows": rows[:8]}}

    def lookup_gaia(self, ra: float, dec: float) -> dict:
        status, text = self.fetch(tap_url(GAIA_TAP, _gaia_query(ra, dec)), timeout=TAP_TIMEOUT, limit=TAP_LIMIT)
        if not _csv_ok(status, text):
            return {"ok": 0, "payload": {"error": short_error(text or f"HTTP {status}")}}
        rows = []
        for row in parse_csv(text):
            sep = angular_sep_arcsec(ra, dec, _column(row, "ra"), _column(row, "dec"))
            if sep is None or sep > 5:
                continue
            rows.append({
                "source_id": _column(row, "source_id"),
                "ruwe": _float(_column(row, "ruwe")),
                "non_single_star": _column(row, "non_single_star"),
                "phot_variable_flag": _column(row, "phot_variable_flag"),
                "sep": round(sep, 2),
            })
        rows.sort(key=lambda item: item["sep"])
        return {"ok": 1, "payload": {"rows": rows[:3]}}


def _has_header(text: str) -> bool:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return "," in stripped or "\t" in stripped
    return False


def _tic_star(row: dict) -> dict:
    return {
        "ra": _float(_column(row, "RAJ2000", "ra")),
        "dec": _float(_column(row, "DEJ2000", "dec")),
        "tmag": _float(_column(row, "Tmag", "tmag")),
        "rad": _float(_column(row, "rad", "Rstar", "st_rad")),
        "mass": _float(_column(row, "mass", "st_mass")),
        "contratio": _float(_column(row, "contratio", "contratio_TESS")),
    }


def _tic_by_id(table: str, tic: int) -> str:
    return (
        "SELECT TOP 1 TIC, RAJ2000, DEJ2000, Tmag, rad, mass, contratio "
        f'FROM "{table}" WHERE TIC = {int(tic)}'
    )


def _tic_cone(table: str, ra: float, dec: float) -> str:
    radius = _num(60.0 / 3600.0)
    return (
        "SELECT TOP 40 TIC, RAJ2000, DEJ2000, Tmag, rad, mass, contratio "
        f'FROM "{table}" WHERE CONTAINS(POINT(\'ICRS\', RAJ2000, DEJ2000), '
        f"CIRCLE('ICRS', {_num(ra)}, {_num(dec)}, {radius})) = 1"
    )


def _simbad_query(ra: float, dec: float) -> str:
    radius = _num(10.0 / 3600.0)
    return (
        "SELECT TOP 8 main_id, otype, ra, dec, oid FROM basic WHERE "
        f"CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {_num(ra)}, {_num(dec)}, {radius})) = 1"
    )


def _vsx_query(ra: float, dec: float) -> str:
    radius = _num(1.0 / 60.0)
    return (
        "SELECT TOP 20 Name, Type, Period, RAJ2000, DEJ2000 "
        'FROM "B/vsx/vsx" WHERE CONTAINS(POINT(\'ICRS\', RAJ2000, DEJ2000), '
        f"CIRCLE('ICRS', {_num(ra)}, {_num(dec)}, {radius})) = 1"
    )


def _gaia_query(ra: float, dec: float) -> str:
    radius = _num(5.0 / 3600.0)
    return (
        "SELECT TOP 5 source_id, ra, dec, ruwe, non_single_star, phot_variable_flag "
        "FROM gaiadr3.gaia_source WHERE "
        f"CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {_num(ra)}, {_num(dec)}, {radius})) = 1"
    )
