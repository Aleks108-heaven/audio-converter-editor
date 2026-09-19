"""Dark, AVS-style color palette and global stylesheet.

Every color used anywhere in the GUI is defined here so the app reads as
one consistent system instead of per-widget one-off styling.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

# -- Palette -----------------------------------------------------------------

BG = "#1b1d23"              # window background
PANEL_BG = "#22252c"        # cards / side panel
FIELD_BG = "#2a2e37"        # inputs, list boxes
FIELD_BG_HOVER = "#31353f"
BORDER = "#383c46"
BORDER_LIGHT = "#454a56"

ACCENT = "#4a90ff"
ACCENT_HOVER = "#6aa4ff"
ACCENT_PRESSED = "#3878e0"
ACCENT_DISABLED = "#3a4257"

OK = "#3ddc84"
ERR = "#ff6b6b"
WARN = "#e8a33d"

TEXT = "#e7e9ee"
TEXT_MUTED = "#9aa2b1"
TEXT_DIM = "#6b7280"

WAVE_BG = "#101217"
WAVE_RULER_BG = "#161920"
WAVE_FG = "#4a90ff"
WAVE_FG_DIM = "#2c4a80"
WAVE_SELECTION = "#4a90ff"
WAVE_MARKER = WARN
WAVE_PLAYHEAD = "#ff5a5f"
WAVE_CENTER_LINE = "#2a2e37"

FONT_FAMILY = "Segoe UI"
MONO_FAMILY = "Consolas"


def apply_theme(app: QApplication) -> None:
    """Dark palette (so native dialogs match) plus a QSS stylesheet for the
    flat, bordered, AVS-ish look of the custom widgets."""
    app.setStyle("Fusion")

    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(BG))
    palette.setColor(QPalette.WindowText, QColor(TEXT))
    palette.setColor(QPalette.Base, QColor(FIELD_BG))
    palette.setColor(QPalette.AlternateBase, QColor(PANEL_BG))
    palette.setColor(QPalette.ToolTipBase, QColor(PANEL_BG))
    palette.setColor(QPalette.ToolTipText, QColor(TEXT))
    palette.setColor(QPalette.Text, QColor(TEXT))
    palette.setColor(QPalette.Button, QColor(FIELD_BG))
    palette.setColor(QPalette.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.BrightText, QColor(ERR))
    palette.setColor(QPalette.Link, QColor(ACCENT))
    palette.setColor(QPalette.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(TEXT_DIM))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(TEXT_DIM))
    palette.setColor(QPalette.PlaceholderText, QColor(TEXT_DIM))
    app.setPalette(palette)

    default_font = QFont(FONT_FAMILY, 10)
    app.setFont(default_font)

    app.setStyleSheet(f"""
        QWidget {{
            background: {BG};
            color: {TEXT};
        }}
        QMainWindow, #centralWidget {{
            background: {BG};
        }}
        QToolTip {{
            background: {PANEL_BG};
            color: {TEXT};
            border: 1px solid {BORDER_LIGHT};
            padding: 4px 6px;
        }}

        /* -- Cards -------------------------------------------------------- */
        QFrame#Card {{
            background: {PANEL_BG};
            border: 1px solid {BORDER};
            border-radius: 8px;
        }}
        QFrame#Toolbar {{
            background: {PANEL_BG};
            border-bottom: 1px solid {BORDER};
        }}

        /* -- Labels --------------------------------------------------------*/
        QLabel {{ background: transparent; }}
        QLabel[role="title"] {{
            font-size: 14pt;
            font-weight: 600;
            color: {TEXT};
        }}
        QLabel[role="panelTitle"] {{
            font-size: 13pt;
            font-weight: 600;
            color: {TEXT};
        }}
        QLabel[role="sectionTitle"] {{
            font-size: 10pt;
            font-weight: 600;
            color: {TEXT_MUTED};
            letter-spacing: 0.5px;
        }}
        QLabel[role="muted"] {{ color: {TEXT_MUTED}; }}
        QLabel[role="statusOk"] {{ color: {OK}; font-weight: 500; }}
        QLabel[role="statusErr"] {{ color: {ERR}; font-weight: 500; }}

        /* -- Buttons ---------------------------------------------------- */
        QPushButton {{
            background: {FIELD_BG};
            color: {TEXT};
            border: 1px solid {BORDER_LIGHT};
            border-radius: 5px;
            padding: 6px 14px;
        }}
        QPushButton:hover {{ background: {FIELD_BG_HOVER}; }}
        QPushButton:pressed {{ background: {BORDER}; }}
        QPushButton:disabled {{ color: {TEXT_DIM}; border-color: {BORDER}; }}
        QPushButton[accent="true"] {{
            background: {ACCENT};
            border: 1px solid {ACCENT};
            color: white;
            font-weight: 600;
        }}
        QPushButton[accent="true"]:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
        QPushButton[accent="true"]:pressed {{ background: {ACCENT_PRESSED}; }}
        QPushButton[accent="true"]:disabled {{ background: {ACCENT_DISABLED}; border-color: {ACCENT_DISABLED}; color: #c7cede; }}
        QPushButton[flat="true"] {{
            background: transparent;
            border: none;
            color: {TEXT_MUTED};
            padding: 4px 8px;
        }}
        QPushButton[flat="true"]:hover {{ color: {TEXT}; background: {FIELD_BG}; }}

        /* -- Inputs ------------------------------------------------------ */
        QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
            background: {FIELD_BG};
            border: 1px solid {BORDER_LIGHT};
            border-radius: 4px;
            padding: 5px 7px;
            selection-background-color: {ACCENT};
        }}
        QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
            border: 1px solid {ACCENT};
        }}
        QComboBox::drop-down {{ border: none; width: 22px; }}
        QComboBox QAbstractItemView {{
            background: {FIELD_BG};
            border: 1px solid {BORDER_LIGHT};
            selection-background-color: {ACCENT};
            outline: none;
        }}

        /* -- Lists / trees ------------------------------------------------ */
        QListWidget, QTreeWidget {{
            background: {FIELD_BG};
            border: 1px solid {BORDER_LIGHT};
            border-radius: 4px;
            outline: none;
        }}
        QListWidget::item, QTreeWidget::item {{
            padding: 4px 4px;
            border: none;
        }}
        QListWidget::item:selected, QTreeWidget::item:selected {{
            background: {ACCENT};
            color: white;
        }}
        QTreeWidget::item:hover:!selected {{ background: {FIELD_BG_HOVER}; }}
        QHeaderView::section {{
            background: {PANEL_BG};
            color: {TEXT_MUTED};
            border: none;
            border-bottom: 1px solid {BORDER};
            padding: 4px;
        }}

        /* -- Tool nav tree specifically ----------------------------------- */
        QTreeWidget#ToolTree {{
            background: {PANEL_BG};
            border: none;
        }}
        QTreeWidget#ToolTree::item {{
            padding: 6px 4px;
            border-radius: 4px;
            margin: 1px 4px;
        }}
        QTreeWidget#ToolTree::item:selected {{
            background: {ACCENT};
            color: white;
        }}
        QTreeWidget#ToolTree::item:hover:!selected {{ background: {FIELD_BG_HOVER}; }}
        QTreeWidget#ToolTree::branch {{ background: transparent; }}

        /* -- Scrollbars ---------------------------------------------------- */
        QScrollBar:vertical {{
            background: transparent;
            width: 11px;
            margin: 2px;
        }}
        QScrollBar::handle:vertical {{
            background: {BORDER_LIGHT};
            border-radius: 4px;
            min-height: 24px;
        }}
        QScrollBar::handle:vertical:hover {{ background: {TEXT_DIM}; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QScrollBar:horizontal {{
            background: transparent;
            height: 11px;
            margin: 2px;
        }}
        QScrollBar::handle:horizontal {{
            background: {BORDER_LIGHT};
            border-radius: 4px;
            min-width: 24px;
        }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

        /* -- Splitter ------------------------------------------------------ */
        QSplitter::handle {{ background: {BORDER}; }}
        QSplitter::handle:hover {{ background: {ACCENT}; }}

        /* -- Sliders (EQ bands, mixer gains) -------------------------------- */
        QSlider::groove:vertical {{
            background: {FIELD_BG};
            border: 1px solid {BORDER_LIGHT};
            width: 6px;
            border-radius: 3px;
        }}
        QSlider::handle:vertical {{
            background: {ACCENT};
            border: 1px solid {ACCENT_HOVER};
            height: 14px;
            margin: 0 -6px;
            border-radius: 7px;
        }}
        QSlider::handle:vertical:hover {{ background: {ACCENT_HOVER}; }}
        QSlider::groove:horizontal {{
            background: {FIELD_BG};
            border: 1px solid {BORDER_LIGHT};
            height: 6px;
            border-radius: 3px;
        }}
        QSlider::handle:horizontal {{
            background: {ACCENT};
            border: 1px solid {ACCENT_HOVER};
            width: 14px;
            margin: -6px 0;
            border-radius: 7px;
        }}

        /* -- Radio / checkboxes --------------------------------------------- */
        QRadioButton, QCheckBox {{ spacing: 6px; }}
        QRadioButton::indicator, QCheckBox::indicator {{
            width: 15px; height: 15px;
        }}

        /* -- Separator ------------------------------------------------------- */
        QFrame[role="hline"] {{
            background: {BORDER};
            max-height: 1px;
            min-height: 1px;
            border: none;
        }}
    """)
