"""Regression tests for the start/stop lifecycle (the "start button" bug).

The original bug: if the worker thread died (device busy, unexpected error),
``_running`` stayed set, ``start()`` returned True without spawning anything,
and the UI showed "active" while nothing ran — unrecoverable.

These tests run without hardware: sounddevice is replaced with a fake whose
stream can be told to fail, so the worker dies exactly like a real device error.
"""

import sys
import os
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

import autoduck.engine as engine_mod  # noqa: E402
from autoduck.engine import Engine, EngineConfig  # noqa: E402


def _cfg():
    return EngineConfig(
        mic_index=0,
        target_ids=("x|y",),
        target_by_exe=False,
        mode="auto",
        sensitivity=55.0,
        max_reduction=0.8,
        fixed_reduction=0.5,
    )


class OkStream:
    def __init__(self, *a, **k):
        self._cb = k["callback"]
        self._stop = False
        self._t = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *e):
        self._stop = True
        return False

    def start(self):
        return self

    def stop(self):
        self._stop = True

    def _run(self):
        while not self._stop:
            self._cb(np.zeros((256, 1), dtype=np.float32), 256, None, None)
            time.sleep(0.01)


class BoomStream:
    """Opening the stream raises, simulating a busy/unplugged device."""

    def __init__(self, *a, **k):
        raise RuntimeError("device busy (simulated)")


def _fake_sd(stream_cls):
    class FakeSD:
        InputStream = staticmethod(stream_cls)

        @staticmethod
        def check_input_settings(**kwargs):
            return None  # pass the pre-check; failure happens at open time

    return FakeSD


def test_open_failure_reports_not_started():
    # If the stream cannot open, start() must report failure immediately and
    # never leave the engine claiming to run (this is what wedged the UI).
    engine_mod.sd = _fake_sd(BoomStream)
    statuses = []
    e = Engine(on_level=lambda v: None, on_status=lambda k, p: statuses.append(k))
    assert e.start(_cfg()) is False, "start() must fail when the device cannot open"
    time.sleep(0.2)
    assert e.running is False, "engine still claims running after open failure"
    assert e._thread is None or not e._thread.is_alive()
    e.stop()
    print("PASS test_open_failure_reports_not_started")


def test_restart_after_open_failure_succeeds():
    # First start fails to open, then swap to a good stream and start again.
    engine_mod.sd = _fake_sd(BoomStream)
    e = Engine(on_level=lambda v: None, on_status=lambda k, p: None)
    assert e.start(_cfg()) is False
    time.sleep(0.2)
    assert not e.running

    engine_mod.sd = _fake_sd(OkStream)
    assert e.start(_cfg()) is True, "restart after open failure failed"
    time.sleep(0.3)
    assert e.running, "engine did not actually start on retry"
    e.stop()
    assert not e.running
    print("PASS test_restart_after_open_failure_succeeds")


def test_double_start_keeps_single_worker():
    engine_mod.sd = _fake_sd(OkStream)
    e = Engine(on_level=lambda v: None, on_status=lambda k, p: None)
    assert e.start(_cfg())
    t1 = e._thread
    assert e.start(_cfg()) is True  # second call is a no-op
    t2 = e._thread
    assert t1 is t2, "double start created a second worker"
    assert sum(1 for t in threading.enumerate() if t.name == "ducking") == 1
    e.stop()
    print("PASS test_double_start_keeps_single_worker")


def test_stop_idempotent_and_releases():
    engine_mod.sd = _fake_sd(OkStream)
    e = Engine(on_level=lambda v: None, on_status=lambda k, p: None)
    e.start(_cfg())
    time.sleep(0.2)
    e.stop()
    e.stop()  # second stop must be harmless
    assert not e.running
    assert e._thread is None
    assert sum(1 for t in threading.enumerate() if t.name == "ducking") == 0
    print("PASS test_stop_idempotent_and_releases")


def test_start_stop_cycles_no_leak():
    engine_mod.sd = _fake_sd(OkStream)
    e = Engine(on_level=lambda v: None, on_status=lambda k, p: None)
    for _ in range(5):
        assert e.start(_cfg())
        time.sleep(0.1)
        e.stop()
    assert not e.running
    assert sum(1 for t in threading.enumerate() if t.name == "ducking") == 0
    print("PASS test_start_stop_cycles_no_leak")


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)