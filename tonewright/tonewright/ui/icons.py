"""Line icons drawn from small SVG snippets, plus the Tonewright logo."""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
                           QPixmap)
from PySide6.QtSvg import QSvgRenderer

from . import theme

# 24x24 canvas, drawn with a 1.8px round stroke. {c} is replaced by the colour.
_ICONS: dict[str, str] = {
    "file-new": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M12 11v6M9 14h6"/>',
    "open": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "save": '<path d="M5 3h11l4 4v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M7 3v5h8V3M7 21v-7h10v7"/>',
    "film": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
    "copy": '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/>',
    "cut": '<circle cx="6" cy="6.5" r="2.6"/><circle cx="6" cy="17.5" r="2.6"/><path d="M20 4 8.2 15.3M20 20 8.2 8.7"/>',
    "paste": '<path d="M9 4h6a1 1 0 0 1 1 1v1H8V5a1 1 0 0 1 1-1z"/><path d="M16 5h2a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h2"/>',
    "trash": '<path d="M4 7h16M10 11v6M14 11v6"/><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/>',
    "crop": '<path d="M7 3v14a2 2 0 0 0 2 2h12"/><path d="M3 7h14a2 2 0 0 1 2 2v12"/>',
    "undo": '<path d="M4 8h10a6 6 0 0 1 0 12H8"/><path d="M8 4 4 8l4 4"/>',
    "redo": '<path d="M20 8H10a6 6 0 0 0 0 12h6"/><path d="m16 4 4 4-4 4"/>',
    "play": '<path d="M7 4.5v15l12-7.5z" fill="{c}"/>',
    "pause": '<rect x="6" y="5" width="4" height="14" rx="1" fill="{c}"/><rect x="14" y="5" width="4" height="14" rx="1" fill="{c}"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2" fill="{c}"/>',
    "loop": '<path d="M17 2l4 4-4 4"/><path d="M3 11V9a3 3 0 0 1 3-3h15"/><path d="m7 22-4-4 4-4"/><path d="M21 13v2a3 3 0 0 1-3 3H3"/>',
    "skip-start": '<path d="M6 5v14"/><path d="M19 5.5v13L9 12z" fill="{c}"/>',
    "skip-end": '<path d="M18 5v14"/><path d="M5 5.5v13L15 12z" fill="{c}"/>',
    "rewind": '<path d="M11 6.5v11L3 12z" fill="{c}"/><path d="M21 6.5v11L13 12z" fill="{c}"/>',
    "forward": '<path d="M13 6.5v11L21 12z" fill="{c}"/><path d="M3 6.5v11L11 12z" fill="{c}"/>',
    "record": '<circle cx="12" cy="12" r="6.5" fill="{c}"/>',
    "mic": '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3M8 21h8"/>',
    "zoom-in": '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4M11 8v6M8 11h6"/>',
    "zoom-out": '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4M8 11h6"/>',
    "zoom-sel": '<path d="M4 9V5a1 1 0 0 1 1-1h4M15 4h4a1 1 0 0 1 1 1v4M20 15v4a1 1 0 0 1-1 1h-4M9 20H5a1 1 0 0 1-1-1v-4"/><rect x="8.5" y="8.5" width="7" height="7" rx="1"/>',
    "zoom-fit": '<path d="M4 12h16M8 8l-4 4 4 4M16 8l4 4-4 4"/>',
    "zoom-1": '<path d="M9 8l3-2v12"/><path d="M5 20h14" opacity="0"/><circle cx="19" cy="17" r="0.6" fill="{c}"/>',
    "vzoom-in": '<path d="M12 4v16M8 8l4-4 4 4M8 16l4 4 4-4"/>',
    "vzoom-out": '<path d="M12 4v16M8 6l4 4 4-4M8 18l4-4 4 4"/>',
    "waveform": '<path d="M3 12h2M7 7v10M11 4v16M15 8v8M19 10v4"/>',
    "spectral": '<rect x="4" y="13" width="3" height="7" rx="1"/><rect x="10.5" y="5" width="3" height="15" rx="1"/><rect x="17" y="9" width="3" height="11" rx="1"/>',
    "envelope": '<path d="M3 17 9 9l5 5 7-9"/><circle cx="9" cy="9" r="1.6" fill="{c}"/><circle cx="14" cy="14" r="1.6" fill="{c}"/><circle cx="21" cy="5" r="1.6" fill="{c}"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "minus": '<path d="M5 12h14"/>',
    "x": '<path d="M6 6l12 12M18 6 6 18"/>',
    "check": '<path d="m5 12.5 4.5 4.5L19 7"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "chevron-right": '<path d="m9 6 6 6-6 6"/>',
    "arrow-up": '<path d="M12 19V5M6 11l6-6 6 6"/>',
    "arrow-down": '<path d="M12 5v14M6 13l6 6 6-6"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    "sliders": '<path d="M5 21v-7M5 10V3M12 21v-9M12 8V3M19 21v-5M19 12V3M2 14h6M9 8h6M16 16h6"/>',
    "star": '<path d="m12 3 2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z"/>',
    "tag": '<path d="M3 12V4a1 1 0 0 1 1-1h8l9 9-9 9z"/><circle cx="8" cy="8" r="1.4" fill="{c}"/>',
    "book": '<path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2z"/><path d="M4 19V5M9 7h6M9 11h6"/>',
    "music": '<path d="M9 18V5l11-2v13"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>',
    "convert": '<path d="M20 7H8a4 4 0 0 0-4 4v1M4 17h12a4 4 0 0 0 4-4v-1"/><path d="m16 3 4 4-4 4M8 21l-4-4 4-4"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "image": '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="1.8"/><path d="m21 16-5-5-8 8"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "moon": '<path d="M20 14.5A8 8 0 1 1 9.5 4 6.5 6.5 0 0 0 20 14.5z"/>',
    "volume": '<path d="M4 9v6h4l5 4V5L8 9z" fill="{c}" stroke-width="1.2"/><path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12"/>',
    "volume-mute": '<path d="M4 9v6h4l5 4V5L8 9z" fill="{c}" stroke-width="1.2"/><path d="m17 9 5 6M22 9l-5 6"/>',
    "flag": '<path d="M5 21V4M5 4h11l-2 4 2 4H5"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
    "wand": '<path d="m4 20 11-11M14 5l1.2 2.3L17.5 8.5 15.2 9.7 14 12l-1.2-2.3L10.5 8.5l2.3-1.2zM19 14l.7 1.3 1.3.7-1.3.7L19 18l-.7-1.3L17 16l1.3-.7z"/>',
    "layers": '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5M3 17.5l9 5 9-5" opacity="0.65"/>',
    "download": '<path d="M12 4v11M7 11l5 5 5-5M5 20h14"/>',
    "edit": '<path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16z"/><path d="m13.5 6.5 4 4"/>',
    "dots": '<circle cx="5" cy="12" r="1.6" fill="{c}"/><circle cx="12" cy="12" r="1.6" fill="{c}"/><circle cx="19" cy="12" r="1.6" fill="{c}"/>',
    "refresh": '<path d="M20 11a8 8 0 0 0-14-4L4 9M4 4v5h5M4 13a8 8 0 0 0 14 4l2-2M20 20v-5h-5"/>',
    "speaker": '<rect x="6" y="3" width="12" height="18" rx="3"/><circle cx="12" cy="15" r="3"/><circle cx="12" cy="8" r="1" fill="{c}"/>',
    "scissors-split": '<path d="M12 3v18M7 8l5-5 5 5M7 16l5 5 5-5"/>',
    "silence": '<path d="M4 9v6h4l5 4V5L8 9z"/><path d="M17 9l4 6M21 9l-4 6"/>',
    "tone": '<path d="M2 12c2-6 4-6 6 0s4 6 6 0 4-6 6 0"/>',
    "noise": '<path d="M3 12h1M6 8v8M9 5v14M12 9v6M15 4v16M18 8v8M21 11v2"/>',
    "keypad": '<circle cx="6" cy="6" r="1.5" fill="{c}"/><circle cx="12" cy="6" r="1.5" fill="{c}"/><circle cx="18" cy="6" r="1.5" fill="{c}"/><circle cx="6" cy="12" r="1.5" fill="{c}"/><circle cx="12" cy="12" r="1.5" fill="{c}"/><circle cx="18" cy="12" r="1.5" fill="{c}"/><circle cx="6" cy="18" r="1.5" fill="{c}"/><circle cx="12" cy="18" r="1.5" fill="{c}"/><circle cx="18" cy="18" r="1.5" fill="{c}"/>',
    "speech": '<path d="M4 5h16v11H11l-5 4v-4H4z"/><path d="M8 9h8M8 12.5h5"/>',
    # a window with its left or right panel marked: the buttons that fold the library and the side panel away
    "panel-left": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9.5 4v16"/>'
                  '<path d="M5 4.9h4v14.2H5a1.1 1.1 0 0 1-1.1-1.1V6A1.1 1.1 0 0 1 5 4.9z" fill="{c}" opacity="0.3" stroke="none"/>',
    "panel-right": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M14.5 4v16"/>'
                   '<path d="M19 4.9h-4v14.2h4a1.1 1.1 0 0 0 1.1-1.1V6A1.1 1.1 0 0 0 19 4.9z" fill="{c}" opacity="0.3" stroke="none"/>',
}

_STROKE = 1.8


def _svg(name: str, color: str, size: int, stroke: float = _STROKE) -> QByteArray:
    body = _ICONS[name].replace("{c}", color)
    return QByteArray(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 24 24" '
        f'fill="none" stroke="{color}" stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">'
        f'{body}</svg>'.encode("utf-8"))


@lru_cache(maxsize=512)
def pixmap(name: str, color: str, size: int = 18, dpr: float = 2.0) -> QPixmap:
    px = int(size * dpr)
    img = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(_svg(name, color, px)).render(p)
    p.end()
    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(dpr)
    return pm


def icon(name: str, size: int = 18, *, color: str | None = None, active: str | None = None) -> QIcon:
    """An icon that follows the theme: dim normally, bright on hover, accent when checked."""
    t = theme.current()
    ic = QIcon()
    normal = color or t.text_dim
    hot = active or (color or t.text)
    ic.addPixmap(pixmap(name, normal, size), QIcon.Mode.Normal, QIcon.State.Off)
    ic.addPixmap(pixmap(name, hot, size), QIcon.Mode.Active, QIcon.State.Off)
    ic.addPixmap(pixmap(name, t.accent, size), QIcon.Mode.Normal, QIcon.State.On)
    ic.addPixmap(pixmap(name, t.accent, size), QIcon.Mode.Active, QIcon.State.On)
    ic.addPixmap(pixmap(name, t.accent, size), QIcon.Mode.Selected, QIcon.State.Off)
    ic.addPixmap(pixmap(name, t.text_faint, size), QIcon.Mode.Disabled, QIcon.State.Off)
    ic.addPixmap(pixmap(name, t.text_faint, size), QIcon.Mode.Disabled, QIcon.State.On)
    return ic


_bound: list[tuple] = []          # (weakref to widget/action, icon name, size)


def _role_color(role):
    return theme.current().accent_fg if role == "on_accent" else None


def bind(obj, name: str, size: int = 18, role=None) -> None:
    """Give `obj` (anything with setIcon) a themed icon and remember it for theme changes.
    role="on_accent" draws the icon in the colour used for text on the accent colour."""
    import weakref
    obj.setIcon(icon(name, size, color=_role_color(role)))
    _bound.append((weakref.ref(obj), name, size, role))


def refresh_all() -> None:
    alive = []
    for ref, name, size, role in _bound:
        obj = ref()
        if obj is None:
            continue
        try:
            obj.setIcon(icon(name, size, color=_role_color(role)))
            alive.append((ref, name, size, role))
        except RuntimeError:          # the C++ object is gone
            continue
    _bound[:] = alive


def names() -> list[str]:
    return sorted(_ICONS)


def logo_pixmap(size: int = 32, dpr: float = 2.0) -> QPixmap:
    """The Tonewright mark: an amber tile with a mirrored waveform and a playhead."""
    px = int(size * dpr)
    img = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = px / 32.0
    p.scale(s, s)
    grad = QLinearGradient(0, 0, 32, 32)
    grad.setColorAt(0.0, QColor("#ffc866"))
    grad.setColorAt(1.0, QColor("#f0841f"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(grad))
    p.drawRoundedRect(QRectF(0.5, 0.5, 31, 31), 8.5, 8.5)
    bars = [(6.5, 3.2), (10.3, 7.0), (14.1, 11.5), (17.9, 8.2), (21.7, 5.0), (25.5, 2.4)]
    p.setBrush(QColor("#1b1304"))
    for x, half in bars:
        p.drawRoundedRect(QRectF(x - 1.2, 16 - half, 2.4, half * 2), 1.2, 1.2)
    p.setPen(QPen(QColor(255, 255, 255, 235), 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(16.0, 4.5), QPointF(16.0, 27.5))
    p.end()
    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(dpr)
    return pm


def app_icon() -> QIcon:
    ic = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        ic.addPixmap(logo_pixmap(size, 1.0))
    return ic
