"""The library panel: every open file, with duration and conversion status."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QModelIndex, QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel, QMenu, QStyle,
                               QStyledItemDelegate, QStyleOptionViewItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from ..core.timefmt import format_time
from . import icons, theme
from .state import AppState, LibraryItem
from .widgets import icon_button, make_button, role_label

ROLE_ID = Qt.ItemDataRole.UserRole
ROLE_STATUS = Qt.ItemDataRole.UserRole + 1
ROLE_PROGRESS = Qt.ItemDataRole.UserRole + 2
ROLE_MODIFIED = Qt.ItemDataRole.UserRole + 3
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".m4b", ".aac", ".ogg", ".oga", ".opus", ".wma", ".amr", ".awb",
              ".mp2", ".aiff", ".aif", ".ac3", ".mka", ".wv", ".ape", ".mp4", ".mkv", ".avi", ".mov", ".wmv",
              ".flv", ".webm", ".m4v", ".3gp", ".mpg", ".mpeg", ".ts"}


class _Delegate(QStyledItemDelegate):
    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), 38)

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        t = theme.current()
        col = index.column()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        rect = QRectF(option.rect)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if col == 0:
            row = QRectF(rect.left() + 4, rect.top() + 2, 10_000, rect.height() - 4)
            row.setRight(option.widget.viewport().width() - 4)
            if selected or hovered:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(theme.qcolor(t.accent_soft if selected else t.bg3))
                p.drawRoundedRect(row, 8, 8)
            if selected:
                p.setBrush(theme.qcolor(t.accent))
                p.drawRoundedRect(QRectF(row.left(), row.top() + 8, 3, row.height() - 16), 1.5, 1.5)
            p.drawPixmap(int(rect.left() + 14), int(rect.center().y() - 8),
                         icons.pixmap("music", t.accent if selected else t.text_faint, 16))
            name = str(index.data(Qt.ItemDataRole.DisplayRole))
            font = theme.ui_font(10, QFont.Weight.DemiBold if selected else QFont.Weight.Normal)
            p.setFont(font)
            p.setPen(theme.qcolor(t.text))
            text_rect = QRectF(rect.left() + 38, rect.top(), rect.width() - 40, rect.height())
            p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       p.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, int(text_rect.width())))
        elif col == 1:
            p.setFont(theme.mono_font(9))
            p.setPen(theme.qcolor(t.text_dim))
            p.drawText(rect.adjusted(0, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       str(index.data(Qt.ItemDataRole.DisplayRole)))
        else:
            status = index.data(ROLE_STATUS) or "ready"
            progress = float(index.data(ROLE_PROGRESS) or 0.0)
            area = rect.adjusted(6, 0, -10, 0)
            if status == "converting":
                bar = QRectF(area.left(), area.center().y() - 4, area.width(), 8)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(theme.qcolor(t.bg2))
                p.drawRoundedRect(bar, 4, 4)
                p.setBrush(theme.qcolor(t.accent))
                p.drawRoundedRect(QRectF(bar.left(), bar.top(), max(8.0, bar.width() * progress), 8), 4, 4)
            elif status in ("done", "error", "skipped", "queued"):
                text, color = {"done": ("Done", t.good), "error": ("Error", t.bad),
                               "skipped": ("Skipped", t.text_faint), "queued": ("Queued", t.text_dim)}[status]
                p.setFont(theme.ui_font(9, QFont.Weight.DemiBold))
                p.setPen(theme.qcolor(color))
                p.drawText(area, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, text)
        p.restore()


class LibraryTree(QTreeWidget):
    filesDropped = Signal(list)
    orderChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)

    @staticmethod
    def _paths(event) -> list[Path]:
        md = event.mimeData()
        return [Path(u.toLocalFile()) for u in md.urls() if u.isLocalFile()] if md.hasUrls() else []

    def dragEnterEvent(self, ev) -> None:
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
        else:
            super().dragEnterEvent(ev)

    def dragMoveEvent(self, ev) -> None:
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
        else:
            super().dragMoveEvent(ev)

    def dropEvent(self, ev) -> None:
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
            self.filesDropped.emit(self._paths(ev))
            return
        super().dropEvent(ev)
        self.orderChanged.emit()


class LibraryPanel(QFrame):
    addRequested = Signal()
    removeRequested = Signal(list)
    filesDropped = Signal(list)
    editRequested = Signal(object)
    showInFolder = Signal(object)
    selectionChanged = Signal(list)

    def __init__(self, state: AppState, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.state = state
        self.setProperty("panel", True)
        self.setMinimumWidth(260)
        self._syncing = False
        self._rows: dict[int, QTreeWidgetItem] = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 8)
        lay.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(0)
        head.addWidget(role_label("LIBRARY", "section"))
        head.addStretch(1)
        self.add_btn = icon_button("plus", "Add audio files…  (Ctrl+O)")
        self.remove_btn = icon_button("trash", "Remove from library  (Del)")
        self.up_btn = icon_button("arrow-up", "Move up")
        self.down_btn = icon_button("arrow-down", "Move down")
        for b in (self.up_btn, self.down_btn, self.remove_btn, self.add_btn):
            head.addWidget(b)
        lay.addLayout(head)

        self.tree = LibraryTree()
        self.tree.setColumnCount(3)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(0)
        self.tree.setItemDelegate(_Delegate(self.tree))
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.tree.setMouseTracking(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        h = self.tree.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        h.resizeSection(1, 56)
        h.resizeSection(2, 84)
        lay.addWidget(self.tree, 1)

        self.empty = QLabel("Add audio or video files to convert, edit, tag\nor bind into an audiobook.")
        self.empty.setProperty("role", "faint")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_btn = make_button("Add files…", "primary", "plus")
        empty_box = QVBoxLayout()
        empty_box.addStretch(1)
        empty_box.addWidget(self.empty)
        empty_box.addWidget(self.empty_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        empty_box.addStretch(2)
        self.empty_wrap = QWidget()
        self.empty_wrap.setLayout(empty_box)
        lay.addWidget(self.empty_wrap, 1)

        self.summary = QLabel("")
        self.summary.setProperty("role", "faint")
        lay.addWidget(self.summary)

        self.add_btn.clicked.connect(self.addRequested)
        self.empty_btn.clicked.connect(self.addRequested)
        self.remove_btn.clicked.connect(lambda: self.removeRequested.emit(self.selected_items()))
        self.up_btn.clicked.connect(lambda: self._move(-1))
        self.down_btn.clicked.connect(lambda: self._move(1))
        self.tree.currentItemChanged.connect(self._on_current)
        self.tree.itemSelectionChanged.connect(self._emit_selection)
        self.tree.itemDoubleClicked.connect(lambda it, c: self.editRequested.emit(self._item_for(it)))
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.filesDropped.connect(self.filesDropped)
        self.tree.orderChanged.connect(self._order_changed)
        state.itemsChanged.connect(self.refresh)
        state.itemUpdated.connect(self.update_item)
        state.currentChanged.connect(self._select_current)
        self.refresh()

    # ---- model <-> view
    def _sync_status_column(self) -> None:
        # the status column only appears once a conversion has touched the list
        self.tree.setColumnHidden(2, all(i.status == "ready" for i in self.state.items))

    def _emit_selection(self) -> None:
        if not self._syncing:
            self.selectionChanged.emit(self.selected_items())

    def _item_for(self, row: Optional[QTreeWidgetItem]) -> Optional[LibraryItem]:
        if row is None:
            return None
        ident = row.data(0, ROLE_ID)
        return next((i for i in self.state.items if i.id == ident), None)

    def selected_items(self) -> list[LibraryItem]:
        rows = self.tree.selectedItems()
        picked = [self._item_for(r) for r in rows]
        return [i for i in self.state.items if i in picked]

    def refresh(self) -> None:
        self._syncing = True
        keep = {i.id for i in self.selected_items()}
        self.tree.clear()
        self._rows.clear()
        for item in self.state.items:
            row = QTreeWidgetItem(["", "", ""])
            row.setData(0, ROLE_ID, item.id)
            row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
            self.tree.addTopLevelItem(row)
            self._rows[item.id] = row
            self._fill(row, item)
        for ident, row in self._rows.items():
            if ident in keep:
                row.setSelected(True)
        cur = self.state.current
        if cur and cur.id in self._rows:
            self.tree.setCurrentItem(self._rows[cur.id])
            self._rows[cur.id].setSelected(True)
        self._syncing = False
        self._sync_status_column()
        has = bool(self.state.items)
        self.tree.setVisible(has)
        self.empty_wrap.setVisible(not has)
        for b in (self.remove_btn, self.up_btn, self.down_btn):
            b.setEnabled(has)
        total = sum(i.length for i in self.state.items)
        self.summary.setText(f"{len(self.state.items)} file{'s' if len(self.state.items) != 1 else ''}"
                             f"  ·  {format_time(total, ms=False, short=True)} total" if has else "")

    def _fill(self, row: QTreeWidgetItem, item: LibraryItem) -> None:
        row.setText(0, item.title)
        row.setText(1, format_time(item.length, ms=False, short=True))
        row.setData(2, ROLE_STATUS, item.status)
        row.setData(2, ROLE_PROGRESS, item.progress)
        tip = str(item.path) if item.path else "Not saved yet"
        if item.message:
            tip += f"\n{item.message}"
        for c in range(3):
            row.setToolTip(c, tip)

    def update_item(self, item: LibraryItem) -> None:
        row = self._rows.get(item.id)
        if row is not None:
            self._fill(row, item)
            self._sync_status_column()
            self.tree.viewport().update()

    def _on_current(self, row: Optional[QTreeWidgetItem], _prev) -> None:
        if self._syncing:
            return
        item = self._item_for(row)
        if item is not None:
            self.state.set_current(item)

    def _select_current(self, item: Optional[LibraryItem]) -> None:
        if item is None or item.id not in self._rows:
            return
        row = self._rows[item.id]
        if self.tree.currentItem() is not row:
            self._syncing = True
            if len(self.tree.selectedItems()) <= 1:
                self.tree.clearSelection()
            self.tree.setCurrentItem(row)
            row.setSelected(True)
            self._syncing = False

    def _order_changed(self) -> None:
        order = []
        for i in range(self.tree.topLevelItemCount()):
            item = self._item_for(self.tree.topLevelItem(i))
            if item is not None:
                order.append(item)
        if len(order) == len(self.state.items):
            self.state.reorder(order)

    def _move(self, delta: int) -> None:
        items = self.selected_items()
        if not items:
            return
        for item in (items if delta < 0 else reversed(items)):
            self.state.move(item, delta)

    def _menu(self, pos: QPoint) -> None:
        item = self._item_for(self.tree.itemAt(pos))
        if item is None:
            return
        menu = QMenu(self)
        menu.addAction(icons.icon("waveform"), "Open in editor", lambda: self.editRequested.emit(item))
        if item.path:
            menu.addAction(icons.icon("folder"), "Show in folder", lambda: self.showInFolder.emit(item))
        menu.addSeparator()
        menu.addAction(icons.icon("arrow-up"), "Move up", lambda: self.state.move(item, -1))
        menu.addAction(icons.icon("arrow-down"), "Move down", lambda: self.state.move(item, 1))
        menu.addSeparator()
        menu.addAction(icons.icon("trash"), "Remove from library", lambda: self.removeRequested.emit(self.selected_items() or [item]))
        menu.exec(self.tree.viewport().mapToGlobal(pos))
