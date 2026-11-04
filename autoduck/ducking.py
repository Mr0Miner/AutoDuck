"""Pure ducking logic: level -> target gain, with attack/release smoothing.

No Windows / audio dependencies live here so the behaviour can be unit tested
deterministically on any machine.
"""

from __future__ import annotations

import math

# Modes
AUTO = "auto"    # reduction proportional to microphone loudness
FIXED = "fixed"  # fixed percentage while the gate is open


def rms_to_dbfs(rms: float) -> float:
    """Convert a linear RMS value (0..1) to dBFS. Silence maps to about -180 dB."""
    return 20.0 * math.log10(max(float(rms), 1e-9))


def db_to_fraction(db: float, floor_db: float = -60.0) -> float:
    """Map a dBFS reading onto 0..1 for a UI meter (floor_db..0 dB)."""
    return min(max((db - floor_db) / (-floor_db), 0.0), 1.0)


class DuckingController:
    """Turns a stream of dBFS levels into a smoothed volume multiplier in [0, 1].

    * ``auto``  - the multiplier follows loudness continuously. Louder mic means
      a stronger reduction; silence means the multiplier returns to 1.0.
    * ``fixed`` - a gate opens above the threshold and applies a constant
      reduction; hysteresis keeps it from chattering around the threshold.

    Attack (reduction) and release (restoration) use independent time constants
    so the volume never jumps or oscillates.
    """

    def __init__(self) -> None:
        self.mode = AUTO
        self.sensitivity = 55.0        # 0..100, higher = triggers on quieter sound
        self.max_reduction = 0.80      # fraction 0..1 used in auto mode
        self.fixed_reduction = 0.50    # fraction 0..1 used in fixed mode
        self.attack_ms = 40.0
        self.release_ms = 320.0
        self.hysteresis_db = 6.0
        self.hold_ms = 2000.0          # stay ducked this long after the input goes quiet

        self.level_db = -120.0
        self._gain = 1.0
        self._gate = False
        self._hold_remaining = 0.0     # seconds left before release may begin

    # ---- tuning helpers -------------------------------------------------
    @property
    def threshold_db(self) -> float:
        """Gate/trigger threshold in dBFS derived from sensitivity.

        sensitivity 0   -> -20 dBFS (only loud input triggers)
        sensitivity 100 -> -55 dBFS (quiet input triggers)
        """
        return -20.0 - (float(self.sensitivity) / 100.0) * 35.0

    def reset(self) -> None:
        self._gain = 1.0
        self._gate = False
        self._hold_remaining = 0.0
        self.level_db = -120.0

    def _held_gain(self) -> float:
        """The gain to freeze at during the hold (the deepest recent duck)."""
        return self._gain

    # ---- core -----------------------------------------------------------
    def target_gain(self, level_db: float) -> float:
        thr = self.threshold_db
        if self.mode == FIXED:
            if level_db > thr:
                self._gate = True
            elif level_db < thr - self.hysteresis_db:
                self._gate = False
            return 1.0 - self.fixed_reduction if self._gate else 1.0

        # auto
        if level_db < thr:
            return 1.0
        span = -thr  # distance from threshold up to 0 dBFS
        norm = (level_db - thr) / span if span > 0 else 1.0
        norm = min(max(norm, 0.0), 1.0)
        return 1.0 - self.max_reduction * norm

    def update(self, level_db: float, dt: float) -> float:
        """Advance the envelope by ``dt`` seconds and return the new gain.

        A *hold* keeps the volume ducked for ``hold_ms`` after the input drops
        below the threshold, so short pauses between words/sentences do not make
        the background volume pump back up and down.
        """
        self.level_db = level_db
        dt = min(max(float(dt), 0.0), 0.1)
        target = self.target_gain(level_db)

        # Is the input currently "active" (should be ducked right now)?
        active = target < 1.0 - 1e-9

        if active:
            # Still speaking: recharge the hold timer so the pause that follows
            # will start from a full hold window.
            self._hold_remaining = max(self.hold_ms, 0.0) / 1000.0
        else:
            # Input has gone quiet; keep the volume down until the hold expires.
            self._hold_remaining -= dt
            if self._hold_remaining > 0.0:
                return self._gain

        # Reducing volume (target below current) is "attack"; restoring is "release".
        tau = (self.attack_ms if target < self._gain else self.release_ms) / 1000.0
        if tau <= 0.0:
            self._gain = target
        else:
            alpha = 1.0 - math.exp(-dt / tau)
            self._gain += (target - self._gain) * alpha
        return self._gain

    @property
    def holding(self) -> bool:
        """True while the volume is being held down after the input went quiet."""
        return self._hold_remaining > 0.0 and self._gain < 1.0 - 1e-9

    @property
    def gain(self) -> float:
        return self._gain

    @property
    def gate_open(self) -> bool:
        return self._gate