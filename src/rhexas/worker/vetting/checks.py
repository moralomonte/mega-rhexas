"""Light-curve and centroid checks. No network and no disk I/O.

Each check returns pass, warn, fail, or unavailable, plus a short reason and
the numbers behind it. Callers pass the arrays already on disk.
"""
from __future__ import annotations

import math

import numpy as np

from rhexas.transit_depth import format_transit_depth

# TESS orbit and the downlink gap. Harmonics of this period often track systematics.
ORBIT_DAYS = 13.7
SECTOR_DAYS = 27.4
# Reaction-wheel desaturation in the SPOC QUALITY column.
MOMENTUM_DUMP_BIT = 32
ODD_EVEN_FAIL = 3.0
SECONDARY_FAIL = 3.0
# Ingress/egress deeper than this fraction of the event reads as V-shaped.
V_SHAPE_RATIO = 0.55
# Observed duration above this multiple of the circular, central expectation.
DURATION_LONG = 2.5
CENTROID_WARN = 3.0
CENTROID_FAIL = 5.0
RUWE_WARN = 1.4
# Two TESS pixels. A neighbour this close can dilute into the aperture.
NEIGHBOUR_ARCSEC = 42.0
PIXEL_ARCSEC = 21.0
ALIAS_FAIL = 1.5

DISCLAIMER = (
    "This is an automated advisory check. It does not confirm the signal, "
    "and it is not a discovery. A person still needs to review the star and follow it up."
)

VERDICTS = (
    "Known object",
    "Likely false positive",
    "Needs review",
    "Passed automated checks",
)


def check(name: str, title: str, status: str, reason: str, numbers: dict | None = None, **extra) -> dict:
    if status not in {"pass", "warn", "fail", "unavailable"}:
        raise ValueError(status)
    item = {
        "name": name,
        "title": title,
        "status": status,
        "reason": reason,
        "numbers": numbers or {},
    }
    item.update(extra)
    return item


def summarize(checks: list[dict]) -> str:
    """One line for the table. A known catalog match outranks other failures."""
    if any(item.get("known") and item.get("status") == "fail" for item in checks):
        return "Known object"
    if any(item.get("status") == "fail" for item in checks):
        return "Likely false positive"
    if any(item.get("status") in {"warn", "unavailable"} for item in checks):
        return "Needs review"
    if checks and all(item.get("status") == "pass" for item in checks):
        return "Passed automated checks"
    return "Needs review"


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _as_arrays(time, flux):
    t = np.asarray(time, dtype=float)
    y = np.asarray(flux, dtype=float)
    if t.shape != y.shape or t.size == 0:
        return None
    mask = np.isfinite(t) & np.isfinite(y)
    t, y = t[mask], y[mask]
    if t.size < 8:
        return None
    med = float(np.median(y))
    if math.isfinite(med) and med > 0:
        y = y / med
    order = np.argsort(t)
    return t[order], y[order]


def _phases(time, t0: float, period: float):
    """Phase in [0, 1), with 0 at mid-transit."""
    return np.mod(np.asarray(time, dtype=float) - t0, period) / period


def _wrap_distance(phase, center: float):
    return np.abs((np.asarray(phase, dtype=float) - center + 0.5) % 1.0 - 0.5)


def measure_dip(time, flux, period, t0, duration, center: float = 0.0) -> dict | None:
    """Depth of a box at ``center`` (phase 0–1). Positive depth is a dip."""
    period_f = _finite(period)
    t0_f = _finite(t0)
    dur = _finite(duration)
    packed = _as_arrays(time, flux)
    if packed is None or period_f is None or t0_f is None or dur is None:
        return None
    if period_f <= 0 or dur <= 0:
        return None
    t, y = packed
    half = 0.5 * dur / period_f
    if half <= 0 or half >= 0.45:
        return None
    dist = _wrap_distance(_phases(t, t0_f, period_f), center)
    inside = dist <= half
    outside = (dist > 2.0 * half) & (dist < min(0.5, max(6.0 * half, 3.0 * half)))
    y_in = y[inside]
    y_out = y[outside]
    if y_in.size < 3 or y_out.size < 5:
        return None
    baseline = float(np.median(y_out))
    depth = baseline - float(np.median(y_in))
    scatter = float(np.std(y_out))
    err = scatter / math.sqrt(float(y_in.size))
    if err < 1e-8:
        if abs(depth) < 1e-6:
            sigma = 0.0
        else:
            sigma = 1e6 if depth > 0 else -1e6
        err = 0.0
    else:
        sigma = depth / err
    return {
        "depth": depth,
        "sigma": float(sigma),
        "err": float(err),
        "n_in": int(y_in.size),
        "n_out": int(y_out.size),
        "baseline": baseline,
    }


def box_fit(time, flux, period, t0, duration) -> dict | None:
    """RMS of a box at this period. A lower RMS is a better description of the curve."""
    period_f = _finite(period)
    t0_f = _finite(t0)
    dur = _finite(duration)
    packed = _as_arrays(time, flux)
    if packed is None or period_f is None or t0_f is None or dur is None or period_f <= 0 or dur <= 0:
        return None
    t, y = packed
    half = 0.5 * dur / period_f
    if half <= 0 or half >= 0.45:
        return None
    dist = _wrap_distance(_phases(t, t0_f, period_f), 0.0)
    inside = dist <= half
    outside = dist > 2.0 * half
    if int(inside.sum()) < 3 or int(outside.sum()) < 5:
        return None
    baseline = float(np.median(y[outside]))
    depth = baseline - float(np.median(y[inside]))
    if depth < 0:
        depth = 0.0
    model = np.full(y.shape, baseline, dtype=float)
    model[inside] = baseline - depth
    rms = float(np.sqrt(np.mean((y - model) ** 2)))
    return {"rms": rms, "depth": float(depth), "n_in": int(inside.sum()), "period": float(period_f)}


def odd_even_check(time, flux, period, t0, duration) -> dict:
    packed = _as_arrays(time, flux)
    period_f = _finite(period)
    t0_f = _finite(t0)
    if packed is None or period_f is None or t0_f is None or period_f <= 0:
        return check(
            "odd_even", "Odd and even transits", "unavailable",
            "The light curve on disk does not have enough points to compare odd and even transits.",
        )
    t, y = packed
    epoch = np.round((t - t0_f) / period_f).astype(int)
    odd = epoch % 2 == 0
    left = measure_dip(t[odd], y[odd], period, t0, duration, 0.0)
    right = measure_dip(t[~odd], y[~odd], period, t0, duration, 0.0)
    if left is None or right is None:
        return check(
            "odd_even", "Odd and even transits", "unavailable",
            "One of the two transit sets does not have enough points.",
            {"odd_depth": None if left is None else round(left["depth"], 6),
             "even_depth": None if right is None else round(right["depth"], 6)},
        )
    err = math.hypot(left["err"], right["err"])
    diff = abs(left["depth"] - right["depth"])
    if err < 1e-8:
        sigma = 0.0 if diff < 1e-5 else 1e6
    else:
        sigma = diff / err
    numbers = {
        "odd_depth": round(left["depth"], 6),
        "even_depth": round(right["depth"], 6),
        "difference": round(diff, 6),
        "sigma": round(float(sigma), 2),
        "odd_points": left["n_in"],
        "even_points": right["n_in"],
    }
    reason = (
        f"Odd transits are {format_transit_depth(left['depth'])} deep and even transits are "
        f"{format_transit_depth(right['depth'])} deep. The difference is {sigma:.1f} sigma."
    )
    if sigma > ODD_EVEN_FAIL:
        return check("odd_even", "Odd and even transits", "fail", reason, numbers)
    return check("odd_even", "Odd and even transits", "pass", reason, numbers)


def secondary_check(time, flux, period, t0, duration) -> dict:
    primary = measure_dip(time, flux, period, t0, duration, 0.0)
    at_half = measure_dip(time, flux, period, t0, duration, 0.5)
    if primary is None or at_half is None:
        return check(
            "secondary", "Secondary eclipse", "unavailable",
            "The light curve on disk does not have enough points to search for a second dip.",
        )
    primary_depth = max(primary["depth"], 0.0)
    best = None
    period_f = float(period)
    dur = float(duration)
    half = 0.5 * dur / period_f
    for center in np.linspace(0.15, 0.85, 29):
        if abs(center - 0.5) <= max(2.0 * half, 0.04):
            continue
        if center <= max(2.0 * half, 0.08) or center >= 1.0 - max(2.0 * half, 0.08):
            continue
        trial = measure_dip(time, flux, period, t0, duration, float(center))
        if trial is None or trial["depth"] <= 0:
            continue
        if best is None or trial["sigma"] > best["sigma"]:
            best = {"phase": round(float(center), 3), **trial}
    numbers = {
        "primary_depth": round(primary_depth, 6),
        "phase_half_depth": round(at_half["depth"], 6),
        "phase_half_sigma": round(at_half["sigma"], 2),
        "other_phase": None if best is None else best["phase"],
        "other_depth": None if best is None else round(best["depth"], 6),
        "other_sigma": None if best is None else round(best["sigma"], 2),
    }
    half_hit = at_half["depth"] > 0 and at_half["sigma"] >= SECONDARY_FAIL and (
        primary_depth <= 0 or at_half["depth"] >= 0.2 * primary_depth
    )
    other_hit = best is not None and best["sigma"] >= 4.0 and (
        primary_depth <= 0 or best["depth"] >= 0.25 * primary_depth
    )
    other_warn = best is not None and best["sigma"] >= SECONDARY_FAIL and not other_hit and (
        primary_depth <= 0 or best["depth"] >= 0.1 * primary_depth
    )
    if half_hit:
        reason = (
            f"A dip at phase 0.5 is {format_transit_depth(at_half['depth'])} deep "
            f"({at_half['sigma']:.1f} sigma), beside a primary of {format_transit_depth(primary_depth)}."
        )
        return check("secondary", "Secondary eclipse", "fail", reason, numbers)
    if other_hit:
        reason = (
            f"The deepest extra dip is at phase {best['phase']:.2f}, "
            f"{format_transit_depth(best['depth'])} deep ({best['sigma']:.1f} sigma). "
            "That can be an eclipse on an off-center orbit."
        )
        return check("secondary", "Secondary eclipse", "fail", reason, numbers)
    if other_warn or (at_half["sigma"] >= SECONDARY_FAIL and at_half["depth"] > 0 and not half_hit):
        reason = (
            f"Phase 0.5 is {format_transit_depth(at_half['depth'])} ({at_half['sigma']:.1f} sigma). "
            "Nothing there is both deep and significant, but a weaker extra dip is worth a look."
        )
        return check("secondary", "Secondary eclipse", "warn", reason, numbers)
    reason = (
        f"Phase 0.5 is {format_transit_depth(at_half['depth'])} ({at_half['sigma']:.1f} sigma), "
        f"against a primary of {format_transit_depth(primary_depth)}. No second dip stands out."
    )
    return check("secondary", "Secondary eclipse", "pass", reason, numbers)


def shape_check(time, flux, period, t0, duration) -> dict:
    """Ratio of the transit shoulders to the center. A box stays near 1; a V falls well below."""
    packed = _as_arrays(time, flux)
    period_f = _finite(period)
    t0_f = _finite(t0)
    dur = _finite(duration)
    if packed is None or period_f is None or t0_f is None or dur is None or period_f <= 0 or dur <= 0:
        return check(
            "shape", "Transit shape", "unavailable",
            "The light curve on disk does not have enough points to judge the transit shape.",
        )
    t, y = packed
    half = 0.5 * dur / period_f
    if half <= 0 or half >= 0.45:
        return check("shape", "Transit shape", "unavailable", "The transit duration is not usable for a shape test.")
    phase = _phases(t, t0_f, period_f)
    phase = np.where(phase > 0.5, phase - 1.0, phase)
    base_m = (np.abs(phase) > 2.5 * half) & (np.abs(phase) < min(0.5, 8.0 * half))
    if int(base_m.sum()) < 5:
        base_m = np.abs(phase) > 2.0 * half
    inside = np.abs(phase) <= half
    if int(base_m.sum()) < 5 or int(inside.sum()) < 6:
        return check("shape", "Transit shape", "unavailable", "Not enough in-transit points to judge the shape.")
    baseline = float(np.median(y[base_m]))
    x = np.abs(phase[inside]) / half
    depth = baseline - y[inside]
    center = depth[x <= 0.35]
    edge = depth[(x >= 0.7) & (x <= 1.0)]
    if center.size < 2 or edge.size < 2:
        return check("shape", "Transit shape", "unavailable", "Not enough points on the shoulders to judge the shape.")
    center_d = float(np.median(center))
    edge_d = float(np.median(edge))
    if center_d <= 1e-5:
        return check(
            "shape", "Transit shape", "unavailable",
            "The center of the transit is not deeper than the continuum, so the shape was not scored.",
            {"center_depth": round(center_d, 6), "edge_depth": round(edge_d, 6)},
        )
    ratio = edge_d / center_d
    numbers = {
        "edge_over_center": round(float(ratio), 3),
        "center_depth": round(center_d, 6),
        "edge_depth": round(edge_d, 6),
    }
    if ratio < V_SHAPE_RATIO:
        return check(
            "shape", "Transit shape", "warn",
            f"The shoulders are only {ratio:.0%} as deep as the center, so the event looks V-shaped rather than flat-bottomed.",
            numbers,
        )
    return check(
        "shape", "Transit shape", "pass",
        f"The shoulders are {ratio:.0%} as deep as the center, which is what a flat-bottomed event looks like.",
        numbers,
    )


def expected_duration_days(period_days: float, radius_sun: float, mass_sun: float) -> float | None:
    """Circular, central duration in days. Needs a radius and a mass, both in solar units."""
    period_f = _finite(period_days)
    radius = _finite(radius_sun)
    mass = _finite(mass_sun)
    if period_f is None or radius is None or mass is None:
        return None
    if period_f <= 0 or radius <= 0 or mass <= 0:
        return None
    # a / R_sun for a solar-mass star at 1 year is 1 AU, about 215 solar radii.
    a_over_rsun = 215.032 * (mass ** (1.0 / 3.0)) * ((period_f / 365.25) ** (2.0 / 3.0))
    a_over_r = a_over_rsun / radius
    if a_over_r <= 1:
        return None
    return (period_f / math.pi) * math.asin(min(1.0, 1.0 / a_over_r))


def duration_check(period, duration, radius, mass) -> dict:
    observed = _finite(duration)
    if observed is None or observed <= 0:
        return check(
            "duration", "Transit duration", "unavailable",
            "The BLS result on disk has no duration to compare.",
        )
    expected = expected_duration_days(period, radius, mass)
    observed_h = observed * 24.0
    if expected is None:
        return check(
            "duration", "Transit duration", "unavailable",
            "No stellar radius and mass are available from the TIC, so the duration was not compared.",
            {"observed_hours": round(observed_h, 3), "radius_sun": _finite(radius), "mass_sun": _finite(mass)},
        )
    ratio = observed / expected
    numbers = {
        "observed_hours": round(observed_h, 3),
        "expected_hours": round(expected * 24.0, 3),
        "ratio": round(float(ratio), 2),
        "radius_sun": round(float(radius), 3),
        "mass_sun": round(float(mass), 3),
    }
    if ratio > DURATION_LONG:
        return check(
            "duration", "Transit duration", "warn",
            (
                f"The event lasts {observed_h:.2f} h, about {ratio:.1f} times longer than the "
                f"{expected * 24.0:.2f} h expected for this radius and mass. That can mean a giant or a blend."
            ),
            numbers,
        )
    return check(
        "duration", "Transit duration", "pass",
        f"The event lasts {observed_h:.2f} h, against {expected * 24.0:.2f} h expected for this radius and mass.",
        numbers,
    )


def _harmonic(period: float):
    targets = [ORBIT_DAYS, SECTOR_DAYS]
    for divisor in (2, 3, 4):
        targets.append(ORBIT_DAYS / divisor)
    for target in targets:
        if abs(period - target) / target <= 0.03:
            return target
    return None


def _transit_times(time, t0: float, period: float, duration: float) -> list[float]:
    t = np.asarray(time, dtype=float)
    start, stop = float(np.min(t)), float(np.max(t))
    n0 = math.floor((start - t0) / period) - 1
    n1 = math.ceil((stop - t0) / period) + 1
    found = []
    half = 0.5 * duration
    for n in range(n0, n1 + 1):
        mid = t0 + n * period
        if mid < start - half or mid > stop + half:
            continue
        span = (t > mid - half) & (t < mid + half)
        if int(span.sum()) >= 1 and start <= mid <= stop:
            found.append(mid)
    return found


def systematics_check(time, flux, period, t0, duration, quality=None) -> dict:
    packed = _as_arrays(time, flux)
    period_f = _finite(period)
    t0_f = _finite(t0)
    dur = _finite(duration)
    if packed is None or period_f is None or t0_f is None or dur is None or period_f <= 0 or dur <= 0:
        return check(
            "systematics", "Period and timing", "unavailable",
            "The light curve on disk does not have times to compare with the orbit and the sector edges.",
        )
    t, _y = packed
    mids = _transit_times(t, t0_f, period_f, dur)
    notes = []
    status = "pass"
    harmonic = _harmonic(period_f)
    if harmonic is not None:
        notes.append(f"The period {period_f:.3f} d sits near {harmonic:.2f} d, a TESS orbit harmonic.")
        status = "warn"
    if len(mids) < 2:
        notes.append("Only one transit falls in this sector, so a single glitch could explain it.")
        status = "warn"
    edges = [float(np.min(t)), float(np.max(t))]
    if t.size >= 3:
        gaps = np.diff(t)
        for index in np.where(gaps > 0.5)[0]:
            edges.append(float(t[index]))
            edges.append(float(t[index + 1]))
    near = 0
    for mid in mids:
        if min(abs(mid - edge) for edge in edges) <= 0.2:
            near += 1
    if mids and near * 2 >= len(mids) and near > 0:
        notes.append(f"{near} of {len(mids)} transits sit on a gap or a sector edge.")
        status = "warn"
    flagged = None
    if quality is not None:
        q_raw = np.asarray(quality, dtype=float)
        t_raw = np.asarray(time, dtype=float)
        y_raw = np.asarray(flux, dtype=float)
        if q_raw.shape == t_raw.shape == y_raw.shape:
            mask = np.isfinite(t_raw) & np.isfinite(y_raw)
            q = q_raw[mask][np.argsort(t_raw[mask])]
            if q.size == t.size:
                phase = _phases(t, t0_f, period_f)
                half = 0.5 * dur / period_f
                inside = _wrap_distance(phase, 0.0) <= half
                outside = _wrap_distance(phase, 0.0) > 2.0 * half
                bits = (np.nan_to_num(q).astype(int) & MOMENTUM_DUMP_BIT) != 0
                in_frac = float(np.mean(bits[inside])) if int(inside.sum()) else 0.0
                out_frac = float(np.mean(bits[outside])) if int(outside.sum()) else 0.0
                flagged = round(in_frac, 3)
                if int(bits[inside].sum()) >= 2 and in_frac >= 0.25 and in_frac > out_frac + 0.1:
                    notes.append(
                        f"{in_frac:.0%} of the in-transit cadences carry a momentum-dump flag, "
                        f"against {out_frac:.0%} out of transit."
                    )
                    status = "warn"
    if not notes:
        notes.append(
            f"{len(mids)} transits are on the sector, and the period is away from the 13.7 day orbit and its harmonics."
        )
    numbers = {
        "period_days": round(period_f, 5),
        "n_transits": len(mids),
        "harmonic_days": None if harmonic is None else round(harmonic, 3),
        "transits_near_edge": near,
        "momentum_dump_fraction": flagged,
    }
    return check("systematics", "Period and timing", status, " ".join(notes), numbers)


def alias_check(time, flux, period, t0, duration) -> dict:
    reported = box_fit(time, flux, period, t0, duration)
    if reported is None:
        return check(
            "alias", "Half and double period", "unavailable",
            "The light curve on disk does not have enough points to compare half and double the period.",
        )
    half = box_fit(time, flux, float(period) / 2.0, t0, duration)
    double = box_fit(time, flux, float(period) * 2.0, t0, duration)
    numbers = {
        "rms": round(reported["rms"], 6),
        "half_rms": None if half is None else round(half["rms"], 6),
        "double_rms": None if double is None else round(double["rms"], 6),
        "half_depth": None if half is None else round(half["depth"], 6),
        "double_depth": None if double is None else round(double["depth"], 6),
    }

    def better(other):
        if other is None or other["depth"] <= 0:
            return None
        if reported["rms"] <= 0:
            return other["rms"] <= 1e-6
        return other["rms"] <= reported["rms"] * 1.02

    def much_better(other):
        if other is None or other["depth"] <= 0 or reported["rms"] <= 0:
            return False
        return other["rms"] < reported["rms"] / ALIAS_FAIL

    if much_better(half) or much_better(double):
        which = "half" if much_better(half) else "double"
        return check(
            "alias", "Half and double period", "fail",
            f"A box at {which} the reported period fits the light curve clearly better.",
            numbers,
        )
    if better(half) or better(double):
        which = "half" if better(half) else "double"
        return check(
            "alias", "Half and double period", "warn",
            f"A box at {which} the reported period fits as well as the reported period.",
            numbers,
        )
    return check(
        "alias", "Half and double period", "pass",
        "The reported period fits better than half or double that period.",
        numbers,
    )


def centroid_check(sigma, shift_arcsec) -> dict:
    sigma_f = _finite(sigma)
    shift = _finite(shift_arcsec)
    numbers = {
        "sigma": None if sigma_f is None else round(sigma_f, 2),
        "shift_arcsec": None if shift is None else round(shift, 3),
    }
    if sigma_f is None and shift is None:
        return check(
            "centroid", "Centroid shift", "unavailable",
            "This sector has no in-transit centroid shift on disk.",
            numbers,
        )
    if sigma_f is None:
        if shift >= PIXEL_ARCSEC:
            return check(
                "centroid", "Centroid shift", "fail",
                f"The in-transit centroid moves {shift:.2f} arcsec, about a TESS pixel or more. That is a blend risk.",
                numbers,
            )
        if shift >= 0.5 * PIXEL_ARCSEC:
            return check(
                "centroid", "Centroid shift", "warn",
                f"The in-transit centroid moves {shift:.2f} arcsec. The significance was not stored.",
                numbers,
            )
        return check(
            "centroid", "Centroid shift", "pass",
            f"The in-transit centroid moves {shift:.2f} arcsec.",
            numbers,
        )
    shift_text = "an unstated distance" if shift is None else f"{shift:.2f} arcsec"
    if sigma_f >= CENTROID_FAIL:
        return check(
            "centroid", "Centroid shift", "fail",
            f"The in-transit centroid shifts by {shift_text} ({sigma_f:.1f} sigma). That is a blend risk.",
            numbers,
        )
    if sigma_f >= CENTROID_WARN:
        return check(
            "centroid", "Centroid shift", "warn",
            f"The in-transit centroid shifts by {shift_text} ({sigma_f:.1f} sigma). Worth a look for a blend.",
            numbers,
        )
    return check(
        "centroid", "Centroid shift", "pass",
        f"The in-transit centroid shifts by {shift_text} ({sigma_f:.1f} sigma).",
        numbers,
    )


def light_checks(time, flux, *, period, t0, duration, quality=None, radius=None, mass=None,
                 centroid_sigma=None, centroid_shift=None) -> list[dict]:
    return [
        odd_even_check(time, flux, period, t0, duration),
        secondary_check(time, flux, period, t0, duration),
        shape_check(time, flux, period, t0, duration),
        duration_check(period, duration, radius, mass),
        systematics_check(time, flux, period, t0, duration, quality),
        alias_check(time, flux, period, t0, duration),
        centroid_check(centroid_sigma, centroid_shift),
    ]


def fold_plot(time, flux, period, t0, duration, bins: int = 64) -> dict | None:
    """Binned phase curve plus odd and even overlays, for the detail page."""
    packed = _as_arrays(time, flux)
    period_f = _finite(period)
    t0_f = _finite(t0)
    dur = _finite(duration)
    if packed is None or period_f is None or t0_f is None or period_f <= 0:
        return None
    t, y = packed
    phase = _phases(t, t0_f, period_f)
    epoch = np.round((t - t0_f) / period_f).astype(int)

    def pack(mask):
        if int(np.sum(mask)) < 4:
            return [], []
        xp, yp = phase[mask], y[mask]
        edges = np.linspace(0.0, 1.0, bins + 1)
        idx = np.digitize(xp, edges) - 1
        xs, ys = [], []
        for i in range(bins):
            sel = yp[idx == i]
            if sel.size == 0:
                continue
            xs.append(round(float((edges[i] + edges[i + 1]) / 2), 4))
            ys.append(round(float(np.median(sel)), 6))
        return xs, ys

    all_x, all_y = pack(np.ones(t.shape, dtype=bool))
    odd_x, odd_y = pack(epoch % 2 == 0)
    even_x, even_y = pack(epoch % 2 == 1)
    half = 0.0 if dur is None or dur <= 0 else 0.5 * dur / period_f
    return {
        "phase": all_x,
        "flux": all_y,
        "odd_phase": odd_x,
        "odd_flux": odd_y,
        "even_phase": even_x,
        "even_flux": even_y,
        "secondary": [round(0.5 - half, 4), round(0.5 + half, 4)],
        "primary_half": round(half, 4),
    }
