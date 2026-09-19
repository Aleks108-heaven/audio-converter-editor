"""Main window: toolbar, grouped tool tree, panel stack, and the persistent
waveform workspace that never disappears no matter which tool is selected."""

from __future__ import annotations

import sys

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import core
from . import icons, theme
from .panels import PANEL_BUILDERS, PANEL_GROUPS, PANEL_WAVE_MODE
from .state import AppState
from .waveform import WaveformWorkspace
from .widgets import FILE_FILTER, accent_button


class Toolbar(QFrame):
    """Top bar: Open on the left (obvious empty-state action), Convert pinned
    on the right (the one action every workflow ends with)."""

    def __init__(self, on_open, on_convert, parent=None):
        super().__init__(parent)
        self.setObjectName("Toolbar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)

        title = QLabel("\U0001f39a  Audio Converter & Editor")
        title.setProperty("role", "title")
        layout.addWidget(title)

        open_btn = accent_button("\U0001f4c2  Open Audio")
        open_btn.clicked.connect(on_open)
        layout.addSpacing(24)
        layout.addWidget(open_btn)

        layout.addStretch(1)

        convert_btn = accent_button("Convert / Export")
        convert_btn.clicked.connect(on_convert)
        layout.addWidget(convert_btn)


class ToolTree(QTreeWidget):
    """Narrow, grouped tool navigator with a distinct icon per tool."""

    def __init__(self, on_select, parent=None):
        super().__init__(parent)
        self.setObjectName("ToolTree")
        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)
        self.setIndentation(14)
        self.setIconSize(QSize(18, 18))
        self.setFixedWidth(220)
        self._on_select = on_select
        self._item_to_panel: dict[int, str] = {}

        for group_title, group_icon, items in PANEL_GROUPS:
            group_item = QTreeWidgetItem([group_title])
            group_item.setIcon(0, icons.get_icon(group_icon, theme.TEXT_MUTED))
            group_item.setFlags(Qt.ItemIsEnabled)
            font = group_item.font(0)
            font.setBold(True)
            font.setPointSize(8)
            group_item.setFont(0, font)
            group_item.setForeground(0, theme.QColor(theme.TEXT_DIM))
            self.addTopLevelItem(group_item)
            for name, icon_key, _mode in items:
                child = QTreeWidgetItem([name])
                child.setIcon(0, icons.get_icon(icon_key, theme.TEXT))
                group_item.addChild(child)
                self._item_to_panel[id(child)] = name
            group_item.setExpanded(True)

        self.itemSelectionChanged.connect(self._handle_select)

    def _handle_select(self) -> None:
        items = self.selectedItems()
        if not items:
            return
        panel = self._item_to_panel.get(id(items[0]))
        if panel:
            self._on_select(panel)

    def select_panel(self, name: str) -> None:
        for iid, panel in self._item_to_panel.items():
            if panel == name:
                for top in range(self.topLevelItemCount()):
                    group = self.topLevelItem(top)
                    for row in range(group.childCount()):
                        child = group.child(row)
                        if id(child) == iid:
                            self.setCurrentItem(child)
                            self._on_select(name)
                            return


class PanelContainer(QStackedWidget):
    """Stacks all tool panels (each wrapped in its own scroll area) and
    raises the selected one."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._panels: dict[str, QWidget] = {}

    def add_panel(self, name: str, widget: QWidget) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        wrapper = QWidget()
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(22, 18, 22, 18)
        wrapper_layout.addWidget(widget)
        scroll.setWidget(wrapper)
        self.addWidget(scroll)
        self._panels[name] = scroll

    def show_panel(self, name: str) -> None:
        panel = self._panels.get(name)
        if panel:
            self.setCurrentWidget(panel)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Audio Converter & Editor")
        self.resize(1220, 820)
        self.setMinimumSize(980, 640)

        self.state = AppState()

        central = QWidget()
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.toolbar = Toolbar(on_open=self._open_file, on_convert=self._go_convert)
        root_layout.addWidget(self.toolbar)

        self.workspace = WaveformWorkspace(self.state)
        self.workspace.waveform.on_empty_click = self._open_file
        body_margin = QWidget()
        body_margin_layout = QVBoxLayout(body_margin)
        body_margin_layout.setContentsMargins(14, 14, 14, 8)
        body_margin_layout.addWidget(self.workspace)
        root_layout.addWidget(body_margin)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setContentsMargins(14, 0, 14, 14)
        splitter.setHandleWidth(6)

        self.tree = ToolTree(on_select=self._select_panel)
        splitter.addWidget(self.tree)

        self.container = PanelContainer()
        for name, builder in PANEL_BUILDERS.items():
            self.container.add_panel(name, builder(self.state, self.workspace))
        splitter.addWidget(self.container)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        body_wrap = QWidget()
        body_wrap_layout = QVBoxLayout(body_wrap)
        body_wrap_layout.setContentsMargins(14, 0, 14, 14)
        body_wrap_layout.addWidget(splitter)
        root_layout.addWidget(body_wrap, 1)

        self.tree.select_panel("Convert")

    def _open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open audio file", "", FILE_FILTER)
        if not path:
            return
        try:
            self.state.set_file(path)
        except core.AudioToolError:
            pass  # a load failure here is rare; the file simply won't populate the workspace

    def _go_convert(self) -> None:
        self.tree.select_panel("Convert")

    def _select_panel(self, name: str) -> None:
        self.workspace.waveform.set_mode(PANEL_WAVE_MODE.get(name, "none"))
        self.container.show_panel(name)


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
