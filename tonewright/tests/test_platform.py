"""The parts of Tonewright that differ between Windows and macOS. They are exercised on whatever system runs the
tests by faking the platform where that is possible; the lines that need a real Mac are skipped elsewhere."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtGui import QFileOpenEvent

from tonewright import i18n, paths
from tonewright.app import FileOpenRelay
from tonewright.core import ffmpeg, generate
from tonewright.ui import theme

import test_ui
from test_ui import qapp  # noqa: F401 - fixture

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file locks")


# ------------------------------------------------------------------ where things live
def test_settings_live_in_application_support_on_a_mac(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "IS_MAC", True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    folder = paths.config_dir()
    assert folder == tmp_path / "Library" / "Application Support" / "Tonewright" and folder.is_dir()


def test_settings_follow_appdata_elsewhere(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "IS_MAC", False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert paths.config_dir() == tmp_path / "Tonewright"


@pytest.mark.parametrize("mac, windows, expected", [(True, False, ["/usr/bin/open", "-R"]), (False, True, None)])
def test_reveal_in_file_manager_uses_the_system_program(monkeypatch, mac, windows, expected):
    calls = []
    monkeypatch.setattr(paths, "IS_MAC", mac)
    monkeypatch.setattr(paths, "IS_WINDOWS", windows)
    monkeypatch.setattr(paths.subprocess, "Popen", lambda cmd, *a, **k: calls.append(cmd))
    paths.reveal_in_file_manager(Path("song.mp3"))
    assert len(calls) == 1
    if expected:
        assert calls[0][:2] == expected and calls[0][2] == "song.mp3"
    else:
        assert calls[0][1] == "/select," and Path(calls[0][0]).name.lower() == "explorer.exe" or calls[0][0] == "explorer.exe"


def test_open_folder_on_a_mac_uses_open(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(paths, "IS_MAC", True)
    monkeypatch.setattr(paths, "IS_WINDOWS", False)
    monkeypatch.setattr(paths.subprocess, "Popen", lambda cmd, *a, **k: calls.append(cmd))
    paths.open_folder(tmp_path)
    assert calls == [["/usr/bin/open", str(tmp_path)]]


@posix_only
def test_a_running_session_is_recognised_by_its_lock_and_a_dead_one_is_not(tmp_path):
    import fcntl
    lock = tmp_path / ".lock"
    assert paths._session_is_running(lock) is False            # no lock file at all
    with open(lock, "wb") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert paths._session_is_running(lock) is True         # another open file holds it
    assert paths._session_is_running(lock) is False            # released: left behind by a crash


@posix_only
def test_stale_sessions_are_removed_but_a_live_one_is_kept(tmp_path, monkeypatch):
    import fcntl
    monkeypatch.setattr(paths, "_scratch_root", lambda: tmp_path)
    live, dead = tmp_path / "s_live", tmp_path / "s_dead"
    for d in (live, dead):
        d.mkdir()
        (d / ".lock").write_bytes(b"")
        (d / "audio.f32").write_bytes(b"x")
    with open(live / ".lock", "rb") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert paths.cleanup_stale_sessions() == 1
    assert live.exists() and not dead.exists()


# ------------------------------------------------------------------ FFmpeg
def test_homebrew_and_the_app_bundle_are_searched_on_a_mac(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    found = ffmpeg._candidate_paths("ffmpeg")
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    assert str(tmp_path / "bin" / exe) in found                 # inside a packaged app
    if sys.platform == "darwin":
        assert "/opt/homebrew/bin/ffmpeg" in found and "/usr/local/bin/ffmpeg" in found


@pytest.mark.parametrize("platform, hint", [("win32", "winget install Gyan.FFmpeg"), ("darwin", "brew install ffmpeg")])
def test_the_install_hint_matches_the_system(monkeypatch, platform, hint):
    monkeypatch.setattr(sys, "platform", platform)
    assert ffmpeg.install_hint() == hint


def test_the_missing_ffmpeg_message_names_the_right_command(monkeypatch):
    monkeypatch.setattr(ffmpeg, "_candidate_paths", lambda name: [])
    monkeypatch.setattr(ffmpeg, "_found", {})
    with pytest.raises(ffmpeg.FFmpegMissing) as err:
        ffmpeg.find_tool("ffmpeg")
    assert ffmpeg.install_hint() in str(err.value)


# ------------------------------------------------------------------ formats the installed FFmpeg can write
def test_every_format_is_offered_when_ffmpeg_has_all_encoders():
    from tonewright.core import formats
    assert formats.available_keys() == list(formats.FORMATS)             # the full builds used on Windows


def test_formats_without_an_encoder_are_hidden(monkeypatch):
    from tonewright.core import formats
    everything = {spec.codec for spec in formats.FORMATS.values()} - {"libopencore_amrnb", "libvo_amrwbenc"}
    monkeypatch.setattr(ffmpeg, "_caps", {"encoders": everything})
    keys = formats.available_keys()
    assert "amr" not in keys and "amrwb" not in keys and "mp3" in keys and "flac" in keys


def test_all_formats_are_offered_when_the_encoder_list_cannot_be_read(monkeypatch):
    from tonewright.core import formats
    monkeypatch.setattr(ffmpeg, "_caps", {"encoders": set()})
    assert formats.available_keys() == list(formats.FORMATS)


def test_the_format_picker_skips_formats_that_are_missing(qapp, monkeypatch):
    from tonewright.core import formats
    from tonewright.ui.format_picker import FormatPicker
    everything = {spec.codec for spec in formats.FORMATS.values()} - {"libopencore_amrnb", "libvo_amrwbenc"}
    monkeypatch.setattr(ffmpeg, "_caps", {"encoders": everything})
    picker = FormatPicker()
    assert "amr" not in picker.chips and "mp3" in picker.chips
    picker.set_settings(formats.EncodeSettings.from_dict({"format": "amr"}))   # saved on a system that had AMR
    assert picker.settings().format in picker.chips


# ------------------------------------------------------------------ speech on a Mac
SAY_LISTING = """\
Albert              en_US    # Hello! My name is Albert.
Eddy (English (US)) en_US    # Hello! My name is Eddy.
Daniel              en_GB    # Hello, my name is Daniel.
Милена              uk_UA    # Привіт! Мене звати Милена.
Reed (Chinese (China mainland)) zh_CN    # 你好
"""


def test_voices_are_read_from_the_say_listing():
    assert generate.parse_say_voices(SAY_LISTING) == ["Albert", "Eddy (English (US))", "Daniel", "Милена",
                                                     "Reed (Chinese (China mainland))"]
    assert generate.parse_say_voices("") == [] and generate.parse_say_voices("not a voice line") == []


def test_the_speed_slider_maps_to_words_per_minute():
    assert generate.say_rate(0) == 175
    assert generate.say_rate(-10) < generate.say_rate(-1) < generate.say_rate(0) < generate.say_rate(1) < generate.say_rate(10)
    assert 60 <= generate.say_rate(-10) and generate.say_rate(10) <= 500


@pytest.mark.skipif(sys.platform != "darwin", reason="needs the Mac voices")
def test_a_mac_voice_can_speak():
    assert generate.speech_available() and generate.list_voices()
    src = generate.synthesize_speech("Hello from Tonewright.", rate=0, volume=100)
    assert src.frames > 1000


# ------------------------------------------------------------------ files opened from Finder or the Dock
def test_files_opened_before_the_window_exists_are_delivered_later(qapp, tmp_path):
    song = tmp_path / "song.wav"
    song.write_bytes(b"RIFF")
    relay = FileOpenRelay()
    assert relay.eventFilter(None, QFileOpenEvent(str(song))) is True
    assert relay.eventFilter(None, QFileOpenEvent(str(tmp_path / "missing.wav"))) is True     # absorbed, ignored
    got = []
    relay.deliver(lambda paths_: got.extend(paths_))
    assert got == [song]
    relay.eventFilter(None, QFileOpenEvent(str(song)))                                       # after the window exists
    assert got == [song, song]


def test_other_events_are_left_alone(qapp):
    assert FileOpenRelay().eventFilter(None, QEvent(QEvent.Type.Show)) is False


# ------------------------------------------------------------------ look
def test_text_sizes_are_the_same_on_every_platform(monkeypatch):
    monkeypatch.setattr(theme, "PT_TO_PX", 0.0)
    assert theme.css_size(8) == "8pt" and theme.css_size(12.5) == "12.5pt"
    monkeypatch.setattr(theme, "PT_TO_PX", 96 / 72)
    assert theme.css_size(8) == "10.7px" and theme.css_size(12) == "16px"


def test_a_mac_font_is_sized_in_pixels(qapp, monkeypatch):
    monkeypatch.setattr(theme, "PT_TO_PX", 96 / 72)
    f = theme.ui_font(10)
    assert f.pixelSize() == 13


# ------------------------------------------------------------------ language
def test_the_system_language_is_taken_from_the_mac_settings(qapp, monkeypatch):
    from PySide6.QtCore import QLocale

    class Fake:
        def uiLanguages(self):
            return ["uk-UA", "en-US"]

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("ctypes.windll", None, raising=False)            # not Windows: no kernel32 to ask
    monkeypatch.setattr(QLocale, "system", staticmethod(lambda: Fake()))
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(var, raising=False)
    assert i18n.system_language() == "uk"
