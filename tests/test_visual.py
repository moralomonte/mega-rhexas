"""Picture speed is local. It does not describe worker load."""
from rhexas.worker.visual import cycle_seconds, loops_enabled, resolve_visual


def test_legacy_names_map_and_reduced_motion_is_slow():
    assert resolve_visual("eco") == "slow"
    assert resolve_visual("live") == "normal"
    assert resolve_visual("turbo") == "realtime"
    assert resolve_visual("slow") == "slow"
    assert resolve_visual("nope") == "normal"
    assert resolve_visual("turbo", reduced_motion=True) == "slow"
    assert cycle_seconds("slow") == 5
    assert cycle_seconds("normal") == 3
    assert cycle_seconds("realtime") == 0
    assert cycle_seconds("eco") == 5
    assert loops_enabled("normal") is True
    assert loops_enabled("realtime") is False
    assert loops_enabled("slow", reduced_motion=True) is False
