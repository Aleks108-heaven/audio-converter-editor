"""The Multitrack page end to end. Windows are never shown; dialogs are scripted."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, QUrl, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QFileDialog, QMessageBox

from tonewright.core import effects as fx
from tonewright.core import ffmpeg
from tonewright.core.mixer import MixProject

import test_ui
from test_audio_io import FakeOutput
from test_ui import _no_hidden_errors, make_files, pump, qapp, ui, wait_until  # noqa: F401 - fixtures

SR = 44100


@pytest.fixture
def mix(ui, qapp, tmp_path):
    """The window switched to Multitrack, with three short files on disk."""
    ui.set_mode("multitrack")
    ui.files = make_files(tmp_path / "src", 3, 3.0)
    ui.mix = ui.mix_page.ctl
    ui.canvas = ui.mix_page.board.canvas
    ui.board = ui.mix_page.board
    ui.board.resize(1100, 700)
    return ui


def press(canvas, x, y, button=Qt.MouseButton.LeftButton):
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(x, y), QPointF(x, y), button, button, Qt.KeyboardModifier.NoModifier)
    canvas.mousePressEvent(ev)


def move(canvas, x, y):
    ev = QMouseEvent(QEvent.Type.MouseMove, QPointF(x, y), QPointF(x, y), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    canvas.mouseMoveEvent(ev)


def release(canvas, x, y):
    ev = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(x, y), QPointF(x, y), Qt.MouseButton.LeftButton,
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    canvas.mouseReleaseEvent(ev)


def drag(canvas, start, end):
    press(canvas, *start)
    move(canvas, *start)
    move(canvas, *end)
    release(canvas, *end)


def centre_of(canvas, clip):
    row = canvas.row_of("line", clip.line)
    r = canvas.clip_rect(clip, row)
    return r.center().x(), r.center().y()


# ------------------------------------------------------------------ the mode
def test_multitrack_is_a_mode_with_its_own_page_and_inspector(mix):
    assert list(mix.mode_buttons) == ["convert", "edit", "multitrack", "tags", "audiobook"]
    assert mix.state.mode == "multitrack"
    assert mix.center_stack.currentWidget() is mix.mix_page
    assert mix.right_stack.currentWidget() is mix.mix_page.inspector
    assert mix.actions["mode_multitrack"].shortcut().toString() == "Ctrl+3"
    assert mix.actions["mode_tags"].shortcut().toString() == "Ctrl+4" and mix.actions["mode_audiobook"].shortcut().toString() == "Ctrl+5"
    assert not mix.toolbar.isVisibleTo(mix) and not mix.convert_bar.isVisibleTo(mix)


def test_the_last_mode_is_remembered(mix):
    assert mix.state.settings.get("last_mode") == "multitrack"
    mix.set_mode("edit")
    assert mix.center_stack.currentIndex() == 0 and mix.right_stack.currentIndex() == 0


# ------------------------------------------------------------------ adding audio
def test_added_files_land_on_separate_lines_on_top_of_each_other(mix, qapp):
    mix.mix.add_files(mix.files[:2])
    p = mix.mix.project
    assert len(p.lines) == 2 and len(p.clips) == 2
    assert [c.start for c in p.clips] == [0, 0] and {c.line for c in p.clips} == {l.id for l in p.lines}
    assert p.clips[0].path == str(mix.files[0]) and p.clips[0].length == pytest.approx(3 * SR, abs=2000)
    assert mix.canvas.selected_clip is p.clips[-1]
    assert [h.line_id for h in mix.board.headers.lines.values()] == [l.id for l in p.lines]


def test_files_start_at_the_playhead_and_reuse_free_lines(mix):
    mix.mix.add_files(mix.files[:1])
    mix.canvas.place_playhead(5 * SR)                       # after the first clip ends: the same line is free
    mix.mix.add_files(mix.files[1:2])
    p = mix.mix.project
    assert len(p.lines) == 1 and sorted(c.start for c in p.clips) == [0, 5 * SR]


def test_library_items_and_edited_documents_can_be_added(mix, tmp_path):
    mix.ctl.open_paths(mix.files[:2])
    assert wait_until(test_ui.QApplication.instance(), lambda: len(mix.state.items) == 2)
    items = mix.state.items
    mix.ctl.state.set_current(items[0])
    wait_until(test_ui.QApplication.instance(), lambda: items[0].doc is not None)
    items[0].doc.delete(0, SR)                               # an edit that is not saved
    mix.mix.add_files(items)
    clips = mix.mix.project.clips
    assert clips[0].path is None and clips[0].length == pytest.approx(2 * SR, abs=10)      # the edited version, shorter
    assert clips[1].path == str(mix.files[1])


def test_dropping_files_places_them_at_the_drop_point(mix):
    mix.mix.add_files(mix.files[:1])
    line = mix.mix.project.lines[0]
    mix.canvas.filesDropped.emit([mix.files[1]], line.id, 8 * SR)
    clips = mix.mix.project.clips_on(line.id)
    assert [c.start for c in clips] == [0, 8 * SR]


def test_drag_and_drop_from_explorer_is_accepted(mix):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(mix.files[0]))])
    assert mix.canvas._local_files(mime) == [mix.files[0]]
    mime.setUrls([QUrl("https://example.com/a.mp3")])
    assert mix.canvas._local_files(mime) == []


# ------------------------------------------------------------------ the timeline
def test_dragging_a_clip_moves_it_and_changes_line_and_undo_restores(mix):
    mix.mix.add_files(mix.files[:2])
    p = mix.mix.project
    clip = p.clips[0]
    x, y = centre_of(mix.canvas, clip)
    second_row = mix.canvas.row_of("line", p.lines[1].id)
    drag(mix.canvas, (x, y), (x + 4 * mix.canvas.px_per_sec, second_row.y + 30))
    assert clip.line == p.lines[1].id and clip.start == pytest.approx(4 * SR, abs=SR * 0.4)
    assert mix.mix.project.can_undo
    mix.mix.undo()
    restored = p.clip(clip.id)                       # undo restores the arrangement, so look the clip up again
    assert restored.start == 0 and restored.line == p.lines[0].id


def test_dragging_a_clip_snaps_to_the_edges_of_other_clips(mix):
    mix.mix.add_files(mix.files[:1])
    mix.canvas.place_playhead(10 * SR)
    mix.mix.add_files(mix.files[1:2])
    p = mix.mix.project
    first, second = p.clips
    x, y = centre_of(mix.canvas, second)
    drag(mix.canvas, (x, y), (x - 6.9 * mix.canvas.px_per_sec - 3, y))          # lands within a few px of the first clip's end
    assert p.clip(second.id).start == p.clip(first.id).end
    mix.canvas.snap = False
    x, y = centre_of(mix.canvas, p.clip(second.id))
    drag(mix.canvas, (x, y), (x + 3, y))
    assert p.clip(second.id).start != p.clip(first.id).end


def test_trimming_with_the_edges_and_fading_with_the_handles(mix):
    mix.mix.add_files(mix.files[:1])
    p = mix.mix.project
    clip = p.clips[0]
    row = mix.canvas.row_of("line", clip.line)
    r = mix.canvas.clip_rect(clip, row)
    drag(mix.canvas, (r.left() + 1, r.center().y()), (r.left() + 1 + 24, r.center().y()))
    assert clip.start > 0 and clip.offset == clip.start
    r = mix.canvas.clip_rect(clip, row)
    drag(mix.canvas, (r.right() - 1, r.center().y()), (r.right() - 13, r.center().y()))
    assert clip.length < 3 * SR - clip.offset
    mix.canvas.select("clip", clip.id)                              # handles are only grabbable on a selected clip
    r = mix.canvas.clip_rect(clip, row)
    drag(mix.canvas, (r.left() + 1, r.top() + 2), (r.left() + 31, r.top() + 2))
    assert clip.fade_in == pytest.approx(30 / mix.canvas.px_per_sec * SR, abs=SR * 0.1)


def test_the_green_line_sets_the_clip_volume(mix):
    mix.mix.add_files(mix.files[:1])
    clip = mix.mix.project.clips[0]
    r = mix.canvas.clip_rect(clip, mix.canvas.row_of("line", clip.line))
    y = mix.canvas.gain_y(clip, r)
    drag(mix.canvas, (r.center().x(), y), (r.center().x(), y - 20))
    assert clip.gain_db == 12.0                                   # 20 px up would be +50 dB: clamped to the maximum


def test_clicking_empty_space_moves_the_playhead_and_clears_the_selection(mix):
    mix.mix.add_files(mix.files[:1])
    row = mix.canvas.rows[-1]
    press(mix.canvas, 300, row.y + 5)
    release(mix.canvas, 300, row.y + 5)
    assert mix.canvas.selected is None and mix.canvas.playhead == mix.canvas.x_to_frame(300)


def test_zoom_scroll_and_fit(mix):
    mix.mix.add_files(mix.files[:1])
    before = mix.canvas.px_per_sec
    mix.board.zoom(2.0)
    assert mix.canvas.px_per_sec == pytest.approx(before * 2)
    mix.canvas.set_t0(3.0)
    assert mix.board.hbar.value() == 3000
    mix.board.hbar.setValue(1500)
    assert mix.canvas.t0 == pytest.approx(1.5)
    mix.board.fit()
    assert mix.canvas.t0 == 0.0 and mix.canvas.px_per_sec > 2.0


# ------------------------------------------------------------------ headers
def test_line_headers_drive_volume_pan_mute_solo_and_remove(mix):
    mix.mix.add_files(mix.files[:2])
    p = mix.mix.project
    h = mix.board.headers.lines[p.lines[0].id]
    h.volume.setValue(-120)
    h.pan.setValue(50)
    assert p.lines[0].volume_db == pytest.approx(-12.0) and p.lines[0].pan == pytest.approx(0.5)
    h.mute.setChecked(True)
    assert p.lines[0].mute and p.audible_lines() == {p.lines[1].id}
    h.solo.setChecked(True)
    assert p.lines[0].solo
    mix.mix.undo()
    assert not p.lines[0].solo
    removed = p.lines[0].id
    h.removeRequested.emit(removed)
    assert len(p.lines) == 1 and len(p.clips) == 1 and removed not in {l.id for l in p.lines}
    assert set(mix.board.headers.lines) == {p.lines[0].id}
    mix.mix.undo()
    assert len(p.lines) == 2 and len(p.clips) == 2 and len(mix.board.headers.lines) == 2


def test_double_click_resets_a_slider_and_names_are_translated_defaults(mix):
    mix.mix.add_line()
    h = next(iter(mix.board.headers.lines.values()))
    h.volume.setValue(55)
    h.volume.mouseDoubleClickEvent(None)
    assert h.volume.value() == 0
    assert h.name.text() == "Line 1"


# ------------------------------------------------------------------ editing commands
def test_split_duplicate_crossfade_and_delete(mix):
    mix.mix.add_files(mix.files[:1])
    p = mix.mix.project
    clip = p.clips[0]
    mix.canvas.place_playhead(SR)
    mix.mix.split_at_playhead()
    assert len(p.clips) == 2 and p.clips[1].start == SR
    mix.mix.duplicate_selected()
    assert len(p.clips) == 3
    copy = p.clips[-1]
    p.move_clip(copy.id, p.clips[1].end - SR // 2)
    mix.canvas.select("clip", copy.id)
    mix.mix.crossfade_selected()
    assert copy.fade_in > 0
    mix.mix.delete_selected()
    assert len(p.clips) == 2
    mix.mix.undo()
    assert len(p.clips) == 3


def test_split_outside_a_clip_and_crossfade_without_overlap_explain_themselves(mix):
    messages = []
    mix.mix.message.connect(messages.append)
    mix.mix.add_files(mix.files[:1])
    mix.canvas.place_playhead(30 * SR)
    mix.mix.split_at_playhead()
    assert "playhead" in messages[-1].lower()
    mix.canvas.select("clip", mix.mix.project.clips[0].id)
    mix.mix.crossfade_selected()
    assert "overlap" in messages[-1].lower()
    assert not mix.mix.project.can_undo or mix.mix.project.undo_label != "Crossfade"


def test_the_inspector_edits_the_selected_clip(mix):
    mix.mix.add_files(mix.files[:1])
    insp = mix.mix_page.inspector
    clip = mix.mix.project.clips[0]
    assert insp.item_page.currentIndex() == 1 and insp.clip_name.text() == clip.name
    insp.clip_gain.setValue(-4.5)
    insp.fade_in.setValue(0.5)
    insp.clip_start.setValue(2.0)
    insp.clip_start.valueEdited.emit(2.0)
    assert clip.gain_db == -4.5 and clip.fade_in == SR // 2 and clip.start == 2 * SR
    mix.canvas.select(None)
    assert insp.item_page.currentIndex() == 0


# ------------------------------------------------------------------ effects on the mix
def test_an_effect_line_is_added_edited_resized_and_applied(mix, tmp_path):
    mix.mix.add_files(mix.files[:1])
    mix.script.dialog_hooks["EffectDialog"] = lambda d: d.controls["gain"].set_value(-6.0) or d.controls["prevent_clipping"].set_value(False)
    mix.mix.add_effect("amplify", 0)
    p = mix.mix.project
    assert len(p.effects) == 1 and p.effects[0].params["gain"] == -6.0
    span = p.effects[0]
    assert mix.canvas.selected == ("effect", span.id) and span.end == min(p.total_frames, 15 * SR)
    assert mix.mix_page.inspector.item_page.currentIndex() == 2
    assert mix.board.headers.effects[span.id].name.text() == "Amplify"
    plain = p.render(effects=False).read(0, 1000)
    with_fx = p.render().read(0, 1000)
    np.testing.assert_allclose(with_fx, plain * 10 ** (-6 / 20), atol=1e-4)
    mix.script.dialog_hooks["EffectDialog"] = lambda d: d.controls["gain"].set_value(3.0)
    mix.mix.edit_effect(span.id)
    assert span.params["gain"] == 3.0
    row = mix.canvas.row_of("effect", span.id)
    r = mix.canvas.effect_rect(span, row)
    drag(mix.canvas, (r.right() - 1, r.center().y()), (r.right() - 61, r.center().y()))
    assert span.end < min(p.total_frames, 15 * SR)
    mix.board.headers.effects[span.id].mute.setChecked(True)
    assert span.mute


def test_an_effect_that_changes_the_length_is_rejected_by_the_dialog(mix):
    mix.mix.add_files(mix.files[:1])
    seen = {}
    def hook(d):
        d.controls["tail"].set_value(True)
        seen["problem"] = d.ctx.validate(d.effect, d.values())
    mix.script.dialog_hooks["EffectDialog"] = hook
    mix.mix.add_effect("reverb", 0)
    assert "length" in seen["problem"]
    assert mix.mix.project.effects == []                          # refused, and nothing was left half-added
    assert not mix.mix.project.can_undo or mix.mix.project.undo_label != "Add effect"


def test_an_effect_needs_some_audio_first(mix):
    messages = []
    mix.mix.message.connect(messages.append)
    mix.mix.add_effect("amplify")
    assert messages and mix.mix.project.effects == []


# ------------------------------------------------------------------ playback
def paused_mix(mix, qapp, at: int = SR // 2):
    """The mix playing, then frozen at `at`, so what an edit does to it can be looked at without racing the clock."""
    mix.mix.play(at)
    assert wait_until(qapp, lambda: mix.mix.player.position > at)
    mix.mix.toggle_play()
    assert mix.mix.player.state == "paused"
    mix.mix.player.seek(at)
    return mix.mix.player


def heard_after(stream, n_before: int, frames: int = 4000) -> np.ndarray:
    """Whatever the fake sound card played from block `n_before` on, once at least `frames` of it exist."""
    assert wait_until(test_ui.QApplication.instance(), lambda: sum(len(b) for b in stream.played[n_before:]) >= frames)
    return np.concatenate(stream.played[n_before:])


def test_play_starts_at_once_and_pauses_and_stops(mix, qapp):
    mix.mix.add_files(mix.files[:2])
    mix.canvas.place_playhead(SR // 2)
    mix.mix.play()
    assert mix.mix.player.state == "playing" and mix.mix_page.play_btn.isEnabled()
    assert wait_until(qapp, lambda: len(FakeOutput.instances[-1].played) > 50)
    heard = FakeOutput.instances[-1].audio()[:3000]                      # no rendering first: the lines are mixed as it plays
    np.testing.assert_allclose(heard / mix.mix.player.volume, mix.mix.project.render().read(SR // 2, SR // 2 + 3000), atol=1e-6)
    mix.mix.toggle_play()
    assert mix.mix.player.state == "paused"
    mix.mix.stop()
    assert mix.mix.player.state == "stopped"
    mix.mix.play()
    assert mix.mix.player.state == "playing"
    mix.mix.stop()


def test_editing_keeps_the_mix_playing_and_the_change_is_heard(mix, qapp):
    mix.mix.add_files(mix.files[:2])
    player = paused_mix(mix, qapp)
    before = player.sequence
    p = mix.mix.project
    stream = FakeOutput.instances[-1]
    mix.mix.add_line()                                                   # any edit used to stop the mix
    assert player.state == "paused"
    assert wait_until(qapp, lambda: player.sequence is not before)       # a fresh copy of the mix took over
    mix.board._mute(p.lines[0].id, True)
    mix.board._mute(p.lines[1].id, True)
    assert wait_until(qapp, lambda: player.sequence.read(SR // 2, SR // 2 + 100).max() == 0.0)
    mark = len(stream.played)
    mix.mix.toggle_play()
    assert player.state == "playing"
    assert not heard_after(stream, mark).any()                           # both lines muted: silence
    mix.board._mute(p.lines[0].id, False)                                # and un-muting is heard while it plays
    mark = len(stream.played)
    assert wait_until(qapp, lambda: heard_after(stream, mark, 2000).any())
    mix.mix.stop()


def test_dragging_a_clip_while_playing_swaps_the_mix_in_as_it_moves(mix, qapp):
    mix.mix.add_files(mix.files[:1])
    player = paused_mix(mix, qapp, 2 * SR)
    clip = mix.mix.project.clips[0]
    before = player.sequence
    x, y = centre_of(mix.canvas, clip)
    press(mix.canvas, x, y)
    move(mix.canvas, x, y)
    move(mix.canvas, x + 2 * mix.canvas.px_per_sec, y)
    assert wait_until(qapp, lambda: player.sequence is not before)
    assert clip.start > 0 and player.sequence.frames == mix.mix.project.total_frames > 3 * SR
    release(mix.canvas, x + 2 * mix.canvas.px_per_sec, y)
    assert player.state == "paused"
    mix.mix.stop()


def test_splitting_while_playing_carries_on_from_the_playhead(mix, qapp):
    mix.mix.add_files(mix.files[:1])
    player = paused_mix(mix, qapp, SR)
    mix.canvas.set_playhead(SR)
    mix.mix.split_at_playhead()
    assert len(mix.mix.project.clips) == 2 and player.state == "paused"
    assert wait_until(qapp, lambda: abs(player.position - SR) < 400)
    mix.mix.stop()


def test_deleting_the_last_clip_while_playing_ends_it_and_undo_brings_it_back(mix, qapp):
    mix.mix.add_files(mix.files[:1])
    player = paused_mix(mix, qapp)
    mix.mix.delete_selected()
    assert wait_until(qapp, lambda: mix.mix.project.empty and player.state == "stopped")      # nothing left to play
    mix.mix.undo()
    assert len(mix.mix.project.clips) == 1
    mix.mix.play()
    assert player.state == "playing"
    mix.mix.stop()


def test_an_effect_added_while_playing_is_heard_when_playing_resumes(mix, qapp):
    mix.mix.add_files(mix.files[:1])
    mix.script.dialog_hooks["EffectDialog"] = lambda d: d.controls["gain"].set_value(-20.0) or d.controls["prevent_clipping"].set_value(False)
    mix.mix.play(SR // 2)
    assert wait_until(qapp, lambda: mix.mix.player.position > SR // 2)
    mix.mix.add_effect("amplify", SR // 2)
    player, stream = mix.mix.player, FakeOutput.instances[-1]
    assert player.state == "playing" and len(mix.mix.project.effects) == 1       # playing went on once the dialog closed
    assert wait_until(qapp, lambda: sum(len(b) for b in stream.played) >= 3000)
    start = player.start
    assert start >= SR // 2                                                      # picked up where it was, inside the effect
    plain = mix.mix.project.render(effects=False).read(start, start + 3000)
    np.testing.assert_allclose(stream.audio()[:3000] / player.volume, plain * 0.1, atol=1e-4)      # -20 dB from the very first sample
    mix.mix.stop()


def test_cancelling_the_effect_dialog_leaves_the_mix_playing(mix, qapp):
    mix.mix.add_files(mix.files[:1])
    mix.mix.play(SR // 2)
    assert wait_until(qapp, lambda: mix.mix.player.position > SR // 2)
    mix.script.dialog_hooks["EffectDialog"] = lambda d: False
    mix.mix.add_effect("amplify", SR // 2)
    assert mix.mix.player.state == "playing" and mix.mix.project.effects == []
    mix.mix.stop()


def test_dragging_on_the_ruler_plays_what_is_under_the_pointer(mix, qapp):
    mix.mix.add_files(mix.files[:2])
    ruler, player = mix.board.ruler, mix.mix.player
    def at(seconds, kind):
        x = (seconds - mix.canvas.t0) * mix.canvas.px_per_sec
        ev = QMouseEvent(kind, QPointF(x, 5), QPointF(x, 5), Qt.MouseButton.LeftButton,
                         Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier)
        {QEvent.Type.MouseButtonPress: ruler.mousePressEvent, QEvent.Type.MouseMove: ruler.mouseMoveEvent,
         QEvent.Type.MouseButtonRelease: ruler.mouseReleaseEvent}[kind](ev)
    at(1.0, QEvent.Type.MouseButtonPress)
    assert player.state == "playing" and mix.mix._scrubbing
    assert abs(player.position - SR) < SR // 10
    test_ui.pump(qapp, 80)
    at(2.0, QEvent.Type.MouseMove)
    assert wait_until(qapp, lambda: abs(player.position - 2 * SR) < SR // 5)
    at(2.0, QEvent.Type.MouseButtonRelease)
    assert player.state == "stopped" and not mix.mix._scrubbing


def test_clicking_the_ruler_while_playing_just_moves_the_playhead(mix, qapp):
    mix.mix.add_files(mix.files[:1])
    mix.mix.play(0)
    ruler = mix.board.ruler
    x = 1.5 * mix.canvas.px_per_sec
    ruler.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(x, 5), QPointF(x, 5), Qt.MouseButton.LeftButton,
                                      Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    assert not mix.mix._scrubbing
    ruler.mouseReleaseEvent(QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(x, 5), QPointF(x, 5), Qt.MouseButton.LeftButton,
                                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
    assert mix.mix.player.state == "playing"                             # releasing did not stop the real playback
    mix.mix.stop()


def test_space_escape_home_end_and_undo_follow_the_mix_in_this_mode(mix, qapp):
    mix.mix.add_files(mix.files[:1])
    mix.update_actions()
    assert mix.actions["play"].isEnabled() and mix.actions["undo"].isEnabled()
    mix.actions["play"].trigger()
    assert mix.mix.player.state == "playing" and mix.ctl.player.state == "stopped"
    mix._escape()
    assert mix.mix.player.state == "stopped"
    mix.actions["go_end"].trigger()
    assert mix.canvas.playhead == mix.mix.project.total_frames
    mix.actions["go_start"].trigger()
    assert mix.canvas.playhead == 0
    mix.actions["undo"].trigger()
    assert mix.mix.project.clips == [] and mix.actions["redo"].isEnabled()
    mix.actions["redo"].trigger()
    assert len(mix.mix.project.clips) == 1


def test_leaving_the_mode_stops_the_mix(mix, qapp):
    mix.mix.add_files(mix.files[:1])
    mix.mix.play()
    mix.set_mode("edit")
    assert mix.mix.player.state == "stopped"


# ------------------------------------------------------------------ building the file
def test_build_adds_the_mix_to_the_library_and_offers_to_save(mix, tmp_path):
    mix.mix.add_files(mix.files[:2])
    out = tmp_path / "out" / "mixed.flac"
    out.parent.mkdir()
    mix.script.dialog_hooks["ExportDialog"] = lambda d: (d.path.setText(str(out)), d.picker._pick_format("flac"), d.path.setText(str(out)))
    before = len(mix.state.items)
    mix.mix.build()
    assert len(mix.state.items) == before + 1
    item = mix.state.current
    assert item.name == "mixed" and item.path == out and out.exists()
    assert ffmpeg.probe(out).duration == pytest.approx(3.0, abs=0.1)


def test_build_without_saving_keeps_an_editable_document(mix):
    mix.mix.add_files(mix.files[:2])
    mix.mix.build(save=False)
    item = mix.state.current
    assert item.name == "Mix" and item.doc is not None and item.doc.frames == pytest.approx(3 * SR, abs=2000)
    mix.mix.build(save=False)
    assert mix.state.current.name == "Mix 2"


def test_build_mixes_the_lines_with_their_levels(mix):
    mix.mix.add_files(mix.files[:2])
    p = mix.mix.project
    p.set_line(p.lines[1].id, mute=True)
    mix.mix.build(save=False)
    only_first = mix.state.current.doc.read(0, 4000).copy()
    p.set_line(p.lines[1].id, mute=False)
    mix.mix.build(save=False)
    both = mix.state.current.doc.read(0, 4000)
    assert not np.allclose(only_first, both, atol=1e-3)


def test_a_mix_that_would_clip_offers_to_be_lowered(mix, monkeypatch):
    mix.mix.add_files(mix.files[:2])
    p = mix.mix.project
    for line in p.lines:
        p.set_line(line.id, volume_db=12.0)
    asked = []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: asked.append(a[1]) or QMessageBox.StandardButton.Yes))
    mix.mix.build(save=False)
    assert asked and "loud" in asked[0].lower()
    peak = max(float(np.abs(c).max()) for c in mix.state.current.doc.iter_chunks())
    assert peak <= 1.0


def test_an_empty_mix_cannot_be_built_or_played(mix):
    messages = []
    mix.mix.message.connect(messages.append)
    mix.mix.build()
    mix.mix.play()
    assert len(messages) == 2 and mix.mix.player.state == "stopped" and not mix.mix_page.build_btn.isEnabled()


# ------------------------------------------------------------------ projects
def test_a_project_is_saved_and_opened_again(mix, tmp_path, monkeypatch):
    mix.mix.add_files(mix.files)
    p = mix.mix.project
    p.set_line(p.lines[0].id, volume_db=-5.0, pan=0.4)
    p.set_clip(p.clips[1].id, fade_in=SR // 4, gain_db=2.0)
    target = tmp_path / "proj" / "my.twmix"
    target.parent.mkdir()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), "")))
    mix.mix.save_project()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert len(data["clips"]) == 3 and data["lines"][0]["pan"] == 0.4
    before = [(c.line, c.start, c.offset, c.length, c.gain_db, c.fade_in) for c in p.clips]
    mix.mix.new_project()
    assert mix.mix.project.empty and mix.mix.project is not p
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(target), "")))
    mix.mix.open_project()
    q = mix.mix.project
    assert [(c.line, c.start, c.offset, c.length, c.gain_db, c.fade_in) for c in q.clips] == before
    assert q.lines[0].volume_db == -5.0 and mix.board.project is q
    assert len(mix.board.headers.lines) == len(q.lines)


def test_a_project_with_unsaved_audio_explains_why_it_cannot_be_saved(mix, monkeypatch):
    from tonewright.core.document import Clip
    from tonewright.core.source import Source
    p = mix.mix.project
    line = p.add_line()
    p.add_clip(line.id, Clip.from_source(Source.from_array(np.zeros((1000, 2), np.float32), SR)), 0, "a recording")
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[2])))
    mix.mix.save_project()
    assert shown and "recording" in shown[0]


def test_a_missing_file_is_reported_when_a_project_is_opened(mix, tmp_path, monkeypatch):
    mix.mix.add_files(mix.files[:1])
    target = tmp_path / "m.twmix"
    mix.mix.project.save(target)
    mix.files[0].unlink()
    mix.mix._file_cache.clear()
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[2])))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(target), "")))
    keep = mix.mix.project
    mix.mix._dirty = False
    assert mix.mix.load_project(target) is False
    assert shown and mix.mix.project is keep


@pytest.mark.parametrize("answer, kept", [(QMessageBox.StandardButton.No, ["amplify"]),
                                           (QMessageBox.StandardButton.Yes, ["custom_filter", "amplify"])])
def test_a_project_with_a_custom_ffmpeg_filter_asks_before_running_it(mix, tmp_path, monkeypatch, answer, kept):
    mix.mix.add_files(mix.files[:1])
    target = tmp_path / "filter.twmix"
    mix.mix.project.save(target)
    data = json.loads(target.read_text(encoding="utf-8"))
    data["effects"] = [{"id": 90, "effect": "custom_filter", "params": {"chain": "volume=0.5"}, "start": 0, "end": SR // 2},
                       {"id": 91, "effect": "amplify", "params": fx.get("amplify").defaults(), "start": 0, "end": SR // 2}]
    target.write_text(json.dumps(data), encoding="utf-8")
    asked = []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: asked.append(a[2]) or answer))
    mix.mix._dirty = False
    assert mix.mix.load_project(target) is True
    assert len(asked) == 1 and "volume=0.5" in asked[0]                    # the user sees what would run
    assert [e.effect for e in mix.mix.project.effects] == kept


def test_closing_with_an_unsaved_mix_asks_first(mix):
    mix.mix.add_files(mix.files[:1])
    mix.script.box_buttons.append("Cancel")
    assert mix.mix.confirm_quit() is False
    mix.script.box_buttons.append("Quit")
    assert mix.mix.confirm_quit() is True
    mix.mix._dirty = False
    assert mix.mix.confirm_quit() is True


# ------------------------------------------------------------------ the rest of the window
def test_the_library_menu_sends_files_to_the_mix(mix):
    mix.ctl.open_paths(mix.files[:2])
    assert wait_until(test_ui.QApplication.instance(), lambda: len(mix.state.items) == 2)
    mix.set_mode("edit")
    mix.library.addToMixRequested.emit(list(mix.state.items))
    assert mix.state.mode == "multitrack" and len(mix.mix.project.clips) == 2


def test_theme_switch_and_language_do_not_break_the_page(mix, qapp):
    mix.mix.add_files(mix.files[:1])
    mix.toggle_theme()
    mix.toggle_theme()
    mix.mix_page.restyle()
    mix.canvas.repaint()
    assert mix.mix_page.grab().width() > 0


def test_the_timeline_paints_every_item_without_errors(mix, qapp):
    mix.mix.add_files(mix.files)
    p = mix.mix.project
    p.set_clip(p.clips[0].id, fade_in=SR // 2, fade_out=SR // 2, gain_db=3.0)
    p.set_line(p.lines[1].id, mute=True)
    p.add_effect("amplify", fx.get("amplify").defaults(), 0, SR)
    mix.board.refresh()
    mix.canvas.select("clip", p.clips[0].id)
    image = mix.board.grab()
    assert image.width() > 100 and image.height() > 100
    mix.canvas.set_zoom(40000)                                # zoomed to (almost) single samples
    mix.board.grab()
    mix.canvas.set_zoom(3)
    mix.board.grab()
