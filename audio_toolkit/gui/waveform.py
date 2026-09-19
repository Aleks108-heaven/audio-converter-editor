"""The persistent waveform workspace: ruler + waveform + transport controls,
docked above every tool panel so it's always the visual center of the app —
the one thing every tool works with or around."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .. import core
from . import theme
from .state import AppState
from .widgets import format_duration

RULER_H = 20
WAVE_MIN_H = 130


def _nice_tick_interval_ms(duration_ms: int, width_px: int) -> int:
    """Pick a round tick interval (in ms) so ticks land roughly every ~80px."""
    if duration_ms <= 0 or width_px <= 0:
        return 1000
    target_px = 80
    target_ticks = max(1, width_px // target_px)
    raw_s = (duration_ms / 1000) / target_ticks
    steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600]
    for step in steps:
        if raw_s <= step:
            return step * 1000
    return steps[-1] * 1000


class WaveformWidget(QWidget):
    """Supports drag-to-select (Trim/Cut) or click-to-add-markers (Split) via
    set_mode(); an empty file prompts to open one on click."""

    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state_ = state
        self.mode = "none"  # "none" | "select" | "markers"
        self.on_selection_change = None
        self.on_markers_change = None
        self.on_empty_click = None
        self.sel_start_ms: int | None = None
        self.sel_end_ms: int | None = None
        self.marker_positions: list[int] = []
        self.playhead_ms: int | None = None
        self._dragging = False

        self.setMinimumHeight(RULER_H + WAVE_MIN_H)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        state.file_changed.connect(self._on_file_changed)

    def _on_file_changed(self) -> None:
        self.sel_start_ms = self.sel_end_ms = None
        self.marker_positions = []
        self.playhead_ms = None
        self.setCursor(Qt.IBeamCursor if self.state_.path else Qt.PointingHandCursor)
        self.update()

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.update()

    def set_playhead(self, ms: int | None) -> None:
        self.playhead_ms = ms
        self.update()

    def set_selection(self, start_ms: int | None, end_ms: int | None) -> None:
        self.sel_start_ms, self.sel_end_ms = start_ms, end_ms
        self.update()

    def clear_markers(self) -> None:
        self.marker_positions.clear()
        if self.on_markers_change:
            self.on_markers_change([])
        self.update()

    def _wave_rect(self) -> QRectF:
        return QRectF(0, RULER_H, self.width(), max(1, self.height() - RULER_H))

    def _x_to_ms(self, x: float) -> int:
        width = self.width() or 1
        frac = max(0.0, min(1.0, x / width))
        return int(frac * self.state_.duration_ms)

    def mousePressEvent(self, event) -> None:
        if not self.state_.path:
            if self.on_empty_click:
                self.on_empty_click()
            return
        if self.mode == "select":
            self._dragging = True
            self.sel_start_ms = self._x_to_ms(event.position().x())
            self.sel_end_ms = self.sel_start_ms
            self.update()

    def mouseMoveEvent(self, event) -> None:
        if self.mode == "select" and self._dragging and self.sel_start_ms is not None:
            self.sel_end_ms = self._x_to_ms(event.position().x())
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self.mode == "select" and self._dragging and self.sel_start_ms is not None:
            self._dragging = False
            lo, hi = sorted((self.sel_start_ms, self.sel_end_ms))
            self.sel_start_ms, self.sel_end_ms = lo, hi
            if self.on_selection_change:
                self.on_selection_change(lo, hi)
            self.update()
        elif self.mode == "markers" and self.state_.path:
            ms = self._x_to_ms(event.position().x())
            self.marker_positions.append(ms)
            self.marker_positions.sort()
            if self.on_markers_change:
                self.on_markers_change(list(self.marker_positions))
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        width, height = self.width(), self.height()

        painter.fillRect(0, 0, width, RULER_H, QColor(theme.WAVE_RULER_BG))
        painter.fillRect(0, RULER_H, width, height - RULER_H, QColor(theme.WAVE_BG))

        samples = self.state_.samples
        if samples is None or len(samples) == 0:
            self._paint_empty_state(painter, width, height)
            painter.end()
            return

        self._paint_ruler(painter, width)
        self._paint_bars(painter, samples, width, height)
        self._paint_overlays(painter, width, height)
        painter.end()

    def _paint_empty_state(self, painter: QPainter, width: int, height: int) -> None:
        cx, cy = width / 2, RULER_H + (height - RULER_H) / 2
        pen = QPen(QColor(theme.ACCENT))
        pen.setWidthF(2)
        painter.setPen(pen)
        painter.drawEllipse(int(cx - 26), int(cy - 26), 52, 52)
        triangle = QPainterPath()
        triangle.moveTo(cx - 8, cy - 13)
        triangle.lineTo(cx - 8, cy + 13)
        triangle.lineTo(cx + 13, cy)
        triangle.closeSubpath()
        painter.setBrush(QColor(theme.ACCENT))
        painter.drawPath(triangle)
        painter.setPen(QColor(theme.TEXT_DIM))
        painter.setFont(QFont(theme.FONT_FAMILY, 10))
        painter.drawText(
            QRectF(0, cy + 40, width, 24), Qt.AlignHCenter, "Click to open an audio file"
        )

    def _paint_ruler(self, painter: QPainter, width: int) -> None:
        duration = self.state_.duration_ms or 1
        interval_ms = _nice_tick_interval_ms(duration, width)
        painter.setPen(QColor(theme.BORDER))
        painter.drawLine(0, RULER_H - 1, width, RULER_H - 1)
        painter.setFont(QFont(theme.MONO_FAMILY, 8))
        t = 0
        while t <= duration:
            x = (t / duration) * width
            painter.setPen(QColor(theme.TEXT_DIM))
            painter.drawLine(int(x), RULER_H - 6, int(x), RULER_H - 1)
            label = format_duration(t)
            painter.setPen(QColor(theme.TEXT_DIM))
            painter.drawText(int(x) + 3, RULER_H - 7, label)
            t += interval_ms

    def _paint_bars(self, painter: QPainter, samples: np.ndarray, width: int, height: int) -> None:
        wave_top = RULER_H
        wave_h = height - RULER_H
        mid = wave_top + wave_h / 2
        n = len(samples)
        buckets = max(1, min(width, 1400))
        step = max(1, n // buckets)
        max_amp = float(np.abs(samples).max()) or 1.0
        bar_w = width / buckets

        gradient = QLinearGradient(0, wave_top, 0, wave_top + wave_h)
        gradient.setColorAt(0.0, QColor(theme.WAVE_FG))
        gradient.setColorAt(0.5, QColor(theme.ACCENT_HOVER))
        gradient.setColorAt(1.0, QColor(theme.WAVE_FG))
        pen = QPen(gradient, max(1.0, bar_w * 0.7))
        pen.setCapStyle(Qt.FlatCap)
        painter.setPen(pen)

        for i in range(buckets):
            start = i * step
            if start >= n:
                break
            chunk = samples[start:start + step]
            if len(chunk) == 0:
                continue
            amp = float(np.abs(chunk).max()) / max_amp
            x = i * bar_w
            painter.drawLine(
                int(x), int(mid - amp * (wave_h / 2) * 0.92),
                int(x), int(mid + amp * (wave_h / 2) * 0.92),
            )

        painter.setPen(QColor(theme.WAVE_CENTER_LINE))
        painter.drawLine(0, int(mid), width, int(mid))

    def _paint_overlays(self, painter: QPainter, width: int, height: int) -> None:
        duration = self.state_.duration_ms or 1
        if self.mode == "select" and self.sel_start_ms is not None and self.sel_end_ms is not None:
            x0 = (self.sel_start_ms / duration) * width
            x1 = (self.sel_end_ms / duration) * width
            left, right = min(x0, x1), max(x0, x1)
            # Dim everything OUTSIDE the selection instead of tinting inside it,
            # so a selection reads clearly even over a loud/full waveform.
            dim = QColor(theme.WAVE_BG)
            dim.setAlpha(150)
            if left > 0:
                painter.fillRect(QRectF(0, RULER_H, left, height - RULER_H), dim)
            if right < width:
                painter.fillRect(QRectF(right, RULER_H, width - right, height - RULER_H), dim)
            edge = QPen(QColor(theme.WAVE_SELECTION))
            edge.setWidthF(1.5)
            painter.setPen(edge)
            painter.drawLine(int(left), RULER_H, int(left), height)
            painter.drawLine(int(right), RULER_H, int(right), height)

        if self.mode == "markers":
            pen = QPen(QColor(theme.WAVE_MARKER))
            pen.setWidthF(2)
            painter.setPen(pen)
            for ms in self.marker_positions:
                x = (ms / duration) * width
                painter.drawLine(int(x), RULER_H, int(x), height)

        if self.playhead_ms is not None:
            pen = QPen(QColor(theme.WAVE_PLAYHEAD))
            pen.setWidthF(2)
            painter.setPen(pen)
            x = (self.playhead_ms / duration) * width
            painter.drawLine(int(x), RULER_H, int(x), height)


class TransportBar(QWidget):
    """Play/Stop with a live elapsed-time readout, plus Start/End/Length info
    (selection info in Trim/Cut mode, whole-file info otherwise)."""

    def __init__(self, state: AppState, waveform: WaveformWidget, parent=None):
        super().__init__(parent)
        self.state_ = state
        self.waveform = waveform
        self._play_start_wall: float | None = None
        self._play_start_ms = 0
        self._play_end_ms = 0
        self._timer = QTimer(self)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._tick)

        try:
            from .. import playback

            self._playback = playback
        except ImportError:
            self._playback = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 8, 4, 4)

        self.play_btn = QPushButton("▶  Play")
        self.play_btn.setProperty("accent", True)
        self.play_btn.clicked.connect(self._play)
        layout.addWidget(self.play_btn)

        stop_btn = QPushButton("■  Stop")
        stop_btn.clicked.connect(self._stop)
        layout.addWidget(stop_btn)

        if self._playback is None:
            self.play_btn.setEnabled(False)
            self.play_btn.setToolTip("Install sounddevice + numpy to enable playback.")

        self.time_label = QLabel("0:00")
        self.time_label.setFont(QFont(theme.MONO_FAMILY, 16, QFont.Bold))
        self.time_label.setStyleSheet(f"color: {theme.TEXT}; padding: 0 20px;")
        layout.addWidget(self.time_label)

        self.start_var, self.end_var, self.length_var = QLabel("—"), QLabel("—"), QLabel("—")
        for title, var in (("Start", self.start_var), ("End", self.end_var), ("Length", self.length_var)):
            cell = QVBoxLayout()
            cell.setSpacing(0)
            head = QLabel(title)
            head.setProperty("role", "muted")
            head.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 8pt;")
            var.setFont(QFont(theme.MONO_FAMILY, 10))
            cell.addWidget(head)
            cell.addWidget(var)
            wrap = QWidget()
            wrap.setLayout(cell)
            layout.addWidget(wrap)
            layout.addSpacing(14)

        layout.addStretch(1)
        self.file_info = QLabel("No file open")
        self.file_info.setProperty("role", "muted")
        layout.addWidget(self.file_info)

        state.file_changed.connect(self.refresh_file_info)
        self.refresh_file_info()

    def refresh_file_info(self) -> None:
        s = self.state_
        if s.path:
            fmt = Path(s.path).suffix.lstrip(".").upper()
            channel_desc = "stereo" if s.channels == 2 else "mono"
            self.file_info.setText(f"{Path(s.path).name}  ·  {s.frame_rate}Hz  ·  {channel_desc}  ·  {fmt}")
            self.set_range_display(0, s.duration_ms)
        else:
            self.file_info.setText("No file open")
            self.set_range_display(None, None)

    def set_range_display(self, start_ms: int | None, end_ms: int | None) -> None:
        if start_ms is None:
            self.start_var.setText("—")
            self.end_var.setText("—")
            self.length_var.setText("—")
        else:
            self.start_var.setText(format_duration(start_ms))
            self.end_var.setText(format_duration(end_ms))
            self.length_var.setText(format_duration(end_ms - start_ms))

    def _play(self) -> None:
        if self._playback is None or not self.state_.path:
            return
        if self.waveform.mode == "select" and self.waveform.sel_start_ms is not None and self.waveform.sel_end_ms is not None:
            start_ms, end_ms = self.waveform.sel_start_ms, self.waveform.sel_end_ms
        else:
            start_ms, end_ms = 0, self.state_.duration_ms
        segment = self.state_.slice_ms(start_ms, end_ms)

        try:
            self._playback.play_segment(segment, blocking=False)
        except core.AudioToolError:
            return
        self._play_start_wall = time.time()
        self._play_start_ms = start_ms
        self._play_end_ms = end_ms
        self._timer.start()

    def _tick(self) -> None:
        if self._play_start_wall is None:
            return
        elapsed_ms = int((time.time() - self._play_start_wall) * 1000)
        current_ms = self._play_start_ms + elapsed_ms
        if current_ms >= self._play_end_ms:
            self._stop()
            return
        self.time_label.setText(format_duration(current_ms))
        self.waveform.set_playhead(current_ms)

    def _stop(self) -> None:
        if self._playback is not None:
            self._playback.stop_playback()
        self._timer.stop()
        self._play_start_wall = None
        self.waveform.set_playhead(None)
        self.time_label.setText("0:00")


class WaveformWorkspace(QFrame):
    """The persistent card containing the waveform + transport, docked above
    the tool panels so it is always the visual center of the app."""

    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(6)

        self.waveform = WaveformWidget(state)
        layout.addWidget(self.waveform)

        self.transport = TransportBar(state, self.waveform)
        layout.addWidget(self.transport)

        def on_selection(start_ms: int, end_ms: int) -> None:
            self.transport.set_range_display(start_ms, end_ms)

        self.waveform.on_selection_change = on_selection
