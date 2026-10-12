"""Small reusable widgets."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QToolButton,
                               QVBoxLayout, QWidget)

from ..core.timefmt import format_time, parse_time
from . import icons, theme


def repolish(w: QWidget) -> None:
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


def make_button(text: str = "", variant: Optional[str] = None, icon: Optional[str] = None,
                tip: str = "") -> QPushButton:
    b = QPushButton(text)
    if variant:
        b.setProperty("variant", variant)
    if icon:
        icons.bind(b, icon, 16, role="on_accent" if variant == "primary" else None)
    if tip:
        b.setToolTip(tip)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def icon_button(name: str, tip: str = "", *, size: int = 18, checkable: bool = False,
                role: Optional[str] = None, text: str = "") -> QToolButton:
    b = QToolButton()
    icons.bind(b, name, size)
    b.setIconSize(QSize(size, size))
    if text:
        b.setText(text)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    if tip:
        b.setToolTip(tip)
    b.setCheckable(checkable)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setAutoRaise(True)
    if role:
        b.setProperty("role", role)
    return b


def shrinkable(combo):
    """Let a combo box narrow below the width of its longest item (long items are elided)."""
    from PySide6.QtWidgets import QComboBox
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(8)
    return combo


def section_label(text: str) -> QLabel:
    lab = QLabel(text.upper())
    lab.setProperty("role", "section")
    return lab


def role_label(text: str, role: str) -> QLabel:
    lab = QLabel(text)
    lab.setProperty("role", role)
    return lab


def divider(vertical: bool = False) -> QFrame:
    f = QFrame()
    f.setProperty("role", "vdivider" if vertical else "divider")
    f.setFixedWidth(1) if vertical else f.setFixedHeight(1)
    return f


class Card(QFrame):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)


class ElidedLabel(QLabel):
    def __init__(self, text: str = "", parent: Optional[QWidget] = None,
                 mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight) -> None:
        super().__init__(parent)
        self._full = text
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        self._full = text
        self._refit()
        self.setToolTip(text)

    def text(self) -> str:
        return self._full

    def _refit(self) -> None:
        fm = QFontMetrics(self.font())
        super().setText(fm.elidedText(self._full, self._mode, max(10, self.width() - 2)))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refit()

    def minimumSizeHint(self) -> QSize:
        return QSize(20, super().minimumSizeHint().height())


class TimeEdit(QLineEdit):
    """A time field (hh:mm:ss.mmm) that accepts typed seconds, mm:ss or hh:mm:ss.fff."""

    valueEdited = Signal(float)

    def __init__(self, parent: Optional[QWidget] = None, *, short: bool = False) -> None:
        super().__init__(parent)
        self.setProperty("role", "time")
        self.setFont(theme.mono_font(9.5))
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._value = 0.0
        self._short = short
        self.editingFinished.connect(self._commit)
        self.setValue(0.0)

    def value(self) -> float:
        return self._value

    def setValue(self, seconds: float) -> None:  # noqa: N802
        self._value = max(0.0, seconds)
        self.setText(format_time(self._value, short=self._short))

    def _commit(self) -> None:
        parsed = parse_time(self.text())
        if parsed is None:
            self.setText(format_time(self._value, short=self._short))
            return
        self._value = parsed
        self.setText(format_time(parsed, short=self._short))
        self.valueEdited.emit(parsed)


class LevelMeter(QWidget):
    """Stereo peak meter with a fast attack and a smooth fall."""

    FLOOR_DB = -60.0

    def __init__(self, parent: Optional[QWidget] = None, horizontal: bool = True) -> None:
        super().__init__(parent)
        self._levels = [0.0, 0.0]
        self._hold = [0.0, 0.0]
        self._horizontal = horizontal
        self.setMinimumSize(80, 18) if horizontal else self.setMinimumSize(18, 80)

    def feed(self, left: float, right: float, decay: float = 0.82) -> None:
        for i, v in enumerate((left, right)):
            self._levels[i] = max(v, self._levels[i] * decay)
            self._hold[i] = max(v, self._hold[i] * 0.985)
        self.update()

    def reset(self) -> None:
        self._levels = [0.0, 0.0]
        self._hold = [0.0, 0.0]
        self.update()

    @staticmethod
    def _frac(level: float) -> float:
        if level <= 0:
            return 0.0
        import math
        db = 20 * math.log10(level)
        return max(0.0, min(1.0, (db - LevelMeter.FLOOR_DB) / -LevelMeter.FLOOR_DB))

    def paintEvent(self, event) -> None:
        t = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        gap = 3
        bar_h = (h - gap) / 2 if self._horizontal else h
        for i in range(2):
            if self._horizontal:
                rect = QRectF(0, i * (bar_h + gap), w, bar_h)
            else:
                bar_w = (w - gap) / 2
                rect = QRectF(i * (bar_w + gap), 0, bar_w, h)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.qcolor(t.bg2))
            p.drawRoundedRect(rect, 3, 3)
            frac = self._frac(self._levels[i])
            if frac > 0:
                if self._horizontal:
                    fill = QRectF(rect.left(), rect.top(), rect.width() * frac, rect.height())
                    grad = QLinearGradient(rect.left(), 0, rect.right(), 0)
                else:
                    fill = QRectF(rect.left(), rect.bottom() - rect.height() * frac, rect.width(), rect.height() * frac)
                    grad = QLinearGradient(0, rect.bottom(), 0, rect.top())
                grad.setColorAt(0.0, theme.qcolor(t.good))
                grad.setColorAt(0.72, theme.qcolor(t.good))
                grad.setColorAt(0.86, theme.qcolor(t.warn))
                grad.setColorAt(1.0, theme.qcolor(t.bad))
                p.setBrush(grad)
                p.drawRoundedRect(fill, 3, 3)
            hold = self._frac(self._hold[i])
            if hold > 0.02:
                p.setBrush(theme.qcolor(t.text_dim))
                if self._horizontal:
                    p.drawRect(QRectF(rect.left() + rect.width() * hold - 1.5, rect.top(), 1.5, rect.height()))
                else:
                    p.drawRect(QRectF(rect.left(), rect.bottom() - rect.height() * hold, rect.width(), 1.5))


class Chip(QPushButton):
    """A selectable pill, used for format and mode choices."""

    def __init__(self, text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setProperty("variant", "chip")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)


def hbox(*widgets, spacing: int = 8, margins=(0, 0, 0, 0), stretch_last: bool = False) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    for w in widgets:
        if w is None:
            lay.addStretch(1)
        elif isinstance(w, int):
            lay.addSpacing(w)
        else:
            lay.addWidget(w)
    return lay


def vbox(*widgets, spacing: int = 8, margins=(0, 0, 0, 0)) -> QVBoxLayout:
    lay = QVBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    for w in widgets:
        if w is None:
            lay.addStretch(1)
        elif isinstance(w, int):
            lay.addSpacing(w)
        else:
            lay.addWidget(w)
    return lay
