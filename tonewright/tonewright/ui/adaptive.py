"""Bars that notice their width, so that what no longer fits is dropped cleanly instead of being cut in half."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget


class AdaptiveBar(QWidget):
    """A plain bar that says so when its width changes (the owner then hides or shrinks what does not fit)."""
    resized = Signal()

    def resizeEvent(self, event) -> None:               # noqa: N802 - Qt's name
        super().resizeEvent(event)
        self.resized.emit()
