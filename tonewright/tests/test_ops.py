from __future__ import annotations

import threading

import numpy as np
import pytest

from tonewright.core import ops
from tonewright.core.common import CancelToken, Cancelled
from tonewright.core.document import Clip, Document
from tonewright.core.source import Source

from conftest import SR, sine


def doc_of(arr, sr=SR) -> Document:
    return Document.from_source(Source.from_array(arr, sr))


def peak_freq(x, sr=SR):
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1 / sr)[int(np.argmax(spec))])


def test_convert_format_same_format_is_a_copy():
    d = doc_of(np.random.default_rng(0).standard_normal((50_000, 2)).astype(np.float32))
    out = ops.convert_format(d, SR, 2, a=1000, b=40_000)
    np.testing.assert_array_equal(out.read(0, out.frames), d.read(1000, 40_000))


def test_resample_keeps_pitch_and_scales_length():
    d = doc_of(sine(1000, 2.0)[:, None])
    out = ops.convert_format(d, 22050, 1)
    assert out.sr == 22050 and abs(out.frames - 44100) < 50
    assert peak_freq(out.read(2000, 20000)[:, 0], 22050) == pytest.approx(1000, abs=10)


def test_channel_remix_both_ways():
    stereo = np.stack([sine(300, 1.0, amp=0.4), sine(300, 1.0, amp=0.2)], axis=1)
    mono = ops.convert_format(doc_of(stereo), SR, 1)
    assert mono.channels == 1
    assert float(np.abs(mono.read(0, mono.frames)).max()) == pytest.approx(0.3, abs=0.01)
    wide = ops.convert_format(doc_of(sine(300, 1.0)[:, None]), SR, 2)
    data = wide.read(0, wide.frames)
    np.testing.assert_allclose(data[:, 0], data[:, 1])
    both = ops.convert_format(doc_of(stereo), 48000, 1)
    assert both.sr == 48000 and both.channels == 1


def test_convert_format_cancel():
    token = CancelToken()
    threading.Timer(0.1, token.cancel).start()
    big = doc_of(np.zeros((SR * 600, 2), np.float32))
    with pytest.raises(Cancelled):
        ops.convert_format(big, 48000, 2, cancel=token)


def test_mix_clip_adds_with_gains():
    base = doc_of(np.full((1000, 2), 0.2, np.float32))
    clip = Clip.from_source(Source.from_array(np.full((300, 2), 0.1, np.float32), SR))
    out = ops.mix_clip(base, clip, 400)
    data = out.read(0, out.frames)
    assert out.frames == 300 and np.allclose(data, 0.3)
    out = ops.mix_clip(base, clip, 400, clip_gain_db=-6.0206, base_gain_db=-6.0206)
    assert np.allclose(out.read(0, out.frames), 0.15, atol=1e-4)


def test_mix_clip_past_the_end_treats_the_original_as_silence():
    base = doc_of(np.full((1000, 1), 0.5, np.float32))
    clip = Clip.from_source(Source.from_array(np.full((400, 1), 0.25, np.float32), SR))
    out = ops.mix_clip(base, clip, 800)
    data = out.read(0, out.frames)[:, 0]
    assert out.frames == 400
    assert np.allclose(data[:200], 0.75) and np.allclose(data[200:], 0.25)
    base.replace(800, base.frames, [], "x")                 # what the editor does: replace the overlapped part
    base.replace_with_source(800, 800, out, "Mix")
    assert base.frames == 1200


def test_mix_clip_rejects_mismatched_formats():
    base = doc_of(np.zeros((100, 2), np.float32))
    clip = Clip.from_source(Source.from_array(np.zeros((10, 1), np.float32), SR))
    with pytest.raises(ValueError):
        ops.mix_clip(base, clip, 0)
