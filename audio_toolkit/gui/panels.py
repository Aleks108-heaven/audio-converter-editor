"""Tool panels. Each builder takes (state, workspace) and returns a QWidget;
PanelContainer in app.py wraps it in a scroll area and stacks it.

Single-file tools (Convert, Trim/Cut, Split, Effects, Equalizer, Channels,
Tags) operate on the one shared AppState file. Multi-file tools (Join, Mix,
Batch Queue, Combine in Channels) keep their own explicit file pickers since
they aren't operating on one "current" file.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSlider,
    QSpinBox,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import batch as batch_module
from .. import channels, core, effects, equalizer, generate, tags
from . import theme
from .state import AppState
from .waveform import WaveformWorkspace
from .widgets import (
    DirRow,
    MultiPathBox,
    PathRow,
    StatusLabel,
    accent_button,
    format_duration,
    hline,
    muted,
    panel_title,
    run_safely,
    section_title,
    short_path,
)


def _vbox(spacing: int = 10) -> tuple[QWidget, QVBoxLayout]:
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setSpacing(spacing)
    layout.setAlignment(Qt.AlignTop)
    return widget, layout


def _row(*widgets: QWidget, stretch_last: bool = False) -> QWidget:
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    for w in widgets:
        layout.addWidget(w)
    if not stretch_last:
        layout.addStretch(1)
    return row


def _labeled(text: str, widget: QWidget, width: int = 90) -> QWidget:
    lbl = QLabel(text)
    lbl.setFixedWidth(width)
    return _row(lbl, widget)


# --------------------------------------------------------------------------
# Convert
# --------------------------------------------------------------------------


def build_convert_panel(state: AppState, ws: WaveformWorkspace) -> QWidget:
    panel, layout = _vbox()
    layout.addWidget(panel_title("Convert Format"))

    out_row = PathRow("Output:", save=True)
    layout.addWidget(out_row)

    bitrate_edit = QLineEdit()
    bitrate_edit.setPlaceholderText("optional, e.g. 192k")
    bitrate_edit.setFixedWidth(140)
    layout.addWidget(_labeled("Bitrate:", bitrate_edit))

    status = StatusLabel()
    layout.addWidget(status)

    def run() -> None:
        run_safely(
            status,
            lambda: core.convert(state.require_path(), out_row.get(), bitrate=bitrate_edit.text().strip() or None),
            f"Converted -> {short_path(out_row.get())}",
        )

    run_btn = accent_button("Convert")
    run_btn.clicked.connect(run)
    layout.addWidget(run_btn, alignment=Qt.AlignLeft)
    return panel


# --------------------------------------------------------------------------
# Trim / Cut
# --------------------------------------------------------------------------


def build_trim_cut_panel(state: AppState, ws: WaveformWorkspace) -> QWidget:
    panel, layout = _vbox()
    layout.addWidget(panel_title("Trim / Cut"))
    layout.addWidget(muted("Drag on the waveform above to select a range, or type times below."))

    out_row = PathRow("Output:", save=True)
    layout.addWidget(out_row)

    start_edit, end_edit = QLineEdit(), QLineEdit()
    start_edit.setFixedWidth(80)
    end_edit.setFixedWidth(80)
    time_row = QWidget()
    time_layout = QHBoxLayout(time_row)
    time_layout.setContentsMargins(0, 0, 0, 0)
    time_layout.addWidget(_labeled("Start:", start_edit))
    time_layout.addWidget(_labeled("End:", end_edit, width=40))
    time_layout.addWidget(muted("(mm:ss)"))
    time_layout.addStretch(1)
    layout.addWidget(time_row)

    def on_selection(start_ms: int, end_ms: int) -> None:
        start_edit.setText(format_duration(start_ms))
        end_edit.setText(format_duration(end_ms))

    ws.waveform.on_selection_change = on_selection

    trim_radio = QRadioButton("Trim (keep this range)")
    cut_radio = QRadioButton("Cut (delete this range)")
    trim_radio.setChecked(True)
    layout.addWidget(_row(trim_radio, cut_radio))

    status = StatusLabel()
    layout.addWidget(status)

    def run() -> None:
        def action():
            in_path = state.require_path()
            start_ms = core.ms_from_str(start_edit.text())
            end_ms = core.ms_from_str(end_edit.text())
            if trim_radio.isChecked():
                core.trim(in_path, out_row.get(), start_ms, end_ms)
            else:
                core.cut(in_path, out_row.get(), start_ms, end_ms)

        run_safely(status, action, f"Done -> {short_path(out_row.get())}")

    run_btn = accent_button("Run")
    run_btn.clicked.connect(run)
    layout.addWidget(run_btn, alignment=Qt.AlignLeft)
    return panel


# --------------------------------------------------------------------------
# Split
# --------------------------------------------------------------------------


def build_split_panel(state: AppState, ws: WaveformWorkspace) -> QWidget:
    panel, layout = _vbox()
    layout.addWidget(panel_title("Split"))
    layout.addWidget(muted("Click the waveform above to add split points, or type times below."))

    out_dir = DirRow("Output dir:")
    layout.addWidget(out_dir)

    points_edit = QLineEdit()
    points_edit.setMinimumWidth(280)
    clear_btn = QPushButton("Clear markers")
    clear_btn.clicked.connect(ws.waveform.clear_markers)
    layout.addWidget(_row(QLabel("Split at:"), points_edit, clear_btn))

    def on_markers(points_ms: list[int]) -> None:
        points_edit.setText(",".join(format_duration(p) for p in points_ms))

    ws.waveform.on_markers_change = on_markers

    status = StatusLabel()
    layout.addWidget(status)

    def run() -> None:
        def action():
            in_path = state.require_path()
            raw_points = [p.strip() for p in points_edit.text().split(",") if p.strip()]
            points = [core.ms_from_str(p) for p in raw_points]
            outputs = core.split(in_path, out_dir.get(), points)
            status.ok(f"Split into {len(outputs)} file(s) in {short_path(out_dir.get())}")

        run_safely(status, action, "")

    run_btn = accent_button("Split")
    run_btn.clicked.connect(run)
    layout.addWidget(run_btn, alignment=Qt.AlignLeft)
    return panel


# --------------------------------------------------------------------------
# Join
# --------------------------------------------------------------------------


def build_join_panel(state: AppState, ws: WaveformWorkspace) -> QWidget:
    panel, layout = _vbox()
    layout.addWidget(panel_title("Join"))
    layout.addWidget(QLabel("Input files (in order):"))
    files_box = MultiPathBox()
    layout.addWidget(files_box)

    out_row = PathRow("Output:", save=True)
    layout.addWidget(out_row)

    gap_spin = QDoubleSpinBox()
    gap_spin.setRange(0, 60)
    gap_spin.setSingleStep(0.1)
    gap_spin.setSuffix(" s")
    gap_spin.setFixedWidth(100)
    layout.addWidget(_labeled("Gap:", gap_spin))

    status = StatusLabel()
    layout.addWidget(status)

    def run() -> None:
        def action():
            gap_ms = int(gap_spin.value() * 1000)
            core.join(files_box.get_all(), out_row.get(), gap_ms=gap_ms)

        run_safely(status, action, f"Joined -> {short_path(out_row.get())}")

    run_btn = accent_button("Join")
    run_btn.clicked.connect(run)
    layout.addWidget(run_btn, alignment=Qt.AlignLeft)
    return panel


# --------------------------------------------------------------------------
# Mix (mixer-style: per-track gain fader, like a small mixing console)
# --------------------------------------------------------------------------


class _MixerStrip(QWidget):
    def __init__(self, label: str, gain_tenths: int = 0, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        self.value_label = QLabel(f"{gain_tenths / 10:+.1f} dB")
        self.value_label.setAlignment(Qt.AlignCenter)
        self.value_label.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 8pt;")
        layout.addWidget(self.value_label)

        self.slider = QSlider(Qt.Vertical)
        self.slider.setRange(-240, 240)
        self.slider.setValue(gain_tenths)
        self.slider.setFixedHeight(120)
        self.slider.valueChanged.connect(self._on_change)
        layout.addWidget(self.slider, alignment=Qt.AlignHCenter)

        name_label = QLabel(label)
        name_label.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        name_label.setWordWrap(True)
        name_label.setFixedWidth(76)
        name_label.setStyleSheet(f"color: {theme.TEXT}; font-size: 8pt;")
        layout.addWidget(name_label)

    def _on_change(self, value: int) -> None:
        self.value_label.setText(f"{value / 10:+.1f} dB")

    def gain_db(self) -> float:
        return self.slider.value() / 10


def build_mix_panel(state: AppState, ws: WaveformWorkspace) -> QWidget:
    panel, layout = _vbox()
    layout.addWidget(panel_title("Mix"))
    layout.addWidget(muted("Overlays every input at the same start time. Add files, then set each track's gain below."))

    layout.addWidget(QLabel("Input files:"))
    files_box = MultiPathBox()
    layout.addWidget(files_box)

    section_title_w = section_title("Track gains")
    layout.addWidget(section_title_w)

    mixer_row = QWidget()
    mixer_layout = QHBoxLayout(mixer_row)
    mixer_layout.setContentsMargins(0, 0, 0, 0)
    mixer_layout.setSpacing(2)
    mixer_layout.addStretch(1)
    layout.addWidget(mixer_row)

    gains_by_path: dict[str, int] = {}
    strips: list[_MixerStrip] = []

    def rebuild_mixer() -> None:
        for strip in strips:
            mixer_layout.removeWidget(strip)
            strip.deleteLater()
        strips.clear()
        paths = files_box.get_all()
        for i, path in enumerate(paths):
            name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            strip = _MixerStrip(f"{i + 1}. {name}", gains_by_path.get(path, 0))
            strip.slider.valueChanged.connect(lambda v, p=path: gains_by_path.__setitem__(p, v))
            mixer_layout.insertWidget(mixer_layout.count() - 1, strip)
            strips.append(strip)
        if not paths:
            placeholder = QLabel("Add at least two files to mix.")
            placeholder.setProperty("role", "muted")
            mixer_layout.insertWidget(0, placeholder)
            strips.append(placeholder)  # cleaned up on next rebuild too

    files_box.on_change = rebuild_mixer
    rebuild_mixer()

    out_row = PathRow("Output:", save=True)
    layout.addWidget(out_row)

    status = StatusLabel()
    layout.addWidget(status)

    def run() -> None:
        def action():
            paths = files_box.get_all()
            gains = [strip.gain_db() for strip in strips if isinstance(strip, _MixerStrip)]
            gains = gains if len(gains) == len(paths) else None
            core.mix(paths, out_row.get(), gains_db=gains)

        run_safely(status, action, f"Mixed -> {short_path(out_row.get())}")

    run_btn = accent_button("Mix")
    run_btn.clicked.connect(run)
    layout.addWidget(run_btn, alignment=Qt.AlignLeft)
    return panel


# --------------------------------------------------------------------------
# Tags & Rename
# --------------------------------------------------------------------------


def build_tags_panel(state: AppState, ws: WaveformWorkspace) -> QWidget:
    panel, layout = _vbox()
    layout.addWidget(panel_title("Tags & Rename"))

    field_edits: dict[str, QLineEdit] = {}
    form = QFormLayout()
    form.setLabelAlignment(Qt.AlignLeft)
    for field in tags.TAG_FIELDS:
        edit = QLineEdit()
        edit.setMinimumWidth(300)
        field_edits[field] = edit
        form.addRow(field.capitalize() + ":", edit)
    form_widget = QWidget()
    form_widget.setLayout(form)
    layout.addWidget(form_widget)

    status = StatusLabel()

    def load_tags() -> None:
        def action():
            values = tags.get_tags(state.require_path())
            for field, edit in field_edits.items():
                edit.setText(values.get(field, ""))
            status.ok("Loaded tags" if values else "No tags set")

        run_safely(status, action, "")

    def save_tags() -> None:
        def action():
            values = {field: edit.text().strip() for field, edit in field_edits.items()}
            tags.set_tags(state.require_path(), **values)

        run_safely(status, action, "Tags saved")

    load_btn = QPushButton("Load")
    load_btn.clicked.connect(load_tags)
    save_btn = accent_button("Save")
    save_btn.clicked.connect(save_tags)
    layout.addWidget(_row(load_btn, save_btn))
    layout.addWidget(status)

    layout.addWidget(hline())
    layout.addWidget(section_title("Batch rename from tags"))

    rename_files_box = MultiPathBox()
    layout.addWidget(rename_files_box)

    pattern_edit = QLineEdit("{artist} - {title}")
    pattern_edit.setMinimumWidth(260)
    layout.addWidget(_labeled("Pattern:", pattern_edit))

    rename_status = StatusLabel()
    layout.addWidget(rename_status)

    def run_rename() -> None:
        def action():
            outputs = tags.rename_from_tags(rename_files_box.get_all(), pattern_edit.text())
            rename_status.ok(f"Renamed {len(outputs)} file(s)")

        run_safely(rename_status, action, "")

    rename_btn = QPushButton("Rename")
    rename_btn.clicked.connect(run_rename)
    layout.addWidget(rename_btn, alignment=Qt.AlignLeft)
    return panel


# --------------------------------------------------------------------------
# Effects
# --------------------------------------------------------------------------

EFFECT_NAMES = ["fade-in", "fade-out", "normalize", "reverse", "echo", "chorus", "flanger", "reverb"]


def build_effects_panel(state: AppState, ws: WaveformWorkspace) -> QWidget:
    panel, layout = _vbox()
    layout.addWidget(panel_title("Effects"))

    out_row = PathRow("Output:", save=True)
    layout.addWidget(out_row)

    effect_combo = QComboBox()
    effect_combo.addItems(EFFECT_NAMES)
    effect_combo.setFixedWidth(160)
    layout.addWidget(_labeled("Effect:", effect_combo))

    duration_spin = QSpinBox()
    duration_spin.setRange(0, 60000)
    duration_spin.setValue(1000)
    duration_spin.setSuffix(" ms")
    duration_spin.setFixedWidth(110)

    decay_spin = QDoubleSpinBox()
    decay_spin.setRange(0.0, 1.0)
    decay_spin.setSingleStep(0.05)
    decay_spin.setValue(0.5)
    decay_spin.setFixedWidth(90)

    param_row = QWidget()
    param_layout = QHBoxLayout(param_row)
    param_layout.setContentsMargins(0, 0, 0, 0)
    param_layout.addWidget(_labeled("Duration:", duration_spin, width=70))
    param_layout.addWidget(_labeled("Decay:", decay_spin, width=50))
    param_layout.addStretch(1)
    layout.addWidget(param_row)
    layout.addWidget(muted("(Duration applies to fades; Decay applies to echo/reverb)"))

    status = StatusLabel()
    layout.addWidget(status)

    def run() -> None:
        def action():
            in_path = state.require_path()
            out_path = out_row.get()
            name = effect_combo.currentText()
            duration = duration_spin.value()
            decay = decay_spin.value()
            dispatch = {
                "fade-in": lambda: effects.fade_in(in_path, out_path, duration),
                "fade-out": lambda: effects.fade_out(in_path, out_path, duration),
                "normalize": lambda: effects.normalize(in_path, out_path),
                "reverse": lambda: effects.reverse(in_path, out_path),
                "echo": lambda: effects.echo(in_path, out_path, decay=decay),
                "chorus": lambda: effects.chorus(in_path, out_path),
                "flanger": lambda: effects.flanger(in_path, out_path),
                "reverb": lambda: effects.reverb(in_path, out_path, amount=decay),
            }
            dispatch[name]()

        run_safely(status, action, f"Applied {effect_combo.currentText()} -> {short_path(out_row.get())}")

    run_btn = accent_button("Apply")
    run_btn.clicked.connect(run)
    layout.addWidget(run_btn, alignment=Qt.AlignLeft)
    return panel


# --------------------------------------------------------------------------
# Record & Play
# --------------------------------------------------------------------------


def build_record_play_panel(state: AppState, ws: WaveformWorkspace) -> QWidget:
    panel, layout = _vbox()
    layout.addWidget(panel_title("Record"))
    try:
        from .. import playback
    except ImportError as exc:
        layout.addWidget(muted(f"Recording unavailable: {exc}\nInstall sounddevice + numpy to enable this panel."))
        return panel

    layout.addWidget(muted("Use Play/Stop in the workspace above to preview the open file."))

    rec_out_row = PathRow("Output:", save=True)
    layout.addWidget(rec_out_row)

    rec_channels_combo = QComboBox()
    rec_channels_combo.addItems(["1", "2"])
    rec_channels_combo.setFixedWidth(70)
    layout.addWidget(_labeled("Channels:", rec_channels_combo))

    rec_status = StatusLabel()
    recorder_holder: dict[str, object] = {}

    def do_record_start() -> None:
        def action():
            recorder = playback.Recorder(channels=int(rec_channels_combo.currentText()))
            recorder.start()
            recorder_holder["recorder"] = recorder

        run_safely(rec_status, action, "Recording... click Stop when done")

    def do_record_stop() -> None:
        recorder = recorder_holder.get("recorder")
        if recorder is None:
            rec_status.fail("Not currently recording")
            return

        def action():
            recorder.stop(rec_out_row.get())
            state.set_file(rec_out_row.get())  # load the new recording so you can trim/effect it right away

        run_safely(rec_status, action, f"Saved and opened -> {short_path(rec_out_row.get())}")
        recorder_holder.pop("recorder", None)

    start_btn = accent_button("Start Recording")
    start_btn.clicked.connect(do_record_start)
    stop_btn = QPushButton("Stop && Save")
    stop_btn.clicked.connect(do_record_stop)
    layout.addWidget(_row(start_btn, stop_btn))
    layout.addWidget(rec_status)
    return panel


# --------------------------------------------------------------------------
# Channels
# --------------------------------------------------------------------------


def build_channels_panel(state: AppState, ws: WaveformWorkspace) -> QWidget:
    panel, layout = _vbox()
    layout.addWidget(panel_title("Channels"))
    layout.addWidget(section_title("Split stereo into left/right mono files"))

    split_out = DirRow("Output dir:")
    layout.addWidget(split_out)

    split_status = StatusLabel()
    layout.addWidget(split_status)

    def do_split() -> None:
        def action():
            left, right = channels.split_channels(state.require_path(), split_out.get())
            split_status.ok(f"-> {short_path(str(left))}, {short_path(str(right))}")

        run_safely(split_status, action, "")

    split_btn = accent_button("Split")
    split_btn.clicked.connect(do_split)
    layout.addWidget(split_btn, alignment=Qt.AlignLeft)

    layout.addWidget(hline())
    layout.addWidget(section_title("Combine two mono files into stereo"))
    left_row = PathRow("Left:")
    layout.addWidget(left_row)
    right_row = PathRow("Right:")
    layout.addWidget(right_row)
    combine_out_row = PathRow("Output:", save=True)
    layout.addWidget(combine_out_row)

    combine_status = StatusLabel()
    layout.addWidget(combine_status)

    def do_combine() -> None:
        run_safely(
            combine_status,
            lambda: channels.combine_channels(left_row.get(), right_row.get(), combine_out_row.get()),
            f"Combined -> {short_path(combine_out_row.get())}",
        )

    combine_btn = QPushButton("Combine")
    combine_btn.clicked.connect(do_combine)
    layout.addWidget(combine_btn, alignment=Qt.AlignLeft)

    layout.addWidget(hline())
    layout.addWidget(section_title("Adjust left/right channel gain"))
    gain_out_row = PathRow("Output:", save=True)
    layout.addWidget(gain_out_row)

    left_gain_spin = QDoubleSpinBox()
    left_gain_spin.setRange(-24, 24)
    left_gain_spin.setSuffix(" dB")
    left_gain_spin.setFixedWidth(100)
    right_gain_spin = QDoubleSpinBox()
    right_gain_spin.setRange(-24, 24)
    right_gain_spin.setSuffix(" dB")
    right_gain_spin.setFixedWidth(100)
    gain_row = QWidget()
    gain_layout = QHBoxLayout(gain_row)
    gain_layout.setContentsMargins(0, 0, 0, 0)
    gain_layout.addWidget(_labeled("Left:", left_gain_spin, width=50))
    gain_layout.addWidget(_labeled("Right:", right_gain_spin, width=50))
    gain_layout.addStretch(1)
    layout.addWidget(gain_row)

    gain_status = StatusLabel()
    layout.addWidget(gain_status)

    def do_gain() -> None:
        def action():
            channels.channel_gain(
                state.require_path(),
                gain_out_row.get(),
                left_db=left_gain_spin.value(),
                right_db=right_gain_spin.value(),
            )

        run_safely(gain_status, action, f"Applied -> {short_path(gain_out_row.get())}")

    gain_btn = QPushButton("Apply Gain")
    gain_btn.clicked.connect(do_gain)
    layout.addWidget(gain_btn, alignment=Qt.AlignLeft)
    return panel


# --------------------------------------------------------------------------
# Generate
# --------------------------------------------------------------------------


def build_generate_panel(state: AppState, ws: WaveformWorkspace) -> QWidget:
    panel, layout = _vbox()
    layout.addWidget(panel_title("Generate"))

    kind_combo = QComboBox()
    kind_combo.addItems(["tone", "white noise", "pink noise", "silence"])
    kind_combo.setFixedWidth(140)
    layout.addWidget(_labeled("Type:", kind_combo))

    out_row = PathRow("Output:", save=True)
    layout.addWidget(out_row)

    duration_spin = QDoubleSpinBox()
    duration_spin.setRange(0.1, 3600)
    duration_spin.setValue(5)
    duration_spin.setSuffix(" s")
    duration_spin.setFixedWidth(100)

    freq_spin = QDoubleSpinBox()
    freq_spin.setRange(1, 20000)
    freq_spin.setValue(440)
    freq_spin.setSuffix(" Hz")
    freq_spin.setFixedWidth(100)

    gen_channels_combo = QComboBox()
    gen_channels_combo.addItems(["1", "2"])
    gen_channels_combo.setFixedWidth(70)

    params_row = QWidget()
    params_layout = QHBoxLayout(params_row)
    params_layout.setContentsMargins(0, 0, 0, 0)
    params_layout.addWidget(_labeled("Duration:", duration_spin, width=70))
    params_layout.addWidget(_labeled("Frequency:", freq_spin, width=70))
    params_layout.addStretch(1)
    layout.addWidget(params_row)
    layout.addWidget(_labeled("Channels:", gen_channels_combo))
    layout.addWidget(muted("(Frequency applies to tone only; Channels applies to silence only)"))

    status = StatusLabel()
    layout.addWidget(status)

    def run() -> None:
        def action():
            duration = duration_spin.value()
            kind = kind_combo.currentText()
            if kind == "tone":
                generate.tone(out_row.get(), freq_spin.value(), duration)
            elif kind == "white noise":
                generate.white_noise(out_row.get(), duration)
            elif kind == "pink noise":
                generate.pink_noise(out_row.get(), duration)
            else:
                generate.silence(out_row.get(), duration, channels=int(gen_channels_combo.currentText()))
            state.set_file(out_row.get())  # load the generated sound so you can trim/effect it right away

        run_safely(status, action, f"Generated and opened -> {short_path(out_row.get())}")

    run_btn = accent_button("Generate")
    run_btn.clicked.connect(run)
    layout.addWidget(run_btn, alignment=Qt.AlignLeft)
    return panel


# --------------------------------------------------------------------------
# Audiobook / TTS
# --------------------------------------------------------------------------


def build_audiobook_panel(state: AppState, ws: WaveformWorkspace) -> QWidget:
    panel, layout = _vbox()
    layout.addWidget(panel_title("Audiobook / TTS"))
    try:
        from .. import audiobook
    except ImportError as exc:
        layout.addWidget(muted(f"Text-to-speech unavailable: {exc}\nInstall pyttsx3 to enable this panel."))
        return panel

    layout.addWidget(QLabel("Text (blank line = new chapter):"))
    text_edit = QTextEdit()
    text_edit.setFixedHeight(160)
    layout.addWidget(text_edit)

    out_row = PathRow("Output:", save=True)
    layout.addWidget(out_row)

    voice_combo = QComboBox()
    voice_combo.setMinimumWidth(320)
    voice_ids: dict[str, str] = {}
    status = StatusLabel()

    def load_voices() -> None:
        def action():
            voice_ids.clear()
            voice_combo.clear()
            names = []
            for voice_id, name in audiobook.list_voices():
                voice_ids[name] = voice_id
                names.append(name)
            voice_combo.addItems(names)

        run_safely(status, action, "Voices loaded")

    refresh_btn = QPushButton("Refresh")
    refresh_btn.clicked.connect(load_voices)
    layout.addWidget(_row(QLabel("Voice:"), voice_combo, refresh_btn))

    rate_spin = QSpinBox()
    rate_spin.setRange(50, 400)
    rate_spin.setValue(200)
    rate_spin.setSuffix(" wpm")
    rate_spin.setFixedWidth(100)
    pause_spin = QDoubleSpinBox()
    pause_spin.setRange(0, 10)
    pause_spin.setValue(1.0)
    pause_spin.setSuffix(" s")
    pause_spin.setFixedWidth(90)
    rate_row = QWidget()
    rate_layout = QHBoxLayout(rate_row)
    rate_layout.setContentsMargins(0, 0, 0, 0)
    rate_layout.addWidget(_labeled("Rate:", rate_spin, width=50))
    rate_layout.addWidget(_labeled("Pause:", pause_spin, width=50))
    rate_layout.addStretch(1)
    layout.addWidget(rate_row)
    layout.addWidget(status)

    def run() -> None:
        def action():
            text = text_edit.toPlainText().strip()
            voice_id = voice_ids.get(voice_combo.currentText())
            audiobook.create_audiobook(
                text,
                out_row.get(),
                voice_id=voice_id,
                rate=rate_spin.value(),
                pause_s=pause_spin.value(),
            )
            state.set_file(out_row.get())

        run_safely(status, action, f"Audiobook created and opened -> {short_path(out_row.get())}")

    run_btn = accent_button("Create Audiobook")
    run_btn.clicked.connect(run)
    layout.addWidget(run_btn, alignment=Qt.AlignLeft)

    load_voices()
    return panel


# --------------------------------------------------------------------------
# Batch Queue
# --------------------------------------------------------------------------


def build_batch_panel(state: AppState, ws: WaveformWorkspace) -> QWidget:
    panel, layout = _vbox()
    layout.addWidget(panel_title("Batch Queue"))
    layout.addWidget(QLabel("Add files, then run one operation across all of them:"))

    tree = QTreeWidget()
    tree.setHeaderLabels(["File", "Status"])
    tree.setRootIsDecorated(False)
    tree.setFixedHeight(160)
    tree.setColumnWidth(0, 420)
    layout.addWidget(tree)

    def add_files() -> None:
        from .widgets import FILE_FILTER

        paths, _ = QFileDialog.getOpenFileNames(panel, "Add files", "", FILE_FILTER)
        for path in paths:
            item = QTreeWidgetItem([path, "queued"])
            tree.addTopLevelItem(item)

    def remove_selected() -> None:
        for item in tree.selectedItems():
            tree.takeTopLevelItem(tree.indexOfTopLevelItem(item))

    def clear_all() -> None:
        tree.clear()

    add_btn = QPushButton("Add files...")
    add_btn.clicked.connect(add_files)
    remove_btn = QPushButton("Remove selected")
    remove_btn.clicked.connect(remove_selected)
    clear_btn = QPushButton("Clear")
    clear_btn.clicked.connect(clear_all)
    layout.addWidget(_row(add_btn, remove_btn, clear_btn))

    layout.addWidget(hline())

    convert_radio = QRadioButton("Convert")
    effect_radio = QRadioButton("Effect")
    convert_radio.setChecked(True)
    layout.addWidget(_row(QLabel("Operation:"), convert_radio, effect_radio))

    format_edit = QLineEdit("mp3")
    format_edit.setFixedWidth(80)
    bitrate_edit = QLineEdit()
    bitrate_edit.setPlaceholderText("optional")
    bitrate_edit.setFixedWidth(100)
    convert_row = QWidget()
    convert_layout = QHBoxLayout(convert_row)
    convert_layout.setContentsMargins(0, 0, 0, 0)
    convert_layout.addWidget(_labeled("Format:", format_edit, width=60))
    convert_layout.addWidget(_labeled("Bitrate:", bitrate_edit, width=60))
    convert_layout.addStretch(1)
    layout.addWidget(convert_row)

    batch_effect_combo = QComboBox()
    batch_effect_combo.addItems(EFFECT_NAMES)
    batch_decay_spin = QDoubleSpinBox()
    batch_decay_spin.setRange(0.0, 1.0)
    batch_decay_spin.setSingleStep(0.05)
    batch_decay_spin.setValue(0.5)
    batch_decay_spin.setFixedWidth(90)
    effect_row = QWidget()
    effect_layout = QHBoxLayout(effect_row)
    effect_layout.setContentsMargins(0, 0, 0, 0)
    effect_layout.addWidget(_labeled("Effect:", batch_effect_combo, width=60))
    effect_layout.addWidget(_labeled("Decay:", batch_decay_spin, width=50))
    effect_layout.addStretch(1)
    layout.addWidget(effect_row)

    out_dir = DirRow("Output dir:")
    layout.addWidget(out_dir)

    status = StatusLabel()
    layout.addWidget(status)

    def run_queue() -> None:
        files = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
        if not files:
            status.fail("Queue is empty")
            return
        if not out_dir.get():
            status.fail("Choose an output directory")
            return

        items_by_label = {tree.topLevelItem(i).text(0): tree.topLevelItem(i) for i in range(tree.topLevelItemCount())}
        for item in items_by_label.values():
            item.setText(1, "running...")

        def on_progress(result) -> None:
            item = items_by_label.get(result.label)
            if item is not None:
                item.setText(1, "done" if result.success else f"error: {result.error}")

        def action():
            if convert_radio.isChecked():
                return batch_module.batch_convert(
                    files, out_dir.get(), format_edit.text().strip().lstrip("."),
                    bitrate=bitrate_edit.text().strip() or None, on_progress=on_progress,
                )
            return batch_module.batch_effect(
                batch_effect_combo.currentText(), files, out_dir.get(), on_progress=on_progress,
                decay=batch_decay_spin.value(),
            )

        def run_and_report():
            result = action()
            status.ok(f"{len(result.succeeded)}/{len(result.results)} succeeded")

        run_safely(status, run_and_report, "")

    run_btn = accent_button("Run Queue")
    run_btn.clicked.connect(run_queue)
    layout.addWidget(run_btn, alignment=Qt.AlignLeft)
    return panel


# --------------------------------------------------------------------------
# Equalizer
# --------------------------------------------------------------------------


class _EqBand(QWidget):
    def __init__(self, freq: int, parent=None):
        super().__init__(parent)
        self.freq = freq
        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 0, 3, 0)
        layout.setSpacing(2)

        self.value_label = QLabel("0.0")
        self.value_label.setAlignment(Qt.AlignCenter)
        self.value_label.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 8pt;")
        layout.addWidget(self.value_label)

        self.slider = QSlider(Qt.Vertical)
        self.slider.setRange(int(equalizer.MIN_GAIN_DB * 10), int(equalizer.MAX_GAIN_DB * 10))
        self.slider.setValue(0)
        self.slider.setFixedHeight(140)
        self.slider.valueChanged.connect(lambda v: self.value_label.setText(f"{v / 10:+.1f}"))
        layout.addWidget(self.slider, alignment=Qt.AlignHCenter)

        label = f"{freq}" if freq < 1000 else f"{freq // 1000}k"
        freq_label = QLabel(f"{label}Hz")
        freq_label.setAlignment(Qt.AlignCenter)
        freq_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 8pt;")
        layout.addWidget(freq_label)

    def gain_db(self) -> float:
        return round(self.slider.value() / 10, 1)

    def reset(self) -> None:
        self.slider.setValue(0)


def build_equalizer_panel(state: AppState, ws: WaveformWorkspace) -> QWidget:
    panel, layout = _vbox()
    layout.addWidget(panel_title("Equalizer"))

    out_row = PathRow("Output:", save=True)
    layout.addWidget(out_row)

    layout.addWidget(section_title("Graphic equalizer (dB per band)"))

    bands_row = QWidget()
    bands_layout = QHBoxLayout(bands_row)
    bands_layout.setContentsMargins(0, 0, 0, 0)
    bands_layout.setSpacing(6)
    bands: list[_EqBand] = []
    for freq in equalizer.BANDS:
        band = _EqBand(freq)
        bands_layout.addWidget(band)
        bands.append(band)
    bands_layout.addStretch(1)
    layout.addWidget(bands_row)

    eq_status = StatusLabel()

    def reset_bands() -> None:
        for band in bands:
            band.reset()

    def apply_eq() -> None:
        def action():
            gains = {band.freq: band.gain_db() for band in bands}
            equalizer.apply_eq(state.require_path(), out_row.get(), gains)

        run_safely(eq_status, action, f"Applied -> {short_path(out_row.get())}")

    apply_btn = accent_button("Apply EQ")
    apply_btn.clicked.connect(apply_eq)
    reset_btn = QPushButton("Reset")
    reset_btn.clicked.connect(reset_bands)
    layout.addWidget(_row(apply_btn, reset_btn))
    layout.addWidget(eq_status)

    layout.addWidget(hline())
    layout.addWidget(section_title("Simple bass / treble"))

    bass_spin = QDoubleSpinBox()
    bass_spin.setRange(-24, 24)
    bass_spin.setFixedWidth(90)
    treble_spin = QDoubleSpinBox()
    treble_spin.setRange(-24, 24)
    treble_spin.setFixedWidth(90)
    bt_row = QWidget()
    bt_layout = QHBoxLayout(bt_row)
    bt_layout.setContentsMargins(0, 0, 0, 0)
    bt_layout.addWidget(_labeled("Bass:", bass_spin, width=50))
    bt_layout.addWidget(_labeled("Treble:", treble_spin, width=50))
    bt_layout.addStretch(1)
    layout.addWidget(bt_row)

    bt_status = StatusLabel()
    layout.addWidget(bt_status)

    def apply_bass_treble() -> None:
        def action():
            equalizer.apply_bass_treble(
                state.require_path(),
                out_row.get(),
                bass_db=bass_spin.value(),
                treble_db=treble_spin.value(),
            )

        run_safely(bt_status, action, f"Applied -> {short_path(out_row.get())}")

    bt_btn = QPushButton("Apply Bass/Treble")
    bt_btn.clicked.connect(apply_bass_treble)
    layout.addWidget(bt_btn, alignment=Qt.AlignLeft)
    return panel


# --------------------------------------------------------------------------
# Registry: (group title, icon, [(panel name, icon, interaction mode)])
# --------------------------------------------------------------------------

PANEL_GROUPS = [
    ("File", "group_file", [
        ("Convert", "convert", "none"),
        ("Tags & Rename", "tags", "none"),
        ("Batch Queue", "batch", "none"),
    ]),
    ("Edit", "group_edit", [
        ("Trim / Cut", "trim", "select"),
        ("Split", "split", "markers"),
        ("Join", "join", "none"),
        ("Mix", "mix", "none"),
        ("Channels", "channels", "none"),
    ]),
    ("Effects", "group_effects", [
        ("Effects", "effects", "none"),
        ("Equalizer", "equalizer", "none"),
    ]),
    ("Create", "group_create", [
        ("Generate", "generate", "none"),
        ("Audiobook / TTS", "audiobook", "none"),
    ]),
    ("Record", "group_record", [
        ("Record & Play", "record", "none"),
    ]),
]

PANEL_BUILDERS = {
    "Convert": build_convert_panel,
    "Trim / Cut": build_trim_cut_panel,
    "Split": build_split_panel,
    "Join": build_join_panel,
    "Mix": build_mix_panel,
    "Tags & Rename": build_tags_panel,
    "Effects": build_effects_panel,
    "Record & Play": build_record_play_panel,
    "Channels": build_channels_panel,
    "Generate": build_generate_panel,
    "Audiobook / TTS": build_audiobook_panel,
    "Batch Queue": build_batch_panel,
    "Equalizer": build_equalizer_panel,
}

PANEL_WAVE_MODE = {name: mode for _, _icon, items in PANEL_GROUPS for name, _icon2, mode in items}
