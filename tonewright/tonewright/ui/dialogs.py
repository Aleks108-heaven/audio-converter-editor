"""Smaller dialogs: generators, recording, analysis, silence tools, export and about."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
                               QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QSlider, QVBoxLayout, QWidget)

from .. import __version__
from ..audio import devices
from ..audio.recorder import Recorder
from ..core import analysis, ffmpeg, formats, generate
from ..core.analysis import AudioStats
from ..core.effects import Param
from ..core.document import PieceSeq
from ..core.formats import EncodeSettings
from ..core.timefmt import format_time, parse_time
from . import icons, theme
from .effect_dialog import ParamControl
from .format_picker import FormatPicker
from .widgets import LevelMeter, divider, make_button, role_label
from ..i18n import tr


# ------------------------------------------------------------------ generic parameter dialog
class ParamsDialog(QDialog):
    """A small form built from Param specs, with an optional 'where does it go' chooser."""

    def __init__(self, title: str, description: str, params: list[Param], values: Optional[dict] = None,
                 parent: Optional[QWidget] = None, targets: Optional[list[tuple[str, str]]] = None,
                 ok_text: str = "OK", extra: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(520)
        values = values or {}
        self.controls: dict[str, ParamControl] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(12)
        head = QLabel(title)
        head.setProperty("role", "title")
        root.addWidget(head)
        if description:
            desc = QLabel(description)
            desc.setProperty("role", "description")
            desc.setWordWrap(True)
            root.addWidget(desc)
        root.addWidget(divider())
        for p in params:
            ctl = ParamControl(p, values.get(p.key, p.default))
            ctl.changed.connect(self._refresh)
            self.controls[p.key] = ctl
            root.addWidget(ctl)
        if extra is not None:
            root.addWidget(extra)
        self.target_combo: Optional[QComboBox] = None
        if targets:
            row = QHBoxLayout()
            lab = QLabel(tr("Put it"))
            lab.setFixedWidth(150)
            self.target_combo = QComboBox()
            for text, key in targets:
                self.target_combo.addItem(tr(text), key)
            row.addWidget(lab)
            row.addWidget(self.target_combo, 1)
            root.addLayout(row)
        root.addWidget(divider())
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = make_button(tr("Cancel"))
        cancel.clicked.connect(self.reject)
        ok = make_button(tr(ok_text), "primary")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        root.addLayout(buttons)
        self._refresh()

    def _refresh(self) -> None:
        vals = self.values()
        for ctl in self.controls.values():
            show = ctl.p.show_if
            if show:
                other, want = show
                cur = vals.get(other)
                ctl.setVisible(cur in want if isinstance(want, tuple) else cur == want)

    def values(self) -> dict:
        return {k: c.value() for k, c in self.controls.items()}

    @property
    def target(self) -> str:
        return self.target_combo.currentData() if self.target_combo else "cursor"


TONE_PARAMS = [
    Param("waveform", "Waveform", "choice", "sine", choices=generate.WAVEFORMS),
    Param("freq", "Frequency", "float", 440.0, 20.0, 20000.0, 1.0, 1, "Hz", log=True),
    Param("sweep", "Sweep to another frequency", "bool", False),
    Param("freq_end", "End frequency", "float", 2000.0, 20.0, 20000.0, 1.0, 1, "Hz", log=True, show_if=("sweep", True)),
    Param("sweep_type", "Sweep type", "choice", "log", choices=(("linear", "Linear"), ("log", "Logarithmic")),
          show_if=("sweep", True)),
    Param("amp_db", "Level", "float", -6.0, -60.0, 0.0, 0.5, 1, "dBFS"),
    Param("seconds", "Duration", "float", 3.0, 0.05, 3600.0, 0.1, 2, "s"),
    Param("fade_ms", "Fade at the ends", "float", 5.0, 0.0, 500.0, 1.0, 0, "ms", help="Avoids clicks."),
]
SILENCE_PARAMS = [Param("seconds", "Duration", "float", 2.0, 0.01, 3600.0, 0.1, 2, "s")]
NOISE_PARAMS = [
    Param("color", "Colour", "choice", "pink", choices=generate.NOISE_COLORS,
          help="White is flat, pink falls 3 dB per octave (natural), brown falls 6 dB per octave (rumble)."),
    Param("amp_db", "Level (RMS)", "float", -20.0, -60.0, 0.0, 0.5, 1, "dBFS"),
    Param("seconds", "Duration", "float", 5.0, 0.1, 3600.0, 0.1, 1, "s"),
]
DTMF_PARAMS = [
    Param("digits", "Keys", "text", "5551234", help="0-9, *, #, A-D. A space or comma adds a pause."),
    Param("tone_ms", "Tone length", "float", 120.0, 40.0, 1000.0, 5.0, 0, "ms"),
    Param("gap_ms", "Gap", "float", 80.0, 20.0, 1000.0, 5.0, 0, "ms"),
    Param("amp_db", "Level", "float", -8.0, -60.0, 0.0, 0.5, 1, "dBFS"),
]
SILENCE_TOOL_PARAMS = [
    Param("action", "What to do", "choice", "markers", choices=(
        ("markers", "Mark the silences (add markers)"),
        ("shorten", "Remove silences (keep a short pause)"),
        ("trim", "Trim silence from the start and end"))),
    Param("threshold", "Silence is quieter than", "float", -45.0, -90.0, -10.0, 1.0, 0, "dB"),
    Param("min_seconds", "…for at least", "float", 0.5, 0.05, 10.0, 0.05, 2, "s"),
    Param("keep", "Keep this much pause", "float", 0.3, 0.0, 3.0, 0.05, 2, "s", show_if=("action", ("shorten", "trim"))),
]
CONVERT_PARAMS = [
    Param("sr", "Sample rate", "choice", 44100, choices=tuple((r, f"{r} Hz") for r in (8000, 11025, 16000, 22050,
                                                                                        32000, 44100, 48000, 88200, 96000))),
    Param("channels", "Channels", "choice", 2, choices=((1, "Mono"), (2, "Stereo"))),
]
MIX_PARAMS = [
    Param("clip_db", "Pasted audio level", "float", -3.0, -40.0, 12.0, 0.5, 1, "dB"),
    Param("base_db", "Existing audio level", "float", 0.0, -40.0, 12.0, 0.5, 1, "dB"),
]


# ------------------------------------------------------------------ speech
class SpeechDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Speech"))
        self.setModal(True)
        self.setMinimumWidth(560)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(12)
        head = QLabel(tr("Speech"))
        head.setProperty("role", "title")
        root.addWidget(head)
        desc = QLabel(tr("Turns text into speech using the voices installed in Windows."))
        desc.setProperty("role", "description")
        root.addWidget(desc)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText(tr("Type or paste the text to speak…"))
        self.text.setMinimumHeight(130)
        root.addWidget(self.text)
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.voice = QComboBox()
        self.voice.addItem(tr("Default voice"), "")
        for v in generate.list_voices():
            self.voice.addItem(v, v)
        self.rate = QSlider(Qt.Orientation.Horizontal)
        self.rate.setRange(-10, 10)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(100)
        form.addRow(tr("Voice"), self.voice)
        form.addRow(tr("Speed"), self.rate)
        form.addRow(tr("Volume"), self.volume)
        root.addLayout(form)
        self.target = QComboBox()
        self.target.addItem(tr("Insert at the cursor"), "cursor")
        self.target.addItem(tr("Replace the selection"), "selection")
        self.target.addItem(tr("Create a new file"), "new")
        form.addRow(tr("Put it"), self.target)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = make_button(tr("Cancel"))
        cancel.clicked.connect(self.reject)
        ok = make_button(tr("Generate speech"), "primary")
        ok.clicked.connect(self._ok)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        root.addLayout(buttons)

    def _ok(self) -> None:
        if not self.text.toPlainText().strip():
            QMessageBox.information(self, tr("Speech"), tr("Type some text first."))
            return
        self.accept()

    def result_values(self) -> dict:
        return {"text": self.text.toPlainText(), "voice": self.voice.currentData(), "rate": self.rate.value(),
                "volume": self.volume.value(), "target": self.target.currentData()}


# ------------------------------------------------------------------ recording
class RecordDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None, *, can_insert: bool = False,
                 has_selection: bool = False, default_sr: int = 44100, device: Optional[int] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Record"))
        self.setModal(True)
        self.setMinimumWidth(520)
        self.recorder: Optional[Recorder] = None
        self.result_source = None
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(12)
        head = QLabel(tr("Record"))
        head.setProperty("role", "title")
        root.addWidget(head)

        form = QFormLayout()
        form.setSpacing(10)
        self.device = QComboBox()
        self.inputs = devices.input_devices()
        for d in self.inputs:
            self.device.addItem(d.name, d.index)
        if not self.inputs:
            self.device.addItem(tr("No microphone found"), None)
        self.rate = QComboBox()
        for r in (44100, 48000, 32000, 22050, 16000):
            self.rate.addItem(tr(f"{r} Hz"), r)
        self.rate.setCurrentIndex(max(0, self.rate.findData(default_sr)))
        self.channels = QComboBox()
        self.channels.addItem(tr("Stereo"), 2)
        self.channels.addItem(tr("Mono"), 1)
        self.target = QComboBox()
        self.target.addItem(tr("Create a new file"), "new")
        if can_insert:
            self.target.addItem(tr("Insert at the cursor"), "cursor")
            if has_selection:
                self.target.addItem(tr("Replace the selection"), "selection")
        form.addRow(tr("Input"), self.device)
        form.addRow(tr("Sample rate"), self.rate)
        form.addRow(tr("Channels"), self.channels)
        form.addRow(tr("Put the recording"), self.target)
        root.addLayout(form)
        root.addWidget(divider())

        self.time = QLabel("00:00:00.000")
        self.time.setFont(theme.mono_font(34, QFont.Weight.DemiBold))
        self.time.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.time)
        self.meter = LevelMeter()
        self.meter.setFixedHeight(22)
        root.addWidget(self.meter)
        self.status = QLabel(tr("Ready. Press Record to start."))
        self.status.setProperty("role", "dim")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.status)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.record_btn = make_button(tr("Record"), "danger", "record")
        self.pause_btn = make_button(tr("Pause"), icon="pause")
        self.pause_btn.setEnabled(False)
        self.stop_btn = make_button(tr("Stop && keep"), "primary", "stop")
        self.stop_btn.setEnabled(False)
        cancel = make_button(tr("Cancel"))
        buttons.addWidget(self.record_btn)
        buttons.addWidget(self.pause_btn)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(self.stop_btn)
        root.addLayout(buttons)
        self.record_btn.clicked.connect(self._start)
        self.pause_btn.clicked.connect(self._pause)
        self.stop_btn.clicked.connect(self._stop)
        cancel.clicked.connect(self.reject)
        self.record_btn.setEnabled(bool(self.inputs))
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)

    def _start(self) -> None:
        try:
            self.recorder = Recorder(self.device.currentData(), self.rate.currentData(), self.channels.currentData())
            self.recorder.start()
        except devices.AudioDeviceError as exc:
            self.status.setText(str(exc))
            self.status.setProperty("role", "bad")
            self.status.style().unpolish(self.status)
            self.status.style().polish(self.status)
            self.recorder = None
            return
        for w in (self.device, self.rate, self.channels, self.record_btn):
            w.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self.status.setText(tr("Recording…"))
        self._timer.start()

    def _pause(self) -> None:
        if not self.recorder:
            return
        if self.recorder.state == "recording":
            self.recorder.pause()
            self.pause_btn.setText(tr("Resume"))
            self.pause_btn.setIcon(icons.icon("play", 16))
            self.status.setText(tr("Paused"))
        else:
            self.recorder.resume()
            self.pause_btn.setText(tr("Pause"))
            self.pause_btn.setIcon(icons.icon("pause", 16))
            self.status.setText(tr("Recording…"))

    def _tick(self) -> None:
        if self.recorder:
            self.time.setText(format_time(self.recorder.seconds))
            self.meter.feed(*self.recorder.consume_peak())

    def _stop(self) -> None:
        if not self.recorder:
            return
        self._timer.stop()
        try:
            self.result_source = self.recorder.stop()
        except devices.AudioDeviceError as exc:
            QMessageBox.warning(self, tr("Record"), str(exc))
            return
        self.recorder = None
        if self.result_source.frames == 0:
            self.result_source = None
            QMessageBox.information(self, tr("Record"), tr("Nothing was recorded."))
            return
        self.accept()

    def done(self, result: int) -> None:
        self._timer.stop()
        if self.recorder:
            self.recorder.cancel()
            self.recorder = None
        super().done(result)

    @property
    def where(self) -> str:
        return self.target.currentData()


# ------------------------------------------------------------------ analysis
class AnalyzeDialog(QDialog):
    def __init__(self, stats: AudioStats, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Audio statistics"))
        self.setModal(True)
        self.setMinimumWidth(480)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(12)
        head = QLabel(tr("Audio statistics"))
        head.setProperty("role", "title")
        root.addWidget(head)
        sub = QLabel(title)
        sub.setProperty("role", "description")
        root.addWidget(sub)
        root.addWidget(divider())
        names = [tr("Left"), tr("Right")] if stats.channels == 2 else [tr("Mono")]
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(8)
        rows: list[tuple[str, list[str], Optional[str]]] = [
            (tr("Duration"), [format_time(stats.duration)], None),
            (tr("Peak level"), [f"{v:.1f} dBFS" for v in stats.peak_db], "bad" if max(stats.peak) >= 0.9999 else None),
            (tr("Average level (RMS)"), [f"{v:.1f} dBFS" for v in stats.rms_db], None),
            (tr("DC offset"), [f"{v * 100:+.2f} %" for v in stats.dc], "warn" if max(abs(v) for v in stats.dc) > 0.01 else None),
            (tr("Clipped samples"), [f"{stats.clipped:,}"], "bad" if stats.clipped else "good"),
        ]
        if stats.loudness is not None:
            rows.append((tr("Integrated loudness"), [f"{stats.loudness:.1f} LUFS"], None))
            if stats.true_peak is not None:
                rows.append((tr("True peak"), [f"{stats.true_peak:.1f} dBTP"], None))
            if stats.loudness_range is not None:
                rows.append((tr("Loudness range"), [f"{stats.loudness_range:.1f} LU"], None))
        grid.addWidget(QLabel(""), 0, 0)
        for i, n in enumerate(names):
            h = role_label(n.upper(), "section")
            h.setAlignment(Qt.AlignmentFlag.AlignRight)
            grid.addWidget(h, 0, i + 1)
        for r, (label, vals, role) in enumerate(rows, start=1):
            lab = QLabel(label)
            lab.setProperty("role", "dim")
            grid.addWidget(lab, r, 0)
            if len(vals) == 1 and len(names) == 2 and label in (tr("Duration"), tr("Clipped samples"), tr("Integrated loudness"),
                                                                  tr("True peak"), tr("Loudness range")):
                v = QLabel(vals[0])
                v.setFont(theme.mono_font(10.5))
                v.setAlignment(Qt.AlignmentFlag.AlignRight)
                if role:
                    v.setProperty("role", role)
                grid.addWidget(v, r, 1, 1, 2)
            else:
                for i, text in enumerate(vals):
                    v = QLabel(text)
                    v.setFont(theme.mono_font(10.5))
                    v.setAlignment(Qt.AlignmentFlag.AlignRight)
                    if role:
                        v.setProperty("role", role)
                    grid.addWidget(v, r, i + 1)
        root.addLayout(grid)
        hints = []
        if stats.clipped:
            hints.append(tr("Clipping detected: lower the level (Amplify) or use Normalize/Limiter."))
        if max(abs(v) for v in stats.dc) > 0.01:
            hints.append(tr("A DC offset is present; Remove DC Offset will recentre the waveform."))
        if hints:
            note = QLabel("\n".join(hints))
            note.setProperty("role", "warn")
            note.setWordWrap(True)
            root.addWidget(note)
        root.addWidget(divider())
        buttons = QHBoxLayout()
        copy = make_button(tr("Copy to clipboard"), "ghost", "copy")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self._text(stats, names)))
        ok = make_button(tr("Close"), "primary")
        ok.clicked.connect(self.accept)
        buttons.addWidget(copy)
        buttons.addStretch(1)
        buttons.addWidget(ok)
        root.addLayout(buttons)

    @staticmethod
    def _text(stats: AudioStats, names: list[str]) -> str:
        lines = [tr(f"Duration: {format_time(stats.duration)}")]
        for i, n in enumerate(names):
            lines.append(tr(f"{n}: peak {stats.peak_db[i]:.1f} dBFS, RMS {stats.rms_db[i]:.1f} dBFS, DC {stats.dc[i] * 100:+.2f}%"))
        lines.append(tr(f"Clipped samples: {stats.clipped}"))
        if stats.loudness is not None:
            lines.append(tr(f"Loudness: {stats.loudness:.1f} LUFS, true peak {stats.true_peak} dBTP, range {stats.loudness_range} LU"))
        return "\n".join(lines)


class SpectrumPlot(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(640, 300)
        self.freqs = np.zeros(1)
        self.db = np.zeros((1, 1))
        self.channels = 1

    def set_data(self, freqs: np.ndarray, db: np.ndarray) -> None:
        self.freqs, self.db = freqs, db
        self.channels = db.shape[0]
        self.update()

    def paintEvent(self, ev) -> None:
        t = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(QPen(theme.qcolor(t.line_strong), 1))
        p.setBrush(theme.qcolor(t.wave_bg))
        p.drawRoundedRect(r, 8, 8)
        inner = r.adjusted(46, 12, -14, -26)
        nyq = float(self.freqs[-1]) if len(self.freqs) > 1 else 22050.0
        lo = 20.0

        def fx(f: float) -> float:
            return inner.left() + (math.log10(max(f, lo)) - math.log10(lo)) / (math.log10(nyq) - math.log10(lo)) * inner.width()

        def dy(db: float) -> float:
            return inner.bottom() - (max(-120.0, min(0.0, db)) + 120) / 120 * inner.height()

        p.setFont(theme.ui_font(8))
        for db in range(-120, 1, 20):
            y = dy(db)
            p.setPen(QPen(theme.qcolor(t.grid), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(inner.left(), y), QPointF(inner.right(), y))
            p.setPen(theme.qcolor(t.text_faint))
            p.drawText(QRectF(r.left() + 2, y - 8, 40, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(db))
        for f in (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000):
            if f >= nyq:
                continue
            x = fx(f)
            p.setPen(QPen(theme.qcolor(t.grid), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(x, inner.top()), QPointF(x, inner.bottom()))
            p.setPen(theme.qcolor(t.text_faint))
            p.drawText(QRectF(x - 24, inner.bottom() + 5, 48, 14), Qt.AlignmentFlag.AlignCenter,
                       f"{f // 1000}k" if f >= 1000 else str(f))
        colors = [t.accent, t.sel_edge]
        mask = self.freqs >= lo
        for c in range(self.channels):
            path = QPainterPath()
            first = True
            xs = self.freqs[mask]
            ys = self.db[c][mask]
            # thin the points so the path stays light on very long FFTs
            step = max(1, len(xs) // 1500)
            for f, d in zip(xs[::step], ys[::step]):
                pt = QPointF(fx(float(f)), dy(float(d)))
                if first:
                    path.moveTo(pt)
                    first = False
                else:
                    path.lineTo(pt)
            p.setPen(QPen(theme.qcolor(colors[c % 2]), 1.6))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)


class SpectrumDialog(QDialog):
    def __init__(self, seq: PieceSeq, a: int, b: int, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Frequency analysis"))
        self.seq, self.a, self.b = seq, a, b
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(10)
        head = QLabel(tr("Frequency analysis"))
        head.setProperty("role", "title")
        root.addWidget(head)
        sub = QLabel(title)
        sub.setProperty("role", "description")
        root.addWidget(sub)
        self.plot = SpectrumPlot()
        root.addWidget(self.plot, 1)
        row = QHBoxLayout()
        self.size = QComboBox()
        for n in (1024, 2048, 4096, 8192, 16384, 32768):
            self.size.addItem(tr(f"{n} points"), n)
        self.size.setCurrentIndex(3)
        self.peak = QLabel("")
        self.peak.setProperty("role", "dim")
        legend = QLabel("●" if seq.channels == 1 else tr("●  Left   ●  Right"))
        legend.setProperty("role", "faint")
        row.addWidget(QLabel(tr("Resolution")))
        row.addWidget(self.size)
        row.addSpacing(16)
        row.addWidget(self.peak, 1)
        row.addWidget(legend)
        close = make_button(tr("Close"), "primary")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        root.addLayout(row)
        self.size.currentIndexChanged.connect(self._compute)
        self.resize(820, 520)
        self._compute()

    def _compute(self) -> None:
        freqs, db = analysis.average_spectrum(self.seq, self.a, self.b, self.size.currentData())
        self.plot.set_data(freqs, db)
        mix = db.max(axis=0)
        mask = freqs >= 20
        i = int(np.argmax(np.where(mask, mix, -1e9)))
        self.peak.setText(tr(f"Strongest component: {freqs[i]:.0f} Hz at {mix[i]:.1f} dBFS"))


# ------------------------------------------------------------------ export
class ExportDialog(QDialog):
    def __init__(self, parent: Optional[QWidget], default_path: Path, settings: EncodeSettings, src_sr: int,
                 src_channels: int, *, has_selection: bool = False, title: str = "Save as") -> None:
        super().__init__(parent)
        title = tr(title)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(560)
        self._dir = default_path.parent
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(12)
        head = QLabel(title)
        head.setProperty("role", "title")
        root.addWidget(head)
        row = QHBoxLayout()
        self.path = QLineEdit(str(default_path))
        browse = make_button(tr("Browse…"))
        browse.clicked.connect(self._browse)
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        root.addLayout(row)
        self.picker = FormatPicker(settings)
        self.picker.set_source(src_sr, src_channels)
        root.addWidget(self.picker)
        self.selection_only = QCheckBox(tr("Save only the selected part"))
        self.selection_only.setVisible(has_selection)
        self.selection_only.setChecked(False)          # never the default: it would silently drop the rest
        root.addWidget(self.selection_only)
        self.summary = QLabel("")
        self.summary.setProperty("role", "dim")
        root.addWidget(self.summary)
        root.addWidget(divider())
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = make_button(tr("Cancel"))
        cancel.clicked.connect(self.reject)
        ok = make_button(tr("Save"), "primary")
        ok.setDefault(True)
        ok.clicked.connect(self._ok)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        root.addLayout(buttons)
        self._src = (src_sr, src_channels)
        self.picker.changed.connect(self._format_changed)
        self._format_changed()

    def _format_changed(self) -> None:
        s = self.picker.settings()
        spec = s.spec
        p = Path(self.path.text())
        self.path.setText(str(p.with_suffix(spec.ext)))
        self.summary.setText(formats.describe(s, *self._src))

    def _browse(self) -> None:
        spec = self.picker.settings().spec
        name, _ = QFileDialog.getSaveFileName(self, tr("Save as"), self.path.text(), f"{tr(spec.name)} (*{spec.ext})")
        if name:
            self.path.setText(str(Path(name).with_suffix(spec.ext)))

    def _ok(self) -> None:
        if not self.path.text().strip():
            return
        self.accept()

    def result_values(self) -> tuple[Path, EncodeSettings, bool]:
        return Path(self.path.text().strip()), self.picker.settings(), self.selection_only.isChecked()


# ------------------------------------------------------------------ about
class AboutDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("About Tonewright"))
        self.setModal(True)
        self.setFixedWidth(440)
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 20)
        root.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(icons.logo_pixmap(72))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(logo)
        name = QLabel(tr("Tonewright"))
        name.setProperty("role", "title")
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(name)
        ver = QLabel(tr(f"Version {__version__}  ·  audio converter, editor and audiobook maker"))
        ver.setProperty("role", "dim")
        ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ver.setWordWrap(True)
        root.addWidget(ver)
        try:
            ffver = ffmpeg.subprocess.run([ffmpeg.find_tool("ffmpeg"), "-version"], capture_output=True,
                                          creationflags=getattr(ffmpeg.subprocess, "CREATE_NO_WINDOW", 0)
                                          ).stdout.decode("utf-8", "replace").splitlines()[0]
        except Exception:
            ffver = tr("FFmpeg not found")
        info = QLabel(tr(f"Powered by FFmpeg ({ffver.replace('ffmpeg version ', '').split(' Copyright')[0]}),\n"
                      "PySide6 (Qt) and NumPy. Everything runs offline on this PC."))
        info.setProperty("role", "faint")
        info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(info)
        root.addWidget(divider())
        ok = make_button(tr("Close"), "primary")
        ok.clicked.connect(self.accept)
        root.addWidget(ok, alignment=Qt.AlignmentFlag.AlignCenter)


def ask_time(parent: QWidget, title: str, label: str, current: float) -> Optional[float]:
    from PySide6.QtWidgets import QInputDialog
    text, ok = QInputDialog.getText(parent, title, label, text=format_time(current))
    if not ok:
        return None
    value = parse_time(text)
    if value is None:
        QMessageBox.warning(parent, title, tr("That is not a valid time. Try 1:23.5, 83.5 or 0:01:23.500."))
        return None
    return value
