"""Transit depth unit: stored flux fractions, single display conversion."""

import numpy as np

from rhexas.transit_depth import format_transit_depth
from rhexas.worker.vetting.checks import odd_even_check, secondary_check


def test_format_transit_depth_fraction_to_percent():
    assert format_transit_depth(0.0047) == "0.47%"


def test_format_transit_depth_small_uses_ppm():
    assert format_transit_depth(0.00012) == "120 ppm"


def test_fold_depth_not_doubled_on_percent_scale_flux():
    """Flux on a 0–100 percent scale must yield ~fractional depths, not thousands of %."""
    period, t0, dur = 3.2, 1.0, 0.08
    step = 2.0 / 1440.0
    times = np.arange(0.0, 27.0, step)
    phase = np.mod(times - t0, period) / period
    centered = np.where(phase > 0.5, phase - 1.0, phase)
    half = 0.5 * dur / period
    inside = np.abs(centered) <= half
    flux = np.ones(times.shape) * 100.0
    flux[inside] = 100.0 * (1.0 - 0.0047)
    odd = odd_even_check(times, flux, period, t0, dur)
    assert "7035" not in odd["reason"]
    assert "7000" not in odd["reason"]
    odd_depth = odd["numbers"].get("odd_depth")
    assert odd_depth is not None
    assert 0.003 < odd_depth < 0.006
    assert format_transit_depth(odd_depth) == "0.47%"
    sec = secondary_check(times, flux, period, t0, dur)
    primary = sec["numbers"].get("primary_depth")
    if primary is not None:
        assert primary < 0.02