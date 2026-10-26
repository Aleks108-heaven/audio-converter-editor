from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from tonewright.core import ffmpeg
from tonewright.core.common import Cancelled, CancelToken
from tonewright.core.source import SourceWriter

from conftest import SR, sine, write_wav


def test_probe_reports_stream_details(stereo_wav):
    info = ffmpeg.probe(stereo_wav)
    st = info.first_audio
    assert (st.sample_rate, st.channels, st.bits, st.codec) == (SR, 2, 16, "pcm_s16le")
    assert info.duration == pytest.approx(3.0, abs=0.01)
    assert not info.has_video


def test_probe_missing_file_raises(tmp_path):
    with pytest.raises(ffmpeg.FFmpegError):
        ffmpeg.probe(tmp_path / "nope.wav")


def test_decode_matches_source_samples(stereo_wav):
    src = ffmpeg.decode_to_source(stereo_wav)
    assert (src.sr, src.channels) == (SR, 2)
    assert src.frames == 3 * SR
    expected = np.stack([sine(440, 3), sine(660, 3)], axis=1)
    np.testing.assert_allclose(src.read(0, src.frames), expected, atol=2 / 32768)


def test_decode_mono_stays_mono(mono_wav):
    src = ffmpeg.decode_to_source(mono_wav)
    assert src.channels == 1 and src.frames == 2 * SR


def test_decode_downmixes_surround_to_stereo(tmp_path):
    out = tmp_path / "six.wav"
    raw = (np.random.default_rng(0).standard_normal((SR, 6)) * 0.1).astype("<f4")
    ffmpeg.encode_pcm([raw], SR, 6, out, ["-c:a", "pcm_s16le"])
    src = ffmpeg.decode_to_source(out)
    assert src.channels == 2 and src.frames == SR


def test_decode_progress_and_cancel(stereo_wav):
    seen: list[float] = []
    ffmpeg.decode_to_source(stereo_wav, progress=seen.append)
    assert seen[-1] == 1.0 and all(0 <= p <= 1 for p in seen)
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        ffmpeg.decode_to_source(stereo_wav, cancel=token)


def test_encode_pcm_to_mp3_roundtrip(tmp_path):
    out = tmp_path / "t.mp3"
    data = np.stack([sine(440, 2.0), sine(440, 2.0)], axis=1)
    ffmpeg.encode_pcm([data], SR, 2, out, ["-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3"])
    info = ffmpeg.probe(out)
    assert info.first_audio.codec == "mp3" and info.first_audio.channels == 2
    assert info.duration == pytest.approx(2.0, abs=0.1)
    back = ffmpeg.decode_to_source(out).read(0, SR)[:, 0]
    assert abs(float(np.abs(back).max()) - 0.5) < 0.05
    assert not list(tmp_path.glob("*.tonewright-part*"))


def test_encode_failure_leaves_no_partial_file(tmp_path):
    out = tmp_path / "bad.mp3"
    with pytest.raises(ffmpeg.FFmpegError):
        ffmpeg.encode_pcm([np.zeros((SR, 2), np.float32)], SR, 2, out, ["-c:a", "no_such_codec"])
    assert not out.exists() and not list(tmp_path.iterdir())


def test_filter_stream_big_transfer_does_not_deadlock():
    n = SR * 40                       # ~14 MB each way, far larger than pipe buffers
    data = np.full((n, 2), 0.5, np.float32)
    chunks = (data[i:i + 100_000] for i in range(0, n, 100_000))
    w = SourceWriter(SR, 2)
    ffmpeg.filter_stream(chunks, SR, 2, "volume=0.5", w, total_frames=n)
    out = w.finish()
    assert out.frames == n
    assert float(out.read(1000, 2000).mean()) == pytest.approx(0.25, abs=1e-4)


def test_samples_that_are_not_finite_are_made_silent_and_clean_blocks_are_left_alone():
    clean = np.array([[0.5, -0.5], [1.0, 0.0]], np.float32)
    assert ffmpeg._finite_samples(clean) is clean
    dirty = np.array([[0.5, np.nan], [np.inf, -np.inf]], np.float32)
    np.testing.assert_array_equal(ffmpeg._finite_samples(dirty), [[0.5, 0.0], [0.0, 0.0]])
    assert np.isnan(dirty[0, 1])                                        # the caller's array was not changed


def test_filter_stream_never_hands_on_nan_or_infinity():
    data = np.full((5000, 2), 0.25, np.float32)
    data[100, 0], data[200, 1], data[300, 0] = np.nan, np.inf, -np.inf
    w = SourceWriter(SR, 2)
    ffmpeg.filter_stream([data], SR, 2, "anull", w, total_frames=len(data))
    out = w.finish().read(0, 5000)
    assert np.isfinite(out).all() and out[0, 0] == pytest.approx(0.25) and out[4000, 1] == pytest.approx(0.25)
    assert (out[100, 0], out[200, 1], out[300, 0]) == (0.0, 0.0, 0.0)


def test_filter_stream_error_is_reported():
    w = SourceWriter(SR, 1)
    with pytest.raises(ffmpeg.FFmpegError):
        ffmpeg.filter_stream([np.zeros((1000, 1), np.float32)], SR, 1, "nonexistentfilter=1", w)


def test_filter_stream_cancel_is_prompt():
    token = CancelToken()

    def endless():
        while True:
            yield np.zeros((50_000, 2), np.float32)
            time.sleep(0.01)

    threading.Timer(0.3, token.cancel).start()
    t0 = time.time()
    with pytest.raises(Cancelled):
        ffmpeg.filter_stream(endless(), SR, 2, "volume=1", SourceWriter(SR, 2), cancel=token)
    assert time.time() - t0 < 5


def test_convert_file_reports_progress(tmp_path):
    src = write_wav(tmp_path / "long.wav", np.tile(sine(220, 1.0), 20)[:, None])
    out = tmp_path / "long.ogg"
    seen: list[float] = []
    ffmpeg.convert_file(src, out, ["-c:a", "libvorbis", "-q:a", "3"], duration=20.0, progress=seen.append)
    assert out.exists() and seen[-1] == 1.0
    assert ffmpeg.probe(out).first_audio.codec == "vorbis"


def test_convert_file_cancel_removes_partial(tmp_path):
    src = write_wav(tmp_path / "long.wav", np.tile(sine(220, 1.0), 60)[:, None])
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        ffmpeg.convert_file(src, tmp_path / "x.mp3", ["-c:a", "libmp3lame"], duration=60, cancel=token)
    assert not (tmp_path / "x.mp3").exists()
    assert not list(tmp_path.glob("*part*"))


def test_capabilities_detected():
    assert ffmpeg.has_encoder("libmp3lame")
    assert ffmpeg.has_filter("aecho")
    assert not ffmpeg.has_filter("definitely_not_a_filter")


def test_convert_whole_file_to_pcm_resamples(stereo_wav):
    chunks = list(ffmpeg.convert_whole_file_to_pcm(stereo_wav, 22050, 1))
    total = sum(len(c) for c in chunks)
    assert total == pytest.approx(3 * 22050, abs=50)
    assert chunks[0].shape[1] == 1
