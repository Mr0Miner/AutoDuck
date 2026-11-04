"""End-to-end engine test with a synthetic microphone.

This machine may have no usable capture device, so the InputStream is replaced
with a fake that pushes a scripted loudness profile: silence -> loud -> silence.
It drives the real Engine control loop and the real WASAPI session control,
targeting a running tone player, and asserts the target is ducked then restored
while other sessions stay untouched.
"""

import sys
import os
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import comtypes  # noqa: E402
from pycaw.pycaw import AudioUtilities  # noqa: E402

import autoduck.engine as engine_mod  # noqa: E402
from autoduck.ducking import AUTO, FIXED  # noqa: E402
from autoduck.engine import Engine, EngineConfig  # noqa: E402
from autoduck.sessions import enumerate_sessions  # noqa: E402


class FakeStream:
    """Feeds a scripted RMS profile at ~100 blocks/sec into the callback."""

    def __init__(self, *args, script=None, **kwargs):
        self._cb = kwargs["callback"]
        self._script = script
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop = True

    def close(self):
        self._stop = True

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        self._thread.join(timeout=1.0)
        return False

    def _run(self):
        for level, dur in self._script:
            end = time.monotonic() + dur
            while time.monotonic() < end and not self._stop:
                data = np.full((256, 1), level, dtype=np.float32)
                self._cb(data, 256, None, None)
                time.sleep(0.01)


def _vol(iid):
    for s in AudioUtilities.GetAllSessions():
        try:
            if str(s.InstanceIdentifier) == iid:
                return s.SimpleAudioVolume.GetMasterVolume()
        except Exception:
            pass
    return None


def _other_volumes(iid):
    out = {}
    for s in AudioUtilities.GetAllSessions():
        try:
            k = str(s.InstanceIdentifier)
            if k != iid:
                out[k] = s.SimpleAudioVolume.GetMasterVolume()
        except Exception:
            pass
    return out


def run_mode(mode, expect_ratio, target):
    comtypes.CoInitialize()
    try:
        others_before = _other_volumes(target.instance_id)
        orig = _vol(target.instance_id)
        assert orig is not None, "target session vanished"

        # silence -> loud -> long silence (longer than hold + release)
        script = [(0.0, 0.5), (0.35, 1.5), (0.0, 5.0)]

        class FakeSD:
            InputStream = staticmethod(lambda *a, **k: FakeStream(*a, script=script, **k))

            @staticmethod
            def check_input_settings(**kwargs):
                return None

        engine_mod.sd = FakeSD

        levels = []
        statuses = []
        eng = Engine(on_level=levels.append, on_status=lambda k, p: statuses.append(k))
        cfg = EngineConfig(
            mic_index=0,
            target_ids=(target.instance_id,),
            target_by_exe=False,
            mode=mode,
            sensitivity=70.0,
            max_reduction=0.8,
            fixed_reduction=0.5,
            hold_ms=1000.0,
        )
        assert eng.start(cfg), "engine failed to start"
        time.sleep(1.6)          # during the loud phase
        ducked = _vol(target.instance_id)
        time.sleep(3.2)          # past hold (1s) + release time
        restored = _vol(target.instance_id)
        eng.stop()

        others_after = _other_volumes(target.instance_id)
        print(f"  original={orig} ducked={ducked} restored={restored}")
        assert ducked is not None, "target session vanished mid-test (tone too short?)"
        assert restored is not None, "target session vanished mid-test (tone too short?)"
        assert ducked < orig - 1e-2, f"no duck in {mode}"
        assert abs(restored - orig) < 1e-2, f"restore failed in {mode}: {restored} vs {orig}"
        ratio = ducked / orig
        assert abs(ratio - expect_ratio) < 0.25, f"{mode} ratio {ratio:.2f} vs {expect_ratio}"
        assert others_before == others_after, "other sessions changed!"
        assert levels, "no level callbacks"
        return True
    finally:
        comtypes.CoUninitialize()


def main():
    entries = enumerate_sessions()
    if not entries:
        print("SKIP: start a tone player first (no active session)")
        return 0
    target = entries[0]
    print("target:", target.exe, "|", target.title)
    print("FIXED mode:")
    run_mode(FIXED, 0.5, target)
    print("FIXED mode PASSED")
    print("AUTO mode:")
    run_mode(AUTO, 0.4, target)
    print("AUTO mode PASSED")
    print("ENGINE E2E PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())