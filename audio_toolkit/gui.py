"""Tkinter GUI for audio_toolkit. Wraps the same core.py functions as the CLI.

Layout: a persistent waveform workspace (with transport controls and
selection/file info) always sits at the top of the window, since that's the
one thing every tool works with or around. Below it, a narrow grouped tool
tree on the left selects which contextual panel shows on the right. A single
shared "current file" (AppState) is set once via the toolbar's Open button;
every single-file tool (Convert, Trim/Cut, Split, Effects, Equalizer,
Channels, Tags) operates on it directly. Multi-file tools (Join, Mix, Batch
Queue, Combine in Channels) keep their own explicit file pickers since they
aren't operating on one "current" file.
"""

from __future__ import annotations

import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, ttk

import numpy as np
from pydub import AudioSegment

from . import batch as batch_module
from . import channels, core, effects, equalizer, generate, tags

FILETYPES = [("Audio files", "*.mp3 *.flac *.wav *.m4a *.wma *.aac *.mp2 *.amr *.ogg"), ("All files", "*.*")]

BG_COLOR = "#f4f5f7"
PANEL_COLOR = "#ffffff"
ACCENT_COLOR = "#2f6fed"
OK_COLOR = "#1a7f37"
ERR_COLOR = "#c0392b"
MUTED_COLOR = "#6b7280"
BORDER_COLOR = "#d8dbe2"
WAVE_BG = "#1c1f26"
WAVE_FG = "#5b8dee"
WAVE_SELECTION = "#2f6fed"
WAVE_MARKER = "#e0a72f"
WAVE_PLAYHEAD = "#ff5a5f"


def _short_path(path: str, max_len: int = 60) -> str:
    """Shorten a long absolute path to '.../parent/file.ext' for display in status messages."""
    if len(path) <= max_len:
        return path
    p = Path(path)
    short = f".../{p.parent.name}/{p.name}" if p.parent.name else p.name
    return short if len(short) < len(path) else path


def _format_duration(ms: int) -> str:
    total_s = max(0, ms) // 1000
    mins, secs = divmod(total_s, 60)
    return f"{mins}:{secs:02d}"


# --------------------------------------------------------------------------
# Shared "current file" state
# --------------------------------------------------------------------------


class AppState:
    """The one audio file every single-file tool panel operates on."""

    def __init__(self):
        self.path: str | None = None
        self.duration_ms: int = 0
        self.channels: int = 0
        self.frame_rate: int = 0
        self.samples: np.ndarray | None = None
        self._audio: AudioSegment | None = None
        self._listeners: list = []

    def subscribe(self, callback) -> None:
        self._listeners.append(callback)

    def _notify(self) -> None:
        for callback in self._listeners:
            callback()

    def set_file(self, path: str) -> None:
        audio = core.load(path)  # raises AudioToolError, left for the caller to show
        self.path = path
        self.duration_ms = len(audio)
        self.channels = audio.channels
        self.frame_rate = audio.frame_rate
        self._audio = audio
        samples = np.array(audio.get_array_of_samples()).astype(np.float32)
        if audio.channels > 1:
            samples = samples.reshape((-1, audio.channels)).mean(axis=1)
        self.samples = samples
        self._notify()

    def require_path(self) -> str:
        if not self.path:
            raise core.AudioToolError('Open a file first ("Open Audio" above).')
        return self.path

    def slice_ms(self, start_ms: int, end_ms: int) -> AudioSegment:
        if self._audio is None:
            raise core.AudioToolError('Open a file first ("Open Audio" above).')
        return self._audio[start_ms:end_ms]


# --------------------------------------------------------------------------
# Shared small widgets
# --------------------------------------------------------------------------


class PathRow(ttk.Frame):
    """A labeled entry + browse button, for one file path."""

    def __init__(self, parent, label: str, save: bool = False):
        super().__init__(parent)
        self.save = save
        ttk.Label(self, text=label, width=12).pack(side="left")
        self.var = tk.StringVar()
        self.entry = ttk.Entry(self, textvariable=self.var, width=48)
        self.entry.pack(side="left", padx=4)
        ttk.Button(self, text="Browse...", command=self._browse).pack(side="left")

    def _browse(self) -> None:
        path = (
            filedialog.asksaveasfilename(filetypes=FILETYPES)
            if self.save
            else filedialog.askopenfilename(filetypes=FILETYPES)
        )
        if path:
            self.var.set(path)
            self.entry.icursor("end")
            self.entry.xview_moveto(1.0)

    def get(self) -> str:
        return self.var.get().strip()


class MultiPathBox(ttk.Frame):
    """A listbox of input files with add/remove controls, in order."""

    def __init__(self, parent):
        super().__init__(parent)
        self.listbox = tk.Listbox(self, width=56, height=6, selectmode="extended")
        self.listbox.pack(side="left", fill="both", expand=True)
        btns = ttk.Frame(self)
        btns.pack(side="left", padx=4, fill="y")
        ttk.Button(btns, text="Add...", command=self._add).pack(fill="x")
        ttk.Button(btns, text="Remove", command=self._remove).pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="Up", command=lambda: self._move(-1)).pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="Down", command=lambda: self._move(1)).pack(fill="x", pady=(4, 0))

    def _add(self) -> None:
        for path in filedialog.askopenfilenames(filetypes=FILETYPES):
            self.listbox.insert("end", path)

    def _remove(self) -> None:
        for index in reversed(self.listbox.curselection()):
            self.listbox.delete(index)

    def _move(self, delta: int) -> None:
        selected = list(self.listbox.curselection())
        if not selected:
            return
        indices = selected if delta < 0 else reversed(selected)
        for index in indices:
            new_index = index + delta
            if 0 <= new_index < self.listbox.size():
                value = self.listbox.get(index)
                self.listbox.delete(index)
                self.listbox.insert(new_index, value)
                self.listbox.selection_set(new_index)

    def get_all(self) -> list[str]:
        return list(self.listbox.get(0, "end"))


class StatusLabel(ttk.Label):
    """A status line that wraps instead of stretching the window to fit long paths."""

    def __init__(self, parent, **kwargs):
        kwargs.setdefault("wraplength", 560)
        kwargs.setdefault("justify", "left")
        super().__init__(parent, **kwargs)

    def ok(self, message: str) -> None:
        self.config(text=f"✓ {message}" if message else "", foreground=OK_COLOR)

    def fail(self, message: str) -> None:
        self.config(text=f"✗ {message}", foreground=ERR_COLOR)


def _run_safely(status: StatusLabel, action, success_message: str) -> None:
    try:
        action()
        status.ok(success_message)
    except core.AudioToolError as exc:
        status.fail(f"Error: {exc}")
    except Exception as exc:  # noqa: BLE001 - surface any unexpected failure to the user
        status.fail(f"Unexpected error: {exc}")


def _panel_title(parent, text: str) -> None:
    ttk.Label(parent, text=text, font=("", 12, "bold")).pack(anchor="w", pady=(0, 10))


def _section_title(parent, text: str) -> None:
    ttk.Label(parent, text=text, font=("", 10, "bold")).pack(anchor="w", pady=(10, 4))


# --------------------------------------------------------------------------
# The persistent waveform workspace: waveform + transport + info, always
# visible regardless of which tool panel is active.
# --------------------------------------------------------------------------


class WaveformCanvas(tk.Canvas):
    """The one waveform view shared by the whole app. Supports drag-to-select
    (used by Trim/Cut) or click-to-add-markers (used by Split), switched via
    set_mode(); an empty file prompts to open one on click."""

    def __init__(self, parent, state: AppState, height: int = 180):
        super().__init__(parent, height=height, bg=WAVE_BG, highlightthickness=0, cursor="hand2")
        self.state_ = state
        self.mode = "none"  # "none" | "select" | "markers"
        self.on_selection_change = None
        self.on_markers_change = None
        self.on_empty_click = None
        self.sel_start_ms: int | None = None
        self.sel_end_ms: int | None = None
        self.marker_positions: list[int] = []
        self.playhead_ms: int | None = None

        self.bind("<Configure>", lambda e: self.redraw())
        state.subscribe(self._on_file_changed)
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _on_file_changed(self) -> None:
        self.sel_start_ms = self.sel_end_ms = None
        self.marker_positions = []
        self.playhead_ms = None
        self.redraw()

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.redraw()

    def set_playhead(self, ms: int | None) -> None:
        self.playhead_ms = ms
        self.redraw()

    def _x_to_ms(self, x: int) -> int:
        width = self.winfo_width() or 1
        frac = max(0.0, min(1.0, x / width))
        return int(frac * self.state_.duration_ms)

    def _on_press(self, event) -> None:
        if not self.state_.path:
            if self.on_empty_click:
                self.on_empty_click()
            return
        if self.mode == "select":
            self.sel_start_ms = self._x_to_ms(event.x)
            self.sel_end_ms = self.sel_start_ms
            self.redraw()

    def _on_drag(self, event) -> None:
        if self.mode == "select" and self.sel_start_ms is not None:
            self.sel_end_ms = self._x_to_ms(event.x)
            self.redraw()

    def _on_release(self, event) -> None:
        if self.mode == "select" and self.sel_start_ms is not None and self.sel_end_ms is not None:
            lo, hi = sorted((self.sel_start_ms, self.sel_end_ms))
            self.sel_start_ms, self.sel_end_ms = lo, hi
            if self.on_selection_change:
                self.on_selection_change(lo, hi)
            self.redraw()
        elif self.mode == "markers" and self.state_.path:
            ms = self._x_to_ms(event.x)
            self.marker_positions.append(ms)
            self.marker_positions.sort()
            if self.on_markers_change:
                self.on_markers_change(list(self.marker_positions))
            self.redraw()

    def clear_markers(self) -> None:
        self.marker_positions.clear()
        if self.on_markers_change:
            self.on_markers_change([])
        self.redraw()

    def set_selection(self, start_ms: int | None, end_ms: int | None) -> None:
        self.sel_start_ms, self.sel_end_ms = start_ms, end_ms
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        if width <= 1 or height <= 1:
            return

        samples = self.state_.samples
        if samples is None or len(samples) == 0:
            self.create_rectangle(0, 0, width, height, fill=WAVE_BG, outline="")
            cx, cy = width / 2, height / 2
            self.create_oval(cx - 34, cy - 34, cx + 34, cy + 34, outline=ACCENT_COLOR, width=2)
            self.create_polygon(cx - 10, cy - 16, cx - 10, cy + 16, cx + 16, cy, fill=ACCENT_COLOR, outline="")
            self.create_text(
                cx, cy + 58, text="Click to open an audio file", fill="#c7cbd6", font=("Segoe UI", 11)
            )
            return

        n = len(samples)
        buckets = max(1, min(width, 1200))
        step = max(1, n // buckets)
        mid = height / 2
        max_amp = float(np.abs(samples).max()) or 1.0
        bar_w = width / buckets
        for i in range(buckets):
            start = i * step
            if start >= n:
                break
            chunk = samples[start:start + step]
            if len(chunk) == 0:
                continue
            amp = float(np.abs(chunk).max()) / max_amp
            x = i * bar_w
            self.create_line(x, mid - amp * mid * 0.92, x, mid + amp * mid * 0.92, fill=WAVE_FG)

        duration = self.state_.duration_ms or 1
        if self.mode == "select" and self.sel_start_ms is not None and self.sel_end_ms is not None:
            x0 = (self.sel_start_ms / duration) * width
            x1 = (self.sel_end_ms / duration) * width
            self.create_rectangle(min(x0, x1), 0, max(x0, x1), height, fill=WAVE_SELECTION, stipple="gray25", outline="")
        if self.mode == "markers":
            for ms in self.marker_positions:
                x = (ms / duration) * width
                self.create_line(x, 0, x, height, fill=WAVE_MARKER, width=2)
        if self.playhead_ms is not None:
            x = (self.playhead_ms / duration) * width
            self.create_line(x, 0, x, height, fill=WAVE_PLAYHEAD, width=2)


class TransportBar(ttk.Frame):
    """Play/Stop with a live elapsed-time readout, plus Start/End/Length info
    (selection info in Trim/Cut mode, whole-file info otherwise)."""

    def __init__(self, parent, state: AppState, waveform: WaveformCanvas):
        super().__init__(parent, padding=(4, 8))
        self.state_ = state
        self.waveform = waveform
        self._play_start_wall: float | None = None
        self._play_start_ms = 0
        self._play_end_ms = 0
        self._tick_job = None

        try:
            from . import playback

            self._playback = playback
        except ImportError:
            self._playback = None

        controls = ttk.Frame(self)
        controls.pack(side="left")
        self.play_btn = ttk.Button(controls, text="▶ Play", style="Accent.TButton", command=self._play)
        self.play_btn.pack(side="left")
        ttk.Button(controls, text="■ Stop", command=self._stop).pack(side="left", padx=6)
        if self._playback is None:
            self.play_btn.state(["disabled"])

        self.time_label = ttk.Label(self, text="0:00", font=("Consolas", 18, "bold"))
        self.time_label.pack(side="left", padx=24)

        info = ttk.Frame(self)
        info.pack(side="left", padx=10)
        self.start_var = tk.StringVar(value="—")
        self.end_var = tk.StringVar(value="—")
        self.length_var = tk.StringVar(value="—")
        for col, (label, var) in enumerate(
            [("Start", self.start_var), ("End", self.end_var), ("Length", self.length_var)]
        ):
            cell = ttk.Frame(info)
            cell.grid(row=0, column=col, padx=10)
            ttk.Label(cell, text=label, foreground=MUTED_COLOR).pack(anchor="w")
            ttk.Label(cell, textvariable=var, font=("Consolas", 10)).pack(anchor="w")

        self.file_info_var = tk.StringVar(value="No file open")
        ttk.Label(self, textvariable=self.file_info_var, foreground=MUTED_COLOR).pack(side="right", padx=10)

        state.subscribe(self.refresh_file_info)
        self.refresh_file_info()

    def refresh_file_info(self) -> None:
        s = self.state_
        if s.path:
            fmt = Path(s.path).suffix.lstrip(".").upper()
            channel_desc = "stereo" if s.channels == 2 else "mono"
            self.file_info_var.set(
                f"{Path(s.path).name}  ·  {s.frame_rate}Hz  ·  {channel_desc}  ·  {fmt}"
            )
            self.set_range_display(0, s.duration_ms)
        else:
            self.file_info_var.set("No file open")
            self.set_range_display(None, None)

    def set_range_display(self, start_ms: int | None, end_ms: int | None) -> None:
        if start_ms is None:
            self.start_var.set("—")
            self.end_var.set("—")
            self.length_var.set("—")
        else:
            self.start_var.set(_format_duration(start_ms))
            self.end_var.set(_format_duration(end_ms))
            self.length_var.set(_format_duration(end_ms - start_ms))

    def _play(self) -> None:
        if self._playback is None or not self.state_.path:
            return
        if self.waveform.mode == "select" and self.waveform.sel_start_ms is not None and self.waveform.sel_end_ms is not None:
            start_ms, end_ms = self.waveform.sel_start_ms, self.waveform.sel_end_ms
            segment = self.state_.slice_ms(start_ms, end_ms)
        else:
            start_ms, end_ms = 0, self.state_.duration_ms
            segment = self.state_.slice_ms(0, self.state_.duration_ms)

        try:
            self._playback.play_segment(segment, blocking=False)
        except core.AudioToolError:
            return
        self._play_start_wall = time.time()
        self._play_start_ms = start_ms
        self._play_end_ms = end_ms
        self._tick()

    def _tick(self) -> None:
        if self._play_start_wall is None:
            return
        elapsed_ms = int((time.time() - self._play_start_wall) * 1000)
        current_ms = self._play_start_ms + elapsed_ms
        if current_ms >= self._play_end_ms:
            self._stop()
            return
        self.time_label.config(text=_format_duration(current_ms))
        self.waveform.set_playhead(current_ms)
        self._tick_job = self.after(100, self._tick)

    def _stop(self) -> None:
        if self._playback is not None:
            self._playback.stop_playback()
        if self._tick_job is not None:
            self.after_cancel(self._tick_job)
            self._tick_job = None
        self._play_start_wall = None
        self.waveform.set_playhead(None)
        self.time_label.config(text="0:00")


class WaveformWorkspace(ttk.Frame):
    """The persistent card containing the waveform + transport, docked above
    the tool panels so it is always the visual center of the app."""

    def __init__(self, parent, state: AppState):
        super().__init__(parent, style="Card.TFrame", padding=1)
        inner = ttk.Frame(self, style="Card.TFrame", padding=10)
        inner.pack(fill="both", expand=True)

        self.waveform = WaveformCanvas(inner, state)
        self.waveform.pack(fill="x")

        self.transport = TransportBar(inner, state, self.waveform)
        self.transport.pack(fill="x")

        def on_selection(start_ms: int, end_ms: int) -> None:
            self.transport.set_range_display(start_ms, end_ms)

        self.waveform.on_selection_change = on_selection


# --------------------------------------------------------------------------
# Tool panels (each takes (frame, state, workspace) and builds its content)
# --------------------------------------------------------------------------


def _build_convert_panel(frame, state: AppState, ws: WaveformWorkspace) -> None:
    _panel_title(frame, "Convert Format")

    out_row = PathRow(frame, "Output:", save=True)
    out_row.pack(anchor="w", pady=4)

    bitrate_frame = ttk.Frame(frame)
    bitrate_frame.pack(anchor="w", pady=4)
    ttk.Label(bitrate_frame, text="Bitrate:", width=12).pack(side="left")
    bitrate_var = tk.StringVar()
    ttk.Entry(bitrate_frame, textvariable=bitrate_var, width=12).pack(side="left")
    ttk.Label(bitrate_frame, text="(optional, e.g. 192k)", foreground=MUTED_COLOR).pack(side="left", padx=6)

    status = StatusLabel(frame, text="")
    status.pack(anchor="w", pady=8)

    def run() -> None:
        _run_safely(
            status,
            lambda: core.convert(state.require_path(), out_row.get(), bitrate=bitrate_var.get().strip() or None),
            f"Converted -> {_short_path(out_row.get())}",
        )

    ttk.Button(frame, text="Convert", style="Accent.TButton", command=run).pack(anchor="w")


def _build_trim_cut_panel(frame, state: AppState, ws: WaveformWorkspace) -> None:
    _panel_title(frame, "Trim / Cut")
    ttk.Label(
        frame, text="Drag on the waveform above to select a range, or type times below.", foreground=MUTED_COLOR
    ).pack(anchor="w", pady=(0, 10))

    out_row = PathRow(frame, "Output:", save=True)
    out_row.pack(anchor="w", pady=4)

    time_frame = ttk.Frame(frame)
    time_frame.pack(anchor="w", pady=4)
    ttk.Label(time_frame, text="Start:", width=12).pack(side="left")
    start_var = tk.StringVar()
    ttk.Entry(time_frame, textvariable=start_var, width=10).pack(side="left")
    ttk.Label(time_frame, text="End:", width=6).pack(side="left", padx=(10, 0))
    end_var = tk.StringVar()
    ttk.Entry(time_frame, textvariable=end_var, width=10).pack(side="left")
    ttk.Label(time_frame, text="(mm:ss)", foreground=MUTED_COLOR).pack(side="left", padx=6)

    def on_selection(start_ms: int, end_ms: int) -> None:
        start_var.set(_format_duration(start_ms))
        end_var.set(_format_duration(end_ms))

    ws.waveform.on_selection_change = on_selection

    mode_var = tk.StringVar(value="trim")
    mode_frame = ttk.Frame(frame)
    mode_frame.pack(anchor="w", pady=4)
    ttk.Radiobutton(mode_frame, text="Trim (keep this range)", variable=mode_var, value="trim").pack(side="left")
    ttk.Radiobutton(mode_frame, text="Cut (delete this range)", variable=mode_var, value="cut").pack(
        side="left", padx=10
    )

    status = StatusLabel(frame, text="")
    status.pack(anchor="w", pady=8)

    def run() -> None:
        def action():
            in_path = state.require_path()
            start_ms = core.ms_from_str(start_var.get())
            end_ms = core.ms_from_str(end_var.get())
            if mode_var.get() == "trim":
                core.trim(in_path, out_row.get(), start_ms, end_ms)
            else:
                core.cut(in_path, out_row.get(), start_ms, end_ms)

        _run_safely(status, action, f"Done -> {_short_path(out_row.get())}")

    ttk.Button(frame, text="Run", style="Accent.TButton", command=run).pack(anchor="w")


def _build_split_panel(frame, state: AppState, ws: WaveformWorkspace) -> None:
    _panel_title(frame, "Split")
    ttk.Label(
        frame, text="Click the waveform above to add split points, or type times below.", foreground=MUTED_COLOR
    ).pack(anchor="w", pady=(0, 10))

    out_dir_frame = ttk.Frame(frame)
    out_dir_frame.pack(anchor="w", pady=4)
    ttk.Label(out_dir_frame, text="Output dir:", width=12).pack(side="left")
    out_dir_var = tk.StringVar()
    ttk.Entry(out_dir_frame, textvariable=out_dir_var, width=46).pack(side="left", padx=4)
    ttk.Button(
        out_dir_frame, text="Browse...", command=lambda: out_dir_var.set(filedialog.askdirectory() or out_dir_var.get())
    ).pack(side="left")

    points_frame = ttk.Frame(frame)
    points_frame.pack(anchor="w", pady=4)
    ttk.Label(points_frame, text="Split at:", width=12).pack(side="left")
    points_var = tk.StringVar()
    ttk.Entry(points_frame, textvariable=points_var, width=36).pack(side="left")
    ttk.Button(points_frame, text="Clear markers", command=ws.waveform.clear_markers).pack(side="left", padx=6)

    def on_markers(points_ms: list[int]) -> None:
        points_var.set(",".join(_format_duration(p) for p in points_ms))

    ws.waveform.on_markers_change = on_markers

    status = StatusLabel(frame, text="")
    status.pack(anchor="w", pady=8)

    def run() -> None:
        def action():
            in_path = state.require_path()
            raw_points = [p.strip() for p in points_var.get().split(",") if p.strip()]
            points = [core.ms_from_str(p) for p in raw_points]
            outputs = core.split(in_path, out_dir_var.get(), points)
            status.ok(f"Split into {len(outputs)} file(s) in {_short_path(out_dir_var.get())}")

        _run_safely(status, action, "")

    ttk.Button(frame, text="Split", style="Accent.TButton", command=run).pack(anchor="w")


def _build_join_panel(frame, state: AppState, ws: WaveformWorkspace) -> None:
    _panel_title(frame, "Join")
    ttk.Label(frame, text="Input files (in order):").pack(anchor="w")
    files_box = MultiPathBox(frame)
    files_box.pack(anchor="w", pady=4, fill="x")

    out_row = PathRow(frame, "Output:", save=True)
    out_row.pack(anchor="w", pady=4)

    gap_frame = ttk.Frame(frame)
    gap_frame.pack(anchor="w", pady=4)
    ttk.Label(gap_frame, text="Gap (sec):", width=12).pack(side="left")
    gap_var = tk.StringVar(value="0")
    ttk.Entry(gap_frame, textvariable=gap_var, width=10).pack(side="left")

    status = StatusLabel(frame, text="")
    status.pack(anchor="w", pady=8)

    def run() -> None:
        def action():
            gap_ms = int(float(gap_var.get() or "0") * 1000)
            core.join(files_box.get_all(), out_row.get(), gap_ms=gap_ms)

        _run_safely(status, action, f"Joined -> {_short_path(out_row.get())}")

    ttk.Button(frame, text="Join", style="Accent.TButton", command=run).pack(anchor="w")


def _build_mix_panel(frame, state: AppState, ws: WaveformWorkspace) -> None:
    _panel_title(frame, "Mix")
    ttk.Label(frame, text="Input files:").pack(anchor="w")
    files_box = MultiPathBox(frame)
    files_box.pack(anchor="w", pady=4, fill="x")

    out_row = PathRow(frame, "Output:", save=True)
    out_row.pack(anchor="w", pady=4)

    gains_frame = ttk.Frame(frame)
    gains_frame.pack(anchor="w", pady=4)
    ttk.Label(gains_frame, text="Gains (dB):", width=12).pack(side="left")
    gains_var = tk.StringVar()
    ttk.Entry(gains_frame, textvariable=gains_var, width=36).pack(side="left")
    ttk.Label(gains_frame, text="(optional, comma-separated)", foreground=MUTED_COLOR).pack(side="left", padx=6)

    status = StatusLabel(frame, text="")
    status.pack(anchor="w", pady=8)

    def run() -> None:
        def action():
            raw = gains_var.get().strip()
            gains = [float(g.strip()) for g in raw.split(",") if g.strip()] if raw else None
            core.mix(files_box.get_all(), out_row.get(), gains_db=gains)

        _run_safely(status, action, f"Mixed -> {_short_path(out_row.get())}")

    ttk.Button(frame, text="Mix", style="Accent.TButton", command=run).pack(anchor="w")


def _build_tags_panel(frame, state: AppState, ws: WaveformWorkspace) -> None:
    _panel_title(frame, "Tags && Rename")

    field_vars: dict[str, tk.StringVar] = {}
    for field in tags.TAG_FIELDS:
        row = ttk.Frame(frame)
        row.pack(anchor="w", pady=2)
        ttk.Label(row, text=field.capitalize() + ":", width=12).pack(side="left")
        var = tk.StringVar()
        ttk.Entry(row, textvariable=var, width=38).pack(side="left")
        field_vars[field] = var

    status = StatusLabel(frame, text="")

    def load_tags() -> None:
        def action():
            values = tags.get_tags(state.require_path())
            for field, var in field_vars.items():
                var.set(values.get(field, ""))
            status.ok("Loaded tags" if values else "No tags set")

        _run_safely(status, action, "")

    def save_tags() -> None:
        def action():
            values = {field: var.get().strip() for field, var in field_vars.items()}
            tags.set_tags(state.require_path(), **values)

        _run_safely(status, action, "Tags saved")

    btn_row = ttk.Frame(frame)
    btn_row.pack(anchor="w", pady=6)
    ttk.Button(btn_row, text="Load", command=load_tags).pack(side="left")
    ttk.Button(btn_row, text="Save", style="Accent.TButton", command=save_tags).pack(side="left", padx=6)

    status.pack(anchor="w", pady=4)

    ttk.Separator(frame).pack(fill="x", pady=10)
    _section_title(frame, "Batch rename from tags")

    rename_files_box = MultiPathBox(frame)
    rename_files_box.pack(anchor="w", pady=4, fill="x")

    pattern_frame = ttk.Frame(frame)
    pattern_frame.pack(anchor="w", pady=4)
    ttk.Label(pattern_frame, text="Pattern:", width=12).pack(side="left")
    pattern_var = tk.StringVar(value="{artist} - {title}")
    ttk.Entry(pattern_frame, textvariable=pattern_var, width=36).pack(side="left")

    rename_status = StatusLabel(frame, text="")
    rename_status.pack(anchor="w", pady=4)

    def run_rename() -> None:
        def action():
            outputs = tags.rename_from_tags(rename_files_box.get_all(), pattern_var.get())
            rename_status.ok(f"Renamed {len(outputs)} file(s)")

        _run_safely(rename_status, action, "")

    ttk.Button(frame, text="Rename", command=run_rename).pack(anchor="w")


def _build_effects_panel(frame, state: AppState, ws: WaveformWorkspace) -> None:
    _panel_title(frame, "Effects")

    out_row = PathRow(frame, "Output:", save=True)
    out_row.pack(anchor="w", pady=4)

    effect_names = ["fade-in", "fade-out", "normalize", "reverse", "echo", "chorus", "flanger", "reverb"]
    effect_var = tk.StringVar(value=effect_names[0])
    effect_frame = ttk.Frame(frame)
    effect_frame.pack(anchor="w", pady=4)
    ttk.Label(effect_frame, text="Effect:", width=12).pack(side="left")
    ttk.Combobox(effect_frame, textvariable=effect_var, values=effect_names, state="readonly", width=15).pack(
        side="left"
    )

    param_frame = ttk.Frame(frame)
    param_frame.pack(anchor="w", pady=4)
    ttk.Label(param_frame, text="Duration (ms):", width=14).pack(side="left")
    duration_var = tk.StringVar(value="1000")
    ttk.Entry(param_frame, textvariable=duration_var, width=8).pack(side="left")
    ttk.Label(param_frame, text="Decay (0-1):", width=12).pack(side="left", padx=(10, 0))
    decay_var = tk.StringVar(value="0.5")
    ttk.Entry(param_frame, textvariable=decay_var, width=8).pack(side="left")
    ttk.Label(
        frame, text="(Duration applies to fades; Decay applies to echo/reverb)", foreground=MUTED_COLOR
    ).pack(anchor="w")

    status = StatusLabel(frame, text="")
    status.pack(anchor="w", pady=8)

    def run() -> None:
        def action():
            in_path = state.require_path()
            out_path = out_row.get()
            name = effect_var.get()
            duration = int(float(duration_var.get() or "1000"))
            decay = float(decay_var.get() or "0.5")
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

        _run_safely(status, action, f"Applied {effect_var.get()} -> {_short_path(out_row.get())}")

    ttk.Button(frame, text="Apply", style="Accent.TButton", command=run).pack(anchor="w")


def _build_record_play_panel(frame, state: AppState, ws: WaveformWorkspace) -> None:
    _panel_title(frame, "Record")
    try:
        from . import playback
    except ImportError as exc:
        ttk.Label(
            frame, text=f"Recording unavailable: {exc}\nInstall sounddevice + numpy to enable this panel."
        ).pack(anchor="w", pady=10)
        return

    ttk.Label(
        frame, text="Use Play/Stop in the workspace above to preview the open file.", foreground=MUTED_COLOR
    ).pack(anchor="w", pady=(0, 10))

    rec_out_row = PathRow(frame, "Output:", save=True)
    rec_out_row.pack(anchor="w", pady=4)

    rec_opts = ttk.Frame(frame)
    rec_opts.pack(anchor="w", pady=4)
    ttk.Label(rec_opts, text="Channels:", width=12).pack(side="left")
    rec_channels_var = tk.StringVar(value="1")
    ttk.Combobox(rec_opts, textvariable=rec_channels_var, values=["1", "2"], state="readonly", width=5).pack(
        side="left"
    )

    rec_status = StatusLabel(frame, text="")
    recorder_holder: dict[str, object] = {}

    def do_record_start() -> None:
        def action():
            recorder = playback.Recorder(channels=int(rec_channels_var.get()))
            recorder.start()
            recorder_holder["recorder"] = recorder

        _run_safely(rec_status, action, "Recording... click Stop when done")

    def do_record_stop() -> None:
        recorder = recorder_holder.get("recorder")
        if recorder is None:
            rec_status.fail("Not currently recording")
            return

        def action():
            recorder.stop(rec_out_row.get())
            state.set_file(rec_out_row.get())  # load the new recording so you can trim/effect it right away

        _run_safely(rec_status, action, f"Saved and opened -> {_short_path(rec_out_row.get())}")
        recorder_holder.pop("recorder", None)

    rec_btns = ttk.Frame(frame)
    rec_btns.pack(anchor="w", pady=4)
    ttk.Button(rec_btns, text="Start Recording", style="Accent.TButton", command=do_record_start).pack(side="left")
    ttk.Button(rec_btns, text="Stop && Save", command=do_record_stop).pack(side="left", padx=6)
    rec_status.pack(anchor="w", pady=4)


def _build_channels_panel(frame, state: AppState, ws: WaveformWorkspace) -> None:
    _panel_title(frame, "Channels")
    _section_title(frame, "Split stereo into left/right mono files")

    split_out_frame = ttk.Frame(frame)
    split_out_frame.pack(anchor="w", pady=4)
    ttk.Label(split_out_frame, text="Output dir:", width=12).pack(side="left")
    split_out_var = tk.StringVar()
    ttk.Entry(split_out_frame, textvariable=split_out_var, width=46).pack(side="left", padx=4)
    ttk.Button(
        split_out_frame,
        text="Browse...",
        command=lambda: split_out_var.set(filedialog.askdirectory() or split_out_var.get()),
    ).pack(side="left")

    split_status = StatusLabel(frame, text="")
    split_status.pack(anchor="w", pady=4)

    def do_split() -> None:
        def action():
            left, right = channels.split_channels(state.require_path(), split_out_var.get())
            split_status.ok(f"-> {_short_path(str(left))}, {_short_path(str(right))}")

        _run_safely(split_status, action, "")

    ttk.Button(frame, text="Split", style="Accent.TButton", command=do_split).pack(anchor="w")

    ttk.Separator(frame).pack(fill="x", pady=10)
    _section_title(frame, "Combine two mono files into stereo")
    left_row = PathRow(frame, "Left:")
    left_row.pack(anchor="w", pady=4)
    right_row = PathRow(frame, "Right:")
    right_row.pack(anchor="w", pady=4)
    combine_out_row = PathRow(frame, "Output:", save=True)
    combine_out_row.pack(anchor="w", pady=4)

    combine_status = StatusLabel(frame, text="")
    combine_status.pack(anchor="w", pady=4)

    def do_combine() -> None:
        _run_safely(
            combine_status,
            lambda: channels.combine_channels(left_row.get(), right_row.get(), combine_out_row.get()),
            f"Combined -> {_short_path(combine_out_row.get())}",
        )

    ttk.Button(frame, text="Combine", command=do_combine).pack(anchor="w")

    ttk.Separator(frame).pack(fill="x", pady=10)
    _section_title(frame, "Adjust left/right channel gain")
    gain_out_row = PathRow(frame, "Output:", save=True)
    gain_out_row.pack(anchor="w", pady=4)

    gain_frame = ttk.Frame(frame)
    gain_frame.pack(anchor="w", pady=4)
    ttk.Label(gain_frame, text="Left dB:", width=10).pack(side="left")
    left_gain_var = tk.StringVar(value="0")
    ttk.Entry(gain_frame, textvariable=left_gain_var, width=8).pack(side="left")
    ttk.Label(gain_frame, text="Right dB:", width=10).pack(side="left", padx=(10, 0))
    right_gain_var = tk.StringVar(value="0")
    ttk.Entry(gain_frame, textvariable=right_gain_var, width=8).pack(side="left")

    gain_status = StatusLabel(frame, text="")
    gain_status.pack(anchor="w", pady=4)

    def do_gain() -> None:
        def action():
            channels.channel_gain(
                state.require_path(),
                gain_out_row.get(),
                left_db=float(left_gain_var.get() or "0"),
                right_db=float(right_gain_var.get() or "0"),
            )

        _run_safely(gain_status, action, f"Applied -> {_short_path(gain_out_row.get())}")

    ttk.Button(frame, text="Apply Gain", command=do_gain).pack(anchor="w")


def _build_generate_panel(frame, state: AppState, ws: WaveformWorkspace) -> None:
    _panel_title(frame, "Generate")

    kind_var = tk.StringVar(value="tone")
    kind_frame = ttk.Frame(frame)
    kind_frame.pack(anchor="w", pady=4)
    ttk.Label(kind_frame, text="Type:", width=12).pack(side="left")
    ttk.Combobox(
        kind_frame, textvariable=kind_var, values=["tone", "white noise", "pink noise", "silence"],
        state="readonly", width=15,
    ).pack(side="left")

    out_row = PathRow(frame, "Output:", save=True)
    out_row.pack(anchor="w", pady=4)

    params = ttk.Frame(frame)
    params.pack(anchor="w", pady=4)
    ttk.Label(params, text="Duration (s):", width=14).pack(side="left")
    duration_var = tk.StringVar(value="5")
    ttk.Entry(params, textvariable=duration_var, width=8).pack(side="left")
    ttk.Label(params, text="Frequency (Hz):", width=14).pack(side="left", padx=(10, 0))
    freq_var = tk.StringVar(value="440")
    ttk.Entry(params, textvariable=freq_var, width=8).pack(side="left")

    channels_frame = ttk.Frame(frame)
    channels_frame.pack(anchor="w", pady=4)
    ttk.Label(channels_frame, text="Channels:", width=14).pack(side="left")
    gen_channels_var = tk.StringVar(value="1")
    ttk.Combobox(channels_frame, textvariable=gen_channels_var, values=["1", "2"], state="readonly", width=5).pack(
        side="left"
    )
    ttk.Label(
        frame, text="(Frequency applies to tone only; Channels applies to silence only)", foreground=MUTED_COLOR
    ).pack(anchor="w")

    status = StatusLabel(frame, text="")
    status.pack(anchor="w", pady=8)

    def run() -> None:
        def action():
            duration = float(duration_var.get())
            kind = kind_var.get()
            if kind == "tone":
                generate.tone(out_row.get(), float(freq_var.get()), duration)
            elif kind == "white noise":
                generate.white_noise(out_row.get(), duration)
            elif kind == "pink noise":
                generate.pink_noise(out_row.get(), duration)
            else:
                generate.silence(out_row.get(), duration, channels=int(gen_channels_var.get()))
            state.set_file(out_row.get())  # load the generated sound so you can trim/effect it right away

        _run_safely(status, action, f"Generated and opened -> {_short_path(out_row.get())}")

    ttk.Button(frame, text="Generate", style="Accent.TButton", command=run).pack(anchor="w")


def _build_audiobook_panel(frame, state: AppState, ws: WaveformWorkspace) -> None:
    _panel_title(frame, "Audiobook / TTS")
    try:
        from . import audiobook
    except ImportError as exc:
        ttk.Label(frame, text=f"Text-to-speech unavailable: {exc}\nInstall pyttsx3 to enable this panel.").pack(
            anchor="w", pady=10
        )
        return

    ttk.Label(frame, text="Text (blank line = new chapter):").pack(anchor="w")
    text_widget = tk.Text(frame, width=64, height=9)
    text_widget.pack(anchor="w", pady=4)

    out_row = PathRow(frame, "Output:", save=True)
    out_row.pack(anchor="w", pady=4)

    voice_frame = ttk.Frame(frame)
    voice_frame.pack(anchor="w", pady=4)
    ttk.Label(voice_frame, text="Voice:", width=12).pack(side="left")
    voice_var = tk.StringVar()
    voice_combo = ttk.Combobox(voice_frame, textvariable=voice_var, state="readonly", width=40)
    voice_combo.pack(side="left")
    voice_ids: dict[str, str] = {}

    status = StatusLabel(frame, text="")

    def load_voices() -> None:
        def action():
            voice_ids.clear()
            names = []
            for voice_id, name in audiobook.list_voices():
                voice_ids[name] = voice_id
                names.append(name)
            voice_combo["values"] = names
            if names:
                voice_var.set(names[0])

        _run_safely(status, action, "Voices loaded")

    ttk.Button(voice_frame, text="Refresh", command=load_voices).pack(side="left", padx=6)

    rate_frame = ttk.Frame(frame)
    rate_frame.pack(anchor="w", pady=4)
    ttk.Label(rate_frame, text="Rate (wpm):", width=12).pack(side="left")
    rate_var = tk.StringVar(value="200")
    ttk.Entry(rate_frame, textvariable=rate_var, width=8).pack(side="left")
    ttk.Label(rate_frame, text="Pause (s):", width=10).pack(side="left", padx=(10, 0))
    pause_var = tk.StringVar(value="1.0")
    ttk.Entry(rate_frame, textvariable=pause_var, width=8).pack(side="left")

    status.pack(anchor="w", pady=8)

    def run() -> None:
        def action():
            text = text_widget.get("1.0", "end").strip()
            voice_id = voice_ids.get(voice_var.get())
            audiobook.create_audiobook(
                text,
                out_row.get(),
                voice_id=voice_id,
                rate=int(rate_var.get() or "200"),
                pause_s=float(pause_var.get() or "1.0"),
            )
            state.set_file(out_row.get())

        _run_safely(status, action, f"Audiobook created and opened -> {_short_path(out_row.get())}")

    ttk.Button(frame, text="Create Audiobook", style="Accent.TButton", command=run).pack(anchor="w")

    load_voices()


def _build_batch_panel(frame, state: AppState, ws: WaveformWorkspace) -> None:
    _panel_title(frame, "Batch Queue")
    ttk.Label(frame, text="Add files, then run one operation across all of them:").pack(anchor="w")

    tree = ttk.Treeview(frame, columns=("status",), show="tree headings", height=7)
    tree.heading("#0", text="File")
    tree.heading("status", text="Status")
    tree.column("status", width=200)
    tree.pack(anchor="w", pady=4, fill="x")

    def add_files() -> None:
        for path in filedialog.askopenfilenames(filetypes=FILETYPES):
            tree.insert("", "end", iid=path, text=path, values=("queued",))

    def remove_selected() -> None:
        for item in tree.selection():
            tree.delete(item)

    def clear_all() -> None:
        for item in tree.get_children():
            tree.delete(item)

    queue_btns = ttk.Frame(frame)
    queue_btns.pack(anchor="w", pady=4)
    ttk.Button(queue_btns, text="Add files...", command=add_files).pack(side="left")
    ttk.Button(queue_btns, text="Remove selected", command=remove_selected).pack(side="left", padx=6)
    ttk.Button(queue_btns, text="Clear", command=clear_all).pack(side="left")

    ttk.Separator(frame).pack(fill="x", pady=10)

    op_var = tk.StringVar(value="convert")
    op_frame = ttk.Frame(frame)
    op_frame.pack(anchor="w", pady=4)
    ttk.Label(op_frame, text="Operation:", width=12).pack(side="left")
    ttk.Radiobutton(op_frame, text="Convert", variable=op_var, value="convert").pack(side="left")
    ttk.Radiobutton(op_frame, text="Effect", variable=op_var, value="effect").pack(side="left", padx=10)

    convert_frame = ttk.Frame(frame)
    convert_frame.pack(anchor="w", pady=2)
    ttk.Label(convert_frame, text="Format:", width=12).pack(side="left")
    format_var = tk.StringVar(value="mp3")
    ttk.Entry(convert_frame, textvariable=format_var, width=10).pack(side="left")
    ttk.Label(convert_frame, text="Bitrate:", width=10).pack(side="left", padx=(10, 0))
    bitrate_var = tk.StringVar()
    ttk.Entry(convert_frame, textvariable=bitrate_var, width=10).pack(side="left")

    effect_names = ["fade-in", "fade-out", "normalize", "reverse", "echo", "chorus", "flanger", "reverb"]
    effect_frame = ttk.Frame(frame)
    effect_frame.pack(anchor="w", pady=2)
    ttk.Label(effect_frame, text="Effect:", width=12).pack(side="left")
    effect_var = tk.StringVar(value=effect_names[0])
    ttk.Combobox(effect_frame, textvariable=effect_var, values=effect_names, state="readonly", width=15).pack(
        side="left"
    )
    ttk.Label(effect_frame, text="Decay:", width=8).pack(side="left", padx=(10, 0))
    decay_var = tk.StringVar(value="0.5")
    ttk.Entry(effect_frame, textvariable=decay_var, width=6).pack(side="left")

    out_dir_frame = ttk.Frame(frame)
    out_dir_frame.pack(anchor="w", pady=4)
    ttk.Label(out_dir_frame, text="Output dir:", width=12).pack(side="left")
    out_dir_var = tk.StringVar()
    ttk.Entry(out_dir_frame, textvariable=out_dir_var, width=46).pack(side="left", padx=4)
    ttk.Button(
        out_dir_frame, text="Browse...", command=lambda: out_dir_var.set(filedialog.askdirectory() or out_dir_var.get())
    ).pack(side="left")

    status = StatusLabel(frame, text="")
    status.pack(anchor="w", pady=8)

    def run_queue() -> None:
        files = list(tree.get_children())
        if not files:
            status.fail("Queue is empty")
            return
        if not out_dir_var.get():
            status.fail("Choose an output directory")
            return

        for path in files:
            tree.set(path, "status", "running...")
        frame.update_idletasks()

        def on_progress(result) -> None:
            tree.set(result.label, "status", "done" if result.success else f"error: {result.error}")
            frame.update_idletasks()

        def action():
            if op_var.get() == "convert":
                return batch_module.batch_convert(
                    files, out_dir_var.get(), format_var.get().strip().lstrip("."),
                    bitrate=bitrate_var.get().strip() or None, on_progress=on_progress,
                )
            return batch_module.batch_effect(
                effect_var.get(), files, out_dir_var.get(), on_progress=on_progress,
                decay=float(decay_var.get() or "0.5"),
            )

        def run_and_report():
            result = action()
            status.ok(f"{len(result.succeeded)}/{len(result.results)} succeeded")

        _run_safely(status, run_and_report, "")

    ttk.Button(frame, text="Run Queue", style="Accent.TButton", command=run_queue).pack(anchor="w")


def _build_equalizer_panel(frame, state: AppState, ws: WaveformWorkspace) -> None:
    _panel_title(frame, "Equalizer")

    out_row = PathRow(frame, "Output:", save=True)
    out_row.pack(anchor="w", pady=4)

    _section_title(frame, "Graphic equalizer (dB per band)")

    bands_frame = ttk.Frame(frame)
    bands_frame.pack(anchor="w")
    band_vars: dict[int, tk.DoubleVar] = {}
    for freq in equalizer.BANDS:
        col = ttk.Frame(bands_frame)
        col.pack(side="left", padx=5)
        var = tk.DoubleVar(value=0.0)
        band_vars[freq] = var
        ttk.Scale(
            col, from_=equalizer.MAX_GAIN_DB, to=equalizer.MIN_GAIN_DB, orient="vertical", variable=var, length=130
        ).pack()
        label = freq if freq < 1000 else f"{freq // 1000}k"
        ttk.Label(col, text=f"{label}Hz").pack()

    eq_status = StatusLabel(frame, text="")

    def reset_bands() -> None:
        for var in band_vars.values():
            var.set(0.0)

    def apply_eq() -> None:
        def action():
            gains = {freq: round(var.get(), 1) for freq, var in band_vars.items()}
            equalizer.apply_eq(state.require_path(), out_row.get(), gains)

        _run_safely(eq_status, action, f"Applied -> {_short_path(out_row.get())}")

    eq_btns = ttk.Frame(frame)
    eq_btns.pack(anchor="w", pady=6)
    ttk.Button(eq_btns, text="Apply EQ", style="Accent.TButton", command=apply_eq).pack(side="left")
    ttk.Button(eq_btns, text="Reset", command=reset_bands).pack(side="left", padx=6)
    eq_status.pack(anchor="w", pady=4)

    ttk.Separator(frame).pack(fill="x", pady=10)
    _section_title(frame, "Simple bass / treble")

    bt_frame = ttk.Frame(frame)
    bt_frame.pack(anchor="w", pady=4)
    ttk.Label(bt_frame, text="Bass dB:", width=10).pack(side="left")
    bass_var = tk.StringVar(value="0")
    ttk.Entry(bt_frame, textvariable=bass_var, width=8).pack(side="left")
    ttk.Label(bt_frame, text="Treble dB:", width=10).pack(side="left", padx=(10, 0))
    treble_var = tk.StringVar(value="0")
    ttk.Entry(bt_frame, textvariable=treble_var, width=8).pack(side="left")

    bt_status = StatusLabel(frame, text="")
    bt_status.pack(anchor="w", pady=4)

    def apply_bass_treble() -> None:
        def action():
            equalizer.apply_bass_treble(
                state.require_path(),
                out_row.get(),
                bass_db=float(bass_var.get() or "0"),
                treble_db=float(treble_var.get() or "0"),
            )

        _run_safely(bt_status, action, f"Applied -> {_short_path(out_row.get())}")

    ttk.Button(frame, text="Apply Bass/Treble", command=apply_bass_treble).pack(anchor="w")


# --------------------------------------------------------------------------
# Toolbar, tool tree, panel container, theme
# --------------------------------------------------------------------------

# (group title, [(panel name, interaction mode for the waveform)])
PANEL_GROUPS = [
    ("File", [("Convert", "none"), ("Tags & Rename", "none"), ("Batch Queue", "none")]),
    ("Edit", [("Trim / Cut", "select"), ("Split", "markers"), ("Join", "none"), ("Mix", "none"), ("Channels", "none")]),
    ("Effects", [("Effects", "none"), ("Equalizer", "none")]),
    ("Create", [("Generate", "none"), ("Audiobook / TTS", "none")]),
    ("Record", [("Record & Play", "none")]),
]

PANEL_BUILDERS = {
    "Convert": _build_convert_panel,
    "Trim / Cut": _build_trim_cut_panel,
    "Split": _build_split_panel,
    "Join": _build_join_panel,
    "Mix": _build_mix_panel,
    "Tags & Rename": _build_tags_panel,
    "Effects": _build_effects_panel,
    "Record & Play": _build_record_play_panel,
    "Channels": _build_channels_panel,
    "Generate": _build_generate_panel,
    "Audiobook / TTS": _build_audiobook_panel,
    "Batch Queue": _build_batch_panel,
    "Equalizer": _build_equalizer_panel,
}

PANEL_WAVE_MODE = {name: mode for _, items in PANEL_GROUPS for name, mode in items}


class Toolbar(ttk.Frame):
    """Top bar: Open on the left (obvious empty-state action), Convert pinned
    on the right (the one action every workflow ends with)."""

    def __init__(self, parent, state: AppState, on_open, on_convert):
        super().__init__(parent, padding=(14, 10))
        ttk.Label(self, text="Audio Converter & Editor", font=("Segoe UI", 13, "bold")).pack(side="left")
        ttk.Button(self, text="\U0001f4c2 Open Audio", style="Accent.TButton", command=on_open).pack(
            side="left", padx=20
        )
        ttk.Button(self, text="Convert / Export", style="Accent.TButton", command=on_convert).pack(side="right")


class ToolTree(ttk.Frame):
    """Narrow, grouped tool navigator — deliberately lighter-weight than the
    waveform workspace above it."""

    def __init__(self, parent, on_select):
        super().__init__(parent, padding=(4, 8), width=190)
        self.pack_propagate(False)
        self.on_select = on_select
        self.tree = ttk.Treeview(self, show="tree", selectmode="browse")
        self.tree.pack(fill="both", expand=True)
        self.tree.column("#0", width=170)
        self.item_to_panel: dict[str, str] = {}

        for group_title, items in PANEL_GROUPS:
            group_id = self.tree.insert("", "end", text=group_title, open=True, tags=("group",))
            for name, _mode in items:
                iid = self.tree.insert(group_id, "end", text=name, tags=("panel",))
                self.item_to_panel[iid] = name

        self.tree.tag_configure("group", foreground=MUTED_COLOR, font=("Segoe UI", 8, "bold"))
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

    def _on_select(self, _event) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        panel = self.item_to_panel.get(selection[0])
        if panel:
            self.on_select(panel)

    def select_panel(self, name: str) -> None:
        for iid, panel in self.item_to_panel.items():
            if panel == name:
                self.tree.selection_set(iid)
                self.tree.see(iid)
                self.on_select(panel)
                return


class PanelContainer(ttk.Frame):
    """Stacks all tool panels and raises the selected one."""

    def __init__(self, parent):
        super().__init__(parent)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.panels: dict[str, ttk.Frame] = {}

    def add_panel(self, name: str, builder, state: AppState, ws: WaveformWorkspace) -> None:
        outer = ttk.Frame(self)
        outer.grid(row=0, column=0, sticky="nsew")
        canvas = tk.Canvas(outer, highlightthickness=0, bg=BG_COLOR)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        inner = ttk.Frame(canvas, padding=18)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(inner_id, width=e.width))

        builder(inner, state, ws)
        self.panels[name] = outer

    def show(self, name: str) -> None:
        panel = self.panels.get(name)
        if panel:
            panel.tkraise()


def _apply_theme(root: tk.Tk) -> None:
    """Native theme where available, larger readable font, an accent button
    style, a card style for the waveform workspace, and roomier spacing."""
    style = ttk.Style(root)
    for theme in ("vista", "clam"):
        if theme in style.theme_names():
            style.theme_use(theme)
            break

    default_font = tkfont.nametofont("TkDefaultFont")
    default_font.configure(family="Segoe UI", size=10)
    root.option_add("*Font", default_font)

    root.configure(bg=BG_COLOR)
    style.configure(".", background=BG_COLOR)
    style.configure("TFrame", background=BG_COLOR)
    style.configure("TLabel", background=BG_COLOR)
    style.configure("TButton", padding=(10, 5))
    style.configure("Accent.TButton", padding=(14, 7), foreground="white", background=ACCENT_COLOR)
    style.map(
        "Accent.TButton",
        background=[("active", "#255bcc"), ("disabled", "#9fb3ef")],
        foreground=[("disabled", "#e8edfb")],
    )
    style.configure("Card.TFrame", background=PANEL_COLOR, relief="solid", borderwidth=1)
    style.configure("Treeview", rowheight=27, font=default_font, background=BG_COLOR, fieldbackground=BG_COLOR)
    style.configure("Treeview.Heading", font=default_font)


def main() -> None:
    root = tk.Tk()
    root.title("Audio Converter & Editor")
    root.geometry("1080x760")
    root.minsize(860, 600)
    _apply_theme(root)

    state = AppState()

    root.grid_rowconfigure(2, weight=1)
    root.grid_columnconfigure(0, weight=1)

    def open_file() -> None:
        path = filedialog.askopenfilename(filetypes=FILETYPES)
        if not path:
            return
        try:
            state.set_file(path)
        except core.AudioToolError:
            pass  # a load failure here is rare; the file simply won't populate the workspace

    def go_convert() -> None:
        tree.select_panel("Convert")

    toolbar = Toolbar(root, state, on_open=open_file, on_convert=go_convert)
    toolbar.grid(row=0, column=0, sticky="ew")

    workspace = WaveformWorkspace(root, state)
    workspace.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 10))
    workspace.waveform.on_empty_click = open_file

    body = ttk.Frame(root)
    body.grid(row=2, column=0, sticky="nsew", padx=14, pady=(0, 14))
    body.grid_rowconfigure(0, weight=1)
    body.grid_columnconfigure(1, weight=1)

    container = PanelContainer(body)

    def select_panel(name: str) -> None:
        workspace.waveform.set_mode(PANEL_WAVE_MODE.get(name, "none"))
        container.show(name)

    tree = ToolTree(body, on_select=select_panel)
    tree.grid(row=0, column=0, sticky="ns", padx=(0, 12))
    container.grid(row=0, column=1, sticky="nsew")

    for name, builder in PANEL_BUILDERS.items():
        container.add_panel(name, builder, state, workspace)

    tree.select_panel("Convert")

    root.mainloop()


if __name__ == "__main__":
    main()
