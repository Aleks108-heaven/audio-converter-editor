"""Small procedurally-drawn flat icons for the tool tree, so each tool has a
distinct glyph without shipping external image assets.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPainterPath, QPen, QPixmap

from . import theme

_SIZE = 22
_CACHE: dict[str, QIcon] = {}


def _new_painter(color: str) -> tuple[QPixmap, QPainter]:
    pix = QPixmap(_SIZE * 2, _SIZE * 2)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.scale(2, 2)
    pen = QPen(theme.QColor(color))
    pen.setWidthF(1.6)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    return pix, painter


def _finish(pix: QPixmap, painter: QPainter, key: str) -> QIcon:
    painter.end()
    icon = QIcon(pix)
    _CACHE[key] = icon
    return icon


def _draw(kind: str, painter: QPainter) -> None:
    s = _SIZE
    m = 4  # margin
    c = s / 2

    if kind == "convert":
        painter.drawArc(QRectF(m, m, s - 2 * m, s - 2 * m), 20 * 16, 250 * 16)
        painter.drawArc(QRectF(m, m, s - 2 * m, s - 2 * m), 200 * 16, 250 * 16)
        painter.drawLine(QPointF(s - m - 1, m + 1), QPointF(s - m + 2, m - 1))
        painter.drawLine(QPointF(s - m - 1, m + 1), QPointF(s - m - 3, m + 4))
        painter.drawLine(QPointF(m + 1, s - m - 1), QPointF(m - 2, s - m + 1))
        painter.drawLine(QPointF(m + 1, s - m - 1), QPointF(m + 3, s - m + 4))
    elif kind == "tags":
        path = QPainterPath()
        path.moveTo(m, c - 4)
        path.lineTo(c + 2, m)
        path.lineTo(s - m, m + 5)
        path.lineTo(c - 2, s - m)
        path.lineTo(m, c + 4)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawEllipse(QPointF(c - 1, m + 5), 1.4, 1.4)
    elif kind == "batch":
        for i, y in enumerate((m, m + 5.5, m + 11)):
            painter.drawLine(QPointF(m, y + 1), QPointF(s - m, y + 1))
    elif kind == "trim":
        painter.drawLine(QPointF(m, m), QPointF(s - m, s - m))
        painter.drawLine(QPointF(m, s - m), QPointF(c - 1, c + 1))
        painter.drawLine(QPointF(s - m, m), QPointF(c + 1, c - 1))
        painter.drawEllipse(QPointF(m + 1.5, m + 1.5), 1.6, 1.6)
        painter.drawEllipse(QPointF(m + 1.5, s - m - 1.5), 1.6, 1.6)
    elif kind == "split":
        painter.drawLine(QPointF(c, m), QPointF(c, s - m))
        painter.drawLine(QPointF(c - 4, m + 3), QPointF(m + 1, m + 3))
        painter.drawLine(QPointF(m + 1, m + 3), QPointF(m + 1, c))
        painter.drawLine(QPointF(c + 4, s - m - 3), QPointF(s - m - 1, s - m - 3))
        painter.drawLine(QPointF(s - m - 1, s - m - 3), QPointF(s - m - 1, c))
    elif kind == "join":
        painter.drawLine(QPointF(m, c - 3), QPointF(c - 1, c - 3))
        painter.drawLine(QPointF(m, c + 3), QPointF(c - 1, c + 3))
        painter.drawLine(QPointF(c + 1, c), QPointF(s - m, c))
        painter.drawEllipse(QPointF(c, c), 2.2, 2.2)
    elif kind == "mix":
        xs = (m + 2, c, s - m - 2)
        heights = (0.6, 0.3, 0.75)
        for x, h in zip(xs, heights):
            top = m + (s - 2 * m) * (1 - h)
            painter.drawLine(QPointF(x, m), QPointF(x, s - m))
            painter.drawEllipse(QPointF(x, top), 1.8, 1.8)
    elif kind == "channels":
        painter.drawEllipse(QRectF(m, c - 4, 8, 8))
        painter.drawEllipse(QRectF(s - m - 8, c - 4, 8, 8))
    elif kind == "effects":
        painter.drawLine(QPointF(m + 1, s - m - 1), QPointF(s - m - 3, m + 3))
        painter.drawLine(QPointF(s - m - 3, m + 3), QPointF(s - m - 1, m + 1))
        painter.drawLine(QPointF(s - m - 3, m + 3), QPointF(s - m - 5, m + 1))
        painter.drawLine(QPointF(m + 3, s - m - 3), QPointF(m + 5, s - m - 1))
        painter.drawLine(QPointF(m + 3, s - m - 3), QPointF(m + 1, s - m - 5))
    elif kind == "equalizer":
        xs = (m + 1, m + 6, m + 11, m + 16)
        tops = (m + 6, m + 1, m + 8, m + 4)
        for x, top in zip(xs, tops):
            painter.drawLine(QPointF(x, top), QPointF(x, s - m))
    elif kind == "generate":
        painter.drawLine(QPointF(m, c), QPointF(m + 4, c))
        painter.drawLine(QPointF(m + 4, c), QPointF(m + 6, c - 5))
        painter.drawLine(QPointF(m + 6, c - 5), QPointF(m + 9, c + 5))
        painter.drawLine(QPointF(m + 9, c + 5), QPointF(m + 11, c))
        painter.drawLine(QPointF(m + 11, c), QPointF(s - m, c))
        painter.drawLine(QPointF(s - m - 2, m + 1), QPointF(s - m - 2, m + 5))
        painter.drawLine(QPointF(s - m - 4, m + 3), QPointF(s - m, m + 3))
    elif kind == "audiobook":
        painter.drawRect(QRectF(m, m + 1, (s - 2 * m) * 0.42, s - 2 * m - 2))
        painter.drawLine(QPointF(c - 0.5, m + 1), QPointF(c - 0.5, s - m - 1))
        painter.drawArc(QRectF(c - 1, m + 1, (s - 2 * m) * 0.58, s - 2 * m - 2), 90 * 16, -180 * 16)
    elif kind == "record":
        painter.setBrush(theme.QColor(theme.WAVE_PLAYHEAD))
        painter.drawEllipse(QRectF(m + 2, m + 2, s - 2 * m - 4, s - 2 * m - 4))
    elif kind == "group_file":
        painter.drawPolyline([QPointF(m, m + 3), QPointF(m, s - m), QPointF(s - m, s - m), QPointF(s - m, m + 5), QPointF(c + 2, m + 5), QPointF(c, m), QPointF(m, m)])
    elif kind == "group_edit":
        painter.drawLine(QPointF(m, s - m), QPointF(s - m - 2, m + 2))
        painter.drawLine(QPointF(s - m - 5, s - m - 3), QPointF(s - m, s - m))
    elif kind == "group_effects":
        for x in (m + 1, c, s - m - 1):
            painter.drawLine(QPointF(x, m), QPointF(x, s - m))
    elif kind == "group_create":
        painter.drawLine(QPointF(c, m), QPointF(c, s - m))
        painter.drawLine(QPointF(m, c), QPointF(s - m, c))
    elif kind == "group_record":
        painter.setBrush(theme.QColor(theme.WAVE_PLAYHEAD))
        painter.drawEllipse(QRectF(c - 4, c - 4, 8, 8))


def get_icon(kind: str, color: str = theme.TEXT_MUTED) -> QIcon:
    key = f"{kind}:{color}"
    if key in _CACHE:
        return _CACHE[key]
    pix, painter = _new_painter(color)
    _draw(kind, painter)
    return _finish(pix, painter, key)
