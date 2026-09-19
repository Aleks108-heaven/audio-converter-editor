"""Small shared widgets reused across every tool panel."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import core

FILE_FILTER = "Audio files (*.mp3 *.flac *.wav *.m4a *.wma *.aac *.mp2 *.amr *.ogg);;All files (*.*)"


def short_path(path: str, max_len: int = 60) -> str:
    """Shorten a long absolute path to '.../parent/file.ext' for display in status messages."""
    if len(path) <= max_len:
        return path
    p = Path(path)
    short = f".../{p.parent.name}/{p.name}" if p.parent.name else p.name
    return short if len(short) < len(path) else path


def format_duration(ms: int) -> str:
    total_s = max(0, ms) // 1000
    mins, secs = divmod(total_s, 60)
    return f"{mins}:{secs:02d}"


def panel_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("role", "panelTitle")
    return label


def section_title(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setProperty("role", "sectionTitle")
    return label


def muted(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("role", "muted")
    label.setWordWrap(True)
    return label


def hline() -> QFrame:
    frame = QFrame()
    frame.setProperty("role", "hline")
    frame.setFixedHeight(1)
    return frame


def accent_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setProperty("accent", True)
    btn.setCursor(Qt.PointingHandCursor)
    return btn


class PathRow(QWidget):
    """A labeled entry + browse button, for one file path."""

    def __init__(self, label: str, save: bool = False, parent=None):
        super().__init__(parent)
        self.save = save
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(label)
        lbl.setFixedWidth(90)
        layout.addWidget(lbl)
        self.edit = QLineEdit()
        self.edit.setMinimumWidth(340)
        layout.addWidget(self.edit)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        layout.addWidget(browse)
        layout.addStretch(1)

    def _browse(self) -> None:
        if self.save:
            path, _ = QFileDialog.getSaveFileName(self, "Choose output file", "", FILE_FILTER)
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Choose input file", "", FILE_FILTER)
        if path:
            self.edit.setText(path)
            self.edit.setCursorPosition(len(path))

    def get(self) -> str:
        return self.edit.text().strip()

    def set(self, value: str) -> None:
        self.edit.setText(value)


class DirRow(QWidget):
    """A labeled entry + browse button, for one output directory."""

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(label)
        lbl.setFixedWidth(90)
        layout.addWidget(lbl)
        self.edit = QLineEdit()
        self.edit.setMinimumWidth(340)
        layout.addWidget(self.edit)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        layout.addWidget(browse)
        layout.addStretch(1)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose output directory")
        if path:
            self.edit.setText(path)

    def get(self) -> str:
        return self.edit.text().strip()


class MultiPathBox(QWidget):
    """A list of input files with add/remove/reorder controls, in order."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.ExtendedSelection)
        self.list.setMinimumHeight(120)
        layout.addWidget(self.list, 1)

        btns = QVBoxLayout()
        btns.setSpacing(4)
        add_btn = QPushButton("Add...")
        add_btn.clicked.connect(self._add)
        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self._remove)
        up_btn = QPushButton("Up")
        up_btn.clicked.connect(lambda: self._move(-1))
        down_btn = QPushButton("Down")
        down_btn.clicked.connect(lambda: self._move(1))
        for b in (add_btn, remove_btn, up_btn, down_btn):
            btns.addWidget(b)
        btns.addStretch(1)
        layout.addLayout(btns)

        self.on_change = None

    def _notify(self) -> None:
        if self.on_change:
            self.on_change()

    def _add(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Add input files", "", FILE_FILTER)
        for path in paths:
            self.list.addItem(path)
        if paths:
            self._notify()

    def _remove(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self._notify()

    def _move(self, delta: int) -> None:
        rows = sorted({self.list.row(i) for i in self.list.selectedItems()})
        if not rows:
            return
        ordered = rows if delta < 0 else list(reversed(rows))
        for row in ordered:
            new_row = row + delta
            if 0 <= new_row < self.list.count():
                item = self.list.takeItem(row)
                self.list.insertItem(new_row, item)
                item.setSelected(True)
        self._notify()

    def get_all(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]


class StatusLabel(QLabel):
    """A status line that wraps instead of stretching the panel to fit long paths."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWordWrap(True)

    def ok(self, message: str) -> None:
        self.setProperty("role", "statusOk")
        self.setText(f"✓ {message}" if message else "")
        self._restyle()

    def fail(self, message: str) -> None:
        self.setProperty("role", "statusErr")
        self.setText(f"✗ {message}")
        self._restyle()

    def clear_status(self) -> None:
        self.setText("")

    def _restyle(self) -> None:
        self.style().unpolish(self)
        self.style().polish(self)


def run_safely(status: StatusLabel, action, success_message: str) -> None:
    try:
        action()
        status.ok(success_message)
    except core.AudioToolError as exc:
        status.fail(f"Error: {exc}")
    except Exception as exc:  # noqa: BLE001 - surface any unexpected failure to the user
        status.fail(f"Unexpected error: {exc}")
