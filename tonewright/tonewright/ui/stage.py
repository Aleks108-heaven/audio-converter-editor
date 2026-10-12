"""The waveform stage: overview strip, ruler, channel lanes, selection, markers and playhead."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from PySide6.QtCore import QLineF, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QCursor, QFont, QFontMetrics, QImage, QKeyEvent, QMouseEvent, QPainter,
                           QPainterPath, QPen, QPixmap, QWheelEvent)
from PySide6.QtWidgets import QWidget

from ..core import analysis
from ..core.document import Document
from ..core.timefmt import format_time
from . import icons, theme

MODES = ("waveform", "spectral", "envelope")
MIN_FPP = 1.0 / 64.0


@dataclass
class ViewState:
    """Everything about how one document is being looked at; kept per library item."""
    start: float = 0.0
    fpp: float = 0.0                       # frames per (logical) pixel; 0 = fit on first show
    vzoom: float = 1.0
    selection: Optional[tuple[int, int]] = None
    cursor: int = 0
    mode: str = "waveform"
    env_points: list = field(default_factory=lambda: [[0.0, 100.0], [1.0, 100.0]])
    env_range: Optional[tuple[int, int]] = None


# ------------------------------------------------------------------ ruler maths (pure, unit tested)
_MAJOR_STEPS = (0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300,
                600, 900, 1800, 3600, 7200, 14400, 28800)
_SUBDIV = {0.001: 5, 0.002: 4, 0.005: 5, 0.01: 5, 0.02: 4, 0.05: 5, 0.1: 5, 0.2: 4, 0.5: 5, 1: 5, 2: 4,
           5: 5, 10: 5, 15: 3, 30: 6, 60: 6, 120: 4, 300: 5, 600: 5, 900: 3, 1800: 6, 3600: 6, 7200: 4,
           14400: 4, 28800: 4}


def pick_major(seconds_per_px: float, min_px: float = 84.0) -> float:
    for step in _MAJOR_STEPS:
        if step / max(seconds_per_px, 1e-12) >= min_px:
            return step
    return _MAJOR_STEPS[-1]


def ruler_ticks(t0: float, t1: float, major: float) -> list[tuple[float, bool]]:
    """(time, is_major) pairs covering [t0, t1]."""
    div = _SUBDIV[major]
    minor = major / div
    k0 = math.ceil(t0 / minor - 1e-9)
    k1 = math.floor(t1 / minor + 1e-9)
    return [(k * minor, k % div == 0) for k in range(max(0, k0), k1 + 1)]


def ruler_label(t: float, major: float) -> str:
    total_ms = int(round(t * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    base = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    if major >= 1:
        return base
    decimals = 1 if major >= 0.1 else 2 if major >= 0.01 else 3
    return base + f".{ms:03d}"[: decimals + 1]


# ------------------------------------------------------------------ spectrogram colours
def _build_lut() -> np.ndarray:
    stops = [(0.0, (6, 5, 24)), (0.18, (34, 14, 78)), (0.36, (96, 20, 112)), (0.54, (172, 45, 96)),
             (0.72, (232, 94, 54)), (0.88, (250, 172, 38)), (1.0, (253, 244, 152))]
    x = np.linspace(0, 1, 256)
    chans = [np.interp(x, [s[0] for s in stops], [s[1][i] for s in stops]) for i in range(3)]
    r, g, b = (c.astype(np.uint32) for c in chans)
    return (np.uint32(255) << 24) | (r << 16) | (g << 8) | b


_LUT = _build_lut()
SPEC_FLOOR_DB, SPEC_CEIL_DB = -100.0, -8.0
DB_TICKS = (0.0, -6.0, -12.0, -18.0, -30.0)


class Stage(QWidget):
    selectionChanged = Signal()
    cursorChanged = Signal()
    viewChanged = Signal()
    seekRequested = Signal(int)
    contextMenuRequested = Signal(QPoint)
    envelopeChanged = Signal()
    openRequested = Signal()
    markerClicked = Signal(int)

    OVERVIEW_H = 42
    GAP = 6
    RULER_H = 30
    LEFT = 34
    RIGHT = 46
    BOTTOM = 8

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(460, 260)
        self.doc: Optional[Document] = None
        self.state = ViewState()
        self.playhead: Optional[int] = None
        self.follow = True
        self.show_db = True
        self.snap_zero = False
        self.spec_fft = 2048
        self.spec_log = True
        self._lanes_cache: Optional[tuple] = None
        self._overview_cache: Optional[tuple] = None
        self._drag: Optional[dict] = None
        self._hover_x: Optional[float] = None
        self._hover_cursor_kind: Optional[str] = None
        self._env_hover: Optional[int] = None
        self._autoscroll = QTimer(self)
        self._autoscroll.setInterval(30)
        self._autoscroll.timeout.connect(self._do_autoscroll)
        self._autoscroll_speed = 0.0

    # ------------------------------------------------------------ document plumbing
    def set_document(self, doc: Optional[Document], state: Optional[ViewState] = None) -> None:
        if self.doc is not None:
            self.doc.remove_listener(self._on_doc)
        self.doc = doc
        self.state = state or ViewState()
        self.playhead = None
        self._lanes_cache = self._overview_cache = None
        self._drag = None
        if doc is not None:
            doc.add_listener(self._on_doc)
            self._clamp_state()
        self.update()
        self.viewChanged.emit()
        self.selectionChanged.emit()

    def _on_doc(self, kind: str) -> None:
        if kind == "saved":
            return
        if kind in ("content", "format"):
            self._lanes_cache = self._overview_cache = None
            self._clamp_state()
            self.selectionChanged.emit()
        else:
            self._lanes_cache = None
        self.update()
        self.viewChanged.emit()

    def invalidate(self) -> None:
        self._lanes_cache = self._overview_cache = None
        self.update()

    # ------------------------------------------------------------ geometry
    def lane_rect(self) -> QRectF:
        top = self.OVERVIEW_H + self.GAP + self.RULER_H
        return QRectF(self.LEFT, top, max(10.0, self.width() - self.LEFT - self.RIGHT),
                      max(10.0, self.height() - top - self.BOTTOM))

    def ruler_rect(self) -> QRectF:
        lr = self.lane_rect()
        return QRectF(lr.left(), self.OVERVIEW_H + self.GAP, lr.width(), self.RULER_H)

    def overview_rect(self) -> QRectF:
        lr = self.lane_rect()
        return QRectF(lr.left(), 2, lr.width(), self.OVERVIEW_H - 2)

    def x_to_frame(self, x: float) -> float:
        return self.state.start + (x - self.lane_rect().left()) * self.state.fpp

    def frame_to_x(self, frame: float) -> float:
        return self.lane_rect().left() + (frame - self.state.start) / max(self.state.fpp, 1e-9)

    @property
    def view_range(self) -> tuple[float, float]:
        return self.state.start, self.state.start + self.lane_rect().width() * self.state.fpp

    def _frames(self) -> int:
        return self.doc.frames if self.doc else 0

    def _clamp_state(self) -> None:
        st, doc = self.state, self.doc
        if doc is None:
            return
        width = self.lane_rect().width()
        frames = max(1, doc.frames)
        max_fpp = max(frames / width, MIN_FPP)
        st.fpp = min(max(st.fpp if st.fpp > 0 else max_fpp, MIN_FPP), max_fpp)
        st.start = min(max(st.start, 0.0), max(0.0, frames - width * st.fpp))
        if st.selection:
            a, b = (max(0, min(v, doc.frames)) for v in st.selection)
            st.selection = (a, b) if b > a else None
        st.cursor = max(0, min(st.cursor, doc.frames))

    # ------------------------------------------------------------ public controls
    @property
    def selection(self) -> Optional[tuple[int, int]]:
        return self.state.selection

    @property
    def cursor(self) -> int:
        return self.state.cursor

    def set_selection(self, a: int, b: int, *, emit: bool = True) -> None:
        if a > b:
            a, b = b, a
        self.state.selection = (a, b) if b > a else None
        if emit:
            self.selectionChanged.emit()
        self.update()

    def clear_selection(self) -> None:
        if self.state.selection is not None:
            self.state.selection = None
            self.selectionChanged.emit()
            self.update()

    def set_cursor(self, frame: int, *, emit: bool = True) -> None:
        self.state.cursor = max(0, min(int(frame), self._frames()))
        if emit:
            self.cursorChanged.emit()
        self.update()

    def select_all(self) -> None:
        if self.doc and self.doc.frames:
            self.set_selection(0, self.doc.frames)

    def set_mode(self, mode: str) -> None:
        if mode not in MODES or mode == self.state.mode:
            return
        self.state.mode = mode
        if mode == "envelope":
            self.begin_envelope()
        self._lanes_cache = None
        self.update()
        self.viewChanged.emit()

    def begin_envelope(self) -> None:
        if not self.doc:
            return
        st = self.state
        st.env_range = st.selection or (0, self.doc.frames)
        st.env_points = [[0.0, 100.0], [1.0, 100.0]]

    def reset_envelope(self) -> None:
        self.state.env_points = [[0.0, 100.0], [1.0, 100.0]]
        self.envelopeChanged.emit()
        self.update()

    def set_playhead(self, frame: Optional[int]) -> None:
        self.playhead = frame
        if frame is not None and self.follow and self._drag is None and self.doc:
            a, b = self.view_range
            span = b - a
            if frame < a or frame > b - 0.04 * span:
                self.state.start = frame - 0.08 * span
                self._clamp_state()
                self._lanes_cache = None
                self.viewChanged.emit()
        self.update()

    def scroll_to(self, start: float) -> None:
        self.state.start = start
        self._clamp_state()
        self._lanes_cache = None
        self.update()
        self.viewChanged.emit()

    def ensure_visible(self, frame: int) -> None:
        a, b = self.view_range
        if frame < a or frame > b:
            self.scroll_to(frame - (b - a) / 2)

    def zoom_at(self, factor: float, anchor_x: Optional[float] = None) -> None:
        if not self.doc:
            return
        lr = self.lane_rect()
        ax = lr.center().x() if anchor_x is None else min(max(anchor_x, lr.left()), lr.right())
        frame = self.x_to_frame(ax)
        self.state.fpp /= factor
        self._clamp_state()
        self.state.start = frame - (ax - lr.left()) * self.state.fpp
        self._clamp_state()
        self._lanes_cache = None
        self.update()
        self.viewChanged.emit()

    def zoom_in(self) -> None:
        self.zoom_at(2.0, None if self.playhead is None else self.frame_to_x(self.playhead))

    def zoom_out(self) -> None:
        self.zoom_at(0.5, None if self.playhead is None else self.frame_to_x(self.playhead))

    def zoom_fit(self) -> None:
        if self.doc:
            self.state.fpp = 1e12
            self.state.start = 0.0
            self._clamp_state()
            self._lanes_cache = None
            self.update()
            self.viewChanged.emit()

    def zoom_selection(self) -> None:
        sel = self.state.selection
        if not (self.doc and sel):
            return
        width = self.lane_rect().width()
        pad = (sel[1] - sel[0]) * 0.04
        self.state.fpp = max(MIN_FPP, (sel[1] - sel[0] + 2 * pad) / width)
        self.state.start = sel[0] - pad
        self._clamp_state()
        self._lanes_cache = None
        self.update()
        self.viewChanged.emit()

    def zoom_samples(self) -> None:
        """One pixel per sample: the finest 'normal' zoom."""
        if self.doc:
            centre = self.playhead if self.playhead is not None else self.state.cursor
            lr = self.lane_rect()
            self.state.fpp = 1.0
            self._clamp_state()
            self.state.start = centre - lr.width() * self.state.fpp / 2
            self._clamp_state()
            self._lanes_cache = None
            self.update()
            self.viewChanged.emit()

    def vzoom(self, factor: float) -> None:
        self.state.vzoom = min(32.0, max(1.0, self.state.vzoom * factor))
        self._lanes_cache = None
        self.update()
        self.viewChanged.emit()

    # ------------------------------------------------------------ painting
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.doc:
            old_w = max(10.0, event.oldSize().width() - self.LEFT - self.RIGHT) if event.oldSize().width() > 0 else self.lane_rect().width()
            was_fit = self.state.fpp * old_w >= self._frames() * 0.999
            if was_fit:
                self.state.fpp = 1e12
            self._clamp_state()
        self._lanes_cache = self._overview_cache = None
        self.viewChanged.emit()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        t = theme.current()
        p.fillRect(self.rect(), theme.qcolor(t.bg1))
        if self.doc is None:
            self._paint_empty(p, t)
            return
        if self.state.fpp <= 0:
            self.state.fpp = 1e12
            self._clamp_state()
        self._paint_overview(p, t)
        self._paint_ruler(p, t)
        self._paint_lanes(p, t)
        self._paint_overlays(p, t)

    # ---- empty state
    def _paint_empty(self, p: QPainter, t) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(16, 16, -16, -16)
        pen = QPen(theme.qcolor(t.line_strong), 1.5, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(theme.qcolor(t.wave_bg))
        p.drawRoundedRect(r, 14, 14)
        cx, cy = r.center().x(), r.center().y()
        logo = icons.logo_pixmap(64)
        p.drawPixmap(int(cx - 32), int(cy - 96), logo)
        p.setPen(theme.qcolor(t.text))
        p.setFont(theme.ui_font(15, QFont.Weight.DemiBold))
        p.drawText(QRectF(r.left(), cy - 14, r.width(), 30), Qt.AlignmentFlag.AlignCenter, "Drop audio here")
        p.setPen(theme.qcolor(t.text_dim))
        p.setFont(theme.ui_font(10.5))
        p.drawText(QRectF(r.left(), cy + 18, r.width(), 24), Qt.AlignmentFlag.AlignCenter,
                   "or click to open a file  ·  Ctrl+O")
        p.setPen(theme.qcolor(t.text_faint))
        p.setFont(theme.ui_font(9.5))
        p.drawText(QRectF(r.left(), cy + 48, r.width(), 22), Qt.AlignmentFlag.AlignCenter,
                   "MP3 · WAV · FLAC · M4A · OGG · Opus · WMA · AAC · AMR · video files too")

    # ---- overview strip
    def _paint_overview(self, p: QPainter, t) -> None:
        ov = self.overview_rect()
        dpr = self.devicePixelRatioF()
        key = (self.doc.state.id, int(ov.width()), int(ov.height()), dpr, t.name)
        if self._overview_cache is None or self._overview_cache[0] != key:
            self._overview_cache = (key, self._render_overview(ov, dpr, t))
        p.drawPixmap(ov.topLeft(), self._overview_cache[1])
        a, b = self.view_range
        frames = max(1, self._frames())
        x0 = ov.left() + ov.width() * max(0.0, a) / frames
        x1 = ov.left() + ov.width() * min(frames, b) / frames
        vr = QRectF(x0, ov.top(), max(3.0, x1 - x0), ov.height())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.with_alpha(t.accent, 0.16))
        p.drawRect(vr)
        p.setBrush(theme.qcolor(t.accent))
        p.drawRect(QRectF(vr.left(), vr.top(), 2, vr.height()))
        p.drawRect(QRectF(vr.right() - 2, vr.top(), 2, vr.height()))
        p.setBrush(theme.with_alpha(t.accent, 0.8))
        p.drawRect(QRectF(vr.left(), vr.top(), vr.width(), 2))
        p.drawRect(QRectF(vr.left(), vr.bottom() - 2, vr.width(), 2))
        sel = self.state.selection
        if sel:
            sx0 = ov.left() + ov.width() * sel[0] / frames
            sx1 = ov.left() + ov.width() * sel[1] / frames
            p.setBrush(theme.qcolor(t.sel))
            p.drawRect(QRectF(sx0, ov.top(), max(1.5, sx1 - sx0), ov.height()))
        if self.playhead is not None:
            px = ov.left() + ov.width() * self.playhead / frames
            p.setPen(QPen(theme.qcolor(t.playhead), 1.2))
            p.drawLine(QLineF(px, ov.top(), px, ov.bottom()))

    def _render_overview(self, ov: QRectF, dpr: float, t) -> QPixmap:
        W, H = max(1, int(round(ov.width() * dpr))), max(1, int(round(ov.height() * dpr)))
        pm = QPixmap(W, H)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.qcolor(t.wave_bg))
        p.drawRect(0, 0, W, H)
        frames = self._frames()
        if frames > 0:
            cols = W
            if frames >= cols:
                mins, maxs = self.doc.peaks(0, frames, cols)
            else:
                data = self.doc.read(0, frames)
                idx = np.minimum((np.arange(frames) * cols // frames), cols - 1)
                mins = np.zeros((self.doc.channels, cols), np.float32)
                maxs = np.zeros_like(mins)
                for c in range(self.doc.channels):
                    mins[c, idx], maxs[c, idx] = data[:, c], data[:, c]
            top = np.clip(maxs.max(axis=0), -1, 1)
            bot = np.clip(mins.min(axis=0), -1, 1)
            mid, half = H / 2.0, H / 2.0 - 2
            pen = QPen(theme.qcolor(t.wave_dim))
            pen.setWidthF(1.0)
            p.setPen(pen)
            lines = [QLineF(i + 0.5, mid - top[i] * half - 0.5, i + 0.5, mid - bot[i] * half + 0.5) for i in range(cols)]
            p.drawLines(lines)
        p.end()
        pm.setDevicePixelRatio(dpr)
        return pm

    # ---- ruler
    def _paint_ruler(self, p: QPainter, t) -> None:
        rr = self.ruler_rect()
        sr = self.doc.sr
        a, b = self.view_range
        t0, t1 = a / sr, b / sr
        major = pick_major((t1 - t0) / rr.width())
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setFont(theme.ui_font(8.0))
        fm = QFontMetrics(p.font())
        p.save()
        p.setClipRect(rr.adjusted(-4, 0, 4, 0))
        for time_s, is_major in ruler_ticks(t0, t1, major):
            x = self.frame_to_x(time_s * sr)
            if is_major:
                p.setPen(QPen(theme.qcolor(t.text_faint), 1))
                p.drawLine(QLineF(round(x) + 0.5, rr.bottom() - 11, round(x) + 0.5, rr.bottom()))
                p.setPen(theme.qcolor(t.text_dim))
                p.drawText(QPointF(x + 4, rr.bottom() - 13 + fm.ascent() * 0.5), ruler_label(time_s, major))
            else:
                p.setPen(QPen(theme.qcolor(t.line_strong), 1))
                p.drawLine(QLineF(round(x) + 0.5, rr.bottom() - 5, round(x) + 0.5, rr.bottom()))
        p.restore()
        p.setPen(QPen(theme.qcolor(t.line), 1))
        p.drawLine(QLineF(rr.left(), rr.bottom() + 0.5, rr.right(), rr.bottom() + 0.5))
        # markers as little flags
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setFont(theme.ui_font(8.0, QFont.Weight.DemiBold))
        for m in self.doc.markers:
            x = self.frame_to_x(m.frame)
            if rr.left() - 2 <= x <= rr.right() + 2:
                label = m.name
                w = min(120, fm.horizontalAdvance(label) + 12)
                flag = QRectF(x, rr.top() + 1, w, 13)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(theme.qcolor(t.marker))
                p.drawRoundedRect(flag, 3, 3)
                p.setPen(QColor("#ffffff"))
                p.drawText(flag.adjusted(6, 0, -4, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                           fm.elidedText(label, Qt.TextElideMode.ElideRight, int(w - 10)))
        # hover time
        if self._hover_x is not None and rr.left() <= self._hover_x <= rr.right() and self._drag is None:
            frame = max(0, min(self._frames(), self.x_to_frame(self._hover_x)))
            label = format_time(frame / sr, short=True)
            tw = fm.horizontalAdvance(label) + 12
            bx = min(max(self._hover_x - tw / 2, rr.left()), rr.right() - tw)
            box = QRectF(bx, rr.bottom() - 17, tw, 16)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.qcolor(t.bg3))
            p.drawRoundedRect(box, 4, 4)
            p.setPen(theme.qcolor(t.text))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, label)

    # ---- lanes (cached)
    def _paint_lanes(self, p: QPainter, t) -> None:
        lr = self.lane_rect()
        dpr = self.devicePixelRatioF()
        st = self.state
        key = (self.doc.state.id, round(st.start, 3), round(st.fpp, 7), st.vzoom, int(lr.width()), int(lr.height()),
               dpr, st.mode, self.show_db, t.name, self.spec_fft, self.spec_log)
        if self._lanes_cache is None or self._lanes_cache[0] != key:
            self._lanes_cache = (key, self._render_lanes(lr, dpr, t))
        p.drawPixmap(lr.topLeft(), self._lanes_cache[1])
        # gutters
        left = QRectF(0, lr.top(), self.LEFT, lr.height())
        right = QRectF(lr.right(), lr.top(), self.RIGHT, lr.height())
        self._paint_gutters(p, t, lr, left, right)

    def _lane_bounds(self, height: float) -> list[tuple[float, float]]:
        n = self.doc.channels
        gap = 6.0 if n > 1 else 0.0
        h = (height - gap * (n - 1)) / n
        return [(i * (h + gap), i * (h + gap) + h) for i in range(n)]

    def _paint_gutters(self, p: QPainter, t, lr: QRectF, left: QRectF, right: QRectF) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setFont(theme.ui_font(8.0))
        names = ["L", "R"] if self.doc.channels == 2 else ["M"]
        bounds = self._lane_bounds(lr.height())
        mode = self.state.mode
        for i, (y0, y1) in enumerate(bounds):
            cy = lr.top() + (y0 + y1) / 2
            p.setPen(theme.qcolor(t.text_faint))
            p.drawText(QRectF(0, cy - 9, self.LEFT - 8, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       names[i])
            if mode == "spectral":
                self._paint_freq_labels(p, t, lr.top() + y0, lr.top() + y1, lr.right())
                continue
            half = (y1 - y0) / 2 * 0.92 * self.state.vzoom
            drawn: list[float] = []
            for value, text in self._amp_ticks():                  # most important ticks come first
                y = cy - value * half
                if y < lr.top() + y0 + 4 or y > lr.top() + y1 - 4:
                    continue
                if any(abs(y - u) < 13 for u in drawn):
                    continue
                drawn.append(y)
                p.drawText(QRectF(lr.right() + 6, y - 7, self.RIGHT - 8, 14),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)

    def _amp_ticks(self) -> list[tuple[float, str]]:
        """(signed linear amplitude, label) pairs for the vertical scale, most important first."""
        out: list[tuple[float, str]] = []
        if self.show_db:
            for db in DB_TICKS:
                amp = 10 ** (db / 20)
                label = "0" if db == 0 else f"{db:g}"
                out += [(amp, label), (-amp, label)]
            out.append((0.0, "-∞"))
        else:
            for v in (1.0, 0.5):
                out += [(v, f"{v:g}"), (-v, f"-{v:g}")]
            out.append((0.0, "0"))
        return out

    def _paint_freq_labels(self, p: QPainter, t, y0: float, y1: float, x: float) -> None:
        nyq = self.doc.sr / 2
        for f in (100, 500, 1000, 2000, 5000, 10000, 20000):
            if f >= nyq:
                continue
            y = y1 - (self._freq_to_frac(f, nyq)) * (y1 - y0)
            p.drawText(QRectF(x + 6, y - 7, self.RIGHT - 8, 14),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f"{f // 1000}k" if f >= 1000 else str(f))

    def _freq_to_frac(self, f: float, nyq: float) -> float:
        if self.spec_log:
            lo = 40.0
            return max(0.0, min(1.0, math.log(max(f, lo) / lo) / math.log(nyq / lo)))
        return max(0.0, min(1.0, f / nyq))

    def _render_lanes(self, lr: QRectF, dpr: float, t) -> QPixmap:
        W, H = max(1, int(round(lr.width() * dpr))), max(1, int(round(lr.height() * dpr)))
        pm = QPixmap(W, H)
        pm.fill(theme.qcolor(t.wave_bg))
        p = QPainter(pm)
        doc, st = self.doc, self.state
        sr = doc.sr
        bounds = self._lane_bounds(H)
        # time grid
        a, b = self.view_range
        major = pick_major((b - a) / sr / lr.width())
        grid = QPen(theme.qcolor(t.grid))
        grid.setStyle(Qt.PenStyle.DashLine)
        grid.setWidthF(max(1.0, dpr * 0.8))
        p.setPen(grid)
        for time_s, is_major in ruler_ticks(a / sr, b / sr, major):
            if is_major:
                x = (self.frame_to_x(time_s * sr) - lr.left()) * dpr
                p.drawLine(QLineF(round(x) + 0.5, 0, round(x) + 0.5, H))
        if st.mode == "spectral":
            for c, (y0, y1) in enumerate(bounds):
                self._draw_spectrogram(p, c, y0, y1, W, a, b, t)
        else:
            for c, (y0, y1) in enumerate(bounds):
                self._draw_wave_lane(p, c, y0, y1, W, a, b, dpr, t)
        # lane separators
        p.setPen(QPen(theme.qcolor(t.bg0), max(2.0, 3 * dpr)))
        for (_, y1), (y0n, _) in zip(bounds, bounds[1:]):
            p.drawLine(QLineF(0, (y1 + y0n) / 2, W, (y1 + y0n) / 2))
        p.end()
        pm.setDevicePixelRatio(dpr)
        return pm

    def _draw_wave_lane(self, p: QPainter, ch: int, y0: float, y1: float, W: int, a: float, b: float,
                        dpr: float, t) -> None:
        st, doc = self.state, self.doc
        mid = (y0 + y1) / 2
        half = (y1 - y0) / 2 * 0.92
        scale = half * st.vzoom
        ia, ib = int(math.floor(a)), int(math.ceil(b))
        # horizontal guide lines
        guide = QPen(theme.qcolor(t.grid))
        guide.setStyle(Qt.PenStyle.DashLine)
        guide.setWidthF(max(1.0, dpr * 0.8))
        p.setPen(guide)
        for value, _ in self._amp_ticks():
            y = mid - value * scale
            if y0 + 1 < y < y1 - 1:
                p.drawLine(QLineF(0, y, W, y))
        p.setPen(QPen(theme.qcolor(t.line_strong), max(1.0, dpr)))
        p.drawLine(QLineF(0, mid, W, mid))
        if ib <= 0 or ia >= doc.frames:
            return
        fpp_phys = (b - a) / W
        wave = theme.qcolor(t.wave)
        edge = theme.qcolor(t.wave_edge)
        clip = theme.qcolor(t.bad)
        if fpp_phys >= 1.0:
            mins, maxs = doc.peaks(ia, ib, W)
            top = np.clip(maxs[ch], -1e3, 1e3)
            bot = np.clip(mins[ch], -1e3, 1e3)
            ytop = np.clip(mid - top * scale, y0, y1)
            ybot = np.clip(mid - bot * scale, y0, y1)
            ybot = np.maximum(ybot, ytop + 1.0)
            p.setPen(QPen(wave, 1.0))
            p.drawLines([QLineF(i + 0.5, ytop[i], i + 0.5, ybot[i]) for i in range(W)])
            hot = np.nonzero((top >= 0.999) | (bot <= -0.999))[0]
            if len(hot):
                p.setPen(QPen(clip, 1.0))
                p.drawLines([QLineF(i + 0.5, ytop[i], i + 0.5, ybot[i]) for i in hot])
        else:
            lo = max(0, ia - 1)
            data = doc.read(lo, ib + 2)[:, ch]
            if len(data) == 0:
                return
            frames = np.arange(lo, lo + len(data), dtype=np.float64)
            xs = (frames - a) / fpp_phys
            ys = np.clip(mid - data * scale, y0, y1)
            path = QPainterPath()
            path.moveTo(xs[0], ys[0])
            for x, y in zip(xs[1:], ys[1:]):
                path.lineTo(x, y)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setPen(QPen(edge, max(1.2, 1.3 * dpr)))
            p.drawPath(path)
            if 1.0 / fpp_phys >= 7.0:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(wave)
                r = 2.6 * dpr
                for x, y in zip(xs, ys):
                    p.drawEllipse(QPointF(x, y), r, r)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    def _draw_spectrogram(self, p: QPainter, ch: int, y0: float, y1: float, W: int, a: float, b: float, t) -> None:
        doc = self.doc
        h = max(2, int(round(y1 - y0)))
        sg = analysis.spectrogram(doc, int(a), int(b), W, self.spec_fft, ch)       # (bins, W) dB
        bins = sg.shape[0]
        nyq = doc.sr / 2
        u = 1.0 - (np.arange(h) + 0.5) / h
        if self.spec_log:
            lo = 40.0
            freqs = lo * (nyq / lo) ** u
        else:
            freqs = u * nyq
        rows = np.clip(np.rint(freqs / nyq * (bins - 1)).astype(np.int64), 0, bins - 1)
        db = sg[rows]
        idx = np.clip((db - SPEC_FLOOR_DB) / (SPEC_CEIL_DB - SPEC_FLOOR_DB) * 255.0, 0, 255).astype(np.uint8)
        pix = np.ascontiguousarray(_LUT[idx])
        img = QImage(pix.data, W, h, W * 4, QImage.Format.Format_ARGB32)
        p.drawImage(0, int(round(y0)), img.copy())

    # ---- overlays (cheap, repainted every frame)
    def _paint_overlays(self, p: QPainter, t) -> None:
        lr = self.lane_rect()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.save()
        p.setClipRect(lr.adjusted(0, -self.RULER_H, 0, 0))
        sel = self.state.selection
        if sel:
            x0, x1 = self.frame_to_x(sel[0]), self.frame_to_x(sel[1])
            area = QRectF(max(x0, lr.left()), lr.top(), 0, lr.height())
            area.setRight(min(x1, lr.right()))
            if area.width() > 0:
                p.fillRect(area, theme.qcolor(t.sel))
                ruler = QRectF(area.left(), self.ruler_rect().top() + 18, area.width(), self.RULER_H - 18)
                p.fillRect(ruler, theme.with_alpha(t.sel_edge, 0.55))
            p.setPen(QPen(theme.qcolor(t.sel_edge), 1.2))
            for x in (x0, x1):
                if lr.left() - 1 <= x <= lr.right() + 1:
                    p.drawLine(QLineF(x, self.ruler_rect().top() + 14, x, lr.bottom()))
                    grip = QRectF(x - 3, lr.center().y() - 14, 6, 28)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(theme.qcolor(t.sel_edge))
                    p.drawRoundedRect(grip, 3, 3)
                    p.setPen(QPen(theme.qcolor(t.sel_edge), 1.2))
            if x1 - x0 > 70:
                label = format_time((sel[1] - sel[0]) / self.doc.sr, short=True)
                fm = QFontMetrics(theme.ui_font(8.5))
                p.setFont(theme.ui_font(8.5))
                bx = (max(x0, lr.left()) + min(x1, lr.right())) / 2
                box = QRectF(bx - (fm.horizontalAdvance(label) + 14) / 2, lr.top() + 6, fm.horizontalAdvance(label) + 14, 18)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(theme.with_alpha(t.bg0, 0.8))
                p.drawRoundedRect(box, 5, 5)
                p.setPen(theme.qcolor(t.text))
                p.drawText(box, Qt.AlignmentFlag.AlignCenter, label)
        for m in self.doc.markers:
            x = self.frame_to_x(m.frame)
            if lr.left() <= x <= lr.right():
                pen = QPen(theme.qcolor(t.marker), 1.2, Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.drawLine(QLineF(x, lr.top(), x, lr.bottom()))
        if self.state.mode == "envelope":
            self._paint_envelope(p, t, lr)
        # clip rect for markers/hover on lanes only; the cursor and playhead also reach into the ruler
        p.restore()
        p.save()
        p.setClipRect(QRectF(lr.left(), self.ruler_rect().top(), lr.width(), lr.bottom() - self.ruler_rect().top()))
        if self._hover_x is not None and lr.left() <= self._hover_x <= lr.right() and self._drag is None \
                and self.state.mode != "envelope":
            p.setPen(QPen(theme.with_alpha(t.text, 0.18), 1))
            p.drawLine(QLineF(self._hover_x, lr.top(), self._hover_x, lr.bottom()))
        cx = self.frame_to_x(self.state.cursor)
        playing = self.playhead is not None
        if lr.left() <= cx <= lr.right():
            p.setPen(QPen(theme.with_alpha(t.playhead, 0.55 if not playing else 0.25), 1))
            p.drawLine(QLineF(round(cx) + 0.5, lr.top(), round(cx) + 0.5, lr.bottom()))
            self._triangle(p, cx, self.ruler_rect().bottom(), theme.with_alpha(t.playhead, 0.0), t.playhead)
        if playing:
            px = self.frame_to_x(self.playhead)
            if lr.left() - 2 <= px <= lr.right() + 2:
                p.setPen(QPen(theme.qcolor(t.accent), 1.6))
                p.drawLine(QLineF(px, lr.top(), px, lr.bottom()))
                self._triangle(p, px, self.ruler_rect().bottom(), theme.qcolor(t.accent), t.accent)
        p.restore()

    @staticmethod
    def _triangle(p: QPainter, x: float, y: float, fill: QColor, outline: str) -> None:
        path = QPainterPath()
        path.moveTo(x - 5, y - 9)
        path.lineTo(x + 5, y - 9)
        path.lineTo(x, y)
        path.closeSubpath()
        p.setPen(QPen(theme.qcolor(outline), 1.2))
        p.setBrush(fill)
        p.drawPath(path)

    # ---- envelope overlay
    def _env_xy(self, point: list[float], lr: QRectF) -> QPointF:
        a, b = self.state.env_range or (0, self._frames())
        x = self.frame_to_x(a + point[0] * (b - a))
        y = lr.bottom() - point[1] / 200.0 * lr.height()
        return QPointF(x, y)

    def _paint_envelope(self, p: QPainter, t, lr: QRectF) -> None:
        a, b = self.state.env_range or (0, self._frames())
        x0, x1 = self.frame_to_x(a), self.frame_to_x(b)
        # dim everything outside the editable range
        dim = theme.with_alpha(t.bg0, 0.55)
        if x0 > lr.left():
            p.fillRect(QRectF(lr.left(), lr.top(), x0 - lr.left(), lr.height()), dim)
        if x1 < lr.right():
            p.fillRect(QRectF(x1, lr.top(), lr.right() - x1, lr.height()), dim)
        unity = lr.bottom() - lr.height() / 2
        p.setPen(QPen(theme.with_alpha(t.text, 0.35), 1, Qt.PenStyle.DashLine))
        p.drawLine(QLineF(lr.left(), unity, lr.right(), unity))
        p.setFont(theme.ui_font(8))
        p.setPen(theme.qcolor(t.text_faint))
        p.drawText(QPointF(lr.left() + 6, unity - 4), "100%")
        pts = [self._env_xy(pt, lr) for pt in self.state.env_points]
        path = QPainterPath(pts[0])
        for pt in pts[1:]:
            path.lineTo(pt)
        p.setPen(QPen(theme.qcolor(t.playhead), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        for i, pt in enumerate(pts):
            hot = i == self._env_hover or (self._drag and self._drag.get("kind") == "env" and self._drag["index"] == i)
            p.setPen(QPen(theme.qcolor(t.bg0), 2))
            p.setBrush(theme.qcolor(t.accent_hover if hot else t.accent))
            p.drawEllipse(pt, 6 if hot else 5, 6 if hot else 5)
            if hot:
                label = f"{self.state.env_points[i][1]:.0f}%"
                p.setPen(theme.qcolor(t.text))
                p.drawText(QPointF(pt.x() + 10, pt.y() - 8), label)

    # ------------------------------------------------------------ interaction
    def _region(self, pos: QPointF) -> str:
        y = pos.y()
        if y < self.OVERVIEW_H:
            return "overview"
        if y < self.lane_rect().top():
            return "ruler"
        return "lanes"

    def _snap(self, frame: float) -> int:
        frame = int(round(max(0, min(self._frames(), frame))))
        if not self.snap_zero or self.doc is None or self.state.fpp > 400:
            return frame
        radius = int(min(4000, max(64, 6 * self.state.fpp)))
        lo = max(0, frame - radius)
        data = self.doc.read(lo, min(self._frames(), frame + radius)).mean(axis=1)
        if len(data) < 2:
            return frame
        signs = np.signbit(data)
        crossings = np.nonzero(signs[1:] != signs[:-1])[0]
        if len(crossings) == 0:
            return frame
        best = crossings[int(np.argmin(np.abs(crossings + lo - frame)))]
        return int(lo + best + 1)

    def _selection_edge_at(self, x: float) -> Optional[str]:
        sel = self.state.selection
        if not sel:
            return None
        if abs(x - self.frame_to_x(sel[0])) <= 6:
            return "start"
        if abs(x - self.frame_to_x(sel[1])) <= 6:
            return "end"
        return None

    def _env_hit(self, pos: QPointF) -> Optional[int]:
        lr = self.lane_rect()
        for i, pt in enumerate(self.state.env_points):
            q = self._env_xy(pt, lr)
            if (q.x() - pos.x()) ** 2 + (q.y() - pos.y()) ** 2 <= 100:
                return i
        return None

    def _env_line_hit(self, pos: QPointF) -> bool:
        lr = self.lane_rect()
        pts = [self._env_xy(pt, lr) for pt in self.state.env_points]
        for p0, p1 in zip(pts, pts[1:]):
            if p0.x() <= pos.x() <= p1.x():
                span = max(1e-6, p1.x() - p0.x())
                y = p0.y() + (p1.y() - p0.y()) * (pos.x() - p0.x()) / span
                if abs(y - pos.y()) <= 9:
                    return True
        return False

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        self.setFocus()
        if self.doc is None:
            if ev.button() == Qt.MouseButton.LeftButton:
                self.openRequested.emit()
            return
        pos = ev.position()
        region = self._region(pos)
        lr = self.lane_rect()
        if ev.button() == Qt.MouseButton.MiddleButton:
            self._drag = {"kind": "pan", "x": pos.x(), "start": self.state.start}
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if ev.button() == Qt.MouseButton.RightButton:
            if self.state.mode == "envelope" and region == "lanes":
                i = self._env_hit(pos)
                if i is not None and 0 < i < len(self.state.env_points) - 1:
                    del self.state.env_points[i]
                    self.envelopeChanged.emit()
                    self.update()
            return
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        if region == "overview":
            ov = self.overview_rect()
            frames = max(1, self._frames())
            a, b = self.view_range
            x0 = ov.left() + ov.width() * a / frames
            x1 = ov.left() + ov.width() * min(frames, b) / frames
            if abs(pos.x() - x0) <= 5:
                self._drag = {"kind": "ov_left"}
            elif abs(pos.x() - x1) <= 5:
                self._drag = {"kind": "ov_right"}
            else:
                if not (x0 <= pos.x() <= x1):
                    self._ov_center(pos.x())
                    a, b = self.view_range
                    x0 = ov.left() + ov.width() * a / frames
                self._drag = {"kind": "ov_pan", "dx": pos.x() - x0}
            return
        if region == "lanes" and self.state.mode == "envelope":
            self._press_envelope(pos)
            return
        # ruler and lanes: selection / cursor
        frame = self._snap(self.x_to_frame(pos.x()))
        edge = self._selection_edge_at(pos.x()) if region == "lanes" else None
        if edge:
            self._drag = {"kind": "edge", "edge": edge}
            return
        marker = self._marker_at(pos) if region == "ruler" else None
        if marker is not None:
            self.markerClicked.emit(marker)
            self.set_cursor(self.doc.markers[marker].frame)
            return
        sel = self.state.selection
        if ev.modifiers() & Qt.KeyboardModifier.ShiftModifier and (sel or self.state.cursor is not None):
            anchor = sel[0] if sel and frame >= sel[0] else (sel[1] if sel else self.state.cursor)
            if sel and frame < sel[0]:
                anchor = sel[1]
            self._drag = {"kind": "select", "anchor": anchor, "moved": True}
            self.set_selection(anchor, frame)
            return
        self._drag = {"kind": "select", "anchor": frame, "moved": False, "x0": pos.x()}

    def _marker_at(self, pos: QPointF) -> Optional[int]:
        rr = self.ruler_rect()
        if not (rr.top() <= pos.y() <= rr.top() + 17):
            return None
        fm = QFontMetrics(theme.ui_font(8.0, QFont.Weight.DemiBold))
        for i, m in enumerate(self.doc.markers):
            x = self.frame_to_x(m.frame)
            if x <= pos.x() <= x + min(120, fm.horizontalAdvance(m.name) + 12):
                return i
        return None

    def _ov_center(self, x: float) -> None:
        ov = self.overview_rect()
        frames = self._frames()
        a, b = self.view_range
        centre = (x - ov.left()) / ov.width() * frames
        self.state.start = centre - (b - a) / 2
        self._clamp_state()
        self._lanes_cache = None
        self.update()
        self.viewChanged.emit()

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:
        pos = ev.position()
        self._hover_x = pos.x()
        if self.doc is None:
            return
        lr = self.lane_rect()
        d = self._drag
        if d is None:
            region = self._region(pos)
            kind = None
            if region == "overview":
                kind = "overview"
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            elif region == "lanes" and self.state.mode == "envelope":
                i = self._env_hit(pos)
                if i != self._env_hover:
                    self._env_hover = i
                self.setCursor(Qt.CursorShape.PointingHandCursor if (i is not None or self._env_line_hit(pos))
                               else Qt.CursorShape.ArrowCursor)
            elif region == "lanes" and self._selection_edge_at(pos.x()):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif region == "ruler" and self._marker_at(pos) is not None:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                self.setCursor(Qt.CursorShape.IBeamCursor if region == "lanes" else Qt.CursorShape.ArrowCursor)
            self.update()
            return
        kind = d["kind"]
        if kind == "pan":
            self.state.start = d["start"] - (pos.x() - d["x"]) * self.state.fpp
            self._clamp_state()
            self._lanes_cache = None
            self.viewChanged.emit()
        elif kind == "ov_pan":
            ov = self.overview_rect()
            frames = self._frames()
            self.state.start = (pos.x() - d["dx"] - ov.left()) / ov.width() * frames
            self._clamp_state()
            self._lanes_cache = None
            self.viewChanged.emit()
        elif kind in ("ov_left", "ov_right"):
            self._ov_resize(kind, pos.x())
        elif kind == "env":
            self._move_env_point(d["index"], pos)
        elif kind == "edge":
            frame = self._snap(self.x_to_frame(pos.x()))
            sel = self.state.selection or (0, 0)
            if d["edge"] == "start":
                self.set_selection(frame, sel[1])
                if self.state.selection is None or frame >= sel[1]:
                    d["edge"] = "end"
            else:
                self.set_selection(sel[0], frame)
                if self.state.selection is None or frame <= sel[0]:
                    d["edge"] = "start"
            self._edge_autoscroll(pos.x(), lr)
        elif kind == "select":
            if not d["moved"] and abs(pos.x() - d.get("x0", pos.x())) < 4:
                return
            d["moved"] = True
            frame = self._snap(self.x_to_frame(pos.x()))
            self.set_selection(d["anchor"], frame)
            self._edge_autoscroll(pos.x(), lr)
        self.update()

    def _edge_autoscroll(self, x: float, lr: QRectF) -> None:
        if x < lr.left() + 8:
            self._autoscroll_speed = -max(1.0, (lr.left() + 8 - x) / 6)
        elif x > lr.right() - 8:
            self._autoscroll_speed = max(1.0, (x - (lr.right() - 8)) / 6)
        else:
            self._autoscroll_speed = 0.0
        if self._autoscroll_speed and not self._autoscroll.isActive():
            self._autoscroll.start()
        elif not self._autoscroll_speed:
            self._autoscroll.stop()

    def _do_autoscroll(self) -> None:
        if not self._drag or not self._autoscroll_speed:
            self._autoscroll.stop()
            return
        self.state.start += self._autoscroll_speed * 8 * self.state.fpp
        self._clamp_state()
        self._lanes_cache = None
        pos = self.mapFromGlobal(QCursor.pos())
        d = self._drag
        frame = self._snap(self.x_to_frame(pos.x()))
        if d["kind"] == "select":
            self.set_selection(d["anchor"], frame)
        elif d["kind"] == "edge" and self.state.selection:
            sel = self.state.selection
            self.set_selection(frame, sel[1]) if d["edge"] == "start" else self.set_selection(sel[0], frame)
        self.viewChanged.emit()
        self.update()

    def _ov_resize(self, kind: str, x: float) -> None:
        ov = self.overview_rect()
        frames = self._frames()
        f = (x - ov.left()) / ov.width() * frames
        a, b = self.view_range
        width = self.lane_rect().width()
        if kind == "ov_left":
            new_a = min(f, b - width * MIN_FPP * 4)
            self.state.fpp = (b - new_a) / width
            self.state.start = new_a
        else:
            new_b = max(f, a + width * MIN_FPP * 4)
            self.state.fpp = (new_b - a) / width
        self._clamp_state()
        self._lanes_cache = None
        self.viewChanged.emit()

    def mouseReleaseEvent(self, ev: QMouseEvent) -> None:
        d, self._drag = self._drag, None
        self._autoscroll.stop()
        self._autoscroll_speed = 0.0
        if d is None or self.doc is None:
            return
        if d["kind"] == "pan":
            self.setCursor(Qt.CursorShape.IBeamCursor)
        if d["kind"] == "select" and not d["moved"]:
            frame = d["anchor"]
            had = self.state.selection is not None
            self.state.selection = None
            self.set_cursor(frame)
            if had:
                self.selectionChanged.emit()
            if self.playhead is not None:
                self.seekRequested.emit(frame)
        elif d["kind"] == "select" and d["moved"] and self.state.selection:
            self.set_cursor(self.state.selection[0], emit=False)
            self.cursorChanged.emit()
        elif d["kind"] == "env":
            self.envelopeChanged.emit()
        self.update()

    def mouseDoubleClickEvent(self, ev: QMouseEvent) -> None:
        if self.doc is None or ev.button() != Qt.MouseButton.LeftButton:
            return
        if self._region(ev.position()) == "lanes" and self.state.mode != "envelope":
            self._drag = None
            self.select_all()

    def leaveEvent(self, ev) -> None:
        self._hover_x = None
        self._env_hover = None
        self.update()

    def contextMenuEvent(self, ev) -> None:
        if self.doc is not None:
            self.contextMenuRequested.emit(ev.globalPos())

    def wheelEvent(self, ev: QWheelEvent) -> None:
        if self.doc is None:
            return
        dy, dx = ev.angleDelta().y(), ev.angleDelta().x()
        mods = ev.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            self.vzoom(1.25 ** (dy / 120))
        elif mods & Qt.KeyboardModifier.ShiftModifier or abs(dx) > abs(dy):
            delta = dx if abs(dx) > abs(dy) else dy
            a, b = self.view_range
            self.scroll_to(self.state.start - (b - a) * 0.12 * delta / 120)
        elif dy:
            self.zoom_at(1.3 ** (dy / 120), ev.position().x())
        ev.accept()

    def keyPressEvent(self, ev: QKeyEvent) -> None:
        if self.doc is None:
            return super().keyPressEvent(ev)
        key, mods = ev.key(), ev.modifiers()
        step = max(1, int(self.state.fpp * 8))
        if mods & Qt.KeyboardModifier.ControlModifier:
            step = int(self.doc.sr)
        extend = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            target = self.state.cursor + (step if key == Qt.Key.Key_Right else -step)
            target = max(0, min(self._frames(), target))
            if extend:
                anchor = self.state.selection[0] if self.state.selection and self.state.cursor == self.state.selection[1] \
                    else (self.state.selection[1] if self.state.selection else self.state.cursor)
                self.set_selection(anchor, target)
            self.set_cursor(target)
            self.ensure_visible(target)
        elif key == Qt.Key.Key_Home:
            self.set_cursor(0)
            self.ensure_visible(0)
        elif key == Qt.Key.Key_End:
            self.set_cursor(self._frames())
            self.ensure_visible(self._frames())
        else:
            super().keyPressEvent(ev)
            return
        ev.accept()

    # ---- envelope editing
    def _press_envelope(self, pos: QPointF) -> None:
        i = self._env_hit(pos)
        if i is None and self._env_line_hit(pos):
            i = self._insert_env_point(pos)
        if i is not None:
            self._drag = {"kind": "env", "index": i}

    def _insert_env_point(self, pos: QPointF) -> int:
        a, b = self.state.env_range or (0, self._frames())
        lr = self.lane_rect()
        t = (self.x_to_frame(pos.x()) - a) / max(1, b - a)
        t = min(max(t, 0.0), 1.0)
        gain = min(max((lr.bottom() - pos.y()) / lr.height() * 200.0, 0.0), 200.0)
        pts = self.state.env_points
        idx = next((k for k, pt in enumerate(pts) if pt[0] > t), len(pts))
        pts.insert(idx, [t, gain])
        self.envelopeChanged.emit()
        return idx

    def _move_env_point(self, index: int, pos: QPointF) -> None:
        a, b = self.state.env_range or (0, self._frames())
        lr = self.lane_rect()
        pts = self.state.env_points
        gain = min(max((lr.bottom() - pos.y()) / lr.height() * 200.0, 0.0), 200.0)
        if index in (0, len(pts) - 1):
            pts[index][1] = gain
        else:
            t = (self.x_to_frame(pos.x()) - a) / max(1, b - a)
            lo, hi = pts[index - 1][0] + 1e-4, pts[index + 1][0] - 1e-4
            pts[index] = [min(max(t, lo), hi), gain]
        self.envelopeChanged.emit()
