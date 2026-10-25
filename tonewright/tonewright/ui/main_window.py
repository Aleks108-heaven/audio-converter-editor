"""The main window: header, toolbar, library, stage, inspectors and the batch flows."""
from __future__ import annotations

import base64
import shutil
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QByteArray, QPoint, QSize, Qt, QTimer
from PySide6.QtGui import (QAction, QActionGroup, QCloseEvent, QDragEnterEvent, QDropEvent, QGuiApplication,
                           QKeySequence)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QDialog, QFileDialog, QHBoxLayout, QLabel, QMainWindow,
                               QMenu, QMessageBox, QPushButton, QSizePolicy, QSplitter, QStackedWidget, QToolButton,
                               QVBoxLayout, QWidget)

from .. import __version__, i18n
from .. import paths as app_paths
from ..core import access, audiobook, formats
from ..core import effects as fx
from ..core import ffmpeg
from ..core import tags as tagmod
from ..core.audiobook import BookOptions, Part
from ..core.convert import convert_all
from ..core.timefmt import format_size, format_time
from . import icons, theme
from .adaptive import AdaptiveBar
from .audiobook_panel import AudiobookPanel
from .controller import Controller
from .convert_panel import ConvertPanel
from .dialogs import AboutDialog
from .edit_panel import EditPanel
from .jobs import Job, JobBar
from .library import LibraryPanel
from .palette import Command, CommandPalette
from .stage import Stage
from .mix_page import MixPage
from .state import MODES, AppState, LibraryItem
from .tags_panel import TagsPage
from .tasks import run_task
from .transport import TransportBar
from .widgets import divider, icon_button, make_button
from .write_problem import show_write_problem
from ..i18n import tr

MODE_INFO = [("convert", "Convert", "convert"), ("edit", "Edit", "waveform"), ("multitrack", "Multitrack", "layers"),
             ("tags", "Tags", "tag"), ("audiobook", "Audiobook", "book")]
RIGHT_PAGE = {"edit": 0, "convert": 1, "audiobook": 2, "multitrack": 3}
CENTER_PAGE = {"tags": 1, "multitrack": 2}
# while the Multitrack page is showing, these editor commands act on the mix instead of the open file
MIX_ROUTES = {"toggle_play": "toggle_play", "go_start": "go_start", "go_end": "go_end", "undo": "undo", "redo": "redo",
              "delete": "delete_selected"}


def enable_dark_title_bar(window: QWidget, dark: bool) -> None:
    """Ask Windows 10/11 to draw the title bar to match the theme."""
    try:
        import ctypes
        hwnd = int(window.winId())
        value = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
        t = theme.current()
        colour = ctypes.c_int(int(t.bg0[5:7] + t.bg0[3:5] + t.bg0[1:3], 16))      # COLORREF is BGR
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(colour), ctypes.sizeof(colour))
    except Exception:
        pass


class MainWindow(QMainWindow):
    @staticmethod
    def _first_size() -> tuple[int, int]:
        """1560 x 900, or a little less than the screen where that would not fit (a 13-inch laptop)."""
        screen = QGuiApplication.primaryScreen()
        if screen is None or QGuiApplication.platformName() == "offscreen":
            return 1560, 900
        area = screen.availableGeometry()
        return min(1560, int(area.width() * 0.96)), min(900, int(area.height() * 0.94))

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self.actions: dict[str, QAction] = {}
        self.setWindowTitle(tr("Tonewright"))
        self.setWindowIcon(icons.app_icon())
        self.setAcceptDrops(True)
        self.resize(*self._first_size())
        self.setMinimumSize(1240, 700)
        self._convert_job: Optional[Job] = None
        self._book_job: Optional[Job] = None
        self._book_path_touched = False
        saved = state.settings.get("panels") or {}
        self._panels = {"library": bool(saved.get("library", True)), "side": bool(saved.get("side", True))}   # what the user chose
        self._panel_width = {"library": 310, "side": 370}                # how wide each was last time it was open
        self._narrow = False                    # too narrow for the library, the middle and the right panel side by side
        self._library_override = False          # the user opened the library by hand although the window is narrow

        self._build_actions()
        self._build_ui()
        self._build_menus()
        self._wire()
        self._build_palette()
        self.set_mode(state.settings.get("last_mode", "edit") if state.settings.get("last_mode") in MODES else "edit", initial=True)
        self._restore_geometry()
        self.update_actions()
        self._update_title()

    # ================================================================ actions
    def _act(self, key: str, text: str, slot: Callable, shortcut=None, icon: Optional[str] = None,
             tip: str = "", checkable: bool = False, checked: bool = False) -> QAction:
        a = QAction(tr(text), self)
        if icon:
            icons.bind(a, icon, 18)
        if shortcut:
            seqs = shortcut if isinstance(shortcut, list) else [shortcut]
            a.setShortcuts([QKeySequence(s) for s in seqs])
        a.setToolTip(tr(tip or text))
        a.setCheckable(checkable)
        if checkable:
            a.setChecked(checked)
            a.toggled.connect(slot)
        else:
            a.triggered.connect(lambda _=False: slot())
        a.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.addAction(a)
        self.actions[key] = a
        return a

    def _command(self, name: str, *args):
        """Run a controller command, or its Multitrack counterpart while that page is showing."""
        if self.state.mode == "multitrack" and name in MIX_ROUTES:
            return getattr(self.mix_page.ctl, MIX_ROUTES[name])()
        return getattr(self.ctl, name)(*args)

    def _build_actions(self) -> None:
        # created before the controller exists, so every slot is looked up lazily
        c = lambda name: (lambda *a: self._command(name, *a))
        act = self._act
        act("new", "New…", c("new_document"), "Ctrl+N", "file-new", "New empty file")
        act("open", "Open…", c("open_dialog"), "Ctrl+O", "open", "Open audio files")
        act("import_video", "Import audio from video…", c("import_video_dialog"), None, "film")
        act("save", "Save", c("save"), "Ctrl+S", "save", "Save changes")
        act("save_as", "Save as…", c("save_as"), "Ctrl+Shift+S", "download", "Save a copy in any format")
        act("close_item", "Close file", c("close_item"), "Ctrl+W")
        act("prev_item", "Previous file", lambda: self.ctl.step_item(-1), "Ctrl+PgUp", "skip-start")
        act("next_item", "Next file", lambda: self.ctl.step_item(1), "Ctrl+PgDown", "skip-end")
        act("quit", "Quit", self.close, "Ctrl+Q")
        act("undo", "Undo", c("undo"), "Ctrl+Z", "undo")
        act("redo", "Redo", c("redo"), ["Ctrl+Y", "Ctrl+Shift+Z"], "redo")
        act("cut", "Cut", c("cut"), "Ctrl+X", "cut")
        act("copy", "Copy", c("copy"), "Ctrl+C", "copy")
        act("paste", "Paste", c("paste"), "Ctrl+V", "paste")
        act("paste_new", "Paste as new file", c("paste_new"), "Ctrl+Shift+V")
        act("mix_paste", "Mix paste…", c("mix_paste"), "Ctrl+Shift+M", "layers")
        act("mix_file", "Mix a file in at the cursor…", c("mix_file"), None, "layers")
        act("insert_file", "Insert a file at the cursor…", c("insert_file"), None, "plus")
        act("delete", "Delete", c("delete"), "Del", "trash")
        act("trim", "Trim to selection", c("trim"), "Ctrl+T", "crop")
        act("silence_sel", "Silence selection", c("silence_selection"), "Ctrl+L", "silence")
        act("select_all", "Select all", c("select_all"), "Ctrl+A")
        act("select_none", "Select none", c("select_none"), "Ctrl+Shift+A")
        act("goto", "Go to time…", c("goto_time"), "Ctrl+G", "clock")
        act("add_marker", "Add marker", c("add_marker"), "M", "flag")
        act("prev_marker", "Previous marker", lambda: self.ctl.step_marker(-1), "[")
        act("next_marker", "Next marker", lambda: self.ctl.step_marker(1), "]")
        act("repeat_effect", "Repeat last effect", c("repeat_effect"), "Ctrl+Shift+E", "refresh")
        act("apply_envelope", "Apply envelope", c("apply_envelope"), None, "check")
        # view
        grp = QActionGroup(self)
        for key, text, ic in (("view_waveform", "Waveform", "waveform"), ("view_spectral", "Spectral", "spectral"),
                              ("view_envelope", "Envelope", "envelope")):
            mode = key.split("_")[1]
            a = act(key, text, (lambda checked, m=mode: checked and self.ctl.set_view_mode(m)), None, ic,
                    f"{text} view", checkable=True, checked=(mode == "waveform"))
            grp.addAction(a)
        act("zoom_in", "Zoom in", lambda: self.stage.zoom_in(), ["Ctrl+=", "Ctrl++"], "zoom-in", "Zoom in")
        act("zoom_out", "Zoom out", lambda: self.stage.zoom_out(), "Ctrl+-", "zoom-out", "Zoom out")
        act("zoom_selection", "Zoom to selection", lambda: self.stage.zoom_selection(), "Ctrl+9", "zoom-sel", "Zoom to the selection")
        act("zoom_fit", "Show everything", lambda: self.stage.zoom_fit(), "Ctrl+0", "zoom-fit", "Fit the whole file")
        act("zoom_samples", "Zoom to samples", lambda: self.stage.zoom_samples(), None, "zoom-1", "One pixel per sample")
        act("vzoom_in", "Taller waveform", lambda: self.stage.vzoom(1.5), None, "vzoom-in", "Zoom in vertically")
        act("vzoom_out", "Shorter waveform", lambda: self.stage.vzoom(1 / 1.5), None, "vzoom-out", "Zoom out vertically")
        act("show_db", "Show decibel scale", self._toggle_db, None, checkable=True,
            checked=bool(self.state.settings.get("show_db_scale", True)))
        act("follow", "Follow the playhead", self._toggle_follow, None, checkable=True,
            checked=bool(self.state.settings.get("follow_playhead", True)))
        act("snap_zero", "Snap selections to zero crossings", self._toggle_snap, None, checkable=True,
            checked=bool(self.state.settings.get("snap_zero_crossings", False)))
        act("spec_log", "Logarithmic frequency axis", self._toggle_spec_log, None, checkable=True, checked=True)
        act("toggle_library", "Show the library", lambda on: self._show_panel("library", on), "Ctrl+B", "panel-left",
            "Show or hide the library  (Ctrl+B)", checkable=True, checked=self._panels["library"])
        act("toggle_side", "Show the side panel", lambda on: self._show_panel("side", on), "Ctrl+Shift+B", "panel-right",
            "Show or hide the panel on the right  (Ctrl+Shift+B)", checkable=True, checked=self._panels["side"])
        act("theme", "Switch to light theme", self.toggle_theme, None, "sun")
        # transport
        act("play", "Play / Pause", c("toggle_play"), "Space", "play", "Play or pause")
        act("stop", "Stop", self._escape, "Esc", "stop", "Stop")
        act("loop", "Loop", c("set_loop"), "L", "loop", "Loop playback", checkable=True)
        act("go_start", "Go to start", c("go_start"), "Home", "skip-start", "Go to the start")
        act("go_end", "Go to end", c("go_end"), "End", "skip-end", "Go to the end")
        act("rewind", "Back 5 seconds", lambda: self.ctl.jump(-5), "Ctrl+Left", "rewind", "Back 5 seconds")
        act("forward", "Forward 5 seconds", lambda: self.ctl.jump(5), "Ctrl+Right", "forward", "Forward 5 seconds")
        act("record", "Record…", c("record"), "Ctrl+R", "record", "Record from a microphone")
        # tools
        act("analyze", "Audio statistics…", c("analyze"), "Ctrl+I", "info")
        act("spectrum", "Frequency analysis…", c("spectrum"), None, "spectral")
        act("silence_tools", "Find silence…", c("silence_tools"), None, "silence")
        act("convert_format", "Convert file format…", c("convert_document_format"), None, "convert")
        # generate
        for kind, text, ic in (("silence", "Silence…", "silence"), ("tone", "Tone…", "tone"), ("noise", "Noise…", "noise"),
                               ("dtmf", "Dial tones…", "keypad"), ("speech", "Speech…", "speech")):
            act(f"gen_{kind}", text, (lambda k=kind: self.ctl.generate(k)), None, ic)
        # modes and help
        for i, (key, text, ic) in enumerate(MODE_INFO, start=1):
            act(f"mode_{key}", tr(text, "mode"), (lambda k=key: self.set_mode(k)), f"Ctrl+{i}", ic)
        act("palette", "Command palette", lambda: self.palette.open_palette(), "Ctrl+K", "search", "Search every command  (Ctrl+K)")
        act("effects_search", "Find an effect…", self._focus_effects, "Ctrl+E", "wand")
        act("about", "About Tonewright", lambda: AboutDialog(self).exec(), None, "info")
        act("shortcuts", "Keyboard shortcuts", self._show_shortcuts, "F1", "keypad")
        act("data_folder", "Open the settings folder", lambda: self.ctl.open_data_folder())

    # ================================================================ UI construction
    def _build_ui(self) -> None:
        st = self.state
        self.stage = Stage()
        self.stage.follow = bool(st.settings.get("follow_playhead", True))
        self.stage.show_db = bool(st.settings.get("show_db_scale", True))
        self.stage.snap_zero = bool(st.settings.get("snap_zero_crossings", False))
        self.transport = TransportBar(self.actions)
        self.library = LibraryPanel(st)
        self.edit_panel = EditPanel(st.favorites)
        self.convert_panel = ConvertPanel(st.settings)
        self.book_panel = AudiobookPanel(st.settings)
        self.tags_page = TagsPage(st)
        self.ctl = Controller(self, st, self.stage, self.transport, self.edit_panel, self.library)
        self.ctl.actions = self.actions

        # ---- header
        header = QWidget()
        header.setObjectName("header")
        header.setStyleSheet(f"#header {{ background: {theme.current().bg1}; border-bottom: 1px solid {theme.current().line}; }}")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(14, 8, 14, 8)
        hl.setSpacing(6)
        logo = QLabel()
        logo.setPixmap(icons.logo_pixmap(30))
        hl.addWidget(logo)
        name = QLabel(tr("Tonewright"))
        name.setStyleSheet(f"font-size: {theme.css_size(12.5)}; font-weight: 700; padding: 0 10px 0 6px;")
        hl.addWidget(name)
        self.menu_bar = self.menuBar()
        self.menu_bar.setNativeMenuBar(False)
        # the menus take whatever room the five tabs leave; a menu bar that is too narrow shows a "»" overflow button
        self.menu_bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.menu_bar.setMinimumWidth(60)
        hl.addWidget(self.menu_bar, 1)
        self.mode_group = QButtonGroup(self)
        self.mode_buttons: dict[str, QToolButton] = {}
        for key, text, ic in MODE_INFO:
            b = QToolButton()
            b.setText(tr(text, "mode"))
            icons.bind(b, ic, 18)
            b.setProperty("role", "tab")
            b.setCheckable(True)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(f"{tr(text, 'mode')}  (Ctrl+{[m[0] for m in MODE_INFO].index(key) + 1})")
            b.clicked.connect(lambda _=False, k=key: self.set_mode(k))
            self.mode_group.addButton(b)
            self.mode_buttons[key] = b
            hl.addWidget(b)
        hl.addSpacing(10)
        self.palette_btn = QPushButton("")
        self.palette_btn.setProperty("variant", "ghost")
        icons.bind(self.palette_btn, "search", 16)
        self.palette_btn.setToolTip(tr("Search every command  (Ctrl+K)"))
        self.palette_btn.setFixedWidth(40)
        self.palette_btn.clicked.connect(lambda: self.palette.open_palette())
        hl.addWidget(self.palette_btn)
        self.theme_btn = icon_button("sun", tr("Switch theme"))
        self.theme_btn.clicked.connect(self.toggle_theme)
        hl.addWidget(self.theme_btn)
        hl.addSpacing(4)
        hl.addWidget(divider(True))
        hl.addSpacing(4)
        self.panel_buttons: dict[str, QToolButton] = {}
        for key in ("toggle_library", "toggle_side"):                    # fold the left and the right panel away and back
            b = QToolButton()
            b.setDefaultAction(self.actions[key])
            b.setIconSize(QSize(20, 20))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            b.setAutoRaise(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.panel_buttons[key] = b
            hl.addWidget(b)

        # ---- edit toolbar
        self.toolbar = AdaptiveBar()
        self.toolbar.setObjectName("toolbar")
        self.toolbar.setStyleSheet(f"#toolbar {{ background: {theme.current().bg0}; border-bottom: 1px solid {theme.current().line}; }}")
        tl = QHBoxLayout(self.toolbar)
        tl.setContentsMargins(14, 5, 14, 5)
        tl.setSpacing(2)

        def tool(key: str, with_menu: Optional[Callable[[], QMenu]] = None) -> QToolButton:
            b = QToolButton()
            b.setDefaultAction(self.actions[key])
            b.setIconSize(QSize(18, 18))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            b.setAutoRaise(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            sc = self.actions[key].shortcut().toString()
            b.setToolTip(self.actions[key].toolTip() + (f"  ({sc})" if sc else ""))
            return b

        for key in ("new", "open", "save"):
            tl.addWidget(tool(key))
        tl.addSpacing(6)
        tl.addWidget(divider(True))
        tl.addSpacing(6)
        for key in ("cut", "copy", "paste", "delete", "trim"):
            tl.addWidget(tool(key))
        tl.addSpacing(6)
        tl.addWidget(divider(True))
        tl.addSpacing(6)
        self.undo_btn = tool("undo")
        self.redo_btn = tool("redo")
        self.undo_menu, self.redo_menu = QMenu(self), QMenu(self)
        self.undo_btn.setMenu(self.undo_menu)
        self.undo_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.redo_btn.setMenu(self.redo_menu)
        self.redo_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.undo_menu.aboutToShow.connect(self._fill_undo_menu)
        self.redo_menu.aboutToShow.connect(self._fill_redo_menu)
        tl.addWidget(self.undo_btn)
        tl.addWidget(self.redo_btn)
        tl.addSpacing(6)
        tl.addWidget(divider(True))
        tl.addSpacing(6)
        for key in ("view_waveform", "view_spectral", "view_envelope"):
            b = tool(key)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            tl.addWidget(b)
        self.env_apply = make_button(tr("Apply envelope"), "primary", "check")
        self.env_apply.clicked.connect(lambda: self.ctl.apply_envelope())
        self.env_reset = make_button(tr("Reset"), "ghost")
        self.env_reset.clicked.connect(lambda: self.stage.reset_envelope())
        tl.addWidget(self.env_apply)
        tl.addWidget(self.env_reset)
        tl.addSpacing(6)
        tl.addWidget(divider(True))
        tl.addSpacing(8)
        recent_lab = QLabel(tr("RECENT EFFECTS"))
        recent_lab.setProperty("role", "section")
        tl.addWidget(recent_lab)
        tl.addSpacing(6)
        self.recent_box = QHBoxLayout()
        self.recent_box.setSpacing(4)
        tl.addLayout(self.recent_box)
        tl.addStretch(1)
        self.rebuild_recent()
        self.toolbar.resized.connect(self._fit_recent)

        # ---- central layout
        self.center_stack = QStackedWidget()
        stage_page = QWidget()
        sl = QVBoxLayout(stage_page)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        stage_wrap = QWidget()
        swl = QVBoxLayout(stage_wrap)
        swl.setContentsMargins(12, 10, 12, 4)
        swl.addWidget(self.stage)
        sl.addWidget(stage_wrap, 1)
        sl.addWidget(self.transport)
        self.center_stack.addWidget(stage_page)
        self.center_stack.addWidget(self.tags_page)
        self.mix_page = MixPage(self, st, self.ctl)
        self.center_stack.addWidget(self.mix_page)

        self.right_stack = QStackedWidget()
        self.right_stack.addWidget(self.edit_panel)
        self.right_stack.addWidget(self.convert_panel)
        self.right_stack.addWidget(self.book_panel)
        self.right_stack.addWidget(self.mix_page.inspector)
        right_wrap = QWidget()
        right_wrap.setProperty("panel", True)
        rl = QVBoxLayout(right_wrap)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.right_stack)
        self.right_wrap = right_wrap
        right_wrap.setMinimumWidth(340)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.library)
        self.splitter.addWidget(self.center_stack)
        self.splitter.addWidget(right_wrap)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([310, 800, 370])

        # ---- bottom bars
        self.convert_bar = JobBar(tr("Convert now"), tr("Output folder"), same_folder_option=True)
        self.book_bar = JobBar(tr("Create audiobook"), tr("Save as"))
        self.convert_bar.path.setText(self.state.settings.get("output_dir") or str(Path.home() / "Music"))
        self.convert_bar.same_folder.setChecked(bool(self.state.settings.get("same_folder", False)))
        self.convert_bar._same_toggled(self.convert_bar.same_folder.isChecked())

        root = QWidget()
        vl = QVBoxLayout(root)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)
        vl.addWidget(header)
        vl.addWidget(self.toolbar)
        vl.addWidget(self.splitter, 1)
        vl.addWidget(self.convert_bar)
        vl.addWidget(self.book_bar)
        self.setCentralWidget(root)

        # ---- status bar
        self.status_msg = QLabel("")
        self.status_right = QLabel("")
        self.status_right.setProperty("role", "faint")
        self.status_right.setContentsMargins(0, 0, 14, 0)      # flush against the window edge, the last letters were cut off
        self.statusBar().addWidget(self.status_msg, 1)
        self.statusBar().addPermanentWidget(self.status_right)
        self.statusBar().setSizeGripEnabled(False)

    def _build_menus(self) -> None:
        mb, a = self.menu_bar, self.actions
        file = mb.addMenu(tr("File"))
        for key in ("new", "open", "import_video"):
            file.addAction(a[key])
        self.recent_menu = file.addMenu(tr("Open recent"))
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        file.addSeparator()
        for key in ("save", "save_as", "close_item"):
            file.addAction(a[key])
        file.addSeparator()
        file.addAction(a["prev_item"])
        file.addAction(a["next_item"])
        file.addSeparator()
        file.addAction(a["record"])
        file.addSeparator()
        file.addAction(a["quit"])

        edit = mb.addMenu(tr("Edit"))
        for key in ("undo", "redo", None, "cut", "copy", "paste", "paste_new", "delete", None, "trim", "silence_sel",
                    None, "select_all", "select_none", "goto", None, "add_marker", "prev_marker", "next_marker"):
            edit.addSeparator() if key is None else edit.addAction(a[key])

        view = mb.addMenu(tr("View"))
        for key in ("toggle_library", "toggle_side", None, "view_waveform", "view_spectral", "view_envelope", None,
                    "zoom_in", "zoom_out", "zoom_selection", "zoom_fit", "zoom_samples", "vzoom_in", "vzoom_out", None,
                    "show_db", "follow", "snap_zero", "spec_log"):
            view.addSeparator() if key is None else view.addAction(a[key])
        view.addSeparator()
        res = view.addMenu(tr("Spectrogram detail"))
        grp = QActionGroup(self)
        for n in (1024, 2048, 4096, 8192):
            act = res.addAction(tr(f"{n} points"))
            act.setCheckable(True)
            act.setChecked(n == int(self.state.settings.get("spectrogram_fft", 2048)))
            act.triggered.connect(lambda _=False, v=n: self._set_spec_fft(v))
            grp.addAction(act)
        view.addAction(a["theme"])
        lang_menu = view.addMenu(tr("Language"))
        lang_group = QActionGroup(self)
        chosen = self.state.settings.get("language", "auto")
        for code, name in (("auto", tr("Automatic (follow the system)")), *i18n.LANGUAGES):
            lang_act = lang_menu.addAction(name)
            lang_act.setCheckable(True)
            lang_act.setChecked(chosen == code or (code == "auto" and chosen not in dict(i18n.LANGUAGES)))
            lang_act.triggered.connect(lambda _=False, c=code: self._set_language(c))
            lang_group.addAction(lang_act)

        effects = mb.addMenu(tr("Effects"))
        for category, items in fx.by_category():
            sub = effects.addMenu(tr(category))
            for e in items:
                act = sub.addAction(tr(e.name))
                act.setToolTip(tr(e.description))
                act.triggered.connect(lambda _=False, eid=e.id: self.ctl.open_effect(eid))
        effects.addSeparator()
        effects.addAction(a["repeat_effect"])
        effects.addAction(a["effects_search"])

        gen = mb.addMenu(tr("Generate"))
        for k in ("silence", "tone", "noise", "dtmf", "speech"):
            gen.addAction(a[f"gen_{k}"])

        tools = mb.addMenu(tr("Tools"))
        for key in ("analyze", "spectrum", "silence_tools", "convert_format"):
            tools.addAction(a[key])

        mix = mb.addMenu(tr("Mix", "menu"))
        for key in ("mix_paste", "mix_file", "insert_file"):
            mix.addAction(a[key])

        self.fav_menu = mb.addMenu(tr("Favorites"))
        self.fav_menu.aboutToShow.connect(self._fill_fav_menu)

        help_menu = mb.addMenu(tr("Help"))
        for key in ("palette", "shortcuts", "data_folder", "about"):
            help_menu.addAction(a[key])

    def _wire(self) -> None:
        st, ctl, stage = self.state, self.ctl, self.stage
        stage.selectionChanged.connect(ctl.on_selection_changed)
        stage.cursorChanged.connect(ctl.on_cursor_changed)
        stage.viewChanged.connect(ctl.on_view_changed)
        stage.seekRequested.connect(ctl.seek_requested)
        stage.scrubRequested.connect(ctl.scrub)
        stage.scrubEnded.connect(ctl.end_scrub)
        stage.openRequested.connect(ctl.open_dialog)
        stage.contextMenuRequested.connect(self._stage_menu)
        stage.markerClicked.connect(lambda i: self.edit_panel.show_tab("Markers"))
        stage.envelopeChanged.connect(self.update_actions)
        self.transport.selectionEdited.connect(ctl.selection_edited)
        self.transport.volumeChanged.connect(ctl.set_volume)
        self.transport.gotoRequested.connect(ctl.goto_time)
        ctl.statusMessage.connect(self._flash)
        ctl.stateChanged.connect(self.update_actions)
        ctl.previewStateChanged.connect(self.update_actions)
        self.library.addRequested.connect(ctl.open_dialog)
        self.library.removeRequested.connect(ctl.remove_items)
        self.library.filesDropped.connect(lambda paths: ctl.open_paths(paths))
        self.library.editRequested.connect(lambda item: (st.set_current(item), self.set_mode("edit")))
        self.library.showInFolder.connect(ctl.show_in_folder)
        self.library.addToMixRequested.connect(self._add_to_mix)
        mix = self.mix_page.ctl
        mix.message.connect(self._flash)
        mix.refreshed.connect(self.update_actions)
        mix.playStateChanged.connect(self.update_actions)
        self.library.selectionChanged.connect(self.tags_page.set_items)
        self.tags_page.renamed.connect(self.library.refresh)
        st.currentChanged.connect(self._current_changed)
        st.itemUpdated.connect(lambda item: self._update_title() if item is st.current else None)
        st.itemsChanged.connect(self._library_changed)
        self.edit_panel.effectRequested.connect(ctl.open_effect)
        self.edit_panel.favoriteRequested.connect(ctl.apply_favorite)
        self.edit_panel.markers.addRequested.connect(ctl.add_marker)
        self.edit_panel.markers.jumpRequested.connect(ctl.marker_jump)
        self.edit_panel.markers.removeRequested.connect(ctl.marker_remove)
        self.edit_panel.markers.renamed.connect(ctl.marker_rename)
        self.edit_panel.markers.moved.connect(ctl.marker_move)
        self.edit_panel.history.jumpRequested.connect(ctl.history_jump)
        # convert flow
        self.convert_bar.primaryClicked.connect(self.start_convert)
        self.convert_bar.browseClicked.connect(self._browse_output)
        self.convert_bar.stopClicked.connect(lambda: self._convert_job and self._convert_job.cancel.cancel())
        self.convert_bar.pauseToggled.connect(self._pause_convert)
        self.convert_panel.changed.connect(self._update_convert_summary)
        self.convert_bar.same_folder.toggled.connect(lambda on: (self.state.settings.set("same_folder", on), self._update_convert_summary()))
        self.convert_bar.pathEdited.connect(lambda t: (self.state.settings.set("output_dir", t), self._update_convert_summary()))
        # audiobook flow
        self.book_panel.previewRequested.connect(self.preview_chapters)
        self.book_panel.changed.connect(self._update_book_path)
        self.book_panel.title.textChanged.connect(self._update_book_path)
        self.book_bar.primaryClicked.connect(self.create_audiobook)
        self.book_bar.browseClicked.connect(self._browse_book)
        self.book_bar.stopClicked.connect(lambda: self._book_job and self._book_job.cancel.cancel())
        self.book_bar.pauseToggled.connect(self._pause_book)
        self.book_bar.pathEdited.connect(lambda t: setattr(self, "_book_path_touched", True))

    def _build_palette(self) -> None:
        self.palette = CommandPalette(self)

        def refresh() -> None:
            cmds: list[Command] = []
            groups = {"new": "File", "open": "File", "import_video": "File", "save": "File", "save_as": "File",
                      "close_item": "File", "prev_item": "File", "next_item": "File", "record": "File", "undo": "Edit", "redo": "Edit", "cut": "Edit",
                      "copy": "Edit", "paste": "Edit", "paste_new": "Edit", "delete": "Edit", "trim": "Edit",
                      "silence_sel": "Edit", "select_all": "Edit", "select_none": "Edit", "goto": "Edit",
                      "add_marker": "Edit", "mix_paste": "Mix", "mix_file": "Mix", "insert_file": "Mix",
                      "analyze": "Tools", "spectrum": "Tools", "silence_tools": "Tools", "convert_format": "Tools",
                      "play": "Transport", "stop": "Transport", "loop": "Transport", "go_start": "Transport",
                      "go_end": "Transport", "zoom_in": "View", "zoom_out": "View", "zoom_selection": "View",
                      "zoom_fit": "View", "view_waveform": "View", "view_spectral": "View", "view_envelope": "View",
                      "toggle_library": "View", "toggle_side": "View",
                      "theme": "View", "mode_convert": "Go to", "mode_edit": "Go to", "mode_tags": "Go to",
                      "mode_audiobook": "Go to", "about": "Help", "shortcuts": "Help", "repeat_effect": "Effects"}
            for key, group in groups.items():
                a = self.actions[key]
                cmds.append(Command(a.text().replace("&", "").rstrip("…"), tr(group), a.trigger, a.shortcut().toString(),
                                    key.replace("_", " "), "", a.isEnabled()))
            for kind in ("silence", "tone", "noise", "dtmf", "speech"):
                a = self.actions[f"gen_{kind}"]
                cmds.append(Command(tr(f"Generate {a.text().rstrip('…').lower()}"), tr("Generate"), a.trigger, "", "create sound", "", a.isEnabled()))
            has_doc = self.ctl.doc is not None and self.state.mode != "tags"
            for e in fx.all_effects():
                cmds.append(Command(tr(e.name), tr(e.category), (lambda eid=e.id: self.ctl.open_effect(eid)), "",
                                    f"{e.name} {e.keywords} {e.description}", "wand", has_doc))
            for fav in self.state.favorites.items:
                cmds.append(Command(f"★ {fav['name']}", tr("Favorite"), (lambda n=fav["name"]: self.ctl.apply_favorite(n, False)),
                                    "", fav["effect"], "star", has_doc))
            self.palette.set_commands(cmds)

        original = self.palette.open_palette
        self.palette.open_palette = lambda: (refresh(), original())

    # ================================================================ mode handling
    def set_mode(self, mode: str, initial: bool = False) -> None:
        if mode not in self.mode_buttons:
            return
        self.state.mode = mode
        self.state.settings.set("last_mode", mode)
        for k, b in self.mode_buttons.items():
            b.setChecked(k == mode)
        self.center_stack.setCurrentIndex(CENTER_PAGE.get(mode, 0))
        self._update_narrow()
        if mode == "multitrack":
            self.ctl.stop_playback()
        else:
            self.mix_page.ctl.stop()
        self._apply_panels()
        if mode in RIGHT_PAGE:
            self.right_stack.setCurrentIndex(RIGHT_PAGE[mode])
        self.convert_bar.setVisible(mode == "convert")
        self.book_bar.setVisible(mode == "audiobook")
        self.toolbar.setVisible(mode == "edit")
        self.library.tree.setSelectionMode(self.library.tree.SelectionMode.ExtendedSelection)
        if mode == "tags":
            self.tags_page.set_items(self.library.selected_items() or ([self.state.current] if self.state.current else []))
        elif mode == "audiobook":
            self._prefill_book()
            self._update_book_path()
        elif mode == "convert":
            self._update_convert_summary()
        self.ctl.show_current()
        self.update_actions()
        self.state.modeChanged.emit(mode)

    # ================================================================ the left and right panels
    def _show_panel(self, key: str, on: bool) -> None:
        """The user folded the library ("library") or the panel on the right ("side") away, or brought it back."""
        self._panels[key] = on
        if key == "library":
            self._library_override = on and self._narrow           # asked for by hand: keep it although there is little room
        self.state.settings.set("panels", dict(self._panels))
        self._apply_panels()

    def _room_needed(self) -> Optional[int]:
        """The width at which the library, the page in the middle and the panel on the right all fit side by side (None
        while the window is still being built). Each part says how much it needs, in the fonts of the day."""
        stack = getattr(self, "center_stack", None)
        if stack is None or stack.currentWidget() is None or not hasattr(self, "right_wrap"):
            return None
        side = self.right_wrap.minimumWidth() if self._panels["side"] and self.state.mode != "tags" else 0
        return self.library.minimumSizeHint().width() + stack.currentWidget().minimumSizeHint().width() + side + 60   # + handles, margins

    def _update_narrow(self) -> bool:
        """Whether the window is too narrow for the library, the middle and the panel on the right side by side (the
        middle page says how much it needs). True if that changed. A narrow window folds the library by itself and
        opens it again when there is room; what the user chose is kept."""
        needed = self._room_needed()
        if needed is None:
            return False
        narrow = self.width() < needed
        if not narrow:
            self._library_override = False
        changed = narrow != self._narrow
        self._narrow = narrow
        return changed

    def resizeEvent(self, event) -> None:               # noqa: N802
        super().resizeEvent(event)
        if self._update_narrow():
            self._apply_panels()

    def _apply_panels(self) -> None:
        """Show or hide the panels as the user chose. The panel on the right is never shown in Tags mode, which has
        its own editor, whatever was chosen; the choice comes back when another mode is selected."""
        folded = self._narrow and not self._library_override
        wanted = {"library": self._panels["library"] and not folded, "side": self._panels["side"] and self.state.mode != "tags"}
        for key, widget in (("library", self.library), ("side", self.right_wrap)):
            if wanted[key] != widget.isHidden():                         # already as it should be
                continue
            index = self.splitter.indexOf(widget)
            if not wanted[key]:
                width = self.splitter.sizes()[index]
                if width > 0:
                    self._panel_width[key] = width                       # remember it, to open at the same width again
                widget.setVisible(False)
                continue
            widget.setVisible(True)
            sizes = self.splitter.sizes()
            total = sum(sizes)
            sizes[index] = self._panel_width[key]
            sizes[1] = max(300, total - sizes[0] - sizes[2])             # the room comes from the middle
            self.splitter.setSizes(sizes)
            QTimer.singleShot(0, lambda s=sizes: self._set_splitter_sizes(s))    # the splitter finishes showing it after this returns
        for key, name in (("library", "toggle_library"), ("side", "toggle_side")):
            action = self.actions[name]
            shown = wanted[key] if key == "library" else self._panels[key]     # the library button says what is on screen
            if action.isChecked() != shown:                              # e.g. restored from the settings
                action.blockSignals(True)
                action.setChecked(shown)
                action.blockSignals(False)
        self.actions["toggle_side"].setEnabled(self.state.mode != "tags")

    def _set_splitter_sizes(self, sizes: list[int]) -> None:
        """Re-apply the widths a panel should open at, unless a panel was folded again in the meantime."""
        if (self.library.isHidden() != (sizes[0] == 0)) or (self.right_wrap.isHidden() != (sizes[2] == 0)):
            return
        self.splitter.setSizes(sizes)

    def _current_changed(self, item: Optional[LibraryItem]) -> None:
        self.ctl.show_current()
        if self.state.mode == "tags":
            sel = self.library.selected_items()
            self.tags_page.set_items(sel or ([item] if item else []))
        self._update_title()
        if self.state.mode == "audiobook":
            self._prefill_book()

    def _library_changed(self) -> None:
        self._update_convert_summary()
        self._update_book_path()
        if self.state.mode == "audiobook":
            self._prefill_book()
        self.update_actions()

    # ================================================================ updates
    def update_actions(self) -> None:
        a = self.actions
        ctl = self.ctl
        doc = ctl.doc
        mode = self.state.mode
        editing = doc is not None and mode != "tags"
        sel = self.stage.selection
        has_sel = bool(sel) and editing
        env = self.stage.state.mode == "envelope"
        busy_preview = ctl._preview_active
        for key in ("save", "save_as", "close_item", "select_all", "select_none", "goto", "add_marker", "zoom_in", "zoom_out",
                    "zoom_fit", "zoom_samples", "vzoom_in", "vzoom_out", "view_waveform", "view_spectral", "view_envelope",
                    "analyze", "spectrum", "silence_tools", "convert_format", "mix_file", "insert_file", "record_stub"):
            if key in a:
                a[key].setEnabled(editing)
        a["close_item"].setEnabled(self.state.current is not None)
        for key in ("prev_item", "next_item"):
            a[key].setEnabled(len(self.state.items) > 1)
        a["record"].setEnabled(True)
        for key in ("cut", "copy", "delete", "trim", "silence_sel", "zoom_selection"):
            a[key].setEnabled(has_sel)
        a["paste"].setEnabled(editing and self.state.clipboard is not None)
        a["paste_new"].setEnabled(self.state.clipboard is not None)
        a["mix_paste"].setEnabled(editing and self.state.clipboard is not None)
        a["undo"].setEnabled(editing and doc.can_undo)
        a["redo"].setEnabled(editing and doc.can_redo)
        a["repeat_effect"].setEnabled(editing and ctl.last_effect is not None)
        for key in ("prev_marker", "next_marker"):
            a[key].setEnabled(editing and bool(doc.markers))
        for key in ("play", "stop", "go_start", "go_end", "rewind", "forward", "loop"):
            a[key].setEnabled(editing)
        a["loop"].blockSignals(True)
        a["loop"].setChecked(ctl.loop_on)
        a["loop"].blockSignals(False)
        mode_view = self.stage.state.mode
        for key, m in (("view_waveform", "waveform"), ("view_spectral", "spectral"), ("view_envelope", "envelope")):
            a[key].blockSignals(True)
            a[key].setChecked(mode_view == m)
            a[key].blockSignals(False)
        for key in [k for k in a if k.startswith("gen_")]:
            a[key].setEnabled(True)
        t = theme.current()
        playing = ctl.player.state == "playing" and not busy_preview
        a["play"].setIcon(icons.icon("pause" if playing else "play", 22, color=t.accent_fg))
        a["play"].setText(tr("Pause") if playing else tr("Play"))
        self.env_apply.setVisible(env and editing)
        self.env_reset.setVisible(env and editing)
        if mode == "multitrack":
            mix = self.mix_page.ctl
            pr = mix.project
            for key in ("play", "stop", "go_start", "go_end"):
                a[key].setEnabled(not pr.empty)
            a["undo"].setEnabled(pr.can_undo)
            a["redo"].setEnabled(pr.can_redo)
            a["delete"].setEnabled(mix.canvas.selected is not None)
            mix_playing = mix.player.state == "playing" and not mix._preview_active
            a["play"].setIcon(icons.icon("pause" if mix_playing else "play", 22, color=t.accent_fg))
            a["play"].setText(tr("Pause") if mix_playing else tr("Play"))
        self.undo_btn.setEnabled(a["undo"].isEnabled())
        self.redo_btn.setEnabled(a["redo"].isEnabled())
        self.status_right.setText(self._status_right())
        self.theme_btn.setToolTip(tr("Switch to the light theme") if t.name == "dark" else tr("Switch to the dark theme"))
        a["theme"].setText(tr("Switch to light theme") if t.name == "dark" else tr("Switch to dark theme"))

    def _status_right(self) -> str:
        item = self.state.current
        if item is None:
            return f"Tonewright {__version__}"
        doc = item.doc
        parts = [item.format_text()]
        if doc is not None:
            parts.append(tr(f"{format_size(doc.frames * doc.channels * 4)} in memory"))
            if doc.history_index:
                parts.append(tr(f"{doc.history_index} edit{'s' if doc.history_index != 1 else ''}"))
        return "  ·  ".join(parts)

    def _update_title(self) -> None:
        item = self.state.current
        self.setWindowTitle(tr(f"{item.title}  —  Tonewright") if item else tr("Tonewright"))

    def _flash(self, text: str) -> None:
        self.status_msg.setText(text)
        if not hasattr(self, "_flash_timer"):
            self._flash_timer = QTimer(self)
            self._flash_timer.setSingleShot(True)
            self._flash_timer.timeout.connect(lambda: self.status_msg.setText(""))
        self._flash_timer.start(5000)

    def rebuild_recent(self) -> None:
        while self.recent_box.count():
            w = self.recent_box.takeAt(0).widget()
            if w:
                w.deleteLater()
        ids = [e for e in self.state.settings.get("recent_effects") if e in {x.id for x in fx.all_effects()}]
        for fallback in ("fade", "normalize", "tempo", "chorus", "reverse"):
            if len(ids) < 5 and fallback not in ids:
                ids.append(fallback)
        for eid in ids[:5]:
            e = fx.get(eid)
            b = QPushButton(tr(e.name))
            b.setProperty("variant", "ghost")
            b.setToolTip(tr(e.description))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, i=eid: self.ctl.open_effect(i))
            self.recent_box.addWidget(b)
        self._fit_recent()

    def _fit_recent(self) -> None:
        """Show as many of the recent-effect buttons as there is room for, so that none is cut in half."""
        if not hasattr(self, "toolbar"):
            return
        lay = self.toolbar.layout()
        margins = lay.contentsMargins()
        used = margins.left() + margins.right()
        for i in range(lay.count()):
            item = lay.itemAt(i)
            if item.layout() is not self.recent_box:
                used += item.sizeHint().width() + lay.spacing()
        room = self.toolbar.width() - used
        for i in range(self.recent_box.count()):
            b = self.recent_box.itemAt(i).widget()
            if b is None:
                continue
            need = b.sizeHint().width() + self.recent_box.spacing()
            fits = need <= room
            if b.isHidden() == fits:
                b.setVisible(fits)
            room = room - need if fits else -1                  # once one does not fit, none of the later ones is shown

    # ================================================================ menus
    def _fill_undo_menu(self) -> None:
        self.undo_menu.clear()
        doc = self.ctl.doc
        if doc is None:
            return
        for n, label in enumerate(doc.undo_labels()[:15], start=1):
            self.undo_menu.addAction(f"{n}.  {tr(label)}", lambda k=n: self.ctl.undo_steps(k))

    def _fill_redo_menu(self) -> None:
        self.redo_menu.clear()
        doc = self.ctl.doc
        if doc is None:
            return
        for n, label in enumerate(doc.redo_labels()[:15], start=1):
            self.redo_menu.addAction(f"{n}.  {tr(label)}", lambda k=n: self.ctl.redo_steps(k))

    def _fill_recent_menu(self) -> None:
        self.recent_menu.clear()
        files = self.state.settings.recent_files()
        if not files:
            self.recent_menu.addAction(tr("No recent files")).setEnabled(False)
            return
        for f in files:
            self.recent_menu.addAction(Path(f).name, lambda p=f: self.ctl.open_paths([Path(p)])).setToolTip(f)
        self.recent_menu.addSeparator()
        self.recent_menu.addAction(tr("Clear list"), self.state.settings.clear_recent_files)

    def _fill_fav_menu(self) -> None:
        self.fav_menu.clear()
        favs = self.state.favorites.items
        if not favs:
            self.fav_menu.addAction(tr("No favorites yet")).setEnabled(False)
        for fav in favs:
            self.fav_menu.addAction(icons.icon("star", 16), fav["name"],
                                    lambda n=fav["name"]: self.ctl.apply_favorite(n, False)).setEnabled(self.ctl.doc is not None)
        self.fav_menu.addSeparator()
        self.fav_menu.addAction(tr("Manage favorites…"), lambda: (self.set_mode("edit"), self.edit_panel.show_tab("Favorites")))

    def _stage_menu(self, pos: QPoint) -> None:
        a = self.actions
        menu = QMenu(self)
        for key in ("cut", "copy", "paste", "delete", None, "trim", "silence_sel", None, "select_all", "add_marker", None,
                    "zoom_selection", "zoom_fit"):
            menu.addSeparator() if key is None else menu.addAction(a[key])
        menu.exec(pos)

    def _focus_effects(self) -> None:
        self.set_mode("edit")
        self.edit_panel.show_tab("Effects")
        self.edit_panel.effects.focus_search()

    def _show_shortcuts(self) -> None:
        rows = [(a.text().replace("&", "").rstrip("…"), a.shortcut().toString()) for k, a in self.actions.items()
                if not a.shortcut().isEmpty() and not k.startswith("mode_")]
        rows += [(tr("Mode: Convert / Edit / Multitrack / Tags / Audiobook"), "Ctrl+1 … Ctrl+5"), (tr("Zoom"), tr("Mouse wheel")),
                 (tr("Scroll"), tr("Shift + wheel, or middle-drag")), (tr("Vertical zoom"), tr("Ctrl + wheel")),
                 (tr("Move the cursor"), tr("← →  (Ctrl = 1 second)")), (tr("Extend the selection"), "Shift + ← →")]
        t = theme.current()
        html = "".join(f"<tr><td style='padding:3px 18px 3px 0'>{n}</td><td style='color:{t.accent}; font-family:{theme.MONO_FONT}'>{s}</td></tr>"
                       for n, s in sorted(rows))
        box = QMessageBox(self)
        box.setWindowTitle(tr("Keyboard shortcuts"))
        box.setText(f"<table>{html}</table>")
        box.exec()

    def _add_to_mix(self, items: list) -> None:
        self.set_mode("multitrack")
        self.mix_page.ctl.add_files(items)

    # ================================================================ view toggles
    def _escape(self) -> None:
        if self.state.mode == "multitrack":
            self.mix_page.ctl.stop()
        elif self.ctl.player.state != "stopped" or self.ctl._preview_active:
            self.ctl.stop_playback()
        else:
            self.stage.clear_selection()

    def _toggle_db(self, on: bool) -> None:
        self.stage.show_db = on
        self.state.settings.set("show_db_scale", on)
        self.stage.invalidate()

    def _toggle_follow(self, on: bool) -> None:
        self.stage.follow = on
        self.state.settings.set("follow_playhead", on)

    def _toggle_snap(self, on: bool) -> None:
        self.stage.snap_zero = on
        self.state.settings.set("snap_zero_crossings", on)

    def _toggle_spec_log(self, on: bool) -> None:
        self.stage.spec_log = on
        self.stage.invalidate()

    def _set_spec_fft(self, n: int) -> None:
        self.stage.spec_fft = n
        self.state.settings.set("spectrogram_fft", n)
        self.stage.invalidate()

    def _set_language(self, code: str) -> None:
        if code == self.state.settings.get("language", "auto"):
            return
        self.state.settings.set("language", code)
        QMessageBox.information(self, tr("Language"),
                                tr("The language will change the next time you start Tonewright."))

    def toggle_theme(self) -> None:
        new = "light" if theme.current().name == "dark" else "dark"
        self.state.settings.set("theme", new)
        theme.apply_theme(QApplication.instance(), new)
        t = theme.current()
        self.findChild(QWidget, "header").setStyleSheet(f"#header {{ background: {t.bg1}; border-bottom: 1px solid {t.line}; }}")
        self.toolbar.setStyleSheet(f"#toolbar {{ background: {t.bg0}; border-bottom: 1px solid {t.line}; }}")
        icons.refresh_all()
        self.edit_panel.effects._fill()
        self.mix_page.restyle()
        self.stage.invalidate()
        enable_dark_title_bar(self, new == "dark")
        self.update_actions()

    # ================================================================ convert flow
    def _out_dir(self) -> Optional[Path]:
        if self.convert_bar.same_folder.isChecked():
            return None
        text = self.convert_bar.path.text().strip()
        return Path(text) if text else None

    def _browse_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, tr("Choose the output folder"), self.convert_bar.path.text() or str(Path.home()))
        if folder:
            self.convert_bar.path.setText(folder)
            self.state.settings.set("output_dir", folder)
            self._update_convert_summary()

    def _update_convert_summary(self) -> None:
        if self.state.mode != "convert" and not self.convert_bar.isVisible():
            return
        s = self.convert_panel.picker.settings()
        items = self.state.items
        first = items[0] if items else None
        sr, ch = (first.sr, first.channels) if first else (44100, 2)
        summary = formats.describe(s, sr, ch)
        need = sum(formats.estimate_size(s, i.length, i.sr, i.channels) for i in items)
        folder = self._out_dir() or (first.path.parent if first and first.path else Path.home())
        try:
            free = shutil.disk_usage(folder if folder.exists() else folder.anchor or str(Path.home())).free
            space = tr(f"Free / required space on {folder.anchor or folder}:  {format_size(free)} / {format_size(need)}")
            low = free < need * 1.1
        except OSError:
            space, low = "", False
        self.convert_bar.set_status(f"{summary}      {space}", "warn" if low else "dim")

    def start_convert(self) -> None:
        items = self.state.items
        if not items:
            self.ctl.open_dialog()
            return
        out_dir = self._out_dir()
        if out_dir is not None:
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.ctl.error(tr("Output folder"), tr(f"Cannot use that folder: {exc.strerror or exc}"))
                return
        opts = self.convert_panel.options(out_dir)
        citems = [i.to_convert_item() for i in items]
        if any(c.path is None and c.doc is None for c in citems):
            self.ctl.error(tr("Convert"), tr("Some files have no audio to convert."))
            return
        need = sum(formats.estimate_size(opts.settings, c.length, c.source_sr, c.source_channels) for c in citems)
        folder = out_dir or (items[0].path.parent if items[0].path else Path.home())
        try:
            if shutil.disk_usage(folder).free < need * 1.05:
                if QMessageBox.question(self, tr("Low disk space"), tr("There may not be enough free space on the drive for the "
                                        f"output (about {format_size(need)}). Continue anyway?")) != QMessageBox.StandardButton.Yes:
                    return
        except OSError:
            pass
        for it in items:
            it.status, it.progress, it.message, it.output = "queued", 0.0, "", None
            self.state.touched(it)

        def run(job: Job):
            return convert_all(citems, opts, on_item_progress=job.report_item, on_overall=job.report,
                               on_result=lambda n, r: job.itemResult.emit(n, r), cancel=job.cancel, pause=job.pause)

        job = Job(run)
        self._convert_job = job
        job.progress.connect(lambda f: self.convert_bar.set_progress(f))
        job.itemProgress.connect(self._convert_item_progress)
        job.itemResult.connect(self._convert_item_result)
        job.completed.connect(self._convert_done)
        self.convert_bar.set_running(True)
        self.convert_bar.set_progress(0.0)
        self.convert_bar.set_status(tr("Converting…"))
        job.start()

    def _pause_convert(self, paused: bool) -> None:
        if self._convert_job:
            (self._convert_job.pause.pause if paused else self._convert_job.pause.resume)()
            self.convert_bar.set_status(tr("Paused") if paused else tr("Converting…"))

    def _convert_item_progress(self, n: int, frac: float) -> None:
        if 0 <= n < len(self.state.items):
            it = self.state.items[n]
            it.status, it.progress = "converting", frac
            self.state.touched(it)

    def _convert_item_result(self, n: int, res) -> None:
        if 0 <= n < len(self.state.items):
            it = self.state.items[n]
            it.status, it.message, it.output = res.status, res.message, res.output
            it.progress = 1.0 if res.status == "done" else 0.0
            self.state.touched(it)

    def _convert_done(self, results, error, cancelled) -> None:
        self.convert_bar.set_running(False)
        self._convert_job = None
        if error is not None:
            self.convert_bar.reset_progress()
            self.convert_bar.set_status(tr(f"Failed: {error}"), "bad")
            for it in self.state.items:
                if it.status in ("queued", "converting"):
                    it.status = "error"
                    self.state.touched(it)
            return
        if cancelled or results is None:
            for it in self.state.items:
                if it.status in ("queued", "converting"):
                    it.status = "ready"
                    self.state.touched(it)
            self.convert_bar.set_status(tr("Cancelled"), "warn")
            self.convert_bar.reset_progress()
            return
        stopped = [r for r in results if r.status == "cancelled"]
        done = [r for r in results if r.status == "done"]
        failed = [r for r in results if r.status == "error"]
        if stopped:
            for it in self.state.items:
                if it.status in ("queued", "converting", "cancelled"):
                    it.status, it.progress = "ready", 0.0
                    self.state.touched(it)
            self.convert_bar.reset_progress()
            self.convert_bar.set_status(tr(f"Cancelled. {len(done)} file{'s' if len(done) != 1 else ''} had already been converted."), "warn")
            return
        skipped = [r for r in results if r.status == "skipped"]
        warned = [r for r in done if r.warning]
        if failed and not done:
            self.convert_bar.reset_progress()          # nothing was written: a full bar would read as success
        elif failed or warned:
            self.convert_bar.set_progress(1.0, tr("Finished, with problems"))
        else:
            self.convert_bar.set_progress(1.0, tr("Completed 100 %"))
        msg = tr(f"Converted {len({r.output for r in done})} file{'s' if len({r.output for r in done}) != 1 else ''}")
        if skipped:
            msg += tr(f", skipped {len(skipped)}")
        if failed:
            msg += tr(f", {len(failed)} failed: {failed[0].message}")
        if warned:
            msg += tr(f", tags not written in {len(warned)}: {warned[0].warning}")
        self.convert_bar.set_status(msg, "bad" if failed else ("warn" if warned else "good"))
        self._explain_blocked_write(tr("Convert"), [r.problem for r in failed])
        if done:
            out = done[-1].output
            self._flash(tr(f"Saved to {out.parent}"))
            if not failed and QApplication.activeWindow() is self:
                QTimer.singleShot(150, lambda: self._offer_open_folder(out.parent))

    def _explain_blocked_write(self, title: str, problems: list) -> None:
        """When Windows Security is what stopped the save, the one line in the status bar is not enough: say which
        programs to allow and offer to open the page. Asked once, however many files were refused."""
        blocked = next((p for p in problems if p is not None and p.kind in access.BLOCKED_KINDS), None)
        if blocked is not None:
            QTimer.singleShot(0, lambda: show_write_problem(self, title, blocked))

    def _offer_open_folder(self, folder: Path) -> None:
        box = QMessageBox(self)
        box.setWindowTitle(tr("Conversion finished"))
        box.setText(tr("All files were converted."))
        box.setInformativeText(str(folder))
        open_btn = box.addButton(tr("Open folder"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(tr("Close"), QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            app_paths.open_folder(folder)

    # ================================================================ audiobook flow
    def _parts(self) -> list[Part]:
        parts = []
        for it in self.state.items:
            markers = []
            if it.doc is not None:
                markers = [(m.frame / it.doc.sr, m.name) for m in it.doc.markers]
            parts.append(Part(it.to_convert_item(), it.tags.title or "", markers))
        return parts

    def _prefill_book(self) -> None:
        items = self.state.items
        if not items:
            return
        t = items[0].tags
        self.book_panel.prefill(t.album or t.title or items[0].name, t.artist or t.album_artist, t.year, t.cover)

    def _update_book_path(self) -> None:
        if self._book_path_touched and self.book_bar.path.text():
            return
        items = self.state.items
        if not items:
            self.book_bar.path.setText("")
            return
        title = self.book_panel.title.text().strip() or items[0].name
        folder = items[0].path.parent if items[0].path else Path.home() / "Music"
        self.book_bar.path.setText(str(folder / (tagmod.sanitize_filename(title) + ".m4b")))
        n = len(items)
        self.book_bar.set_status(tr(f"{n} file{'s' if n != 1 else ''} in the library will be joined, in this order."))

    def _browse_book(self) -> None:
        name, _ = QFileDialog.getSaveFileName(self, tr("Save audiobook"), self.book_bar.path.text(), tr("Audiobook (*.m4b)"))
        if name:
            self._book_path_touched = True
            self.book_bar.path.setText(str(Path(name).with_suffix(".m4b")))

    def preview_chapters(self) -> None:
        if not self.state.items:
            self.ctl.open_dialog()
            return
        parts = self._parts()
        opts = self.book_panel.options()
        needs_audio = opts.pause_detection and opts.split in ("count", "time")
        try:
            if needs_audio:
                ok, res = run_task(self, tr("Looking for pauses"), lambda progress, cancel: audiobook.analyze_pauses(
                    parts, opts, progress=progress, cancel=cancel), message=tr("Listening for silences between phrases…"), delay_ms=300)
                if not ok:
                    return
                pauses, durations = res
            else:
                pauses, durations = None, [p.item.length for p in parts]
        except Exception as exc:  # noqa: BLE001
            self.ctl.error(tr("Chapters"), exc)
            return
        chapters = audiobook.plan_chapters(durations, parts, opts, pauses)
        note = tr(f"{len(chapters)} chapters.")
        if needs_audio and pauses is not None:
            note += tr(f" {len(pauses)} pauses were found; chapters start at the pause nearest to each ideal split.")
        self.book_panel.set_chapters(chapters, note + tr(" Double-click a name or time to change it."))

    def create_audiobook(self) -> None:
        if not self.state.items:
            self.ctl.open_dialog()
            return
        opts = self.book_panel.options()
        if not opts.title:
            opts.title = self.state.items[0].name
        text = self.book_bar.path.text().strip()
        if not text:
            self._browse_book()
            text = self.book_bar.path.text().strip()
            if not text:
                return
        path = Path(text).with_suffix(".m4b")
        if path.exists() and QMessageBox.question(self, tr("Create audiobook"), tr(f"{path.name} already exists. Replace it?")) != QMessageBox.StandardButton.Yes:
            return
        parts = self._parts()
        chapters = self.book_panel.chapters()

        def run(job: Job):
            return audiobook.create_audiobook(parts, opts, path, chapters=chapters, progress=job.report,
                                              cancel=job.cancel, pause=job.pause)

        job = Job(run)
        self._book_job = job
        job.progress.connect(lambda f: self.book_bar.set_progress(f))
        job.completed.connect(self._book_done)
        self.book_bar.set_running(True)
        self.book_bar.set_progress(0.0)
        self.book_bar.set_status(tr("Creating the audiobook…"))
        job.start()

    def _pause_book(self, paused: bool) -> None:
        if self._book_job:
            (self._book_job.pause.pause if paused else self._book_job.pause.resume)()
            self.book_bar.set_status(tr("Paused") if paused else tr("Creating the audiobook…"))

    def _book_done(self, result, error, cancelled) -> None:
        self.book_bar.set_running(False)
        self._book_job = None
        if error is not None:
            problem = None
            if isinstance(error, ffmpeg.OutputError):                  # say why the file could not be written
                problem = access.diagnose_output_error(error)
                reason = access.one_line(problem, error.path.name)
            else:
                reason = str(error).strip().splitlines()[0] if str(error).strip() else str(error)
            self.book_bar.reset_progress()
            self.book_bar.set_status(tr(f"Failed: {reason}"), "bad")
            self._explain_blocked_write(tr("Create audiobook"), [problem])
            return
        if cancelled or result is None:
            self.book_bar.set_status(tr("Cancelled"), "warn")
            self.book_bar.reset_progress()
            return
        path, chapters = result
        self.book_panel.set_chapters(chapters, tr(f"{len(chapters)} chapters written to the file."))
        self.book_bar.set_progress(1.0, tr("Completed 100 %"))
        self.book_bar.set_status(tr(f"Created {path.name} with {len(chapters)} chapters."), "good")
        self._flash(tr(f"Audiobook saved: {path}"))

    # ================================================================ window plumbing
    def dragEnterEvent(self, ev: QDragEnterEvent) -> None:
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev: QDropEvent) -> None:
        paths = [Path(u.toLocalFile()) for u in ev.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.ctl.open_paths(paths)

    def _restore_geometry(self) -> None:
        w = self.state.settings.get("window") or {}
        try:
            if w.get("geometry"):
                self.restoreGeometry(QByteArray(base64.b64decode(w["geometry"])))
            if w.get("sizes") and len(w["sizes"]) == 3:
                sizes = [int(s) for s in w["sizes"]]
                for key, i in (("library", 0), ("side", 2)):
                    if sizes[i] > 0:
                        self._panel_width[key] = sizes[i]
                self.splitter.setSizes(sizes)
        except (ValueError, TypeError):
            pass
        self._apply_panels()

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        enable_dark_title_bar(self, theme.current().name == "dark")

    def closeEvent(self, ev: QCloseEvent) -> None:
        if (self._convert_job and self._convert_job.isRunning()) or (self._book_job and self._book_job.isRunning()):
            if QMessageBox.question(self, tr("Quit"), tr("A conversion is still running. Stop it and quit?")) != QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
            for job in (self._convert_job, self._book_job):
                if job:
                    job.pause.resume()
                    job.cancel.cancel()
                    job.wait(5000)
        if not self.ctl.confirm_quit():
            ev.ignore()
            return
        if not self.mix_page.ctl.confirm_quit():
            ev.ignore()
            return
        self.ctl.stop_playback()
        self.mix_page.ctl.stop()
        sizes = self.splitter.sizes()
        for key, i in (("library", 0), ("side", 2)):
            if sizes[i] <= 0:                                            # folded away: keep the width it will open at
                sizes[i] = self._panel_width[key]
        self.state.settings.set("window", {"geometry": base64.b64encode(bytes(self.saveGeometry())).decode("ascii"),
                                           "sizes": sizes})
        ev.accept()
