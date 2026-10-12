"""Opening video files and working with long recordings."""
from __future__ import annotations

import subprocess
import time

import numpy as np
import pytest

from tonewright.core import effects as fx
from tonewright.core import ffmpeg
from tonewright.core.document import Document
from tonewright.core.source import Source

from conftest import SR, sine


def make_video(path, with_two_audio_tracks: bool = False):
    cmd = [ffmpeg.find_tool("ffmpeg"), "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=3",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=3:sample_rate=44100"]
    if with_two_audio_tracks:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=880:duration=3:sample_rate=48000"]
    cmd += ["-map", "0:v", "-map", "1:a"] + (["-map", "2:a"] if with_two_audio_tracks else []) + \
           ["-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(path)]
    subprocess.run(cmd, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return path


def test_audio_is_extracted_from_a_video(tmp_path):
    video = make_video(tmp_path / "clip.mp4")
    info = ffmpeg.probe(video)
    assert info.has_video and len(info.audio) == 1
    src = ffmpeg.decode_to_source(video)
    assert src.sr == 44100 and abs(src.frames - 3 * SR) < SR * 0.1
    data = src.read(SR // 2, SR)[:, 0]
    spec = np.abs(np.fft.rfft(data * np.hanning(len(data))))
    assert abs(np.fft.rfftfreq(len(data), 1 / SR)[int(np.argmax(spec))] - 440) < 15


def test_second_audio_track_can_be_chosen(tmp_path):
    video = make_video(tmp_path / "two.mkv", with_two_audio_tracks=True)
    info = ffmpeg.probe(video)
    assert len(info.audio) == 2
    src = ffmpeg.decode_to_source(video, stream=info.audio[1], info=info)
    assert src.sr == 48000
    data = src.read(4000, 30000)[:, 0]
    spec = np.abs(np.fft.rfft(data * np.hanning(len(data))))
    assert abs(np.fft.rfftfreq(len(data), 1 / 48000)[int(np.argmax(spec))] - 880) < 20


def test_long_recording_stays_responsive(tmp_path):
    """Half an hour of stereo audio: decode into a disk-backed source, draw peaks, edit, process."""
    from tonewright.core.convert import ConvertItem
    n = 30 * 60 * SR
    rng = np.random.default_rng(1)
    src_path = tmp_path / "long.wav"
    chunk = (rng.standard_normal((SR * 60, 2)) * 0.1).astype(np.float32)
    t0 = time.time()
    ffmpeg.encode_pcm((chunk for _ in range(30)), SR, 2, src_path, ["-c:a", "pcm_s16le", "-f", "wav"])
    t_write = time.time() - t0
    t0 = time.time()
    src = ffmpeg.decode_to_source(src_path)
    t_decode = time.time() - t0
    assert src.frames == n and type(src._store).__name__ == "_FileStore"
    t0 = time.time()
    src.peaks()
    t_peaks = time.time() - t0
    doc = Document.from_source(src)
    t0 = time.time()
    for _ in range(20):
        mn, mx = doc.peaks(0, doc.frames, 1500)
    t_draw = (time.time() - t0) / 20
    t0 = time.time()
    doc.delete(10 * SR, 600 * SR)
    doc.insert(SR, doc.copy(5 * SR, 700 * SR))
    doc.silence(0, 100 * SR)
    t_edit = time.time() - t0
    assert doc.frames == n - 590 * SR + 695 * SR
    t0 = time.time()
    fx.run_effect(fx.get("normalize"), None, doc.snapshot(), 0, doc.frames)
    t_norm = time.time() - t0
    print(f"\nwrite {t_write:.1f}s decode {t_decode:.1f}s peaks {t_peaks:.2f}s draw {t_draw * 1000:.1f}ms "
          f"edits {t_edit * 1000:.1f}ms normalize {t_norm:.1f}s")
    assert t_draw < 0.1 and t_edit < 0.5          # drawing and editing are interactive however long the file is
    assert t_decode < 60 and t_norm < 90
    for _ in range(3):
        doc.undo()
    assert doc.read(0, 1000).shape == (1000, 2)
    src.close()
