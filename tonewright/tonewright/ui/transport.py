"""Transport bar: play controls, big time readout, selection/view fields, meter and zoom."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QFont, QMouseEvent
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QSlider, QToolButton, QVBoxLayout, QWidget)

from ..core.timefmt import format_time
from . import icons, theme
from .widgets import LevelMeter, TimeEdit, divider, icon_button, role_label
from ..i18n import tr


class BigTime(QLabel):
    clicked = Signal()

    def __init__(self) -> None:
        super().__init__("00:00:00.000")
        self.setFont(theme.mono_font(27, QFont.Weight.DemiBold))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(tr("Click to jump to a time"))

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        self.clicked.emit()


class TransportBar(QFrame):
    selectionEdited = Signal(float, float)       # start, end (seconds)
    volumeChanged = Signal(float)
    gotoRequested = Signal()

    def __init__(self, actions: dict[str, QAction], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setProperty("panel", True)
        self._actions = actions
        self._selection: Optional[tuple[float, float]] = None
        self._muted_from: Optional[int] = None
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 10, 16, 8)
        root.setSpacing(6)

        # ---- row 1: transport buttons, big time, meter and volume
        row1 = QHBoxLayout()
        row1.setSpacing(2)
        for name in ("go_start", "rewind"):
            row1.addWidget(self._tool(name, 20))
        self.play_btn = self._tool("play", 22, role="play")
        self.play_btn.setFixedSize(46, 46)
        self.play_btn.setIconSize(QSize(22, 22))
        row1.addSpacing(4)
        row1.addWidget(self.play_btn)
        row1.addSpacing(4)
        for name in ("stop", "loop", "forward", "go_end"):
            row1.addWidget(self._tool(name, 20))
        row1.addSpacing(8)
        row1.addWidget(self._tool("record", 20, role="rec"))
        row1.addStretch(1)
        box = QVBoxLayout()
        box.setSpacing(0)
        self.time = BigTime()
        self.time.clicked.connect(self.gotoRequested)
        caption = role_label(tr("POSITION"), "section")
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(self.time)
        box.addWidget(caption)
        row1.addLayout(box)
        row1.addStretch(1)
        self.meter = LevelMeter()
        self.meter.setFixedWidth(84)
        row1.addWidget(self.meter)
        self.mute_btn = icon_button("volume", tr("Mute"), size=18)
        self.mute_btn.clicked.connect(self._toggle_mute)
        row1.addWidget(self.mute_btn)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(90)
        self.volume.setFixedWidth(76)
        self.volume.setToolTip(tr("Playback volume"))
        self.volume.valueChanged.connect(lambda v: self.volumeChanged.emit(v / 100.0))
        row1.addWidget(self.volume)
        root.addLayout(row1)

        # ---- row 2: zoom buttons and the selection fields
        row2 = QHBoxLayout()
        row2.setSpacing(2)
        for name in ("zoom_in", "zoom_out", "zoom_selection", "zoom_fit", "zoom_samples"):
            row2.addWidget(self._tool(name, 18))
        row2.addSpacing(6)
        row2.addWidget(divider(vertical=True))
        row2.addSpacing(6)
        for name in ("vzoom_in", "vzoom_out"):
            row2.addWidget(self._tool(name, 18))
        row2.addStretch(1)
        sel = QGridLayout()
        sel.setHorizontalSpacing(6)
        sel.setVerticalSpacing(1)
        for col, text in enumerate((tr("START"), tr("END"), tr("LENGTH")), start=1):
            sel.addWidget(role_label(text, "section"), 0, col, alignment=Qt.AlignmentFlag.AlignRight)
        sel.addWidget(role_label(tr("Selection"), "dim"), 1, 0)
        self.sel_start, self.sel_end, self.sel_len = (TimeEdit() for _ in range(3))
        for box, name in ((self.sel_start, "Selection start"), (self.sel_end, "Selection end"), (self.sel_len, "Selection length")):
            box.setAccessibleName(tr(name))
        for i, w in enumerate((self.sel_start, self.sel_end, self.sel_len), start=1):
            w.setFixedWidth(112)
            sel.addWidget(w, 1, i)
        self.sel_start.valueEdited.connect(self._edited)
        self.sel_end.valueEdited.connect(self._edited)
        self.sel_len.valueEdited.connect(self._length_edited)
        row2.addLayout(sel)
        root.addLayout(row2)

        # ---- row 3: format info and the visible range
        row3 = QHBoxLayout()
        self.info = QLabel("")
        self.info.setProperty("role", "faint")
        row3.addWidget(self.info, 1)
        view_lab = role_label(tr("VIEW"), "section")
        self.view_text = QLabel("00:00:00.000 – 00:00:00.000")
        self.view_text.setFont(theme.mono_font(9))
        self.view_text.setProperty("role", "dim")
        row3.addWidget(view_lab)
        row3.addSpacing(6)
        row3.addWidget(self.view_text)
        root.addLayout(row3)
        self.set_selection(None)

    def _tool(self, action_name: str, size: int, role: Optional[str] = None) -> QToolButton:
        act = self._actions[action_name]
        b = QToolButton()
        b.setDefaultAction(act)
        b.setIconSize(QSize(size, size))
        b.setToolTip(f"{act.toolTip() or act.text()}" + (f"  ({act.shortcut().toString()})" if not act.shortcut().isEmpty() else ""))
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setAutoRaise(True)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        if role:
            b.setProperty("role", role)
        return b

    # ---- updates from the controller
    def set_position(self, seconds: float) -> None:
        self.time.setText(format_time(seconds))

    def set_selection(self, sel: Optional[tuple[float, float]], cursor: float = 0.0) -> None:
        self._selection = sel
        if sel:
            a, b = sel
            self.sel_start.setValue(a)
            self.sel_end.setValue(b)
            self.sel_len.setValue(b - a)
        else:
            self.sel_start.setValue(cursor)
            self.sel_end.setValue(cursor)
            self.sel_len.setValue(0.0)

    def set_view(self, a: float, b: float) -> None:
        self.view_text.setText(f"{format_time(a)} – {format_time(b)}")

    def set_info(self, text: str) -> None:
        self.info.setText(text)

    def set_playing(self, playing: bool) -> None:
        self.play_btn.setProperty("playing", playing)

    def feed_meter(self, left: float, right: float) -> None:
        self.meter.feed(left, right)

    def _edited(self) -> None:
        a, b = self.sel_start.value(), self.sel_end.value()
        if b < a:
            a, b = b, a
        self.selectionEdited.emit(a, b)

    def _length_edited(self, length: float) -> None:
        a = self.sel_start.value()
        self.selectionEdited.emit(a, a + length)

    def _toggle_mute(self) -> None:
        if self._muted_from is None:
            self._muted_from = self.volume.value()
            self.volume.setValue(0)
            self.mute_btn.setIcon(icons.icon("volume-mute", 18))
        else:
            self.volume.setValue(self._muted_from or 90)
            self._muted_from = None
            self.mute_btn.setIcon(icons.icon("volume", 18))
