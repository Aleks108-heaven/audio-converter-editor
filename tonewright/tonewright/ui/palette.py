"""Command palette: type to find and run any command or effect."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtGui import QColor, QFont, QKeyEvent, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QFrame, QGraphicsDropShadowEffect, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

from . import icons, theme
from ..i18n import tr


@dataclass
class Command:
    name: str
    group: str
    callback: Callable[[], None]
    shortcut: str = ""
    keywords: str = ""
    icon: str = ""
    enabled: bool = True


class _Delegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        s = super().sizeHint(option, index)
        s.setHeight(38)
        return s

    def paint(self, p: QPainter, option, index) -> None:
        t = theme.current()
        cmd: Command = index.data(Qt.ItemDataRole.UserRole)
        rect = option.rect.adjusted(6, 2, -6, -2)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.qcolor(t.accent_soft))
            p.drawRoundedRect(rect, 8, 8)
        color = t.text if cmd.enabled else t.text_faint
        if cmd.icon:
            p.drawPixmap(rect.left() + 10, rect.center().y() - 9,
                         icons.pixmap(cmd.icon, t.accent if selected else (t.text_dim if cmd.enabled else t.text_faint), 18))
        p.setFont(theme.ui_font(10.5, QFont.Weight.DemiBold if selected else QFont.Weight.Normal))
        p.setPen(theme.qcolor(color))
        p.drawText(rect.adjusted(40, 0, -150, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, cmd.name)
        p.setFont(theme.ui_font(9))
        p.setPen(theme.qcolor(t.text_faint))
        right = f"{cmd.shortcut}   " if cmd.shortcut else ""
        p.drawText(rect.adjusted(0, 0, -12, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                   right + cmd.group)
        p.restore()


class CommandPalette(QDialog):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self.commands: list[Command] = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 18, 28, 34)
        card = QFrame()
        t = theme.current()
        card.setStyleSheet(f"QFrame#card {{ background: {t.bg2}; border: 1px solid {t.line_strong}; border-radius: 14px; }}")
        card.setObjectName("card")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(46)
        shadow.setOffset(0, 12)
        shadow.setColor(QColor(0, 0, 0, 160))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Type a command or effect…"))
        self.search.setStyleSheet("QLineEdit { font-size: 12pt; padding: 10px 12px; border: none; background: transparent; }")
        lay.addWidget(self.search)
        self.list = QListWidget()
        self.list.setItemDelegate(_Delegate(self.list))
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setMinimumHeight(340)
        lay.addWidget(self.list)
        self.empty = QLabel(tr("No matching command."))
        self.empty.setProperty("role", "faint")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.empty)
        self.setFixedWidth(640)
        self.search.textChanged.connect(self._filter)
        self.list.itemActivated.connect(self._run)
        self.list.itemClicked.connect(self._run)
        self.search.installEventFilter(self)

    def set_commands(self, commands: list[Command]) -> None:
        self.commands = commands

    def open_palette(self) -> None:
        self.search.clear()
        self._filter("")
        p = self.parentWidget()
        if p is not None:
            g = p.window().frameGeometry()
            self.adjustSize()
            self.move(g.center().x() - self.width() // 2, g.top() + 90)
        self.show()
        self.search.setFocus()

    def _filter(self, text: str) -> None:
        tokens = text.lower().split()
        self.list.clear()
        scored = []
        for cmd in self.commands:
            hay = f"{cmd.name} {cmd.group} {cmd.keywords}".lower()
            if all(tok in hay for tok in tokens):
                name = cmd.name.lower()
                score = (0 if tokens and name.startswith(tokens[0]) else 1, 0 if cmd.enabled else 1, name)
                scored.append((score, cmd))
        for _, cmd in sorted(scored, key=lambda s: s[0])[:60]:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, cmd)
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)
        self.empty.setVisible(self.list.count() == 0)
        self.list.setVisible(self.list.count() > 0)

    def _run(self, item: QListWidgetItem) -> None:
        cmd: Command = item.data(Qt.ItemDataRole.UserRole)
        if not cmd.enabled:
            return
        self.hide()
        cmd.callback()

    def eventFilter(self, obj, ev) -> bool:
        if obj is self.search and ev.type() == QEvent.Type.KeyPress:
            key = ev.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                row = self.list.currentRow() + (1 if key == Qt.Key.Key_Down else -1)
                self.list.setCurrentRow(max(0, min(self.list.count() - 1, row)))
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if self.list.currentItem():
                    self._run(self.list.currentItem())
                return True
            if key == Qt.Key.Key_Escape:
                self.hide()
                return True
        return super().eventFilter(obj, ev)
