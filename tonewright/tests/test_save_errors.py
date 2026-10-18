"""What the user sees when a save is refused: the real reason and, for Windows Security, what to allow. No window is shown."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tonewright.core import access, ffmpeg

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
