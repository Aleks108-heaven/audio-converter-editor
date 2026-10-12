"""Design tokens and the Qt stylesheet built from them (dark and light)."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen

from .. import paths


@dataclass(frozen=True)
class Theme:
    name: str
    bg0: str        # window
    bg1: str        # panels
    bg2: str        # inputs, raised
    bg3: str        # hover
    line: str
    line_strong: str
    text: str
    text_dim: str
    text_faint: str
    accent: str
    accent_hover: str
    accent_press: str
    accent_fg: str          # text on top of the accent colour
    accent_soft: str        # translucent accent wash
    wave_bg: str
    wave: str
    wave_edge: str
    wave_dim: str
    grid: str
    sel: str                # selection wash (rgba)
    sel_edge: str
    playhead: str
    marker: str
    good: str
    bad: str
    warn: str
    shadow: str


DARK = Theme(
    name="dark", bg0="#111317", bg1="#171a20", bg2="#1f232b", bg3="#282d37",
    line="#272c35", line_strong="#363c49", text="#e9ebef", text_dim="#a3abb9", text_faint="#6b7383",
    accent="#ffb23e", accent_hover="#ffc366", accent_press="#e39a28", accent_fg="#1b1304",
    accent_soft="rgba(255,178,62,0.14)", wave_bg="#0d0f12", wave="#f2a63a", wave_edge="#ffd08a",
    wave_dim="#7b5420", grid="rgba(255,255,255,0.055)", sel="rgba(78,166,255,0.26)", sel_edge="#6bb4ff",
    playhead="#ffffff", marker="#ff6b81", good="#5fd38d", bad="#ff6b6b", warn="#ffcc66",
    shadow="rgba(0,0,0,0.45)")

LIGHT = Theme(
    name="light", bg0="#eceae6", bg1="#f7f5f2", bg2="#ffffff", bg3="#e9e6e0",
    line="#dcd8d0", line_strong="#c6c1b7", text="#23262b", text_dim="#5b6270", text_faint="#8f95a1",
    accent="#d9822b", accent_hover="#c97420", accent_press="#b56518", accent_fg="#ffffff",
    accent_soft="rgba(217,130,43,0.14)", wave_bg="#fbfaf8", wave="#c8721a", wave_edge="#8f4e0c",
    wave_dim="#e6c79a", grid="rgba(0,0,0,0.07)", sel="rgba(40,120,220,0.20)", sel_edge="#2878dc",
    playhead="#1b1d21", marker="#d6334f", good="#1f9a55", bad="#d33a3a", warn="#b8860b",
    shadow="rgba(0,0,0,0.18)")

THEMES = {"dark": DARK, "light": LIGHT}
_current = DARK

UI_FONT = "Segoe UI Variable Text"
UI_FONT_FALLBACK = "Segoe UI"
MONO_FONT = "Cascadia Mono"


def current() -> Theme:
    return _current


def qcolor(value: str) -> QColor:
    """Parse '#rrggbb' or 'rgba(r,g,b,a)' (alpha 0..1)."""
    if value.startswith("rgba"):
        r, g, b, a = [p.strip() for p in value[value.index("(") + 1:value.index(")")].split(",")]
        return QColor(int(r), int(g), int(b), int(round(float(a) * 255)))
    return QColor(value)


def ui_font(point_size: float = 10.0, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    f = QFont(UI_FONT)
    f.setFamilies([UI_FONT, UI_FONT_FALLBACK, "Arial"])
    f.setPointSizeF(point_size)
    f.setWeight(weight)
    return f


def mono_font(point_size: float = 10.0, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    f = QFont(MONO_FONT)
    f.setFamilies([MONO_FONT, "Consolas", "Courier New"])
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setPointSizeF(point_size)
    f.setWeight(weight)
    return f


# ------------------------------------------------------------------ generated images used by the stylesheet
def _asset_dir(t: Theme) -> Path:
    d = paths.config_dir() / "cache" / f"theme-{t.name}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _draw_png(path: Path, w: int, h: int, draw, scale: int) -> None:
    img = QImage(w * scale, h * scale, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(scale, scale)
    draw(p)
    p.end()
    img.save(str(path))


def _chevron(color: str, up: bool = False):
    def draw(p: QPainter) -> None:
        pen = QPen(qcolor(color), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        path = QPainterPath()
        if up:
            path.moveTo(2, 7), path.lineTo(6, 3), path.lineTo(10, 7)
        else:
            path.moveTo(2, 3), path.lineTo(6, 7), path.lineTo(10, 3)
        p.drawPath(path)
    return draw


def _chevron_right(color: str):
    def draw(p: QPainter) -> None:
        p.setPen(QPen(qcolor(color), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        path = QPainterPath()
        path.moveTo(3, 2), path.lineTo(7, 6), path.lineTo(3, 10)
        p.drawPath(path)
    return draw


def _check(color: str):
    def draw(p: QPainter) -> None:
        p.setPen(QPen(qcolor(color), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        path = QPainterPath()
        path.moveTo(3.2, 7.4), path.lineTo(6.0, 10.2), path.lineTo(11.0, 4.2)
        p.drawPath(path)
    return draw


def build_assets(t: Theme) -> dict[str, str]:
    """Write the small PNGs the stylesheet references and return their (forward-slash) paths."""
    d = _asset_dir(t)
    specs = {
        "chev_down": (12, 10, _chevron(t.text_dim)),
        "chev_down_hi": (12, 10, _chevron(t.text)),
        "chev_up": (12, 10, _chevron(t.text_dim, up=True)),
        "chev_right": (12, 12, _chevron_right(t.text_dim)),
        "chev_down_tree": (12, 12, lambda p: _chevron(t.text_dim)(p)),
        "check": (14, 14, _check(t.accent_fg)),
    }
    out: dict[str, str] = {}
    for name, (w, h, draw) in specs.items():
        for scale, suffix in ((1, ""), (2, "@2x")):
            _draw_png(d / f"{name}{suffix}.png", w, h, draw, scale)
        out[name] = (d / f"{name}.png").as_posix()
    return out


def build_qss(t: Theme, a: dict[str, str]) -> str:
    fam = f'"{UI_FONT}", "{UI_FONT_FALLBACK}", Arial'
    return f"""
* {{ font-family: {fam}; outline: none; }}
QWidget {{ color: {t.text}; background: transparent; selection-background-color: {t.accent}; selection-color: {t.accent_fg}; }}
QMainWindow, QDialog {{ background: {t.bg0}; }}
QToolTip {{ background: {t.bg3}; color: {t.text}; border: 1px solid {t.line_strong}; padding: 5px 8px; border-radius: 6px; }}

QLabel[role="faint"] {{ color: {t.text_faint}; }}
QLabel[role="dim"] {{ color: {t.text_dim}; }}
QLabel[role="section"] {{ color: {t.text_faint}; font-size: 8pt; font-weight: 600; letter-spacing: 1.2px; }}
QLabel[role="title"] {{ font-size: 15pt; font-weight: 600; }}
QLabel[role="h2"] {{ font-size: 11pt; font-weight: 600; }}
QLabel[role="good"] {{ color: {t.good}; }}
QLabel[role="bad"] {{ color: {t.bad}; }}
QLabel[role="warn"] {{ color: {t.warn}; }}

QFrame[panel="true"] {{ background: {t.bg1}; border: none; }}
QFrame[card="true"] {{ background: {t.bg1}; border: 1px solid {t.line}; border-radius: 10px; }}
QFrame[role="divider"] {{ background: {t.line}; border: none; min-height: 1px; max-height: 1px; }}
QFrame[role="vdivider"] {{ background: {t.line}; border: none; min-width: 1px; max-width: 1px; }}

QPushButton {{ background: {t.bg2}; color: {t.text}; border: 1px solid {t.line_strong}; border-radius: 7px;
    padding: 6px 14px; min-height: 18px; }}
QPushButton:hover {{ background: {t.bg3}; border-color: {t.text_faint}; }}
QPushButton:pressed {{ background: {t.line}; }}
QPushButton:disabled {{ color: {t.text_faint}; background: {t.bg1}; border-color: {t.line}; }}
QPushButton:focus {{ border-color: {t.accent}; }}
QPushButton[variant="primary"] {{ background: {t.accent}; color: {t.accent_fg}; border: 1px solid {t.accent}; font-weight: 600; }}
QPushButton[variant="primary"]:hover {{ background: {t.accent_hover}; border-color: {t.accent_hover}; }}
QPushButton[variant="primary"]:pressed {{ background: {t.accent_press}; }}
QPushButton[variant="primary"]:disabled {{ background: {t.bg3}; color: {t.text_faint}; border-color: {t.bg3}; }}
QPushButton[variant="ghost"] {{ background: transparent; border: 1px solid transparent; color: {t.text_dim}; padding: 5px 10px; }}
QPushButton[variant="ghost"]:hover {{ background: {t.bg3}; color: {t.text}; }}
QPushButton[variant="danger"] {{ color: {t.bad}; }}
QPushButton[variant="danger"]:hover {{ background: {t.bad}; color: #ffffff; border-color: {t.bad}; }}
QPushButton[variant="chip"] {{ background: {t.bg2}; border: 1px solid {t.line}; border-radius: 8px; padding: 8px 6px; font-weight: 600; color: {t.text_dim}; }}
QPushButton[variant="chip"]:hover {{ border-color: {t.text_faint}; color: {t.text}; }}
QPushButton[variant="chip"]:checked {{ background: {t.accent_soft}; border: 1px solid {t.accent}; color: {t.accent}; }}
QPushButton::menu-indicator {{ image: none; width: 0px; }}

QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 7px; padding: 5px; color: {t.text_dim}; }}
QToolButton:hover {{ background: {t.bg3}; color: {t.text}; }}
QToolButton:pressed {{ background: {t.line}; }}
QToolButton:checked {{ background: {t.accent_soft}; color: {t.accent}; border-color: transparent; }}
QToolButton:disabled {{ color: {t.text_faint}; }}
QToolButton::menu-indicator {{ image: url({a["chev_down"]}); subcontrol-origin: padding; subcontrol-position: right bottom; width: 8px; height: 8px; right: 2px; bottom: 2px; }}
QToolButton::menu-button {{ border: none; background: transparent; width: 13px; }}
QToolButton::menu-arrow {{ image: url({a["chev_down"]}); width: 9px; height: 8px; }}
QToolButton[role="tab"] {{ padding: 8px 18px; border-radius: 9px; font-weight: 600; color: {t.text_dim}; }}
QToolButton[role="tab"]:hover {{ background: {t.bg3}; color: {t.text}; }}
QToolButton[role="tab"]:checked {{ background: {t.accent}; color: {t.accent_fg}; }}
QToolButton[role="play"] {{ background: {t.accent}; color: {t.accent_fg}; border-radius: 22px; padding: 0px; }}
QToolButton[role="play"]:hover {{ background: {t.accent_hover}; }}
QToolButton[role="play"]:disabled {{ background: {t.bg3}; }}
QToolButton[role="rec"] {{ color: {t.bad}; }}
QToolButton[role="rec"]:hover {{ background: {t.bad}; color: #ffffff; }}

QLineEdit, QTextEdit, QPlainTextEdit {{ background: {t.bg2}; border: 1px solid {t.line_strong}; border-radius: 7px; padding: 6px 9px; selection-background-color: {t.accent}; }}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{ border-color: {t.accent}; }}
QLineEdit:disabled {{ color: {t.text_faint}; background: {t.bg1}; }}
QLineEdit[role="time"] {{ font-family: "{MONO_FONT}", Consolas; padding: 4px 6px; border-radius: 5px; }}

QComboBox {{ background: {t.bg2}; border: 1px solid {t.line_strong}; border-radius: 7px; padding: 5px 30px 5px 10px; min-height: 20px; }}
QComboBox:hover {{ border-color: {t.text_faint}; }}
QComboBox:focus, QComboBox:on {{ border-color: {t.accent}; }}
QComboBox:disabled {{ color: {t.text_faint}; background: {t.bg1}; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 26px; border: none; }}
QComboBox::down-arrow {{ image: url({a["chev_down"]}); width: 12px; height: 10px; }}
QComboBox QAbstractItemView {{ background: {t.bg2}; border: 1px solid {t.line_strong}; border-radius: 8px; padding: 4px; outline: none;
    selection-background-color: {t.accent_soft}; selection-color: {t.text}; }}
QComboBox QAbstractItemView::item {{ padding: 6px 10px; border-radius: 5px; min-height: 22px; }}
QComboBox QAbstractItemView::item:hover {{ background: {t.bg3}; }}

QSpinBox, QDoubleSpinBox {{ background: {t.bg2}; border: 1px solid {t.line_strong}; border-radius: 7px; padding: 5px 8px; padding-right: 22px; min-height: 20px; }}
QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {t.accent}; }}
QSpinBox:disabled, QDoubleSpinBox:disabled {{ color: {t.text_faint}; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 20px; border: none; background: transparent; margin: 2px 2px 0 0; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; width: 20px; border: none; background: transparent; margin: 0 2px 2px 0; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({a["chev_up"]}); width: 10px; height: 8px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url({a["chev_down"]}); width: 10px; height: 8px; }}

QCheckBox, QRadioButton {{ spacing: 9px; padding: 3px 0; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {t.text_faint}; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border: 1.5px solid {t.text_faint}; border-radius: 5px; background: {t.bg2}; }}
QCheckBox::indicator:hover {{ border-color: {t.text_dim}; }}
QCheckBox::indicator:checked {{ background: {t.accent}; border-color: {t.accent}; image: url({a["check"]}); }}
QCheckBox::indicator:disabled {{ border-color: {t.line}; }}
QRadioButton::indicator {{ width: 16px; height: 16px; border: 1.5px solid {t.text_faint}; border-radius: 9px; background: {t.bg2}; }}
QRadioButton::indicator:hover {{ border-color: {t.text_dim}; }}
QRadioButton::indicator:checked {{ border-color: {t.accent}; background: qradialgradient(cx:.5, cy:.5, radius:.5, fx:.5, fy:.5, stop:0 {t.accent}, stop:.5 {t.accent}, stop:.56 {t.bg2}, stop:1 {t.bg2}); }}

QSlider::groove:horizontal {{ height: 4px; background: {t.line_strong}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {t.accent}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {t.text}; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; border: 2px solid {t.bg1}; }}
QSlider::handle:horizontal:hover {{ background: {t.accent_hover}; }}
QSlider::groove:vertical {{ width: 4px; background: {t.line_strong}; border-radius: 2px; }}
QSlider::add-page:vertical {{ background: {t.line_strong}; border-radius: 2px; }}
QSlider::sub-page:vertical {{ background: {t.accent}; border-radius: 2px; }}
QSlider::handle:vertical {{ background: {t.text}; height: 14px; width: 14px; margin: 0 -5px; border-radius: 7px; border: 2px solid {t.bg1}; }}
QSlider::handle:vertical:hover {{ background: {t.accent_hover}; }}
QSlider:disabled {{ opacity: 0.4; }}

QProgressBar {{ background: {t.bg2}; border: none; border-radius: 6px; min-height: 12px; max-height: 12px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {t.accent}; border-radius: 6px; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t.line_strong}; border-radius: 4px; min-height: 36px; margin: 0 2px; }}
QScrollBar::handle:vertical:hover {{ background: {t.text_faint}; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {t.line_strong}; border-radius: 4px; min-width: 36px; margin: 2px 0; }}
QScrollBar::handle:horizontal:hover {{ background: {t.text_faint}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: none; width: 0; height: 0; }}

QSplitter::handle {{ background: {t.line}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QSplitter::handle:hover {{ background: {t.accent}; }}

QMenuBar {{ background: transparent; padding: 0; spacing: 2px; }}
QMenuBar::item {{ background: transparent; padding: 6px 11px; border-radius: 6px; color: {t.text_dim}; }}
QMenuBar::item:selected {{ background: {t.bg3}; color: {t.text}; }}
QMenu {{ background: {t.bg2}; border: 1px solid {t.line_strong}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 28px 7px 12px; border-radius: 6px; min-width: 150px; }}
QMenu::item:selected {{ background: {t.accent_soft}; color: {t.text}; }}
QMenu::item:disabled {{ color: {t.text_faint}; }}
QMenu::separator {{ height: 1px; background: {t.line}; margin: 5px 8px; }}
QMenu::indicator {{ width: 14px; height: 14px; margin-left: 6px; }}
QMenu::right-arrow {{ image: url({a["chev_right"]}); width: 10px; height: 10px; margin-right: 8px; }}
QMenu::icon {{ padding-left: 6px; }}

QTabWidget::pane {{ border: none; border-top: 1px solid {t.line}; top: -1px; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{ background: transparent; color: {t.text_dim}; padding: 9px 8px; margin-right: 0px; border-bottom: 2px solid transparent; font-weight: 600; }}
QTabBar::tab:hover {{ color: {t.text}; }}
QTabBar::tab:selected {{ color: {t.accent}; border-bottom: 2px solid {t.accent}; }}

QTreeView, QListView, QListWidget, QTreeWidget, QTableView, QTableWidget {{ background: transparent; border: none; alternate-background-color: transparent; outline: none; show-decoration-selected: 0; }}
QTreeView::item, QListView::item, QTreeWidget::item, QListWidget::item {{ padding: 5px 6px; border-radius: 6px; }}
QTreeView::item:hover, QListView::item:hover, QTreeWidget::item:hover, QListWidget::item:hover {{ background: {t.bg3}; }}
QTreeView::item:selected, QListView::item:selected, QTreeWidget::item:selected, QListWidget::item:selected {{ background: {t.accent_soft}; color: {t.text}; }}
QTreeView::item:selected:!active, QTreeWidget::item:selected:!active {{ background: {t.bg3}; }}
QTreeView::branch, QTreeView::branch:selected, QTreeView::branch:hover, QTreeView::branch:selected:active, QTreeView::branch:selected:!active {{ background: transparent; border: none; }}
QTreeView::branch:has-children:closed {{ image: url({a["chev_right"]}); }}
QTreeView::branch:has-children:open {{ image: url({a["chev_down_tree"]}); }}
QHeaderView {{ background: transparent; border: none; }}
QHeaderView::section {{ background: transparent; color: {t.text_faint}; border: none; border-bottom: 1px solid {t.line}; padding: 6px 8px; font-size: 8pt; font-weight: 600; }}
QTableWidget {{ gridline-color: {t.line}; }}
QTableWidget::item {{ padding: 4px 6px; }}
QTableWidget QLineEdit {{ padding: 2px 4px; border-radius: 3px; }}

QGroupBox {{ border: 1px solid {t.line}; border-radius: 10px; margin-top: 16px; padding: 14px 12px 10px 12px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {t.text_dim}; background: {t.bg1}; }}

QStatusBar {{ background: {t.bg1}; border-top: 1px solid {t.line}; color: {t.text_dim}; }}
QStatusBar::item {{ border: none; }}
QDialog QLabel[role="description"] {{ color: {t.text_dim}; }}
QMessageBox {{ background: {t.bg1}; }}
"""


def apply_theme(app, name: str = "dark") -> Theme:
    global _current
    _current = THEMES.get(name, DARK)
    if app.style().objectName().lower() != "fusion":
        app.setStyle("Fusion")        # the Windows 11 style paints its own accent-coloured selection bars
    assets = build_assets(_current)
    app.setStyleSheet(build_qss(_current, assets))
    palette = app.palette()
    palette.setColor(palette.ColorRole.Window, qcolor(_current.bg0))
    palette.setColor(palette.ColorRole.WindowText, qcolor(_current.text))
    palette.setColor(palette.ColorRole.Base, qcolor(_current.bg2))
    palette.setColor(palette.ColorRole.Text, qcolor(_current.text))
    palette.setColor(palette.ColorRole.Button, qcolor(_current.bg2))
    palette.setColor(palette.ColorRole.ButtonText, qcolor(_current.text))
    palette.setColor(palette.ColorRole.ToolTipBase, qcolor(_current.bg3))
    palette.setColor(palette.ColorRole.ToolTipText, qcolor(_current.text))
    palette.setColor(palette.ColorRole.Highlight, qcolor(_current.accent))
    palette.setColor(palette.ColorRole.HighlightedText, qcolor(_current.accent_fg))
    app.setPalette(palette)
    app.setFont(ui_font(10))
    return _current


def with_alpha(color: str, alpha: float) -> QColor:
    c = qcolor(color)
    c.setAlphaF(alpha)
    return c
