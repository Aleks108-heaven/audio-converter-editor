"""Custom controls used inside effect dialogs: curve previews, the graphic EQ and noise capture."""
from __future__ import annotations

import math
from typing import Callable, Optional

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QLabel, QSlider, QVBoxLayout, QWidget)

from ..core.effects import dsp
from ..core.effects.fx_ffmpeg import GRAPHIC_BANDS
from . import theme
from .widgets import make_button
from ..i18n import tr


class Editor(QWidget):
    """Base for dialog editors. `claims` lists the parameter keys this editor owns."""

    changed = Signal()
    claims: tuple[str, ...] = ()

    def values(self) -> dict:
        return {}

    def set_values(self, values: dict) -> None:
        pass

    def on_params(self, values: dict) -> None:
        """Called with the full parameter set whenever anything changes (for preview-only editors)."""


def _frame(p: QPainter, rect: QRectF, t) -> None:
    p.setPen(QPen(theme.qcolor(t.line_strong), 1))
    p.setBrush(theme.qcolor(t.wave_bg))
    p.drawRoundedRect(rect, 8, 8)


class FadeCurveEditor(Editor):
    """Shows the gain shape of a fade as a mirrored waveform envelope."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(150)
        self._p = {"direction": "in", "shape": "slow", "start": 0.0, "end": 100.0}

    def on_params(self, values: dict) -> None:
        self._p.update({k: values[k] for k in ("direction", "shape", "start", "end") if k in values})
        self.update()

    def paintEvent(self, ev) -> None:
        t = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        _frame(p, r, t)
        inner = r.adjusted(36, 12, -14, -12)
        mid = inner.center().y()
        half = inner.height() / 2
        p.setFont(theme.ui_font(8))
        for pct in (100, 50, 0):
            for sign in (-1, 1):
                y = mid + sign * half * pct / 100
                p.setPen(QPen(theme.qcolor(t.grid), 1, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(inner.left(), y), QPointF(inner.right(), y))
            p.setPen(theme.qcolor(t.text_faint))
            p.drawText(QRectF(r.left() + 2, mid - half * pct / 100 - 8, 30, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{pct}%")
        xs = np.linspace(0.0, 1.0, 160)
        s, e = self._p["start"] / 100.0, self._p["end"] / 100.0
        gain = dsp.fade_gain(xs, self._p["shape"], self._p["direction"], s, e)
        top, bottom = QPainterPath(), QPainterPath()
        for i, (x, g) in enumerate(zip(xs, gain)):
            px = inner.left() + x * inner.width()
            (top.moveTo if i == 0 else top.lineTo)(QPointF(px, mid - g * half))
            (bottom.moveTo if i == 0 else bottom.lineTo)(QPointF(px, mid + g * half))
        fill = QPainterPath(top)
        for x, g in zip(xs[::-1], gain[::-1]):
            fill.lineTo(QPointF(inner.left() + x * inner.width(), mid + g * half))
        fill.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.with_alpha(t.accent, 0.20))
        p.drawPath(fill)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(theme.qcolor(t.accent), 2.2))
        p.drawPath(top)
        p.drawPath(bottom)


def db_axis_y(db: float, rect: QRectF, lo: float = -60.0, hi: float = 6.0) -> float:
    return rect.bottom() - (db - lo) / (hi - lo) * rect.height()


class CompressorCurveEditor(Editor):
    """Input level against output level, with the knee and make-up gain drawn in."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(190)
        self._p = {"threshold": -18.0, "ratio": 3.0, "knee": 6.0, "makeup": 3.0}

    def on_params(self, values: dict) -> None:
        self._p.update({k: values[k] for k in self._p if k in values})
        self.update()

    @staticmethod
    def transfer(x: np.ndarray, thr: float, ratio: float, knee: float) -> np.ndarray:
        y = x.copy()
        over = x > thr + knee / 2
        y[over] = thr + (x[over] - thr) / ratio
        if knee > 0:
            soft = (x >= thr - knee / 2) & (x <= thr + knee / 2)
            y[soft] = x[soft] + (1 / ratio - 1) * (x[soft] - thr + knee / 2) ** 2 / (2 * knee)
        return y

    def paintEvent(self, ev) -> None:
        t = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        _frame(p, r, t)
        inner = r.adjusted(38, 12, -14, -24)
        p.setFont(theme.ui_font(8))
        for db in range(-60, 1, 12):
            x = inner.left() + (db + 60) / 60 * inner.width()
            y = db_axis_y(db, inner, -60, 0)
            p.setPen(QPen(theme.qcolor(t.grid), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(x, inner.top()), QPointF(x, inner.bottom()))
            p.drawLine(QPointF(inner.left(), y), QPointF(inner.right(), y))
            p.setPen(theme.qcolor(t.text_faint))
            p.drawText(QRectF(x - 20, inner.bottom() + 4, 40, 14), Qt.AlignmentFlag.AlignCenter, f"{db}")
            p.drawText(QRectF(r.left() + 2, y - 8, 32, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{db}")
        p.setPen(QPen(theme.qcolor(t.line_strong), 1.2))
        p.drawLine(QPointF(inner.left(), inner.bottom()), QPointF(inner.right(), inner.top()))
        xs = np.linspace(-60, 0, 200)
        ys = self.transfer(xs, self._p["threshold"], self._p["ratio"], self._p["knee"]) + self._p["makeup"]
        path = QPainterPath()
        for i, (x, y) in enumerate(zip(xs, ys)):
            pt = QPointF(inner.left() + (x + 60) / 60 * inner.width(), db_axis_y(float(y), inner, -60, 0))
            pt.setY(min(max(pt.y(), inner.top()), inner.bottom()))
            (path.moveTo if i == 0 else path.lineTo)(pt)
        p.setPen(QPen(theme.qcolor(t.accent), 2.4))
        p.drawPath(path)
        tx = inner.left() + (self._p["threshold"] + 60) / 60 * inner.width()
        p.setPen(QPen(theme.qcolor(t.sel_edge), 1.2, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(tx, inner.top()), QPointF(tx, inner.bottom()))
        p.setPen(theme.qcolor(t.text_dim))
        p.drawText(QRectF(inner.left(), inner.bottom() + 12, inner.width(), 14), Qt.AlignmentFlag.AlignCenter, "")


class GraphicEQEditor(Editor):
    """Ten vertical band sliders under a live frequency-response curve."""

    claims = tuple(key for _, key, _ in GRAPHIC_BANDS)
    RANGE = 15.0

    class _Plot(QWidget):
        def __init__(self, owner: "GraphicEQEditor") -> None:
            super().__init__()
            self.owner = owner
            self.setMinimumHeight(110)

        def paintEvent(self, ev) -> None:
            t = theme.current()
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
            _frame(p, r, t)
            inner = r.adjusted(30, 10, -12, -10)
            p.setFont(theme.ui_font(8))
            for db in (-12, -6, 0, 6, 12):
                y = inner.center().y() - db / GraphicEQEditor.RANGE * inner.height() / 2
                p.setPen(QPen(theme.qcolor(t.line_strong if db == 0 else t.grid), 1, Qt.PenStyle.SolidLine if db == 0 else Qt.PenStyle.DashLine))
                p.drawLine(QPointF(inner.left(), y), QPointF(inner.right(), y))
                p.setPen(theme.qcolor(t.text_faint))
                p.drawText(QRectF(r.left() + 2, y - 8, 24, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{db:+d}" if db else "0")
            freqs = np.geomspace(25, 20000, 200)
            gains = self.owner.response(freqs)
            path = QPainterPath()
            for i, (f, g) in enumerate(zip(freqs, gains)):
                x = inner.left() + (math.log10(f) - math.log10(25)) / (math.log10(20000) - math.log10(25)) * inner.width()
                y = inner.center().y() - g / GraphicEQEditor.RANGE * inner.height() / 2
                (path.moveTo if i == 0 else path.lineTo)(QPointF(x, min(max(y, inner.top()), inner.bottom())))
            fill = QPainterPath(path)
            fill.lineTo(QPointF(inner.right(), inner.center().y()))
            fill.lineTo(QPointF(inner.left(), inner.center().y()))
            fill.closeSubpath()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.with_alpha(t.accent, 0.18))
            p.drawPath(fill)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(theme.qcolor(t.accent), 2.2))
            p.drawPath(path)

    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.plot = self._Plot(self)
        lay.addWidget(self.plot)
        row = QHBoxLayout()
        row.setSpacing(0)
        self.sliders: dict[str, QSlider] = {}
        self.readouts: dict[str, QLabel] = {}
        for freq, key, label in GRAPHIC_BANDS:
            col = QVBoxLayout()
            col.setSpacing(4)
            readout = QLabel("0")
            readout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            readout.setProperty("role", "dim")
            readout.setFont(theme.mono_font(8.5))
            slider = QSlider(Qt.Orientation.Vertical)
            slider.setRange(int(-self.RANGE * 2), int(self.RANGE * 2))       # half-dB steps
            slider.setValue(0)
            slider.setMinimumHeight(120)
            slider.setTickPosition(QSlider.TickPosition.NoTicks)
            slider.valueChanged.connect(self._moved)
            name = QLabel(label)
            name.setAlignment(Qt.AlignmentFlag.AlignCenter)
            name.setProperty("role", "dim")
            col.addWidget(readout)
            col.addWidget(slider, 1, Qt.AlignmentFlag.AlignHCenter)
            col.addWidget(name)
            row.addLayout(col, 1)
            self.sliders[key] = slider
            self.readouts[key] = readout
        lay.addLayout(row)

    def _moved(self) -> None:
        for key, s in self.sliders.items():
            v = s.value() / 2.0
            self.readouts[key].setText(f"{v:+.1f}".rstrip("0").rstrip(".") if v else "0")
        self.plot.update()
        self.changed.emit()

    def response(self, freqs: np.ndarray) -> np.ndarray:
        total = np.zeros_like(freqs)
        for freq, key, _ in GRAPHIC_BANDS:
            g = self.sliders[key].value() / 2.0
            x = np.log2(freqs / freq)
            total += g * np.exp(-0.5 * (x / 0.62) ** 2)
        return total

    def values(self) -> dict:
        return {key: s.value() / 2.0 for key, s in self.sliders.items()}

    def set_values(self, values: dict) -> None:
        for key, s in self.sliders.items():
            if key in values:
                s.blockSignals(True)
                s.setValue(int(round(float(values[key]) * 2)))
                s.blockSignals(False)
        self._moved_silent()

    def _moved_silent(self) -> None:
        for key, s in self.sliders.items():
            v = s.value() / 2.0
            self.readouts[key].setText(f"{v:+.1f}".rstrip("0").rstrip(".") if v else "0")
        self.plot.update()


class NoiseProfileEditor(Editor):
    """Capture a noise sample from the selection; it is remembered between dialogs.

    The dialog is modal, so the usual flow is: select a stretch of pure noise, open
    Noise Reduction, press Capture, close it, select the whole recording, and open it again.
    """

    def __init__(self, store: dict, capture: Callable[[], Optional[np.ndarray]], sr: int) -> None:
        super().__init__()
        self._store = store
        self._capture = capture
        self._sr = sr
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        row = QHBoxLayout()
        self.button = make_button(tr("Capture selection as noise sample"), icon="wand")
        self.button.clicked.connect(self._do_capture)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        row.addWidget(self.button)
        row.addWidget(self.status, 1)
        lay.addLayout(row)
        hint = QLabel(tr("Select a second or two of pure background noise (no speech or music), press Capture, "
                      "close this dialog, select the whole recording and open Noise Reduction again."))
        hint.setProperty("role", "faint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self._refresh()

    @property
    def sample(self) -> Optional[np.ndarray]:
        return self._store.get("noise")

    def _set_status(self, text: str, role: str) -> None:
        self.status.setText(text)
        self.status.setProperty("role", role)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def _refresh(self) -> None:
        if self.sample is not None:
            self._set_status(tr(f"✓ Using a {len(self.sample) / self._sr:.2f} s noise sample."), "good")
        else:
            self._set_status(tr("No noise sample captured yet."), "dim")

    def _do_capture(self) -> None:
        data = self._capture()
        if data is None or len(data) < self._sr * 0.1:
            self._set_status(tr("Select at least 0.1 s of noise in the editor first."), "warn")
            return
        self._store["noise"] = data
        self._refresh()
        self.changed.emit()
