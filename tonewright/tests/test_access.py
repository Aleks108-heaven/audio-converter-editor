"""Saying why a file could not be written: Windows Security's Controlled folder access, a missing folder, no permission."""
from __future__ import annotations

import errno
import os
import stat
import subprocess
from pathlib import Path

import numpy as np
import pytest

from tonewright import i18n, paths
from tonewright.core import access, ffmpeg
from tonewright.core import tags as tagmod
from tonewright.core.convert import ConvertItem, ConvertOptions, convert_all
from tonewright.core.formats import EncodeSettings

from conftest import SR, sine, write_wav

SCREENSHOT = ("[out#0/mp3 @ 0000020e82d6c600] Error opening output C:\\Users\\odyn2\\Videos\\concert.tonewright-part.mp3: "
              "No such file or directory\nError opening output file C:\\Users\\odyn2\\Videos\\concert.tonewright-part.mp3.\n"
              "Error opening output files: No such file or directory")


@pytest.fixture(autouse=True)
def _forget_the_cache(monkeypatch):
    monkeypatch.setattr(access, "_cfa_cache", (0.0, None))
    i18n.set_language("en")
    yield


def test_the_message_ffmpeg_gave_in_the_bug_report_is_recognised():
    assert ffmpeg.opening_output_failed(SCREENSHOT)
    assert ffmpeg.opening_output_failed("av_interleaved_write_frame(): Permission denied")
    assert not ffmpeg.opening_output_failed("Unknown encoder 'no_such_codec'")
    assert not ffmpeg.opening_output_failed("")


def test_writing_into_a_missing_folder_is_an_output_error_that_names_the_path(tmp_path):
    target = tmp_path / "gone" / "x.mp3"
    with pytest.raises(ffmpeg.OutputError) as caught:
        ffmpeg.encode_pcm([np.zeros((SR, 2), np.float32)], SR, 2, target, ["-c:a", "libmp3lame"])
    assert caught.value.path == target and isinstance(caught.value, ffmpeg.FFmpegError)
    assert not list(tmp_path.rglob("*part*"))


def test_other_ffmpeg_failures_stay_plain_ffmpeg_errors(tmp_path):
    with pytest.raises(ffmpeg.FFmpegError) as caught:
        ffmpeg.encode_pcm([np.zeros((SR, 2), np.float32)], SR, 2, tmp_path / "x.mp3", ["-c:a", "no_such_codec"])
    assert not isinstance(caught.value, ffmpeg.OutputError)


def test_a_failed_rename_is_an_output_error_too(tmp_path, monkeypatch):
    def refuse(src, dst):
        raise PermissionError(13, "Access is denied")
    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(ffmpeg.OutputError) as caught:
        ffmpeg.encode_pcm([np.zeros((SR, 2), np.float32)], SR, 2, tmp_path / "x.wav", ["-c:a", "pcm_s16le"])
    assert "denied" in caught.value.detail.lower() and not list(tmp_path.glob("*part*"))


def test_a_missing_folder_is_diagnosed_as_such(tmp_path):
    problem = access.diagnose(tmp_path / "gone" / "x.mp3", SCREENSHOT)
    assert problem.kind == "folder-missing" and problem.folder == tmp_path / "gone"
    assert "does not exist" in access.one_line(problem)


def test_ffmpeg_blocked_by_controlled_folder_access_is_recognised(tmp_path, monkeypatch):
    """This is what happened: Tonewright itself could write to Videos, FFmpeg could not."""
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(access, "controlled_folder_access", lambda: True)
    problem = access.diagnose(tmp_path / "x.mp3", SCREENSHOT)
    assert problem.kind == "ffmpeg-blocked" and problem.folder == tmp_path
    assert any(p.lower().endswith("ffmpeg.exe") or p.lower().endswith("ffmpeg") for p in problem.programs)
    assert len(problem.programs) == 2 and problem.programs[0] != problem.programs[1]
    line = access.one_line(problem)
    assert "Windows Security" in line and tmp_path.name in line
    assert not list(tmp_path.glob(".tonewright-write-test*"))                 # the probe file is always removed


def test_the_same_error_with_protection_off_is_not_blamed_on_windows_security(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(access, "controlled_folder_access", lambda: False)
    assert access.diagnose(tmp_path / "x.mp3", SCREENSHOT).kind == "unknown"
    monkeypatch.setattr(access, "controlled_folder_access", lambda: None)       # could not be told: still offer the hint
    assert access.diagnose(tmp_path / "x.mp3", SCREENSHOT).kind == "ffmpeg-blocked"


def test_a_folder_this_program_cannot_write_to(tmp_path, monkeypatch):
    monkeypatch.setattr(access, "_can_write_here", lambda folder: (False, "Access is denied"))
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(access, "controlled_folder_access", lambda: True)
    assert access.diagnose(tmp_path / "x.mp3", "").kind == "windows-security"
    monkeypatch.setattr(access, "controlled_folder_access", lambda: False)
    problem = access.diagnose(tmp_path / "x.mp3", "")
    assert problem.kind == "not-writable" and problem.reason == "Access is denied" and problem.programs == ()
    assert "not allowed to write" in access.one_line(problem) and "denied" in access.one_line(problem)


def test_nothing_is_blamed_on_windows_on_other_systems(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "IS_WINDOWS", False)
    assert access.controlled_folder_access() is False
    assert access.diagnose(tmp_path / "x.mp3", SCREENSHOT).kind == "unknown"


@pytest.mark.parametrize("output,expected", [("1\r\n", True), ("3\r\n", True), ("0\r\n", False), ("2\r\n", False),
                                             ("4\r\n", False)])
def test_controlled_folder_access_reads_the_defender_setting(monkeypatch, output, expected):
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=output, stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert access.controlled_folder_access() is expected
    assert access.controlled_folder_access() is expected and len(calls) == 1      # remembered for a while
    assert "Get-MpPreference" in calls[0][-1] and os.path.isabs(calls[0][0]) == os.path.isabs(paths.powershell_exe())


@pytest.mark.parametrize("failure", [OSError("no powershell"), subprocess.TimeoutExpired("powershell", 10)])
def test_controlled_folder_access_is_unknown_when_defender_cannot_be_asked(monkeypatch, failure):
    monkeypatch.setattr(paths, "IS_WINDOWS", True)

    def broken(cmd, **kw):
        raise failure
    monkeypatch.setattr(subprocess, "run", broken)
    assert access.controlled_folder_access() is None
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="not a number", stderr=""))
    monkeypatch.setattr(access, "_cfa_cache", (0.0, None))
    assert access.controlled_folder_access() is None


def test_a_batch_conversion_reports_the_reason_in_the_row(tmp_path, monkeypatch):
    src = write_wav(tmp_path / "a.wav", sine(220, 0.5)[:, None])
    item = ConvertItem(path=src, name="a", duration=0.5, sr=SR, channels=1)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    opts = ConvertOptions(settings=EncodeSettings(format="mp3"), out_dir=out_dir)
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(access, "controlled_folder_access", lambda: True)
    real = ffmpeg.convert_file

    def blocked(in_path, out_path, *a, **kw):
        raise ffmpeg.OutputError("Could not convert a.wav", SCREENSHOT, out_path)
    monkeypatch.setattr(ffmpeg, "convert_file", blocked)
    (result,) = convert_all([item], opts)
    assert result.status == "error" and "Windows Security" in result.message and "out" in result.message
    monkeypatch.setattr(ffmpeg, "convert_file", real)


# ------------------------------------------------------------------ changing a file that already exists (its tags)
def _existing_file(tmp_path, name="x.m4a"):
    path = tmp_path / name
    path.write_bytes(b"audio")
    return path


def test_tags_refused_by_windows_security_are_recognised_from_the_error_number(tmp_path, monkeypatch):
    """The laptop bug: the tag writer was told only "[Errno 9] Bad file descriptor", while the test file could be made."""
    target = _existing_file(tmp_path)
    refused = OSError(errno.EBADF, "Bad file descriptor")
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(access, "controlled_folder_access", lambda: True)
    problem = access.diagnose_modify(target, refused)
    assert problem.kind == "windows-security" and problem.folder == tmp_path and len(problem.programs) == 2
    assert problem.reason == "Bad file descriptor" and problem.kind in access.BLOCKED_KINDS
    line = access.one_line(problem, target.name)
    assert "Windows Security" in line and "Errno" not in line
    monkeypatch.setattr(access, "controlled_folder_access", lambda: None)         # could not be asked: still offer the hint
    assert access.diagnose_modify(target, refused).kind == "windows-security"
    monkeypatch.setattr(access, "controlled_folder_access", lambda: False)        # protection is off: not the cause
    assert access.diagnose_modify(target, refused).kind == "unknown"


@pytest.mark.parametrize("number", [errno.EBADF, errno.EACCES, errno.EPERM])
def test_every_way_windows_security_can_refuse_a_write_is_recognised(tmp_path, monkeypatch, number):
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(access, "controlled_folder_access", lambda: True)
    assert access.diagnose_modify(_existing_file(tmp_path), OSError(number, "refused")).kind == "windows-security"


def test_other_errors_are_not_blamed_on_windows_security(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(access, "controlled_folder_access", lambda: True)
    problem = access.diagnose_modify(_existing_file(tmp_path), OSError(errno.ENOSPC, "No space left on device"))
    assert problem.kind == "unknown" and problem.reason == "No space left on device"
    monkeypatch.setattr(paths, "IS_WINDOWS", False)                                # and never on a Mac or Linux
    assert access.diagnose_modify(_existing_file(tmp_path), OSError(errno.EBADF, "Bad file descriptor")).kind == "unknown"


def test_a_read_only_file_is_reported_as_such(tmp_path, monkeypatch):
    monkeypatch.setattr(access, "controlled_folder_access", lambda: False)
    target = _existing_file(tmp_path)
    os.chmod(target, stat.S_IREAD)
    try:
        problem = access.diagnose_modify(target, PermissionError(errno.EACCES, "Permission denied"))
    finally:
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
    assert problem.kind == "not-writable" and problem.reason == "Permission denied"


def test_an_output_error_that_carries_the_system_error_is_read_by_its_number(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(access, "controlled_folder_access", lambda: True)
    cause = OSError(errno.EBADF, "Bad file descriptor")
    carried = ffmpeg.OutputError("Could not write the tags of x.m4b", "Bad file descriptor", tmp_path / "x.m4b", cause=cause)
    assert access.diagnose_output_error(carried).kind == "windows-security"
    plain = ffmpeg.OutputError("Could not convert", SCREENSHOT, tmp_path / "x.mp3")      # FFmpeg's own words: as before
    assert access.diagnose_output_error(plain).kind == "ffmpeg-blocked"


# ------------------------------------------------------------------ what a conversion reports back
def _blocked_batch(tmp_path, monkeypatch, *, fmt="mp3", tags=None):
    src = write_wav(tmp_path / "a.wav", sine(220, 0.5)[:, None])
    item = ConvertItem(path=src, name="a", duration=0.5, sr=SR, channels=1, tags=tags or tagmod.TagSet())
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(access, "controlled_folder_access", lambda: True)
    return item, ConvertOptions(settings=EncodeSettings(format=fmt), out_dir=out_dir)


def test_a_refused_file_hands_the_problem_back_not_just_a_sentence(tmp_path, monkeypatch):
    item, opts = _blocked_batch(tmp_path, monkeypatch)

    def blocked(in_path, out_path, *a, **kw):
        raise ffmpeg.OutputError("Could not convert a.wav", SCREENSHOT, out_path)
    monkeypatch.setattr(ffmpeg, "convert_file", blocked)
    (result,) = convert_all([item], opts)
    assert result.status == "error" and result.problem is not None and result.problem.kind in access.BLOCKED_KINDS
    assert len(result.problem.programs) == 2                                 # what the screen needs to say what to allow


def test_tags_that_cannot_be_written_come_back_as_a_warning_not_as_silence(tmp_path, monkeypatch):
    item, opts = _blocked_batch(tmp_path, monkeypatch, tags=tagmod.TagSet(title="Song", artist="Band"))

    def refuse(*a, **kw):
        raise OSError(errno.EBADF, "Bad file descriptor")
    monkeypatch.setattr(tagmod, "write_tags", refuse)
    (result,) = convert_all([item], opts)
    assert result.status == "done" and result.output.is_file()               # the audio is fine
    assert "Windows Security" in result.warning and "Errno" not in result.warning


def test_a_conversion_whose_tags_were_written_has_no_warning(tmp_path, monkeypatch):
    item, opts = _blocked_batch(tmp_path, monkeypatch, tags=tagmod.TagSet(title="Song"))
    (result,) = convert_all([item], opts)
    assert result.status == "done" and result.warning == "" and tagmod.read_tags(result.output).title == "Song"
