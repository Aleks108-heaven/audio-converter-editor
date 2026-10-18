"""The multi-track mixer engine: placement, level/pan/mute/solo, trims, splits, fades, effect spans, history, projects."""
from __future__ import annotations

import json

import numpy as np
import pytest

from tonewright.core import effects as fx
from tonewright.core.common import CancelToken, Cancelled
from tonewright.core.document import Clip
from tonewright.core.mixer import (BLOCK, MIN_CLIP, MixProject, balance_gains, custom_filters_in, fade_envelope)
from tonewright.core.source import Source

SR = 44100


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
    fade_in = fade_envelope(0, n, n, n, 0)
    fade_out = fade_envelope(0, n, n, 0, n)
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
    expected = fade_envelope(0, n, n, BLOCK + 500, BLOCK + 500)
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
