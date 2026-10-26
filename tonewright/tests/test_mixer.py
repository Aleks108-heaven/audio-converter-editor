"""The multi-track mixer engine: placement, level/pan/mute/solo, trims, splits, fades, effect spans, history, projects."""
from __future__ import annotations

import json

import numpy as np
import pytest

from tonewright.core import effects as fx
from tonewright.core.common import CancelToken, Cancelled
from tonewright.core.document import Clip
from tonewright.core.mixer import (BLOCK, ENVELOPE_MAX, MAX_ENVELOPE_POINTS, MIN_CLIP, MIN_GAP, MixProject,
                                    add_point, balance_gains, clean_envelope, custom_filters_in, envelope_at,
                                    envelope_gain, fade_envelope, g_to_gain, gain_to_g, move_point, peak_of,
                                    points_from_fractions, remove_point, saturate_clip)
from tonewright.core.source import Source

SR = 44100

# the hand-drawn sketch (sketch_points_check.svg): (t = fraction of the clip, g = height of the line, 0.5 = unchanged)
SKETCH = [[0, 0.5], [0.08, 0.15], [0.11, 0.5], [0.14, 0.37], [0.32, 0.38], [0.37, 0.22], [0.38, 0.82], [0.45, 0.79],
          [0.53, 0.01], [0.78, 0], [0.84, 0.77], [0.9, 0.77], [0.94, 0.5]]


def middle_crossings(sketch) -> list[float]:
    """The t at which the line passes through the middle between two points (not at a point)."""
    out = []
    for (t0, g0), (t1, g1) in zip(sketch, sketch[1:]):
        if (g0 - 0.5) * (g1 - 0.5) < 0:
            out.append(t0 + (t1 - t0) * (0.5 - g0) / (g1 - g0))
    return out


SKETCH_CROSSINGS = middle_crossings(SKETCH)


def stereo(values, n: int = SR // 2) -> Clip:
    """A clip whose left/right channels are constant at the given values."""
    left, right = values if isinstance(values, tuple) else (values, values)
    data = np.empty((n, 2), np.float32)
    data[:, 0], data[:, 1] = left, right
    return Clip.from_source(Source.from_array(data, SR))


def ramp(n: int) -> Clip:
    data = np.stack([np.arange(n, dtype=np.float32) / n] * 2, axis=1)
    return Clip.from_source(Source.from_array(data, SR))


def audio(clip: Clip) -> np.ndarray:
    return clip.read(0, clip.frames)


def fades(first: int, count: int, length: int, fade_in: int, fade_out: int) -> np.ndarray:
    """`fade_envelope` for a fade that is known to exist (it returns None when there is none)."""
    env = fade_envelope(first, count, length, fade_in, fade_out)
    assert env is not None
    return env


@pytest.fixture
def project() -> MixProject:
    return MixProject(SR)


# ------------------------------------------------------------------ placement and summing
def test_one_clip_renders_unchanged(project):
    line = project.add_line()
    clip = ramp(5000)
    project.add_clip(line.id, clip, 0, "a")
    np.testing.assert_allclose(audio(project.render()), audio(clip), atol=1e-7)


def test_clip_start_leaves_silence_before_it(project):
    line = project.add_line()
    project.add_clip(line.id, stereo(0.5, 1000), 3000)
    out = audio(project.render())
    assert len(out) == 4000 and not out[:3000].any() and np.allclose(out[3000:], 0.5)


def test_clips_on_two_lines_are_summed_where_they_overlap(project):
    a, b = project.add_line(), project.add_line()
    project.add_clip(a.id, stereo(0.25, 4000), 0)
    project.add_clip(b.id, stereo(0.5, 2000), 1000)
    out = audio(project.render())
    assert np.allclose(out[:1000], 0.25) and np.allclose(out[1000:3000], 0.75) and np.allclose(out[3000:], 0.25)


def test_clips_crossing_block_boundaries_are_exact(project):
    line = project.add_line()
    clip = ramp(BLOCK * 2 + 777)
    project.add_clip(line.id, clip, BLOCK - 100)
    out = audio(project.render())
    np.testing.assert_allclose(out[BLOCK - 100:], audio(clip), atol=1e-7)


def test_total_frames_and_empty_project(project):
    assert project.total_frames == 0 and project.empty
    line = project.add_line()
    project.add_clip(line.id, stereo(0.1, 100), 50)
    assert project.total_frames == 150 and project.duration == pytest.approx(150 / SR)


def test_wrong_format_and_empty_audio_are_rejected(project):
    line = project.add_line()
    mono = Clip.from_source(Source.from_array(np.zeros((100, 1), np.float32), SR))
    with pytest.raises(ValueError):
        project.add_clip(line.id, mono, 0)
    other_rate = Clip.from_source(Source.from_array(np.zeros((100, 2), np.float32), 22050))
    with pytest.raises(ValueError):
        project.add_clip(line.id, other_rate, 0)
    with pytest.raises(ValueError):
        project.add_clip(line.id, stereo(0.1, 0), 0)


# ------------------------------------------------------------------ level, pan, mute, solo
def test_line_volume_and_clip_gain_in_db(project):
    line = project.add_line()
    project.set_line(line.id, volume_db=-6.0)
    clip = project.add_clip(line.id, stereo(1.0, 100), 0, gain_db=-6.0)
    out = audio(project.render())
    assert out[0, 0] == pytest.approx(10 ** (-12 / 20), rel=1e-5)
    project.set_clip(clip.id, gain_db=100)                       # clamped to the allowed range
    assert project.clip(clip.id).gain_db == 12.0
    project.set_line(line.id, volume_db=-999)
    assert project.line(line.id).volume_db == -60.0


@pytest.mark.parametrize("pan,left,right", [(0.0, 1.0, 1.0), (1.0, 0.0, 1.0), (-1.0, 1.0, 0.0), (0.5, 0.5, 1.0), (-0.25, 1.0, 0.75)])
def test_pan_is_a_balance_control(project, pan, left, right):
    assert balance_gains(pan) == (left, right)
    line = project.add_line()
    project.set_line(line.id, pan=pan)
    project.add_clip(line.id, stereo(1.0, 10), 0)
    out = audio(project.render())
    assert out[0, 0] == pytest.approx(left) and out[0, 1] == pytest.approx(right)


def test_mute_and_solo(project):
    a, b, c = project.add_line(), project.add_line(), project.add_line()
    for line, value in ((a, 0.1), (b, 0.2), (c, 0.4)):
        project.add_clip(line.id, stereo(value, 10), 0)
    assert audio(project.render())[0, 0] == pytest.approx(0.7)
    project.set_line(b.id, mute=True)
    assert audio(project.render())[0, 0] == pytest.approx(0.5)
    project.set_line(a.id, solo=True)                            # only soloed lines play
    assert audio(project.render())[0, 0] == pytest.approx(0.1)
    project.set_line(a.id, mute=True)                            # a muted line stays silent even if soloed
    assert audio(project.render())[0, 0] == pytest.approx(0.0)
    assert project.audible_lines() == set()


# ------------------------------------------------------------------ editing clips
def test_move_clip_between_lines_and_never_before_zero(project):
    a, b = project.add_line(), project.add_line()
    clip = project.add_clip(a.id, stereo(0.3, 100), 500)
    project.move_clip(clip.id, -40, b.id)
    assert (project.clip(clip.id).start, project.clip(clip.id).line) == (0, b.id)


def test_trim_start_and_end_follow_the_audio(project):
    line = project.add_line()
    clip = project.add_clip(line.id, ramp(10000), 2000)
    c = project.clip(clip.id)
    project.trim_start(c.id, 3000)
    assert (c.start, c.offset, c.length) == (3000, 1000, 9000)
    out = audio(project.render())
    np.testing.assert_allclose(out[3000:], audio(clip.seq)[1000:], atol=1e-7)
    project.trim_start(c.id, 0)                                  # cannot extend past the start of the audio
    assert (c.start, c.offset) == (2000, 0)
    project.trim_end(c.id, 4000)
    assert c.length == 2000
    project.trim_end(c.id, 10 ** 9)                              # cannot extend past the end of the audio
    assert c.length == 10000
    project.trim_start(c.id, 10 ** 9)                            # a clip never disappears
    assert c.length == MIN_CLIP


def test_split_gives_two_clips_that_render_like_the_original(project):
    line = project.add_line()
    clip = project.add_clip(line.id, ramp(8000), 1000)
    before = audio(project.render())
    project.set_clip(clip.id, fade_in=500, fade_out=700)
    reference = audio(project.render())
    left, right = project.split_clip(clip.id, 5000)
    assert (left.end, right.start) == (5000, 5000) and right.fade_in == 0 and left.fade_out == 0 and right.fade_out == 700
    np.testing.assert_allclose(audio(project.render()), reference, atol=1e-6)
    assert before.shape == reference.shape
    assert project.split_clip(left.id, left.start + 5) is None   # too close to an edge to split


def test_duplicate_and_delete(project):
    line = project.add_line()
    clip = project.add_clip(line.id, stereo(0.2, 100), 0)
    copy = project.duplicate_clip(clip.id)
    assert copy.start == 100 and copy.id != clip.id and project.total_frames == 200
    project.delete_clip(clip.id)
    assert [c.id for c in project.clips] == [copy.id]


def test_removing_a_line_removes_its_clips(project):
    a, b = project.add_line(), project.add_line()
    project.add_clip(a.id, stereo(0.2, 100), 0)
    keep = project.add_clip(b.id, stereo(0.2, 100), 0)
    project.remove_line(a.id)
    assert [c.id for c in project.clips] == [keep.id] and [l.id for l in project.lines] == [b.id]
    assert project.add_line().name == "Line 1"                   # names are reused


# ------------------------------------------------------------------ fades and transitions
def test_fade_envelope_is_equal_power_and_exact_at_the_ends():
    n = 1000
    fade_in = fades(0, n, n, n, 0)
    fade_out = fades(0, n, n, 0, n)
    assert fade_in[0] == pytest.approx(0.0) and fade_in[-1] == pytest.approx(1.0, abs=2e-3)
    assert fade_out[0] == pytest.approx(1.0) and fade_out[-1] == pytest.approx(0.0, abs=2e-3)
    np.testing.assert_allclose(fade_in ** 2 + fade_out ** 2, 1.0, atol=1e-5)
    assert fade_envelope(0, 10, 10, 0, 0) is None


def test_fades_are_applied_per_block_correctly(project):
    line = project.add_line()
    n = BLOCK * 3
    project.add_clip(line.id, stereo(1.0, n), 0)
    project.set_clip(project.clips[0].id, fade_in=BLOCK + 500, fade_out=BLOCK + 500)
    out = audio(project.render())[:, 0]
    expected = fades(0, n, n, BLOCK + 500, BLOCK + 500)
    np.testing.assert_allclose(out, expected, atol=1e-6)


def test_crossfade_two_overlapping_clips(project):
    line = project.add_line()
    first = project.add_clip(line.id, stereo(0.5, 10000), 0)
    second = project.add_clip(line.id, stereo(0.5, 10000), 8000)       # overlaps by 2000 frames
    assert project.overlapping(second.id) == [first]
    assert project.crossfade(second.id)
    assert project.clip(second.id).fade_in == 2000 and project.clip(first.id).fade_out == 2000
    out = audio(project.render())[:, 0]
    assert np.allclose(out[:8000], 0.5) and np.allclose(out[10000:], 0.5)
    mid = out[9000]                                              # equal power: sin(pi/4) + cos(pi/4) of 0.5
    assert mid == pytest.approx(0.5 * 2 ** 0.5, rel=1e-3)
    assert not project.crossfade(first.id) or project.clip(first.id).fade_in >= 0


def test_crossfade_needs_something_to_fade_with(project):
    line = project.add_line()
    clip = project.add_clip(line.id, stereo(0.5, 100), 0)
    assert project.crossfade(clip.id) is False


def test_clips_on_different_lines_can_crossfade_too(project):
    a, b = project.add_line(), project.add_line()
    project.add_clip(a.id, stereo(0.5, 10000), 0)
    second = project.add_clip(b.id, stereo(0.5, 10000), 7000)
    assert project.crossfade(second.id) and project.clip(second.id).fade_in == 3000


# ------------------------------------------------------------------ the volume curve of a clip
def test_a_volume_curve_joins_its_points_with_straight_lines_and_holds_the_ends(project):
    line = project.add_line()
    clip = project.add_clip(line.id, stereo(1.0, 4000), 0)
    project.set_envelope(clip.id, [(1000, 0.0), (2000, 1.0), (3000, 0.5)])
    out = audio(project.render())[:, 0]
    assert np.allclose(out[:1000], 0.0) and out[1500] == pytest.approx(0.5, abs=1e-3)
    assert out[2000] == pytest.approx(1.0) and out[2500] == pytest.approx(0.75, abs=1e-3) and np.allclose(out[3000:], 0.5)


def test_a_clip_without_a_curve_is_untouched_and_the_curve_scales_with_fades_and_levels(project):
    line = project.add_line()
    clip = project.add_clip(line.id, stereo(0.5, 2000), 0)
    assert clip.envelope == () and envelope_at(clip.envelope, 100) == 1.0
    project.set_envelope(clip.id, [(0, 0.5)])                                      # a single point is a constant level
    project.set_clip(clip.id, gain_db=-6.0206, fade_in=1000)
    out = audio(project.render())[:, 0]
    expected = 0.5 * 0.5 * 0.5 * fades(0, 2000, 2000, 1000, 0)
    np.testing.assert_allclose(out, expected, atol=1e-4)


def test_the_curve_is_exact_across_block_boundaries(project):
    line = project.add_line()
    n = BLOCK * 3
    clip = project.add_clip(line.id, stereo(1.0, n), 0)
    points = [(BLOCK - 5, 1.0), (BLOCK * 2 + 9, 0.0)]
    project.set_envelope(clip.id, points)
    out = audio(project.render())[:, 0]
    k = np.arange(n)
    np.testing.assert_allclose(out, np.interp(k, [p[0] for p in points], [p[1] for p in points]), atol=1e-6)


def test_a_curve_can_fade_a_clip_to_silence_and_back_for_a_smooth_transition(project):
    a, b = project.add_line(), project.add_line()
    first = project.add_clip(a.id, stereo(0.5, 6000), 0)
    second = project.add_clip(b.id, stereo(0.5, 6000), 3000)
    project.set_envelope(first.id, [(3000, 1.0), (6000, 0.0)])
    project.set_envelope(second.id, [(0, 0.0), (3000, 1.0)])
    out = audio(project.render())[:, 0]
    assert np.allclose(out[:3000], 0.5) and np.allclose(out[3000:9000], 0.5, atol=2e-3)      # straight cross-fade: steady


def test_curve_points_are_cleaned_sorted_and_limited():
    assert clean_envelope([(30, 1.0), (10, 0.5), (10, 0.7)]) == ((10, 0.7), (30, 1.0))
    assert clean_envelope([(5, 9.0), (6, -3.0)]) == ((5, ENVELOPE_MAX), (6, 0.0))
    assert clean_envelope([("x", 1), (1,), None, (2, float("nan")), (3, float("inf")), (4.9, "0.25")]) == ((4, 0.25),)
    assert clean_envelope(None) == () and clean_envelope(5) == () and clean_envelope([]) == ()
    assert len(clean_envelope([(i, 1.0) for i in range(MAX_ENVELOPE_POINTS + 50)])) == MAX_ENVELOPE_POINTS


def test_trimming_keeps_the_curve_on_the_same_part_of_the_audio_and_never_loses_points(project):
    line = project.add_line()
    clip = project.add_clip(line.id, ramp(4000), 100)
    project.set_envelope(clip.id, [(1000, 0.2), (3000, 1.0)])
    before = audio(project.render())
    project.trim_start(clip.id, 1600)                                              # cuts 1500 frames off the front
    assert clip.envelope == ((-500, 0.2), (1500, 1.0))
    after = audio(project.render())                                                # the audio has not moved, only its start
    assert not after[:1600].any()
    np.testing.assert_allclose(after[1600:], before[1600:], atol=1e-6)
    project.trim_start(clip.id, 100)                                                # ...and bring them back
    assert clip.envelope == ((1000, 0.2), (3000, 1.0))
    np.testing.assert_allclose(audio(project.render()), before, atol=1e-6)


def test_splitting_gives_each_half_its_part_of_the_curve(project):
    line = project.add_line()
    clip = project.add_clip(line.id, stereo(1.0, 4000), 0)
    project.set_envelope(clip.id, [(0, 0.0), (4000, 1.0)])
    before = audio(project.render())
    left, right = project.split_clip(clip.id, 1000)
    np.testing.assert_allclose(audio(project.render()), before, atol=1e-6)
    assert right.envelope == ((-1000, 0.0), (3000, 1.0)) and left.envelope == ((0, 0.0), (4000, 1.0))


def test_the_curve_is_one_undo_step_and_survives_a_duplicate(project):
    line = project.add_line()
    clip = project.add_clip(line.id, stereo(1.0, 1000), 0)
    project.checkpoint("Volume curve")
    project.set_envelope(clip.id, [(0, 1.0), (500, 0.0)])
    copy = project.duplicate_clip(clip.id)
    assert copy.envelope == clip.envelope
    project.undo()
    assert project.clip(clip.id).envelope == ()


def test_the_curve_is_saved_in_the_project_and_bad_data_in_a_file_is_ignored(project, tmp_path):
    line = project.add_line()
    source = ramp(5000)
    clip = project.add_clip(line.id, source, 0, "a", "C:/music/a.wav")
    project.set_envelope(clip.id, [(100, 0.25), (4000, 1.5)])
    path = tmp_path / "mix.twmix"
    project.save(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["clips"][0]["envelope"] == [[100, 0.25], [4000, 1.5]]
    loaded = MixProject.from_dict(data, loader({"C:/music/a.wav": source}))
    assert loaded.clips[0].envelope == ((100, 0.25), (4000, 1.5))
    np.testing.assert_allclose(audio(loaded.render()), audio(project.render()), atol=1e-6)
    for junk in ("loud", 7, [["a", "b"]], {"x": 1}, [[1, 2, 3]]):
        data["clips"][0]["envelope"] = junk
        assert MixProject.from_dict(data, loader({"C:/music/a.wav": source})).clips[0].envelope == ()
    del data["clips"][0]["envelope"]                                               # a project from before the curve existed
    assert MixProject.from_dict(data, loader({"C:/music/a.wav": source})).clips[0].envelope == ()


def test_a_live_mix_hears_a_changed_curve(project):
    line = project.add_line()
    clip = project.add_clip(line.id, stereo(1.0, 3000), 0)
    project.add_effect("amplify", amplify_params(0.0), 0, 3000)
    cache: dict = {}
    first = project.live(cache)
    project.set_envelope(clip.id, [(0, 0.5)])
    second = project.live(cache)
    assert np.allclose(first.read(0, 100), 1.0) and np.allclose(second.read(0, 100), 0.5)
    assert first._spans[0].key != second._spans[0].key                           # the effect is rendered again, not reused


# ------------------------------------------------------------------ the point rule (pure functions)
N = 100_000


def levels(points, n: int = N) -> np.ndarray:
    """The curve at every frame 0..n (1 where there is no curve)."""
    gains = envelope_gain(0, n + 1, points)
    return np.ones(n + 1, np.float32) if gains is None else gains


def test_height_on_the_line_is_a_linear_multiplier_with_the_middle_unchanged_and_the_top_double():
    assert (g_to_gain(0.0), g_to_gain(0.5), g_to_gain(1.0)) == (0.0, 1.0, 2.0)
    assert g_to_gain(-1) == 0.0 and g_to_gain(7) == 2.0
    assert g_to_gain(0.25) == pytest.approx(0.5) and gain_to_g(g_to_gain(0.8)) == pytest.approx(0.8)
    assert gain_to_g(1.0) == 0.5 and gain_to_g(99) == 1.0


def test_the_sketch_gives_13_points_and_none_where_the_line_only_crosses_the_middle():
    points = points_from_fractions(SKETCH, N)
    assert len(points) == 13 and [f for f, _ in points] == sorted({f for f, _ in points})
    assert [i + 1 for i, (_, g) in enumerate(SKETCH) if g == 0.5] == [1, 3, 13]      # the ones that sit on the middle are vertices
    assert len(SKETCH_CROSSINGS) == 3
    frames = {f for f, _ in points}
    assert all(round(t * N) not in frames for t in SKETCH_CROSSINGS)                  # no point at a crossing
    for t in SKETCH_CROSSINGS:                                                         # yet the line does pass through unity there
        assert envelope_at(points, t * N) == pytest.approx(1.0, abs=0.01)


def test_the_curve_is_straight_between_points_and_holds_before_the_first_and_after_the_last():
    points = points_from_fractions(SKETCH, N)
    assert envelope_at(points, 0.08 * N) == pytest.approx(0.3) and envelope_at(points, 0.53 * N) == pytest.approx(0.02)
    mid = envelope_at(points, 0.04 * N)                                               # halfway from point 1 (g .5) to point 2 (g .15)
    assert mid == pytest.approx((1.0 + 0.3) / 2, abs=1e-3)
    assert envelope_at(points, -500) == 1.0 and envelope_at(points, N * 3) == 1.0     # first and last points are both on the middle
    held = points_from_fractions([[0.2, 0.25], [0.6, 0.75]], N)
    assert envelope_at(held, 0) == 0.5 and envelope_at(held, N) == 1.5


@pytest.mark.parametrize("frame", [0, 1, 7000, 8000, 11_000, 31_999, 53_000, 60_000, 93_999, 94_000, 99_999, N])
def test_adding_a_point_on_the_line_does_not_change_the_curve(frame):
    points = points_from_fractions(SKETCH, N)
    added = add_point(points, frame, N)
    assert (len(added) == len(points) + 1) == (frame not in {f for f, _ in points})
    np.testing.assert_allclose(levels(added), levels(points), atol=1e-6)


def test_a_point_added_outside_the_first_and_last_points_holds_their_level():
    points = points_from_fractions([[0.2, 0.25], [0.6, 0.75]], N)
    for frame in (5000, 90_000):
        np.testing.assert_allclose(levels(add_point(points, frame, N)), levels(points), atol=1e-6)
    assert add_point((), 500, N) == ((500, 1.0),)                                      # on a flat line: the middle


def test_a_point_is_not_added_twice_at_one_time_or_beyond_the_limit():
    points = ((1000, 0.5), (2000, 1.5))
    assert add_point(points, 1000, N) == points and add_point(points, N + 50, N)[-1][0] == N
    full = tuple((i * 10, 1.0) for i in range(MAX_ENVELOPE_POINTS))
    assert add_point(full, 5, N) == full


def test_moving_a_point_changes_the_curve_only_between_its_neighbours():
    points = points_from_fractions(SKETCH, N)
    moved = move_point(points, 6, points[6][0], 0.1, N)                                # point 7: from g .82 down to .05
    before, after = levels(points), levels(moved)
    lo, hi = points[5][0], points[7][0]
    np.testing.assert_array_equal(before[:lo + 1], after[:lo + 1])
    np.testing.assert_array_equal(before[hi:], after[hi:])
    assert not np.allclose(before[lo:hi], after[lo:hi])
    assert moved[:6] == points[:6] and moved[7:] == points[7:]


def test_a_moved_point_stays_inside_its_neighbours_and_never_shares_a_time_with_one():
    points = ((1000, 1.0), (5000, 0.5), (9000, 1.0))
    assert move_point(points, 1, 99_999, 0.5, N)[1][0] == 9000 - MIN_GAP
    assert move_point(points, 1, -5, 0.5, N)[1][0] == 1000 + MIN_GAP
    assert move_point(points, 0, -5, 0.5, N)[0][0] == 0 and move_point(points, 2, 10 ** 9, 0.5, N)[2][0] == N
    assert move_point(points, 1, 5000, 9.0, N)[1] == (5000, ENVELOPE_MAX) and move_point(points, 1, 5000, -1, N)[1] == (5000, 0.0)
    for i in range(3):
        frames = [f for f, _ in move_point(points, i, 5000, 1.0, N)]
        assert all(b - a >= MIN_GAP for a, b in zip(frames, frames[1:]))
    tight = ((10, 1.0), (10 + MIN_GAP, 0.5), (10 + 2 * MIN_GAP, 1.0))                  # no room at all: the time stays
    assert move_point(tight, 1, 500, 0.2, N)[1] == (10 + MIN_GAP, 0.2)


def test_removing_a_point_joins_its_neighbours_with_a_straight_line_and_the_last_one_gives_the_flat_middle():
    points = points_from_fractions(SKETCH, N)
    cut = remove_point(points, 6)
    assert len(cut) == 12
    (f0, v0), (f1, v1) = points[5], points[7]
    k = np.arange(f0, f1 + 1)
    np.testing.assert_allclose(levels(cut)[f0:f1 + 1], v0 + (v1 - v0) * (k - f0) / (f1 - f0), atol=1e-5)
    assert remove_point(((500, 0.2),), 0) == () and envelope_gain(0, 10, remove_point(((500, 0.2),), 0)) is None
    assert envelope_at(remove_point(((500, 0.2),), 0), 123) == 1.0


def test_collinear_points_are_kept():
    line = ((0, 1.0), (1000, 1.0), (2000, 1.0))
    assert remove_point(line, 2) == line[:2] and add_point(line, 1500, N) == (*line[:2], (1500, 1.0), line[2])


# ------------------------------------------------------------------ layers, saturation, one gain for preview and export
def loud_sine(amp: float, n: int = SR) -> Clip:
    wave = (amp * np.sin(np.arange(n) * 2 * np.pi * 440 / SR)).astype(np.float32)
    return Clip.from_source(Source.from_array(np.stack([wave, wave], axis=1), SR))


def test_the_same_file_twice_at_the_middle_sums_to_double_with_no_wraparound(project):
    flat = points_from_fractions([[0, 0.5], [1, 0.5]], SR)
    for amp in (0.4, 0.9):
        project = MixProject(SR)
        source = loud_sine(amp)
        for _ in range(2):
            clip = project.add_clip(project.add_line().id, source, 0)
            project.set_envelope(clip.id, flat)
        raw = audio(project.render(saturate=False))
        np.testing.assert_allclose(raw, 2 * audio(source), atol=1e-6)               # exactly twice the file
        assert peak_of(project.render(saturate=False)) == pytest.approx(2 * amp, abs=1e-3)
        out = audio(project.render())
        assert np.abs(out).max() <= 1.0
        np.testing.assert_allclose(out, np.clip(2 * audio(source), -1, 1), atol=1e-6)      # flattened at the top, never wrapped
        assert (np.sign(out) == np.sign(raw)).all()


def test_saturating_leaves_a_quiet_clip_alone_and_flattens_a_loud_one_without_touching_the_original():
    quiet, loud = loud_sine(0.5), loud_sine(1.7)
    assert saturate_clip(quiet) is quiet
    flat = saturate_clip(loud)
    assert np.abs(audio(flat)).max() == 1.0 and peak_of(loud) == pytest.approx(1.7, abs=1e-3) and peak_of(flat) == 1.0


def test_preview_and_export_are_the_same_sound_with_curves_levels_and_saturation(project):
    sketch = points_from_fractions(SKETCH, SR)
    a, b = project.add_line(), project.add_line()
    first = project.add_clip(a.id, loud_sine(0.8), 0)
    second = project.add_clip(b.id, loud_sine(0.7), SR // 5, gain_db=6.0)
    project.set_envelope(first.id, sketch)
    project.set_envelope(second.id, points_from_fractions([[0, 0.5], [0.4, 1.0], [0.8, 0.9]], SR))
    project.set_clip(first.id, fade_in=2000)
    exported = audio(project.render())
    assert np.abs(exported).max() <= 1.0 and peak_of(project.render(saturate=False)) > 1.0       # saturation really was in play
    live = project.live()
    np.testing.assert_allclose(live.read(0, live.frames), exported, atol=1e-6)
    pieces = [live.read(i, i + 7777) for i in range(0, live.frames, 7777)]                       # and in the blocks the card asks for
    np.testing.assert_allclose(np.concatenate(pieces), exported, atol=1e-6)


def test_adding_a_point_does_not_change_the_rendered_sound(project):
    clip = project.add_clip(project.add_line().id, loud_sine(0.6), 0)
    project.set_envelope(clip.id, points_from_fractions(SKETCH, clip.length))
    before = audio(project.render())
    for frame in (3000, 20_000, 33_000, 44_000, 1):
        project.set_envelope(clip.id, add_point(clip.envelope, frame, clip.length))
    assert len(clip.envelope) == 18
    np.testing.assert_allclose(audio(project.render()), before, atol=1e-5)


# ------------------------------------------------------------------ effect spans (gain: -6 dB is x0.501)
def amplify_params(gain: float) -> dict:
    params = fx.get("amplify").defaults()
    params["gain"] = gain
    params["prevent_clipping"] = False
    return params


def test_an_effect_span_changes_only_its_range(project):
    line = project.add_line()
    project.add_clip(line.id, stereo(0.4, 10000), 0)
    span = project.add_effect("amplify", amplify_params(-6.0), 2000, 6000)
    out = audio(project.render())[:, 0]
    assert np.allclose(out[:2000], 0.4) and np.allclose(out[6000:], 0.4)
    assert np.allclose(out[2000:6000], 0.4 * 10 ** (-6 / 20), atol=1e-4)
    project.set_effect(span.id, mute=True)
    assert np.allclose(audio(project.render())[:, 0], 0.4)
    assert np.allclose(audio(project.render(effects=False))[:, 0], 0.4)


def test_effect_that_changes_the_length_is_refused(project):
    with pytest.raises(ValueError):
        project.add_effect("reverb", {"tail": True}, 0, 1000)


def test_effect_span_resize_and_move(project):
    span = project.add_effect("amplify", amplify_params(3.0), 1000, 2000)
    project.move_effect(span.id, 5000)
    assert (span.start, span.end) == (5000, 6000)
    project.resize_effect(span.id, start=5500)
    project.resize_effect(span.id, end=5500)                      # never shorter than the minimum
    assert span.end - span.start == MIN_CLIP
    project.delete_effect(span.id)
    assert project.effects == []


# ------------------------------------------------------------------ the mix as it is played while being edited
def wait_for(cond, timeout: float = 10.0) -> bool:
    import time
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def busy_project(project):
    a, b = project.add_line(), project.add_line()
    project.add_clip(a.id, ramp(BLOCK * 2 + 500), 0, "a")
    c2 = project.add_clip(b.id, stereo((0.2, -0.3), 40000), 20000, "b", gain_db=-3.0)
    project.set_clip(c2.id, fade_in=3000, fade_out=5000)
    project.set_line(b.id, pan=0.5)
    return project


def test_the_live_mix_is_the_final_mix_block_by_block(project):
    busy_project(project)
    live = project.live()
    assert live.frames == project.total_frames and live.sr == SR and live.channels == 2
    whole = audio(project.render())
    for a, b in ((0, 1000), (19_000, 25_000), (BLOCK - 5, BLOCK + 5), (live.frames - 700, live.frames + 900)):
        np.testing.assert_allclose(live.read(a, b), whole[a:min(b, live.frames)], atol=1e-6)
    assert live.read(live.frames, live.frames + 10).shape == (0, 2)
    assert not live.pending


def test_the_live_mix_is_frozen_when_the_project_changes_afterwards(project):
    busy_project(project)
    live = project.live()
    before = live.read(0, 30000).copy()
    project.set_line(project.lines[0].id, mute=True)
    project.move_clip(project.clips[1].id, 0)
    project.delete_clip(project.clips[0].id)
    np.testing.assert_array_equal(live.read(0, 30000), before)
    assert not np.allclose(project.live().read(0, 30000), before)


def test_the_live_mix_renders_effects_in_the_background_and_matches_render(project):
    line = project.add_line()
    project.add_clip(line.id, stereo(0.4, 30000), 0)
    project.add_effect("amplify", amplify_params(-6.0), 5000, 15000)
    project.add_effect("amplify", amplify_params(-6.0), 10000, 20000)           # overlaps: applied on top of the first
    live = project.live()
    assert live.pending
    assert np.allclose(live.read(6000, 7000), 0.4)                               # not rendered yet: plays dry, never waits
    live.start_effects(delay=0.0)
    assert wait_for(lambda: not live.pending) and live.error is None
    whole = audio(project.render())
    np.testing.assert_allclose(live.read(0, 30000), whole, atol=1e-5)
    assert np.allclose(live.read(11000, 12000)[:, 0], 0.4 * 0.25, atol=1e-3)     # both spans on the overlap


def test_a_muted_effect_is_not_part_of_the_live_mix(project):
    line = project.add_line()
    project.add_clip(line.id, stereo(0.4, 10000), 0)
    span = project.add_effect("amplify", amplify_params(-6.0), 0, 10000)
    project.set_effect(span.id, mute=True)
    live = project.live()
    assert not live.pending
    assert np.allclose(live.read(0, 10000), 0.4)


def test_finished_effect_spans_are_reused_by_the_next_live_mix_unless_something_under_them_changed(project):
    line = project.add_line()
    other = project.add_line()
    project.add_clip(line.id, stereo(0.4, 30000), 0)
    far = project.add_clip(other.id, stereo(0.1, 5000), 25000)
    project.add_effect("amplify", amplify_params(-6.0), 2000, 8000)
    cache: dict = {}
    first = project.live(cache)
    first.start_effects(delay=0.0)
    assert wait_for(lambda: not first.pending)
    project.move_clip(far.id, 26000)                                           # nowhere near the effect
    second = project.live(cache)
    assert not second.pending                                                  # ready at once, nothing rendered again
    project.set_clip(project.clips[0].id, gain_db=-1.0)                        # the clip under the effect
    third = project.live(cache)
    assert third.pending
    for live in (first, second, third):
        live.close()


def test_closing_a_live_mix_stops_its_background_work(project):
    line = project.add_line()
    project.add_clip(line.id, stereo(0.4, 30000), 0)
    project.add_effect("amplify", amplify_params(-6.0), 0, 30000)
    live = project.live()
    live.start_effects(delay=5.0)
    live.close()
    assert wait_for(lambda: not live._thread.is_alive(), 2.0)
    assert live.pending and live.error is None


# ------------------------------------------------------------------ history
def test_undo_and_redo_restore_the_arrangement_but_share_the_audio(project):
    line = project.add_line()
    project.checkpoint("Add clip")
    clip = project.add_clip(line.id, stereo(0.3, 100), 0)
    seq = clip.seq
    project.checkpoint("Move")
    project.move_clip(clip.id, 700)
    assert project.undo_label == "Move"
    assert project.undo() == "Move" and project.clip(clip.id).start == 0
    assert project.undo() == "Add clip" and project.clips == []
    assert not project.can_undo and project.can_redo
    assert project.redo() == "Add clip" and project.clip(clip.id).seq is seq
    assert project.redo() == "Move" and project.clip(clip.id).start == 700 and not project.can_redo


def test_a_new_change_clears_redo(project):
    line = project.add_line()
    project.checkpoint("one")
    project.add_clip(line.id, stereo(0.3, 100), 0)
    project.undo()
    project.checkpoint("two")
    assert not project.can_redo


def test_history_is_limited(project):
    for i in range(300):
        project.checkpoint(str(i))
    assert len(project._undo) == 100


def test_version_changes_with_every_edit(project):
    versions = {project.version}
    line = project.add_line()
    versions.add(project.version)
    clip = project.add_clip(line.id, stereo(0.3, 100), 0)
    versions.add(project.version)
    project.set_line(line.id, pan=0.3)
    versions.add(project.version)
    project.move_clip(clip.id, 5)
    versions.add(project.version)
    assert len(versions) == 5


# ------------------------------------------------------------------ rendering control
def test_render_reports_progress_and_can_be_cancelled(project):
    line = project.add_line()
    project.add_clip(line.id, stereo(0.2, BLOCK * 4), 0)
    seen = []
    project.render(progress=seen.append)
    assert seen[-1] == pytest.approx(1.0) and seen == sorted(seen)
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        project.render(cancel=token)


def test_peak_estimate_warns_about_clipping(project):
    a, b = project.add_line(), project.add_line()
    project.add_clip(a.id, stereo(0.8, 100), 0)
    assert project.peak_estimate() == pytest.approx(0.8)
    project.add_clip(b.id, stereo(0.8, 100), 0)
    assert project.peak_estimate() == pytest.approx(1.6)


def test_master_level_scales_the_whole_mix(project):
    line = project.add_line()
    project.add_clip(line.id, stereo(0.8, 100), 0)
    assert audio(project.render(master=0.5))[0, 0] == pytest.approx(0.4)


def test_a_partial_range_can_be_rendered(project):
    line = project.add_line()
    project.add_clip(line.id, ramp(10000), 0)
    out = audio(project.render_tracks(2000, 4000))
    np.testing.assert_allclose(out, audio(project.clips[0].seq)[2000:4000], atol=1e-7)


# ------------------------------------------------------------------ projects on disk
def loader(files: dict):
    def load(path: str, sr: int):
        return files[path]
    return load


def test_a_project_round_trips_through_json(project, tmp_path):
    a, b = project.add_line(), project.add_line()
    project.set_line(a.id, volume_db=-3.0, pan=0.25, mute=False)
    project.set_line(b.id, solo=True, name="Drums")
    first = ramp(8000)
    project.add_clip(a.id, first, 100, "song a", "C:/music/a.wav")
    c2 = project.add_clip(b.id, first, 2000, "song a again", "C:/music/a.wav", gain_db=-2.0)
    project.trim_start(c2.id, 2500)
    project.set_clip(c2.id, fade_in=300, fade_out=200)
    project.add_effect("amplify", amplify_params(2.0), 500, 3000)
    path = tmp_path / "mix.twmix"
    project.save(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["format"] == "tonewright-mix" and len(data["clips"]) == 2

    loaded = MixProject.from_dict(data, loader({"C:/music/a.wav": first}))
    assert [(l.name, l.volume_db, l.pan, l.solo) for l in loaded.lines] == [(l.name, l.volume_db, l.pan, l.solo) for l in project.lines]
    assert [(c.start, c.offset, c.length, c.gain_db, c.fade_in, c.fade_out, c.path) for c in loaded.clips] == \
           [(c.start, c.offset, c.length, c.gain_db, c.fade_in, c.fade_out, c.path) for c in project.clips]
    assert [(e.effect, e.start, e.end) for e in loaded.effects] == [("amplify", 500, 3000)]
    np.testing.assert_allclose(audio(loaded.render()), audio(project.render()), atol=1e-6)
    assert loaded.add_line().id > max(c.id for c in loaded.clips)            # new ids never clash


def test_clips_without_a_file_cannot_be_saved(project, tmp_path):
    line = project.add_line()
    project.add_clip(line.id, stereo(0.3, 100), 0, "recorded take")
    with pytest.raises(ValueError, match="recorded take"):
        project.save(tmp_path / "x.twmix")


def test_loading_rejects_foreign_and_newer_projects():
    with pytest.raises(ValueError):
        MixProject.from_dict({"format": "something else"}, loader({}))
    with pytest.raises(ValueError):
        MixProject.from_dict({"format": "tonewright-mix", "version": 99}, loader({}))


def test_loading_survives_unknown_effects_and_orphan_clips():
    data = {"format": "tonewright-mix", "version": 1, "sample_rate": SR,
            "lines": [{"id": 1, "name": "L"}],
            "clips": [{"id": 2, "line": 1, "path": "p", "start": 0, "offset": 99999, "length": 99999},
                      {"id": 3, "line": 77, "path": "p", "start": 0}],
            "effects": [{"id": 4, "effect": "does-not-exist", "start": 0, "end": 10}]}
    project = MixProject.from_dict(data, loader({"p": stereo(0.1, 1000)}))
    assert len(project.clips) == 1 and project.clips[0].length >= 1 and project.clips[0].offset < 1000
    assert project.effects == []


# ------------------------------------------------------------------ a project file is untrusted input
def project_with_custom_filter(chain: str) -> dict:
    return {"format": "tonewright-mix", "version": 1, "sample_rate": SR, "lines": [{"id": 1, "name": "L"}],
            "clips": [{"id": 2, "line": 1, "path": "p", "start": 0, "offset": 0, "length": 1000}],
            "effects": [{"id": 3, "effect": "custom_filter", "params": {"chain": chain}, "start": 0, "end": 500},
                        {"id": 4, "effect": "amplify", "params": amplify_params(2.0), "start": 0, "end": 500}]}


def test_a_project_cannot_run_a_custom_ffmpeg_filter_unless_it_is_trusted():
    chain = "amovie=C\\\\:/secret.wav,volume=1"
    data = project_with_custom_filter(chain)
    audio_in = {"p": stereo(0.1, 1000)}
    refused = MixProject.from_dict(data, loader(audio_in))
    assert [e.effect for e in refused.effects] == ["amplify"]               # the harmless effect survives
    assert refused.skipped_effects == ["custom_filter"]
    allowed = MixProject.from_dict(data, loader(audio_in), trust_custom_filters=True)
    assert [e.effect for e in allowed.effects] == ["custom_filter", "amplify"] and allowed.skipped_effects == []


def test_custom_filters_in_finds_chains_and_ignores_malformed_data():
    assert custom_filters_in(project_with_custom_filter("volume=2")) == ["volume=2"]
    for bad in (None, [], "text", {"effects": "x"}, {"effects": [1, None, {"effect": "amplify"}]}):
        assert custom_filters_in(bad) == []
    assert custom_filters_in({"effects": [{"effect": "custom_filter", "params": "oops"}]}) == [""]
