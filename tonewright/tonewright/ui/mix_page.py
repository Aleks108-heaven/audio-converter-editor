"""The Multitrack page: toolbar, timeline, transport, the inspector, and the controller behind them."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
from PySide6.QtCore import QObject, QPoint, QTimer, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QCheckBox, QDialog, QDoubleSpinBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QMenu, QMessageBox, QStackedWidget, QTabWidget, QToolButton, QVBoxLayout, QWidget)

from ..audio.devices import AudioDeviceError
from ..audio.playback import Player
from ..core import effects as fx
from ..core import ffmpeg, ops
from ..core.document import Clip, Document, PieceSeq
from ..core.mixer import CHANNELS, DEFAULT_SR, LiveMix, MixClip, MixProject, custom_filters_in
from ..core.source import Source, SourceWriter
from ..core.timefmt import format_time
from ..i18n import tr
from . import icons, theme
from .dialogs import RecordDialog
from .edit_panel import EffectsTab
from .effect_dialog import EffectContext, EffectDialog
from .mix_view import Hit, MixBoard
from .state import AppState, LibraryItem
from .tasks import run_task
from .widgets import TimeEdit, divider, icon_button, make_button, role_label, section_label

PROJECT_FILTER = "Tonewright mix (*.twmix)"
LIVE_INTERVAL_MS = 40         # at most one swap of the playing mix per this many milliseconds while it is being edited
SCRUB_INTERVAL = 0.03         # seconds between jumps while dragging on the ruler
EFFECT_LEAD_IN = 1.5          # seconds ahead of the start that an effect must be rendered for, before playing begins
EFFECT_WAIT = 8.0             # the longest to wait for that (then it starts anyway and the effect comes in when ready)


# ====================================================================== the controller
class MixController(QObject):
    refreshed = Signal()              # the project or the selection changed: panels should refresh
    playStateChanged = Signal()
    positionChanged = Signal(int)
    message = Signal(str)

    def __init__(self, win, state: AppState, main_ctl, board: MixBoard) -> None:
        super().__init__(win)
        self.win, self.state, self.main_ctl, self.board = win, state, main_ctl, board
        self.project = MixProject()
        self.player = Player(stream_factory=state.player._factory)
        self.noise_store: dict = {}
        self._live: Optional[LiveMix] = None              # what the player is playing; replaced after every change
        self._effect_cache: dict = {}                     # rendered effect spans, shared by successive LiveMix objects
        self._fx_pending = False
        self._scrubbing = False
        self._scrub_at = 0.0
        self._file_cache: dict = {}
        self._preview_active = False
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._live_timer = QTimer(self)                   # changes made while playing are swapped in at most this often
        self._live_timer.setSingleShot(True)
        self._live_timer.setInterval(LIVE_INTERVAL_MS)
        self._live_timer.timeout.connect(self._apply_live)
        self._dirty = False
        board.bind(self.project)
        board.editStarting.connect(self.begin_edit)
        board.changed.connect(self._changed)
        board.addLineRequested.connect(self.add_line)
        board.addEffectRequested.connect(lambda: self.choose_effect_menu())
        board.lineRecordRequested.connect(self.record_to_line)
        board.ruler.scrubRequested.connect(self.scrub)
        board.ruler.scrubEnded.connect(self.end_scrub)
        c = board.canvas
        c.selectionChanged.connect(self.refreshed)
        c.playheadMoved.connect(self._playhead_moved)
        c.filesDropped.connect(lambda files, line, frame: self.add_files(files, line, frame))
        c.itemActivated.connect(self._activated)
        c.deleteRequested.connect(self.delete_selected)
        c.splitRequested.connect(self.split_at_playhead)
        c.duplicateRequested.connect(self.duplicate_selected)

    # ---- helpers
    @property
    def canvas(self):
        return self.board.canvas

    @property
    def playing(self) -> bool:
        return self.player.state in ("playing", "paused") and not self._preview_active

    @property
    def sr(self) -> int:
        return self.project.sr

    def info(self, text: str) -> None:
        self.message.emit(tr(text))

    def error(self, title: str, exc) -> None:
        self.main_ctl.error(title, exc)

    def _changed(self) -> None:
        self._dirty = True
        self._live_changed()
        self.refreshed.emit()

    def begin_edit(self, label: str) -> None:
        """Called before every change, which becomes one undo step. Playback goes on: the change is swapped into it
        (see `_live_changed`), so moving a clip, trimming, changing a level or adding an effect is heard as it happens."""
        self.project.checkpoint(label)

    def _refresh_all(self) -> None:
        sel = self.canvas.selected
        if sel is not None:
            alive = (any(c.id == sel[1] for c in self.project.clips) if sel[0] == "clip"
                     else any(e.id == sel[1] for e in self.project.effects))
            if not alive:
                self.canvas.select(None)
        self.board.refresh()
        self._dirty = True
        self._live_changed()
        self.refreshed.emit()

    def _playhead_moved(self, frame: int) -> None:
        if self.playing and not self._scrubbing:                 # while scrubbing, `scrub` does the (throttled) seeking
            self.player.seek(frame)
        self.positionChanged.emit(frame)

    def _activated(self, kind: str, ident: int) -> None:
        if kind == "add-line":
            self.add_line()
        elif kind == "add-effect":
            self.choose_effect_menu()
        elif kind == "effect":
            self.edit_effect(ident)
        else:
            self.refreshed.emit()

    # ---- history
    def undo(self) -> None:
        label = self.project.undo()
        if label:
            self._refresh_all()
            self.info(tr(f"Undid {tr(label)}"))

    def redo(self) -> None:
        label = self.project.redo()
        if label:
            self._refresh_all()
            self.info(tr(f"Redid {tr(label)}"))

    # ---- getting audio in
    def _load_file(self, path: Path, progress=None, cancel=None) -> PieceSeq:
        """The file's audio at the project's format, cached (so adding it twice, or reopening a project, is instant)."""
        stamp = (str(path), path.stat().st_mtime_ns, self.sr)
        hit = self._file_cache.get(stamp)
        if hit is not None:
            return hit
        src = ffmpeg.decode_to_source(path, progress=progress, cancel=cancel)
        src.peaks()
        seq = self._to_project_format(Clip.from_source(src), cancel)
        self._file_cache[stamp] = seq
        return seq

    def _to_project_format(self, seq: PieceSeq, cancel=None) -> PieceSeq:
        if seq.sr == self.sr and seq.channels == CHANNELS:
            return seq
        out = ops.convert_format(seq, self.sr, CHANNELS, cancel=cancel)
        out.peaks()
        return Clip.from_source(out)

    def _resolve(self, item) -> tuple[str, Optional[str], PieceSeq]:
        """(name, file path or None, audio) for a library item or a path."""
        if isinstance(item, LibraryItem):
            if item.doc is not None:
                seq = item.doc.snapshot()
                path = str(item.path) if item.path and not item.doc.modified else None
                return item.name, path, self._to_project_format(seq)
            return item.name, str(item.path), self._load_file(Path(item.path))
        path = Path(item)
        return path.stem, str(path), self._load_file(path)

    def add_files(self, files: Sequence, line_id: Optional[int] = None, frame: Optional[int] = None) -> None:
        """Put files (paths or library items) on the mix, each on a free line, starting at `frame`."""
        files = list(files)
        if not files:
            return
        start = self.canvas.playhead if frame is None else frame
        if self.project.empty:
            first = next(iter(files))
            rate = first.sr if isinstance(first, LibraryItem) else None
            if rate and rate != self.project.sr and not self.project.clips:
                self.project.sr = rate if rate in (22050, 32000, 44100, 48000, 88200, 96000) else DEFAULT_SR

        def job(progress, cancel):
            out = []
            for n, item in enumerate(files):
                out.append(self._resolve(item))
                if progress:
                    progress((n + 1) / len(files))
            return out

        try:
            ok, resolved = run_task(self.win, tr("Adding to the mix"), job,
                                    message=tr(f"Reading {len(files)} file(s)…"), delay_ms=300)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.error(tr("Add files"), exc)
            return
        if not ok:
            return
        self.begin_edit("Add clips")
        last = None
        for name, path, seq in resolved:
            try:
                last = self.place(seq, name, path, line_id, start)
            except ValueError as exc:
                self.error(tr("Add files"), exc)
            line_id = None                                  # the next file goes on the next free line
        if last is not None:
            self.canvas.select("clip", last.id)
        self._refresh_all()
        self.info(tr(f"Added {len(resolved)} clip(s)"))

    def place(self, seq: PieceSeq, name: str, path: Optional[str], line_id: Optional[int], start: int) -> MixClip:
        p = self.project
        if line_id is None:
            line_id = self._free_line(start, seq.frames)
        elif line_id not in {l.id for l in p.lines}:
            line_id = p.add_line().id
        return p.add_clip(line_id, seq, start, name, path)

    def _free_line(self, start: int, length: int) -> int:
        p = self.project
        for line in p.lines:
            if not any(c.start < start + length and c.end > start for c in p.clips_on(line.id)):
                return line.id
        return p.add_line().id

    def add_files_dialog(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(self.win, tr("Add audio to the mix"), self.state.settings.last_dir("open"),
                                                self._open_filter())
        if names:
            self.state.settings.remember_dir("open", names[0])
            self.add_files([Path(n) for n in names])

    @staticmethod
    def _open_filter() -> str:
        from .controller import open_filter
        return open_filter()

    def add_library_selection(self) -> None:
        items = self.win.library.selected_items() or ([self.state.current] if self.state.current else [])
        if not items:
            self.info(tr("Select files in the library first"))
            return
        self.add_files(items)

    def record_to_line(self, line_id: int) -> None:
        self.stop()
        dlg = RecordDialog(self.win, default_sr=self.sr, device=self.state.player.device)
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.result_source is None:
            return
        try:
            seq = self._to_project_format(Clip.from_source(dlg.result_source))
            self.begin_edit("Record")
            clip = self.place(seq, tr("Recording"), None, line_id, self.canvas.playhead)
        except Exception as exc:  # noqa: BLE001
            self.error(tr("Record"), exc)
            return
        self.canvas.select("clip", clip.id)
        self._refresh_all()
        self.info(tr("Recording added"))

    # ---- editing
    def add_line(self) -> None:
        self.begin_edit("Add line")
        self.project.add_line()
        self._refresh_all()

    def selected_clip(self) -> Optional[MixClip]:
        return self.canvas.selected_clip

    def delete_selected(self) -> None:
        clip, span = self.canvas.selected_clip, self.canvas.selected_effect
        if clip is not None:
            self.begin_edit("Delete clip")
            self.project.delete_clip(clip.id)
        elif span is not None:
            self.begin_edit("Remove effect")
            self.project.delete_effect(span.id)
        else:
            return
        self.canvas.select(None)
        self._refresh_all()

    def split_at_playhead(self) -> None:
        at = self.canvas.playhead
        clip = self.canvas.selected_clip
        if clip is None or not (clip.start < at < clip.end):
            clip = next((c for c in reversed(self.project.clips) if c.start < at < c.end), None)
        if clip is None:
            self.info(tr("Put the playhead inside a clip to split it"))
            return
        self.begin_edit("Split clip")
        parts = self.project.split_clip(clip.id, at)
        if parts is None:
            self.project.undo()
            self.info(tr("The playhead is too close to the edge of the clip"))
            return
        self.canvas.select("clip", parts[1].id)
        self._refresh_all()

    def duplicate_selected(self) -> None:
        clip = self.canvas.selected_clip
        if clip is None:
            return
        self.begin_edit("Duplicate clip")
        copy = self.project.duplicate_clip(clip.id)
        self.canvas.select("clip", copy.id)
        self._refresh_all()

    def crossfade_selected(self) -> None:
        clip = self.canvas.selected_clip
        if clip is None:
            self.info(tr("Select a clip that overlaps another one"))
            return
        self.begin_edit("Crossfade")
        if not self.project.crossfade(clip.id):
            self.project.undo()
            self.info(tr("Drag the clip so it overlaps the one it should blend with"))
            return
        self._refresh_all()
        self.info(tr("Cross-faded"))

    def set_clip_values(self, **values) -> None:
        clip = self.canvas.selected_clip
        if clip is None:
            return
        self.begin_edit("Edit clip")
        if "start" in values:
            self.project.move_clip(clip.id, values.pop("start"))
        if values:
            self.project.set_clip(clip.id, **values)
        self._refresh_all()

    def set_effect_range(self, start: Optional[int] = None, end: Optional[int] = None) -> None:
        span = self.canvas.selected_effect
        if span is None:
            return
        self.begin_edit("Resize effect")
        self.project.resize_effect(span.id, start=start, end=end)
        self._refresh_all()

    # ---- effects
    def choose_effect_menu(self) -> None:
        menu = QMenu(self.win)
        for category, effects in fx.by_category():
            sub = menu.addMenu(tr(category))
            for e in effects:
                if e.id == "envelope":
                    continue
                action = sub.addAction(tr(e.name))
                action.triggered.connect(lambda _=False, eid=e.id: self.add_effect(eid))
        menu.exec(self.win.cursor().pos())

    def _effect_context(self, effect, a: int, b: int) -> EffectContext:
        sr = self.sr
        preview_s = float(self.state.settings.get("preview_seconds", 8))
        text = tr(f"Applies to the effect line  {format_time(a / sr, ms=False, short=True)} – "
                  f"{format_time(b / sr, ms=False, short=True)}  ({(b - a) / sr:.1f} s)")

        def render(effect_, vals, bypass, channels, extra, progress, cancel):
            seq = self.project.render_tracks(a, b, cancel=cancel)
            return fx.render_preview(effect_, vals, seq, 0, seq.frames, seconds=preview_s, bypass=bypass, channels=channels,
                                     progress=progress, cancel=cancel, extra=extra)

        def play_array(arr, rate):
            self.stop()
            self._preview_active = True
            self.player.play(Clip.from_source(Source.from_array(arr, rate)))

        def stop_preview():
            if self._preview_active:
                self._preview_active = False
                self.player.stop()

        def is_playing():
            return self._preview_active and self.player.state == "playing"

        def validate(effect_, vals):
            if not effect_.preserves_length(effect_.clean(vals)):
                return tr(f"{tr(effect_.name)} changes the length of the sound, so it cannot be used on a mix.")
            if effect_.id != "custom_filter":
                return None
            try:
                silent = Clip.from_source(Source.silence(int(0.2 * sr), sr, CHANNELS))
                fx.run_effect(effect_, vals, silent, 0, silent.frames)
            except ffmpeg.FFmpegError as exc:
                return tr("FFmpeg did not accept that filter chain:\n\n") + (exc.detail or str(exc)).strip()[-500:]
            return None

        return EffectContext(range_text=text, channels=CHANNELS, frames=b - a, sr=sr, render_preview=render,
                             play_array=play_array, stop_preview=stop_preview, is_preview_playing=is_playing,
                             capture_noise=lambda: None, favorites=self.state.favorites, noise_store=self.noise_store,
                             validate=validate)

    def add_effect(self, effect_id: str, frame: Optional[int] = None) -> None:
        if self.project.empty:
            self.info(tr("Add some audio to the mix first"))
            return
        effect = fx.get(effect_id)
        total = self.project.total_frames
        a = self.canvas.playhead if frame is None else frame
        if a >= total - 1:
            a = 0
        b = min(total, a + 15 * self.sr)
        resume = self._suspend()
        try:
            ctx = self._effect_context(effect, a, b)
            dlg = EffectDialog(effect, None, ctx, self.win)
            accepted = dlg.exec() == QDialog.DialogCode.Accepted
            ctx.stop_preview()
            if not accepted:
                return
            self.begin_edit("Add effect")
            try:
                span = self.project.add_effect(effect_id, dlg.values(), a, b)
            except ValueError as exc:
                self.project.undo()
                self.error(tr(effect.name), exc)
                return
            self.canvas.select("effect", span.id)
            self._refresh_all()
        finally:
            self._resume(resume)

    def edit_effect(self, span_id: int) -> None:
        span = self.project.effect_span(span_id)
        effect = fx.get(span.effect)
        resume = self._suspend()
        try:
            ctx = self._effect_context(effect, span.start, min(span.end, max(span.start + 1, self.project.total_frames)))
            dlg = EffectDialog(effect, span.params, ctx, self.win)
            accepted = dlg.exec() == QDialog.DialogCode.Accepted
            ctx.stop_preview()
            if not accepted:
                return
            self.begin_edit("Edit effect")
            self.project.set_effect(span_id, params=dlg.values())
            self._refresh_all()
        finally:
            self._resume(resume)

    def _suspend(self) -> Optional[int]:
        """Stop for a dialog whose Preview plays through this same player. Returns the frame to pick up again from,
        or None if nothing was playing (a paused mix stays stopped)."""
        pos = self.player.position if self.playing and self.player.state == "playing" else None
        self.stop()
        return pos

    def _resume(self, pos: Optional[int]) -> None:
        if pos is not None and not self.project.empty:
            self.play(pos)

    # ---- playback
    def toggle_play(self) -> None:
        if self.playing:
            self.player.toggle_pause()
            self.playStateChanged.emit()
            return
        self.play()

    def _new_live(self) -> LiveMix:
        """A fresh copy of the mix as it is now, with its effects rendering in the background."""
        if self._live is not None:
            self._live.close()
        self._live = live = self.project.live(self._effect_cache)
        live.start_effects()
        return live

    def play(self, from_frame: Optional[int] = None) -> None:
        if self.project.empty:
            self.info(tr("Add some audio to the mix first"))
            return
        self.main_ctl.stop_playback()
        self.stop()
        live = self._new_live()
        if live.frames == 0:
            return
        start = self.canvas.playhead if from_frame is None else from_frame
        if start >= live.frames - 1:
            start = 0
        if not self._prepare_effects(live, start):
            return
        self.player.volume = float(self.state.settings.get("volume", 0.9))
        self.player.device = self.state.player.device
        try:
            self.player.play(live, start)
        except AudioDeviceError as exc:
            self.error(tr("Playback"), exc)
            return
        self._timer.start()
        self.playStateChanged.emit()

    def _prepare_effects(self, live: LiveMix, start: int) -> bool:
        """Starting inside an effect: give it a moment to render, so the first thing heard already has the effect.
        Everything later renders while the mix plays. False if the user cancelled."""
        ahead = int(EFFECT_LEAD_IN * self.sr)
        if not live.pending_at(start, ahead):
            return True
        live.start_effects(delay=0.0)

        def wait(progress, cancel):
            end = time.monotonic() + EFFECT_WAIT
            while live.pending_at(start, ahead) and time.monotonic() < end:
                cancel.check()
                time.sleep(0.02)

        try:
            ok, _ = run_task(self.win, tr("Preparing the effect"), wait, message=tr("Rendering the effect…"), delay_ms=400)
        except Exception as exc:  # noqa: BLE001
            self.error(tr("Playback"), exc)
            return False
        return ok

    def _live_changed(self) -> None:
        """The project changed. If it is playing, the player gets a fresh copy a moment later (changes that come
        faster, like a clip being dragged, share one swap)."""
        if self.playing and not self._live_timer.isActive():
            self._live_timer.start()

    def _apply_live(self) -> None:
        if not self.playing:
            return
        if self.project.empty:
            self.stop()
            return
        live = self._new_live()
        if live.frames == 0 or not self.player.swap(live):
            self.stop()

    def stop(self) -> None:
        was = self.player.state != "stopped"
        self._preview_active = False
        self._scrubbing = False
        self._live_timer.stop()
        if was:
            self.player.stop()
        if self._live is not None:
            self._live.close()
            self._live = None
        self._fx_pending = False
        self._timer.stop()
        if was:
            self.playStateChanged.emit()

    def scrub(self, frame: int) -> None:
        """Press and drag on the ruler: hear the mix at the pointer, to find a cut by ear. Does nothing while the mix
        is playing (the ruler then simply moves the playhead)."""
        if self.project.empty:
            return
        if self._scrubbing and self.player.state == "stopped":
            self._scrubbing = False
        if self._scrubbing:
            now = time.monotonic()
            if now - self._scrub_at >= SCRUB_INTERVAL:
                self._scrub_at = now
                self.player.seek(frame)
            return
        if self.playing or self._preview_active:
            return
        self.main_ctl.stop_playback()
        live = self._new_live()
        if live.frames == 0 or frame >= live.frames - 1:
            return
        self.player.volume = float(self.state.settings.get("volume", 0.9))
        self.player.device = self.state.player.device
        try:
            self.player.play(live, max(0, frame))
        except AudioDeviceError:
            return                                           # scrubbing is a convenience: say nothing without a sound card
        self._scrubbing = True
        self._scrub_at = time.monotonic()
        self._timer.start()
        self.playStateChanged.emit()

    def end_scrub(self) -> None:
        if self._scrubbing:
            self.stop()

    def go_start(self) -> None:
        self.canvas.place_playhead(0)
        self.canvas.ensure_visible(0)
        self.positionChanged.emit(0)

    def go_end(self) -> None:
        self.stop()
        self.canvas.place_playhead(self.project.total_frames)
        self.canvas.ensure_visible(self.project.total_frames)
        self.positionChanged.emit(self.project.total_frames)

    def _tick(self) -> None:
        p = self.player
        if self._preview_active:
            if p.consume_finished() or p.state == "stopped":
                self._preview_active = False
                p.stop()
            return
        if p.state == "stopped":
            if p.consume_finished():
                p.stop()
                self._timer.stop()
                self.playStateChanged.emit()
            return
        pos = p.position
        self.canvas.set_playhead(pos, follow=True)
        self.positionChanged.emit(pos)
        self._watch_effects()
        if p.consume_finished():
            p.stop()
            self._timer.stop()
            self.playStateChanged.emit()

    def _watch_effects(self) -> None:
        """Tell the user while an effect is still being rendered, and about it if that went wrong."""
        live = self._live
        if live is None:
            return
        if live.error is not None and not live.error_reported:
            live.error_reported = True
            self.error(tr("Playback"), live.error)
        pending = live.pending
        if pending != self._fx_pending:
            self._fx_pending = pending
            self.info(tr("Rendering the effects…") if pending else tr("Effects ready"))

    def confirm_quit(self) -> bool:
        """Offer to keep a mix that was never saved as a project."""
        if self.project.empty or not self._dirty:
            return True
        self.stop()
        box = QMessageBox(self.win)
        box.setWindowTitle(tr("Multitrack"))
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(tr("The multitrack mix has not been saved as a project."))
        box.setInformativeText(tr("Quit anyway? Build the mix or save the project first to keep your work."))
        quit_ = box.addButton(tr("Quit"), QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() is quit_

    # ---- building the file
    def build(self, *, save: bool = True) -> None:
        p = self.project
        if p.empty:
            self.info(tr("Add some audio to the mix first"))
            return
        self.stop()

        def job(progress, cancel):
            clip = p.render(progress=progress, cancel=cancel)
            peak = 0.0
            for chunk in clip.iter_chunks():
                if len(chunk):
                    peak = max(peak, float(np.abs(chunk).max()))
                cancel.check()
            return clip, peak

        try:
            ok, result = run_task(self.win, tr("Building the mix"), job,
                                  message=tr("Mixing the lines and applying the effects…"), delay_ms=200)
        except Exception as exc:  # noqa: BLE001
            self.error(tr("Build the mix"), exc)
            return
        if not ok:
            return
        clip, peak = result
        if peak > 1.0:
            answer = QMessageBox.question(
                self.win, tr("The mix is too loud"),
                tr(f"The loudest part of the mix is {20 * np.log10(peak):+.1f} dB over the maximum, so it would distort. "
                   "Lower the whole mix to prevent that?"))
            if answer == QMessageBox.StandardButton.Yes:
                clip = self._scaled(clip, 0.98 / peak)
        doc = Document(p.sr, CHANNELS, clip.pieces, name=self.main_ctl._unique_name(p.name or tr("Mix", "document")))
        item = self.main_ctl._add_document(doc)
        self.info(tr(f"Built {doc.name}: it is in the library"))
        if save:
            self.main_ctl.save_as(item)

    @staticmethod
    def _scaled(clip: PieceSeq, gain: float) -> Clip:
        writer = SourceWriter(clip.sr, clip.channels, label="mix")
        for chunk in clip.iter_chunks():
            writer.write(chunk * np.float32(gain))
        return Clip.from_source(writer.finish())

    # ---- projects
    def new_project(self) -> None:
        if not self._confirm_discard():
            return
        self.stop()
        self._replace_project(MixProject(self.state.settings.get("mix_sr", DEFAULT_SR)))
        self.info(tr("New mix"))

    def _confirm_discard(self) -> bool:
        if self.project.empty and not self.project.effects:
            return True
        answer = QMessageBox.question(self.win, tr("Multitrack"), tr("Replace the current mix? Unsaved changes will be lost."))
        return answer == QMessageBox.StandardButton.Yes

    def _replace_project(self, project: MixProject) -> None:
        self.project = project
        self._effect_cache.clear()
        self._dirty = False
        self.board.bind(project)
        self.canvas.place_playhead(0)
        self.canvas.fit()
        self.refreshed.emit()

    def save_project(self) -> None:
        try:
            self.project.to_dict()
        except ValueError as exc:
            self.error(tr("Save mix project"), exc)
            return
        folder = Path(self.state.settings.last_dir("save"))
        name, _ = QFileDialog.getSaveFileName(self.win, tr("Save mix project"), str(folder / "mix.twmix"), tr(PROJECT_FILTER))
        if not name:
            return
        path = Path(name).with_suffix(".twmix")
        try:
            self.project.save(path)
        except OSError as exc:
            self.error(tr("Save mix project"), exc)
            return
        self.state.settings.remember_dir("save", path)
        self._dirty = False
        self.info(tr(f"Saved {path.name}"))

    def open_project(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self.win, tr("Open mix project"), self.state.settings.last_dir("save"), tr(PROJECT_FILTER))
        if name and self._confirm_discard():
            self.load_project(Path(name))

    def load_project(self, path: Path) -> bool:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.error(tr("Open mix project"), exc)
            return False
        sr = int(data.get("sample_rate") or DEFAULT_SR) if isinstance(data, dict) else DEFAULT_SR
        trust = False
        chains = custom_filters_in(data)
        if chains:                                      # free-text FFmpeg filters can open files and addresses: ask first
            answer = QMessageBox.question(
                self.win, tr("Open mix project"),
                tr("This project contains a custom FFmpeg filter. Filters can read files and network addresses, so "
                   "only run it if you trust where the project came from. Run it?") + "\n\n" + chains[0][:300])
            trust = answer == QMessageBox.StandardButton.Yes
        previous = self.project.sr
        self.project.sr = sr                                    # the loader converts audio to the project's rate
        loader_project = self.project

        def job(progress, cancel):
            def load(file: str, rate: int) -> PieceSeq:
                p = Path(file)
                if not p.is_file():
                    raise FileNotFoundError(tr(f"The file {file} is missing."))
                return self._load_file(p, cancel=cancel)
            return MixProject.from_dict(data, load, trust_custom_filters=trust)

        try:
            ok, project = run_task(self.win, tr("Opening the mix"), job, message=tr("Reading the files of the mix…"), delay_ms=200)
        except Exception as exc:  # noqa: BLE001
            loader_project.sr = previous
            self.error(tr("Open mix project"), exc)
            return False
        if not ok:
            loader_project.sr = previous
            return False
        self.stop()
        self._replace_project(project)
        self.info(tr(f"Opened {Path(path).name}"))
        if project.skipped_effects:
            self.info(tr("The custom FFmpeg filter in this project was left out."))
        return True


# ====================================================================== the inspector (right-hand panel)
class MixInspector(QWidget):
    def __init__(self, ctl: MixController) -> None:
        super().__init__()
        self.ctl = ctl
        self.setMinimumWidth(300)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.tabBar().setExpanding(False)
        lay.addWidget(self.tabs)
        self.item_page = QStackedWidget()
        self.tabs.addTab(self.item_page, tr("Selection"))
        self.effects = EffectsTab()
        self.effects.effectRequested.connect(lambda eid: ctl.add_effect(eid))
        self.tabs.addTab(self.effects, tr("Effects"))
        self.item_page.addWidget(self._build_help())
        self.item_page.addWidget(self._build_clip_page())
        self.item_page.addWidget(self._build_effect_page())
        self._loading = False
        ctl.refreshed.connect(self.refresh)
        self.refresh()

    # ---- pages
    def _build_help(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 16, 16, 12)
        title = QLabel(tr("Multitrack mix"))
        title.setProperty("role", "title")
        lay.addWidget(title)
        text = QLabel(tr("Put as many files on top of one another as you like, each on its own line.\n\n"
                         "• Add files with “Add files…”, from the library, or by dropping them on the timeline.\n"
                         "• Drag a clip to move it, drag its edges to trim, and drag the white squares at its top "
                         "corners to fade it in or out.\n"
                         "• Overlap two clips and press “Crossfade” for a smooth transition.\n"
                         "• Use each line's sliders for volume and pan, and Mute / Solo to listen to parts.\n"
                         "• Double-click an effect, or choose one in the Effects tab, to lay it over a stretch of the mix.\n\n"
                         "When it sounds right, press “Build the mix”."))
        text.setWordWrap(True)
        text.setProperty("role", "dim")
        text.setAlignment(Qt.AlignmentFlag.AlignTop)
        lay.addWidget(text, 1)
        return w

    def _row(self, label: str, widget: QWidget) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(label)
        lab.setProperty("role", "dim")
        lab.setFixedWidth(96)
        h.addWidget(lab)
        h.addWidget(widget, 1)
        return w

    def _build_clip_page(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(8)
        lay.addWidget(section_label(tr("Clip")))
        self.clip_name = QLineEdit()
        self.clip_name.editingFinished.connect(lambda: None if self._loading else self.ctl.set_clip_values(name=self.clip_name.text().strip()))
        lay.addWidget(self._row(tr("Name"), self.clip_name))
        self.clip_line = QLabel("")
        lay.addWidget(self._row(tr("Line"), self.clip_line))
        self.clip_start = TimeEdit()
        self.clip_start.valueEdited.connect(lambda s: None if self._loading else self.ctl.set_clip_values(start=int(s * self.ctl.sr)))
        lay.addWidget(self._row(tr("Starts at"), self.clip_start))
        self.clip_length = QLabel("")
        lay.addWidget(self._row(tr("Length"), self.clip_length))
        self.clip_gain = QDoubleSpinBox()
        self.clip_gain.setRange(-30.0, 12.0)
        self.clip_gain.setDecimals(1)
        self.clip_gain.setSingleStep(0.5)
        self.clip_gain.setSuffix(tr(" dB"))
        self.clip_gain.valueChanged.connect(lambda v: None if self._loading else self.ctl.set_clip_values(gain_db=v))
        lay.addWidget(self._row(tr("Volume"), self.clip_gain))
        self.fade_in = QDoubleSpinBox()
        self.fade_out = QDoubleSpinBox()
        for spin, key in ((self.fade_in, "fade_in"), (self.fade_out, "fade_out")):
            spin.setRange(0.0, 600.0)
            spin.setDecimals(2)
            spin.setSingleStep(0.25)
            spin.setSuffix(tr(" s"))
            spin.valueChanged.connect(lambda v, k=key: None if self._loading else self.ctl.set_clip_values(**{k: int(v * self.ctl.sr)}))
        lay.addWidget(self._row(tr("Fade in"), self.fade_in))
        lay.addWidget(self._row(tr("Fade out"), self.fade_out))
        lay.addWidget(divider())
        for text, icon_name, handler in ((tr("Crossfade with the clip it overlaps"), "wand", self.ctl.crossfade_selected),
                                         (tr("Split at the playhead"), "scissors-split", self.ctl.split_at_playhead),
                                         (tr("Duplicate"), "copy", self.ctl.duplicate_selected),
                                         (tr("Delete clip"), "trash", self.ctl.delete_selected)):
            b = make_button(text, icon=icon_name)
            b.clicked.connect(lambda _=False, h=handler: h())
            lay.addWidget(b)
        lay.addStretch(1)
        return w

    def _build_effect_page(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(8)
        lay.addWidget(section_label(tr("Effect on the mix")))
        self.fx_name = QLabel("")
        self.fx_name.setProperty("role", "title")
        lay.addWidget(self.fx_name)
        self.fx_start = TimeEdit()
        self.fx_end = TimeEdit()
        self.fx_start.valueEdited.connect(lambda s: None if self._loading else self.ctl.set_effect_range(start=int(s * self.ctl.sr)))
        self.fx_end.valueEdited.connect(lambda s: None if self._loading else self.ctl.set_effect_range(end=int(s * self.ctl.sr)))
        lay.addWidget(self._row(tr("From"), self.fx_start))
        lay.addWidget(self._row(tr("To"), self.fx_end))
        for text, icon_name, handler in ((tr("Settings…"), "sliders", lambda: self.ctl.edit_effect(self.ctl.canvas.selected[1])),
                                         (tr("Remove effect"), "trash", self.ctl.delete_selected)):
            b = make_button(text, icon=icon_name)
            b.clicked.connect(lambda _=False, h=handler: h())
            lay.addWidget(b)
        lay.addStretch(1)
        return w

    # ---- state
    def refresh(self) -> None:
        self._loading = True
        try:
            clip, span = self.ctl.canvas.selected_clip, self.ctl.canvas.selected_effect
            sr = self.ctl.sr
            if clip is not None:
                self.item_page.setCurrentIndex(1)
                self.clip_name.setText(clip.name)
                line = next((l for l in self.ctl.project.lines if l.id == clip.line), None)
                self.clip_line.setText(tr(line.name) if line else "")
                self.clip_start.setValue(clip.start / sr)
                self.clip_length.setText(format_time(clip.length / sr, short=True))
                self.clip_gain.setValue(clip.gain_db)
                self.fade_in.setValue(clip.fade_in / sr)
                self.fade_out.setValue(clip.fade_out / sr)
            elif span is not None:
                self.item_page.setCurrentIndex(2)
                self.fx_name.setText(tr(fx.get(span.effect).name))
                self.fx_start.setValue(span.start / sr)
                self.fx_end.setValue(span.end / sr)
            else:
                self.item_page.setCurrentIndex(0)
        finally:
            self._loading = False


# ====================================================================== the page (centre)
class MixPage(QWidget):
    def __init__(self, win, state: AppState, main_ctl) -> None:
        super().__init__()
        self.win, self.state = win, state
        self.board = MixBoard()
        self.ctl = MixController(win, state, main_ctl, self.board)
        self.inspector = MixInspector(self.ctl)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_toolbar())
        root.addWidget(self.board, 1)
        root.addWidget(self._build_transport())
        self.ctl.refreshed.connect(self.sync)
        self.ctl.playStateChanged.connect(self.sync)
        self.ctl.positionChanged.connect(self._position)
        self.board.canvas.contextRequested.connect(self._context_menu)
        self.sync()

    # ---- toolbar
    def _tool(self, icon_name: str, tip: str, handler, *, checkable: bool = False, text: str = "") -> QToolButton:
        b = icon_button(icon_name, tip, checkable=checkable, text=text)
        if checkable:
            b.toggled.connect(handler)
        else:
            b.clicked.connect(lambda _=False: handler())
        return b

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("mixtoolbar")
        bar.setStyleSheet(f"#mixtoolbar {{ background: {theme.current().bg0}; border-bottom: 1px solid {theme.current().line}; }}")
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 5, 14, 5)
        h.setSpacing(2)
        c = self.ctl
        self.add_files_btn = self._tool("open", tr("Add audio files to the mix"), c.add_files_dialog, text=tr("Add files…"))
        self.add_lib_btn = self._tool("layers", tr("Add the files selected in the library"), c.add_library_selection)
        self.add_line_btn = self._tool("plus", tr("Add an empty line"), c.add_line)
        for b in (self.add_files_btn, self.add_lib_btn, self.add_line_btn):
            h.addWidget(b)
        h.addSpacing(6)
        h.addWidget(divider(True))
        h.addSpacing(6)
        self.split_btn = self._tool("scissors-split", tr("Split the clip at the playhead (S)"), c.split_at_playhead)
        self.dup_btn = self._tool("copy", tr("Duplicate the selected clip (Ctrl+D)"), c.duplicate_selected)
        self.cross_btn = self._tool("wand", tr("Cross-fade the selected clip with the one it overlaps"), c.crossfade_selected, text=tr("Crossfade"))
        self.del_btn = self._tool("trash", tr("Delete the selection (Del)"), c.delete_selected)
        for b in (self.split_btn, self.dup_btn, self.cross_btn, self.del_btn):
            h.addWidget(b)
        h.addSpacing(6)
        h.addWidget(divider(True))
        h.addSpacing(6)
        self.fx_btn = self._tool("sliders", tr("Lay an effect over part of the mix"), c.choose_effect_menu)
        h.addWidget(self.fx_btn)
        h.addSpacing(6)
        h.addWidget(divider(True))
        h.addSpacing(6)
        self.snap = QCheckBox(tr("Snap"))
        self.snap.setChecked(True)
        self.snap.setToolTip(tr("Snap clips to each other and to the playhead"))
        self.snap.toggled.connect(lambda on: setattr(self.board.canvas, "snap", on))
        h.addWidget(self.snap)
        h.addSpacing(6)
        self.zoom_out_btn = self._tool("zoom-out", tr("Zoom out"), lambda: self.board.zoom(0.8))
        self.zoom_in_btn = self._tool("zoom-in", tr("Zoom in"), lambda: self.board.zoom(1.25))
        self.fit_btn = self._tool("zoom-fit", tr("Fit the whole mix"), self.board.fit)
        for b in (self.zoom_out_btn, self.zoom_in_btn, self.fit_btn):
            h.addWidget(b)
        h.addSpacing(6)
        h.addWidget(divider(True))
        h.addSpacing(6)
        self.undo_btn = self._tool("undo", tr("Undo"), c.undo)
        self.redo_btn = self._tool("redo", tr("Redo"), c.redo)
        h.addWidget(self.undo_btn)
        h.addWidget(self.redo_btn)
        h.addStretch(1)
        self.project_btn = QToolButton()
        icons.bind(self.project_btn, "save", 18)
        self.project_btn.setToolTip(tr("Mix project"))
        self.project_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.project_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.project_btn.setAutoRaise(True)
        menu = QMenu(self.project_btn)
        menu.addAction(tr("New mix"), c.new_project)
        menu.addAction(tr("Open mix project…"), c.open_project)
        menu.addAction(tr("Save mix project…"), c.save_project)
        self.project_btn.setMenu(menu)
        h.addWidget(self.project_btn)
        return bar

    # ---- transport
    def _build_transport(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("mixtransport")
        bar.setStyleSheet(f"#mixtransport {{ background: {theme.current().bg1}; border-top: 1px solid {theme.current().line}; }}")
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 8, 16, 8)
        h.setSpacing(8)
        c = self.ctl
        self.start_btn = icon_button("skip-start", tr("Go to the start"), size=20)
        self.start_btn.clicked.connect(c.go_start)
        self.play_btn = make_button("", "primary", "play")
        self.play_btn.setFixedSize(46, 40)
        self.play_btn.setToolTip(tr("Play or pause"))
        self.play_btn.clicked.connect(c.toggle_play)
        self.stop_btn = icon_button("stop", tr("Stop"), size=20)
        self.stop_btn.clicked.connect(c.stop)
        for w in (self.start_btn, self.play_btn, self.stop_btn):
            h.addWidget(w)
        self.time = QLabel("00:00:00.000")
        self.time.setFont(theme.mono_font(16, QFont.Weight.DemiBold))
        self.total = role_label("", "dim")
        h.addSpacing(10)
        h.addWidget(self.time)
        h.addWidget(self.total)
        h.addStretch(1)
        self.status = role_label("", "dim")
        h.addWidget(self.status)
        h.addSpacing(8)
        self.lib_btn = make_button(tr("Add to library"), icon="layers")
        self.lib_btn.setToolTip(tr("Build the mix and add it to the library without saving a file yet"))
        self.lib_btn.clicked.connect(lambda: c.build(save=False))
        self.build_btn = make_button(tr("Build the mix…"), "primary", "download")
        self.build_btn.setToolTip(tr("Mix everything into one audio file"))
        self.build_btn.setMinimumHeight(40)
        self.build_btn.clicked.connect(lambda: c.build(save=True))
        h.addWidget(self.lib_btn)
        h.addWidget(self.build_btn)
        c.message.connect(self.status.setText)
        return bar

    # ---- state
    def sync(self) -> None:
        c = self.ctl
        p = c.project
        self.undo_btn.setEnabled(p.can_undo)
        self.redo_btn.setEnabled(p.can_redo)
        sel_clip = c.canvas.selected_clip is not None
        for b in (self.split_btn, self.dup_btn, self.cross_btn):
            b.setEnabled(sel_clip)
        self.del_btn.setEnabled(c.canvas.selected is not None)
        self.total.setText("/  " + format_time(p.duration, ms=False, short=True))
        playing = c.player.state == "playing" and not c._preview_active
        icons.bind(self.play_btn, "pause" if playing else "play", 16, role="on_accent")
        self.build_btn.setEnabled(not p.empty)
        self.lib_btn.setEnabled(not p.empty)
        self.play_btn.setEnabled(not p.empty)
        self.undo_btn.setToolTip(tr("Undo") + (f": {tr(p.undo_label)}" if p.can_undo else ""))
        self.redo_btn.setToolTip(tr("Redo") + (f": {tr(p.redo_label)}" if p.can_redo else ""))

    def _position(self, frame: int) -> None:
        self.time.setText(format_time(frame / self.ctl.sr))

    def restyle(self) -> None:
        t = theme.current()
        self.findChild(QWidget, "mixtoolbar").setStyleSheet(f"#mixtoolbar {{ background: {t.bg0}; border-bottom: 1px solid {t.line}; }}")
        self.findChild(QWidget, "mixtransport").setStyleSheet(f"#mixtransport {{ background: {t.bg1}; border-top: 1px solid {t.line}; }}")
        self.board.restyle()

    # ---- context menu
    def _context_menu(self, pos: QPoint, hit: Hit, frame: int) -> None:
        c = self.ctl
        menu = QMenu(self)
        if hit.kind in ("clip", "clip-l", "clip-r", "fade-in", "fade-out", "gain"):
            menu.addAction(tr("Split at the playhead"), c.split_at_playhead)
            menu.addAction(tr("Duplicate"), c.duplicate_selected)
            menu.addAction(tr("Crossfade with the clip it overlaps"), c.crossfade_selected)
            menu.addSeparator()
            menu.addAction(tr("Delete clip"), c.delete_selected)
        elif hit.kind.startswith("effect"):
            menu.addAction(tr("Settings…"), lambda: c.edit_effect(hit.id))
            menu.addAction(tr("Remove effect"), c.delete_selected)
        elif hit.kind in ("line", "none"):
            menu.addAction(tr("Add files here…"), lambda: self._add_files_at(hit, frame))
            menu.addAction(tr("Add the selected library files here"), lambda: self._add_library_at(hit, frame))
            menu.addAction(tr("Add line"), c.add_line)
        elif hit.kind in ("fx-row", "spare"):
            menu.addAction(tr("Add effect…"), c.choose_effect_menu)
        if not menu.isEmpty():
            menu.exec(pos)

    def _add_files_at(self, hit: Hit, frame: int) -> None:
        names, _ = QFileDialog.getOpenFileNames(self.win, tr("Add audio to the mix"), self.state.settings.last_dir("open"),
                                                self.ctl._open_filter())
        if names:
            self.ctl.add_files([Path(n) for n in names], hit.id if hit.kind == "line" else None, frame)

    def _add_library_at(self, hit: Hit, frame: int) -> None:
        items = self.win.library.selected_items()
        if items:
            self.ctl.add_files(items, hit.id if hit.kind == "line" else None, frame)
