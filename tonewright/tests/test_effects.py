from __future__ import annotations

import json
import re
import threading

import numpy as np
import pytest

from tonewright.core import effects as fx
from tonewright.core import ffmpeg
from tonewright.core.common import Cancelled, CancelToken
from tonewright.core.document import Clip, Document
from tonewright.core.effects import dsp
from tonewright.core.source import Source

SR = 44100


def clip_of(arr: np.ndarray, sr: int = SR) -> Clip:
    return Clip.from_source(Source.from_array(arr, sr))


def run(effect_id: str, params: dict | None, arr: np.ndarray, *, a: int = 0, b: int | None = None,
        channels=None, **kw) -> np.ndarray:
    clip = clip_of(arr)
    b = clip.frames if b is None else b
    out = fx.run_effect(fx.get(effect_id), params, clip, a, b, channels=channels, **kw)
    return out.read(0, out.frames)


def sine(freq: float, seconds: float, amp: float = 0.5, channels: int = 1) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return np.repeat((amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)[:, None], channels, axis=1)


def amp_at(x: np.ndarray, freq: float) -> float:
    """Amplitude of the `freq` component of a mono signal."""
    t = np.arange(len(x)) / SR
    z = (x * np.exp(-2j * np.pi * freq * t)).sum()
    return float(2 * abs(z) / len(x))


def db(x: float) -> float:
    return 20 * np.log10(max(x, 1e-12))


# ------------------------------------------------------------------ registry
def test_registry_is_consistent():
    effects = fx.all_effects()
    assert len({e.id for e in effects}) == len(effects) >= 30
    known = set(fx.CATEGORY_ORDER)
    for e in effects:
        assert e.name and e.description and e.category in known, e.id
        keys = {p.key for p in e.params}
        assert len(keys) == len(e.params), e.id
        assert e.clean(None) == e.clean(e.defaults())
        for preset, values in e.presets.items():
            assert set(values) <= keys, (e.id, preset)
            cleaned = e.clean(values)
            for k, v in values.items():
                assert cleaned[k] == pytest.approx(v), (e.id, preset, k)
        for p in e.params:
            if p.show_if:
                assert p.show_if[0] in keys, (e.id, p.key)
            if p.kind == "choice":
                assert p.default in [c[0] for c in p.choices], (e.id, p.key)
            if p.kind in ("float", "int"):
                assert p.minimum <= p.default <= p.maximum, (e.id, p.key)


def test_clean_clamps_and_falls_back():
    amp = fx.get("amplify")
    assert amp.clean({"gain": 999})["gain"] == 60.0
    assert amp.clean({"gain": "oops"})["gain"] == amp.defaults()["gain"]
    assert fx.get("fade").clean({"shape": "nope"})["shape"] == "slow"
    env = fx.get("envelope").clean({"points": [[0.9, 50], [0.1, 300]]})
    assert env["points"] == [[0.1, 200.0], [0.9, 50.0]]


@pytest.mark.parametrize("effect", [e for e in fx.all_effects() if e.id != "custom_filter"],
                         ids=lambda e: e.id)
def test_every_effect_runs_with_defaults_and_presets(effect):
    rng = np.random.default_rng(4)
    arr = (rng.standard_normal((SR, 2)) * 0.1).astype(np.float32)
    for params in [None, *effect.presets.values()]:
        out = run(effect.id, params, arr)
        assert out.shape[1] == 2 and len(out) > 0 and np.isfinite(out).all(), (effect.id, params)
        expected = effect.out_frames(len(arr), SR, effect.clean(params))
        if effect.preserves_length(effect.clean(params)):
            assert len(out) == len(arr), (effect.id, params)
        else:
            assert abs(len(out) - expected) <= max(2000, 0.03 * expected), (effect.id, len(out), expected)


def test_custom_filter_and_bad_filter():
    arr = sine(440, 0.5)
    out = run("custom_filter", {"chain": "volume=0.5"}, arr)
    assert float(np.abs(out).max()) == pytest.approx(0.25, abs=0.01)
    with pytest.raises(ffmpeg.FFmpegError):
        run("custom_filter", {"chain": "notafilter=1"}, arr)


# ------------------------------------------------------------------ volume
def test_amplify_and_clip_prevention():
    arr = sine(440, 0.5, amp=0.4)
    out = run("amplify", {"gain": 6.0, "prevent_clipping": False}, arr)
    assert float(np.abs(out).max()) == pytest.approx(0.4 * 1.9953, rel=1e-3)
    out = run("amplify", {"gain": 20.0, "prevent_clipping": True}, arr)
    assert float(np.abs(out).max()) == pytest.approx(1.0, abs=1e-3)
    out = run("amplify", {"gain": -6.0, "prevent_clipping": True}, arr)
    assert float(np.abs(out).max()) == pytest.approx(0.4 * 0.5012, rel=1e-3)


def test_normalize_peak_independent_and_silence():
    arr = np.concatenate([sine(300, 0.5, 0.2), sine(300, 0.5, 0.05)], axis=1)  # L loud, R quiet
    target = 10 ** (-1 / 20)
    out = run("normalize", {"target": -1.0}, arr)
    assert float(np.abs(out).max()) == pytest.approx(target, rel=1e-3)
    assert float(np.abs(out[:, 1]).max()) == pytest.approx(target * 0.25, rel=1e-2)      # ratio kept
    out = run("normalize", {"target": -1.0, "independent": True}, arr)
    assert float(np.abs(out[:, 1]).max()) == pytest.approx(target, rel=1e-3)
    silent = np.zeros((1000, 2), np.float32)
    assert not run("normalize", None, silent).any()


def test_normalize_rms_never_clips_and_removes_dc():
    arr = sine(200, 1.0, 0.3) + 0.1                   # DC offset of 0.1
    out = run("normalize", {"target": -3.0, "mode": "peak", "remove_dc": True}, arr)
    assert abs(float(out.mean())) < 1e-3
    assert float(np.abs(out).max()) == pytest.approx(10 ** (-3 / 20), rel=1e-3)
    out = run("normalize", {"target": -6.0, "mode": "rms"}, sine(200, 1.0, 0.1))
    assert float(np.sqrt((out ** 2).mean())) == pytest.approx(10 ** (-6 / 20), rel=1e-2) or float(np.abs(out).max()) <= 1.0
    peaky = np.zeros((SR, 1), np.float32)
    peaky[100] = 0.9
    assert float(np.abs(run("normalize", {"target": -6.0, "mode": "rms"}, peaky)).max()) <= 1.0 + 1e-6


def test_remove_dc_invert_reverse():
    arr = sine(200, 0.5, 0.2) + 0.15
    assert abs(float(run("dc_offset", None, arr).mean())) < 1e-4
    np.testing.assert_array_equal(run("invert", None, arr), -arr)
    rng = np.random.default_rng(1)
    noise = rng.standard_normal((SR, 2)).astype(np.float32)
    np.testing.assert_array_equal(run("reverse", None, noise), noise[::-1])


def test_reverse_across_multiple_chunks():
    n = 3 * 262144 + 12345
    arr = np.arange(n, dtype=np.float32)[:, None]
    np.testing.assert_array_equal(run("reverse", None, arr), arr[::-1])


# ------------------------------------------------------------------ fades
def test_fade_in_linear_and_out():
    arr = np.ones((1000, 1), np.float32)
    out = run("fade", {"direction": "in", "shape": "linear", "start": 0, "end": 100}, arr)[:, 0]
    np.testing.assert_allclose(out, np.linspace(0, 1, 1000), atol=1e-6)
    out = run("fade", {"direction": "out", "shape": "linear", "start": 100, "end": 0}, arr)[:, 0]
    np.testing.assert_allclose(out, np.linspace(1, 0, 1000), atol=1e-6)
    out = run("fade", {"direction": "in", "shape": "linear", "start": 50, "end": 100}, arr)[:, 0]
    assert out[0] == pytest.approx(0.5) and out[-1] == pytest.approx(1.0)


@pytest.mark.parametrize("shape", [s for s, _ in dsp.FADE_SHAPES])
def test_fade_shapes_are_monotonic_with_exact_endpoints(shape):
    x = np.linspace(0, 1, 500)
    y = dsp.fade_shape(x, shape)
    assert y[0] == pytest.approx(0.0, abs=1e-9) and y[-1] == pytest.approx(1.0, abs=1e-9)
    assert (np.diff(y) >= -1e-12).all()
    out = dsp.fade_gain(x, shape, "out", 1.0, 0.0)
    assert out[0] == pytest.approx(1.0) and out[-1] == pytest.approx(0.0, abs=1e-9)


def test_slow_fade_is_gentle_at_first_and_fast_is_the_mirror():
    x = np.array([0.5])
    assert dsp.fade_shape(x, "slow")[0] < 0.1 < 0.9 < dsp.fade_shape(x, "fast")[0]


def test_preview_window_fades_over_the_whole_selection():
    arr = np.ones((10_000, 1), np.float32)
    clip = clip_of(arr)
    whole = fx.run_effect(fx.get("fade"), {"shape": "linear"}, clip, 0, 10_000)
    part = fx.run_effect(fx.get("fade"), {"shape": "linear"}, clip, 0, 10_000, window=1_000)
    assert part.frames == 1_000
    np.testing.assert_allclose(part.read(0, 1000), whole.read(0, 1000), atol=1e-6)
    assert float(part.read(999, 1000)[0, 0]) < 0.11


def test_envelope_interpolates_points():
    arr = np.ones((1001, 1), np.float32)
    pts = [[0, 100], [0.5, 0], [1, 200]]
    out = run("envelope", {"points": pts}, arr)[:, 0]
    assert out[0] == pytest.approx(1.0) and out[500] == pytest.approx(0.0, abs=1e-6)
    assert out[-1] == pytest.approx(2.0) and out[250] == pytest.approx(0.5, abs=1e-3)


# ------------------------------------------------------------------ channels
def test_effects_can_target_one_channel():
    rng = np.random.default_rng(2)
    arr = (rng.standard_normal((5000, 2)) * 0.1).astype(np.float32)
    out = run("amplify", {"gain": 6.0, "prevent_clipping": False}, arr, channels=[0])
    np.testing.assert_allclose(out[:, 0], arr[:, 0] * 1.99526, rtol=1e-4)
    np.testing.assert_array_equal(out[:, 1], arr[:, 1])
    out = run("reverse", None, arr, channels=[1])
    np.testing.assert_array_equal(out[:, 0], arr[:, 0])
    np.testing.assert_array_equal(out[:, 1], arr[::-1, 1])


def test_channel_subset_is_ignored_for_length_changing_effects():
    arr = sine(440, 1.0, channels=2)
    out = run("tempo", {"percent": 100.0, "mode": "speed"}, arr, channels=[0])
    assert out.shape[1] == 2 and abs(len(out) - SR // 2) < 200


def test_balance_and_channel_mixer():
    arr = np.ones((100, 2), np.float32)
    out = run("balance", {"balance": 50.0}, arr)
    assert out[0, 0] == pytest.approx(0.5) and out[0, 1] == pytest.approx(1.0)
    stereo = np.stack([np.full(100, 0.2), np.full(100, 0.6)], axis=1).astype(np.float32)
    np.testing.assert_allclose(run("channel_mixer", {"mode": "swap"}, stereo)[0], [0.6, 0.2], atol=1e-6)
    np.testing.assert_allclose(run("channel_mixer", {"mode": "left"}, stereo)[0], [0.2, 0.2], atol=1e-6)
    np.testing.assert_allclose(run("channel_mixer", {"mode": "mono"}, stereo)[0], [0.4, 0.4], atol=1e-6)
    out = run("vocal_remover", {"mode": "remove"}, stereo)
    np.testing.assert_allclose(out[0], [-0.2, -0.2], atol=1e-5)
    centre = np.stack([sine(300, 1.0)[:, 0], sine(300, 1.0)[:, 0]], axis=1)
    assert float(np.abs(run("vocal_remover", {"mode": "remove"}, centre)).max()) < 1e-4


# ------------------------------------------------------------------ time & pitch
def test_tempo_changes_length_not_pitch():
    arr = sine(440, 2.0)
    out = run("tempo", {"percent": 100.0, "mode": "tempo"}, arr)[:, 0]
    assert abs(len(out) - SR) < SR * 0.03
    mid = out[len(out) // 4: len(out) * 3 // 4]
    assert amp_at(mid, 440) > 0.4 and amp_at(mid, 880) < 0.05


def test_speed_mode_shifts_pitch_with_speed():
    out = run("tempo", {"percent": 100.0, "mode": "speed"}, sine(440, 2.0))[:, 0]
    assert abs(len(out) - SR) < 50
    assert amp_at(out[2000:-2000], 880) > 0.4


def test_pitch_shift_keeps_length_and_moves_frequency():
    arr = sine(440, 2.0)
    out = run("pitch", {"semitones": 12.0}, arr)[:, 0]
    assert len(out) == len(arr)
    mid = out[SR // 2: SR + SR // 2]
    assert amp_at(mid, 880) > 0.35 and amp_at(mid, 440) < 0.1
    down = run("pitch", {"semitones": -12.0}, arr)[:, 0][SR // 2: SR + SR // 2]
    assert amp_at(down, 220) > 0.35


def test_tempo_without_rubberband_falls_back_to_atempo(monkeypatch):
    monkeypatch.setattr(ffmpeg, "has_filter", lambda name: False)
    out = run("tempo", {"percent": 50.0}, sine(440, 2.0))[:, 0]
    assert abs(len(out) - int(2 * SR / 1.5)) < SR * 0.02
    assert amp_at(out[2000:-2000], 440) > 0.4
    pitched = run("pitch", {"semitones": 12.0}, sine(440, 2.0))[:, 0]
    assert amp_at(pitched[2000:-2000], 880) > 0.35


# ------------------------------------------------------------------ equalizer & filters
def test_graphic_eq_boosts_the_chosen_band():
    arr = np.concatenate([sine(1000, 1.0, 0.1), ], axis=1)
    out = run("graphic_eq", {"b1k": 12.0}, arr)[:, 0]
    gain = db(amp_at(out[SR // 4:], 1000)) - db(amp_at(arr[SR // 4:, 0], 1000))
    assert gain == pytest.approx(12.0, abs=1.5)
    flat = run("graphic_eq", {}, arr)[:, 0]
    np.testing.assert_allclose(flat, arr[:, 0], atol=1e-6)


def test_parametric_eq_cuts():
    arr = sine(500, 1.0, 0.3)
    out = run("parametric_eq", {"g2": -12.0, "f2": 500.0, "q2": 2.0}, arr)[:, 0]
    assert db(amp_at(out[SR // 4:], 500)) - db(amp_at(arr[SR // 4:, 0], 500)) == pytest.approx(-12.0, abs=1.5)


def test_filters_attenuate_the_right_side():
    mix = sine(100, 1.0, 0.3) + sine(5000, 1.0, 0.3)
    hp = run("filter", {"type": "highpass", "freq": 1000.0, "slope": 24}, mix)[:, 0][SR // 4:]
    assert db(amp_at(hp, 100)) < db(0.3) - 30 and db(amp_at(hp, 5000)) > db(0.3) - 1.5
    lp = run("filter", {"type": "lowpass", "freq": 1000.0, "slope": 24}, mix)[:, 0][SR // 4:]
    assert db(amp_at(lp, 5000)) < db(0.3) - 30 and db(amp_at(lp, 100)) > db(0.3) - 1.5
    bp = run("filter", {"type": "bandpass", "freq": 5000.0, "bandwidth": 500.0}, mix)[:, 0][SR // 4:]
    assert db(amp_at(bp, 100)) < db(0.3) - 20
    br = run("filter", {"type": "bandreject", "freq": 5000.0, "bandwidth": 300.0, "slope": 36}, mix)[:, 0][SR // 4:]
    assert db(amp_at(br, 5000)) < db(0.3) - 25 and db(amp_at(br, 100)) > db(0.3) - 1.5


def test_bass_treble_and_hum_removal():
    low = sine(60, 1.0, 0.1)
    out = run("tone", {"bass": 12.0, "treble": 0.0, "bass_freq": 200.0}, low)[:, 0][SR // 4:]
    assert db(amp_at(out, 60)) - db(0.1) > 8
    hum = sine(50, 1.0, 0.3) + sine(440, 1.0, 0.3)
    out = run("hum", {"base": 50, "harmonics": 2}, hum)[:, 0][SR // 2:]
    assert db(amp_at(out, 50)) < db(0.3) - 25 and db(amp_at(out, 440)) > db(0.3) - 1


# ------------------------------------------------------------------ dynamics
def test_compressor_and_limiter_reduce_peaks():
    loud = sine(300, 1.0, 0.9)
    out = run("compressor", {"threshold": -20.0, "ratio": 8.0, "makeup": 0.0, "attack": 1.0}, loud)[:, 0]
    assert float(np.abs(out[SR // 2:]).max()) < 0.6
    out = run("limiter", {"ceiling": -6.0}, loud)[:, 0]
    assert float(np.abs(out).max()) <= 10 ** (-6 / 20) + 0.01


def test_noise_gate_silences_quiet_parts():
    quiet = sine(300, 1.0, 0.003)
    out = run("gate", {"threshold": -30.0, "reduction": 40.0}, quiet)[:, 0]
    assert float(np.abs(out[SR // 2:]).max()) < 0.0005


# ------------------------------------------------------------------ modulation & space
def test_modulation_effects_change_the_signal_but_keep_length():
    arr = sine(440, 1.0, 0.3)
    for effect_id in ("chorus", "flanger", "phaser", "vibrato", "tremolo"):
        out = run(effect_id, None, arr)
        assert len(out) == len(arr) and not np.allclose(out, arr, atol=1e-3), effect_id


def test_echo_repeats_an_impulse():
    arr = np.zeros((SR, 1), np.float32)
    arr[1000] = 1.0
    out = run("echo", {"delay": 100.0, "decay": 50.0, "repeats": 2}, arr)[:, 0]
    d = int(0.1 * SR)
    assert out[1000] == pytest.approx(1.0, abs=1e-3)
    assert out[1000 + d] == pytest.approx(0.5, abs=0.02)
    assert out[1000 + 2 * d] == pytest.approx(0.25, abs=0.02)
    longer = run("echo", {"delay": 100.0, "repeats": 2, "tail": True}, arr[:2000])
    assert len(longer) > 2000 + d


def test_reverb_adds_a_decaying_tail_and_respects_levels():
    impulse = np.zeros((SR, 2), np.float32)
    impulse[100] = 0.5
    out = run("reverb", {"decay": 1.0, "wet": 0.0, "dry": -60.0, "predelay": 0.0}, impulse)
    assert len(out) == SR
    early = float((out[100:100 + 4410] ** 2).sum())
    late = float((out[100 + 22050: 100 + 26460] ** 2).sum())
    assert early > 0 and late > 0 and late < early * 0.35
    dry_only = run("reverb", {"wet": -40.0, "dry": 0.0}, impulse)
    assert dry_only[100, 0] == pytest.approx(0.5, abs=0.02)
    tail = run("reverb", {"decay": 0.5, "tail": True}, impulse[:5000])
    assert len(tail) == fx.get("reverb").out_frames(5000, SR, fx.get("reverb").clean({"decay": 0.5, "tail": True}))


def test_reverb_streams_across_chunk_boundaries_seamlessly():
    rng = np.random.default_rng(8)
    arr = (rng.standard_normal((300_000, 1)) * 0.1).astype(np.float32)   # > one 262144-frame chunk
    out = run("reverb", {"decay": 0.4, "dry": -60.0, "wet": 0.0}, arr)[:, 0]
    ir = dsp.make_reverb_ir(SR, 1, 0.4, 20.0, 0.5, 1.0)[:, 0]
    ref = np.convolve(arr[:, 0].astype(np.float64), ir.astype(np.float64))[:len(arr)]
    err = np.abs(out - ref).max() / np.abs(ref).max()
    assert err < 1e-3


# ------------------------------------------------------------------ restoration
def test_noise_reduction_lowers_noise_and_keeps_the_tone():
    rng = np.random.default_rng(3)
    noise = (rng.standard_normal((SR * 3, 1)) * 0.05).astype(np.float32)
    tone = np.zeros_like(noise)
    tone[SR:] = sine(1000, 2.0, 0.3)
    noisy = noise + tone
    sample = noise[:SR]
    out = run("noise_reduction", {"reduction": 30.0, "profile": "sample", "sensitivity": 5.0}, noisy,
              extra={"noise": sample})[:, 0]
    quiet_in = float(np.sqrt((noisy[2000:SR - 2000, 0] ** 2).mean()))
    quiet_out = float(np.sqrt((out[2000:SR - 2000] ** 2).mean()))
    assert db(quiet_out) < db(quiet_in) - 12
    assert db(amp_at(out[SR + 5000:], 1000)) == pytest.approx(db(amp_at(noisy[SR + 5000:, 0], 1000)), abs=1.5)
    auto = run("noise_reduction", {"reduction": 30.0}, noisy)[:, 0]
    assert db(float(np.sqrt((auto[2000:SR - 2000] ** 2).mean()))) < db(quiet_in) - 6


def test_noise_reduction_blocks_join_without_clicks():
    rng = np.random.default_rng(6)
    n = (1 << 20) + 50_000                                   # forces two processing blocks
    arr = (rng.standard_normal((n, 1)) * 0.02).astype(np.float32) + sine(500, n / SR, 0.3)[:n]
    out = run("noise_reduction", {"reduction": 20.0}, arr)[:, 0]
    seam = out[(1 << 20) - 200:(1 << 20) + 200]
    ref = out[(1 << 19) - 200:(1 << 19) + 200]
    assert float(np.abs(np.diff(seam)).max()) < 3 * float(np.abs(np.diff(ref)).max())


def test_stft_roundtrip_is_transparent():
    rng = np.random.default_rng(0)
    x = rng.standard_normal(10_000).astype(np.float32)
    np.testing.assert_allclose(dsp.istft(dsp.stft(x), len(x)), x, atol=1e-4)


def test_loudness_normalize_hits_target():
    rng = np.random.default_rng(5)
    arr = (rng.standard_normal((SR * 6, 2)) * 0.02).astype(np.float32)           # very quiet
    out = run("loudness", {"target": -16.0, "peak": -1.0}, arr)
    assert len(out) == len(arr)
    report = ffmpeg.analyze_stream([out], SR, 2, "loudnorm=I=-16:print_format=json")
    measured = float(json.loads(re.findall(r"\{[^{}]*input_i[^{}]*\}", report, re.S)[-1])["input_i"])
    assert measured == pytest.approx(-16.0, abs=1.0)


# ------------------------------------------------------------------ runner behaviour
def test_cancel_stops_an_effect_and_leaves_no_result():
    arr = np.zeros((SR * 120, 2), np.float32)
    token = CancelToken()
    threading.Timer(0.2, token.cancel).start()
    with pytest.raises(Cancelled):
        run("reverb", {"decay": 8.0}, arr, cancel=token)


def test_applying_an_effect_to_part_of_a_document_leaves_the_rest_alone():
    base = sine(440, 2.0, 0.5)
    doc = Document.from_source(Source.from_array(base, SR))
    a, b = SR // 2, SR
    clip = fx.run_effect(fx.get("invert"), None, doc.snapshot(), a, b)
    doc.replace(a, b, clip.pieces, "Invert")
    out = doc.read(0, doc.frames)
    np.testing.assert_array_equal(out[:a], base[:a])
    np.testing.assert_array_equal(out[a:b], -base[a:b])
    np.testing.assert_array_equal(out[b:], base[b:])
    doc.undo()
    np.testing.assert_array_equal(doc.read(0, doc.frames), base)


def test_progress_is_monotonic_and_ends_at_one():
    seen: list[float] = []
    run("normalize", None, sine(300, 1.0, 0.2, channels=2), progress=seen.append)
    assert seen[-1] == pytest.approx(1.0) and all(b >= a - 1e-9 for a, b in zip(seen, seen[1:]))


def test_render_preview_bypass_and_window():
    arr = sine(440, 5.0, 0.4)
    clip = clip_of(arr)
    prev = fx.render_preview(fx.get("amplify"), {"gain": 6.0, "prevent_clipping": False}, clip, 0, len(arr), seconds=1.0)
    assert len(prev) == SR and float(np.abs(prev).max()) == pytest.approx(0.4 * 1.9953, rel=1e-3)
    byp = fx.render_preview(fx.get("amplify"), None, clip, 0, len(arr), seconds=1.0, bypass=True)
    np.testing.assert_array_equal(byp, arr[:SR])
