"""Group agreed candidates by star and score them for a human to look at.

The review priority is arithmetic. Nothing here is trained. The same text is
``PRIORITY_HELP``, which the status page shows under the candidate table.

Review priority =
    0.5 × SNR (capped at 40)
  + 0.4 × SDE (capped at 30)
  + 6 for each extra sector whose period agrees (capped at 4 extra sectors)
  + 18 when the companion is under about 1.8 Jupiter radii and the duration fits
  − 80 for a known object
  − 40 for a likely binary
  − 35 for a serious blend, or 12 for a milder one
  − 15 for a Gaia warning that is not already an eclipsing non-single star
  − 10 for a SIMBAD or VSX warning
  − 40 while a catalog lookup has not succeeded
  − 200 when a person has marked the star rejected
  + 10 when a person has marked a CTOI submitted

SNR and SDE come from the agreeing detection with the highest SNR. A known
object is a SIMBAD variable or binary, a VSX match on the star, a TOI or CTOI,
or a NASA archive planet. A likely binary is a radius over about 2 Jupiter
radii, a duration that does not fit the star, odd and even transits that
disagree, a secondary eclipse, or a Gaia eclipsing non-single star. Higher is
looked at first.

Sectors merge when their periods agree within 1% or at a simple ratio
(1/2, 2, 1/3, 3, 2/3, 3/2, 1/4, 4). Odd/even and the secondary eclipse stay
"not checked" until the stored worker result includes ``odd_even_sigma`` and
``sec_sigma``.
"""
from __future__ import annotations

import math

PRIORITY_HELP = (
    "Review priority = 0.5×SNR (capped at 40) + 0.4×SDE (capped at 30) "
    "+ 6 for each extra sector whose period agrees within 1% or a simple ratio "
    "(1/2, 2, 1/3, 3, 2/3, 3/2, 1/4, 4; cap 4 extra sectors) "
    "+ 18 when the companion is under about 1.8 Jupiter radii and the duration fits the star. "
    "Subtract 80 for a known object (SIMBAD variable or binary, a VSX match on the star, "
    "a TOI or CTOI, or a NASA archive planet), "
    "40 for a likely binary (radius over about 2 Jupiter radii, a duration that does not fit, "
    "odd/even disagreement, a secondary eclipse, or a Gaia eclipsing non-single star), "
    "35 for a serious blend or 12 for a milder one, "
    "15 for another Gaia warning, 10 for a SIMBAD or VSX warning, "
    "and 40 while a catalog lookup has not succeeded. "
    "Rejected subtracts 200. Marking a CTOI submitted adds 10. "
    "SNR and SDE come from the agreeing sector with the highest SNR. "
    "Higher is looked at first. The arithmetic is fixed; nothing here is trained. "
    "Odd/even and the secondary eclipse stay not checked until the worker upload "
    "includes odd_even_sigma and sec_sigma."
)

# Period aliases BLS commonly reports for the same signal.
_RATIOS = (
    (1.0, ""),
    (2.0, "2×"),
    (0.5, "1/2×"),
    (3.0, "3×"),
    (1.0 / 3.0, "1/3×"),
    (1.5, "3/2×"),
    (2.0 / 3.0, "2/3×"),
    (4.0, "4×"),
    (0.25, "1/4×"),
)
PERIOD_RTOL = 0.01

# sqrt(depth) * R_sun / R_earth, then / R_jup. 2 Jupiter radii is the stellar cut.
R_SUN_EARTH = 109.2
R_JUP_EARTH = 11.21
RJUP_FAIL = 2.0
RJUP_WARN = 1.8
# a/R* at 1 day and solar density. See the comment on duration_limit.
A_OVER_R_1D = 4.206
DURATION_LONG = 1.5
DURATION_SHORT = 0.2
ODD_EVEN_MAX = 3.0
SECONDARY_MAX = 3.0
CONTRATIO_FAIL = 0.25
CONTRATIO_WARN = 0.05
CROWDSAP_MIN = 0.8
RUWE_WARN = 1.4

SNR_CAP = 40.0
SDE_CAP = 30.0
SNR_WEIGHT = 0.5
SDE_WEIGHT = 0.4
SECTOR_POINT = 6.0
SECTOR_EXTRA_CAP = 4
RADIUS_BONUS = 18.0
PENALTY_KNOWN = 80.0
PENALTY_BINARY = 40.0
PENALTY_BLEND_FAIL = 35.0
PENALTY_BLEND_WARN = 12.0
PENALTY_GAIA = 15.0
PENALTY_CATALOG_WARN = 10.0
PENALTY_UNKNOWN = 40.0
PENALTY_REJECTED = 200.0
BONUS_CTOI = 10.0

STATUS_LABELS = {
    "new": "New",
    "pending": "Checks pending",
    "passed": "Passed checks",
    "known": "Known object",
    "binary": "Likely binary",
    "rejected": "Rejected",
    "ctoi": "CTOI submitted",
}
REVIEW_STATUSES = tuple(STATUS_LABELS)

# SIMBAD types that mean the star is already a known variable, binary, or planet.
# Matched as prefixes so EB*Algol and PulsV*dSct follow EB and Puls.
_KNOWN_OTYPE_PREFIXES = (
    "EB", "V*", "Puls", "SB", "El*", "Pl", "Ir*", "Er*", "Ro*", "RR", "Ce*", "Mi*",
    "CV", "XB", "DN", "DQ", "AM*", "RS*", "BY*", "UV", "WX*", "Z*", "Or*", "TT*",
    "Ae*", "Be*", "WR*", "OH*", "Sy", "QSO", "AGN", "LMX", "HMX", "YSO", "pMS",
    "a2*", "dS*", "SN",
)
_ECLIPSING_VSX = ("EA", "EB", "EW", "ECL", "ELL", "EC", "ED", "ESD", "EP", "E")
_WORKER_ODD_EVEN = (
    "Not checked yet. The agreed result has no odd_even_sigma. "
    "The worker candidate row needs odd_even_sigma "
    "(depth_odd and depth_even are optional). "
    "Current workers send odd_even_sigma when candidates.csv has that column."
)
_WORKER_SECONDARY = (
    "Not checked yet. The agreed result has no sec_sigma. "
    "The worker candidate row needs sec_sigma, the secondary eclipse depth "
    "at phase 0.5 divided by its uncertainty. "
    "Current workers send sec_sigma when candidates.csv has that column."
)

_CHECK_NAMES = (
    "simbad", "vsx", "exofop", "nea", "gaia", "blend", "planet", "odd_even", "secondary",
)


def as_float(value):
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def periods_compatible(left, right) -> tuple[bool, str]:
    """True when two periods are the same signal or a simple alias of it."""
    first, second = as_float(left), as_float(right)
    if first is None or second is None or first <= 0 or second <= 0:
        return False, ""
    ratio = first / second
    for target, label in _RATIOS:
        if abs(ratio - target) <= PERIOD_RTOL * target:
            return True, label
    return False, ""


def _check(state: str, summary: str, detail: str, *, blocking: bool | None = None) -> dict:
    if blocking is None:
        blocking = state == "unknown"
    return {
        "state": state,
        "summary": summary,
        "detail": detail,
        "blocking": bool(blocking),
    }


def _when(detail: str, stamp: str) -> str:
    if stamp:
        return f"{detail} Checked {stamp}."
    return detail


def _known_otype(otype: str) -> bool:
    folded = (otype or "").strip().casefold()
    if not folded:
        return False
    return any(folded.startswith(prefix.casefold()) for prefix in _KNOWN_OTYPE_PREFIXES)


def _eclipsing_vsx(kind: str) -> bool:
    text = (kind or "").strip().upper().split("/")[0].strip()
    if not text:
        return False
    return text in _ECLIPSING_VSX or text.startswith("E") and len(text) <= 3


def _join(parts: list[str]) -> str:
    return " ".join(part.strip() for part in parts if part and part.strip())


def simbad_check(entry: dict | None) -> dict:
    """SIMBAD object type and names. A plain star is a pass."""
    if entry is None:
        return _check("unknown", "Not checked yet", "SIMBAD has not been queried yet.")
    if not entry.get("ok"):
        return _check("unknown", "Not checked yet", _service_down("SIMBAD", entry))
    payload = entry.get("payload") or {}
    if payload.get("skipped"):
        return _check("unknown", "Not checked yet", str(payload["skipped"]))
    objects = list(payload.get("objects") or [])
    stamp = entry.get("checked_at") or ""
    if not objects:
        return _check(
            "pass", "No SIMBAD object within 10 arcsec",
            _when("No object within 10 arcsec.", stamp), blocking=False,
        )
    closest = min(objects, key=lambda row: row.get("sep") if row.get("sep") is not None else 1e9)
    sep = as_float(closest.get("sep"))
    otype = str(closest.get("otype") or "").strip()
    names = _simbad_names(closest)
    described = _join([
        names or "unnamed",
        f"type {otype}" if otype else "type not given",
        f"{sep:.1f} arcsec" if sep is not None else "",
    ])
    others = _format_simbad_list(objects)
    detail = _when(others or described, stamp)
    if sep is not None and sep > 5:
        if _known_otype(otype):
            return _check(
                "warn", f"Nearby SIMBAD {otype}",
                _when(f"Closest object is {described}. It is outside 5 arcsec, so it may not be this star.", stamp),
                blocking=False,
            )
        return _check("pass", "No SIMBAD object on this star", detail, blocking=False)
    if _known_otype(otype):
        return _check("fail", f"SIMBAD {otype}: {names or 'named object'}", detail, blocking=False)
    if otype.strip() == "**":
        return _check("warn", f"SIMBAD double star {names}".strip(), detail, blocking=False)
    summary = f"SIMBAD {otype or 'star'}: {names}" if names else f"SIMBAD {otype or 'star'}"
    return _check("pass", summary, detail, blocking=False)


def _simbad_names(obj: dict) -> str:
    names = []
    main = str(obj.get("main_id") or "").strip()
    if main:
        names.append(main)
    for extra in obj.get("ids") or []:
        text = str(extra).strip()
        if text and text not in names:
            names.append(text)
    return ", ".join(names[:8])


def _format_simbad_list(objects: list[dict]) -> str:
    bits = []
    for obj in objects[:5]:
        sep = as_float(obj.get("sep"))
        where = f"{sep:.1f} arcsec" if sep is not None else "unknown separation"
        bits.append(f"{_simbad_names(obj) or 'unnamed'} [{obj.get('otype') or '?'}] {where}")
    return "; ".join(bits)


def vsx_check(entry: dict | None) -> dict:
    """A VSX variable within about 1 arcminute."""
    if entry is None:
        return _check("unknown", "Not checked yet", "VSX has not been queried yet.")
    if not entry.get("ok"):
        return _check("unknown", "Not checked yet", _service_down("VSX", entry))
    payload = entry.get("payload") or {}
    if payload.get("skipped"):
        return _check("unknown", "Not checked yet", str(payload["skipped"]))
    rows = list(payload.get("rows") or [])
    stamp = entry.get("checked_at") or ""
    if not rows:
        return _check(
            "pass", "No VSX variable within 1 arcmin",
            _when("No row in VSX B/vsx within 1 arcminute.", stamp), blocking=False,
        )
    closest = min(rows, key=lambda row: row.get("sep") if row.get("sep") is not None else 1e9)
    detail = _when(_format_vsx(rows), stamp)
    sep = as_float(closest.get("sep")) or 0.0
    kind = str(closest.get("type") or "")
    name = str(closest.get("name") or "unnamed")
    if _eclipsing_vsx(kind) or sep <= 15:
        return _check(
            "fail", f"VSX {name} ({kind or 'variable'}) at {sep:.0f} arcsec",
            detail, blocking=False,
        )
    return _check(
        "warn", f"VSX {name} ({kind or 'variable'}) at {sep:.0f} arcsec",
        detail, blocking=False,
    )


def _format_vsx(rows: list[dict]) -> str:
    bits = []
    for row in rows[:6]:
        sep = as_float(row.get("sep"))
        where = f"{sep:.0f} arcsec" if sep is not None else "?"
        period = row.get("period")
        period_bit = f", P={period} d" if period not in (None, "") else ""
        bits.append(f"{row.get('name') or 'unnamed'} {row.get('type') or ''}{period_bit} ({where})")
    return "; ".join(bits)


def exofop_check(toi_file, ctoi_file, tic: int) -> dict:
    """Already a TOI or a CTOI on ExoFOP."""
    toi_state, toi_rows, toi_detail = _file_rows(toi_file, tic, "TOI list")
    ctoi_state, ctoi_rows, ctoi_detail = _file_rows(ctoi_file, tic, "CTOI list")
    if toi_state == "unknown" or ctoi_state == "unknown":
        missing = []
        if toi_state == "unknown":
            missing.append(toi_detail)
        if ctoi_state == "unknown":
            missing.append(ctoi_detail)
        return _check("unknown", "Not checked yet", " ".join(missing))
    if toi_rows:
        bits = []
        for row in toi_rows[:6]:
            bits.append(_join([
                f"TOI {row.get('toi')}" if row.get("toi") else "TOI",
                str(row.get("disposition") or ""),
                f"P={row.get('period')} d" if row.get("period") else "",
            ]))
        return _check("fail", "Already a TOI", "; ".join(bits) + _file_stamp(toi_file), blocking=False)
    if ctoi_rows:
        bits = []
        for row in ctoi_rows[:6]:
            bits.append(_join([
                f"CTOI {row.get('ctoi')}" if row.get("ctoi") else "CTOI",
                f"P={row.get('period')} d" if row.get("period") else "",
            ]))
        return _check("fail", "Already a CTOI", "; ".join(bits) + _file_stamp(ctoi_file), blocking=False)
    return _check(
        "pass", "Not a TOI or CTOI",
        "This TIC is not in the ExoFOP TOI or CTOI lists." + _file_stamp(toi_file),
        blocking=False,
    )


def nea_check(nea_file, tic: int) -> dict:
    """A known planet in the NASA Exoplanet Archive composite table."""
    state, rows, detail = _file_rows(nea_file, tic, "NASA Exoplanet Archive")
    if state == "unknown":
        return _check("unknown", "Not checked yet", detail)
    if rows:
        bits = []
        for row in rows[:6]:
            bits.append(_join([
                str(row.get("name") or "planet"),
                f"P={row.get('period')} d" if row.get("period") else "",
            ]))
        return _check(
            "fail", "Known planet: " + ", ".join(str(row.get("name") or "planet") for row in rows[:4]),
            "; ".join(bits) + _file_stamp(nea_file), blocking=False,
        )
    return _check(
        "pass", "No archive planet",
        "No planet in the NASA Exoplanet Archive composite table for this TIC." + _file_stamp(nea_file),
        blocking=False,
    )


def _file_rows(record, tic: int, label: str):
    if not record:
        return "unknown", [], f"The {label} has not been downloaded yet."
    if not record.get("ok"):
        reason = record.get("error") or "the download failed"
        return "unknown", [], f"The {label} could not be downloaded ({reason})."
    by_tic = record.get("by_tic") or {}
    return "ready", list(by_tic.get(int(tic)) or []), ""


def _file_stamp(record) -> str:
    if not record:
        return ""
    stamp = record.get("fetched_at") or ""
    extra = ""
    if record.get("error"):
        extra = f" A later refresh failed ({record['error']})."
    if stamp:
        return f" List fetched {stamp}.{extra}"
    return extra


def gaia_check(entry: dict | None) -> dict:
    """Gaia DR3 variability, non-single star, and RUWE."""
    if entry is None:
        return _check("unknown", "Not checked yet", "Gaia has not been queried yet.")
    if not entry.get("ok"):
        return _check("unknown", "Not checked yet", _service_down("Gaia", entry))
    payload = entry.get("payload") or {}
    if payload.get("skipped"):
        return _check("unknown", "Not checked yet", str(payload["skipped"]))
    rows = list(payload.get("rows") or [])
    stamp = entry.get("checked_at") or ""
    if not rows:
        return _check(
            "warn", "No Gaia DR3 source within 5 arcsec",
            _when("No gaiadr3.gaia_source row within 5 arcsec.", stamp), blocking=False,
        )
    row = min(rows, key=lambda item: item.get("sep") if item.get("sep") is not None else 1e9)
    ruwe = as_float(row.get("ruwe"))
    nss = row.get("non_single_star")
    try:
        nss_code = int(nss) if nss not in (None, "") else 0
    except (TypeError, ValueError):
        nss_code = 0
    variable = str(row.get("phot_variable_flag") or "").strip().upper()
    sep = as_float(row.get("sep"))
    bits = [f"source {row.get('source_id') or '?'}"]
    if sep is not None:
        bits.append(f"{sep:.1f} arcsec")
    if ruwe is not None:
        bits.append(f"RUWE {ruwe:.2f}")
    bits.append(f"non_single_star {nss_code}")
    if variable:
        bits.append(f"phot_variable_flag {variable}")
    detail = _when("; ".join(bits) + ". " + _gaia_nss_meaning(nss_code), stamp)
    # Gaia DR3 non_single_star: 3, 5, 6, and 7 include an eclipsing solution.
    if nss_code in (3, 5, 6, 7):
        return _check("fail", f"Gaia eclipsing non-single star ({nss_code})", detail, blocking=False)
    warnings = []
    if nss_code in (1, 2, 4):
        warnings.append(f"non-single star ({nss_code})")
    if ruwe is not None and ruwe > RUWE_WARN:
        warnings.append(f"RUWE {ruwe:.2f}")
    if variable == "VARIABLE":
        warnings.append("variability flag VARIABLE")
    if warnings:
        return _check("warn", "Gaia " + ", ".join(warnings), detail, blocking=False)
    return _check("pass", "Gaia looks single", detail, blocking=False)


def _gaia_nss_meaning(code: int) -> str:
    return {
        0: "Not flagged as a non-single star.",
        1: "Astrometric non-single star.",
        2: "Spectroscopic non-single star.",
        3: "Eclipsing non-single star.",
        4: "Astrometric and spectroscopic.",
        5: "Astrometric and eclipsing.",
        6: "Spectroscopic and eclipsing.",
        7: "Astrometric, spectroscopic, and eclipsing.",
    }.get(code, "Unrecognised non_single_star code.")


def blend_check(entry: dict | None, depth, flags: str) -> dict:
    """Contamination ratio, a bright neighbour, and a CROWDSAP flag from the worker."""
    crowdsap = crowdsap_from_flags(flags)
    reasons_fail = []
    reasons_warn = []
    if crowdsap is not None and crowdsap < CROWDSAP_MIN:
        reasons_fail.append(f"worker flags CROWDSAP {crowdsap:.2f}, below {CROWDSAP_MIN:.1f}")
    if entry is None:
        if reasons_fail:
            return _check("fail", reasons_fail[0], " ".join(reasons_fail), blocking=False)
        return _check("unknown", "Not checked yet", "The TIC has not been queried yet, so the contamination ratio is unknown.")
    if not entry.get("ok"):
        if reasons_fail:
            return _check("fail", reasons_fail[0], _join(reasons_fail + [_service_down("TIC", entry)]), blocking=False)
        return _check("unknown", "Not checked yet", _service_down("TIC", entry))
    payload = entry.get("payload") or {}
    stamp = entry.get("checked_at") or ""
    if not payload.get("found"):
        detail = _when(payload.get("skipped") or "This TIC was not in the TIC at VizieR.", stamp)
        if reasons_fail:
            return _check("fail", reasons_fail[0], _join(reasons_fail + [detail]), blocking=False)
        return _check("unknown", "Not checked yet", detail)
    contratio = as_float(payload.get("contratio"))
    tmag = as_float(payload.get("tmag"))
    if contratio is None:
        reasons_warn.append("TIC has no contamination ratio")
    elif contratio >= CONTRATIO_FAIL:
        reasons_fail.append(
            f"TIC contamination ratio {contratio:.3f} (nearby light is at least a quarter of the target)"
        )
    elif contratio >= CONTRATIO_WARN:
        reasons_warn.append(f"TIC contamination ratio {contratio:.3f}")
    neighbors = list(payload.get("neighbors") or [])
    depth_value = as_float(depth)
    dm_max = None
    if depth_value is not None and depth_value > 0:
        dm_max = -2.5 * math.log10(depth_value)
    brightest = None
    for neighbor in neighbors:
        sep = as_float(neighbor.get("sep"))
        mag = as_float(neighbor.get("tmag"))
        if sep is None:
            continue
        if brightest is None or (mag is not None and (brightest[1] is None or mag < brightest[1])):
            brightest = (neighbor, mag, sep)
        if tmag is None or mag is None:
            continue
        delta = mag - tmag
        if sep <= 21 and delta <= 1:
            reasons_fail.append(
                f"TIC {neighbor.get('tic')} is {delta:.1f} mag from the target at {sep:.0f} arcsec"
            )
        elif dm_max is not None and sep <= 42 and delta < dm_max and delta < 5:
            reasons_warn.append(
                f"TIC {neighbor.get('tic')} at {sep:.0f} arcsec could produce this depth ({delta:.1f} mag fainter)"
            )
    if payload.get("neighbor_error"):
        reasons_warn.append(f"neighbour search failed ({payload['neighbor_error']})")
    detail_bits = []
    if contratio is not None:
        detail_bits.append(f"Contamination ratio {contratio:.3f}.")
    if tmag is not None:
        detail_bits.append(f"Tmag {tmag:.2f}.")
    if brightest is not None:
        neighbor, mag, sep = brightest
        mag_text = f"{mag:.2f}" if mag is not None else "unknown"
        detail_bits.append(f"Brightest neighbour TIC {neighbor.get('tic')} Tmag {mag_text} at {sep:.0f} arcsec.")
    elif not payload.get("neighbor_error"):
        detail_bits.append("No other TIC within 1 arcmin.")
    detail_bits.extend(reasons_fail)
    detail_bits.extend(reasons_warn)
    detail = _when(" ".join(detail_bits), stamp)
    if reasons_fail:
        return _check("fail", reasons_fail[0], detail, blocking=False)
    if reasons_warn:
        return _check("warn", reasons_warn[0], detail, blocking=False)
    summary = "Low contamination"
    if contratio is not None:
        summary = f"Contamination ratio {contratio:.3f}"
    return _check("pass", summary, detail, blocking=False)


def crowdsap_from_flags(flags: str) -> float | None:
    text = flags or ""
    marker = "CROWDSAP"
    index = text.upper().find(marker)
    if index < 0:
        return None
    tail = text[index + len(marker):].strip()
    token = []
    for char in tail:
        if char.isdigit() or char == ".":
            token.append(char)
        elif token:
            break
    return as_float("".join(token))


def planet_check(entry: dict | None, detections: list[dict]) -> dict:
    """Companion radius from depth and TIC radius, plus duration against stellar density."""
    if entry is None:
        return _check("unknown", "Not checked yet", "The TIC stellar radius has not been queried yet.")
    if not entry.get("ok"):
        return _check("unknown", "Not checked yet", _service_down("TIC", entry))
    payload = entry.get("payload") or {}
    stamp = entry.get("checked_at") or ""
    if not payload.get("found"):
        return _check("unknown", "Not checked yet", _when(payload.get("skipped") or "This TIC was not in the TIC.", stamp))
    radius = as_float(payload.get("rad"))
    mass = as_float(payload.get("mass"))
    if radius is None or radius <= 0:
        return _check(
            "unknown", "Not checked yet",
            _when("The TIC row has no stellar radius, so the companion size was not computed.", stamp),
        )
    depths = []
    for row in detections:
        depth = as_float(row.get("depth"))
        if depth is not None and depth > 0:
            depths.append((depth, row))
    if not depths:
        return _check("unknown", "Not checked yet", "The detection has no positive depth.")
    depth, row = max(depths, key=lambda item: item[0])
    rp_earth, rp_jup = companion_radius(depth, radius)
    size = (
        f"Depth {depth * 1e6:.0f} ppm in sector {row.get('sector')} and R* {radius:.3f} Rsun "
        f"imply Rp {rp_earth:.1f} Rearth ({rp_jup:.2f} Rjup)."
    )
    duration_text, duration_state = _duration_sentence(detections, radius, mass)
    detail = _when(size + " " + duration_text, stamp)
    if rp_jup > RJUP_FAIL or duration_state == "fail":
        summary = f"Rp {rp_jup:.2f} Rjup, likely stellar" if rp_jup > RJUP_FAIL else "Duration does not fit the star"
        return _check("fail", summary, detail, blocking=False)
    if rp_jup > RJUP_WARN or duration_state == "warn":
        summary = f"Rp {rp_jup:.2f} Rjup, near the stellar limit" if rp_jup > RJUP_WARN else f"Rp {rp_jup:.2f} Rjup"
        return _check("warn", summary, detail, blocking=False)
    return _check("pass", f"Rp {rp_jup:.2f} Rjup", detail, blocking=False)


def companion_radius(depth: float, rstar_sun: float) -> tuple[float, float]:
    """(Rearth, Rjup) from a central transit depth and a stellar radius in solar units."""
    rp_earth = math.sqrt(depth) * rstar_sun * R_SUN_EARTH
    return rp_earth, rp_earth / R_JUP_EARTH


def duration_limit(period_days: float, density_ratio: float, depth: float) -> float | None:
    """Maximum central circular duration in days.

    a/R* = (G ρ P² / 3π)^(1/3). At solar density and P in days that is
    4.206 × P^(2/3) × (ρ/ρsun)^(1/3), which is about 215 for the Earth.
    T_max ≈ (P/π) × (1+k) / (a/R*) for impact parameter 0. The 1.5× margin
    used by the caller leaves room for a modest eccentricity.
    """
    if period_days <= 0 or density_ratio <= 0 or depth <= 0:
        return None
    k = math.sqrt(depth)
    a_over_r = A_OVER_R_1D * (period_days ** (2.0 / 3.0)) * (density_ratio ** (1.0 / 3.0))
    if a_over_r <= 0:
        return None
    return (period_days / math.pi) * (1.0 + k) / a_over_r


def _duration_sentence(detections: list[dict], radius: float, mass: float | None) -> tuple[str, str]:
    have = [row for row in detections if as_float(row.get("duration")) is not None and as_float(row.get("period"))]
    if not have:
        return (
            "Duration check: not checked yet. The worker result needs duration in days.",
            "warn",
        )
    if mass is None or mass <= 0 or radius <= 0:
        return (
            "Duration check: not checked yet. The TIC row has no stellar mass, so the density is unknown.",
            "warn",
        )
    density = mass / (radius ** 3)
    worst = "pass"
    bits = [f"Stellar density {density:.3f} solar from M {mass:.3f} and R {radius:.3f}."]
    for row in have:
        period = as_float(row.get("period"))
        duration = as_float(row.get("duration"))
        depth = as_float(row.get("depth")) or 0.0
        limit = duration_limit(period, density, depth if depth > 0 else 1e-6)
        if limit is None or duration is None:
            continue
        bits.append(
            f"Sector {row.get('sector')}: duration {duration * 24:.2f} h, "
            f"central circular maximum about {limit * 24:.2f} h."
        )
        if duration > DURATION_LONG * limit:
            worst = "fail"
            bits.append("That is longer than 1.5× the central duration, so it does not fit this star.")
        elif duration < DURATION_SHORT * limit and worst != "fail":
            worst = "warn"
            bits.append("That is under 0.2× the central duration, so it is unusually short.")
    return " ".join(bits), worst


def odd_even_check(detections: list[dict]) -> dict:
    values = [as_float(row.get("odd_even_sigma")) for row in detections]
    present = [value for value in values if value is not None]
    if not present:
        return _check("unknown", "Not checked yet", _WORKER_ODD_EVEN, blocking=False)
    worst = max(present)
    detail = f"Largest odd_even_sigma among agreeing sectors is {worst:.2f}. The worker filter flags values above {ODD_EVEN_MAX:.0f}."
    if worst > ODD_EVEN_MAX:
        return _check("fail", f"Odd/even {worst:.1f}σ", detail, blocking=False)
    return _check("pass", f"Odd/even {worst:.1f}σ", detail, blocking=False)


def secondary_check(detections: list[dict]) -> dict:
    values = [as_float(row.get("sec_sigma")) for row in detections]
    present = [value for value in values if value is not None]
    if not present:
        return _check("unknown", "Not checked yet", _WORKER_SECONDARY, blocking=False)
    worst = max(present, key=lambda value: abs(value))
    detail = (
        f"Largest |sec_sigma| among agreeing sectors is {abs(worst):.2f} "
        f"(signed {worst:.2f}). This is the phase-0.5 depth divided by its uncertainty. "
        f"The worker filter flags absolute values above {SECONDARY_MAX:.0f}."
    )
    if abs(worst) > SECONDARY_MAX:
        return _check("fail", f"Secondary {abs(worst):.1f}σ", detail, blocking=False)
    return _check("pass", f"Secondary {abs(worst):.1f}σ", detail, blocking=False)


def _service_down(name: str, entry: dict) -> str:
    payload = entry.get("payload") or {}
    reason = payload.get("error") or "the service did not respond"
    return f"{name} is not available ({reason}). The next try is automatic."


def checks_for(tic: int, cluster: list[dict], cache: dict, files: dict) -> dict:
    flags = "; ".join(str(row.get("flags") or "") for row in cluster if row.get("flags"))
    depth = None
    for row in cluster:
        value = as_float(row.get("depth"))
        if value is not None and (depth is None or value > depth):
            depth = value
    cache = cache or {}
    return {
        "simbad": simbad_check(cache.get("simbad")),
        "vsx": vsx_check(cache.get("vsx")),
        "exofop": exofop_check(files.get("exofop_toi"), files.get("exofop_ctoi"), tic),
        "nea": nea_check(files.get("nea_planets"), tic),
        "gaia": gaia_check(cache.get("gaia")),
        "blend": blend_check(cache.get("tic"), depth, flags),
        "planet": planet_check(cache.get("tic"), cluster),
        "odd_even": odd_even_check(cluster),
        "secondary": secondary_check(cluster),
    }


def automatic_status(checks: dict) -> tuple[str, str]:
    """Return (code, sentence) before any hand-set status.

    Checks pending means a catalog lookup that can change the decision has not
    succeeded. A missing odd/even or secondary value does not hold the star
    there. Known object wins over likely binary when a catalog already names
    the star. A hard blend, once the other lookups have finished, uses the
    likely-binary state so it is not marked passed; the sentence says it is
    contamination.
    """
    if not checks:
        return "pending", "Catalog checks have not finished."
    if any(checks[name]["state"] == "fail" for name in ("simbad", "vsx", "exofop", "nea")):
        reasons = [checks[name]["summary"] for name in ("simbad", "vsx", "exofop", "nea") if checks[name]["state"] == "fail"]
        return "known", "Known object: " + "; ".join(reasons) + "."
    if any(checks[name]["state"] == "unknown" and checks[name].get("blocking", True) for name in _CHECK_NAMES):
        waiting = [
            name for name in _CHECK_NAMES
            if checks[name]["state"] == "unknown" and checks[name].get("blocking", True)
        ]
        return "pending", "Still waiting on: " + ", ".join(waiting) + "."
    binary = [
        checks[name]["summary"]
        for name in ("planet", "odd_even", "secondary", "gaia")
        if checks[name]["state"] == "fail"
    ]
    if binary:
        return "binary", "Likely binary: " + "; ".join(binary) + "."
    if checks["blend"]["state"] == "fail":
        return "binary", "High contamination: " + checks["blend"]["summary"] + ". It is not marked as passed."
    return "passed", "No check failed. Warnings, if any, are on their own badges."


def _started(cache: dict, files: dict) -> bool:
    if cache:
        return True
    for record in files.values():
        if record:
            return True
    return False


def review_priority(snr, sde, n_sectors: int, checks: dict, review_status: str | None) -> tuple[float, list[dict]]:
    """Explainable score. Higher means look at this star first.

    Parts are rounded to 0.1 and the total is their sum, so the tooltip adds up.
    """
    snr_value = min(max(as_float(snr) or 0.0, 0.0), SNR_CAP)
    sde_value = min(max(as_float(sde) or 0.0, 0.0), SDE_CAP)
    parts = {
        "signal": SNR_WEIGHT * snr_value + SDE_WEIGHT * sde_value,
        "sectors": SECTOR_POINT * min(max(int(n_sectors), 1) - 1, SECTOR_EXTRA_CAP),
    }
    planet = checks.get("planet") or {}
    if planet.get("state") == "pass":
        parts["radius"] = RADIUS_BONUS
    if any(checks.get(name, {}).get("state") == "fail" for name in ("simbad", "vsx", "exofop", "nea")):
        parts["known"] = -PENALTY_KNOWN
    elif any(checks.get(name, {}).get("state") == "warn" for name in ("simbad", "vsx")):
        parts["catalog"] = -PENALTY_CATALOG_WARN
    if any(checks.get(name, {}).get("state") == "fail" for name in ("planet", "odd_even", "secondary", "gaia")):
        parts["binary"] = -PENALTY_BINARY
    blend = (checks.get("blend") or {}).get("state")
    if blend == "fail":
        parts["blend"] = -PENALTY_BLEND_FAIL
    elif blend == "warn":
        parts["blend"] = -PENALTY_BLEND_WARN
    gaia = checks.get("gaia") or {}
    if gaia.get("state") == "warn":
        parts["gaia"] = -PENALTY_GAIA
    if any(item.get("state") == "unknown" and item.get("blocking", True) for item in checks.values()):
        parts["pending"] = -PENALTY_UNKNOWN
    if review_status == "rejected":
        parts["rejected"] = -PENALTY_REJECTED
    elif review_status == "ctoi":
        parts["ctoi"] = BONUS_CTOI
    ordered = []
    total = 0.0
    for label in ("signal", "sectors", "radius", "known", "catalog", "binary", "blend", "gaia", "pending", "rejected", "ctoi"):
        if label not in parts:
            continue
        value = round(parts[label], 1)
        if value == 0:
            continue
        ordered.append({"label": label, "value": value})
        total += value
    return round(total, 1), ordered


def _cluster(detections: list[dict]) -> tuple[list[dict], list[dict]]:
    parent = list(range(len(detections)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for left in range(len(detections)):
        for right in range(left + 1, len(detections)):
            ok, _label = periods_compatible(detections[left].get("period"), detections[right].get("period"))
            if ok:
                parent[find(right)] = find(left)
    groups: dict[int, list[dict]] = {}
    for index, row in enumerate(detections):
        groups.setdefault(find(index), []).append(row)
    ranked = sorted(groups.values(), key=_cluster_rank)
    primary = ranked[0]
    extra = [row for group in ranked[1:] for row in group]
    return primary, extra


def _cluster_rank(rows: list[dict]):
    sectors = {int(row["sector"]) for row in rows if row.get("sector") is not None}
    snr = max((as_float(row.get("snr")) or -1.0) for row in rows)
    sector = min(int(row["sector"]) for row in rows)
    return (-len(sectors), -snr, sector)


def _best_row(rows: list[dict]) -> dict:
    return min(rows, key=lambda row: (
        -(as_float(row.get("snr")) or -1.0),
        -(as_float(row.get("sde")) or -1.0),
        int(row.get("sector") or 0),
    ))


def _ratio_against(period, reference) -> str:
    ok, label = periods_compatible(period, reference)
    if not ok:
        return ""
    return label


def build_groups(detections: list[dict], caches: dict, reviews: dict, files: dict) -> list[dict]:
    """One public row per TIC. ``detections`` are candidate rows plus worker fields."""
    by_tic: dict[int, list[dict]] = {}
    for row in detections:
        try:
            tic = int(row["tic"])
        except (KeyError, TypeError, ValueError):
            continue
        by_tic.setdefault(tic, []).append(row)
    groups = []
    for tic, rows in by_tic.items():
        primary, extra = _cluster(rows)
        best = _best_row(primary)
        reference = as_float(best.get("period"))
        sectors = sorted({int(row["sector"]) for row in primary if row.get("sector") is not None})
        ratio_labels = []
        for row in primary:
            label = _ratio_against(row.get("period"), reference)
            if label and label not in ratio_labels:
                ratio_labels.append(label)
        if len(sectors) > 1 and ratio_labels:
            period_note = "Periods agree, including " + ", ".join(ratio_labels) + "."
        elif len(sectors) > 1:
            period_note = "Periods agree."
        else:
            period_note = "One sector."
        if extra:
            period_note += f" {len(extra)} detection(s) on this star have a period that does not match."
        cache = caches.get(tic) or {}
        review = reviews.get(tic) or {}
        hand = review.get("status") or None
        if hand not in STATUS_LABELS:
            hand = None
        checks = checks_for(tic, primary, cache, files)
        if not _started(cache, files) and all(
            item["state"] == "unknown" and item.get("blocking", True) for item in checks.values()
            if item.get("blocking", True)
        ) and not any(item["state"] != "unknown" for item in checks.values()):
            code, detail = "new", "Agreed by the workers. Catalog checks have not run yet."
        else:
            code, detail = automatic_status(checks)
        manual = hand is not None
        if manual:
            shown = hand
            sentence = f"{STATUS_LABELS[hand]} was set by hand."
            if hand != code:
                sentence += f" The checks on their own read as {STATUS_LABELS.get(code, code)}."
            if review.get("note"):
                sentence += " Note: " + str(review["note"])
        else:
            shown = code
            sentence = detail
            if review.get("note"):
                sentence += " Note: " + str(review["note"])
        priority, parts = review_priority(best.get("snr"), best.get("sde"), len(sectors), checks, hand)
        flags = []
        for row in primary + extra:
            text = str(row.get("flags") or "").strip()
            if text and text not in flags:
                flags.append(text)
        primary_ids = {id(row) for row in primary}
        groups.append({
            "tic": tic,
            "sector": int(best["sector"]) if best.get("sector") is not None else None,
            "sectors": sectors,
            "sectors_label": ", ".join(str(sector) for sector in sectors),
            "n_sectors": len(sectors),
            "period": as_float(best.get("period")),
            "period_note": period_note,
            "depth": as_float(best.get("depth")),
            "sde": as_float(best.get("sde")),
            "snr": as_float(best.get("snr")),
            "score": as_float(best.get("score")),
            "flags": "; ".join(flags),
            "review_priority": priority,
            "priority_parts": parts,
            "status": STATUS_LABELS.get(shown, shown),
            "status_code": shown,
            "status_label": STATUS_LABELS.get(shown, shown),
            "status_detail": sentence,
            "automatic_status": code,
            "review_manual": manual,
            "review_note": str(review.get("note") or ""),
            "review_updated_at": review.get("updated_at") or "",
            "checks": checks,
            "detections": [_detection_public(row, primary_ids, reference) for row in sorted(
                primary + extra, key=lambda item: int(item.get("sector") or 0),
            )],
        })
    return groups


def _detection_public(row: dict, primary_ids: set[int], reference) -> dict:
    grouped = id(row) in primary_ids
    label = ""
    if grouped and reference is not None:
        label = _ratio_against(row.get("period"), reference)
    elif not grouped:
        label = "period does not match"
    return {
        "sector": int(row["sector"]) if row.get("sector") is not None else None,
        "period": as_float(row.get("period")),
        "depth": as_float(row.get("depth")),
        "sde": as_float(row.get("sde")),
        "snr": as_float(row.get("snr")),
        "duration": as_float(row.get("duration")),
        "odd_even_sigma": as_float(row.get("odd_even_sigma")),
        "sec_sigma": as_float(row.get("sec_sigma")),
        "grouped": grouped,
        "ratio": label,
        "flags": str(row.get("flags") or ""),
    }
