"""The parameter dialog shared by every effect."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np
from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFrame, QGroupBox, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QMessageBox, QScrollArea, QSlider, QSpinBox,
                               QVBoxLayout, QWidget)

from ..core.effects import Effect, Param
from ..core.settings import Favorites
from ..core.timefmt import format_time
from . import icons
from .editors import (CompressorCurveEditor, Editor, FadeCurveEditor, GraphicEQEditor, NoiseProfileEditor)
from .tasks import run_task
from .widgets import divider, make_button, role_label
from ..i18n import tr

MIN_WIDTH = 580


@dataclass
class EffectContext:
    """Everything a dialog needs from the editor, without knowing about documents."""
    range_text: str
    channels: int
    frames: int
    sr: int
    render_preview: Callable[..., np.ndarray]
    play_array: Callable[[np.ndarray, int], None]
    stop_preview: Callable[[], None]
    is_preview_playing: Callable[[], bool]
    capture_noise: Callable[[], Optional[np.ndarray]]
    favorites: Favorites
    noise_store: dict = field(default_factory=dict)
    validate: Optional[Callable[[Effect, dict], Optional[str]]] = None


class ParamControl(QWidget):
    changed = Signal()

    def __init__(self, p: Param, value: Any) -> None:
        super().__init__()
        self.p = p
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        self.label: Optional[QLabel] = None
        self.slider: Optional[QSlider] = None
        if p.kind != "bool":
            self.label = QLabel(tr(p.label))
            self.label.setFixedWidth(150)
            self.label.setWordWrap(True)
            row.addWidget(self.label)
        if p.kind == "float":
            self.slider = QSlider(Qt.Orientation.Horizontal)
            self.slider.setRange(0, 1000)
            self.spin = QDoubleSpinBox()
            self.spin.setDecimals(p.decimals)
            self.spin.setRange(p.minimum, p.maximum)
            self.spin.setSingleStep(p.step)
            self.spin.setSuffix(f" {tr(p.unit)}" if p.unit else "")
            self.spin.setFixedWidth(118)
            self.spin.setKeyboardTracking(False)
            row.addWidget(self.slider, 1)
            row.addWidget(self.spin)
            self.slider.valueChanged.connect(self._from_slider)
            self.spin.valueChanged.connect(self._from_spin)
        elif p.kind == "int":
            self.spin = QSpinBox()
            self.spin.setRange(int(p.minimum), int(p.maximum))
            self.spin.setSingleStep(max(1, int(p.step)))
            self.spin.setSuffix(f" {tr(p.unit)}" if p.unit else "")
            self.spin.setFixedWidth(118)
            if p.maximum - p.minimum <= 100:
                self.slider = QSlider(Qt.Orientation.Horizontal)
                self.slider.setRange(int(p.minimum), int(p.maximum))
                self.slider.valueChanged.connect(self._int_slider)
                row.addWidget(self.slider, 1)
            else:
                row.addStretch(1)
            row.addWidget(self.spin)
            self.spin.valueChanged.connect(self._int_spin)
        elif p.kind == "choice":
            self.combo = QComboBox()
            for val, text in p.choices:
                self.combo.addItem(tr(text), val)
            self.combo.currentIndexChanged.connect(lambda _=0: self.changed.emit())
            row.addWidget(self.combo, 1)
        elif p.kind == "bool":
            self.check = QCheckBox(tr(p.label))
            self.check.toggled.connect(lambda _=False: self.changed.emit())
            row.addWidget(self.check, 1)
        else:
            self.edit = QLineEdit()
            self.edit.textChanged.connect(lambda _="": self.changed.emit())
            row.addWidget(self.edit, 1)
        if p.help:
            for w in (self, self.label):
                if w is not None:
                    w.setToolTip(tr(p.help))
        self.set_value(value)

    # ---- slider <-> spin mapping
    def _slider_to_value(self, s: int) -> float:
        p = self.p
        frac = s / 1000.0
        if p.log and p.minimum > 0:
            v = p.minimum * (p.maximum / p.minimum) ** frac
        else:
            v = p.minimum + (p.maximum - p.minimum) * frac
        if not p.log and p.step > 0:
            v = round(v / p.step) * p.step
        return min(p.maximum, max(p.minimum, v))

    def _value_to_slider(self, v: float) -> int:
        p = self.p
        if p.log and p.minimum > 0:
            frac = math.log(max(v, p.minimum) / p.minimum) / math.log(p.maximum / p.minimum)
        else:
            frac = (v - p.minimum) / max(1e-12, p.maximum - p.minimum)
        return int(round(min(1.0, max(0.0, frac)) * 1000))

    def _from_slider(self, s: int) -> None:
        self.spin.blockSignals(True)
        self.spin.setValue(self._slider_to_value(s))
        self.spin.blockSignals(False)
        self.changed.emit()

    def _from_spin(self, v: float) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(self._value_to_slider(v))
        self.slider.blockSignals(False)
        self.changed.emit()

    def _int_slider(self, v: int) -> None:
        self.spin.blockSignals(True)
        self.spin.setValue(v)
        self.spin.blockSignals(False)
        self.changed.emit()

    def _int_spin(self, v: int) -> None:
        if self.slider is not None:
            self.slider.blockSignals(True)
            self.slider.setValue(v)
            self.slider.blockSignals(False)
        self.changed.emit()

    # ---- value access
    def value(self) -> Any:
        k = self.p.kind
        if k == "float":
            return float(self.spin.value())
        if k == "int":
            return int(self.spin.value())
        if k == "choice":
            return self.combo.currentData()
        if k == "bool":
            return self.check.isChecked()
        return self.edit.text()

    def set_value(self, v: Any) -> None:
        k = self.p.kind
        widgets = [w for w in (getattr(self, "spin", None), self.slider, getattr(self, "combo", None),
                               getattr(self, "check", None), getattr(self, "edit", None)) if w is not None]
        for w in widgets:
            w.blockSignals(True)
        if k == "float":
            self.spin.setValue(float(v))
            self.slider.setValue(self._value_to_slider(float(v)))
        elif k == "int":
            self.spin.setValue(int(v))
            if self.slider is not None:
                self.slider.setValue(int(v))
        elif k == "choice":
            idx = max(0, self.combo.findData(v))
            self.combo.setCurrentIndex(idx)
        elif k == "bool":
            self.check.setChecked(bool(v))
        else:
            self.edit.setText(str(v))
        for w in widgets:
            w.blockSignals(False)


class EffectDialog(QDialog):
    CUSTOM = "Custom"

    def __init__(self, effect: Effect, params: Optional[dict], ctx: EffectContext,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.effect = effect
        self.ctx = ctx
        self.setWindowTitle(tr(effect.name))
        self.setModal(True)
        self.setMinimumWidth(MIN_WIDTH)
        self.controls: dict[str, ParamControl] = {}
        self.editors: list[Editor] = []
        self._loading = True
        self._previewing = False
        self._preview_stale = False
        values = effect.clean(params)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(12)

        head = QHBoxLayout()
        title = QLabel(tr(effect.name))
        title.setProperty("role", "title")
        chip = role_label(tr(effect.category).upper(), "section")
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(chip, alignment=Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(head)
        desc = QLabel(tr(effect.description))
        desc.setProperty("role", "description")
        desc.setWordWrap(True)
        root.addWidget(desc)
        applies = QLabel(f"●  {ctx.range_text}")
        applies.setProperty("role", "faint")
        root.addWidget(applies)
        root.addWidget(divider())

        # ---- presets
        self.presets = QComboBox()
        self.presets.addItem(tr(self.CUSTOM), None)
        for name, data in effect.presets.items():
            self.presets.addItem(tr(name), data)
        for fav in ctx.favorites.items:
            if fav["effect"] == effect.id:
                self.presets.addItem(f"★  {fav['name']}", fav["params"])
        if self.presets.count() > 1:
            row = QHBoxLayout()
            row.setSpacing(12)
            lab = QLabel(tr("Preset"))
            lab.setFixedWidth(150)
            row.addWidget(lab)
            row.addWidget(self.presets, 1)
            root.addLayout(row)
        self.presets.currentIndexChanged.connect(self._preset_chosen)

        # ---- custom editors
        claimed: set[str] = set()
        editor: Optional[Editor] = None
        kind = effect.editor
        if kind == "fade_curve":
            editor = FadeCurveEditor()
        elif kind == "compressor":
            editor = CompressorCurveEditor()
        elif kind == "graphic_eq":
            editor = GraphicEQEditor()
        elif kind == "noise_profile":
            editor = NoiseProfileEditor(ctx.noise_store, ctx.capture_noise, ctx.sr)
        if editor is not None:
            self.editors.append(editor)
            claimed.update(editor.claims)
            root.addWidget(editor)
            editor.changed.connect(self._on_changed)

        # ---- generic parameter controls
        body = QVBoxLayout()
        body.setSpacing(9)
        body.setContentsMargins(0, 0, 0, 0)
        groups: dict[str, QVBoxLayout] = {}
        for p in effect.params:
            if p.key in claimed or p.kind == "points":
                continue
            ctl = ParamControl(p, values[p.key])
            ctl.changed.connect(self._on_changed)
            self.controls[p.key] = ctl
            if p.group:
                if p.group not in groups:
                    box = QGroupBox(tr(p.group))
                    inner = QVBoxLayout(box)
                    inner.setSpacing(8)
                    groups[p.group] = inner
                    body.addWidget(box)
                groups[p.group].addWidget(ctl)
            else:
                body.addWidget(ctl)
        holder = QWidget()
        holder.setLayout(body)
        generic_count = len(self.controls)
        if generic_count > 8:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(holder)
            scroll.setMinimumHeight(min(420, 44 * generic_count))
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            root.addWidget(scroll, 1)
        else:
            root.addWidget(holder)

        # ---- channels and length
        self.channel_row = QWidget()
        crow = QHBoxLayout(self.channel_row)
        crow.setContentsMargins(0, 0, 0, 0)
        crow.setSpacing(12)
        clab = QLabel(tr("Apply to"))
        clab.setFixedWidth(150)
        self.channel_combo = QComboBox()
        self.channel_combo.addItem(tr("Both channels"), None)
        self.channel_combo.addItem(tr("Left channel only"), [0])
        self.channel_combo.addItem(tr("Right channel only"), [1])
        crow.addWidget(clab)
        crow.addWidget(self.channel_combo, 1)
        root.addWidget(self.channel_row)
        self.length_label = QLabel("")
        self.length_label.setProperty("role", "dim")
        root.addWidget(self.length_label)
        root.addWidget(divider())

        # ---- buttons
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        fav = make_button(tr("To Favorites"), "ghost", "star")
        fav.clicked.connect(self._to_favorites)
        buttons.addWidget(fav)
        buttons.addStretch(1)
        self.bypass = QCheckBox(tr("Bypass"))
        self.bypass.setToolTip(tr("Preview the original sound, without the effect, to compare"))
        buttons.addWidget(self.bypass)
        self.preview_btn = make_button(tr("Preview"), icon="play")
        self.preview_btn.clicked.connect(self._toggle_preview)
        buttons.addWidget(self.preview_btn)
        cancel = make_button(tr("Cancel"))
        cancel.clicked.connect(self.reject)
        self.apply_btn = make_button(tr("Apply"), "primary")
        self.apply_btn.setDefault(True)
        self.apply_btn.clicked.connect(self._accept)
        buttons.addWidget(cancel)
        buttons.addWidget(self.apply_btn)
        root.addLayout(buttons)

        self._fit_width()
        self._timer = QTimer(self)
        self._timer.setInterval(160)
        self._timer.timeout.connect(self._poll_preview)

        # initial values for editors that own parameters
        for ed in self.editors:
            ed.set_values(values)
        self._loading = False
        self._on_changed()

    def _fit_width(self) -> None:
        """An explicit minimum width switches the layout's own minimum off, so the button row would squeeze its
        widgets (a longer translation, or "Update preview") instead of widening the dialog."""
        wanted = max(MIN_WIDTH, self.layout().minimumSize().width())
        if wanted != self.minimumWidth():
            self.setMinimumWidth(wanted)

    def event(self, ev) -> bool:
        handled = super().event(ev)
        if ev.type() == QEvent.Type.LayoutRequest and self.layout() is not None:      # a label changed: the layout knows now
            self._fit_width()
        return handled

    # ---- values
    def values(self) -> dict:
        out = {k: c.value() for k, c in self.controls.items()}
        for ed in self.editors:
            out.update(ed.values())
        for p in self.effect.params:
            out.setdefault(p.key, p.default)
        return self.effect.clean(out)

    def selected_channels(self) -> Optional[list[int]]:
        if not self.channel_row.isVisible():
            return None
        return self.channel_combo.currentData()

    def extra(self) -> dict:
        sample = self.ctx.noise_store.get("noise")
        if self.effect.id == "noise_reduction" and self.values().get("profile") == "sample" and sample is not None:
            return {"noise": sample}
        return {}

    # ---- reactions
    def _on_changed(self) -> None:
        if self._loading:
            return
        vals = self.values()
        for ed in self.editors:
            ed.on_params(vals)
        for key, ctl in self.controls.items():
            show = ctl.p.show_if
            visible = True
            if show:
                other, want = show
                cur = vals.get(other)
                visible = cur in want if isinstance(want, tuple) else cur == want
            ctl.setVisible(visible)
        # a single channel can only be targeted by effects that work on one channel at a time
        self.channel_row.setVisible(self.ctx.channels == 2 and self.effect.preserves_length(vals)
                                    and self.effect.min_channels < 2)
        if not self.effect.preserves_length(vals) or self.effect.out_frames(self.ctx.frames, self.ctx.sr, vals) != self.ctx.frames:
            new = self.effect.out_frames(self.ctx.frames, self.ctx.sr, vals) / self.ctx.sr
            self.length_label.setText(tr(f"Length:  {format_time(self.ctx.frames / self.ctx.sr, ms=False, short=True)}"
                                      f"   →   {format_time(new, ms=False, short=True)}"))
            self.length_label.setVisible(True)
        else:
            self.length_label.setVisible(False)
        self.presets.blockSignals(True)
        self.presets.setCurrentIndex(0)
        self.presets.blockSignals(False)
        if self._previewing:
            self._preview_stale = True
            self.preview_btn.setText(tr("Update preview"))
            self.preview_btn.setIcon(icons.icon("refresh", 16))

    def _preset_chosen(self, index: int) -> None:
        data = self.presets.itemData(index)
        if not data:
            return
        merged = self.effect.clean({**self.effect.defaults(), **data})
        self._loading = True
        for key, ctl in self.controls.items():
            ctl.set_value(merged[key])
        for ed in self.editors:
            ed.set_values(merged)
        self._loading = False
        self._on_changed()
        self.presets.blockSignals(True)
        self.presets.setCurrentIndex(index)
        self.presets.blockSignals(False)

    def _to_favorites(self) -> None:
        name, ok = QInputDialog.getText(self, tr("Add to Favorites"), tr("Name for this setting:"),
                                        text=tr(self.effect.name))
        if ok and name.strip():
            self.ctx.favorites.add(name.strip(), self.effect.id, self.values())
            QMessageBox.information(self, tr("Favorites"), tr(f"Saved “{name.strip()}”. Find it in the Favorites tab."))

    # ---- preview
    def _toggle_preview(self) -> None:
        if self._previewing and not self._preview_stale:
            self._stop_preview()
            return
        self.ctx.stop_preview()
        vals, extra = self.values(), self.extra()
        bypass, channels = self.bypass.isChecked(), self.selected_channels()

        def job(progress, cancel):
            return self.ctx.render_preview(self.effect, vals, bypass, channels, extra, progress, cancel)

        try:
            ok, audio = run_task(self, tr("Preparing preview…"), job, delay_ms=400)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            QMessageBox.warning(self, tr(self.effect.name), tr(str(exc).strip() or repr(exc)))
            self._stop_preview()
            return
        if not ok or audio is None or len(audio) == 0:
            self._stop_preview()
            return
        self.ctx.play_array(audio, self.ctx.sr)
        self._previewing, self._preview_stale = True, False
        self.preview_btn.setText(tr("Stop"))
        self.preview_btn.setIcon(icons.icon("stop", 16))
        self._timer.start()

    def _poll_preview(self) -> None:
        if not self.ctx.is_preview_playing():
            self._stop_preview()

    def _stop_preview(self) -> None:
        self._timer.stop()
        self.ctx.stop_preview()
        self._previewing = self._preview_stale = False
        self.preview_btn.setText(tr("Preview"))
        self.preview_btn.setIcon(icons.icon("play", 16))

    def _accept(self) -> None:
        if self.ctx.validate is not None:
            problem = self.ctx.validate(self.effect, self.values())
            if problem:
                QMessageBox.warning(self, tr(self.effect.name), tr(problem))
                return
        self._stop_preview()
        self.accept()

    def done(self, result: int) -> None:
        self._stop_preview()
        super().done(result)
