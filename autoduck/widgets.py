"""Small custom widgets for the AutoDuck UI."""

from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, Property, QPropertyAnimation, QEasingCurve, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget


class LevelMeter(QWidget):
    """Horizontal input-level meter with a moving segment fill and a soft glow."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(14)
        self._level = 0.0          # 0..1 displayed level
        self._target = 0.0
        self._anim = QPropertyAnimation(self, b"level", self)
        self._anim.setDuration(90)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _get_level(self) -> float:
        return self._level

    def _set_level(self, value: float) -> None:
        self._level = max(0.0, min(1.0, float(value)))
        self.update()

    level = Property(float, _get_level, _set_level)

    def set_level(self, level: float) -> None:
        level = max(0.0, min(1.0, level))
        if abs(level - self._target) < 0.003:
            return
        self._target = level
        self._anim.stop()
        self._anim.setStartValue(self._level)
        self._anim.setEndValue(level)
        self._anim.start()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = r.height() / 2

        # Track
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#23262e"))
        p.drawRoundedRect(r, radius, radius)

        # Fill
        w = r.width() * self._level
        if w > 1:
            fill = QRectF(r.left(), r.top(), w, r.height())
            # green -> amber -> red as the level rises
            if self._level < 0.6:
                color = QColor("#3ddc97")
            elif self._level < 0.85:
                color = QColor("#f5c451")
            else:
                color = QColor("#ff5f6d")
            p.setBrush(color)
            p.drawRoundedRect(fill, radius, radius)


class ToggleSwitch(QWidget):
    """A pill toggle with an animated knob, 44x24 logical pixels."""

    toggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(44, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._checked = False
        self._pos = 0.0
        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _get_pos(self) -> float:
        return self._pos

    def _set_pos(self, value: float) -> None:
        self._pos = float(value)
        self.update()

    pos = Property(float, _get_pos, _set_pos)

    def isChecked(self) -> bool:  # noqa: N802
        return self._checked

    def setChecked(self, checked: bool) -> None:  # noqa: N802
        if checked == self._checked:
            return
        self._checked = checked
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.setChecked(not self._checked)
            self.toggled.emit(self._checked)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0, 0, self.width(), self.height())
        radius = r.height() / 2

        track_on = QColor("#3d6bff")
        track_off = QColor("#3a3f4b")
        c = QColor(
            int(track_off.red() + (track_on.red() - track_off.red()) * self._pos),
            int(track_off.green() + (track_on.green() - track_off.green()) * self._pos),
            int(track_off.blue() + (track_on.blue() - track_off.blue()) * self._pos),
        )
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawRoundedRect(r, radius, radius)

        d = self.height() - 6
        x = 3 + self._pos * (self.width() - d - 6)
        p.setBrush(QColor("#ffffff"))
        p.setPen(QPen(QColor(0, 0, 0, 40), 1))
        p.drawEllipse(QRectF(x, 3, d, d))