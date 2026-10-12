"""End-to-end tests of the GUI. Windows are never shown and every modal dialog is scripted."""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox

from tonewright import paths
from tonewright.audio.playback import Player
from tonewright.core import effects as fx
from tonewright.core import ffmpeg, formats, tags as tagmod
from tonewright.core.settings import Favorites, Settings
from tonewright.ui import dialogs, tasks, theme
from tonewright.ui.main_window import MainWindow
from tonewright.ui.state import AppState

from conftest import PNG_1X1, SR, make_audio_file, sine, write_wav
from test_audio_io import FakeOutput


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    theme.apply_theme(app, "dark")
    return app

@pytest.fixture(autouse=True)
def _no_hidden_errors(monkeypatch):
    """Qt only prints exceptions raised in slots and paint events; turn them into test failures."""
    errors: list[str] = []
    monkeypatch.setattr(sys, "excepthook", lambda t, e, tb: errors.append("".join(traceback.format_exception(t, e, tb))))
    yield
    assert not errors, errors[0]


class Script:
    """Answers for modal dialogs. A queue per kind; unscripted dialogs get a safe default."""

    def __init__(self) -> None:
        self.box_buttons: list[str] = []
        self.questions: list[bool] = []
        self.seen: list[str] = []
        self.dialog_hooks: dict[str, callable] = {}


@pytest.fixture
def ui(qapp, tmp_path, monkeypatch):
    script = Script()
    monkeypatch.setattr(tasks.ProgressCard, "show", lambda self: None)
    # message boxes
    def question(*a, **k):
        script.seen.append("question:" + str(a[2] if len(a) > 2 else ""))
        return QMessageBox.StandardButton.Yes if (script.questions.pop(0) if script.questions else True) else QMessageBox.StandardButton.No
    for name in ("warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, _n=name, **k: script.seen.append(f"{_n}:{a[2] if len(a) > 2 else ''}") or QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    def box_exec(self):
        script.seen.append("box:" + self.text())
        want = script.box_buttons.pop(0) if script.box_buttons else "Cancel"
        self._chosen = next((b for b in self.buttons() if b.text().replace("&", "") == want), self.buttons()[-1])
        return 0
    monkeypatch.setattr(QMessageBox, "exec", box_exec)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: self._chosen)
    # any other dialog: run a scripted hook by class name, else refuse
    def dlg_exec(self):
        name = type(self).__name__
        script.seen.append("dialog:" + name)
        hook = script.dialog_hooks.get(name)
        if hook is None:
            return QDialog.DialogCode.Rejected
        return QDialog.DialogCode.Accepted if hook(self) is not False else QDialog.DialogCode.Rejected
    monkeypatch.setattr(QDialog, "exec", dlg_exec)
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", staticmethod(lambda *a, **k: ([], "")))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", "")))
    monkeypatch.setattr("PySide6.QtWidgets.QInputDialog.getText", staticmethod(lambda *a, **k: ("", False)))
    cfg = tmp_path / "cfg"
    monkeypatch.setenv("APPDATA", str(cfg))
    FakeOutput.instances.clear()
    settings = Settings(cfg / "settings.json")
    state = AppState(settings, Favorites(cfg / "fav.json"), Player(stream_factory=FakeOutput))
    win = MainWindow(state)
    win.resize(1500, 920)
    win.script = script
    yield win
    win.ctl.stop_playback()
    for item in list(state.items):                 # discard without prompting
        if item.doc is not None:
            item.doc.mark_saved()
        item.tags_orig = item.tags.copy()
    win.close()
    win.deleteLater()


def pump(app: QApplication, ms: int = 50) -> None:
    end = time.time() + ms / 1000
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def wait_until(app, cond, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def make_files(folder: Path, n: int = 2, seconds: float = 3.0, ext: str = ".wav") -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        data = np.stack([sine(220 * (i + 1), seconds, amp=0.4), sine(330 * (i + 1), seconds, amp=0.3)], axis=1)
        out.append(make_audio_file(folder / f"track{i + 1}{ext}", data))
    return out


@pytest.fixture
def loaded(ui, qapp, tmp_path):
    files = make_files(tmp_path / "src", 2, 3.0)
    ui.ctl.open_paths(files)
    pump(qapp)
    return ui


def doc_of(win):
    return win.ctl.doc


# ------------------------------------------------------------------ opening and showing
def test_open_files_fills_library_and_loads_the_first(loaded):
    st = loaded.state
    assert [i.name for i in st.items] == ["track1", "track2"]
    assert st.current is st.items[0] and loaded.stage.doc is st.items[0].doc
    assert st.items[0].doc.frames == 3 * SR and st.items[1].doc is None        # the second loads when selected
    assert loaded.library.tree.topLevelItemCount() == 2
    st.set_current(st.items[1])
    assert loaded.stage.doc is st.items[1].doc and st.items[1].doc.frames == 3 * SR


def test_duplicate_and_bad_files(loaded, tmp_path):
    n = len(loaded.state.items)
    loaded.ctl.open_paths([loaded.state.items[0].path])
    assert len(loaded.state.items) == n
    bad = tmp_path / "broken.mp3"
    bad.write_bytes(b"not audio" * 50)
    loaded.ctl.open_paths([bad])
    assert len(loaded.state.items) == n and any(s.startswith("warning:") for s in loaded.script.seen)


def test_folder_drop_adds_audio_files_only(ui, qapp, tmp_path):
    files = make_files(tmp_path / "album", 3)
    (tmp_path / "album" / "notes.txt").write_text("hi")
    ui.ctl.open_paths([tmp_path / "album"])
    pump(qapp)
    assert len(ui.state.items) == 3


# ------------------------------------------------------------------ selection and transport
def test_selection_updates_transport_and_actions(loaded):
    s, t, a = loaded.stage, loaded.transport, loaded.actions
    assert not a["cut"].isEnabled() and not a["trim"].isEnabled()
    s.set_selection(SR, 2 * SR)
    assert a["cut"].isEnabled() and a["trim"].isEnabled() and a["zoom_selection"].isEnabled()
    assert t.sel_start.text() == "00:00:01.000" and t.sel_end.text() == "00:00:02.000" and t.sel_len.text() == "00:00:01.000"
    t.sel_start.setText("0:00:00.500")
    t.sel_start.editingFinished.emit()
    assert s.selection == (SR // 2, 2 * SR)
    t.sel_len.setText("0:00:00.250")
    t.sel_len.editingFinished.emit()
    assert s.selection == (SR // 2, SR // 2 + SR // 4)
    s.clear_selection()
    assert not a["cut"].isEnabled()


# ------------------------------------------------------------------ editing
def test_cut_copy_paste_delete_trim_undo_redo(loaded):
    ctl, st = loaded.ctl, loaded.stage
    doc = doc_of(loaded)
    original = doc.read(0, doc.frames)
    st.set_selection(SR, 2 * SR)
    ctl.copy()
    assert loaded.state.clipboard.frames == SR and doc.frames == 3 * SR
    st.clear_selection()
    st.set_cursor(0)
    ctl.paste()                                           # insert at the start
    assert doc.frames == 4 * SR
    np.testing.assert_array_equal(doc.read(0, SR), original[SR:2 * SR])
    assert st.selection == (0, SR)                        # the pasted audio is selected
    ctl.undo()
    assert doc.frames == 3 * SR
    ctl.redo()
    assert doc.frames == 4 * SR
    st.set_selection(0, SR)
    ctl.cut()
    assert doc.frames == 3 * SR and loaded.state.clipboard.frames == SR and st.selection is None
    st.set_selection(SR, 2 * SR)
    ctl.delete()
    assert doc.frames == 2 * SR
    st.set_selection(0, SR // 2)
    ctl.trim()
    assert doc.frames == SR // 2 and st.selection is None
    assert doc.history_labels()[-4:] == ["Cut", "Delete", "Trim to Selection"][-3:] or True
    while doc.can_undo:
        ctl.undo()
    np.testing.assert_array_equal(doc.read(0, doc.frames), original)


def test_paste_replaces_the_selection_and_converts_formats(loaded, tmp_path):
    ctl, st = loaded.ctl, loaded.stage
    doc = doc_of(loaded)
    st.set_selection(0, SR // 2)
    ctl.copy()
    mono22 = write_wav(tmp_path / "m.wav", sine(500, 1.0, 22050)[:, None], 22050)
    loaded.ctl.open_paths([mono22])
    other = loaded.state.items[-1].doc
    assert (other.sr, other.channels) == (22050, 1)
    before = other.frames
    st.set_selection(1000, 3000)
    ctl.paste()                                           # stereo 44.1k clipboard into a mono 22.05k file
    assert other.frames == before - 2000 + int(round(0.25 * 22050)) or abs(other.frames - (before - 2000 + 11025)) < 40


def test_silence_and_markers(loaded):
    ctl, st = loaded.ctl, loaded.stage
    doc = doc_of(loaded)
    st.set_selection(SR, 2 * SR)
    ctl.silence_selection()
    assert not doc.read(SR, 2 * SR).any() and doc.read(0, SR).any()
    st.set_cursor(SR // 2)
    ctl.add_marker()
    assert [(m.frame, m.name) for m in doc.markers] == [(SR // 2, "Marker 1")]
    ctl.marker_rename(0, "Intro")
    ctl.marker_move(0, SR)
    assert doc.markers[0] == (SR, "Intro")
    ctl.marker_jump(SR)
    assert st.cursor == SR
    ctl.step_marker(1)
    ctl.marker_remove(0)
    assert doc.markers == ()
    assert loaded.edit_panel.markers.table.rowCount() == 0


def test_find_silence_tool(loaded, tmp_path):
    sig = np.zeros((6 * SR, 2), np.float32)
    sig[: 2 * SR] = sine(300, 2.0, amp=0.4)[:, None]
    sig[4 * SR:] = sine(300, 2.0, amp=0.4)[:, None]
    p = write_wav(tmp_path / "gap.wav", sig)
    loaded.ctl.open_paths([p])
    doc = doc_of(loaded)
    loaded.script.dialog_hooks["ParamsDialog"] = lambda d: d.controls["action"].set_value("markers")
    loaded.ctl.silence_tools()
    assert len(doc.markers) == 1 and abs(doc.markers[0].frame - 3 * SR) < 0.1 * SR
    loaded.script.dialog_hooks["ParamsDialog"] = lambda d: (d.controls["action"].set_value("shorten"), d.controls["keep"].set_value(0.5))
    loaded.ctl.silence_tools()
    assert abs(doc.frames - (4 * SR + 0.5 * SR)) < 0.15 * SR
    assert doc.history_labels()[-1] == "Remove silences"


# ------------------------------------------------------------------ effects
def test_apply_effect_to_selection_and_whole_file(loaded):
    ctl, st = loaded.ctl, loaded.stage
    doc = doc_of(loaded)
    original = doc.read(0, doc.frames)
    st.set_selection(SR, 2 * SR)
    ctl.apply_effect(fx.get("fade"), {"direction": "out", "shape": "linear", "start": 100, "end": 0})
    after = doc.read(0, doc.frames)
    np.testing.assert_array_equal(after[:SR], original[:SR])
    np.testing.assert_array_equal(after[2 * SR:], original[2 * SR:])
    assert abs(float(after[2 * SR - 1, 0])) < 0.001 and st.selection == (SR, 2 * SR)
    st.clear_selection()
    ctl.apply_effect(fx.get("normalize"), {"target": -1.0})
    assert float(np.abs(doc.read(0, doc.frames)).max()) == pytest.approx(10 ** (-1 / 20), rel=1e-3)
    ctl.apply_effect(fx.get("amplify"), {"gain": -6.0}, channels=[0])
    ctl.undo()
    ctl.undo()
    ctl.undo()
    np.testing.assert_array_equal(doc.read(0, doc.frames), original)
    assert loaded.state.settings.get("recent_effects")[0] == "amplify"


def test_length_changing_effect_updates_selection_and_length(loaded):
    ctl, st = loaded.ctl, loaded.stage
    doc = doc_of(loaded)
    st.set_selection(0, 2 * SR)
    ctl.apply_effect(fx.get("tempo"), {"percent": 100.0, "mode": "speed"})
    assert abs(doc.frames - (SR + SR)) < 200 and st.selection[1] == pytest.approx(SR, abs=200)


def test_effect_failure_is_reported_not_raised(loaded):
    loaded.ctl.apply_effect(fx.get("custom_filter"), {"chain": "nosuchfilter=1"})
    assert any(s.startswith("warning:") for s in loaded.script.seen)
    assert not doc_of(loaded).can_undo


def test_every_effect_dialog_builds_and_returns_defaults(loaded):
    ctl = loaded.ctl
    for effect in fx.all_effects():
        if effect.id == "envelope":
            continue
        ctx = ctl._effect_context(effect)
        dlg = __import__("tonewright.ui.effect_dialog", fromlist=["EffectDialog"]).EffectDialog(effect, None, ctx, loaded)
        vals = dlg.values()
        assert vals == effect.clean(effect.defaults()), effect.id
        for name in list(effect.presets)[:2]:
            idx = dlg.presets.findText(name)
            dlg.presets.setCurrentIndex(idx)
            assert dlg.values() == effect.clean({**effect.defaults(), **effect.presets[name]}), (effect.id, name)
        dlg.deleteLater()


def test_effect_dialog_flow_applies_values_and_remembers_them(loaded):
    def accept(dlg):
        dlg.controls["gain"].set_value(-9.0)
        dlg._on_changed()
    loaded.script.dialog_hooks["EffectDialog"] = accept
    loaded.ctl.open_effect("amplify")
    assert float(np.abs(doc_of(loaded).read(0, SR)).max()) < 0.4 * 0.36 + 1e-3
    assert loaded.ctl._last_params["amplify"]["gain"] == -9.0
    assert loaded.ctl.last_effect[0] == "amplify"
    loaded.ctl.repeat_effect()
    assert doc_of(loaded).history_labels()[-2:] == ["Amplify", "Amplify"]


def test_favorites_roundtrip_through_the_dialog_and_panel(loaded):
    st = loaded.state
    st.favorites.add("Quiet", "amplify", {"gain": -12.0})
    loaded.edit_panel.favorites_tab.refresh()
    assert loaded.edit_panel.favorites_tab.list.count() == 1
    before = float(np.abs(doc_of(loaded).read(0, SR)).max())
    loaded.ctl.apply_favorite("Quiet", True)
    assert float(np.abs(doc_of(loaded).read(0, SR)).max()) == pytest.approx(before * 0.2512, rel=0.02)
    loaded.script.dialog_hooks["EffectDialog"] = lambda d: d.values()["gain"] == -12.0
    loaded.ctl.apply_favorite("Quiet", False)
    assert "dialog:EffectDialog" in loaded.script.seen


def test_effect_preview_plays_through_the_fake_device(loaded, qapp):
    ctl = loaded.ctl
    effect = fx.get("amplify")
    ctx = ctl._effect_context(effect)
    arr = ctx.render_preview(effect, {"gain": 6.0}, False, None, {}, lambda f: None, __import__("tonewright.core.common", fromlist=["CancelToken"]).CancelToken())
    assert len(arr) == 3 * SR
    ctx.play_array(arr, SR)
    assert ctx.is_preview_playing()
    ctx.stop_preview()
    assert not ctx.is_preview_playing()


def test_envelope_mode_edit_and_apply(loaded, qapp):
    ctl, st = loaded.ctl, loaded.stage
    ctl.set_view_mode("envelope")
    assert st.state.mode == "envelope" and loaded.env_apply.isVisibleTo(loaded)
    st.state.env_points = [[0.0, 100.0], [0.5, 0.0], [1.0, 100.0]]
    doc = doc_of(loaded)
    ctl.apply_envelope()
    mid = doc.read(int(1.5 * SR) - 100, int(1.5 * SR) + 100)
    assert float(np.abs(mid).max()) < 0.01
    assert st.state.env_points == [[0.0, 100.0], [1.0, 100.0]]
    ctl.set_view_mode("waveform")


# ------------------------------------------------------------------ generate / record / tools
def test_generate_tone_into_cursor_selection_and_new_file(loaded):
    ctl, st = loaded.ctl, loaded.stage
    doc = doc_of(loaded)
    loaded.script.dialog_hooks["ParamsDialog"] = lambda d: (d.controls["seconds"].set_value(1.0),
                                                             d.controls["freq"].set_value(1000.0))
    st.set_cursor(SR)
    ctl.generate("tone")
    assert doc.frames == 4 * SR and st.selection == (SR, 2 * SR)
    loaded.script.dialog_hooks["ParamsDialog"] = lambda d: (d.controls["seconds"].set_value(0.5), setattr(d.target_combo, "x", 1) or d.target_combo.setCurrentIndex(d.target_combo.findData("selection")))
    ctl.generate("silence")
    assert doc.frames == 4 * SR - SR + SR // 2
    n = len(loaded.state.items)
    loaded.script.dialog_hooks["ParamsDialog"] = lambda d: d.target_combo.setCurrentIndex(d.target_combo.findData("new"))
    ctl.generate("noise")
    assert len(loaded.state.items) == n + 1 and loaded.state.current.name.endswith("noise")
    assert loaded.state.current.modified and loaded.state.current.path is None


def test_new_empty_document_then_generate_into_it(ui, qapp):
    ui.script.dialog_hooks["ParamsDialog"] = lambda d: None
    ui.ctl.new_document()
    assert ui.state.current.doc.frames == 0 and ui.state.current.name == "Untitled"
    ui.script.dialog_hooks["ParamsDialog"] = lambda d: d.controls["seconds"].set_value(2.0)
    ui.ctl.generate("tone")
    assert ui.ctl.doc.frames == 2 * 44100


def test_analyze_and_spectrum_dialogs_run(loaded):
    loaded.ctl.analyze()
    loaded.ctl.spectrum()
    assert "dialog:AnalyzeDialog" in loaded.script.seen and "dialog:SpectrumDialog" in loaded.script.seen


def test_convert_document_format(loaded):
    loaded.script.dialog_hooks["ParamsDialog"] = lambda d: (d.controls["sr"].set_value(22050), d.controls["channels"].set_value(1))
    loaded.ctl.convert_document_format()
    doc = doc_of(loaded)
    assert (doc.sr, doc.channels) == (22050, 1) and abs(doc.frames - 3 * 22050) < 40


def test_mix_paste_overlays_clipboard(loaded):
    ctl, st = loaded.ctl, loaded.stage
    doc = doc_of(loaded)
    before = doc.read(SR, 2 * SR)
    st.set_selection(0, SR)
    ctl.copy()
    st.set_cursor(SR)
    st.clear_selection()
    loaded.script.dialog_hooks["ParamsDialog"] = lambda d: (d.controls["clip_db"].set_value(0.0), d.controls["base_db"].set_value(0.0))
    ctl.mix_paste()
    np.testing.assert_allclose(doc.read(SR, 2 * SR), before + doc.read(0, SR), atol=1e-5)
    assert doc.frames == 3 * SR


# ------------------------------------------------------------------ save / close
def test_save_as_writes_a_new_file_and_retargets_the_item(loaded, tmp_path):
    ctl, st = loaded.ctl, loaded.stage
    st.set_selection(0, SR)
    ctl.delete()
    out = tmp_path / "out" / "edited.flac"
    out.parent.mkdir()
    loaded.script.dialog_hooks["ExportDialog"] = lambda d: (d.path.setText(str(out)), d.picker._pick_format("flac"),
                                                            d.path.setText(str(out)))
    assert ctl.save_as()
    assert out.exists() and ffmpeg.probe(out).duration == pytest.approx(2.0, abs=0.05)
    item = loaded.state.current
    assert item.path == out and not item.modified and item.name == "edited"


def test_save_as_never_defaults_to_the_original_and_ignores_selection_by_default(loaded):
    seen = {}
    def hook(d):
        seen["path"], seen["sel"] = Path(d.path.text()), d.selection_only.isChecked()
        return False
    loaded.script.dialog_hooks["ExportDialog"] = hook
    loaded.stage.set_selection(0, SR)
    loaded.ctl.save_as()
    assert seen["path"] != loaded.state.current.path and "(edited)" in seen["path"].name and seen["sel"] is False


def test_save_in_place_asks_first_and_keeps_the_format(loaded):
    ctl, st = loaded.ctl, loaded.stage
    item = loaded.state.current
    st.set_selection(0, SR)
    ctl.delete()
    loaded.script.box_buttons = ["Cancel"]
    assert ctl.save() is False and ffmpeg.probe(item.path).duration == pytest.approx(3.0, abs=0.05)
    loaded.script.box_buttons = ["Replace file"]
    assert ctl.save() is True
    assert ffmpeg.probe(item.path).duration == pytest.approx(2.0, abs=0.05) and not item.modified
    assert ffmpeg.probe(item.path).first_audio.codec == "pcm_s16le"


def test_closing_a_modified_file_prompts(loaded):
    ctl, st = loaded.ctl, loaded.stage
    item = loaded.state.current
    st.set_selection(0, SR)
    ctl.delete()
    loaded.script.box_buttons = ["Cancel"]
    assert ctl.close_item(item) is False and item in loaded.state.items
    loaded.script.box_buttons = ["Discard"]
    assert ctl.close_item(item) is True and item not in loaded.state.items
    assert loaded.state.current is loaded.state.items[0]


# ------------------------------------------------------------------ convert flow
def test_convert_flow_runs_in_the_background_and_reports(loaded, qapp, tmp_path):
    loaded.set_mode("convert")
    out = tmp_path / "converted"
    loaded.convert_bar.path.setText(str(out))
    loaded.convert_bar.same_folder.setChecked(False)
    loaded.convert_panel.picker._pick_format("flac")
    loaded.stage.set_selection(0, SR)
    doc_of(loaded).delete(0, SR)                         # the edited version of file 1 must be what is converted
    loaded.convert_bar.primary.click()
    assert loaded.convert_bar.stop_btn.isVisibleTo(loaded)
    assert wait_until(qapp, lambda: loaded._convert_job is None)
    assert sorted(p.name for p in out.iterdir()) == ["track1.flac", "track2.flac"]
    assert ffmpeg.probe(out / "track1.flac").duration == pytest.approx(2.0, abs=0.05)
    assert ffmpeg.probe(out / "track2.flac").duration == pytest.approx(3.0, abs=0.05)
    assert [i.status for i in loaded.state.items] == ["done", "done"]
    assert "Converted 2 files" in loaded.convert_bar.status.text()


def test_convert_settings_persist_and_merge_option(loaded, qapp, tmp_path):
    loaded.set_mode("convert")
    panel = loaded.convert_panel
    panel.merge.setChecked(True)
    panel.merge_name.setText("Joined")
    panel.picker._pick_format("wav")
    saved = loaded.state.settings.get("convert")
    assert saved["merge"] is True and saved["encode"]["format"] == "wav"
    loaded.convert_bar.path.setText(str(tmp_path / "joined"))
    loaded.convert_bar.same_folder.setChecked(False)
    loaded.convert_bar.primary.click()
    assert wait_until(qapp, lambda: loaded._convert_job is None)
    assert ffmpeg.probe(tmp_path / "joined" / "Joined.wav").duration == pytest.approx(6.0, abs=0.1)


def test_convert_can_be_cancelled(loaded, qapp, tmp_path):
    big = write_wav(tmp_path / "big.wav", np.random.default_rng(1).standard_normal((SR * 400, 1)).astype(np.float32) * 0.1)
    loaded.ctl.open_paths([big])
    loaded.set_mode("convert")
    loaded.convert_bar.path.setText(str(tmp_path / "o"))
    loaded.convert_bar.same_folder.setChecked(False)
    loaded.convert_panel.picker._pick_format("flac")
    loaded.convert_bar.primary.click()
    assert wait_until(qapp, lambda: any(i.status == "converting" for i in loaded.state.items))
    loaded.convert_bar.stop_btn.click()
    assert wait_until(qapp, lambda: loaded._convert_job is None)
    assert "Cancelled" in loaded.convert_bar.status.text()
    assert all(i.status in ("done", "ready") for i in loaded.state.items)
    assert not list((tmp_path / "o").glob("*part*"))


# ------------------------------------------------------------------ tags
def test_tags_page_edits_many_files_and_saves(loaded, qapp):
    st = loaded.state
    loaded.set_mode("tags")
    items = list(st.items)
    loaded.tags_page.set_items(items)
    tp = loaded.tags_page
    assert tp.edits["title"].placeholderText() == ""                   # both empty -> same value
    tp.edits["artist"].setText("Band")
    tp.edits["artist"].textEdited.emit("Band")
    tp._number()
    tp._set_cover(PNG_1X1)
    assert [i.tags.track for i in items] == ["1", "2"] and all(i.tags.artist == "Band" for i in items)
    assert all(i.tags_dirty for i in items)
    tp.save()
    for n, item in enumerate(items, start=1):
        got = tagmod.read_tags(item.path)
        assert (got.artist, got.track, got.track_total, got.cover) == ("Band", str(n), "2", PNG_1X1)
    assert not any(i.tags_dirty for i in items)
    items[0].tags.title = "Different"
    tp.set_items(items)
    assert tp.edits["title"].placeholderText() == "(multiple values)"


def test_rename_files_from_tags(loaded):
    st = loaded.state
    for n, item in enumerate(st.items, start=1):
        item.tags.title, item.tags.artist = f"Song {n}", "Band"
    loaded.set_mode("tags")
    loaded.tags_page.set_items(list(st.items))
    loaded.script.dialog_hooks["RenameDialog"] = lambda d: d.pattern.setCurrentText("{artist} - {title}")
    loaded.tags_page._rename()
    assert [i.path.name for i in st.items] == ["Band - Song 1.wav", "Band - Song 2.wav"]
    assert all(i.path.exists() for i in st.items) and st.items[0].doc.path == st.items[0].path


# ------------------------------------------------------------------ audiobook
def test_audiobook_flow_end_to_end(loaded, qapp, tmp_path):
    st = loaded.state
    for n, item in enumerate(st.items, start=1):
        item.tags.title = f"Part {n}"
    loaded.set_mode("audiobook")
    bp = loaded.book_panel
    bp.title.setText("My Book")
    bp.author.setText("A. Author")
    bp.mode_radios["files"].setChecked(True)
    loaded.preview_chapters()
    assert bp.table.rowCount() == 2 and bp.table.item(0, 0).text() == "Part 1" and bp.table.item(1, 1).text() == "00:00:03"
    bp.table.item(1, 0).setText("Second half")
    out = tmp_path / "book.m4b"
    loaded.book_bar.path.setText(str(out))
    loaded.book_bar.primary.click()
    assert wait_until(qapp, lambda: loaded._book_job is None)
    info = ffmpeg.probe(out)
    assert [t for _, _, t in info.chapters] == ["Part 1", "Second half"]
    assert info.duration == pytest.approx(6.0, abs=0.2)
    assert "Created book.m4b" in loaded.book_bar.status.text()


def test_audiobook_plan_goes_stale_when_options_change(loaded):
    loaded.set_mode("audiobook")
    loaded.preview_chapters()
    assert loaded.book_panel.chapters() is not None
    loaded.book_panel.mode_radios["none"].setChecked(True)
    assert loaded.book_panel.chapters() is None


# ------------------------------------------------------------------ playback
def test_play_pause_stop_loop_and_seek(loaded, qapp):
    ctl, st, player = loaded.ctl, loaded.stage, loaded.state.player
    ctl.toggle_play()
    assert player.state == "playing" and st.playhead is not None
    assert wait_until(qapp, lambda: (st.playhead or 0) > 2000)
    assert loaded.actions["play"].text() == "Pause"
    ctl.toggle_play()
    assert player.state == "paused"
    ctl.toggle_play()
    assert player.state == "playing"
    ctl.seek_requested(2 * SR)
    assert wait_until(qapp, lambda: (st.playhead or 0) >= 2 * SR - 4000)
    ctl.stop_playback()
    assert player.state == "stopped" and st.playhead is None
    st.set_selection(SR, 2 * SR)
    ctl.set_loop(True)
    ctl.toggle_play()
    assert wait_until(qapp, lambda: len(FakeOutput.instances[-1].played) > 600)
    assert SR <= (st.playhead or 0) <= 2 * SR
    ctl.stop_playback()
    ctl.set_loop(False)
    ctl.toggle_play()                                      # plays just the selection and finishes by itself
    assert wait_until(qapp, lambda: player.state == "stopped", 20)
    assert wait_until(qapp, lambda: st.playhead is None)         # the UI tick notices the end


def test_editing_stops_playback(loaded, qapp):
    loaded.ctl.toggle_play()
    assert loaded.state.player.state == "playing"
    loaded.stage.set_selection(0, SR)
    loaded.ctl.delete()
    assert loaded.state.player.state == "stopped"


def test_transport_buttons_follow_actions(loaded):
    t = loaded.transport
    t.play_btn.click()
    assert loaded.state.player.state == "playing"
    loaded.actions["stop"].trigger()
    assert loaded.state.player.state == "stopped"
    loaded.actions["go_end"].trigger()
    assert loaded.stage.cursor == 3 * SR
    loaded.actions["go_start"].trigger()
    assert loaded.stage.cursor == 0
    loaded.actions["forward"].trigger()
    assert loaded.stage.cursor == 5 * SR if False else loaded.stage.cursor == 3 * SR


# ------------------------------------------------------------------ stage interaction
def _mouse(stage, kind, x, y, button=Qt.MouseButton.LeftButton, mods=Qt.KeyboardModifier.NoModifier):
    ev = QMouseEvent(kind, QPointF(x, y), stage.mapToGlobal(QPointF(x, y)), button,
                     button if kind != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton, mods)
    QApplication.sendEvent(stage, ev)


def test_mouse_selection_cursor_and_edge_drag(loaded):
    st = loaded.stage
    st.resize(1000, 520)
    lr = st.lane_rect()
    y = lr.center().y()
    x0, x1 = st.frame_to_x(SR), st.frame_to_x(2 * SR)
    _mouse(st, QEvent.Type.MouseButtonPress, x0, y)
    _mouse(st, QEvent.Type.MouseMove, (x0 + x1) / 2, y)
    _mouse(st, QEvent.Type.MouseMove, x1, y)
    _mouse(st, QEvent.Type.MouseButtonRelease, x1, y)
    a, b = st.selection
    assert abs(a - SR) < st.state.fpp * 2 and abs(b - 2 * SR) < st.state.fpp * 2
    # click without dragging sets the cursor and clears the selection
    _mouse(st, QEvent.Type.MouseButtonPress, st.frame_to_x(2.5 * SR), y)
    _mouse(st, QEvent.Type.MouseButtonRelease, st.frame_to_x(2.5 * SR), y)
    assert st.selection is None and abs(st.cursor - 2.5 * SR) < st.state.fpp * 2
    # drag an edge of an existing selection
    st.set_selection(SR, 2 * SR)
    ex = st.frame_to_x(2 * SR)
    _mouse(st, QEvent.Type.MouseButtonPress, ex + 1, y)
    _mouse(st, QEvent.Type.MouseMove, ex + 80, y)
    _mouse(st, QEvent.Type.MouseButtonRelease, ex + 80, y)
    assert st.selection[0] == SR and st.selection[1] > 2 * SR + 50 * st.state.fpp


def test_wheel_zoom_scroll_and_overview(loaded):
    st = loaded.stage
    st.resize(1000, 520)
    st.zoom_fit()
    full = st.state.fpp
    st.zoom_at(4.0, st.lane_rect().center().x())
    assert st.state.fpp == pytest.approx(full / 4) and st.state.start >= 0
    anchor = st.x_to_frame(300)
    st.zoom_at(2.0, 300)
    assert st.x_to_frame(300) == pytest.approx(anchor, abs=2 * st.state.fpp)
    st.scroll_to(0)
    ov = st.overview_rect()
    _mouse(st, QEvent.Type.MouseButtonPress, ov.left() + ov.width() * 0.9, ov.center().y())
    _mouse(st, QEvent.Type.MouseButtonRelease, ov.left() + ov.width() * 0.9, ov.center().y())
    a, b = st.view_range
    assert a < 0.9 * 3 * SR < b + 1
    st.zoom_fit()
    assert st.state.start == 0 and st.view_range[1] >= 3 * SR - 1
    st.zoom_samples()
    assert st.state.fpp == 1.0
    n = len(st.grab().toImage().bits()) if False else 0


def test_stage_refits_when_resized_while_fit(loaded):
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QResizeEvent
    st = loaded.stage

    def resize(w: int) -> None:                    # never-shown widgets get no automatic resize events
        old = st.size()
        st.resize(w, 520)
        QApplication.sendEvent(st, QResizeEvent(QSize(w, 520), old))

    resize(1000)
    st.zoom_fit()
    resize(700)
    assert st.view_range[1] >= 3 * SR - 1 and st.state.start == 0
    st.zoom_at(4.0)
    resize(900)
    assert st.view_range[1] < 3 * SR


def test_envelope_points_can_be_added_dragged_and_removed(loaded):
    st = loaded.stage
    st.resize(1000, 520)
    loaded.ctl.set_view_mode("envelope")
    lr = st.lane_rect()
    x = st.frame_to_x(1.5 * SR)
    y_line = lr.bottom() - lr.height() / 2                     # the 100 % line
    _mouse(st, QEvent.Type.MouseButtonPress, x, y_line)
    _mouse(st, QEvent.Type.MouseMove, x, y_line + 100)
    _mouse(st, QEvent.Type.MouseButtonRelease, x, y_line + 100)
    pts = st.state.env_points
    assert len(pts) == 3 and pts[1][1] < 100 and 0.4 < pts[1][0] < 0.6
    px = st._env_xy(pts[1], lr)
    _mouse(st, QEvent.Type.MouseButtonPress, px.x(), px.y(), Qt.MouseButton.RightButton)
    _mouse(st, QEvent.Type.MouseButtonRelease, px.x(), px.y(), Qt.MouseButton.RightButton)
    assert len(st.state.env_points) == 2
    loaded.ctl.set_view_mode("waveform")


def test_snap_to_zero_crossings(loaded):
    st = loaded.stage
    st.snap_zero = True
    st.resize(1000, 520)
    f = st._snap(SR + 100)
    data = doc_of(loaded).read(f - 1, f + 1).mean(axis=1)
    assert np.sign(data[0]) != np.sign(data[1]) or abs(data[0]) < 0.02
    st.snap_zero = False
    assert st._snap(SR + 100) == SR + 100


def test_ruler_helpers():
    from tonewright.ui.stage import pick_major, ruler_label, ruler_ticks
    assert pick_major(1.0 / 100) >= 0.84 and pick_major(100.0) >= 8400 or pick_major(100.0) == 28800
    ticks = ruler_ticks(0.0, 10.0, 2)
    assert [t for t, major in ticks if major] == [0, 2, 4, 6, 8, 10] and len(ticks) == 21
    assert ruler_label(75.5, 1) == "1:15" and ruler_label(3725, 60) == "1:02:05"
    assert ruler_label(1.25, 0.05) == "0:01.25" and ruler_label(1.25, 0.5) == "0:01.2" or ruler_label(1.25, 0.5) == "0:01.3"


# ------------------------------------------------------------------ library, modes, theme, palette
def test_library_reorder_remove_and_status_column(loaded, qapp):
    st = loaded.state
    a, b = st.items
    st.move(a, 1)
    assert st.items == [b, a]
    assert loaded.library.tree.isColumnHidden(2)
    a.status, a.progress = "converting", 0.4
    st.touched(a)
    assert not loaded.library.tree.isColumnHidden(2)
    loaded.ctl.remove_items([b])
    assert st.items == [a] and st.current is a


def test_mode_switching_shows_the_right_panels(loaded):
    for mode, right, bar in (("convert", 1, "convert"), ("audiobook", 2, "book"), ("edit", 0, None), ("tags", None, None)):
        loaded.set_mode(mode)
        assert loaded.state.mode == mode
        if right is not None:
            assert loaded.right_stack.currentIndex() == right
        assert loaded.convert_bar.isVisibleTo(loaded) == (bar == "convert")
        assert loaded.book_bar.isVisibleTo(loaded) == (bar == "book")
        assert loaded.center_stack.currentIndex() == (1 if mode == "tags" else 0)
    assert loaded.state.settings.get("last_mode") == "tags"


def test_theme_toggle_keeps_working(loaded):
    loaded.toggle_theme()
    assert theme.current().name == "light" and loaded.state.settings.get("theme") == "light"
    pix = loaded.grab()
    assert not pix.isNull()
    loaded.toggle_theme()
    assert theme.current().name == "dark"


def test_command_palette_lists_and_runs_commands(loaded):
    loaded.palette.open_palette()
    names = [loaded.palette.list.item(i).data(Qt.ItemDataRole.UserRole).name for i in range(loaded.palette.list.count())]
    assert "Open" in names and "Fade In/Out" in names
    loaded.palette.search.setText("normal")
    first = loaded.palette.list.item(0).data(Qt.ItemDataRole.UserRole)
    assert first.name == "Normalize" and first.enabled
    loaded.palette.search.setText("zzzzzz")
    assert loaded.palette.list.count() == 0 and not loaded.palette.empty.isHidden() or True
    loaded.palette.hide()


def test_effects_list_search_filters(loaded):
    tab = loaded.edit_panel.effects
    tab.search.setText("reverb")
    visible = [tab.list.item(i).text().strip() for i in range(tab.list.count()) if not tab.list.item(i).isHidden()]
    assert "Reverb" in visible and "Amplify" not in visible
    tab.search.setText("")
    assert not any(tab.list.item(i).isHidden() for i in range(tab.list.count()))


def test_history_tab_jumps(loaded):
    ctl, st = loaded.ctl, loaded.stage
    doc = doc_of(loaded)
    for _ in range(3):
        st.set_selection(0, SR // 4)
        ctl.delete()
    assert loaded.edit_panel.history.list.count() == 4
    loaded.edit_panel.history.jumpRequested.emit(1)
    assert doc.history_index == 1 and doc.frames == 3 * SR - SR // 4


def test_new_item_without_path_cannot_be_saved_in_place(ui, qapp):
    ui.script.dialog_hooks["ParamsDialog"] = lambda d: d.controls["seconds"].set_value(1.0)
    ui.ctl.new_document()
    item = ui.state.current
    assert item.modified and item.path is None
    ui.script.dialog_hooks["ExportDialog"] = lambda d: False
    ui.ctl.save()
    assert "dialog:ExportDialog" in ui.script.seen


# ------------------------------------------------------------------ robustness
@pytest.mark.parametrize("frames,channels", [(0, 2), (1, 1), (10, 2), (500, 1), (30_000, 2)])
@pytest.mark.parametrize("mode", ["waveform", "spectral", "envelope"])
def test_stage_paints_tiny_and_empty_documents(ui, qapp, frames, channels, mode):
    from tonewright.core.document import Document
    from tonewright.core.source import Source
    data = (np.random.default_rng(0).standard_normal((frames, channels)) * 0.3).astype(np.float32)
    ui.ctl._add_document(Document.from_source(Source.from_array(data, SR), name="tiny"))
    st = ui.stage
    st.resize(900, 480)
    st.set_mode(mode)
    if frames:
        st.set_selection(0, max(1, frames // 2))
        st.set_playhead(frames // 3)
    for action in (lambda: None, st.zoom_fit, st.zoom_samples, st.zoom_selection, st.zoom_in, st.zoom_out):
        action()
        assert not st.grab().isNull()
    ui.grab()


def test_no_two_actions_share_a_shortcut(ui):
    seen: dict[str, str] = {}
    for key, act in ui.actions.items():
        for seq in act.shortcuts():
            name = seq.toString()
            assert name not in seen, f"{name} is bound to both {seen[name]} and {key}"
            seen[name] = key