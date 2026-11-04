"""AutoDuck main window: dark, minimal, single screen."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QIcon, QPainter, QPixmap, QColor
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSlider,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
    QMenu,
)

import sounddevice as sd

from . import autostart
from .audio_watch import AudioWatcher
from .ducking import AUTO, FIXED, db_to_fraction
from .engine import Engine, EngineConfig, MicMonitor
from .widgets import LevelMeter, ToggleSwitch

APP_TITLE = "AutoDuck"
STYLESHEET = """
* { font-family: 'Segoe UI', 'Segoe UI Variable', sans-serif; }
QWidget#root { background: #14161b; }
QLabel#title { color: #f2f4f8; font-size: 20px; font-weight: 700; }
QLabel#subtitle { color: #7c8493; font-size: 12px; }
QLabel.section { color: #aeb6c4; font-size: 12px; font-weight: 600; padding-top: 4px; }
QLabel.value { color: #cfd6e2; font-size: 12px; }
QFrame#card { background: #1b1e25; border: 1px solid #262a33; border-radius: 14px; }
QComboBox {
    background: #22262f; color: #e6eaf1; border: 1px solid #2e333d;
    border-radius: 9px; padding: 8px 12px; min-height: 20px; font-size: 13px;
}
QComboBox:hover { border-color: #3d6bff; }
QComboBox::drop-down { border: 0; width: 26px; }
QComboBox QAbstractItemView {
    background: #1f232b; color: #e6eaf1; border: 1px solid #2e333d;
    selection-background-color: #3d6bff; outline: 0;
}
QPushButton {
    background: #22262f; color: #e6eaf1; border: 1px solid #2e333d;
    border-radius: 9px; padding: 9px 16px; font-size: 13px; font-weight: 600;
}
QPushButton:hover { border-color: #3d6bff; }
QPushButton:disabled { color: #5a6172; border-color: #262a33; }
QPushButton#primary { background: #3d6bff; border: 0; color: white; padding: 11px 18px; }
QPushButton#primary:hover { background: #4d79ff; }
QPushButton#primary[active="true"] { background: #b3384a; }
QPushButton#primary[active="true"]:hover { background: #c94558; }
QPushButton#ghost { background: transparent; padding: 6px 10px; font-weight: 500; }
QSlider::groove:horizontal { height: 4px; background: #2a2f39; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #3d6bff; border-radius: 2px; }
QSlider::handle:horizontal {
    width: 16px; height: 16px; margin: -7px 0; border-radius: 8px;
    background: #ffffff; border: 2px solid #3d6bff;
}
QSlider:disabled::sub-page:horizontal { background: #3a3f4b; }
"""


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _make_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#3d6bff"))
    p.drawRoundedRect(4, 4, 56, 56, 16, 16)
    p.setBrush(QColor("#ffffff"))
    # a simple "duck down" glyph: two bars
    p.drawRoundedRect(16, 22, 8, 20, 3, 3)
    p.drawRoundedRect(28, 30, 8, 12, 3, 3)
    p.drawRoundedRect(40, 36, 8, 6, 3, 3)
    p.end()
    return QIcon(pm)


class MainWindow(QMainWindow):
    status_signal = Signal(str, dict)
    level_signal = Signal(float)
    mic_signal = Signal(float)
    mic_error_signal = Signal(str)
    entries_signal = Signal(object)   # list[AudioEntry] from the watcher thread

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(_make_icon())
        # Fixed size: toggling start/mode must not resize or shift the window.
        self.setFixedSize(460, 672)

        self.engine = Engine(
            on_level=self.level_signal.emit,
            on_status=self.status_signal.emit,
        )
        self.status_signal.connect(self._on_status)
        self.level_signal.connect(self._on_level)
        self.mic_signal.connect(self._on_mic_level)
        self.mic_error_signal.connect(lambda msg: self._set_hint(msg, warn=True))
        self.entries_signal.connect(self._on_entries)

        self.monitor = MicMonitor(
            on_level=self.mic_signal.emit,
            on_error=self.mic_error_signal.emit,
        )

        # Watcher runs on its own COM thread and pushes the list via a signal,
        # so the UI never performs an audio enumeration on the GUI thread.
        self.watcher = AudioWatcher(on_change=self.entries_signal.emit)
        self._entries: list = []
        self._selected_id: str | None = None

        self._build_ui()
        self._build_tray()
        self._refresh_mics()
        self.watcher.start()

        # Reconcile the button with the real engine state a few times a second;
        # if a worker dies unexpectedly the UI recovers instead of sticking.
        self._reconcile = QTimer(self)
        self._reconcile.setInterval(400)
        self._reconcile.timeout.connect(self._sync_running_ui)
        self._reconcile.start()

    # ---- layout ---------------------------------------------------------
    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        self.setStyleSheet(STYLESHEET)

        outer = QVBoxLayout(root)
        outer.setContentsMargins(22, 20, 22, 20)
        outer.setSpacing(14)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel(APP_TITLE)
        title.setObjectName("title")
        subtitle = QLabel("Ducks one app automatically when you talk")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch(1)

        self.status_pill = QLabel("Inactive")
        self.status_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_pill.setFixedWidth(78)
        self._style_pill(active=False)
        header.addWidget(self.status_pill, alignment=Qt.AlignmentFlag.AlignTop)
        outer.addLayout(header)

        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 16, 16, 16)
        cl.setSpacing(10)

        # microphone
        cl.addWidget(self._section("Microphone"))
        mic_row = QHBoxLayout()
        self.mic_combo = QComboBox()
        mic_row.addWidget(self.mic_combo, 1)
        self.mic_test = QPushButton("Test")
        self.mic_test.setObjectName("ghost")
        self.mic_test.setFixedWidth(52)
        self.mic_test.setCheckable(True)
        self.mic_test.setToolTip("Preview the microphone level")
        self.mic_test.clicked.connect(self._toggle_mic_test)
        mic_row.addWidget(self.mic_test)
        self.mic_refresh = QPushButton("↻")
        self.mic_refresh.setObjectName("ghost")
        self.mic_refresh.setFixedWidth(38)
        self.mic_refresh.setToolTip("Refresh microphone list")
        self.mic_refresh.clicked.connect(self._refresh_mics)
        mic_row.addWidget(self.mic_refresh)
        cl.addLayout(mic_row)

        # level meter (shown for both preview and live detection)
        self.meter = LevelMeter()
        cl.addWidget(self.meter)

        # target app
        cl.addWidget(self._section("Target app"))
        app_row = QHBoxLayout()
        self.app_combo = QComboBox()
        app_row.addWidget(self.app_combo, 1)
        self.app_refresh = QPushButton("↻")
        self.app_refresh.setObjectName("ghost")
        self.app_refresh.setFixedWidth(38)
        self.app_refresh.setToolTip("Refresh app list")
        self.app_refresh.clicked.connect(lambda: self.watcher.refresh_now())
        app_row.addWidget(self.app_refresh)
        cl.addLayout(app_row)
        outer.addWidget(card)

        # mode card
        card2 = QFrame()
        card2.setObjectName("card")
        c2 = QVBoxLayout(card2)
        c2.setContentsMargins(16, 16, 16, 16)
        c2.setSpacing(12)

        mode_row = QHBoxLayout()
        mode_row.addWidget(self._section("Ducking mode"))
        mode_row.addStretch(1)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Automatic (proportional)", AUTO)
        self.mode_combo.addItem("Fixed (percentage)", FIXED)
        self.mode_combo.setFixedWidth(200)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self.mode_combo)
        c2.addLayout(mode_row)

        # fixed / max reduction share one fixed-height slot so switching mode
        # does not change the card height or shift the start button.
        self.mode_slot = QWidget()
        self.mode_slot.setFixedHeight(56)
        slot = QVBoxLayout(self.mode_slot)
        slot.setContentsMargins(0, 0, 0, 0)
        slot.setSpacing(0)

        self.fixed_widget = QWidget()
        fw = QVBoxLayout(self.fixed_widget)
        fw.setContentsMargins(0, 0, 0, 0)
        fw.setSpacing(6)
        self.fixed_slider, self.fixed_label = self._slider_row(fw, "Reduction amount", 0, 100, 50, "%")
        slot.addWidget(self.fixed_widget)

        self.max_widget = QWidget()
        mw = QVBoxLayout(self.max_widget)
        mw.setContentsMargins(0, 0, 0, 0)
        mw.setSpacing(6)
        self.max_slider, self.max_label = self._slider_row(mw, "Maximum reduction", 10, 100, 80, "%")
        slot.addWidget(self.max_widget)

        c2.addWidget(self.mode_slot)

        # sensitivity (both modes)
        sens_box = QWidget()
        sb = QVBoxLayout(sens_box)
        sb.setContentsMargins(0, 0, 0, 0)
        sb.setSpacing(6)
        self.sens_slider, self.sens_label = self._slider_row(sb, "Microphone sensitivity", 0, 100, 55, "")
        c2.addWidget(sens_box)

        # hold time before the volume is restored (both modes)
        hold_box = QWidget()
        hb = QVBoxLayout(hold_box)
        hb.setContentsMargins(0, 0, 0, 0)
        hb.setSpacing(6)
        self.hold_slider, self.hold_label = self._slider_row(
            hb, "Restore delay", 0, 10, 2, " s"
        )
        c2.addWidget(hold_box)

        outer.addWidget(card2)

        # start button
        self.start_btn = QPushButton("Start")
        self.start_btn.setObjectName("primary")
        self.start_btn.setProperty("active", "false")
        self.start_btn.clicked.connect(self._toggle)
        outer.addWidget(self.start_btn)

        self.hint = QLabel("")
        self.hint.setObjectName("subtitle")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setWordWrap(True)
        self.hint.setFixedHeight(34)  # reserve space so hints never push the layout
        outer.addWidget(self.hint)

        # autostart
        auto_row = QHBoxLayout()
        auto_label = QLabel("Launch at startup")
        auto_label.setProperty("class", "value")
        auto_row.addWidget(auto_label)
        auto_row.addStretch(1)
        self.autostart_toggle = ToggleSwitch()
        self.autostart_toggle.setChecked(autostart.is_enabled())
        self.autostart_toggle.toggled.connect(self._on_autostart)
        auto_row.addWidget(self.autostart_toggle)
        outer.addLayout(auto_row)

        outer.addStretch(1)
        self.app_combo.currentIndexChanged.connect(self._on_app_changed)
        self._on_mode_changed()

    def _section(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setProperty("class", "section")
        return lbl

    def _slider_row(self, layout, title, lo, hi, val, suffix):
        row = QHBoxLayout()
        name = QLabel(title)
        name.setProperty("class", "section")
        value = QLabel()
        value.setProperty("class", "value")
        value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(name)
        row.addStretch(1)
        row.addWidget(value)
        layout.addLayout(row)

        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(val)
        layout.addWidget(slider)

        def fmt(v):
            value.setText(f"{v}{suffix}")

        def on_change(v):
            fmt(v)
            self._push_config()

        slider.valueChanged.connect(on_change)
        fmt(val)
        slider._fmt = fmt  # allow external refresh
        slider._value_label = value
        return slider, value

    # ---- tray -----------------------------------------------------------
    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(_make_icon(), self)
        self.tray.setToolTip(APP_TITLE)
        menu = QMenu()
        show = QAction("Show", self)
        show.triggered.connect(self._show_from_tray)
        quit_act = QAction("Quit", self)
        quit_act.triggered.connect(self._quit)
        menu.addAction(show)
        menu.addSeparator()
        menu.addAction(quit_act)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_from_tray()

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit(self) -> None:
        self.monitor.stop()
        self.watcher.stop()
        self.engine.stop()
        self.tray.hide()
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802
        # Keep running in the tray so ducking survives window close.
        if self.tray.isVisible():
            event.ignore()
            self.hide()
            self.tray.showMessage(
                APP_TITLE, "Still running in the system tray.", QSystemTrayIcon.MessageIcon.Information, 2500
            )
        else:
            self.watcher.stop()
            self.engine.stop()
            event.accept()

    # ---- population -----------------------------------------------------
    def _refresh_mics(self) -> None:
        if self.monitor.active:
            self._set_mic_test(False)
        current = self.mic_combo.currentData()
        self.mic_combo.clear()
        try:
            devices = sd.query_devices()
        except Exception:
            devices = []
        found = False
        for idx, dev in enumerate(devices):
            if dev.get("max_input_channels", 0) > 0:
                self.mic_combo.addItem(dev["name"], idx)
                if idx == current:
                    found = True
        if self.mic_combo.count() == 0:
            self.mic_combo.addItem("No microphone found", None)
            self.start_btn.setEnabled(False)
            self._set_hint("No microphone connected.", warn=True)
        else:
            self.start_btn.setEnabled(True)
            self._set_hint("")
            if not found and current is not None:
                pass

    def _rebuild_app_combo(self) -> None:
        """Rebuild the combo from the latest entries, preserving the choice.

        Selection is keyed by session instance id (not the display label), so
        renaming a tab or the list reordering never changes the user's pick.
        """
        prev = self._selected_id
        self.app_combo.blockSignals(True)
        self.app_combo.clear()

        if not self._entries:
            self.app_combo.addItem("No app is playing audio", None)
            self.app_combo.blockSignals(False)
            self._selected_id = None
            self._update_target_hint()
            return

        for e in self._entries:
            label = _shorten(e.title, 46) + ("  ●" if e.active else "")
            self.app_combo.addItem(label, e.instance_id)
            self.app_combo.setItemData(
                self.app_combo.count() - 1, e.title, Qt.ItemDataRole.ToolTipRole
            )

        # Restore the previous pick by instance id; otherwise keep index 0.
        idx = self.app_combo.findData(prev) if prev is not None else -1
        if idx < 0:
            idx = 0
        self.app_combo.setCurrentIndex(idx)
        self._selected_id = self.app_combo.currentData()

        self.app_combo.blockSignals(False)
        self._update_target_hint()
        self._push_config()

    def _on_entries(self, entries) -> None:
        """Slot: watcher delivered a fresh list (already on the GUI thread)."""
        self._entries = list(entries)
        self._rebuild_app_combo()

    def _update_target_hint(self) -> None:
        """Warn if the currently selected session disappeared."""
        if self.engine.running and self._selected_id is not None:
            if not any(e.instance_id == self._selected_id for e in self._entries):
                self._set_hint("The selected audio session closed; pick another app.", warn=True)

    def _on_app_changed(self) -> None:
        self._selected_id = self.app_combo.currentData()
        self._push_config()

    # ---- config ---------------------------------------------------------
    def _mode(self) -> str:
        return self.mode_combo.currentData() or AUTO

    def _cfg(self) -> EngineConfig:
        sid = self.app_combo.currentData()
        return EngineConfig(
            mic_index=self.mic_combo.currentData(),
            target_ids=(sid,) if sid else (),
            target_by_exe=False,
            mode=self._mode(),
            sensitivity=float(self.sens_slider.value()),
            max_reduction=self.max_slider.value() / 100.0,
            fixed_reduction=self.fixed_slider.value() / 100.0,
            hold_ms=float(self.hold_slider.value()) * 1000.0,
        )

    def _push_config(self) -> None:
        if self.engine.running:
            self.engine.update(self._cfg())

    def _on_autostart(self, enabled: bool) -> None:
        try:
            autostart.set_enabled(enabled)
        except OSError as exc:
            self._set_hint(f"Could not change startup setting: {exc}", warn=True)

    def _on_mode_changed(self) -> None:
        is_auto = self._mode() == AUTO
        self.max_widget.setVisible(is_auto)
        self.fixed_widget.setVisible(not is_auto)
        self._push_config()

    # ---- run / stop -----------------------------------------------------
    def _toggle(self) -> None:
        if self.engine.running:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        cfg = self._cfg()
        if self.monitor.active:
            self._set_mic_test(False)  # release the device for the live engine
        if cfg.mic_index is None:
            self._set_hint("Select a microphone first.", warn=True)
            return
        if not cfg.target_ids:
            self._set_hint("Select a target app first.", warn=True)
            return
        if self.engine.start(cfg):
            self._apply_running_ui(True)
            self._set_hint("")

    def _stop(self) -> None:
        self.engine.stop()
        self._apply_running_ui(False)
        self.meter.set_level(0.0)

    def _apply_running_ui(self, running: bool) -> None:
        """Single place that synchronises the button/pill with engine state."""
        self.start_btn.setText("Stop" if running else "Start")
        self.start_btn.setProperty("active", "true" if running else "false")
        self._repolish(self.start_btn)
        self._set_controls_enabled(not running)
        self._style_pill(active=running)
        self.status_pill.setText("Active" if running else "Inactive")

    def _sync_running_ui(self) -> None:
        """Reconcile the UI with the engine's *actual* state.

        Guards against the button claiming "Active" while no worker is alive,
        which is how the start button used to wedge the app.
        """
        if not self.engine.running and self.start_btn.property("active") == "true":
            self._apply_running_ui(False)
            self.meter.set_level(0.0)

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.mic_combo.setEnabled(enabled)
        self.app_combo.setEnabled(enabled)
        self.mic_refresh.setEnabled(enabled)
        self.app_refresh.setEnabled(enabled)
        self.mode_combo.setEnabled(enabled)
        # Live engine owns the device; stop preview and lock the test button.
        self.mic_test.setEnabled(enabled)
        if not enabled and self.monitor.active:
            self._set_mic_test(False)

    # ---- microphone preview --------------------------------------------
    def _toggle_mic_test(self) -> None:
        self._set_mic_test(not self.monitor.active)

    def _set_mic_test(self, on: bool) -> None:
        if on:
            idx = self.mic_combo.currentData()
            if idx is None:
                self.mic_test.setChecked(False)
                self._set_hint("Select a microphone first.", warn=True)
                return
            if self.monitor.start(idx):
                self.mic_test.setChecked(True)
                self.mic_test.setText("Stop")
                self._set_hint("Testing microphone — speak to fill the meter.")
        else:
            self.monitor.stop()
            self.mic_test.setChecked(False)
            self.mic_test.setText("Test")
            self.meter.set_level(0.0)
            self._set_hint("")

    def _on_mic_level(self, level_db: float) -> None:
        self.meter.set_level(db_to_fraction(level_db))

    # ---- signals from engine -------------------------------------------
    def _on_level(self, level_db: float) -> None:
        self.meter.set_level(db_to_fraction(level_db))

    def _on_status(self, kind: str, payload: dict) -> None:
        if kind == "error":
            self._set_hint(payload.get("message", "Unknown error"), warn=True)
            # The worker reports the error and then exits; make sure the UI
            # reflects the stopped state even if `running` already flipped.
            if self.start_btn.property("active") == "true":
                self.engine.stop()
                self._apply_running_ui(False)
        elif kind == "device_lost":
            self._set_hint("Microphone disconnected. Waiting to reconnect…", warn=True)
        elif kind == "device_ok":
            self._set_hint("")
        elif kind == "target_lost":
            self._set_hint("Target app is not playing audio or has closed.", warn=True)
        elif kind == "target_ok":
            self._set_hint("")

    # ---- helpers --------------------------------------------------------
    def _set_hint(self, text: str, warn: bool = False) -> None:
        self.hint.setText(text)
        self.hint.setStyleSheet("color: #ff8a95;" if warn else "color: #7c8493;")

    def _style_pill(self, active: bool) -> None:
        if active:
            self.status_pill.setStyleSheet(
                "background: rgba(61,220,151,0.14); color:#3ddc97; border-radius:11px;"
                "padding:4px 8px; font-size:12px; font-weight:600;"
            )
        else:
            self.status_pill.setStyleSheet(
                "background: rgba(124,132,147,0.14); color:#9aa3b2; border-radius:11px;"
                "padding:4px 8px; font-size:12px; font-weight:600;"
            )

    @staticmethod
    def _repolish(widget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)