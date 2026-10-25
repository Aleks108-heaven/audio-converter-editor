"""Names for the controls that have none, so that a screen reader (Narrator, NVDA, VoiceOver) can say what they are.

A button that shows only an icon has a tooltip but no text, and a screen reader announces it as just "button". Two
helpers close that gap: `install` makes every such control take its tooltip as its name, and `label_for` ties a form
row's text label to the field it describes."""
from __future__ import annotations

import re
from typing import Optional

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import (QAbstractButton, QAbstractSlider, QAbstractSpinBox, QApplication, QComboBox, QLabel, QLineEdit,
                               QPlainTextEdit, QWidget)

_CONTROLS = (QAbstractButton, QAbstractSlider, QAbstractSpinBox, QComboBox, QLineEdit, QPlainTextEdit)
_SHORTCUT_HINT = re.compile(r"\s*\((?:Ctrl|Shift|Alt|Del|Cmd|⌘)[^)]*\)\s*$|\s{2,}\([^)]*\)\s*$")
_AUTO = "autoAccessibleName"


def plain(text: str) -> str:
    """A tooltip or label as a name: no "&" mnemonics, no "(Ctrl+K)" shortcut hint, no trailing colon."""
    return _SHORTCUT_HINT.sub("", (text or "").replace("&", "")).strip().rstrip(":").strip()


def name_from_tooltip(widget: QWidget) -> None:
    """Give an unnamed icon-only control its tooltip as its name (and keep it in step when the tooltip changes)."""
    if not isinstance(widget, _CONTROLS):
        return
    current = widget.accessibleName()
    if current and not widget.property(_AUTO):
        return                                              # someone named it on purpose
    if isinstance(widget, QAbstractButton) and widget.text().strip():
        return                                              # its own text is its name
    if isinstance(widget, QLineEdit) and widget.objectName() == "qt_spinbox_lineedit":
        return                                              # the spin box around it carries the name
    name = plain(widget.toolTip())
    if not name and isinstance(widget, QLineEdit):
        name = plain(widget.placeholderText()).rstrip("…").strip()      # a search box that says "Search effects…" in grey
    if name and name != current:
        widget.setAccessibleName(name)
        widget.setProperty(_AUTO, True)


class _Namer(QObject):
    def eventFilter(self, obj, event) -> bool:              # noqa: N802 - Qt's name
        kind = event.type()
        if (kind == QEvent.Type.Polish or kind == QEvent.Type.ToolTipChange) and isinstance(obj, QWidget):
            name_from_tooltip(obj)
        return False


_namer: Optional[_Namer] = None


def install(app: QApplication) -> None:
    """Name controls as they appear and as their tooltips change. Safe to call more than once."""
    global _namer
    if _namer is None:
        _namer = _Namer(app)
        app.installEventFilter(_namer)


def label_for(label: QLabel, widget: QWidget) -> None:
    """Tie `label` to the control it describes: the control (or the first one inside `widget`, when that is a wrapper)
    takes the label's text as its name, and the label becomes its buddy."""
    target: Optional[QWidget] = widget if isinstance(widget, _CONTROLS) else None
    if target is None:
        for kind in (QComboBox, QAbstractSpinBox, QLineEdit, QPlainTextEdit, QAbstractSlider, QAbstractButton):
            target = widget.findChild(kind)
            if target is not None:
                break
    if target is None:
        return
    label.setBuddy(target)
    if not target.accessibleName() or target.property(_AUTO):
        target.setAccessibleName(plain(label.text()))
        target.setProperty(_AUTO, False)
