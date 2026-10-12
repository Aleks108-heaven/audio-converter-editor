"""Running long operations off the UI thread, with a modal progress card."""
from __future__ import annotations

import time
import traceback
from typing import Any, Callable, Optional

from PySide6.QtCore import QEventLoop, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QProgressBar,
                               QPushButton, QVBoxLayout, QWidget)

from ..core.common import Cancelled, CancelToken
from . import theme
from .widgets import make_button, role_label


class Worker(QThread):
    """Runs fn(progress, cancel) and keeps its result or error for the UI thread."""

    progressed = Signal(float, str)

    def __init__(self, fn: Callable, cancel: Optional[CancelToken] = None, parent=None) -> None:
        super().__init__(parent)
        self._fn = fn
        self.cancel = cancel or CancelToken()
        self.result: Any = None
        self.error: Optional[BaseException] = None
        self.trace = ""
        self.was_cancelled = False
        self._last = 0.0

    def report(self, frac: float, message: str = "") -> None:
        now = time.monotonic()
        if frac >= 1.0 or now - self._last >= 0.04:
            self._last = now
            self.progressed.emit(float(frac), message)

    def run(self) -> None:
        try:
            self.result = self._fn(self.report, self.cancel)
        except Cancelled:
            self.was_cancelled = True
        except BaseException as exc:  # noqa: BLE001 - re-raised on the UI thread
            if self.cancel.cancelled:
                self.was_cancelled = True
            else:
                self.error = exc
                self.trace = traceback.format_exc()


class ProgressCard(QDialog):
    cancelRequested = Signal()

    def __init__(self, parent: Optional[QWidget], title: str, message: str, cancellable: bool) -> None:
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setModal(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._cancellable = cancellable
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 22, 28, 34)
        card = QFrame()
        card.setProperty("card", True)
        card.setStyleSheet(f"QFrame[card=\"true\"] {{ background: {theme.current().bg2}; border: 1px solid {theme.current().line_strong}; border-radius: 14px; }}")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(42)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(0, 0, 0, 150))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(10)
        self.title = QLabel(title)
        self.title.setProperty("role", "h2")
        self.message = QLabel(message)
        self.message.setProperty("role", "dim")
        self.message.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)               # busy until the first real progress arrives
        self.percent = QLabel("")
        self.percent.setProperty("role", "dim")
        self.percent.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.cancel_btn = make_button("Cancel")
        self.cancel_btn.clicked.connect(self._cancel)
        self.cancel_btn.setVisible(cancellable)
        row = QHBoxLayout()
        row.addWidget(self.percent, 1)
        lay.addWidget(self.title)
        lay.addWidget(self.message)
        lay.addSpacing(4)
        lay.addWidget(self.bar)
        lay.addLayout(row)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self.cancel_btn)
        lay.addLayout(btn_row)
        self.setFixedWidth(430)

    def set_progress(self, frac: float, message: str = "") -> None:
        if self.bar.maximum() == 0:
            self.bar.setRange(0, 1000)
        self.bar.setValue(int(max(0.0, min(1.0, frac)) * 1000))
        self.percent.setText(f"{int(frac * 100)} %")
        if message:
            self.message.setText(message)

    def _cancel(self) -> None:
        if self._cancellable:
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.setText("Cancelling…")
            self.cancelRequested.emit()

    def reject(self) -> None:
        self._cancel()

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        p = self.parentWidget()
        if p is not None:
            g = p.window().frameGeometry()
            self.adjustSize()
            self.move(g.center().x() - self.width() // 2, g.center().y() - self.height() // 2)


def run_task(parent: Optional[QWidget], title: str, fn: Callable, *, message: str = "Operation in progress…",
             cancellable: bool = True, delay_ms: int = 250) -> tuple[bool, Any]:
    """Run fn(progress, cancel) on a worker thread while showing a progress card.

    Returns (True, result), or (False, None) if the user cancelled. Exceptions from fn are
    re-raised here on the UI thread. The window stays responsive but input is blocked.
    """
    worker = Worker(fn)
    card = ProgressCard(parent, title, message, cancellable)
    loop = QEventLoop()
    worker.progressed.connect(card.set_progress, Qt.ConnectionType.QueuedConnection)
    worker.finished.connect(loop.quit, Qt.ConnectionType.QueuedConnection)
    card.cancelRequested.connect(worker.cancel.cancel)
    card.setWindowOpacity(0.0)
    card.show()
    reveal = QTimer(card)                 # owned by the card, so it cannot outlive it
    reveal.setSingleShot(True)
    reveal.timeout.connect(lambda: card.setWindowOpacity(1.0))
    reveal.start(delay_ms)
    worker.start()
    loop.exec()
    worker.wait()
    card.hide()
    card.deleteLater()
    if worker.error is not None:
        raise worker.error
    if worker.was_cancelled:
        return False, None
    return True, worker.result


_background: set[Worker] = set()


def run_background(fn: Callable, on_done: Optional[Callable[[Any], None]] = None,
                   on_error: Optional[Callable[[BaseException], None]] = None,
                   on_progress: Optional[Callable[[float, str], None]] = None) -> Worker:
    """Run fn(progress, cancel) without blocking; callbacks fire on the UI thread."""
    worker = Worker(fn)
    _background.add(worker)

    def finished() -> None:
        _background.discard(worker)
        if worker.error is not None:
            if on_error:
                on_error(worker.error)
        elif not worker.was_cancelled and on_done:
            on_done(worker.result)
        worker.deleteLater()

    if on_progress:
        worker.progressed.connect(on_progress, Qt.ConnectionType.QueuedConnection)
    worker.finished.connect(finished, Qt.ConnectionType.QueuedConnection)
    worker.start()
    return worker
