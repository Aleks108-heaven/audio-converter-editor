"""The Convert inspector: output format and processing options."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QScrollArea, QVBoxLayout, QWidget)

from ..core import formats
from ..core.convert import ConvertOptions
from ..core.formats import EncodeSettings
from ..core.settings import Settings
from .format_picker import FormatPicker
from .widgets import divider, role_label, section_label, shrinkable

PATTERNS = [("Original name", "{name}"), ("Artist - Title", "{artist} - {title}"),
            ("Track - Title", "{track} - {title}"), ("Track - Artist - Title", "{track} - {artist} - {title}"),
            ("Album - Track - Title", "{album} - {track} - {title}")]


def _row(label: str, widget: QWidget, width: int = 110) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(10)
    lab = QLabel(label)
    lab.setFixedWidth(width)
    lab.setProperty("role", "dim")
    lay.addWidget(lab)
    lay.addWidget(widget, 1)
    return w


class ConvertPanel(QScrollArea):
    changed = Signal()

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumWidth(320)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        saved = settings.get("convert") or {}
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(16, 14, 16, 16)
        lay.setSpacing(12)

        lay.addWidget(section_label("Output format"))
        self.picker = FormatPicker(EncodeSettings.from_dict(saved.get("encode")))
        lay.addWidget(self.picker)
        lay.addWidget(divider())

        lay.addWidget(section_label("Process"))
        self.normalize = QComboBox()
        self.normalize.addItem("Off", "off")
        self.normalize.addItem("Peak level", "peak")
        self.normalize.addItem("Loudness (LUFS)", "lufs")
        lay.addWidget(_row("Normalize", shrinkable(self.normalize)))
        self.peak = QDoubleSpinBox()
        self.peak.setRange(-30.0, 0.0)
        self.peak.setSingleStep(0.5)
        self.peak.setSuffix(" dB")
        self.peak.setValue(float(saved.get("peak_target", -1.0)))
        self.peak_row = _row("Peak target", self.peak)
        self.lufs = QDoubleSpinBox()
        self.lufs.setRange(-30.0, -5.0)
        self.lufs.setSingleStep(0.5)
        self.lufs.setSuffix(" LUFS")
        self.lufs.setValue(float(saved.get("lufs_target", -16.0)))
        self.lufs_row = _row("Loudness target", self.lufs)
        lay.addWidget(self.peak_row)
        lay.addWidget(self.lufs_row)
        self.fade_in = QDoubleSpinBox()
        self.fade_in.setRange(0.0, 60.0)
        self.fade_in.setSingleStep(0.5)
        self.fade_in.setSuffix(" s")
        self.fade_in.setValue(float(saved.get("fade_in", 0.0)))
        self.fade_out = QDoubleSpinBox()
        self.fade_out.setRange(0.0, 60.0)
        self.fade_out.setSingleStep(0.5)
        self.fade_out.setSuffix(" s")
        self.fade_out.setValue(float(saved.get("fade_out", 0.0)))
        lay.addWidget(_row("Fade in", self.fade_in))
        lay.addWidget(_row("Fade out", self.fade_out))
        self.merge = QCheckBox("Join all files into one")
        self.merge.setChecked(bool(saved.get("merge", False)))
        lay.addWidget(self.merge)
        self.merge_name = QLineEdit(str(saved.get("merge_name", "Merged")))
        self.merge_name_row = _row("Joined name", self.merge_name)
        self.gap = QDoubleSpinBox()
        self.gap.setRange(0.0, 30.0)
        self.gap.setSingleStep(0.5)
        self.gap.setSuffix(" s")
        self.gap.setValue(float(saved.get("gap", 0.0)))
        self.gap_row = _row("Pause between", self.gap)
        lay.addWidget(self.merge_name_row)
        lay.addWidget(self.gap_row)
        self.merge_note = QLabel("Normalizing is skipped when joining files.")
        self.merge_note.setProperty("role", "faint")
        self.merge_note.setWordWrap(True)
        lay.addWidget(self.merge_note)
        lay.addWidget(divider())

        lay.addWidget(section_label("Names and tags"))
        self.pattern = QComboBox()
        self.pattern.setEditable(True)
        for label, pat in PATTERNS:
            self.pattern.addItem(label, pat)
        pat = str(saved.get("pattern", "{name}"))
        idx = self.pattern.findData(pat)
        if idx >= 0:
            self.pattern.setCurrentIndex(idx)
        else:
            self.pattern.setEditText(pat)
        self.pattern.setToolTip("Tokens: {name} {title} {artist} {album} {album_artist} {track} {year} {genre}")
        lay.addWidget(_row("File name", shrinkable(self.pattern)))
        self.conflict = QComboBox()
        self.conflict.addItem("Keep both (add a number)", "rename")
        self.conflict.addItem("Replace the existing file", "overwrite")
        self.conflict.addItem("Skip it", "skip")
        self.conflict.setCurrentIndex(max(0, self.conflict.findData(saved.get("conflict", "rename"))))
        lay.addWidget(_row("If it exists", shrinkable(self.conflict)))
        self.keep_tags = QCheckBox("Keep tags and cover art")
        self.keep_tags.setChecked(bool(saved.get("keep_tags", True)))
        lay.addWidget(self.keep_tags)
        note = QLabel("The original file is never replaced, even when asked to.")
        note.setWordWrap(True)
        note.setProperty("role", "faint")
        lay.addWidget(note)
        lay.addStretch(1)
        self.setWidget(body)

        self.normalize.setCurrentIndex(max(0, self.normalize.findData(saved.get("normalize", "off"))))
        for sig in (self.picker.changed, self.normalize.currentIndexChanged, self.peak.valueChanged,
                    self.lufs.valueChanged, self.fade_in.valueChanged, self.fade_out.valueChanged,
                    self.merge.toggled, self.merge_name.textChanged, self.gap.valueChanged,
                    self.pattern.currentTextChanged, self.conflict.currentIndexChanged, self.keep_tags.toggled):
            sig.connect(self._changed)
        self._sync_visibility()

    def _sync_visibility(self) -> None:
        mode = self.normalize.currentData()
        merging = self.merge.isChecked()
        self.peak_row.setVisible(mode == "peak" and not merging)
        self.lufs_row.setVisible(mode == "lufs" and not merging)
        self.normalize.setEnabled(not merging)
        self.merge_name_row.setVisible(merging)
        self.gap_row.setVisible(merging)
        self.merge_note.setVisible(merging)

    def _changed(self) -> None:
        self._sync_visibility()
        self.save()
        self.changed.emit()

    def pattern_text(self) -> str:
        text = self.pattern.currentText().strip()
        for label, pat in PATTERNS:
            if text == label:
                return pat
        return text or "{name}"

    def options(self, out_dir: Optional[Path]) -> ConvertOptions:
        return ConvertOptions(
            settings=self.picker.settings(), out_dir=out_dir, pattern=self.pattern_text(),
            conflict=self.conflict.currentData(), normalize=self.normalize.currentData(),
            peak_target_db=self.peak.value(), lufs_target=self.lufs.value(), fade_in=self.fade_in.value(),
            fade_out=self.fade_out.value(), keep_tags=self.keep_tags.isChecked(), merge=self.merge.isChecked(),
            merge_name=self.merge_name.text().strip() or "Merged", gap_seconds=self.gap.value())

    def save(self) -> None:
        self.settings.set("convert", {
            "encode": self.picker.settings().to_dict(), "normalize": self.normalize.currentData(),
            "peak_target": self.peak.value(), "lufs_target": self.lufs.value(), "fade_in": self.fade_in.value(),
            "fade_out": self.fade_out.value(), "merge": self.merge.isChecked(),
            "merge_name": self.merge_name.text(), "gap": self.gap.value(), "pattern": self.pattern_text(),
            "conflict": self.conflict.currentData(), "keep_tags": self.keep_tags.isChecked()})
