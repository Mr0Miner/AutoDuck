"""Live tracking of audio sessions for the app list.

Windows' manager-level ``IAudioSessionNotification.OnSessionCreated`` does not
reliably deliver callbacks to a Python COM object (verified empirically), so the
watcher uses a hybrid strategy that is still cheap:

* **Event-driven (primary):** every known session gets an ``IAudioSessionEvents``
  sink.  Windows calls ``OnStateChanged`` (play/pause/stop) and
  ``OnDisplayNameChanged`` and ``OnSessionDisconnected`` immediately, so state
  and name updates are real-time and cost nothing while idle.
* **Light snapshot diff (secondary):** every few seconds a full enumeration is
  diffed against the last result to notice *new* sessions and expirations.  It
  runs on a private thread and only fires a debounced callback when the set
  actually changed, so it never thrashes the UI.

The watcher lives on its own thread with its own COM apartment and reports
changes through a single callback; the UI marshals that onto the GUI thread.
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional

import comtypes
from comtypes import COMObject
from pycaw.pycaw import AudioUtilities, IAudioSessionEvents

from .sessions import AudioEntry, enumerate_sessions


class _SessionSink(COMObject):
    """Per-session COM sink: forwards name/state/disconnect events."""

    _com_interfaces_ = [IAudioSessionEvents]

    def __init__(self, on_change: Callable[[], None]) -> None:
        super().__init__()
        self._on_change = on_change

    def OnDisplayNameChanged(self, NewDisplayName, EventContext):  # noqa: N803
        self._on_change()
        return 0

    def OnIconPathChanged(self, NewIconPath, EventContext):  # noqa: N803
        self._on_change()
        return 0

    def OnSimpleVolumeChanged(self, NewVolume, NewMute, EventContext):  # noqa: N803
        return 0

    def OnChannelVolumeChanged(self, ChannelCount, NewChannelVolumeArray, ChangedChannel, EventContext):  # noqa: N803
        return 0

    def OnGroupingParamChanged(self, NewGroupingParam, EventContext):  # noqa: N803
        self._on_change()
        return 0

    def OnStateChanged(self, NewState):  # noqa: N803
        self._on_change()
        return 0

    def OnSessionDisconnected(self, DisconnectReason):  # noqa: N803
        self._on_change()
        return 0


class AudioWatcher:
    """Keeps a live list of :class:`AudioEntry` and reports changes."""

    def __init__(
        self,
        on_change: Callable[[list[AudioEntry]], None],
        poll_interval: float = 2.0,
    ) -> None:
        self._on_change = on_change
        self._poll_interval = poll_interval

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._dirty = threading.Event()
        self._entries: list[AudioEntry] = []

        # Per-thread COM bookkeeping (only touched on the watcher thread).
        self._sinks: dict[str, _SessionSink] = {}
        self._sink_keys: set[str] = set()

    # ---- public API -----------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="audio-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None
        # COM sinks are released when the apartment uninitialises on the thread.

    def refresh_now(self) -> None:
        """Request an out-of-band refresh (called from the UI thread)."""
        self._dirty.set()

    @property
    def entries(self) -> list[AudioEntry]:
        return list(self._entries)

    # ---- internals ------------------------------------------------------
    def _mark_dirty(self) -> None:
        self._dirty.set()

    def _run(self) -> None:
        # STA apartment + a message pump are required for the COM event sinks to
        # receive callbacks.
        comtypes.CoInitialize()
        user32 = ctypes.windll.user32
        msg = wintypes.MSG()
        try:
            self._poll_once(force=True)
            last_poll = time.monotonic()
            while not self._stop.is_set():
                # Pump pending COM/Win32 messages so IAudioSessionEvents fires.
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))

                now = time.monotonic()
                if self._dirty.is_set() or (now - last_poll) >= self._poll_interval:
                    self._dirty.clear()
                    self._poll_once(force=False)
                    last_poll = now
                time.sleep(0.02)
        except Exception:
            pass
        finally:
            self._release_sinks()
            comtypes.CoUninitialize()

    def _release_sinks(self) -> None:
        # Drop all references; COM objects release on GC within this apartment.
        self._sinks.clear()
        self._sink_keys.clear()

    def _sync_sinks(self, entry_sessions: dict[str, object]) -> bool:
        """Attach sinks to new sessions, drop sinks for gone ones.

        Returns True if the set of sessions changed (attachment is bookkeeping,
        not a user-visible change).
        """
        EPSILON = 1e-9  # noqa: N806
        changed = False

        current = set(entry_sessions)
        gone = self._sink_keys - current
        for iid in gone:
            self._sinks.pop(iid, None)
            changed = True
        self._sink_keys &= current

        for iid, session in entry_sessions.items():
            if iid in self._sinks:
                continue
            try:
                sink = _SessionSink(self._mark_dirty)
                session.register_notification(sink)
                self._sinks[iid] = sink
                self._sink_keys.add(iid)
                changed = True
            except Exception:
                continue
        return changed

    def _poll_once(self, force: bool) -> None:
        # Enumerate sessions and their raw COM objects in one pass.
        raw: dict[str, object] = {}
        for session in AudioUtilities.GetAllSessions():
            try:
                pid = session.ProcessId
                if not pid:
                    continue
                iid = str(session.InstanceIdentifier)
            except Exception:
                continue
            raw[iid] = session

        self._sync_sinks(raw)

        entries = enumerate_sessions()
        if force or [e.instance_id for e in entries] != [e.instance_id for e in self._entries] \
                or [(e.title, e.state) for e in entries] != [(e.title, e.state) for e in self._entries]:
            self._entries = entries
            try:
                self._on_change(entries)
            except Exception:
                pass