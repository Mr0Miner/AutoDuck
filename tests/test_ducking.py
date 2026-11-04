"""Deterministic tests for the ducking math (no audio hardware required)."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autoduck.ducking import (  # noqa: E402
    AUTO,
    FIXED,
    DuckingController,
    db_to_fraction,
    rms_to_dbfs,
)


def test_rms_to_dbfs():
    assert abs(rms_to_dbfs(1.0) - 0.0) < 1e-6
    assert abs(rms_to_dbfs(0.5) - (-6.0206)) < 1e-3
    assert rms_to_dbfs(0.0) < -100  # treated as silence


def test_db_to_fraction():
    assert db_to_fraction(0.0) == 1.0
    assert db_to_fraction(-60.0) == 0.0
    assert db_to_fraction(-120.0) == 0.0
    assert abs(db_to_fraction(-30.0) - 0.5) < 1e-9


def test_auto_silence_returns_unity():
    c = DuckingController()
    c.mode = AUTO
    # Feed silence for a while; gain must settle back to 1.0.
    for _ in range(200):
        c.update(-120.0, 1 / 60)
    assert abs(c.gain - 1.0) < 1e-3


def test_auto_louder_means_more_reduction():
    c = DuckingController()
    c.mode = AUTO
    c.max_reduction = 0.8

    c.reset()
    for _ in range(200):
        g_loud = c.update(-3.0, 1 / 60)

    c.reset()
    for _ in range(200):
        g_mid = c.update(-30.0, 1 / 60)

    # Higher level -> lower gain (more reduction).
    assert g_loud < g_mid <= 1.0


def test_auto_max_reduction_respected():
    c = DuckingController()
    c.mode = AUTO
    c.max_reduction = 0.6
    for _ in range(400):
        g = c.update(0.0, 1 / 60)
    # At 0 dBFS the reduction should approach max_reduction but never exceed it.
    assert g >= 1.0 - c.max_reduction - 1e-3
    assert g <= 1.0


def test_fixed_reduction_exact():
    c = DuckingController()
    c.mode = FIXED
    c.fixed_reduction = 0.5
    c.sensitivity = 50.0
    for _ in range(300):
        g = c.update(0.0, 1 / 60)  # loud, gate open
    assert abs(g - 0.5) < 1e-3, g


def test_fixed_restores_after_silence():
    c = DuckingController()
    c.mode = FIXED
    c.fixed_reduction = 0.5
    for _ in range(300):
        c.update(0.0, 1 / 60)
    assert c.gain < 0.6
    for _ in range(600):
        g = c.update(-120.0, 1 / 60)
    assert abs(g - 1.0) < 1e-3, g


def test_fixed_hysteresis_prevents_chatter():
    c = DuckingController()
    c.mode = FIXED
    c.fixed_reduction = 0.5
    c.hysteresis_db = 6.0
    thr = c.threshold_db
    # Open the gate.
    c.update(thr + 2, 1 / 60)
    assert c.gate_open
    # Drop just below threshold but within hysteresis -> must stay open.
    c.update(thr - 3, 1 / 60)
    assert c.gate_open
    # Drop well below threshold -> closes.
    c.update(thr - 10, 1 / 60)
    assert not c.gate_open


def test_attack_faster_than_release():
    c = DuckingController()
    c.mode = FIXED
    c.fixed_reduction = 0.5
    c.attack_ms = 40.0
    c.release_ms = 320.0

    # Attack: from unity toward 0.5
    c.reset()
    g_after_attack = c.update(0.0, 0.04)
    attack_progress = 1.0 - g_after_attack  # 0 -> 0.5 range

    # Release: from 0.5 toward unity
    c._gain = 0.5
    c._gate = True
    g_after_release = c.update(-120.0, 0.04)
    release_progress = g_after_release - 0.5

    # In the same time window, attack moves further toward its target.
    assert attack_progress > release_progress


def test_hold_keeps_volume_down_after_silence():
    """After speech stops, the gain must stay low for hold_ms before rising."""
    c = DuckingController()
    c.mode = FIXED
    c.fixed_reduction = 0.5
    c.hold_ms = 2000.0

    # Speak for a while -> fully ducked to 0.5.
    for _ in range(200):
        c.update(0.0, 1 / 60)
    assert abs(c.gain - 0.5) < 1e-2, c.gain

    # Feed silence for 1.5s (less than the 2s hold): gain must NOT rise much.
    for _ in range(int(1.5 * 60)):
        g = c.update(-120.0, 1 / 60)
    assert g < 0.55, f"volume started rising too early: {g}"
    assert c.holding

    # After the full hold plus release time, it must be back to unity.
    for _ in range(int(3.0 * 60)):
        g = c.update(-120.0, 1 / 60)
    assert abs(g - 1.0) < 1e-2, f"did not restore after hold: {g}"


def test_hold_zero_restores_promptly():
    """hold_ms=0 means restore as soon as input goes quiet."""
    c = DuckingController()
    c.mode = FIXED
    c.fixed_reduction = 0.5
    c.hold_ms = 0.0
    for _ in range(200):
        c.update(0.0, 1 / 60)
    assert c.gain < 0.6
    for _ in range(int(1.5 * 60)):
        g = c.update(-120.0, 1 / 60)
    assert abs(g - 1.0) < 1e-2, g


def test_hold_recharged_by_continued_speech():
    """A brief dip during speech should not let the hold expire early."""
    c = DuckingController()
    c.mode = FIXED
    c.fixed_reduction = 0.5
    c.hold_ms = 1000.0

    # Speak, then a short 0.3s dip (less than hold), then speak again.
    for _ in range(60):
        c.update(0.0, 1 / 60)
    for _ in range(int(0.3 * 60)):
        c.update(-120.0, 1 / 60)
    assert c.gain < 0.6, "should still be ducked during a short pause"
    for _ in range(60):
        c.update(0.0, 1 / 60)
    # Now go quiet for 1.5s: hold should be freshly recharged.
    for _ in range(int(1.5 * 60)):
        g = c.update(-120.0, 1 / 60)
    # After the recharged 1s hold, it should be rising toward unity.
    assert g > 0.6, f"hold was not recharged by continued speech: {g}"


def test_no_overshoot():
    c = DuckingController()
    c.mode = AUTO
    for _ in range(500):
        g = c.update(0.0, 1 / 60)
        assert 0.0 <= g <= 1.0


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)