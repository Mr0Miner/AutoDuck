"""Per-application volume control through the Windows Audio Session API.

Uses pycaw (IAudioSessionManager2 -> IAudioSessionControl2 / ISimpleAudioVolume).
Only the sessions belonging to the selected target are touched; system-wide
(endpoint) volume and every other application are left alone.

Identity
--------
A *session* is identified by its **session instance identifier**
(``IAudioSessionControl2::GetSessionInstanceIdentifier``) — the string that
encodes the endpoint plus the owning process.  It is stable for the lifetime of
the session and is what the UI keeps as the user's selection, so a refresh never
loses the choice even though display names change.

Display names
-------------
``IAudioSessionControl2::GetDisplayName`` is the correct, honest source.  Most
applications never set it, so it is empty for them; when that happens we fall
back to the executable's *FileDescription* (the friendly product name from the
binary's version resource) and finally to a tidied process name.  We never guess
a browser tab title: Windows exposes no public per-tab audio-session API.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass

from pycaw.constants import AudioSessionState
from pycaw.pycaw import AudioUtilities

_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080

# Session states that represent something the user could still interact with.
_LIVE_STATES = {int(AudioSessionState.Active), int(AudioSessionState.Inactive)}


# ---------------------------------------------------------------------------
# Executable friendly names (FileDescription from the version resource)
# ---------------------------------------------------------------------------
_version_cache: dict[str, str] = {}


def _file_description(path: str) -> str:
    """Return the binary's FileDescription (e.g. 'Microsoft Edge'), or ''."""
    if not path:
        return ""
    if path in _version_cache:
        return _version_cache[path]
    value = ""
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
        if size:
            buf = ctypes.create_string_buffer(size)
            if ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buf):
                r = ctypes.c_void_p()
                ln = wintypes.UINT()
                if ctypes.windll.version.VerQueryValueW(
                    buf, "\\VarFileInfo\\Translation", ctypes.byref(r), ctypes.byref(ln)
                ):
                    trans = ctypes.cast(r, ctypes.POINTER(wintypes.WORD))
                    lang, cp = trans[0], trans[1]
                    sub = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\FileDescription"
                    if ctypes.windll.version.VerQueryValueW(
                        buf, sub, ctypes.byref(r), ctypes.byref(ln)
                    ):
                        # VerQueryValue reports the length in *characters*
                        # including the terminating NUL.
                        value = ctypes.wstring_at(r, ln.value).rstrip("\x00").strip()
    except Exception:
        value = ""
    _version_cache[path] = value
    return value


def _friendly_name(exe: str | None, path: str | None) -> str:
    desc = _file_description(path) if path else ""
    if desc:
        return desc
    if exe:
        base = os.path.splitext(exe)[0]
        return base.replace("_", " ").replace("-", " ").title()
    return ""


# ---------------------------------------------------------------------------
# Window titles (used only to enrich an otherwise-generic label)
# ---------------------------------------------------------------------------
def _window_titles(pids: set[int]) -> list[str]:
    """Return visible, user-facing main-window titles owned by the given PIDs."""
    if not pids:
        return []
    user32 = ctypes.windll.user32
    titles: list[str] = []
    cb_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) & _WS_EX_TOOLWINDOW:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if title and title not in titles:
            titles.append(title)
        return True

    user32.EnumWindows(cb_type(_cb), 0)
    return titles


# ---------------------------------------------------------------------------
# Session snapshot model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AudioEntry:
    """One controllable audio session, as offered to the user."""

    instance_id: str      # stable identity (GetSessionInstanceIdentifier)
    pid: int
    exe: str              # lower-case executable name
    title: str            # display label shown in the list
    state: int            # AudioSessionState value

    @property
    def active(self) -> bool:
        return self.state == int(AudioSessionState.Active)


def _session_label(session, exe: str, exe_path: str | None, title_cache: dict[int, list[str]]) -> str:
    """Build the most honest label available for a single session."""
    # 1) The session's own display name, if the app bothered to set one.
    try:
        name = session.DisplayName
    except Exception:
        name = ""
    if name and not name.startswith("@"):
        return name.strip()

    # 2) Friendly product name from the executable's version info.
    friendly = _friendly_name(exe, exe_path)

    # 3) For processes that own a single visible window, the window title is a
    #    better, more specific label (this is genuinely available from Win32).
    pid = session.ProcessId
    if pid not in title_cache:
        title_cache[pid] = _window_titles({pid})
    titles = title_cache[pid]
    if len(titles) == 1 and len(titles[0]) > len(friendly):
        return titles[0]

    return friendly or f"{exe} (PID {pid})"


def enumerate_sessions() -> list[AudioEntry]:
    """Return every live audio session on the default render endpoint.

    Sessions in the expired state and the special system-sounds session
    (PID 0) are omitted: the former cannot be adjusted and the latter is not an
    application the user can pick.
    """
    entries: dict[str, AudioEntry] = {}
    title_cache: dict[int, list[str]] = {}

    for session in AudioUtilities.GetAllSessions():
        pid = session.ProcessId
        if not pid:  # system sounds / no process
            continue
        state = int(session.State)
        if state not in _LIVE_STATES:
            continue
        try:
            exe = session.Process.name().lower() if session.Process else ""
        except Exception:
            exe = ""
        if not exe:
            continue
        try:
            iid = str(session.InstanceIdentifier)
        except Exception:
            iid = f"{exe}|{pid}"
        try:
            exe_path = session.Process.exe() if session.Process else None
        except Exception:
            exe_path = None

        label = _session_label(session, exe, exe_path, title_cache)
        entries[iid] = AudioEntry(
            instance_id=iid,
            pid=pid,
            exe=exe,
            title=label,
            state=state,
        )

    # Active sessions first, then alphabetical by label.
    return sorted(entries.values(), key=lambda e: (not e.active, e.title.lower()))


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------
class SessionController:
    """Applies a volume multiplier to a set of selected sessions.

    The target is a collection of *session instance identifiers*.  Each matched
    session's original volume is remembered the first time it is ducked so it
    can be restored exactly; sessions the user did not select are never touched.
    """

    def __init__(self) -> None:
        self._targets: tuple[str, ...] = ()
        self._by_exe: bool = False
        self._originals: dict[str, float] = {}

    # ---- target selection ----------------------------------------------
    def set_targets(self, instance_ids, by_exe: bool = False) -> None:
        """Select sessions by instance id (or by all sessions of an exe).

        Call :meth:`restore` before changing the selection so previously ducked
        sessions are returned to their original volume.
        """
        targets = tuple(sorted(instance_ids or ()))
        self._targets = targets
        self._by_exe = by_exe

    @property
    def targets(self) -> tuple[str, ...]:
        return self._targets

    @property
    def by_exe(self) -> bool:
        return self._by_exe

    # ---- matching -------------------------------------------------------
    def _matching(self) -> list:
        wanted = set(self._targets)
        if not wanted and not self._by_exe:
            return []
        wanted_exes = set()
        if self._by_exe:
            for iid in self._targets:
                exe = iid.split("|")[-1].lower() if "|" in iid else iid.lower()
                wanted_exes.add(exe)

        out = []
        for session in AudioUtilities.GetAllSessions():
            if int(session.State) == int(AudioSessionState.Expired):
                continue
            try:
                iid = str(session.InstanceIdentifier)
            except Exception:
                continue
            if iid in wanted:
                out.append((iid, session))
            elif self._by_exe:
                try:
                    exe = session.Process.name().lower() if session.Process else ""
                except Exception:
                    exe = ""
                if exe in wanted_exes:
                    out.append((iid, session))
        return out

    # ---- volume ---------------------------------------------------------
    def apply_gain(self, gain: float) -> bool:
        """Set each selected session to original_volume * gain.

        Returns True if at least one selected session is currently present.
        First-seen sessions record their current volume as the original.
        """
        gain = min(max(float(gain), 0.0), 1.0)
        matches = self._matching()
        if not matches:
            return False
        for iid, session in matches:
            vol = session.SimpleAudioVolume
            try:
                if iid not in self._originals:
                    self._originals[iid] = vol.GetMasterVolume()
                target = self._originals[iid] * gain
                if abs(vol.GetMasterVolume() - target) > 1e-4:
                    vol.SetMasterVolume(target, None)
            except Exception:
                self._originals.pop(iid, None)
        return True

    def restore(self) -> None:
        """Restore every remembered session to its original volume."""
        if not self._originals:
            return
        for iid, session in self._matching():
            if iid in self._originals:
                try:
                    session.SimpleAudioVolume.SetMasterVolume(self._originals[iid], None)
                except Exception:
                    pass
        self._originals.clear()

    def target_present(self) -> bool:
        return bool(self._matching())