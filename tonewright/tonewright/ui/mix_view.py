"""The multi-track timeline: line headers on the left, clips on a time canvas, a ruler above and a scroll bar below."""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QKeyEvent, QMouseEvent, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QScrollBar,
                               QSizePolicy, QSlider, QToolTip, QVBoxLayout, QWidget)

from ..core.mixer import CLIP_GAIN_RANGE, MIN_CLIP, VOLUME_RANGE, EffectSpan, Line, MixClip, MixProject
from ..core.timefmt import format_time
from ..i18n import tr
from . import icons, theme
from .widgets import ElidedLabel, icon_button

LINE_H = 104
EFFECT_H = 44
SECTION_H = 28
ADD_H = 44
HEADER_W = 214
RULER_H = 30
EDGE = 6                      # grab zone at a clip's left/right edge, px
HANDLE = 9                    # fade handle size, px
GAIN_PX_PER_DB = 0.4
LINE_COLOURS = ("#4ea6ff", "#5fd38d", "#ffb23e", "#ff6b81", "#b48cff", "#3ed6d6")
STEPS = (0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600)
SNAP_PX = 8


@dataclass
class Row:
    kind: str        # section-mix | line | add-line | section-fx | effect | spare
    id: int
    y: int
    h: int


@dataclass
class Hit:
    kind: str = "none"       # none | line | clip | clip-l | clip-r | fade-in | fade-out | gain | effect | effect-l | effect-r | fx-row | spare
    id: int = 0
    row: Optional[Row] = None


@dataclass
class _Drag:
    kind: str
    id: int
    press: QPointF
    start: int = 0
    end: int = 0
    offset: int = 0
    line: int = 0
    fade_in: int = 0
    fade_out: int = 0
    gain: float = 0.0
    started: bool = False


def compute_rows(project: Optional[MixProject]) -> tuple[list[Row], int]:
    rows: list[Row] = [Row("section-mix", 0, 0, SECTION_H)]
    y = SECTION_H
    if project is not None:
        for line in project.lines:
            rows.append(Row("line", line.id, y, LINE_H))
            y += LINE_H
    rows.append(Row("add-line", 0, y, ADD_H))
    y += ADD_H
    rows.append(Row("section-fx", 0, y, SECTION_H))
    y += SECTION_H
    if project is not None:
        for span in project.effects:
            rows.append(Row("effect", span.id, y, EFFECT_H))
            y += EFFECT_H
    rows.append(Row("spare", 0, y, EFFECT_H))
    y += EFFECT_H
    return rows, y


def tick_step(px_per_sec: float) -> float:
    for step in STEPS:
        if step * px_per_sec >= 84:
            return step
    return STEPS[-1]


def line_colour(project: MixProject, line_id: int) -> QColor:
    index = next((i for i, l in enumerate(project.lines) if l.id == line_id), 0)
    return QColor(LINE_COLOURS[index % len(LINE_COLOURS)])


def mix_colours(fg: QColor, bg: QColor, amount: float) -> QColor:
    return QColor(int(bg.red() + (fg.red() - bg.red()) * amount), int(bg.green() + (fg.green() - bg.green()) * amount),
                  int(bg.blue() + (fg.blue() - bg.blue()) * amount))


# ====================================================================== the canvas
class MixCanvas(QWidget):
    changed = Signal()                       # the project was edited by dragging
    selectionChanged = Signal()
    playheadMoved = Signal(int)              # the user placed the playhead (frames)
    viewChanged = Signal()                   # zoom or scroll
    layoutChanged = Signal()                 # rows were added or removed
    editStarting = Signal(str)               # before the first change of a drag: checkpoint, stop playback
    contextRequested = Signal(QPoint, object, int)      # global position, Hit, frame under the pointer
    filesDropped = Signal(list, object, int)            # paths, line id (or None for "a new line"), frame
    itemActivated = Signal(str, int)         # double click on ("clip" | "effect", id)
    deleteRequested = Signal()
    splitRequested = Signal()
    duplicateRequested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.project: Optional[MixProject] = None
        self.px_per_sec = 24.0
        self.t0 = 0.0
        self.playhead = 0
        self.selected: Optional[tuple[str, int]] = None
        self.snap = True
        self.rows: list[Row] = []
        self._total_h = 0
        self._drag: Optional[_Drag] = None
        self._drop: Optional[tuple[Optional[int], int]] = None
        self._peak_cache: dict = {}
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(200)
        self.relayout()

    # ---- model and geometry
    def set_project(self, project: Optional[MixProject]) -> None:
        self.project = project
        self.selected = None
        self._peak_cache.clear()
        self.relayout()

    @property
    def sr(self) -> int:
        return self.project.sr if self.project else 44100

    def relayout(self) -> None:
        self.rows, self._total_h = compute_rows(self.project)
        self.setFixedHeight(self._total_h + 24)
        self.layoutChanged.emit()
        self.update()

    def row_at(self, y: float) -> Optional[Row]:
        for row in self.rows:
            if row.y <= y < row.y + row.h:
                return row
        return None

    def row_of(self, kind: str, ident: int) -> Optional[Row]:
        return next((r for r in self.rows if r.kind == kind and r.id == ident), None)

    def frame_to_x(self, frame: float) -> float:
        return (frame / self.sr - self.t0) * self.px_per_sec

    def x_to_frame(self, x: float) -> int:
        return max(0, int(round((x / self.px_per_sec + self.t0) * self.sr)))

    @property
    def visible_seconds(self) -> float:
        return self.width() / self.px_per_sec

    # ---- view
    def set_zoom(self, px_per_sec: float, anchor_x: Optional[float] = None) -> None:
        px_per_sec = max(2.0, min(float(self.sr), px_per_sec))
        if anchor_x is None:
            anchor_x = self.width() / 2
        anchor_time = anchor_x / self.px_per_sec + self.t0
        self.px_per_sec = px_per_sec
        self.t0 = max(0.0, anchor_time - anchor_x / px_per_sec)
        self.update()
        self.viewChanged.emit()

    def set_t0(self, seconds: float) -> None:
        seconds = max(0.0, seconds)
        if abs(seconds - self.t0) > 1e-9:
            self.t0 = seconds
            self.update()
            self.viewChanged.emit()

    def fit(self) -> None:
        total = max(self.project.duration if self.project else 0.0, 5.0)
        self.px_per_sec = max(2.0, min(float(self.sr), (self.width() - 24) / (total * 1.04)))
        self.t0 = 0.0
        self.update()
        self.viewChanged.emit()

    def ensure_visible(self, frame: int) -> None:
        t = frame / self.sr
        if t < self.t0 or t > self.t0 + self.visible_seconds * 0.95:
            self.set_t0(max(0.0, t - self.visible_seconds * 0.1))

    def set_playhead(self, frame: int, *, follow: bool = False) -> None:
        old = self.playhead
        self.playhead = max(0, int(frame))
        if follow:
            self.ensure_visible(self.playhead)
        for f in (old, self.playhead):
            x = int(self.frame_to_x(f))
            self.update(x - 4, 0, 9, self.height())

    # ---- selection
    def select(self, kind: Optional[str], ident: int = 0) -> None:
        new = (kind, ident) if kind else None
        if new != self.selected:
            self.selected = new
            self.update()
            self.selectionChanged.emit()

    @property
    def selected_clip(self) -> Optional[MixClip]:
        if self.project and self.selected and self.selected[0] == "clip":
            return next((c for c in self.project.clips if c.id == self.selected[1]), None)
        return None

    @property
    def selected_effect(self) -> Optional[EffectSpan]:
        if self.project and self.selected and self.selected[0] == "effect":
            return next((e for e in self.project.effects if e.id == self.selected[1]), None)
        return None

    # ---- geometry of the things on a row
    def clip_rect(self, c: MixClip, row: Row) -> QRectF:
        x0, x1 = self.frame_to_x(c.start), self.frame_to_x(c.end)
        return QRectF(x0, row.y + 4, max(2.0, x1 - x0), row.h - 8)

    def effect_rect(self, e: EffectSpan, row: Row) -> QRectF:
        x0, x1 = self.frame_to_x(e.start), self.frame_to_x(e.end)
        return QRectF(x0, row.y + 4, max(2.0, x1 - x0), row.h - 8)

    def gain_y(self, c: MixClip, r: QRectF) -> float:
        mid = r.top() + 16 + (r.height() - 16) / 2
        return max(r.top() + 18, min(r.bottom() - 3, mid - c.gain_db * GAIN_PX_PER_DB))

    # ---- hit testing
    def hit_test(self, pos: QPointF) -> Hit:
        row = self.row_at(pos.y())
        if row is None or self.project is None:
            return Hit("none")
        x, y = pos.x(), pos.y()
        if row.kind == "line":
            for c in reversed([c for c in self.project.clips if c.line == row.id]):
                r = self.clip_rect(c, row)
                if not (r.left() - EDGE <= x <= r.right() + EDGE and r.top() <= y <= r.bottom()):
                    continue
                if self.selected == ("clip", c.id) or c.fade_in or c.fade_out:
                    fi = self.frame_to_x(c.start + c.fade_in)
                    fo = self.frame_to_x(c.end - c.fade_out)
                    if abs(x - fi) <= HANDLE and abs(y - (r.top() + 2)) <= HANDLE:
                        return Hit("fade-in", c.id, row)
                    if abs(x - fo) <= HANDLE and abs(y - (r.top() + 2)) <= HANDLE:
                        return Hit("fade-out", c.id, row)
                if abs(x - r.left()) <= EDGE and r.width() > 3 * EDGE:
                    return Hit("clip-l", c.id, row)
                if abs(x - r.right()) <= EDGE and r.width() > 3 * EDGE:
                    return Hit("clip-r", c.id, row)
                if r.left() <= x <= r.right() and abs(y - self.gain_y(c, r)) <= 4 and r.width() > 20:
                    return Hit("gain", c.id, row)
                if r.left() <= x <= r.right():
                    return Hit("clip", c.id, row)
            return Hit("line", row.id, row)
        if row.kind == "effect":
            e = next((e for e in self.project.effects if e.id == row.id), None)
            if e is not None:
                r = self.effect_rect(e, row)
                if r.top() <= y <= r.bottom():
                    if abs(x - r.left()) <= EDGE:
                        return Hit("effect-l", e.id, row)
                    if abs(x - r.right()) <= EDGE:
                        return Hit("effect-r", e.id, row)
                    if r.left() <= x <= r.right():
                        return Hit("effect", e.id, row)
            return Hit("fx-row", row.id, row)
        if row.kind == "spare":
            return Hit("spare", 0, row)
        return Hit("none", 0, row)

    # ---- painting
    def paintEvent(self, event) -> None:
        t = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = event.rect()
        bg0, bg1, bg2 = theme.qcolor(t.bg0), theme.qcolor(t.bg1), theme.qcolor(t.bg2)
        line_col = theme.qcolor(t.line)
        p.fillRect(area, bg0)
        audible = self.project.audible_lines() if self.project else set()
        for n, row in enumerate(self.rows):
            if row.y + row.h < area.top() or row.y > area.bottom():
                continue
            rect = QRectF(0, row.y, self.width(), row.h)
            if row.kind in ("section-mix", "section-fx"):
                p.fillRect(rect, bg2)
            elif row.kind in ("line", "effect", "spare"):
                p.fillRect(rect, bg1 if n % 2 else bg0)
            p.setPen(QPen(line_col, 1))
            p.drawLine(QPointF(0, row.y + row.h - 0.5), QPointF(self.width(), row.y + row.h - 0.5))
        self._draw_grid(p, area, t)
        if self.project is not None:
            for row in self.rows:
                if row.y + row.h < area.top() or row.y > area.bottom():
                    continue
                if row.kind == "line":
                    line = next(l for l in self.project.lines if l.id == row.id)
                    for c in self.project.clips:
                        if c.line == row.id:
                            self._draw_clip(p, c, row, line, row.id in audible, t)
                elif row.kind == "effect":
                    span = next((e for e in self.project.effects if e.id == row.id), None)
                    if span is not None:
                        self._draw_effect(p, span, row, t)
        if self._drop is not None:
            self._draw_drop_hint(p, t)
        x = self.frame_to_x(self.playhead)
        if -2 <= x <= self.width() + 2:
            pen = QPen(theme.qcolor(t.playhead), 1)
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(x, 0), QPointF(x, self.height()))
        p.end()

    def _draw_grid(self, p: QPainter, area, t) -> None:
        step = tick_step(self.px_per_sec)
        first = math.floor(self.t0 / step) * step
        grid = theme.qcolor(t.grid)
        p.setPen(QPen(grid, 1))
        k = first
        end = self.t0 + self.visible_seconds + step
        while k <= end:
            x = (k - self.t0) * self.px_per_sec
            if area.left() - 2 <= x <= area.right() + 2:
                p.drawLine(QPointF(x, SECTION_H), QPointF(x, self.height()))
            k += step

    def _peaks(self, c: MixClip, x0: float, vx0: int, vx1: int):
        """(mins, maxs) for the visible columns [vx0, vx1) of a clip, or None."""
        cols = vx1 - vx0
        if cols <= 0:
            return None
        fpp = self.sr / self.px_per_sec
        a = c.offset + int(max(0.0, (vx0 - x0)) * fpp)
        b = min(c.offset + c.length, c.offset + int(math.ceil((vx1 - x0) * fpp)))
        if b - a < 1:
            return None
        cols = max(1, min(cols, int((b - a) / fpp) or 1))
        key = (id(c.seq), a, b, cols)
        hit = self._peak_cache.get(key)
        if hit is None:
            if len(self._peak_cache) > 300:
                self._peak_cache.clear()
            try:
                hit = c.seq.peaks(a, b, cols)
            except ValueError:
                return None
            self._peak_cache[key] = hit
        return hit

    def _draw_clip(self, p: QPainter, c: MixClip, row: Row, line: Line, audible: bool, t) -> None:
        r = self.clip_rect(c, row)
        if r.right() < 0 or r.left() > self.width():
            return
        base = theme.qcolor(t.bg2)
        colour = line_colour(self.project, c.line)
        selected = self.selected == ("clip", c.id)
        fill = mix_colours(colour, base, 0.30 if selected else 0.20)
        path = QPainterPath()
        path.addRoundedRect(r, 5, 5)
        p.save()
        p.setClipPath(path)
        p.fillPath(path, fill)
        # waveform: left on top, right below
        vx0, vx1 = max(0, int(r.left())), min(self.width(), int(math.ceil(r.right())))
        peaks = self._peaks(c, r.left(), vx0, vx1)
        if peaks is not None:
            mins, maxs = peaks
            gain = 10 ** (c.gain_db / 20.0)
            body_top = r.top() + 16
            body_h = r.height() - 18
            half = body_h * 0.235
            cols = mins.shape[1]
            span = max(1, vx1 - vx0)
            pen = QPen(mix_colours(colour, QColor("#ffffff"), 0.55) if audible else theme.qcolor(t.text_faint), 1)
            p.setPen(pen)
            for ch in range(min(2, mins.shape[0])):
                cy = body_top + body_h * (0.25 if ch == 0 else 0.75)
                lines = []
                for i in range(cols):
                    x = vx0 + (i + 0.5) * span / cols
                    lo, hi = float(mins[ch, i]) * gain, float(maxs[ch, i]) * gain
                    lines.append((x, cy - max(-1.0, min(1.0, hi)) * half, cy - max(-1.0, min(1.0, lo)) * half))
                for x, y_hi, y_lo in lines:
                    p.drawLine(QPointF(x, y_hi), QPointF(x, max(y_lo, y_hi + 1)))
        # fades
        shade = QColor(0, 0, 0, 90)
        if c.fade_in:
            fx_ = self.frame_to_x(c.start + c.fade_in)
            p.setBrush(shade)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(QPolygonF([QPointF(r.left(), r.top()), QPointF(fx_, r.top()), QPointF(r.left(), r.bottom())]))
        if c.fade_out:
            fx_ = self.frame_to_x(c.end - c.fade_out)
            p.setBrush(shade)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(QPolygonF([QPointF(r.right(), r.top()), QPointF(fx_, r.top()), QPointF(r.right(), r.bottom())]))
        # gain line
        gy = self.gain_y(c, r)
        p.setPen(QPen(theme.qcolor(t.good), 1.4 if c.gain_db else 1))
        p.drawLine(QPointF(r.left(), gy), QPointF(r.right(), gy))
        # name
        font = theme.ui_font(8.5)
        font.setBold(True)
        p.setFont(font)
        fm = QFontMetrics(font)
        label = fm.elidedText(c.name or tr("Clip"), Qt.TextElideMode.ElideRight, max(0, int(r.width()) - 12))
        p.setPen(QColor(0, 0, 0, 110))
        p.drawText(QPointF(r.left() + 7, r.top() + 12.5), label)
        p.setPen(QColor("#ffffff") if t.name == "dark" else QColor("#1b1d21"))
        p.drawText(QPointF(r.left() + 6, r.top() + 11.5), label)
        if not audible:
            dim = theme.qcolor(t.bg0)
            dim.setAlpha(150)
            p.fillPath(path, dim)
        p.restore()
        # border and handles
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(theme.qcolor(t.accent) if selected else mix_colours(colour, base, 0.85), 2 if selected else 1))
        p.drawRoundedRect(r, 5, 5)
        for frame, shown in ((c.start + c.fade_in, selected or c.fade_in), (c.end - c.fade_out, selected or c.fade_out)):
            if shown:
                hx = self.frame_to_x(frame)
                p.setBrush(QColor("#ffffff"))
                p.setPen(QPen(QColor(0, 0, 0, 160), 1))
                p.drawRect(QRectF(hx - HANDLE / 2 + 0.5, r.top() - 0.5, HANDLE - 1, HANDLE - 1))

    def _draw_effect(self, p: QPainter, e: EffectSpan, row: Row, t) -> None:
        from ..core import effects as fx
        r = self.effect_rect(e, row)
        if r.right() < 0 or r.left() > self.width():
            return
        selected = self.selected == ("effect", e.id)
        accent = theme.qcolor(t.sel_edge)
        fill = mix_colours(accent, theme.qcolor(t.bg2), 0.42 if selected else 0.30)
        if e.mute:
            fill = mix_colours(theme.qcolor(t.text_faint), theme.qcolor(t.bg2), 0.25)
        p.setPen(QPen(theme.qcolor(t.good) if selected else mix_colours(accent, theme.qcolor(t.bg2), 0.8), 2 if selected else 1))
        p.setBrush(fill)
        p.drawRoundedRect(r, 4, 4)
        font = theme.ui_font(9)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor("#ffffff") if t.name == "dark" else QColor("#1b1d21"))
        name = tr(fx.get(e.effect).name)
        label = QFontMetrics(font).elidedText(name, Qt.TextElideMode.ElideRight, max(0, int(r.width()) - 14))
        p.drawText(r.adjusted(7, 0, -7, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
        if selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.qcolor(t.warn))
            p.drawRect(QRectF(r.right() - 3, r.top() + 2, 3, r.height() - 4))
            p.drawRect(QRectF(r.left(), r.top() + 2, 3, r.height() - 4))

    def _draw_drop_hint(self, p: QPainter, t) -> None:
        line_id, frame = self._drop
        row = self.row_of("line", line_id) if line_id is not None else self.row_of("add-line", 0)
        if row is None:
            return
        p.setPen(QPen(theme.qcolor(t.accent), 2, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(self.frame_to_x(frame), row.y + 3, 140, row.h - 6), 5, 5)

    # ---- mouse
    def _snap_points(self, exclude_clip: Optional[int] = None, exclude_effect: Optional[int] = None) -> list[int]:
        pts = {0, self.playhead}
        if self.project:
            for c in self.project.clips:
                if c.id != exclude_clip:
                    pts.update((c.start, c.end))
            for e in self.project.effects:
                if e.id != exclude_effect:
                    pts.update((e.start, e.end))
        return sorted(pts)

    def _snap(self, frame: int, points: list[int]) -> int:
        if not self.snap:
            return frame
        limit = SNAP_PX / self.px_per_sec * self.sr
        best = min(points, key=lambda q: abs(q - frame), default=frame)
        return best if abs(best - frame) <= limit else frame

    def _snap_span(self, start: int, length: int, points: list[int]) -> int:
        """Move a span so its start or its end lands on a snap point, whichever is closer (if either is near one)."""
        if not self.snap:
            return start
        snapped_start, snapped_end = self._snap(start, points), self._snap(start + length, points)
        candidates = []
        if snapped_start != start:
            candidates.append(snapped_start)
        if snapped_end != start + length:
            candidates.append(snapped_end - length)
        return min(candidates, key=lambda c: abs(c - start)) if candidates else start

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        self.setFocus()
        if self.project is None:
            return
        pos = ev.position()
        hit = self.hit_test(pos)
        if ev.button() == Qt.MouseButton.RightButton:
            if hit.kind.startswith("clip") or hit.kind in ("fade-in", "fade-out", "gain"):
                self.select("clip", hit.id)
            elif hit.kind.startswith("effect"):
                self.select("effect", hit.id)
            self.contextRequested.emit(ev.globalPosition().toPoint(), hit, self.x_to_frame(pos.x()))
            return
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        if hit.kind in ("clip", "clip-l", "clip-r", "fade-in", "fade-out", "gain"):
            c = self.project.clip(hit.id)
            self.select("clip", c.id)
            self._drag = _Drag(hit.kind, c.id, pos, c.start, c.end, c.offset, c.line, c.fade_in, c.fade_out, c.gain_db)
            return
        if hit.kind in ("effect", "effect-l", "effect-r"):
            e = self.project.effect_span(hit.id)
            self.select("effect", e.id)
            self._drag = _Drag(hit.kind, e.id, pos, e.start, e.end)
            return
        self.select(None)
        self.place_playhead(self.x_to_frame(pos.x()))

    def place_playhead(self, frame: int) -> None:
        self.set_playhead(frame)
        self.playheadMoved.emit(self.playhead)

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:
        pos = ev.position()
        d = self._drag
        if d is None or self.project is None:
            self._update_cursor(self.hit_test(pos))
            return
        if not d.started:
            if abs(pos.x() - d.press.x()) < 3 and abs(pos.y() - d.press.y()) < 3:
                return
            d.started = True
            self.editStarting.emit(self._drag_label(d.kind))
        dx = int((pos.x() - d.press.x()) / self.px_per_sec * self.sr)
        pr = self.project
        tip = ""
        if d.kind == "clip":
            c = pr.clip(d.id)
            start = self._snap_span(d.start + dx, d.end - d.start, self._snap_points(exclude_clip=d.id))
            row = self.row_at(pos.y())
            line_id = row.id if row is not None and row.kind == "line" else c.line
            pr.move_clip(d.id, start, line_id)
            tip = format_time(max(0, start) / self.sr, short=True)
        elif d.kind == "clip-l":
            pr.trim_start(d.id, self._snap(d.start + dx, self._snap_points(exclude_clip=d.id)))
            tip = format_time(pr.clip(d.id).start / self.sr, short=True)
        elif d.kind == "clip-r":
            pr.trim_end(d.id, self._snap(d.end + dx, self._snap_points(exclude_clip=d.id)))
            tip = format_time(pr.clip(d.id).end / self.sr, short=True)
        elif d.kind == "fade-in":
            c = pr.clip(d.id)
            pr.set_clip(d.id, fade_in=max(0, self.x_to_frame(pos.x()) - c.start))
            tip = tr(f"Fade in {pr.clip(d.id).fade_in / self.sr:.2f} s")
        elif d.kind == "fade-out":
            c = pr.clip(d.id)
            pr.set_clip(d.id, fade_out=max(0, c.end - self.x_to_frame(pos.x())))
            tip = tr(f"Fade out {pr.clip(d.id).fade_out / self.sr:.2f} s")
        elif d.kind == "gain":
            pr.set_clip(d.id, gain_db=d.gain - (pos.y() - d.press.y()) / GAIN_PX_PER_DB)
            tip = f"{pr.clip(d.id).gain_db:+.1f} dB"
        elif d.kind == "effect":
            pr.move_effect(d.id, self._snap_span(d.start + dx, d.end - d.start, self._snap_points(exclude_effect=d.id)))
            tip = format_time(pr.effect_span(d.id).start / self.sr, short=True)
        elif d.kind == "effect-l":
            pr.resize_effect(d.id, start=self._snap(d.start + dx, self._snap_points(exclude_effect=d.id)))
            tip = format_time(pr.effect_span(d.id).start / self.sr, short=True)
        elif d.kind == "effect-r":
            pr.resize_effect(d.id, end=self._snap(d.end + dx, self._snap_points(exclude_effect=d.id)))
            tip = format_time(pr.effect_span(d.id).end / self.sr, short=True)
        self.update()
        self.changed.emit()
        if tip:
            QToolTip.showText(ev.globalPosition().toPoint(), tip, self)

    @staticmethod
    def _drag_label(kind: str) -> str:
        return {"clip": "Move clip", "clip-l": "Trim clip", "clip-r": "Trim clip", "fade-in": "Fade in", "fade-out": "Fade out",
                "gain": "Clip volume", "effect": "Move effect", "effect-l": "Resize effect",
                "effect-r": "Resize effect"}.get(kind, "Edit")

    def mouseReleaseEvent(self, ev: QMouseEvent) -> None:
        d, self._drag = self._drag, None
        QToolTip.hideText()
        if d is not None and not d.started and d.kind in ("clip", "effect"):
            self.update()

    def mouseDoubleClickEvent(self, ev: QMouseEvent) -> None:
        if self.project is None:
            return
        hit = self.hit_test(ev.position())
        if hit.kind in ("clip", "gain"):
            self.itemActivated.emit("clip", hit.id)
        elif hit.kind in ("effect",):
            self.itemActivated.emit("effect", hit.id)
        elif hit.kind in ("add-line", "none") and hit.row is not None and hit.row.kind == "add-line":
            self.itemActivated.emit("add-line", 0)
        elif hit.kind == "spare":
            self.itemActivated.emit("add-effect", 0)

    def _update_cursor(self, hit: Hit) -> None:
        shape = {"clip-l": Qt.CursorShape.SizeHorCursor, "clip-r": Qt.CursorShape.SizeHorCursor,
                 "effect-l": Qt.CursorShape.SizeHorCursor, "effect-r": Qt.CursorShape.SizeHorCursor,
                 "fade-in": Qt.CursorShape.SizeHorCursor, "fade-out": Qt.CursorShape.SizeHorCursor,
                 "gain": Qt.CursorShape.SizeVerCursor, "clip": Qt.CursorShape.OpenHandCursor,
                 "effect": Qt.CursorShape.OpenHandCursor}.get(hit.kind, Qt.CursorShape.ArrowCursor)
        self.setCursor(shape)

    def wheelEvent(self, ev) -> None:
        mods = ev.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            factor = 1.25 if ev.angleDelta().y() > 0 else 0.8
            self.set_zoom(self.px_per_sec * factor, ev.position().x())
            ev.accept()
        elif mods & Qt.KeyboardModifier.ShiftModifier or abs(ev.angleDelta().x()) > abs(ev.angleDelta().y()):
            delta = ev.angleDelta().y() or ev.angleDelta().x()
            self.set_t0(self.t0 - delta / 120.0 * self.visible_seconds * 0.15)
            ev.accept()
        else:
            ev.ignore()

    def keyPressEvent(self, ev: QKeyEvent) -> None:
        key = ev.key()
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.deleteRequested.emit()
        elif key == Qt.Key.Key_S and not ev.modifiers():
            self.splitRequested.emit()
        elif key == Qt.Key.Key_D and ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.duplicateRequested.emit()
        else:
            super().keyPressEvent(ev)

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self.viewChanged.emit()

    # ---- dropping files from Explorer
    @staticmethod
    def _local_files(mime) -> list[Path]:
        return [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile() and Path(u.toLocalFile()).is_file()]

    def dragEnterEvent(self, ev) -> None:
        if self._local_files(ev.mimeData()):
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev) -> None:
        pos = ev.position()
        row = self.row_at(pos.y())
        self._drop = (row.id if row is not None and row.kind == "line" else None, self.x_to_frame(pos.x()))
        self.update()
        ev.acceptProposedAction()

    def dragLeaveEvent(self, ev) -> None:
        self._drop = None
        self.update()

    def dropEvent(self, ev) -> None:
        files = self._local_files(ev.mimeData())
        target, frame = self._drop if self._drop else (None, self.x_to_frame(ev.position().x()))
        self._drop = None
        self.update()
        if files:
            self.filesDropped.emit(files, target, frame)
            ev.acceptProposedAction()


# ====================================================================== ruler
class MixRuler(QWidget):
    def __init__(self, canvas: MixCanvas) -> None:
        super().__init__()
        self.canvas = canvas
        self.setFixedHeight(RULER_H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        canvas.viewChanged.connect(self.update)

    def paintEvent(self, event) -> None:
        t = theme.current()
        c = self.canvas
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), theme.qcolor(t.bg1))
        p.setPen(QPen(theme.qcolor(t.line), 1))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        step = tick_step(c.px_per_sec)
        sub = step / 5 if step >= 1 else step / 2
        font = theme.mono_font(8)
        p.setFont(font)
        k = math.floor(c.t0 / sub) * sub
        end = c.t0 + self.width() / c.px_per_sec + step
        while k <= end:
            x = (k - c.t0) * c.px_per_sec
            major = abs(k / step - round(k / step)) < 1e-6
            p.setPen(QPen(theme.qcolor(t.text_dim if major else t.text_faint), 1))
            p.drawLine(QPointF(x, self.height() - (12 if major else 6)), QPointF(x, self.height() - 1))
            if major:
                p.setPen(theme.qcolor(t.text_dim))
                p.drawText(QPointF(x + 4, 12), format_time(k, ms=step < 1, short=False))
            k += sub
        x = c.frame_to_x(c.playhead)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.qcolor(t.playhead))
        p.drawPolygon(QPolygonF([QPointF(x - 6, self.height() - 11), QPointF(x + 6, self.height() - 11), QPointF(x, self.height() - 1)]))
        p.end()

    def _move(self, ev: QMouseEvent) -> None:
        self.canvas.place_playhead(self.canvas.x_to_frame(max(0.0, ev.position().x())))
        self.update()

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self._move(ev)

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:
        if ev.buttons() & Qt.MouseButton.LeftButton:
            self._move(ev)

    def wheelEvent(self, ev) -> None:
        self.canvas.wheelEvent(ev)


# ====================================================================== headers
class ResetSlider(QSlider):
    """Double-click puts it back to its neutral position."""

    def __init__(self, neutral: int) -> None:
        super().__init__(Qt.Orientation.Horizontal)
        self.neutral = neutral
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseDoubleClickEvent(self, ev) -> None:
        self.setValue(self.neutral)


def _toggle_style(checked_bg: str) -> str:
    t = theme.current()
    return (f"QPushButton {{ padding: 2px 8px; border-radius: 4px; background: {t.bg3}; color: {t.text}; "
            f"border: 1px solid {t.line_strong}; font-size: {theme.css_size(8.5)}; }}"
            f"QPushButton:checked {{ background: {checked_bg}; color: {t.accent_fg}; border-color: {checked_bg}; }}"
            f"QPushButton:hover {{ border-color: {t.accent}; }}")


class LineHeader(QFrame):
    volumeChanged = Signal(int, float)
    panChanged = Signal(int, float)
    muteToggled = Signal(int, bool)
    soloToggled = Signal(int, bool)
    removeRequested = Signal(int)
    recordRequested = Signal(int)
    renameRequested = Signal(int, str)
    editStarting = Signal(str)

    def __init__(self, line: Line, colour: str) -> None:
        super().__init__()
        self.line_id = line.id
        self.colour = colour
        self.setFixedSize(HEADER_W - 6, LINE_H - 4)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 4, 6, 4)
        lay.setSpacing(3)
        top = QHBoxLayout()
        top.setSpacing(4)
        self.name = ElidedLabel(tr(line.name))
        self.name.setStyleSheet("font-weight: 700;")
        self.name.mouseDoubleClickEvent = lambda ev: self._rename()
        self.close_btn = icon_button("x", tr("Remove this line"), size=14)
        self.close_btn.clicked.connect(lambda: self.removeRequested.emit(self.line_id))
        top.addWidget(self.name, 1)
        top.addWidget(self.close_btn)
        lay.addLayout(top)
        self.volume = ResetSlider(0)
        self.volume.setRange(int(VOLUME_RANGE[0] * 10), int(VOLUME_RANGE[1] * 10))
        self.volume.setToolTip(tr("Line volume (double-click for 0 dB)"))
        self.pan = ResetSlider(0)
        self.pan.setRange(-100, 100)
        self.pan.setToolTip(tr("Pan: left to right (double-click for centre)"))
        for slider, icon_name, handler in ((self.volume, "volume", self._volume), (self.pan, "sliders", self._pan)):
            row = QHBoxLayout()
            row.setSpacing(6)
            ic = QLabel()
            ic.setPixmap(icons.pixmap(icon_name, theme.current().text_dim, 14))
            row.addWidget(ic)
            row.addWidget(slider, 1)
            lay.addLayout(row)
            slider.sliderPressed.connect(lambda s=slider: self.editStarting.emit(tr("Line volume") if s is self.volume else tr("Pan")))
            slider.valueChanged.connect(handler)
        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        self.mute = QPushButton(tr("Mute", "short"))
        self.solo = QPushButton(tr("Solo"))
        for b in (self.mute, self.solo):
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mute.toggled.connect(lambda on: self.muteToggled.emit(self.line_id, on))
        self.solo.toggled.connect(lambda on: self.soloToggled.emit(self.line_id, on))
        self.rec = icon_button("record", tr("Record onto this line"), size=14)
        self.rec.clicked.connect(lambda: self.recordRequested.emit(self.line_id))
        buttons.addWidget(self.mute, 1)
        buttons.addWidget(self.solo, 1)
        buttons.addWidget(self.rec)
        lay.addLayout(buttons)
        self.restyle()
        self.refresh(line)

    def restyle(self) -> None:
        t = theme.current()
        self.setStyleSheet(f"LineHeader {{ background: {t.bg2}; border: 1px solid {t.line_strong}; border-left: 4px solid {self.colour}; border-radius: 6px; }}")
        self.mute.setStyleSheet(_toggle_style(t.bad))
        self.solo.setStyleSheet(_toggle_style(t.warn))
        self.name.setStyleSheet("font-weight: 700; background: transparent;")

    def refresh(self, line: Line) -> None:
        for w, v in ((self.volume, int(round(line.volume_db * 10))), (self.pan, int(round(line.pan * 100)))):
            w.blockSignals(True)
            w.setValue(v)
            w.blockSignals(False)
        for b, v in ((self.mute, line.mute), (self.solo, line.solo)):
            b.blockSignals(True)
            b.setChecked(v)
            b.blockSignals(False)
        self.name.setText(tr(line.name))
        self.name.setToolTip(tr(line.name) + "\n" + tr("Double-click to rename"))

    def _volume(self, v: int) -> None:
        if not self.volume.isSliderDown():
            self.editStarting.emit(tr("Line volume"))
        self.volumeChanged.emit(self.line_id, v / 10.0)

    def _pan(self, v: int) -> None:
        if not self.pan.isSliderDown():
            self.editStarting.emit(tr("Pan"))
        self.panChanged.emit(self.line_id, v / 100.0)

    def _rename(self) -> None:
        edit = QLineEdit(self.name.text(), self)
        edit.setGeometry(self.name.geometry().adjusted(-2, -2, 2, 2))
        edit.selectAll()
        edit.show()
        edit.setFocus()

        def done() -> None:
            text = edit.text().strip()
            edit.deleteLater()
            if text and text != self.name.text():
                self.renameRequested.emit(self.line_id, text)
        edit.editingFinished.connect(done)


class EffectHeader(QFrame):
    muteToggled = Signal(int, bool)
    removeRequested = Signal(int)

    def __init__(self, span: EffectSpan, title: str) -> None:
        super().__init__()
        self.span_id = span.id
        self.setFixedSize(HEADER_W - 6, EFFECT_H - 4)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 2, 6, 2)
        lay.setSpacing(4)
        self.name = ElidedLabel(title)
        self.mute = QPushButton(tr("Mute", "short"))
        self.mute.setCheckable(True)
        self.mute.setChecked(span.mute)
        self.mute.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mute.toggled.connect(lambda on: self.muteToggled.emit(self.span_id, on))
        self.close_btn = icon_button("x", tr("Remove this effect"), size=14)
        self.close_btn.clicked.connect(lambda: self.removeRequested.emit(self.span_id))
        lay.addWidget(self.name, 1)
        lay.addWidget(self.mute)
        lay.addWidget(self.close_btn)
        self.restyle()

    def restyle(self) -> None:
        t = theme.current()
        self.setStyleSheet(f"EffectHeader {{ background: {t.bg2}; border: 1px solid {t.line_strong}; border-left: 4px solid {t.sel_edge}; border-radius: 6px; }}")
        self.mute.setStyleSheet(_toggle_style(t.bad))
        self.name.setStyleSheet("font-weight: 600; background: transparent;")

    def refresh(self, span: EffectSpan, title: str) -> None:
        self.name.setText(title)
        self.mute.blockSignals(True)
        self.mute.setChecked(span.mute)
        self.mute.blockSignals(False)


class HeaderColumn(QWidget):
    """The fixed left column: section titles, one header per line and per effect, aligned with the canvas rows."""

    addLineRequested = Signal()
    addEffectRequested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setFixedWidth(HEADER_W)
        self._rows: list[Row] = []
        self.lines: dict[int, LineHeader] = {}
        self.effects: dict[int, EffectHeader] = {}
        self.titles: list[QLabel] = []
        self.add_line_btn = QPushButton("＋  " + tr("Add line"), self)
        self.add_line_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_line_btn.setProperty("variant", "ghost")
        self.add_line_btn.clicked.connect(self.addLineRequested)
        self.add_fx_btn = QPushButton("＋  " + tr("Add effect"), self)
        self.add_fx_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_fx_btn.setProperty("variant", "ghost")
        self.add_fx_btn.clicked.connect(self.addEffectRequested)
        self.mix_title = QLabel(tr("AUDIO MIX"), self)
        self.fx_title = QLabel(tr("AUDIO EFFECTS"), self)
        for lab in (self.mix_title, self.fx_title):
            lab.setProperty("role", "section")

    def paintEvent(self, event) -> None:
        t = theme.current()
        p = QPainter(self)
        bg0, bg1, bg2 = theme.qcolor(t.bg0), theme.qcolor(t.bg1), theme.qcolor(t.bg2)
        p.fillRect(self.rect(), bg0)
        for n, row in enumerate(self._rows):
            shade = bg2 if row.kind.startswith("section") else (bg1 if n % 2 else bg0)
            p.fillRect(0, row.y, self.width(), row.h, shade)
            p.setPen(QPen(theme.qcolor(t.line), 1))
            p.drawLine(0, row.y + row.h - 1, self.width(), row.y + row.h - 1)
        p.setPen(QPen(theme.qcolor(t.line_strong), 1))
        p.drawLine(self.width() - 1, 0, self.width() - 1, self.height())
        p.end()

    def place(self, rows: list[Row]) -> None:
        self._rows = rows
        self.update()
        for row in rows:
            if row.kind == "section-mix":
                self.mix_title.setGeometry(10, row.y, HEADER_W - 20, row.h)
            elif row.kind == "section-fx":
                self.fx_title.setGeometry(10, row.y, HEADER_W - 20, row.h)
            elif row.kind == "line" and row.id in self.lines:
                self.lines[row.id].move(3, row.y + 2)
            elif row.kind == "effect" and row.id in self.effects:
                self.effects[row.id].move(3, row.y + 2)
            elif row.kind == "add-line":
                self.add_line_btn.setGeometry(8, row.y + 6, HEADER_W - 22, row.h - 12)
            elif row.kind == "spare":
                self.add_fx_btn.setGeometry(8, row.y + 6, HEADER_W - 22, row.h - 12)


# ====================================================================== the whole board
class MixBoard(QWidget):
    """Ruler, headers + canvas (scrolling vertically together), and a horizontal scroll bar."""

    editStarting = Signal(str)
    changed = Signal()                      # the project was edited from here (canvas or headers)
    lineRecordRequested = Signal(int)
    addLineRequested = Signal()
    addEffectRequested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.canvas = MixCanvas()
        self.ruler = MixRuler(self.canvas)
        self.headers = HeaderColumn()
        inner = QWidget()
        hl = QHBoxLayout(inner)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(0)
        hl.addWidget(self.headers, 0, Qt.AlignmentFlag.AlignTop)
        hl.addWidget(self.canvas, 1, Qt.AlignmentFlag.AlignTop)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.scroll.setWidget(inner)
        self.hbar = QScrollBar(Qt.Orientation.Horizontal)
        sb = self.scroll.verticalScrollBar().sizeHint().width()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        top = QHBoxLayout()
        top.setSpacing(0)
        top.addSpacing(HEADER_W)
        top.addWidget(self.ruler, 1)
        top.addSpacing(sb)
        lay.addLayout(top)
        lay.addWidget(self.scroll, 1)
        bottom = QHBoxLayout()
        bottom.setSpacing(0)
        bottom.addSpacing(HEADER_W)
        bottom.addWidget(self.hbar, 1)
        bottom.addSpacing(sb)
        lay.addLayout(bottom)

        self.project: Optional[MixProject] = None
        self.canvas.viewChanged.connect(self._sync_scrollbar)
        self.canvas.layoutChanged.connect(self._place_headers)
        self.canvas.changed.connect(self._after_edit)
        self.canvas.editStarting.connect(self.editStarting)
        self.hbar.valueChanged.connect(lambda v: self.canvas.set_t0(v / 1000.0))
        self.headers.addLineRequested.connect(self.addLineRequested)
        self.headers.addEffectRequested.connect(self.addEffectRequested)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), theme.qcolor(theme.current().bg1))
        p.end()

    # ---- project
    def bind(self, project: MixProject) -> None:
        self.project = project
        self.canvas.set_project(project)
        self.refresh()

    def refresh(self) -> None:
        """Bring headers, rows and scroll range in line with the project after any change."""
        if self.project is None:
            return
        self.canvas.relayout()
        self._sync_headers()
        self._place_headers()
        self._sync_scrollbar()
        self.canvas.update()
        self.ruler.update()

    def _after_edit(self) -> None:
        self._sync_headers()
        self._sync_scrollbar()
        self.changed.emit()

    # ---- headers
    def _effect_title(self, span: EffectSpan) -> str:
        from ..core import effects as fx
        return tr(fx.get(span.effect).name)

    def _sync_headers(self) -> None:
        project, h = self.project, self.headers
        if project is None:
            return
        wanted = {l.id for l in project.lines}
        for line_id in [i for i in h.lines if i not in wanted]:
            h.lines.pop(line_id).deleteLater()
        for line in project.lines:
            header = h.lines.get(line.id)
            if header is None:
                header = LineHeader(line, LINE_COLOURS[project.lines.index(line) % len(LINE_COLOURS)])
                header.setParent(h)
                header.volumeChanged.connect(self._volume)
                header.panChanged.connect(self._pan)
                header.muteToggled.connect(self._mute)
                header.soloToggled.connect(self._solo)
                header.removeRequested.connect(self._remove_line)
                header.recordRequested.connect(self.lineRecordRequested)
                header.renameRequested.connect(self._rename_line)
                header.editStarting.connect(self.editStarting)
                header.show()
                h.lines[line.id] = header
            else:
                header.refresh(line)
        wanted_fx = {e.id for e in project.effects}
        for span_id in [i for i in h.effects if i not in wanted_fx]:
            h.effects.pop(span_id).deleteLater()
        for span in project.effects:
            header = h.effects.get(span.id)
            if header is None:
                header = EffectHeader(span, self._effect_title(span))
                header.setParent(h)
                header.muteToggled.connect(self._mute_effect)
                header.removeRequested.connect(self._remove_effect)
                header.show()
                h.effects[span.id] = header
            else:
                header.refresh(span, self._effect_title(span))

    def _place_headers(self) -> None:
        self.headers.setFixedHeight(self.canvas.height())
        self.headers.place(self.canvas.rows)

    def restyle(self) -> None:
        for header in [*self.headers.lines.values(), *self.headers.effects.values()]:
            header.restyle()
        self.canvas.update()
        self.ruler.update()

    # ---- header actions (each is one undo step)
    def _volume(self, line_id: int, db: float) -> None:
        self.project.set_line(line_id, volume_db=db)
        self.changed.emit()

    def _pan(self, line_id: int, pan: float) -> None:
        self.project.set_line(line_id, pan=pan)
        self.changed.emit()

    def _mute(self, line_id: int, on: bool) -> None:
        self.editStarting.emit(tr("Mute"))
        self.project.set_line(line_id, mute=on)
        self.canvas.update()
        self.changed.emit()

    def _solo(self, line_id: int, on: bool) -> None:
        self.editStarting.emit(tr("Solo"))
        self.project.set_line(line_id, solo=on)
        self.canvas.update()
        self.changed.emit()

    def _rename_line(self, line_id: int, name: str) -> None:
        self.editStarting.emit(tr("Rename line"))
        self.project.set_line(line_id, name=name)
        self.refresh()
        self.changed.emit()

    def _remove_line(self, line_id: int) -> None:
        self.editStarting.emit(tr("Remove line"))
        self.project.remove_line(line_id)
        if self.canvas.selected and self.canvas.selected[0] == "clip" and not any(c.id == self.canvas.selected[1] for c in self.project.clips):
            self.canvas.select(None)
        self.refresh()
        self.changed.emit()

    def _mute_effect(self, span_id: int, on: bool) -> None:
        self.editStarting.emit(tr("Mute"))
        self.project.set_effect(span_id, mute=on)
        self.canvas.update()
        self.changed.emit()

    def _remove_effect(self, span_id: int) -> None:
        self.editStarting.emit(tr("Remove effect"))
        self.project.delete_effect(span_id)
        if self.canvas.selected == ("effect", span_id):
            self.canvas.select(None)
        self.refresh()
        self.changed.emit()

    # ---- scrolling
    def _sync_scrollbar(self) -> None:
        c = self.canvas
        page = max(1, int(c.visible_seconds * 1000))
        total = max(60.0, (self.project.duration if self.project else 0.0) + c.visible_seconds * 0.5)
        self.hbar.blockSignals(True)
        self.hbar.setRange(0, max(0, int(total * 1000) - page))
        self.hbar.setPageStep(page)
        self.hbar.setSingleStep(max(1, page // 12))
        self.hbar.setValue(int(c.t0 * 1000))
        self.hbar.blockSignals(False)

    def zoom(self, factor: float) -> None:
        self.canvas.set_zoom(self.canvas.px_per_sec * factor)

    def fit(self) -> None:
        self.canvas.fit()

    def scroll_to_row(self, row: Optional[Row]) -> None:
        if row is not None:
            self.scroll.ensureVisible(0, row.y, 0, row.h)
