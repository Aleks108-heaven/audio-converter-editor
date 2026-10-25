"""The Audiobook inspector: book details, chapters and sound settings."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
                               QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QRadioButton,
                               QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..core.audiobook import SPLIT_MODES, BookOptions, Chapter
from ..core.settings import Settings
from ..core.timefmt import format_time, parse_time
from . import theme
from .safe_image import read_image, read_pixmap
from .tags_panel import image_bytes_from_qimage
from .widgets import TimeEdit, divider, make_button, section_label, shrinkable
from .accessibility import label_for
from ..i18n import tr, width_scale


def _row(label: str, widget: QWidget, width: int = 104) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(10)
    lab = QLabel(label)
    lab.setFixedWidth(int(width * width_scale()))
    lab.setProperty("role", "dim")
    lay.addWidget(lab)
    lay.addWidget(widget, 1)
    label_for(lab, widget)                  # a screen reader says what the field is for
    return w


class AudiobookPanel(QScrollArea):
    changed = Signal()
    previewRequested = Signal()

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumWidth(340)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        saved = settings.get("audiobook") or {}
        self._cover: Optional[bytes] = None
        self._touched: set[str] = set()
        self._plan_valid = False
        self._loading = False
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(16, 14, 16, 16)
        lay.setSpacing(10)

        # ---- book
        lay.addWidget(section_label(tr("Book")))
        self.title = QLineEdit()
        self.author = QLineEdit()
        self.narrator = QLineEdit()
        self.year = QLineEdit()
        self.year.setPlaceholderText(tr("e.g. 2026"))
        self.genre = QComboBox()
        self.genre.setEditable(True)
        for g in (tr("Audiobook"), tr("Fiction"), tr("Non-Fiction"), tr("Biography"), tr("Meditation"), tr("Education"), tr("Spoken Word")):
            self.genre.addItem(g)
        self.genre.setCurrentText(str(saved.get("genre", tr("Audiobook"))))
        for key, edit in (("title", self.title), ("author", self.author), ("narrator", self.narrator), ("year", self.year)):
            edit.textEdited.connect(lambda _t, k=key: self._touch(k))
        lay.addWidget(_row(tr("Title"), self.title))
        lay.addWidget(_row(tr("Author"), self.author))
        lay.addWidget(_row(tr("Narrator"), self.narrator))
        lay.addWidget(_row(tr("Year"), self.year))
        lay.addWidget(_row(tr("Genre"), shrinkable(self.genre)))
        cov = QHBoxLayout()
        cov.setSpacing(12)
        self.cover_label = QLabel()
        self.cover_label.setFixedSize(96, 96)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cov.addWidget(self.cover_label)
        btns = QVBoxLayout()
        btns.setSpacing(6)
        self.cover_choose = make_button(tr("Choose cover…"), icon="image")
        self.cover_remove = make_button(tr("Remove cover"), "ghost", "trash")
        btns.addWidget(self.cover_choose)
        btns.addWidget(self.cover_remove)
        btns.addStretch(1)
        cov.addLayout(btns, 1)
        cwrap = QWidget()
        cwrap.setLayout(cov)
        lay.addWidget(cwrap)
        self.cover_choose.clicked.connect(self._choose_cover)
        self.cover_remove.clicked.connect(lambda: self.set_cover(None, user=True))
        lay.addWidget(divider())

        # ---- chapters
        lay.addWidget(section_label(tr("Chapters")))
        self.mode_group = QButtonGroup(self)
        self.mode_radios: dict[str, QRadioButton] = {}
        for key, text in SPLIT_MODES:
            r = QRadioButton(tr(text))
            self.mode_group.addButton(r)
            self.mode_radios[key] = r
            lay.addWidget(r)
        self.mode_radios[saved.get("split", "files") if saved.get("split") in self.mode_radios else "files"].setChecked(True)
        self.count = QSpinBox()
        self.count.setRange(1, 999)
        self.count.setValue(int(saved.get("count", 5)))
        self.count_row = _row(tr("Chapters"), self.count)
        self.every = TimeEdit(short=False)
        self.every.setValue(float(saved.get("every", 180.0)))
        self.every.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.every_row = _row(tr("Every"), self.every)
        self.pause_detect = QCheckBox(tr("Start chapters at pauses"))
        self.pause_detect.setChecked(bool(saved.get("pause", True)))
        self.pause_detect.setToolTip(tr("Moves each chapter start to the nearest silence, so chapters begin between phrases."))
        self.keep_titles = QCheckBox(tr("Name chapters from title tags"))
        self.keep_titles.setChecked(bool(saved.get("keep_titles", True)))
        self.keep_titles.setToolTip(tr("Use each file's title tag as its chapter name instead of 'Chapter N'."))
        self.label = QLineEdit(str(saved.get("label", tr("Chapter"))))
        self.label_row = _row(tr("Name prefix"), self.label)
        for w in (self.count_row, self.every_row, self.pause_detect, self.keep_titles, self.label_row):
            lay.addWidget(w)
        self.preview_btn = make_button(tr("Preview chapters"), icon="flag")
        self.preview_btn.clicked.connect(self.previewRequested)
        lay.addWidget(self.preview_btn)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels([tr("CHAPTER"), tr("STARTS AT")])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setMinimumHeight(190)
        lay.addWidget(self.table)
        self.table_note = QLabel("")
        self.table_note.setProperty("role", "faint")
        self.table_note.setWordWrap(True)
        lay.addWidget(self.table_note)
        lay.addWidget(divider())

        # ---- sound
        lay.addWidget(section_label(tr("Sound")))
        self.bitrate = QComboBox()
        for k in (32, 48, 64, 96, 128, 160):
            self.bitrate.addItem(tr(f"{k} kbps"), k)
        self.bitrate.setCurrentIndex(max(0, self.bitrate.findData(int(saved.get("bitrate", 64)))))
        self.mono = QCheckBox(tr("Mono (smaller file)"))
        self.mono.setChecked(bool(saved.get("mono", False)))
        self.mono.setToolTip(tr("Halves the file size; ideal for speech."))
        self.rate = QComboBox()
        self.rate.addItem(tr("Keep original"), 0)
        for r in (22050, 32000, 44100, 48000):
            self.rate.addItem(tr(f"{r} Hz"), r)
        self.rate.setCurrentIndex(max(0, self.rate.findData(int(saved.get("rate", 0)))))
        self.gap = QDoubleSpinBox()
        self.gap.setRange(0.0, 30.0)
        self.gap.setSingleStep(0.5)
        self.gap.setSuffix(tr(" s"))
        self.gap.setValue(float(saved.get("gap", 0.0)))
        lay.addWidget(_row(tr("Bitrate"), shrinkable(self.bitrate)))
        lay.addWidget(self.mono)
        lay.addWidget(_row(tr("Sample rate"), shrinkable(self.rate)))
        lay.addWidget(_row(tr("Pause between files"), self.gap, 120))
        lay.addStretch(1)
        self.setWidget(body)

        for sig in (self.mode_group.buttonToggled, self.count.valueChanged, self.every.editingFinished,
                    self.pause_detect.toggled, self.keep_titles.toggled, self.label.textChanged,
                    self.gap.valueChanged, self.title.textChanged):
            sig.connect(self._options_changed)
        for sig in (self.bitrate.currentIndexChanged, self.mono.toggled, self.rate.currentIndexChanged,
                    self.genre.currentTextChanged, self.author.textChanged, self.narrator.textChanged,
                    self.year.textChanged):
            sig.connect(self._save)
        self.table.itemChanged.connect(self._table_edited)
        self._sync_visibility()
        self.set_cover(None)

    # ------------------------------------------------------------ cover
    def set_cover(self, data: Optional[bytes], *, user: bool = False) -> None:
        self._cover = data
        if user:
            self._touch("cover")
        t = theme.current()
        pm = read_pixmap(data) if data else QPixmap()
        if not pm.isNull():
            self.cover_label.setStyleSheet("background: transparent; border-radius: 10px;")
            self.cover_label.setPixmap(pm.scaled(96, 96, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                                 Qt.TransformationMode.SmoothTransformation).copy(0, 0, 96, 96))
        else:
            self.cover_label.setPixmap(QPixmap())
            self.cover_label.setText(tr("No cover"))
            self.cover_label.setStyleSheet(f"background: {t.bg2}; border: 1px dashed {t.line_strong}; border-radius: 10px; color: {t.text_faint};")
        self.cover_remove.setEnabled(bool(data))

    def _choose_cover(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, tr("Choose cover art"), self.settings.last_dir("image"),
                                              tr("Images (*.jpg *.jpeg *.png *.bmp *.gif)"))
        if not name:
            return
        self.settings.remember_dir("image", name)
        img = read_image(name)
        if img.isNull():
            QMessageBox.warning(self, tr("Cover art"), tr("That image could not be read. Use a JPEG, PNG, GIF or BMP picture."))
            return
        self.set_cover(image_bytes_from_qimage(img), user=True)

    # ------------------------------------------------------------ prefill and state
    def _touch(self, key: str) -> None:
        self._touched.add(key)

    def prefill(self, title: str, author: str, year: str, cover: Optional[bytes]) -> None:
        """Fill fields the user has not typed in yet (from the first file's tags)."""
        self._loading = True
        if "title" not in self._touched and not self.title.text():
            self.title.setText(title)
        if "author" not in self._touched and not self.author.text():
            self.author.setText(author)
        if "year" not in self._touched and not self.year.text():
            self.year.setText(year)
        if "cover" not in self._touched and self._cover is None and cover:
            self.set_cover(cover)
        self._loading = False

    def mode(self) -> str:
        for key, r in self.mode_radios.items():
            if r.isChecked():
                return key
        return "files"

    def _sync_visibility(self) -> None:
        mode = self.mode()
        self.count_row.setVisible(mode == "count")
        self.every_row.setVisible(mode == "time")
        self.pause_detect.setVisible(mode in ("count", "time"))
        self.keep_titles.setVisible(mode == "files")
        self.label_row.setVisible(mode != "none")

    def _options_changed(self, *_args) -> None:
        self._sync_visibility()
        self.invalidate_plan()
        self._save()
        self.changed.emit()

    def _save(self, *_args) -> None:
        if self._loading:
            return
        self.settings.set("audiobook", {
            "split": self.mode(), "count": self.count.value(), "every": self.every.value(),
            "pause": self.pause_detect.isChecked(), "keep_titles": self.keep_titles.isChecked(),
            "label": self.label.text(), "bitrate": self.bitrate.currentData(), "mono": self.mono.isChecked(),
            "rate": self.rate.currentData(), "gap": self.gap.value(), "genre": self.genre.currentText()})

    def options(self) -> BookOptions:
        return BookOptions(
            title=self.title.text().strip(), author=self.author.text().strip(), narrator=self.narrator.text().strip(),
            year=self.year.text().strip(), genre=self.genre.currentText().strip() or tr("Audiobook"), cover=self._cover,
            bitrate=int(self.bitrate.currentData()), mono=self.mono.isChecked(), sample_rate=int(self.rate.currentData()),
            split=self.mode(), chapter_count=self.count.value(), chapter_seconds=max(5.0, self.every.value()),
            pause_detection=self.pause_detect.isChecked(), keep_title_tags=self.keep_titles.isChecked(),
            gap_seconds=self.gap.value(), chapter_label=self.label.text().strip() or tr("Chapter"))

    # ------------------------------------------------------------ chapter table
    def invalidate_plan(self) -> None:
        if self._plan_valid:
            self._plan_valid = False
            self.table.setEnabled(False)
            self.table_note.setText(tr("Options changed. Preview the chapters again to refresh this list."))

    def set_chapters(self, chapters: list[Chapter], note: str = "") -> None:
        self._loading = True
        self.table.setEnabled(True)
        self.table.setRowCount(len(chapters))
        for r, ch in enumerate(chapters):
            self.table.setItem(r, 0, QTableWidgetItem(ch.title))
            tm = QTableWidgetItem(format_time(ch.start, ms=False))
            tm.setFont(theme.mono_font(9))
            self.table.setItem(r, 1, tm)
        self._plan_valid = True
        self.table_note.setText(note or tr(f"{len(chapters)} chapters. Double-click a name or a time to change it."))
        self._loading = False

    def chapters(self) -> Optional[list[Chapter]]:
        """The reviewed list, or None when it is missing or out of date (it is then recomputed)."""
        if not self._plan_valid or self.table.rowCount() == 0:
            return None
        out: list[Chapter] = []
        for r in range(self.table.rowCount()):
            title = self.table.item(r, 0).text().strip() or tr(f"Chapter {r + 1}")
            start = parse_time(self.table.item(r, 1).text())
            out.append(Chapter(start if start is not None else 0.0, title))
        out.sort(key=lambda c: c.start)
        if out:
            out[0].start = 0.0
        return out

    def _table_edited(self, item: QTableWidgetItem) -> None:
        if self._loading or item.column() != 1:
            return
        value = parse_time(item.text())
        self._loading = True
        item.setText(format_time(value if value is not None else 0.0, ms=False))
        self._loading = False
