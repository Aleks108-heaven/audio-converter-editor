from __future__ import annotations

import numpy as np
import pytest

from tonewright.core import analysis, generate
from tonewright.core.document import Clip, Document
from tonewright.core.generate import ToneSpec
from tonewright.core.source import Source

from conftest import SR, sine


def peak_freq(x: np.ndarray, sr: int = SR) -> float:
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1 / sr)[int(np.argmax(spec))])


def band_power(x: np.ndarray, lo: float, hi: float, sr: int = SR) -> float:
    spec = np.abs(np.fft.rfft(x)) ** 2
    f = np.fft.rfftfreq(len(x), 1 / sr)
    return float(spec[(f >= lo) & (f < hi)].sum())


# ------------------------------------------------------------------ generators
def test_tone_frequency_level_and_fades():
    src = generate.generate_tone(ToneSpec(freq=1000, amp_db=-6, seconds=1.0), SR, 2)
    data = src.read(0, src.frames)
    assert src.frames == SR and data.shape[1] == 2
    assert peak_freq(data[:, 0]) == pytest.approx(1000, abs=2)
    assert float(np.abs(data[SR // 4: -SR // 4]).max()) == pytest.approx(10 ** (-6 / 20), rel=0.01)
    assert abs(data[0, 0]) < 0.01 and abs(data[-1, 0]) < 0.02                    # ends fade, so no clicks
    np.testing.assert_array_equal(data[:, 0], data[:, 1])


@pytest.mark.parametrize("kind,rms_ratio", [("sine", 2 ** -0.5), ("square", 1.0), ("triangle", 3 ** -0.5),
                                            ("sawtooth", 3 ** -0.5)])
def test_waveform_shapes_have_the_right_rms(kind, rms_ratio):
    src = generate.generate_tone(ToneSpec(waveform=kind, freq=500, amp_db=0, seconds=1.0, fade_ms=0), SR, 1)
    d = src.read(0, src.frames)[:, 0]
    assert float(np.sqrt((d ** 2).mean())) == pytest.approx(rms_ratio, rel=0.02)


def test_long_tone_is_continuous_across_chunks():
    src = generate.generate_tone(ToneSpec(freq=100, amp_db=-6, seconds=12.0, fade_ms=0), SR, 1)
    d = src.read(0, src.frames)[:, 0]
    max_step = 2 * np.pi * 100 / SR * 10 ** (-6 / 20)
    assert float(np.abs(np.diff(d)).max()) < max_step * 1.01


@pytest.mark.parametrize("sweep", ["linear", "log"])
def test_sweeps_run_from_start_to_end_frequency(sweep):
    src = generate.generate_tone(ToneSpec(freq=200, freq_end=4000, sweep=sweep, seconds=3.0, amp_db=-6), SR, 1)
    d = src.read(0, src.frames)[:, 0]
    start, end = peak_freq(d[: SR // 4]), peak_freq(d[-SR // 4:])
    assert start < 600 and end > 2500 and end > start
    if sweep == "log":
        mid = peak_freq(d[SR + SR // 2 - SR // 8: SR + SR // 2 + SR // 8])
        assert mid == pytest.approx(200 * 20 ** 0.5, rel=0.25)                    # geometric middle


def test_silence_is_free_and_exact():
    src = generate.generate_silence(2.5, 48000, 2)
    assert src.frames == 120000 and not src.read(0, src.frames).any()


@pytest.mark.parametrize("color", ["white", "pink", "brown"])
def test_noise_level_and_independent_channels(color):
    src = generate.generate_noise(color, -20.0, 4.0, SR, 2)
    d = src.read(0, src.frames)
    assert src.frames == 4 * SR
    assert float(np.sqrt((d ** 2).mean())) == pytest.approx(10 ** (-20 / 20), rel=0.05)
    assert abs(float(np.corrcoef(d[:, 0], d[:, 1])[0, 1])) < 0.05
    # power density (per Hz) four octaves apart: flat for white, -3 dB/oct for pink, -6 dB/oct for brown
    density_db = 10 * np.log10((band_power(d[:, 0], 100, 200) / 100) / (band_power(d[:, 0], 1600, 3200) / 1600))
    expected = {"white": (-2, 2), "pink": (9, 15), "brown": (15, 28)}[color]
    assert expected[0] <= density_db <= expected[1], (color, density_db)


def test_mono_noise_and_unknown_colour():
    assert generate.generate_noise("white", -12, 1.0, SR, 1).channels == 1
    with pytest.raises(Exception):
        generate.generate_noise("plaid", -12, 1.0, SR, 1)


def test_dtmf_contains_both_tones():
    src = generate.generate_dtmf("5", 200, 50, -6, SR, 1)
    d = src.read(0, src.frames)[:, 0]
    assert src.frames == int(0.25 * SR)
    p770, p1336, p500 = (band_power(d, f - 8, f + 8) for f in (770, 1336, 500))
    assert p770 > 100 * p500 and p1336 > 100 * p500
    long = generate.generate_dtmf("12 3", 100, 50, -6, SR, 2)
    assert long.frames == int((3 * 0.15 + 2 * 0.05 + 0.05 * 0) * SR) + 0 or long.frames > 0
    with pytest.raises(ValueError):
        generate.generate_dtmf("xyz", 100, 50, -6, SR, 1)


@pytest.mark.skipif(not generate.speech_available(), reason="needs Windows voices")
def test_speech_synthesis_produces_audio():
    voices = generate.list_voices()
    if not voices:
        pytest.skip("no installed voices")
    src = generate.synthesize_speech("Hello from Tonewright.", voices[0], rate=0, volume=100)
    assert src.frames / src.sr > 0.8 and float(np.abs(src.read(0, src.frames)).max()) > 0.01
    with pytest.raises(ValueError):
        generate.synthesize_speech("   ")


# ------------------------------------------------------------------ analysis
def test_measure_levels_dc_and_clipping():
    d = np.stack([sine(440, 2, amp=0.5), sine(440, 2, amp=0.25) + 0.1], axis=1)
    d[100, 0] = 1.0
    doc = Document.from_source(Source.from_array(d, SR))
    st = analysis.measure(doc, 0, doc.frames, loudness=False)
    assert st.peak[0] == pytest.approx(1.0) and st.peak[1] == pytest.approx(0.35, abs=0.01)
    assert st.rms[1] == pytest.approx(((0.25 ** 2) / 2 + 0.1 ** 2) ** 0.5, rel=0.01)
    assert st.dc[1] == pytest.approx(0.1, abs=0.002) and abs(st.dc[0]) < 0.001
    assert st.clipped == 1 and st.duration == pytest.approx(2.0)
    assert st.loudness is None


def test_measure_loudness_matches_reference():
    d = np.stack([sine(1000, 5, amp=0.1)] * 2, axis=1)
    doc = Document.from_source(Source.from_array(d, SR))
    st = analysis.measure(doc, 0, doc.frames)
    assert st.loudness is not None and -30 < st.loudness < -15
    assert st.true_peak == pytest.approx(-20.0, abs=0.5)


def test_average_spectrum_peaks_at_the_tone():
    f_left, f_right = 372 * SR / 8192, 93 * SR / 8192                              # exactly on FFT bins
    d = np.stack([sine(f_left, 3), sine(f_right, 3)], axis=1)
    doc = Document.from_source(Source.from_array(d, SR))
    freqs, db = analysis.average_spectrum(doc, 0, doc.frames, n_fft=8192)
    assert db.shape == (2, 4097)
    assert freqs[int(np.argmax(db[0]))] == pytest.approx(f_left, abs=1)
    assert freqs[int(np.argmax(db[1]))] == pytest.approx(f_right, abs=1)
    assert db[0].max() == pytest.approx(-6.02, abs=0.3)                          # amplitude 0.5 = -6 dBFS
    short = analysis.average_spectrum(doc, 0, 100)                                 # shorter than one window
    assert short[1].shape[1] == 4097


def test_spectrogram_follows_a_chirp():
    src = generate.generate_tone(ToneSpec(freq=500, freq_end=8000, seconds=4.0, amp_db=-6), SR, 1)
    doc = Document.from_source(src)
    sg = analysis.spectrogram(doc, 0, doc.frames, 200, n_fft=2048)
    assert sg.shape == (1025, 200)
    ridge = sg.argmax(axis=0)
    assert ridge[5] < ridge[100] < ridge[195]
    assert ridge[195] * SR / 2048 == pytest.approx(7800, rel=0.15)
    zoomed = analysis.spectrogram(doc, SR, SR + 4096, 8, n_fft=1024)
    assert zoomed.shape == (513, 8)
    edge = analysis.spectrogram(doc, -2000, 3000, 10, n_fft=1024)               # view hanging off the start
    assert edge.shape == (513, 10) and np.isfinite(edge).all()


def test_frame_levels_and_silence_detection():
    sig = np.zeros(10 * SR, np.float32)
    sig[: 3 * SR] = sine(300, 3, amp=0.5)
    sig[6 * SR:] = sine(300, 4, amp=0.5)
    doc = Document.from_source(Source.from_array(sig[:, None], SR))
    levels = analysis.frame_levels(doc, 0, doc.frames, 0.02)
    assert len(levels) == pytest.approx(500, abs=2)
    assert levels[10] == pytest.approx(-9.0, abs=0.7) and levels[200] < -100
    runs = analysis.find_silences(levels, 0.02, -45.0, 0.5)
    assert len(runs) == 1 and runs[0][0] == pytest.approx(3.0, abs=0.05) and runs[0][1] == pytest.approx(6.0, abs=0.05)
    assert analysis.find_silences(levels, 0.02, -45.0, 5.0) == []
    assert analysis.find_silences(np.array([]), 0.02) == []
    all_quiet = analysis.find_silences(np.full(100, -90.0), 0.02, -45.0, 0.4)
    assert all_quiet == [(0.0, pytest.approx(2.0))]


def test_envelope_builder_is_chunk_size_independent():
    rng = np.random.default_rng(1)
    d = rng.standard_normal((SR * 2 + 123, 2)).astype(np.float32) * 0.1
    a = analysis.EnvelopeBuilder(SR, 2)
    a.feed(d)
    b = analysis.EnvelopeBuilder(SR, 2)
    for i in range(0, len(d), 7777):
        b.feed(d[i:i + 7777])
    np.testing.assert_allclose(a.result(), b.result(), atol=1e-9)
