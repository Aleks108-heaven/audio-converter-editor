"""What the user sees when a save is refused: the real reason and, for Windows Security, what to allow. No window is shown."""
from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from tonewright import paths
from tonewright.core import access, audiobook, ffmpeg
from tonewright.core import tags as tagmod

from conftest import SR
from test_ui import _no_hidden_errors, loaded, make_files, pump, qapp, ui, wait_until  # noqa: F401 - fixtures

PROGRAMS = (r"C:\Program Files\Tonewright\python\pythonw.exe", r"C:\Program Files\Tonewright\app\bin\ffmpeg.exe")
BLOCKED = access.WriteProblem("ffmpeg-blocked", Path(r"C:\Users\someone\Videos"), PROGRAMS, "")


def test_the_explanation_names_the_folder_the_two_programs_and_the_page_to_open(loaded, monkeypatch):
    opened = []
    monkeypatch.setattr(os, "startfile", opened.append, raising=False)
    loaded.script.box_buttons = ["Open Windows Security"]
    loaded.ctl._show_write_problem("Save", BLOCKED)
    shown = loaded.script.seen[-1]
    assert shown.startswith("box:Windows Security blocked Tonewright from saving to") and "Videos" in shown
    assert opened == [access.CFA_LINK] and access.CFA_LINK.startswith("windowsdefender://")


def test_ok_closes_the_explanation_without_opening_anything(loaded, monkeypatch):
    opened = []
    monkeypatch.setattr(os, "startfile", opened.append, raising=False)
    loaded.script.box_buttons = ["OK"]
    loaded.ctl._show_write_problem("Save", BLOCKED)
    assert opened == []


def test_the_dialog_text_lists_what_to_allow(loaded, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    boxes = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: boxes.append(self) or 0)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: None)
    loaded.ctl._show_write_problem("Save", BLOCKED)
    (box,) = boxes
    info = box.informativeText()
    assert all(p in info for p in PROGRAMS) and "Controlled folder access" in info and "Ransomware protection" in info
    assert "Open Windows Security" in [b.text() for b in box.buttons()]


def test_the_other_kinds_of_problem_are_explained_without_the_windows_button(loaded, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    boxes = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: boxes.append(self) or 0)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: None)
    for problem in (access.WriteProblem("folder-missing", Path("/nowhere")),
                    access.WriteProblem("not-writable", Path("/locked"), reason="Access is denied"),
                    access.WriteProblem("unknown", Path("/odd"), reason="something odd")):
        loaded.ctl._show_write_problem("Save", problem)
    assert len(boxes) == 3
    assert all("Open Windows Security" not in [b.text() for b in box.buttons()] for box in boxes)
    assert "does not exist" in boxes[0].text() and "denied" in boxes[1].informativeText() and "odd" in boxes[2].informativeText()


def test_a_refused_save_shows_the_explanation_and_keeps_the_document_unsaved(loaded, monkeypatch, tmp_path):
    item = loaded.state.current
    item.doc.delete(0, SR)
    assert item.doc.modified

    def refused(*a, **k):
        raise ffmpeg.OutputError("Could not write x.wav", "Error opening output x: No such file or directory", tmp_path / "x.wav")
    monkeypatch.setattr("tonewright.ui.controller.convert_item", refused)
    monkeypatch.setattr(access, "diagnose", lambda path, detail="": BLOCKED)
    loaded.script.box_buttons = ["Replace file", "OK"]
    assert loaded.ctl.save() is False
    assert any(s.startswith("box:Windows Security blocked") for s in loaded.script.seen)
    assert item.doc.modified and not any(s.startswith("warning:") for s in loaded.script.seen)


def test_an_ordinary_ffmpeg_error_still_shows_ffmpegs_own_text(loaded, monkeypatch, tmp_path):
    item = loaded.state.current
    item.doc.delete(0, SR)

    def broken(*a, **k):
        raise ffmpeg.FFmpegError("Could not write x.wav", "Unknown encoder 'nope'")
    monkeypatch.setattr("tonewright.ui.controller.convert_item", broken)
    loaded.script.box_buttons = ["Replace file"]
    assert loaded.ctl.save() is False
    assert any("Unknown encoder" in s for s in loaded.script.seen if s.startswith("warning:"))
    assert not any(s.startswith("box:Windows Security") for s in loaded.script.seen)

# ------------------------------------------------------------------ saving tags
def _tag_every_file(loaded):
    loaded.set_mode("tags")
    items = list(loaded.state.items)
    loaded.tags_page.set_items(items)
    loaded.tags_page.edits["artist"].setText("Band")
    loaded.tags_page.edits["artist"].textEdited.emit("Band")
    assert all(i.tags_dirty for i in items)
    return items, loaded.tags_page


def test_tags_refused_by_windows_security_say_so_instead_of_bad_file_descriptor(loaded, monkeypatch):
    """The bug from the laptop: the Tags page printed "[Errno 9] Bad file descriptor" and nothing else."""
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(access, "controlled_folder_access", lambda: True)
    opened = []
    monkeypatch.setattr(os, "startfile", opened.append, raising=False)

    def refuse(*a, **k):
        raise OSError(errno.EBADF, "Bad file descriptor")
    monkeypatch.setattr(tagmod, "write_tags", refuse)
    items, page = _tag_every_file(loaded)
    loaded.script.box_buttons = ["Open Windows Security"]
    page.save()
    status = page.status.text()
    assert "Saved tags to 0 files." in status and "Windows Security" in status
    assert "Errno" not in status and "descriptor" not in status
    assert status.count("Windows Security (Controlled folder access)") == 1              # once, not once per file
    assert any(s.startswith("box:Windows Security blocked Tonewright") for s in loaded.script.seen)
    assert opened == [access.CFA_LINK]
    assert all(i.tags_dirty for i in items) and page.status.property("role") == "bad"   # nothing was written


def test_only_the_files_that_were_written_count_as_saved(loaded, monkeypatch):
    monkeypatch.setattr(access, "controlled_folder_access", lambda: False)
    items, page = _tag_every_file(loaded)
    first, second = items
    real = tagmod.write_tags

    def refuse_the_first(path, *a, **k):
        if Path(path) == first.path:
            raise PermissionError(errno.EACCES, "Access is denied")
        return real(path, *a, **k)
    monkeypatch.setattr(tagmod, "write_tags", refuse_the_first)
    page.save()
    assert first.tags_dirty and not second.tags_dirty
    assert tagmod.read_tags(second.path).artist == "Band" and tagmod.read_tags(first.path).artist == ""
    status = page.status.text()
    assert "Saved tags to 1 file." in status and "track1" in status and "Access is denied" in status
    assert not any(s.startswith("box:") for s in loaded.script.seen)                   # not Windows Security: no dialog


# ------------------------------------------------------------------ converting and making an audiobook
def _blocked(monkeypatch):
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(access, "diagnose", lambda path, detail="": BLOCKED)


def _convert_to(loaded, tmp_path, fmt="flac"):
    loaded.set_mode("convert")
    loaded.convert_bar.path.setText(str(tmp_path / "converted"))
    loaded.convert_bar.same_folder.setChecked(False)
    loaded.convert_panel.picker._pick_format(fmt)
    loaded.convert_bar.primary.click()


def test_a_conversion_that_wrote_nothing_ends_in_an_empty_bar_and_the_explanation(loaded, qapp, tmp_path, monkeypatch):
    _blocked(monkeypatch)

    def refuse(item, out, *a, **k):
        raise ffmpeg.OutputError("Could not convert", "Error opening output x: No such file or directory", out)
    monkeypatch.setattr("tonewright.core.convert.convert_item", refuse)
    loaded.script.box_buttons = ["OK"]
    _convert_to(loaded, tmp_path)
    assert wait_until(qapp, lambda: loaded._convert_job is None)
    pump(qapp, 150)
    bar = loaded.convert_bar
    assert bar.bar.value() == 0 and "100" not in bar.percent.text() and "Completed" not in bar.percent.text()
    assert "failed" in bar.status.text() and bar.status.property("role") == "bad"
    assert [s for s in loaded.script.seen if s.startswith("box:Windows Security blocked")] == [
        "box:Windows Security blocked Tonewright from saving to " + str(BLOCKED.folder) + "."]          # once, for two files


def test_a_conversion_with_untagged_files_says_so_and_does_not_claim_a_clean_finish(loaded, qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(access, "controlled_folder_access", lambda: True)

    def refuse(*a, **k):
        raise OSError(errno.EBADF, "Bad file descriptor")
    monkeypatch.setattr(tagmod, "write_tags", refuse)
    for item in loaded.state.items:
        item.tags.title = "Song"
    _convert_to(loaded, tmp_path)
    assert wait_until(qapp, lambda: loaded._convert_job is None)
    bar = loaded.convert_bar
    assert sorted(p.name for p in (tmp_path / "converted").iterdir()) == ["track1.flac", "track2.flac"]
    assert "tags not written in 2" in bar.status.text() and "Windows Security" in bar.status.text()
    assert bar.status.property("role") == "warn" and "problems" in bar.percent.text() and bar.bar.value() == 1000
    assert not any(s.startswith("box:") for s in loaded.script.seen)       # the files are fine: no alarming dialog


def test_a_clean_conversion_still_ends_at_100_percent(loaded, qapp, tmp_path):
    _convert_to(loaded, tmp_path)
    assert wait_until(qapp, lambda: loaded._convert_job is None)
    assert loaded.convert_bar.bar.value() == 1000 and "100" in loaded.convert_bar.percent.text()
    assert loaded.convert_bar.status.property("role") == "good"


def test_an_audiobook_refused_by_windows_security_explains_what_to_allow(loaded, qapp, tmp_path, monkeypatch):
    _blocked(monkeypatch)

    def refuse(parts, opts, path, **k):
        raise ffmpeg.OutputError("Could not save book.m4b", "Access is denied", path, cause=OSError(errno.EACCES, "Access is denied"))
    monkeypatch.setattr(audiobook, "create_audiobook", refuse)
    loaded.set_mode("audiobook")
    loaded.book_bar.path.setText(str(tmp_path / "book.m4b"))
    loaded.script.box_buttons = ["OK"]
    loaded.book_bar.primary.click()
    assert wait_until(qapp, lambda: loaded._book_job is None)
    pump(qapp, 150)
    assert "Windows Security" in loaded.book_bar.status.text() and loaded.book_bar.bar.value() == 0
    assert any(s.startswith("box:Windows Security blocked") for s in loaded.script.seen)


# ------------------------------------------------------------------ saving an edited file whose tags cannot be written
def test_a_save_whose_tags_were_refused_says_so_after_saving_the_audio(loaded, qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(access, "controlled_folder_access", lambda: True)
    loaded.ctl.open_paths(make_files(tmp_path / "flac", 1, 1.0, ".flac"))              # a WAV's tags go through FFmpeg, a FLAC's do not
    pump(qapp)
    item = next(i for i in loaded.state.items if i.path.suffix == ".flac")
    loaded.state.set_current(item)
    item.tags.title = "Song"
    item.doc.delete(0, 22050)

    def refuse(*a, **k):
        raise OSError(errno.EBADF, "Bad file descriptor")
    monkeypatch.setattr(tagmod, "write_tags", refuse)
    loaded.script.box_buttons = ["Replace file"]
    messages = []
    loaded.ctl.statusMessage.connect(messages.append)
    assert loaded.ctl.save(item) is True and not item.doc.modified                     # the audio was saved
    assert messages and "tags were not written" in messages[-1] and "Windows Security" in messages[-1]


