"""The editor's side panel: effects, favourites, markers, history and file info."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMenu, QMessageBox, QTableWidget, QTableWidgetItem, QTabWidget,
                               QVBoxLayout, QWidget)

from ..core import effects as fx
from ..core.document import Document
from ..core.settings import Favorites
from ..core.timefmt import format_size, format_time, parse_time
from . import icons, theme
from .widgets import icon_button, make_button, role_label

ROLE_EFFECT = Qt.ItemDataRole.UserRole


class EffectsTab(QWidget):
    effectRequested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 6)
        lay.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search effects…")
        self.search.setClearButtonEnabled(True)
        lay.addWidget(self.search)
        self.list = QListWidget()
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setSpacing(1)
        lay.addWidget(self.list, 1)
        hint = QLabel("Double-click an effect to open it.\nIt applies to the selection, or to the whole file.")
        hint.setProperty("role", "faint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.search.textChanged.connect(self._filter)
        self.list.itemDoubleClicked.connect(self._open)
        self.list.itemActivated.connect(self._open)
        self._fill()

    def _fill(self) -> None:
        self.list.clear()
        for category, effects in fx.by_category():
            head = QListWidgetItem(category.upper())
            head.setFlags(Qt.ItemFlag.NoItemFlags)
            f = head.font()
            f.setPointSizeF(8)
            f.setBold(True)
            head.setFont(f)
            head.setForeground(theme.qcolor(theme.current().text_faint))
            head.setData(ROLE_EFFECT, None)
            self.list.addItem(head)
            for e in effects:
                item = QListWidgetItem("   " + e.name)
                item.setData(ROLE_EFFECT, e.id)
                item.setToolTip(e.description)
                self.list.addItem(item)

    def _filter(self, text: str) -> None:
        q = text.strip().lower()
        header: Optional[QListWidgetItem] = None
        visible_in_group = False
        for i in range(self.list.count()):
            item = self.list.item(i)
            ident = item.data(ROLE_EFFECT)
            if ident is None:
                if header is not None:
                    header.setHidden(not visible_in_group)
                header, visible_in_group = item, False
                continue
            e = fx.get(ident)
            match = not q or q in e.name.lower() or q in e.keywords.lower() or q in e.description.lower() \
                or q in e.category.lower()
            item.setHidden(not match)
            visible_in_group = visible_in_group or match
        if header is not None:
            header.setHidden(not visible_in_group)

    def _open(self, item: QListWidgetItem) -> None:
        ident = item.data(ROLE_EFFECT)
        if ident:
            self.effectRequested.emit(ident)

    def focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()


class FavoritesTab(QWidget):
    applyRequested = Signal(str, bool)       # favourite name, apply immediately

    def __init__(self, favorites: Favorites) -> None:
        super().__init__()
        self.favorites = favorites
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 6)
        lay.setSpacing(8)
        self.list = QListWidget()
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        lay.addWidget(self.list, 1)
        self.empty = QLabel("No favourites yet.\nIn any effect dialog, press “To Favorites”\nto save your settings here.")
        self.empty.setProperty("role", "faint")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.empty)
        self.list.itemDoubleClicked.connect(lambda it: self.applyRequested.emit(it.data(ROLE_EFFECT), False))
        self.list.customContextMenuRequested.connect(self._menu)
        self.refresh()

    def refresh(self) -> None:
        self.list.clear()
        for fav in self.favorites.items:
            try:
                kind = fx.get(fav["effect"]).name
            except KeyError:
                continue
            item = QListWidgetItem(f"{fav['name']}")
            item.setData(ROLE_EFFECT, fav["name"])
            item.setToolTip(kind)
            item.setIcon(icons.icon("star", 16, color=theme.current().accent))
            self.list.addItem(item)
        self.empty.setVisible(self.list.count() == 0)
        self.list.setVisible(self.list.count() > 0)

    def _menu(self, pos: QPoint) -> None:
        item = self.list.itemAt(pos)
        if item is None:
            return
        name = item.data(ROLE_EFFECT)
        menu = QMenu(self)
        menu.addAction(icons.icon("play"), "Apply now", lambda: self.applyRequested.emit(name, True))
        menu.addAction(icons.icon("sliders"), "Open with these settings…", lambda: self.applyRequested.emit(name, False))
        menu.addSeparator()
        menu.addAction(icons.icon("edit"), "Rename…", lambda: self._rename(name))
        menu.addAction(icons.icon("trash"), "Delete", lambda: (self.favorites.remove(name), self.refresh()))
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _rename(self, name: str) -> None:
        new, ok = QInputDialog.getText(self, "Rename favourite", "New name:", text=name)
        if ok and new.strip():
            self.favorites.rename(name, new.strip())
            self.refresh()


class MarkersTab(QWidget):
    jumpRequested = Signal(int)
    addRequested = Signal()
    removeRequested = Signal(int)
    renamed = Signal(int, str)
    moved = Signal(int, int)

    def __init__(self) -> None:
        super().__init__()
        self._doc: Optional[Document] = None
        self._busy = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 6)
        lay.setSpacing(8)
        row = QHBoxLayout()
        self.add_btn = make_button("Add at cursor", icon="flag")
        self.remove_btn = icon_button("trash", "Remove the selected marker")
        row.addWidget(self.add_btn, 1)
        row.addWidget(self.remove_btn)
        lay.addLayout(row)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["NAME", "TIME"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        lay.addWidget(self.table, 1)
        hint = QLabel("Press M in the editor to drop a marker.\nMarkers can split an audiobook into chapters.")
        hint.setProperty("role", "faint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.add_btn.clicked.connect(self.addRequested)
        self.remove_btn.clicked.connect(self._remove)
        self.table.cellClicked.connect(self._clicked)
        self.table.itemChanged.connect(self._edited)

    def set_document(self, doc: Optional[Document]) -> None:
        self._doc = doc
        self.refresh()

    def refresh(self) -> None:
        self._busy = True
        self.table.setRowCount(0)
        doc = self._doc
        if doc is not None:
            for i, m in enumerate(doc.markers):
                self.table.insertRow(i)
                name = QTableWidgetItem(m.name)
                tm = QTableWidgetItem(format_time(m.frame / doc.sr))
                tm.setFont(theme.mono_font(9))
                tm.setForeground(theme.qcolor(theme.current().text_dim))
                self.table.setItem(i, 0, name)
                self.table.setItem(i, 1, tm)
        self._busy = False
        self.add_btn.setEnabled(doc is not None)
        self.remove_btn.setEnabled(doc is not None and bool(doc.markers))

    def _clicked(self, row: int, col: int) -> None:
        if self._doc and 0 <= row < len(self._doc.markers):
            self.jumpRequested.emit(self._doc.markers[row].frame)

    def _remove(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.removeRequested.emit(row)

    def _edited(self, item: QTableWidgetItem) -> None:
        if self._busy or self._doc is None:
            return
        row = item.row()
        if item.column() == 0:
            self.renamed.emit(row, item.text().strip() or f"Marker {row + 1}")
        else:
            seconds = parse_time(item.text())
            if seconds is not None:
                self.moved.emit(row, int(round(seconds * self._doc.sr)))
            else:
                self.refresh()


class HistoryTab(QWidget):
    jumpRequested = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self._doc: Optional[Document] = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 6)
        self.list = QListWidget()
        lay.addWidget(self.list, 1)
        hint = QLabel("Click a step to go back (or forward) to it.\nEditing from an earlier step discards the later ones.")
        hint.setProperty("role", "faint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.list.itemClicked.connect(lambda it: self.jumpRequested.emit(self.list.row(it)))

    def set_document(self, doc: Optional[Document]) -> None:
        self._doc = doc
        self.refresh()

    def refresh(self) -> None:
        self.list.clear()
        doc = self._doc
        if doc is None:
            return
        t = theme.current()
        for i, label in enumerate(doc.history_labels()):
            item = QListWidgetItem(("◂  " if i == doc.history_index else "     ") + (label if i else "Original"))
            if i > doc.history_index:
                item.setForeground(theme.qcolor(t.text_faint))
            elif i == doc.history_index:
                f = item.font()
                f.setBold(True)
                item.setFont(f)
                item.setForeground(theme.qcolor(t.accent))
            self.list.addItem(item)
        self.list.scrollToItem(self.list.item(doc.history_index))


class InfoTab(QWidget):
    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 8)
        self.label = QLabel("")
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.label.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.label)
        lay.addStretch(1)

    def set_document(self, doc: Optional[Document]) -> None:
        if doc is None:
            self.label.setText("")
            return
        t = theme.current()
        rows = [("Name", doc.name), ("File", str(doc.path) if doc.path else "Not saved yet"),
                ("Duration", format_time(doc.duration)),
                ("Sample rate", f"{doc.sr} Hz"), ("Channels", "Mono" if doc.channels == 1 else "Stereo")]
        meta = doc.meta
        if meta.get("codec"):
            rows.append(("Original codec", str(meta["codec"]).upper()))
        if meta.get("bits"):
            rows.append(("Bit depth", f"{meta['bits']}-bit"))
        if meta.get("bit_rate"):
            rows.append(("Original bitrate", f"{int(meta['bit_rate']) // 1000} kbps"))
        rows.append(("In memory as", f"{format_size(doc.frames * doc.channels * 4)} (32-bit float)"))
        rows.append(("Markers", str(len(doc.markers))))
        rows.append(("Edits", str(doc.history_index)))
        html = "".join(f"<tr><td style='color:{t.text_faint}; padding:3px 14px 3px 0;'>{k}</td>"
                       f"<td style='color:{t.text};'>{v}</td></tr>" for k, v in rows)
        self.label.setText(f"<table>{html}</table>")


class EditPanel(QWidget):
    effectRequested = Signal(str)
    favoriteRequested = Signal(str, bool)

    def __init__(self, favorites: Favorites) -> None:
        super().__init__()
        self.setMinimumWidth(270)
        self._doc: Optional[Document] = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.effects = EffectsTab()
        self.favorites_tab = FavoritesTab(favorites)
        self.markers = MarkersTab()
        self.history = HistoryTab()
        self.info = InfoTab()
        self.tabs.addTab(self.effects, "Effects")
        self.tabs.addTab(self.favorites_tab, "Favorites")
        self.tabs.addTab(self.markers, "Markers")
        self.tabs.addTab(self.history, "History")
        self.tabs.addTab(self.info, "Info")
        self.tabs.tabBar().setExpanding(False)
        self.tabs.tabBar().setUsesScrollButtons(True)
        lay.addWidget(self.tabs)
        self.effects.effectRequested.connect(self.effectRequested)
        self.favorites_tab.applyRequested.connect(self.favoriteRequested)

    def set_document(self, doc: Optional[Document]) -> None:
        if self._doc is not None:
            self._doc.remove_listener(self._on_doc)
        self._doc = doc
        if doc is not None:
            doc.add_listener(self._on_doc)
        for tab in (self.markers, self.history, self.info):
            tab.set_document(doc)

    def _on_doc(self, kind: str) -> None:
        self.history.refresh()
        if kind in ("markers", "content", "format"):
            self.markers.refresh()
        self.info.set_document(self._doc)

    def show_tab(self, name: str) -> None:
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i).lower() == name.lower():
                self.tabs.setCurrentIndex(i)
