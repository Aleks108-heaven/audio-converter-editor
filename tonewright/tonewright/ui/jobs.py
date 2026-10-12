"""Long-running jobs (conversion, audiobook) with progress, pause and cancel, plus their action bar."""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

from ..core.common import Cancelled, CancelToken, PauseToken
from . import icons
from .widgets import make_button


class Job(QThread):
    """Runs fn(job); the function reports through the job's signals and honours cancel/pause."""

    progress = Signal(float)
    itemProgress = Signal(int, float)
    itemResult = Signal(int, object)
    completed = Signal(object, object, bool)       # result, error, cancelled

    def __init__(self, fn: Callable[["Job"], Any]) -> None:
        super().__init__()
        self._fn = fn
        self.cancel = CancelToken()
        self.pause = PauseToken()
        self._last = {"p": 0.0}

    def report(self, frac: float) -> None:
        now = time.monotonic()
        if frac >= 1.0 or now - self._last["p"] > 0.05:
            self._last["p"] = now
            self.progress.emit(float(frac))

    def report_item(self, index: int, frac: float) -> None:
        now = time.monotonic()
        key = f"i{index}"
        if frac >= 1.0 or now - self._last.get(key, 0.0) > 0.08:
            self._last[key] = now
            self.itemProgress.emit(index, float(frac))

    def run(self) -> None:
        try:
            result = self._fn(self)
            self.completed.emit(result, None, False)
        except Cancelled:
            self.completed.emit(None, None, True)
        except BaseException as exc:  # noqa: BLE001 - reported to the UI
            self.completed.emit(None, exc, self.cancel.cancelled)


class JobBar(QFrame):
    """Output location, progress and the big action button."""

    primaryClicked = Signal()
    pauseToggled = Signal(bool)
    stopClicked = Signal()
    browseClicked = Signal()
    pathEdited = Signal(str)

    def __init__(self, primary_text: str, path_label: str = "Output folder", *, same_folder_option: bool = False) -> None:
        super().__init__()
        self.setProperty("panel", True)
        self._primary_text = primary_text
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 10, 16, 10)
        lay.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(10)
        lab = QLabel(path_label)
        lab.setProperty("role", "dim")
        self.path = QLineEdit()
        self.path.setMinimumWidth(240)
        self.path.editingFinished.connect(lambda: self.pathEdited.emit(self.path.text()))
        self.browse = make_button("Browse…", icon="folder")
        self.browse.clicked.connect(self.browseClicked)
        top.addWidget(lab)
        top.addWidget(self.path, 1)
        top.addWidget(self.browse)
        self.same_folder: Optional[QCheckBox] = None
        if same_folder_option:
            self.same_folder = QCheckBox("Same folder as the source")
            top.addWidget(self.same_folder)
            self.same_folder.toggled.connect(self._same_toggled)
        lay.addLayout(top)

        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        info = QVBoxLayout()
        info.setSpacing(4)
        prog = QHBoxLayout()
        prog.setSpacing(10)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.percent = QLabel("")
        self.percent.setProperty("role", "dim")
        self.percent.setFixedWidth(120)
        prog.addWidget(self.bar, 1)
        prog.addWidget(self.percent)
        info.addLayout(prog)
        self.status = QLabel("")
        self.status.setProperty("role", "dim")
        info.addWidget(self.status)
        bottom.addLayout(info, 1)
        self.stop_btn = make_button("Stop", "danger", "stop")
        self.stop_btn.setVisible(False)
        self.pause_btn = make_button("Pause", icon="pause")
        self.pause_btn.setVisible(False)
        self.primary = QPushButton(primary_text)
        self.primary.setProperty("variant", "primary")
        self.primary.setMinimumSize(190, 44)
        self.primary.setCursor(Qt.CursorShape.PointingHandCursor)
        bottom.addWidget(self.stop_btn)
        bottom.addWidget(self.pause_btn)
        bottom.addWidget(self.primary)
        lay.addLayout(bottom)
        self.primary.clicked.connect(self.primaryClicked)
        self.stop_btn.clicked.connect(self.stopClicked)
        self.pause_btn.clicked.connect(self._toggle_pause)
        self._paused = False

    def _same_toggled(self, on: bool) -> None:
        self.path.setEnabled(not on)
        self.browse.setEnabled(not on)

    def set_running(self, running: bool) -> None:
        self.primary.setVisible(not running)
        self.pause_btn.setVisible(running)
        self.stop_btn.setVisible(running)
        for w in (self.path, self.browse):
            w.setEnabled(not running and not (self.same_folder is not None and self.same_folder.isChecked()))
        if self.same_folder is not None:
            self.same_folder.setEnabled(not running)
        if running:
            self._paused = False
            self.pause_btn.setText("Pause")
            self.pause_btn.setIcon(icons.icon("pause", 16))

    def set_progress(self, frac: float, text: Optional[str] = None) -> None:
        self.bar.setValue(int(max(0.0, min(1.0, frac)) * 1000))
        self.percent.setText(text if text is not None else f"Completed {int(frac * 100)} %")

    def reset_progress(self) -> None:
        self.bar.setValue(0)
        self.percent.setText("")

    def set_status(self, text: str, role: str = "dim") -> None:
        self.status.setText(text)
        self.status.setProperty("role", role)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self.pause_btn.setText("Resume" if self._paused else "Pause")
        self.pause_btn.setIcon(icons.icon("play" if self._paused else "pause", 16))
        self.pauseToggled.emit(self._paused)
