"""Editing logic that connects the widgets to the core: files, clipboard, effects, tools, playback."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QDialog, QFileDialog, QInputDialog, QMainWindow, QMessageBox)

from .. import paths as paths_module
from ..audio.devices import AudioDeviceError
from ..core import access, analysis, effects as fx
from ..core import ffmpeg, formats, generate, ops
from ..core import tags as tagmod
from ..core.convert import ConvertItem, ConvertOptions, convert_item
from ..core.document import Clip, Document, Marker, PieceSeq, diff_range, map_frame
from ..core.effects import Effect
from ..core.formats import EncodeSettings
from ..core.generate import ToneSpec
from ..core.source import Source
from ..core.timefmt import format_time
from . import dialogs
from .dialogs import (AnalyzeDialog, ExportDialog, ParamsDialog, RecordDialog, SpeechDialog, SpectrumDialog)
from .edit_panel import EditPanel
from .effect_dialog import EffectContext, EffectDialog
from .library import AUDIO_EXTS, LibraryPanel
from .stage import Stage
from .state import AppState, LibraryItem, decode_item, probe_path
from .tasks import run_task
from .transport import TransportBar
from .write_problem import show_write_problem
from ..i18n import tr

def open_filter() -> str:
    return (tr("Audio and video files") + " (" + " ".join(f"*{e}" for e in sorted(AUDIO_EXTS)) + ");;"
            + tr("All files") + " (*.*)")


EXT_TO_FORMAT = {".mp3": "mp3", ".m4a": "m4a", ".m4b": "m4b", ".aac": "aac", ".ogg": "ogg", ".oga": "ogg",
                 ".opus": "opus", ".wma": "wma", ".flac": "flac", ".wav": "wav", ".aiff": "aiff", ".aif": "aiff",
                 ".mp2": "mp2", ".amr": "amr", ".awb": "amrwb"}
TARGETS = [("Insert at the cursor", "cursor"), ("Replace the selection", "selection"), ("Create a new file", "new")]
EDIT_PREROLL = 1.5          # seconds of the old sound played before a cut or a paste, so the join is heard
SCRUB_INTERVAL = 0.03       # seconds between jumps while dragging on the ruler


def default_settings_for(path: Optional[Path], meta: dict) -> EncodeSettings:
    """Encoder settings that mirror a file's own format, so saving does not change its character."""
    key = EXT_TO_FORMAT.get(path.suffix.lower(), "wav") if path else "wav"
    spec = formats.FORMATS[key]
    s = EncodeSettings(format=key)
    kbps = float(meta.get("bit_rate") or 0) / 1000.0
    if spec.bitrates:
        pool = [b for b in spec.bitrates if b >= kbps * 0.95] or [spec.bitrates[-1]]
        s.bitrate = min(pool) if kbps else spec.default_bitrate
    bits = int(meta.get("bits") or 0)
    if spec.bit_depths:
        s.bit_depth = bits if bits in spec.bit_depths else spec.default_depth
        if key == "wav" and bits == 24:
            s.bit_depth = 24
    return s


class Controller(QObject):
    statusMessage = Signal(str)
    stateChanged = Signal()            # something the actions' enabled states depend on changed
    previewStateChanged = Signal()

    def __init__(self, win: QMainWindow, state: AppState, stage: Stage, transport: TransportBar,
                 edit_panel: EditPanel, library: LibraryPanel) -> None:
        super().__init__(win)
        self.win = win
        self.state = state
        self.stage = stage
        self.transport = transport
        self.edit_panel = edit_panel
        self.library = library
        self.actions: dict[str, QAction] = {}
        self.loop_on = False
        self.last_effect: Optional[tuple[str, dict]] = None
        self._last_params: dict[str, dict] = {}
        self._preview_active = False
        self._scrubbing = False
        self._scrub_at = 0.0
        self._play_origin = 0
        self._confirmed_overwrite: set[int] = set()
        self.noise_store: dict = {}
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(16)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()

    # ================================================================ helpers
    @property
    def doc(self) -> Optional[Document]:
        return self.stage.doc

    @property
    def item(self) -> Optional[LibraryItem]:
        return self.state.current

    @property
    def player(self):
        return self.state.player

    def info(self, text: str) -> None:
        self.statusMessage.emit(tr(text))

    def error(self, title: str, exc: BaseException | str) -> None:
        if isinstance(exc, ffmpeg.OutputError):
            self._show_write_problem(title, access.diagnose_output_error(exc))
            return
        if isinstance(exc, ffmpeg.FFmpegError) and exc.detail:
            exc = exc.detail
        text = tr(str(exc).strip() or repr(exc))
        QMessageBox.warning(self.win, tr(title), text if len(text) < 900 else text[:900] + "…")

    def _show_write_problem(self, title: str, problem: access.WriteProblem) -> None:
        show_write_problem(self.win, title, problem)

    def _range(self) -> tuple[int, int]:
        doc = self.doc
        return self.stage.selection or (0, doc.frames if doc else 0)

    def _need_doc(self, action: str = "") -> Optional[Document]:
        if self.doc is None or self._item_for_doc(self.doc) is None:
            return None
        return self.doc

    def _item_for_doc(self, doc: Document) -> Optional[LibraryItem]:
        return next((i for i in self.state.items if i.doc is doc), None)

    def _refresh(self) -> None:
        self.stateChanged.emit()

    # ================================================================ opening files
    def open_dialog(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(self.win, tr("Open audio"), self.state.settings.last_dir("open"), open_filter())
        if names:
            self.state.settings.remember_dir("open", names[0])
            self.open_paths([Path(n) for n in names])

    def import_video_dialog(self) -> None:
        video = (tr("Video files") + " (*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v *.3gp *.mpg *.mpeg *.ts);;"
                 + tr("All files") + " (*.*)")
        names, _ = QFileDialog.getOpenFileNames(self.win, tr("Import audio from video"), self.state.settings.last_dir("open"), video)
        if names:
            self.state.settings.remember_dir("open", names[0])
            self.open_paths([Path(n) for n in names])

    def open_paths(self, paths: list[Path], *, select: bool = True) -> list[LibraryItem]:
        files: list[Path] = []
        for p in paths:
            if p.is_dir():
                files += sorted(f for f in p.iterdir() if f.suffix.lower() in AUDIO_EXTS)
            elif p.exists():
                files.append(p)
        existing = [self.state.find(p) for p in files]
        todo = [p for p, e in zip(files, existing) if e is None]
        added: list[LibraryItem] = []
        errors: list[str] = []
        if todo:
            probed: dict[Path, LibraryItem] = {}

            def job(progress, cancel):
                for n, path in enumerate(todo):
                    cancel.check()
                    try:
                        probed[path] = probe_path(path)
                    except Exception as exc:  # noqa: BLE001 - reported below
                        errors.append(f"{path.name}: {str(exc).strip().splitlines()[0] if str(exc).strip() else exc}")
                    progress((n + 1) / len(todo))

            ok, _ = run_task(self.win, tr("Opening files"), job, message=tr(f"Reading {len(todo)} file(s)…"), delay_ms=450)
            for path in todo:
                item = probed.get(path)
                if item is None:
                    continue
                if len(item.info.audio) > 1 and not self._choose_stream(item):
                    continue
                self.state.add(item, select=False)
                added.append(item)
                self.state.settings.add_recent_file(path)
        if errors:
            self.error(tr("Could not open some files"), "\n".join(errors[:8]))
        target = (added[0] if added else next((e for e in existing if e is not None), None)) if select else None
        if target is not None:
            self.state.set_current(target, force=True)
        if added:
            self.info(tr(f"Opened {len(added)} file{'s' if len(added) != 1 else ''}"))
        return added

    def _choose_stream(self, item: LibraryItem) -> bool:
        info = item.info
        labels = [tr(f"Stream {i + 1}: {s.codec.upper()}, {s.channels} ch, {s.sample_rate} Hz")
                  + (f"  ({s.language})" if s.language else "") + (f"  {s.title}" if s.title else "")
                  for i, s in enumerate(info.audio)]
        choice, ok = QInputDialog.getItem(self.win, tr("Choose the audio track"), tr(f"{item.name} has several audio tracks:"),
                                          labels, 0, False)
        if not ok:
            return False
        item.stream = info.audio[labels.index(choice)]
        item.duration = item.stream.duration or info.duration
        item.sr = item.stream.sample_rate
        item.channels = 1 if item.stream.channels == 1 else 2
        return True

    # ================================================================ current item <-> stage
    def show_current(self) -> None:
        item = self.state.current
        self.stop_playback()
        if item is None:
            self.stage.set_document(None)
            self.edit_panel.set_document(None)
            self._refresh()
            return
        if self.state.mode == "tags":
            self._refresh()
            return
        if item.doc is None and not self._load(item):
            self.stage.set_document(None)
            self.edit_panel.set_document(None)
            self._refresh()
            return
        if self.stage.doc is not item.doc:
            self.stage.set_document(item.doc, item.view)
            self.edit_panel.set_document(item.doc)
            self.stage.set_mode(item.view.mode)
        self.state.trim_memory()
        self._sync_all()
        self._refresh()

    def _load(self, item: LibraryItem) -> bool:
        if item.path is None:
            return item.doc is not None
        try:
            ok, doc = run_task(self.win, tr("Opening"), lambda progress, cancel: decode_item(item, progress, cancel),
                               message=tr(f"Opening {item.name}…"), delay_ms=250)
        except Exception as exc:  # noqa: BLE001
            item.status, item.message = "error", str(exc)
            self.state.touched(item)
            self.error(tr(f"Could not open {item.name}"), exc)
            return False
        if not ok or doc is None:
            return False
        item.doc = doc
        item.duration = doc.duration
        doc.add_listener(lambda kind, it=item: self._doc_event(it, kind))
        self.state.touched(item)
        return True

    def _doc_event(self, item: LibraryItem, kind: str) -> None:
        item.duration = item.doc.duration if item.doc else item.duration
        self.state.touched(item)
        if item is self.state.current:
            if kind == "format":
                self.stop_playback()
            elif kind == "content":
                self._follow_edit()
            self._sync_all()
            self._refresh()

    def _follow_edit(self) -> None:
        """The audio changed (a cut, a paste, an effect, an undo...). If it is playing, carry on with the new audio
        instead of going silent, so every edit can be heard as it is made.

        An edit that keeps the length (an effect, silence) carries on from the same place. One that cuts something
        out or puts something in restarts a moment before it, so the join is the first thing you hear."""
        player, doc = self.player, self.doc
        old = player.sequence
        if self._preview_active or self._scrubbing or not isinstance(old, PieceSeq) or doc is None:
            return                                      # (what plays may also be the multitrack mix, which has no pieces)
        new = doc.snapshot()
        if new.frames == 0 or new.sr != old.sr:
            self.stop_playback()
            return
        a, old_b, new_b = diff_range(old._pieces(), new._pieces())
        sr = new.sr
        if old_b - a != new_b - a:
            frame = max(0, a - int(EDIT_PREROLL * sr))
            end = None
        else:
            frame = map_frame(player.position, a, old_b, new_b)
            end = None if player.end >= old.frames else map_frame(player.end, a, old_b, new_b)
        loop = player.loop_range
        loop = (map_frame(loop[0], a, old_b, new_b), map_frame(loop[1], a, old_b, new_b)) if loop else None
        if player.swap(new, frame=frame, end=end, loop_range=loop):
            self.stage.set_playhead(player.position)

    def _sync_all(self) -> None:
        self._sync_selection()
        self._sync_view()
        self._sync_info()

    def _sync_selection(self) -> None:
        doc = self.doc
        if doc is None:
            self.transport.set_selection(None)
            self.transport.set_position(0.0)
            return
        sel = self.stage.selection
        sr = doc.sr
        self.transport.set_selection((sel[0] / sr, sel[1] / sr) if sel else None, self.stage.cursor / sr)
        if not self.player.is_playing and self.player.state != "paused":
            self.transport.set_position((sel[0] if sel else self.stage.cursor) / sr)
        self._refresh()

    def _sync_view(self) -> None:
        doc = self.doc
        if doc is None:
            self.transport.set_view(0.0, 0.0)
            return
        a, b = self.stage.view_range
        self.transport.set_view(max(0.0, a) / doc.sr, min(doc.frames, b) / doc.sr)

    def _sync_info(self) -> None:
        item = self.item
        doc = self.doc
        if doc is None or item is None:
            self.transport.set_info("")
            return
        self.transport.set_info(tr(f"{item.format_text()}  ·  {format_time(doc.duration, ms=False, short=True)}  ·  "
                                f"{len(doc.markers)} marker{'s' if len(doc.markers) != 1 else ''}"))

    # ================================================================ selection and cursor
    def on_selection_changed(self) -> None:
        self._sync_selection()

    def on_cursor_changed(self) -> None:
        if self.doc is not None and self.player.state == "stopped":
            sel = self.stage.selection
            self.transport.set_position((sel[0] if sel else self.stage.cursor) / self.doc.sr)
        self._refresh()

    def on_view_changed(self) -> None:
        self._sync_view()

    def selection_edited(self, a: float, b: float) -> None:
        if self.doc is None:
            return
        sr = self.doc.sr
        fa, fb = int(round(a * sr)), int(round(b * sr))
        if fb > fa:
            self.stage.set_selection(fa, fb)
        else:
            self.stage.clear_selection()
            self.stage.set_cursor(fa)

    def select_all(self) -> None:
        self.stage.select_all()

    def select_none(self) -> None:
        self.stage.clear_selection()

    def goto_time(self) -> None:
        if self.doc is None:
            return
        pos = (self.player.position if self.player.state != "stopped" else self.stage.cursor) / self.doc.sr
        value = dialogs.ask_time(self.win, tr("Go to time"), tr("Time (hh:mm:ss.mmm):"), pos)
        if value is not None:
            frame = int(round(value * self.doc.sr))
            self.stage.set_cursor(frame)
            self.stage.clear_selection()
            self.stage.ensure_visible(frame)
            if self.player.state != "stopped":
                self.player.seek(frame)

    # ================================================================ clipboard and edits
    def cut(self) -> None:
        doc, sel = self.doc, self.stage.selection
        if doc is None or not sel:
            return
        self.state.clipboard = doc.cut(*sel)
        self.stage.clear_selection()
        self.stage.set_cursor(sel[0])
        self.info(tr(f"Cut {format_time((sel[1] - sel[0]) / doc.sr, ms=False, short=True)}"))

    def copy(self) -> None:
        doc, sel = self.doc, self.stage.selection
        if doc is None or not sel:
            return
        self.state.clipboard = doc.copy(*sel)
        self.info(tr(f"Copied {format_time((sel[1] - sel[0]) / doc.sr, ms=False, short=True)}"))
        self._refresh()

    def delete(self) -> None:
        doc, sel = self.doc, self.stage.selection
        if doc is None or not sel:
            return
        doc.delete(*sel)
        self.stage.clear_selection()
        self.stage.set_cursor(sel[0])

    def trim(self) -> None:
        doc, sel = self.doc, self.stage.selection
        if doc is None or not sel:
            return
        doc.crop(*sel)
        self.stage.clear_selection()
        self.stage.set_cursor(0)
        self.stage.zoom_fit()

    def silence_selection(self) -> None:
        doc, sel = self.doc, self.stage.selection
        if doc is None or not sel:
            return
        doc.silence(*sel)

    def _matching_clip(self, clip: Clip, doc: Document) -> Optional[Clip]:
        """The clipboard converted to the document's format (cached by the clip itself)."""
        if clip.sr == doc.sr and clip.channels == doc.channels:
            return clip
        ok, src = run_task(self.win, tr("Converting"), lambda progress, cancel: ops.convert_format(
            clip, doc.sr, doc.channels, progress=progress, cancel=cancel),
            message=tr(f"Converting the clipboard to {doc.sr} Hz, {doc.channels} ch…"), delay_ms=300)
        if not ok:
            return None
        converted = Clip.from_source(src)
        return converted

    def paste(self) -> None:
        doc, clip = self.doc, self.state.clipboard
        if doc is None or clip is None:
            return
        try:
            clip = self._matching_clip(clip, doc)
        except Exception as exc:  # noqa: BLE001
            self.error(tr("Paste"), exc)
            return
        if clip is None:
            return
        sel = self.stage.selection
        a, b = sel if sel else (self.stage.cursor, self.stage.cursor)
        doc.replace(a, b, clip._pieces(), tr("Paste"))
        self.stage.set_selection(a, a + clip.frames)
        self.stage.set_cursor(a)
        self.stage.ensure_visible(a)
        self.info(tr(f"Pasted {format_time(clip.duration, ms=False, short=True)}"))

    def paste_as_new(self) -> None:
        clip = self.state.clipboard
        if clip is None:
            return
        doc = Document(clip.sr, clip.channels, clip._pieces(), name=self._unique_name(tr("Pasted audio")))
        self._add_document(doc)

    def mix_paste(self) -> None:
        doc, clip = self.doc, self.state.clipboard
        if doc is None or clip is None:
            return
        dlg = ParamsDialog(tr("Mix paste"), tr("Lays the copied audio on top of the existing audio instead of pushing it aside."),
                           dialogs.MIX_PARAMS, self._last_params.get("_mix"), self.win, ok_text=tr("Mix"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dlg.values()
        self._last_params["_mix"] = vals
        self._mix_clip(clip, vals)

    def mix_file(self) -> None:
        doc = self.doc
        if doc is None:
            return
        name, _ = QFileDialog.getOpenFileName(self.win, tr("Mix an audio file"), self.state.settings.last_dir("open"), open_filter())
        if not name:
            return
        self.state.settings.remember_dir("open", name)
        try:
            ok, src = run_task(self.win, tr("Opening"), lambda progress, cancel: ffmpeg.decode_to_source(
                name, progress=progress, cancel=cancel), message=tr("Reading the file…"), delay_ms=250)
        except Exception as exc:  # noqa: BLE001
            self.error(tr("Mix file"), exc)
            return
        if not ok:
            return
        dlg = ParamsDialog(tr("Mix a file"), tr(f"Mixes {Path(name).name} in at the cursor."), dialogs.MIX_PARAMS,
                           self._last_params.get("_mix"), self.win, ok_text=tr("Mix"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dlg.values()
        self._last_params["_mix"] = vals
        self._mix_clip(Clip.from_source(src), vals)

    def insert_file(self) -> None:
        doc = self.doc
        if doc is None:
            return
        name, _ = QFileDialog.getOpenFileName(self.win, tr("Insert an audio file"), self.state.settings.last_dir("open"), open_filter())
        if not name:
            return
        self.state.settings.remember_dir("open", name)
        try:
            ok, src = run_task(self.win, tr("Opening"), lambda progress, cancel: ffmpeg.decode_to_source(
                name, progress=progress, cancel=cancel), message=tr("Reading the file…"), delay_ms=250)
        except Exception as exc:  # noqa: BLE001
            self.error(tr("Insert file"), exc)
            return
        if ok:
            self._place_source(src, "cursor", tr("Insert file"))

    def _mix_clip(self, clip: Clip, vals: dict) -> None:
        doc = self.doc
        try:
            clip = self._matching_clip(clip, doc)
        except Exception as exc:  # noqa: BLE001
            self.error(tr("Mix"), exc)
            return
        if clip is None:
            return
        sel = self.stage.selection
        pos = sel[0] if sel else self.stage.cursor
        snap = doc.snapshot()

        def job(progress, cancel):
            src = ops.mix_clip(snap, clip, pos, clip_gain_db=vals["clip_db"], base_gain_db=vals["base_db"],
                               progress=progress, cancel=cancel)
            src.peaks()
            return src

        try:
            ok, src = run_task(self.win, tr("Mixing"), job, message=tr("Mixing the audio…"), delay_ms=300)
        except Exception as exc:  # noqa: BLE001
            self.error(tr("Mix"), exc)
            return
        if not ok:
            return
        end = min(doc.frames, pos + src.frames)
        doc.replace_with_source(pos, end, src, tr("Mix paste"))
        self.stage.set_selection(pos, pos + src.frames)
        self.stage.set_cursor(pos)

    # ================================================================ undo / redo / markers
    def undo(self) -> None:
        if self.doc and self.doc.can_undo:
            self.doc.undo()
            self._clamp_after_history()

    def redo(self) -> None:
        if self.doc and self.doc.can_redo:
            self.doc.redo()
            self._clamp_after_history()

    def undo_steps(self, n: int) -> None:
        if self.doc:
            self.doc.undo(n)
            self._clamp_after_history()

    def redo_steps(self, n: int) -> None:
        if self.doc:
            self.doc.redo(n)
            self._clamp_after_history()

    def history_jump(self, index: int) -> None:
        if self.doc:
            self.doc.goto_history(index)
            self._clamp_after_history()

    def _clamp_after_history(self) -> None:
        self.stage._clamp_state()
        self.stage.invalidate()
        self._sync_all()
        self._refresh()

    def add_marker(self) -> None:
        doc = self.doc
        if doc is None:
            return
        frame = self.player.position if self.player.state != "stopped" else self.stage.cursor
        doc.add_marker(frame)
        self.edit_panel.show_tab("Markers")

    def marker_jump(self, frame: int) -> None:
        if self.doc is None:
            return
        self.stage.clear_selection()
        self.stage.set_cursor(frame)
        self.stage.ensure_visible(frame)
        if self.player.state != "stopped":
            self.player.seek(frame)

    def marker_remove(self, index: int) -> None:
        if self.doc:
            self.doc.remove_marker(index)

    def marker_rename(self, index: int, name: str) -> None:
        if self.doc:
            self.doc.rename_marker(index, name)

    def marker_move(self, index: int, frame: int) -> None:
        doc = self.doc
        if doc and 0 <= index < len(doc.markers):
            marks = list(doc.markers)
            marks[index] = Marker(max(0, min(frame, doc.frames)), marks[index].name)
            doc.set_markers(marks, tr("Move marker"))

    def step_marker(self, direction: int) -> None:
        doc = self.doc
        if doc is None or not doc.markers:
            return
        here = self.player.position if self.player.state != "stopped" else self.stage.cursor
        frames = [m.frame for m in doc.markers]
        if direction > 0:
            nxt = next((f for f in frames if f > here + 2), None)
        else:
            nxt = next((f for f in reversed(frames) if f < here - 2), None)
        if nxt is not None:
            self.marker_jump(nxt)

    # ================================================================ effects
    def _effect_context(self, effect: Effect) -> EffectContext:
        doc = self.doc
        a, b = self._range()
        sel = self.stage.selection
        sr = doc.sr
        snap = doc.snapshot()
        preview_s = float(self.state.settings.get("preview_seconds", 8))
        if sel:
            text = (tr(f"Applies to the selection  {format_time(a / sr, ms=False, short=True)} – "
                    f"{format_time(b / sr, ms=False, short=True)}  ({(b - a) / sr:.1f} s)"))
        else:
            text = tr(f"Applies to the whole file  ({format_time(doc.duration, ms=False, short=True)})")

        def render(effect_, vals, bypass, channels, extra, progress, cancel):
            return fx.render_preview(effect_, vals, snap, a, b, seconds=preview_s, bypass=bypass, channels=channels,
                                     progress=progress, cancel=cancel, extra=extra)

        def play_array(arr, rate):
            self.stop_playback()
            clip = Clip.from_source(Source.from_array(arr, rate))
            self._preview_active = True
            self.stage.set_playhead(None)
            self.player.play(clip)

        def stop_preview():
            if self._preview_active:
                self._preview_active = False
                self.player.stop()

        def is_playing():
            return self._preview_active and self.player.state == "playing"

        def capture():
            s = self.stage.selection
            return doc.read(*s) if s else None

        def validate(effect_, vals):
            if effect_.id != "custom_filter":
                return None
            try:
                silent = Clip.from_source(Source.silence(int(0.2 * sr), sr, doc.channels))
                fx.run_effect(effect_, vals, silent, 0, silent.frames)
            except ffmpeg.FFmpegError as exc:
                return tr("FFmpeg did not accept that filter chain:\n\n") + (exc.detail or str(exc)).strip()[-500:]
            return None

        return EffectContext(range_text=text, channels=doc.channels, frames=b - a, sr=sr, render_preview=render,
                             play_array=play_array, stop_preview=stop_preview, is_preview_playing=is_playing,
                             capture_noise=capture, favorites=self.state.favorites, noise_store=self.noise_store,
                             validate=validate)

    def open_effect(self, effect_id: str, params: Optional[dict] = None) -> None:
        doc = self.doc
        if doc is None or doc.frames == 0:
            self.info(tr("Open or create some audio first"))
            return
        effect = fx.get(effect_id)
        if effect.id == "envelope":
            self.set_view_mode("envelope")
            return
        if effect.min_channels > doc.channels:
            self.error(tr(effect.name), tr(f"{tr(effect.name)} needs a stereo file."))
            return
        ctx = self._effect_context(effect)
        dlg = EffectDialog(effect, params if params is not None else self._last_params.get(effect_id), ctx, self.win)
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        ctx.stop_preview()
        if not accepted:
            return
        vals = dlg.values()
        self._last_params[effect_id] = vals
        self.apply_effect(effect, vals, dlg.selected_channels(), dlg.extra())

    def apply_effect(self, effect: Effect, params: dict, channels: Optional[list[int]] = None,
                     extra: Optional[dict] = None) -> None:
        doc = self.doc
        if doc is None:
            return
        a, b = self._range()
        had_selection = self.stage.selection is not None
        snap = doc.snapshot()

        def job(progress, cancel):
            clip = fx.run_effect(effect, params, snap, a, b, channels=channels, progress=progress, cancel=cancel,
                                 extra=extra)
            for piece in clip.pieces:
                piece.src.peaks()
            return clip

        try:
            ok, clip = run_task(self.win, tr(effect.name), job, message=tr(f"Applying {tr(effect.name)}…"), delay_ms=250)
        except Exception as exc:  # noqa: BLE001
            self.error(tr(effect.name), exc.detail if isinstance(exc, ffmpeg.FFmpegError) and exc.detail else exc)
            return
        if not ok:
            self.info(tr("Cancelled"))
            return
        doc.replace(a, b, clip.pieces, tr(effect.name))
        if had_selection:
            self.stage.set_selection(a, a + clip.frames)
        self.last_effect = (effect.id, params)
        self.state.settings.used_effect(effect.id)
        self.info(tr(f"Applied {tr(effect.name)}"))
        self._refresh()

    def repeat_effect(self) -> None:
        if self.last_effect:
            self.apply_effect(fx.get(self.last_effect[0]), self.last_effect[1])

    def apply_favorite(self, name: str, apply_now: bool) -> None:
        fav = self.state.favorites.get(name)
        if fav is None:
            return
        try:
            effect = fx.get(fav["effect"])
        except KeyError:
            self.error(tr("Favorites"), tr(f"The effect “{fav['effect']}” no longer exists."))
            return
        if self.doc is None:
            self.info(tr("Open or create some audio first"))
            return
        if apply_now:
            self.apply_effect(effect, effect.clean(fav["params"]))
        else:
            self.open_effect(effect.id, fav["params"])

    def apply_envelope(self) -> None:
        doc, st = self.doc, self.stage.state
        if doc is None or st.env_range is None:
            return
        a, b = st.env_range
        pts = [list(p) for p in st.env_points]
        env = fx.get("envelope")
        snap = doc.snapshot()

        def job(progress, cancel):
            clip = fx.run_effect(env, {"points": pts}, snap, a, b, progress=progress, cancel=cancel)
            for piece in clip.pieces:
                piece.src.peaks()
            return clip

        ok, clip = run_task(self.win, tr("Volume envelope"), job, message=tr("Applying the envelope…"), delay_ms=250)
        if not ok:
            return
        doc.replace(a, b, clip.pieces, tr("Volume envelope"))
        self.stage.reset_envelope()
        self.info(tr("Applied the volume envelope"))

    # ================================================================ views and zoom
    def set_view_mode(self, mode: str) -> None:
        if self.doc is None:
            return
        self.stage.set_mode(mode)
        if self.item:
            self.item.view.mode = mode
        self.stateChanged.emit()

    # ================================================================ generators
    def _unique_name(self, base: str) -> str:
        names = {i.name for i in self.state.items}
        if base not in names:
            return base
        n = 2
        while f"{base} {n}" in names:
            n += 1
        return f"{base} {n}"

    def _add_document(self, doc: Document) -> LibraryItem:
        item = LibraryItem.from_document(doc, doc.name)
        doc.add_listener(lambda kind, it=item: self._doc_event(it, kind))
        self.state.add(item, select=True)
        return item

    def _place_source(self, src: Source, target: str, label: str, new_name: str = "Untitled") -> None:
        doc = self.doc
        if target == "new" or doc is None:
            self._add_document(Document.from_source(src, name=self._unique_name(tr(new_name))))
            return
        if (src.sr, src.channels) != (doc.sr, doc.channels):
            ok, conv = run_task(self.win, tr("Converting"), lambda progress, cancel: ops.convert_format(
                Clip.from_source(src), doc.sr, doc.channels, progress=progress, cancel=cancel), delay_ms=300)
            if not ok:
                return
            src = conv
        sel = self.stage.selection if target == "selection" else None
        a, b = sel if sel else (self.stage.cursor, self.stage.cursor)
        doc.replace_with_source(a, b, src, tr(label))
        self.stage.set_selection(a, a + src.frames)
        self.stage.set_cursor(a)
        self.stage.ensure_visible(a)

    def _target_list(self) -> list[tuple[str, str]]:
        if self.doc is None:
            return [TARGETS[2]]
        return TARGETS if self.stage.selection else [TARGETS[0], TARGETS[2]]

    def _format_for_generation(self) -> tuple[int, int]:
        doc = self.doc
        return (doc.sr, doc.channels) if doc else (44100, 2)

    def generate(self, kind: str) -> None:
        sr, ch = self._format_for_generation()
        titles = {"tone": (tr("Tone"), dialogs.TONE_PARAMS, tr("Generates a steady tone, a sweep or a test signal.")),
                  "silence": (tr("Silence"), dialogs.SILENCE_PARAMS, tr("Inserts a stretch of digital silence.")),
                  "noise": (tr("Noise"), dialogs.NOISE_PARAMS, tr("Generates white, pink, brown, blue or violet noise.")),
                  "dtmf": (tr("Dial tones"), dialogs.DTMF_PARAMS, tr("Telephone keypad (DTMF) tones."))}
        if kind == "speech":
            self._generate_speech(sr, ch)
            return
        title, params, desc = titles[kind]
        dlg = ParamsDialog(title, desc, params, self._last_params.get(f"_gen_{kind}"), self.win,
                           targets=self._target_list(), ok_text=tr("Generate"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        self._last_params[f"_gen_{kind}"] = v

        def job(progress, cancel):
            if kind == "tone":
                spec = ToneSpec(v["waveform"], v["freq"], v["freq_end"] if v["sweep"] else None, v["sweep_type"],
                                v["amp_db"], v["seconds"], v["fade_ms"])
                return generate.generate_tone(spec, sr, ch, progress=progress, cancel=cancel)
            if kind == "silence":
                return generate.generate_silence(v["seconds"], sr, ch)
            if kind == "noise":
                return generate.generate_noise(v["color"], v["amp_db"], v["seconds"], sr, ch, progress=progress, cancel=cancel)
            return generate.generate_dtmf(v["digits"], v["tone_ms"], v["gap_ms"], v["amp_db"], sr, ch)

        try:
            ok, src = run_task(self.win, title, job, message=tr(f"Generating {title.lower()}…"), delay_ms=300)
        except Exception as exc:  # noqa: BLE001
            self.error(title, exc)
            return
        if ok:
            name = {"tone": tr(f"Tone {v.get('freq', 0):g} Hz"), "silence": tr("Silence"), "noise": tr(f"{v.get('color', '').title()} noise"),
                    "dtmf": tr("Dial tones")}[kind]
            self._place_source(src, dlg.target, title, name)

    def _generate_speech(self, sr: int, ch: int) -> None:
        if not generate.speech_available():
            self.error(tr("Speech"), tr("Speech generation needs the voices that come with your computer."))
            return
        dlg = SpeechDialog(self.win)
        if self.doc is None:
            dlg.target.clear()
            dlg.target.addItem(tr("Create a new file"), "new")
        elif not self.stage.selection:
            dlg.target.removeItem(1)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.result_values()

        def job(progress, cancel):
            return generate.synthesize_speech(v["text"], v["voice"], v["rate"], v["volume"], cancel=cancel)

        try:
            ok, src = run_task(self.win, tr("Speech"), job, message=tr("Speaking…"), delay_ms=200)
        except Exception as exc:  # noqa: BLE001
            self.error(tr("Speech"), exc)
            return
        if ok:
            self._place_source(src, v["target"], tr("Speech"), tr("Speech"))

    def record(self) -> None:
        self.stop_playback()
        doc = self.doc
        dlg = RecordDialog(self.win, can_insert=doc is not None, has_selection=bool(self.stage.selection),
                           default_sr=doc.sr if doc else 44100, device=self.player.device)
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.result_source is None:
            return
        self._place_source(dlg.result_source, dlg.where, tr("Record"), tr("Recording"))
        self.info(tr("Recording added"))

    def new_document(self) -> None:
        params = [dialogs.Param("sr", "Sample rate", "choice", 44100, choices=dialogs.CONVERT_PARAMS[0].choices),
                  dialogs.Param("channels", "Channels", "choice", 2, choices=dialogs.CONVERT_PARAMS[1].choices),
                  dialogs.Param("seconds", "Starting length", "float", 0.0, 0.0, 3600.0, 0.5, 1, "s",
                                help="Leave at 0 for an empty file you can record or generate into.")]
        dlg = ParamsDialog(tr("New file"), tr("Creates an empty audio file to record into, generate sounds in or paste to."),
                           params, None, self.win, ok_text=tr("Create"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        frames = int(v["seconds"] * v["sr"])
        src = Source.silence(frames, v["sr"], v["channels"]) if frames else Source.from_array(
            np.zeros((0, v["channels"]), np.float32), v["sr"])
        self._add_document(Document.from_source(src, name=self._unique_name(tr("Untitled"))))

    # ================================================================ tools
    def analyze(self) -> None:
        doc = self.doc
        if doc is None or doc.frames == 0:
            return
        a, b = self._range()
        snap = doc.snapshot()
        try:
            ok, stats = run_task(self.win, tr("Analyzing"), lambda progress, cancel: analysis.measure(
                snap, a, b, progress=progress, cancel=cancel), message=tr("Measuring levels and loudness…"), delay_ms=300)
        except Exception as exc:  # noqa: BLE001
            self.error(tr("Analyze"), exc)
            return
        if ok:
            what = tr("the selection") if self.stage.selection else tr("the whole file")
            AnalyzeDialog(stats, f"{doc.name} · {what}", self.win).exec()

    def spectrum(self) -> None:
        doc = self.doc
        if doc is None or doc.frames == 0:
            return
        a, b = self._range()
        what = tr("the selection") if self.stage.selection else tr("the whole file")
        SpectrumDialog(doc.snapshot(), a, b, tr(f"{doc.name} · average spectrum of {what}"), self.win).exec()

    def silence_tools(self) -> None:
        doc = self.doc
        if doc is None or doc.frames == 0:
            return
        dlg = ParamsDialog(tr("Find silence"), tr("Detects quiet stretches. Useful for splitting audiobooks, tightening speech "
                           "or trimming the start and end of a recording."), dialogs.SILENCE_TOOL_PARAMS,
                           self._last_params.get("_silence"), self.win, ok_text=tr("Run"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        self._last_params["_silence"] = v
        a, b = self._range()
        snap = doc.snapshot()
        hop = 0.02
        try:
            ok, levels = run_task(self.win, tr("Finding silence"), lambda progress, cancel: analysis.frame_levels(
                snap, a, b, hop, progress=progress, cancel=cancel), delay_ms=300)
        except Exception as exc:  # noqa: BLE001
            self.error(tr("Find silence"), exc)
            return
        if not ok:
            return
        runs = analysis.find_silences(levels, hop, v["threshold"], v["min_seconds"])
        sr = doc.sr
        if not runs:
            self.info(tr("No silences found with those settings"))
            return
        if v["action"] == "markers":
            marks = list(doc.markers) + [Marker(a + int(((s + e) / 2) * sr), tr(f"Pause {i}")) for i, (s, e) in enumerate(runs, 1)]
            doc.set_markers(marks, tr("Mark silences"))
            self.edit_panel.show_tab("Markers")
            self.info(tr(f"Added {len(runs)} markers"))
        elif v["action"] == "shorten":
            keep = v["keep"]
            ranges = [(a + int((s + keep / 2) * sr), a + int((e - keep / 2) * sr)) for s, e in runs if e - s > keep + 0.02]
            doc.delete_ranges(ranges, tr("Remove silences"))
            self.stage.clear_selection()
            self.info(tr(f"Shortened {len(ranges)} silences"))
        else:
            keep = v["keep"]
            total = (b - a) / sr
            ranges = []
            if runs[0][0] <= hop * 2 and runs[0][1] - keep > 0:
                ranges.append((a, a + int((runs[0][1] - keep) * sr)))
            if runs[-1][1] >= total - hop * 2 and runs[-1][0] + keep < total:
                ranges.append((a + int((runs[-1][0] + keep) * sr), b))
            if not ranges:
                self.info(tr("There is no silence at the start or end"))
                return
            doc.delete_ranges(ranges, tr("Trim silence"))
            self.stage.clear_selection()
            self.info(tr("Trimmed the silence at the ends"))

    def convert_document_format(self) -> None:
        doc = self.doc
        if doc is None or doc.frames == 0:
            return
        values = {"sr": doc.sr, "channels": doc.channels}
        dlg = ParamsDialog(tr("Convert file format"), tr("Changes the sample rate or channel layout of this file. "
                           "Pitch and speed stay the same."), dialogs.CONVERT_PARAMS, values, self.win, ok_text=tr("Convert"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        if (v["sr"], v["channels"]) == (doc.sr, doc.channels):
            return
        snap = doc.snapshot()

        def job(progress, cancel):
            src = ops.convert_format(snap, v["sr"], v["channels"], progress=progress, cancel=cancel)
            src.peaks()
            return src

        try:
            ok, src = run_task(self.win, tr("Converting"), job, message=tr(f"Converting to {v['sr']} Hz…"), delay_ms=250)
        except Exception as exc:  # noqa: BLE001
            self.error(tr("Convert"), exc)
            return
        if ok:
            doc.replace_all(src, tr("Convert format"))
            self.stage.clear_selection()
            self.stage.zoom_fit()

    # ================================================================ save / export / close
    def _export_settings(self, item: LibraryItem) -> EncodeSettings:
        if item.last_export:
            return EncodeSettings.from_dict(item.last_export)
        return default_settings_for(item.path, item.doc.meta if item.doc else {})

    def _write_document(self, item: LibraryItem, path: Path, settings: EncodeSettings,
                        frame_range: Optional[tuple[int, int]]) -> bool:
        doc = item.doc
        snap = doc.snapshot()
        cv = ConvertItem(path=None, name=item.name, duration=doc.duration, sr=doc.sr, channels=doc.channels,
                         tags=item.tags.copy(), doc=snap, frame_range=frame_range)
        opts = ConvertOptions(settings=settings, keep_tags=True)
        warnings: list[str] = []

        def job(progress, cancel):
            return convert_item(cv, path, opts, progress=progress, cancel=cancel, on_warning=warnings.append)

        try:
            ok, _ = run_task(self.win, tr("Saving"), job, message=tr(f"Writing {path.name}…"), delay_ms=250)
        except Exception as exc:  # noqa: BLE001
            self.error(tr("Save"), exc)
            return False
        self._save_warning = warnings[0] if ok and warnings else ""
        return bool(ok)

    _save_warning = ""

    def _saved_text(self, name: str) -> str:
        """What to say after a save: if the audio was saved but its tags were not, say so too."""
        warning, self._save_warning = self._save_warning, ""
        return tr(f"Saved {name}, but its tags were not written: {warning}") if warning else tr(f"Saved {name}")

    def save(self, item: Optional[LibraryItem] = None) -> bool:
        item = item or self.item
        if item is None or item.doc is None:
            return False
        if item.path is None:
            return self.save_as(item)
        if not item.doc.modified:
            self.info(tr("No changes to save"))
            return True
        if item.id not in self._confirmed_overwrite:
            box = QMessageBox(self.win)
            box.setWindowTitle(tr("Save changes"))
            box.setIcon(QMessageBox.Icon.Question)
            box.setText(tr(f"Replace the original file {item.path.name} with your edited version?"))
            suffix = item.path.suffix.lower()
            if suffix in (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma"):
                box.setInformativeText(tr("Saving re-encodes lossy audio, which costs a little quality each time. "
                                       "“Save as…” keeps the original untouched."))
            replace = box.addButton(tr("Replace file"), QMessageBox.ButtonRole.AcceptRole)
            save_as = box.addButton(tr("Save as…"), QMessageBox.ButtonRole.ActionRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked is save_as:
                return self.save_as(item)
            if clicked is not replace:
                return False
            self._confirmed_overwrite.add(item.id)
        settings = self._export_settings(item)
        if not self._write_document(item, item.path, settings, None):
            return False
        item.doc.mark_saved()
        item.tags_orig = item.tags.copy()
        self._after_save(item)
        self.info(self._saved_text(item.path.name))
        return True

    def save_as(self, item: Optional[LibraryItem] = None) -> bool:
        item = item or self.item
        if item is None or item.doc is None:
            return False
        doc = item.doc
        settings = self._export_settings(item)
        folder = item.path.parent if item.path else Path(self.state.settings.last_dir("export"))
        default = folder / (tagmod.sanitize_filename(item.name) + settings.spec.ext)
        if item.path is not None and default.exists() and default.resolve() == item.path.resolve():
            default = folder / (tagmod.sanitize_filename(item.name) + tr(" (edited)") + settings.spec.ext)   # never offer the original
        sel = self.stage.selection if item is self.item else None
        dlg = ExportDialog(self.win, default, settings, doc.sr, doc.channels, has_selection=bool(sel))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        path, settings, only_sel = dlg.result_values()
        if path.exists():
            original = item.path is not None and path.resolve() == item.path.resolve()
            text = (tr(f"{path.name} is the original file. Replace it with the edited version?") if original
                    else tr(f"{path.name} already exists. Replace it?"))
            if QMessageBox.question(self.win, tr("Save as"), text) != QMessageBox.StandardButton.Yes:
                return False
        rng = sel if (only_sel and sel) else None
        if not self._write_document(item, path, settings, rng):
            return False
        self.state.settings.remember_dir("export", path)
        self.state.settings.add_recent_file(path)
        item.last_export = settings.to_dict()
        if rng is None:
            item.path = path
            item.name = path.stem
            doc.path, doc.name = path, path.stem
            doc.mark_saved()
            item.tags_orig = item.tags.copy()
            self._confirmed_overwrite.add(item.id)
            self._after_save(item)
        self.info(self._saved_text(path.name))
        return True

    def _after_save(self, item: LibraryItem) -> None:
        try:
            item.info = ffmpeg.probe(item.path)
            item.stream = item.info.first_audio
        except ffmpeg.FFmpegError:
            pass
        self.state.touched(item)
        self._sync_all()
        self._refresh()

    def close_item(self, item: Optional[LibraryItem] = None) -> bool:
        item = item or self.item
        if item is None:
            return True
        if item.modified:
            box = QMessageBox(self.win)
            box.setWindowTitle(tr("Unsaved changes"))
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText(tr(f"Save changes to {item.name}?"))
            save = box.addButton(tr("Save"), QMessageBox.ButtonRole.AcceptRole)
            discard = box.addButton(tr("Discard"), QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked is save:
                if not self.save(item):
                    return False
            elif clicked is not discard:
                return False
        if item is self.item:
            self.stop_playback()
        if item.doc is not None and self.stage.doc is item.doc:
            self.stage.set_document(None)
            self.edit_panel.set_document(None)
        self.state.remove(item)
        return True

    def remove_items(self, items: list[LibraryItem]) -> None:
        for item in list(items):
            if not self.close_item(item):
                break

    def confirm_quit(self) -> bool:
        for item in list(self.state.items):
            if item.modified:
                self.state.set_current(item)
                if not self.close_item(item):
                    return False
        return True

    # ================================================================ playback
    @property
    def _active(self) -> bool:
        return self.player.state != "stopped" and not self._preview_active

    def toggle_play(self) -> None:
        if self.doc is None:
            return
        if self._preview_active:
            self._preview_active = False
            self.player.stop()
        if self.player.state in ("playing", "paused"):
            self.player.toggle_pause()
            self._refresh_play_state()
        else:
            self.start_playback()

    def start_playback(self, from_frame: Optional[int] = None) -> None:
        doc = self.doc
        if doc is None or doc.frames == 0:
            return
        sel = self.stage.selection
        if sel:
            start, end, loop_range = (from_frame if from_frame is not None and sel[0] <= from_frame < sel[1] else sel[0]), sel[1], sel
        else:
            start = self.stage.cursor if from_frame is None else from_frame
            if start >= doc.frames - 1:
                start = 0
            end, loop_range = doc.frames, None
        self._play_origin = sel[0] if sel else self.stage.cursor
        self.player.volume = float(self.state.settings.get("volume", 0.9))
        try:
            self.player.play(doc.snapshot(), start, end, loop=self.loop_on, loop_range=loop_range)
        except AudioDeviceError as exc:
            self.error(tr("Playback"), exc)
            return
        self.stage.set_playhead(start)
        self._refresh_play_state()

    def stop_playback(self) -> None:
        self._scrubbing = False
        if self._preview_active:
            self._preview_active = False
            self.player.stop()
            self._refresh_play_state()
            return
        if self.player.state != "stopped":
            self.player.stop()
        self.stage.set_playhead(None)
        self.transport.meter.reset()
        if self.doc is not None:
            self._sync_selection()
        self._refresh_play_state()

    def seek_requested(self, frame: int) -> None:
        if self.player.state != "stopped" and not self._preview_active:
            self.player.seek(frame)

    def scrub(self, frame: int) -> None:
        """Press and drag on the ruler: hear the audio under the pointer, to find a cut by ear. Does nothing while the
        file is already playing (a click on the ruler then just moves the playhead, as it always did)."""
        doc = self.doc
        if doc is None or doc.frames == 0:
            return
        frame = max(0, min(frame, doc.frames - 1))
        if self._scrubbing and self.player.state == "stopped":
            self._scrubbing = False                          # it ran off the end: the next press starts it again
        if self._scrubbing:
            now = time.monotonic()
            if now - self._scrub_at >= SCRUB_INTERVAL:
                self._scrub_at = now
                self.player.seek(frame)
            return
        if self.player.state != "stopped" or self._preview_active:
            return
        self.player.volume = float(self.state.settings.get("volume", 0.9))
        try:
            self.player.play(doc.snapshot(), frame, doc.frames)
        except AudioDeviceError:
            return                                           # scrubbing is a convenience: say nothing if there is no sound card
        self._scrubbing = True
        self._scrub_at = time.monotonic()
        self.stage.set_playhead(frame)
        self._refresh_play_state()

    def end_scrub(self) -> None:
        if self._scrubbing:
            self.stop_playback()

    def set_loop(self, on: bool) -> None:
        self.loop_on = on
        if self.player.state == "playing" and not self._preview_active and self.doc is not None:
            self.start_playback(self.player.position)

    def set_volume(self, v: float) -> None:
        self.player.volume = v
        self.state.settings.set("volume", v)

    def jump(self, seconds: float) -> None:
        doc = self.doc
        if doc is None:
            return
        here = self.player.position if self.player.state != "stopped" else self.stage.cursor
        frame = max(0, min(doc.frames, here + int(seconds * doc.sr)))
        if self.player.state != "stopped":
            self.player.seek(frame)
        else:
            self.stage.clear_selection()
            self.stage.set_cursor(frame)
        self.stage.ensure_visible(frame)

    def go_start(self) -> None:
        if self.doc is None:
            return
        self.stage.clear_selection()
        self.stage.set_cursor(0)
        self.stage.ensure_visible(0)
        if self.player.state != "stopped" and not self._preview_active:
            self.player.seek(0)

    def go_end(self) -> None:
        if self.doc is None:
            return
        self.stage.clear_selection()
        self.stage.set_cursor(self.doc.frames)
        self.stage.ensure_visible(self.doc.frames)
        if self.player.state != "stopped" and not self._preview_active:
            self.stop_playback()

    def _refresh_play_state(self) -> None:
        self.previewStateChanged.emit()
        self._refresh()

    def _tick(self) -> None:
        p = self.player
        if self._preview_active:
            self.transport.feed_meter(*p.consume_peak())
            if p.consume_finished() or p.state == "stopped":
                self._preview_active = False
                p.stop()
                self.transport.meter.reset()
                self._refresh_play_state()
            return
        doc = self.doc
        if p.state == "stopped":
            if p.consume_finished():
                p.stop()
                self.stage.set_playhead(None)
                self.transport.meter.reset()
                self._sync_selection()
                self._refresh_play_state()
            return
        if doc is not None:
            pos = p.position
            self.stage.set_playhead(pos)
            self.transport.set_position(pos / doc.sr)
        self.transport.feed_meter(*p.consume_peak())
        if p.consume_finished():
            p.stop()
            self.stage.set_playhead(None)
            self._sync_selection()
            self._refresh_play_state()

    def step_item(self, delta: int) -> None:
        items = self.state.items
        if len(items) > 1 and self.state.current in items:
            self.state.set_current(items[(items.index(self.state.current) + delta) % len(items)])

    # ================================================================ misc
    def show_in_folder(self, item: LibraryItem) -> None:
        if item.path and item.path.exists():
            from .. import paths
            paths.reveal_in_file_manager(item.path)

    def open_data_folder(self) -> None:
        from .. import paths
        paths.open_folder(paths.config_dir())
