"""Live test of per-session volume control against real Windows sessions.

Requires at least one application with an active audio session (start a tone
player first).  It verifies:
  1. sessions are enumerated,
  2. a target session is selected by instance id,
  3. ducking changes ONLY that session,
  4. restore returns it to its exact original volume.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import comtypes  # noqa: E402
from pycaw.pycaw import AudioUtilities  # noqa: E402

from autoduck.sessions import SessionController, enumerate_sessions  # noqa: E402


def _volumes():
    out = {}
    for s in AudioUtilities.GetAllSessions():
        try:
            out[str(s.InstanceIdentifier)] = s.SimpleAudioVolume.GetMasterVolume()
        except Exception:
            pass
    return out


def main() -> int:
    comtypes.CoInitialize()
    try:
        entries = enumerate_sessions()
        print("entries:", [(e.exe, e.title, e.state) for e in entries])
        if not entries:
            print("SKIP: no live audio session (start a tone player)")
            return 0

        target = entries[0]
        print("target:", target.exe, "|", target.title)

        before = _volumes()
        ctrl = SessionController()
        ctrl.set_targets([target.instance_id])

        assert ctrl.apply_gain(0.4), "apply_gain matched nothing"
        mid = _volumes()
        for iid, vol in mid.items():
            orig = before.get(iid)
            if orig is None:
                continue
            if iid == target.instance_id:
                assert abs(vol - orig * 0.4) < 1e-3, f"target not ducked: {vol} vs {orig*0.4}"
            else:
                assert abs(vol - orig) < 1e-4, f"UNTOUCHED VIOLATION {iid}: {orig}->{vol}"
        print("duck applied to target only; others untouched OK")

        ctrl.restore()
        after = _volumes()
        for iid, vol in after.items():
            if iid in before:
                assert abs(vol - before[iid]) < 1e-3, f"restore failed {iid}: {vol}"
        print("restore OK")
        print("LIVE SESSION TEST PASSED")
        return 0
    finally:
        comtypes.CoUninitialize()


if __name__ == "__main__":
    raise SystemExit(main())