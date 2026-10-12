"""Choose an output format and its quality settings."""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QGridLayout, QHBoxLayout, QLabel, QSlider, QVBoxLayout,
                               QWidget)

from ..core import formats
from ..core.formats import EncodeSettings, FormatSpec
from .widgets import Chip, role_label, shrinkable
from ..i18n import tr, width_scale

CHIP_LABELS = {"mp3": "MP3", "m4a": "M4A", "m4b": "M4B", "aac": "AAC", "ogg": "OGG", "opus": "Opus",
               "wma": "WMA", "flac": "FLAC", "wav": "WAV", "aiff": "AIFF", "mp2": "MP2", "amr": "AMR",
               "amrwb": "AMR-WB"}


def _row(label: str, widget: QWidget, field_width: int = 110) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(10)
    lab = QLabel(label)
    lab.setFixedWidth(int(field_width * width_scale()))
    lab.setProperty("role", "dim")
    lay.addWidget(lab)
    lay.addWidget(widget, 1)
    return w


class FormatPicker(QWidget):
    changed = Signal()

    def __init__(self, settings: Optional[EncodeSettings] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._s = replace(settings) if settings else EncodeSettings()
        self._src_sr, self._src_ch = 44100, 2
        self._loading = True
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        grid = QGridLayout()
        grid.setSpacing(6)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.chips: dict[str, Chip] = {}
        for i, key in enumerate(formats.available_keys()):
            chip = Chip(CHIP_LABELS.get(key, key.upper()))
            chip.setToolTip(tr(formats.FORMATS[key].name))
            self.group.addButton(chip)
            self.chips[key] = chip
            chip.clicked.connect(lambda _=False, k=key: self._pick_format(k))
            grid.addWidget(chip, i // 4, i % 4)
        root.addLayout(grid)
        self.description = QLabel("")
        self.description.setProperty("role", "faint")
        self.description.setWordWrap(True)
        root.addWidget(self.description)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem(tr("Constant bitrate"), "cbr")
        self.mode_combo.addItem(tr("Variable quality"), "vbr")
        self.bitrate_combo = QComboBox()
        self.quality_combo = QComboBox()
        self.depth_combo = QComboBox()
        self.comp_slider = QSlider(Qt.Orientation.Horizontal)
        self.comp_slider.setRange(0, 12)
        self.comp_label = QLabel("")
        comp_box = QWidget()
        comp_lay = QHBoxLayout(comp_box)
        comp_lay.setContentsMargins(0, 0, 0, 0)
        comp_lay.addWidget(self.comp_slider, 1)
        comp_lay.addWidget(self.comp_label)
        self.rate_combo = QComboBox()
        self.ch_combo = QComboBox()
        for combo in (self.mode_combo, self.bitrate_combo, self.quality_combo, self.depth_combo, self.rate_combo, self.ch_combo):
            shrinkable(combo)
        self.rows = {
            "mode": _row(tr("Bitrate mode"), self.mode_combo),
            "bitrate": _row(tr("Bitrate"), self.bitrate_combo),
            "quality": _row(tr("Quality"), self.quality_combo),
            "depth": _row(tr("Bit depth"), self.depth_combo),
            "comp": _row(tr("Compression"), comp_box),
            "rate": _row(tr("Sample rate"), self.rate_combo),
            "channels": _row(tr("Channels"), self.ch_combo),
        }
        for w in self.rows.values():
            root.addWidget(w)
        for combo in (self.mode_combo, self.bitrate_combo, self.quality_combo, self.depth_combo,
                      self.rate_combo, self.ch_combo):
            combo.currentIndexChanged.connect(self._controls_changed)
        self.comp_slider.valueChanged.connect(self._controls_changed)
        self._rebuild()
        self._loading = False

    # ---- public API
    def settings(self) -> EncodeSettings:
        return replace(self._s)

    def set_settings(self, s: EncodeSettings) -> None:
        self._s = replace(s)
        self._rebuild()
        self.changed.emit()

    def set_source(self, sr: int, channels: int) -> None:
        """Tell the picker what 'keep original' means so the labels can say so."""
        if (sr, channels) != (self._src_sr, self._src_ch):
            self._src_sr, self._src_ch = sr, channels
            self._rebuild()

    # ---- internals
    def _pick_format(self, key: str) -> None:
        old = self._s
        spec = formats.FORMATS[key]
        s = replace(old, format=key)
        if s.bitrate not in spec.bitrates:
            s.bitrate = spec.default_bitrate
        if spec.vbr_choices and int(s.quality) not in [c[0] for c in spec.vbr_choices]:
            s.quality = spec.default_vbr
        if not spec.vbr_choices:
            s.mode = "cbr"
        if spec.bit_depths and s.bit_depth not in spec.bit_depths:
            s.bit_depth = spec.default_depth
        if s.sample_rate and s.sample_rate not in spec.sample_rates and spec.fixed_sample_rate:
            s.sample_rate = 0
        s.channels = min(s.channels, spec.max_channels) if s.channels else 0
        self._s = s
        self._rebuild()
        self.changed.emit()

    def _fill(self, combo: QComboBox, items: list[tuple[str, object]], current: object) -> None:
        combo.blockSignals(True)
        combo.clear()
        for text, data in items:
            combo.addItem(text, data)
        idx = combo.findData(current)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

    def _rebuild(self) -> None:
        s = self._s
        if s.format not in self.chips:                      # a format this FFmpeg build cannot write (saved on another system)
            s = self._s = replace(s, format=next(iter(self.chips)))
        spec: FormatSpec = s.spec
        self.chips[s.format].setChecked(True)
        self.description.setText(tr(spec.description))
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(1 if (s.mode == "vbr" and spec.vbr_choices) else 0)
        self.mode_combo.blockSignals(False)
        vbr = s.mode == "vbr" and bool(spec.vbr_choices)
        lossy = not spec.lossless
        self.rows["mode"].setVisible(lossy and bool(spec.vbr_choices))
        self.rows["bitrate"].setVisible(lossy and not vbr)
        self.rows["quality"].setVisible(lossy and vbr)
        self.rows["depth"].setVisible(bool(spec.bit_depths))
        self.rows["comp"].setVisible(spec.compression)
        if lossy:
            self._fill(self.bitrate_combo, [(tr(f"{b:g} kbps"), b) for b in spec.bitrates],
                       s.bitrate if s.bitrate in spec.bitrates else spec.default_bitrate)
            if spec.vbr_choices:
                self._fill(self.quality_combo, [(tr(c[1]), c[0]) for c in spec.vbr_choices],
                           int(s.quality) if s.quality else spec.default_vbr)
        if spec.bit_depths:
            self._fill(self.depth_combo, [(tr(f"{d}-bit") + (" float" if d == 32 and s.format == "wav" else ""), d)
                                          for d in spec.bit_depths], s.bit_depth or spec.default_depth)
        self.comp_slider.blockSignals(True)
        self.comp_slider.setValue(int(s.compression))
        self.comp_slider.blockSignals(False)
        self.comp_label.setText(tr(f"Level {s.compression}"))
        keep_rate = tr(f"Keep original ({self._src_sr} Hz)")
        rates = [(keep_rate, 0)] if not spec.fixed_sample_rate else []
        rates += [(tr(f"{r} Hz"), r) for r in spec.sample_rates]
        self._fill(self.rate_combo, rates, s.sample_rate if not spec.fixed_sample_rate else spec.fixed_sample_rate)
        self.rows["rate"].setEnabled(not spec.fixed_sample_rate)
        names = {1: tr("Mono"), 2: tr("Stereo")}
        chans = [(tr(f"Keep original ({names.get(min(self._src_ch, spec.max_channels), self._src_ch)})"), 0)]
        chans += [(names[c], c) for c in (1, 2) if c <= spec.max_channels]
        if spec.fixed_channels:
            chans = [(names[spec.fixed_channels], spec.fixed_channels)]
        self._fill(self.ch_combo, chans, s.channels if not spec.fixed_channels else spec.fixed_channels)
        self.rows["channels"].setEnabled(not spec.fixed_channels)

    def _controls_changed(self) -> None:
        if self._loading:
            return
        s, spec = self._s, self._s.spec
        s.mode = self.mode_combo.currentData() or "cbr"
        if not spec.vbr_choices:
            s.mode = "cbr"
        if self.bitrate_combo.currentData() is not None:
            s.bitrate = float(self.bitrate_combo.currentData())
        if self.quality_combo.currentData() is not None:
            s.quality = float(self.quality_combo.currentData())
        if self.depth_combo.currentData() is not None:
            s.bit_depth = int(self.depth_combo.currentData())
        s.compression = self.comp_slider.value()
        s.sample_rate = int(self.rate_combo.currentData() or 0)
        s.channels = int(self.ch_combo.currentData() or 0)
        self.comp_label.setText(tr(f"Level {s.compression}"))
        vbr = s.mode == "vbr" and bool(spec.vbr_choices)
        self.rows["bitrate"].setVisible(not spec.lossless and not vbr)
        self.rows["quality"].setVisible(not spec.lossless and vbr)
        self.changed.emit()
