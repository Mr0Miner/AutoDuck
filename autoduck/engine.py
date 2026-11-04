"""Runtime engine: captures the microphone and drives the ducking loop.

Thread model
------------
* PortAudio thread  - ``_audio_cb`` only computes an RMS level (no COM).
* worker thread      - runs the ~60 Hz control loop, updates the
  :class:`DuckingController`, writes per-session volume through COM, and is the
  *single owner* of the ``_running`` lifecycle.
* UI thread          - receives status/level through Qt signals; it only calls
  ``start``/``stop``/``update``, which never block on COM.

Lifecycle invariants
--------------------
* At most one worker thread ever exists.
* The worker clears ``_running`` itself in ``finally`` (and on any startup
  failure) so the UI can never observe "running" while no worker is alive.
* ``stop`` joins the worker and is idempotent.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import comtypes
import numpy as np
import sounddevice as sd

from .ducking import DuckingController, rms_to_dbfs
from .sessions import SessionController


@dataclass
class EngineConfig:
    mic_index: int
    target_ids: tuple[str, ...]   # session instance ids selected by the user
    target_by_exe: bool           # True: duck every session of the exe(s)
    mode: str
    sensitivity: float
    max_reduction: float
    fixed_reduction: float
    hold_ms: float = 2000.0       # stay ducked this long after the input goes quiet


class MicMonitor:
    """Lightweight microphone preview used by the in-app "test mic" button."""

    def __init__(self, on_level: Callable[[float], None],
                 on_error: Callable[[str], None]) -> None:
        self._on_level = on_level
        self._on_error = on_error
        self._stream: Optional[sd.InputStream] = None
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def start(self, mic_index: int) -> bool:
        if self._active:
            return True
        try:
            sd.check_input_settings(device=mic_index, channels=1, dtype="float32")
        except Exception as exc:
            self._on_error(f"Microphone unavailable: {exc}")
            return False
        try:
            self._stream = sd.InputStream(
                device=mic_index, channels=1, dtype="float32", callback=self._cb
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            self._on_error(f"Microphone test failed to start: {exc}")
            return False
        self._active = True
        return True

    def stop(self) -> None:
        self._active = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _cb(self, indata, frames, time_info, status) -> None:
        block = indata[:, 0] if indata.ndim > 1 else indata
        rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64)))) if block.size else 0.0
        self._on_level(rms_to_dbfs(rms))


class Engine:
    """Owns the microphone stream and the single ducking worker thread."""

    def __init__(
        self,
        on_level: Callable[[float], None],
        on_status: Callable[[str, dict], None],
    ) -> None:
        self._on_level = on_level
        self._on_status = on_status

        self._ctrl = DuckingController()
        self._sessions = SessionController()

        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._stopping = threading.Event()
        self._cfg: Optional[EngineConfig] = None

        self._level_db = -120.0
        self._last_cb = 0.0
        self._lock = threading.Lock()

        # Startup handshake: the worker signals whether the stream opened.
        self._open_event = threading.Event()
        self._open_ok = False
        self._error_emitted = False

    # ---- lifecycle ------------------------------------------------------
    @property
    def running(self) -> bool:
        """True only while a live worker thread is actually processing."""
        return self._running.is_set() and self._thread is not None and self._thread.is_alive()

    def start(self, cfg: EngineConfig) -> bool:
        """Start ducking. Returns False (never throws) if it could not start."""
        # Reap a worker that died on its own so a restarted instance is clean.
        if self._thread is not None and not self._thread.is_alive():
            self._thread = None
            self._running.clear()
        if self.running:
            return True  # already running; do not spawn a second worker

        try:
            sd.check_input_settings(device=cfg.mic_index, channels=1, dtype="float32")
        except Exception as exc:
            self._emit("error", {"message": f"Microphone unavailable: {exc}"})
            return False

        self._cfg = cfg
        self._apply_ctrl(cfg)
        self._ctrl.reset()
        self._sessions.set_targets(cfg.target_ids, by_exe=cfg.target_by_exe)

        with self._lock:
            self._level_db = -120.0
            self._last_cb = time.monotonic()

        self._open_event.clear()
        self._open_ok = False
        self._stopping.clear()
        self._running.set()
        self._thread = threading.Thread(target=self._worker, name="ducking", daemon=True)
        self._thread.start()

        # Wait for the worker to report that the audio stream actually opened,
        # so a failure surfaces here (and the button never falsely turns active).
        self._open_event.wait(timeout=2.5)
        if not self._open_ok:
            self.stop()
            if not self._error_emitted:
                self._emit("error", {"message": "Failed to start microphone capture."})
            return False

        self._error_emitted = False
        self._emit("running", {})
        return True

    def stop(self) -> None:
        """Stop ducking and release every audio resource. Safe to call twice."""
        if self._thread is None and not self._running.is_set():
            return
        self._stopping.set()
        self._running.clear()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        self._thread = None
        # Belt-and-braces: the worker restores on its way out, but make sure the
        # sessions are back to their original volume even if the worker died.
        try:
            self._sessions.restore()
        except Exception:
            pass
        self._emit("stopped", {})

    def update(self, cfg: EngineConfig) -> None:
        """Apply live parameter changes without restarting capture."""
        self._cfg = cfg
        self._apply_ctrl(cfg)
        if self._sessions.targets != tuple(sorted(cfg.target_ids or ())) or \
                self._sessions.by_exe != cfg.target_by_exe:
            self._sessions.restore()
            self._sessions.set_targets(cfg.target_ids, by_exe=cfg.target_by_exe)

    @staticmethod
    def _apply_ctrl_to(ctrl: DuckingController, cfg: EngineConfig) -> None:
        ctrl.mode = cfg.mode
        ctrl.sensitivity = cfg.sensitivity
        ctrl.max_reduction = cfg.max_reduction
        ctrl.fixed_reduction = cfg.fixed_reduction
        ctrl.hold_ms = cfg.hold_ms

    def _apply_ctrl(self, cfg: EngineConfig) -> None:
        self._apply_ctrl_to(self._ctrl, cfg)

    # ---- audio ----------------------------------------------------------
    def _audio_cb(self, indata, frames, time_info, status) -> None:
        now = time.monotonic()
        block = indata[:, 0] if indata.ndim > 1 else indata
        rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64)))) if block.size else 0.0
        with self._lock:
            self._last_cb = now
            self._level_db = rms_to_dbfs(rms)

    def _worker(self) -> None:
        comtypes.CoInitialize()
        opened = False
        try:
            cfg = self._cfg
            if cfg is None:
                return
            interval = 1.0 / 60.0
            last = time.monotonic()
            device_lost_reported = False
            target_lost_reported = False
            last_level_emit = 0.0

            stream = sd.InputStream(
                device=cfg.mic_index,
                channels=1,
                dtype="float32",
                samplerate=None,
                blocksize=0,
                callback=self._audio_cb,
            )
            stream.start()
            opened = True
            self._open_ok = True
            self._open_event.set()

            while self._running.is_set():
                now = time.monotonic()
                dt = max(now - last, interval)
                last = now

                with self._lock:
                    level = self._level_db
                    last_cb = self._last_cb

                # Watchdog: PortAudio silently stalls when the device vanishes.
                if now - last_cb > 1.0:
                    if not device_lost_reported:
                        device_lost_reported = True
                        self._sessions.restore()
                        self._emit("device_lost", {})
                elif device_lost_reported:
                    device_lost_reported = False
                    self._emit("device_ok", {})

                if not device_lost_reported:
                    gain = self._ctrl.update(level, dt)
                    present = self._sessions.apply_gain(gain)
                    if not present and not target_lost_reported:
                        target_lost_reported = True
                        self._emit("target_lost", {})
                    elif present and target_lost_reported:
                        target_lost_reported = False
                        self._emit("target_ok", {})

                    if now - last_level_emit > 1.0 / 30.0:
                        last_level_emit = now
                        self._on_level(level)

                time.sleep(interval)
        except Exception as exc:
            self._error_emitted = True
            self._emit("error", {"message": f"Audio processing error: {exc}"})
        finally:
            self._open_event.set()  # unblock start() if opening failed
            try:
                self._sessions.restore()
            except Exception:
                pass
            comtypes.CoUninitialize()
            # Hand ownership of the lifecycle back to the UI *after* cleanup, so
            # `running` becomes False exactly when no worker remains.
            with self._lock:
                self._running.clear()

    def _emit(self, kind: str, payload: dict) -> None:
        try:
            self._on_status(kind, payload)
        except Exception:
            pass