"""Security regression tests: hostile files, hostile names and hostile folders.

Nothing here touches the network beyond a throw-away listener on 127.0.0.1, and nothing is ever run from an
untrusted place - the 'planted' programs are harmless copies of whoami.exe that are only looked up, or that
print a user name instead of the expected text.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from tonewright import paths
from tonewright.core import ffmpeg, generate
from tonewright.core.audiobook import BookOptions
from tonewright.core.convert import ConvertOptions, plan_output, probe_item
from tonewright.core.tags import TagSet, name_from_pattern, sanitize_filename
from tonewright.ui import safe_image

from conftest import SR, sine, write_wav
from test_audiobook import build

ROOT = Path(__file__).resolve().parents[1]
windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows program lookup")


# ------------------------------------------------------------------ programs planted in the current folder
@windows_only
def test_programs_in_the_current_folder_are_never_used(tmp_path):
    """Windows normally searches the current folder first. A fake ffmpeg.exe / powershell.exe next to a
    downloaded file must not be picked up - checked in a fresh process with the default Windows behaviour."""
    whoami = Path(os.environ["SystemRoot"]) / "System32" / "whoami.exe"
    for name in ("ffmpeg.exe", "ffprobe.exe", "powershell.exe"):
        shutil.copy(whoami, tmp_path / name)
    script = (
        "import json, shutil, subprocess\n"
        "import tonewright\n"                                   # sets the protection under test
        "from tonewright.core import ffmpeg\n"
        "r = subprocess.run(['powershell.exe', '-NoProfile', '-Command', 'echo REAL_POWERSHELL'],\n"
        "                   capture_output=True, text=True)\n"
        "print(json.dumps({'ffmpeg': ffmpeg.find_tool('ffmpeg'), 'which': shutil.which('powershell'),\n"
        "                  'ps': r.stdout.strip()}))\n")
    env = {k: v for k, v in os.environ.items() if k.lower() != "nodefaultcurrentdirectoryinexepath"}
    env["PYTHONPATH"] = str(ROOT)
    res = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert Path(out["ffmpeg"]).resolve().parent != tmp_path.resolve()
    assert out["which"] is None or Path(out["which"]).resolve().parent != tmp_path.resolve()
    assert out["ps"] == "REAL_POWERSHELL"


def test_importing_the_package_turns_off_current_folder_search():
    assert os.environ.get("NoDefaultCurrentDirectoryInExePath")


def test_a_program_found_in_the_current_folder_is_rejected(tmp_path, monkeypatch):
    exe = tmp_path / "ffmpeg.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.chdir(tmp_path)
    assert ffmpeg._in_cwd(str(exe))
    assert not ffmpeg._in_cwd(str(ROOT / "pytest.ini"))


def test_the_bundled_ffmpeg_is_preferred_over_path(monkeypatch):
    bundled = Path(ffmpeg.__file__).resolve().parents[2] / "bin"
    names = [Path(c) for c in ffmpeg._candidate_paths("ffmpeg")]
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    assert bundled / exe in names
    first_path_based = next((i for i, c in enumerate(names) if c == Path(shutil.which("ffmpeg") or "?")), len(names))
    assert names.index(bundled / exe) < first_path_based


def test_windows_helper_programs_are_started_by_full_path():
    assert "-ExecutionPolicy" not in generate._PS
    if os.name == "nt":
        assert Path(generate._PS[0]).is_absolute() and Path(generate._PS[0]).is_file()
        assert Path(paths.explorer_exe()).is_absolute() and Path(paths.explorer_exe()).is_file()
        assert isinstance(generate.list_voices(), list)           # still works without the policy bypass


# ------------------------------------------------------------------ hostile media files
class _Recorder(BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):  # noqa: N802
        type(self).hits.append(self.path)
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.mark.parametrize("name", ["playlist.m3u8", "playlist.mp3"])
def test_a_playlist_cannot_make_ffmpeg_fetch_urls(tmp_path, name):
    _Recorder.hits = []
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        evil = tmp_path / name
        evil.write_text("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n#EXTINF:1.0,\n"
                        f"http://127.0.0.1:{port}/seg0.mp3\n#EXT-X-ENDLIST\n")
        for action in (ffmpeg.probe, ffmpeg.decode_to_source):
            with pytest.raises(Exception):
                action(evil)
    finally:
        server.shutdown()
    assert _Recorder.hits == []


def test_a_concat_list_cannot_pull_in_other_files(tmp_path):
    secret = tmp_path / "secret.wav"
    write_wav(secret, np.stack([sine(440, 0.5)] * 2, axis=1))
    evil = tmp_path / "list.mp3"
    evil.write_text(f"ffconcat version 1.0\nfile '{secret.as_posix()}'\n")
    with pytest.raises(Exception):
        ffmpeg.decode_to_source(evil)


# ------------------------------------------------------------------ hostile tags and names
@pytest.mark.parametrize("title", ["..\\..\\Windows\\evil", "../../evil", "C:\\Windows\\evil", "CON", "NUL.mp3",
                                   "a<b>c|d?e*f\"g", "..", "   ", "trailing dot.", "x" * 500, "evil\x00name"])
def test_names_built_from_tags_stay_inside_the_output_folder(tmp_path, title):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    src = write_wav(tmp_path / "in.wav", np.stack([sine(440, 0.3)] * 2, axis=1))
    item = probe_item(src)
    item.tags = TagSet(title=title, artist=title, album=title)
    for pattern in ("{title}", "{artist} - {title}", "{album}\\{title}", "..\\{title}"):
        planned = plan_output(item, ConvertOptions(out_dir=out_dir, pattern=pattern), set())
        assert planned is not None
        assert planned.parent == out_dir
        assert planned.stem.upper() not in {"CON", "NUL", "PRN", "AUX"}
        assert not planned.name.endswith((".", " ")) and len(planned.name) < 200
    assert not any(ch in sanitize_filename(title) for ch in '<>:"/\\|?*\x00')
    assert name_from_pattern("{title}", item.tags, "stem") not in ("", ".", "..")


def test_chapter_titles_cannot_inject_metadata_sections(tmp_path):
    nasty = "Ch 1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1\ntitle=Injected"
    out, _ = build(tmp_path, [1.0, 1.0], BookOptions(title="B\ntitle=Evil", split="files"), titles=[nasty, "Two"])
    info = ffmpeg.probe(out)
    assert len(info.chapters) == 2                                   # no extra chapter was smuggled in
    assert info.chapters[0][2] == "Ch 1 [CHAPTER] TIMEBASE=1/1000 START=0 END=1 title=Injected"
    assert info.chapters[1][2] == "Two"
    assert info.tags.get("title", "").startswith("B")                # still one value, not a second 'title' key
    assert info.tags.get("artist", "") == ""


# ------------------------------------------------------------------ hostile pictures
def _png(width: int, height: int, rows: int = 4) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" * (1 + width * 4) * rows)) + chunk(b"IEND", b""))


def _encode(fmt: str) -> bytes:
    img = QImage(32, 32, QImage.Format.Format_RGB32)
    img.fill(QColor("orange"))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    return bytes(buf.data()) if img.save(buf, fmt) else b""


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("fmt", ["PNG", "JPG", "BMP", "GIF"])
def test_plain_cover_art_formats_are_decoded(qapp, fmt):
    data = _encode(fmt)
    if not data:
        pytest.skip(f"this Qt build cannot write {fmt}")
    image = safe_image.read_image(data)
    assert not image.isNull() and (image.width(), image.height()) == (32, 32)


@pytest.mark.parametrize("fmt", ["TIFF", "WEBP", "TGA", "ICO", "WBMP"])
def test_rare_picture_formats_are_refused(qapp, fmt):
    data = _encode(fmt)
    if not data:
        pytest.skip(f"this Qt build cannot write {fmt}")
    assert safe_image.read_image(data).isNull()


def test_oversized_and_corrupt_pictures_are_refused(qapp, tmp_path):
    assert safe_image.read_image(_png(60000, 60000)).isNull()                # decompression bomb
    assert safe_image.read_image(_png(safe_image.MAX_SIDE + 1, 1)).isNull()  # absurd dimension
    assert safe_image.read_image(b"").isNull()
    assert safe_image.read_image(os.urandom(4096)).isNull()
    assert safe_image.read_image(_png(32, 32)[:40]).isNull()                 # truncated
    bad = tmp_path / "x.png"
    bad.write_bytes(b"not a picture")
    assert safe_image.read_image(bad).isNull()
    assert safe_image.read_image(tmp_path / "missing.png").isNull()
    good = tmp_path / "ok.png"
    good.write_bytes(_png(8, 8, rows=8))
    assert not safe_image.read_image(good).isNull()
