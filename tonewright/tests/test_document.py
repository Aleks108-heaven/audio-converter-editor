from __future__ import annotations

import numpy as np
import pytest

from tonewright.core.document import Clip, Document, Marker, diff_range, map_frame
from tonewright.core.source import Source, SourceWriter

SR = 8000


def make_doc(arr: np.ndarray) -> Document:
    return Document.from_source(Source.from_array(arr, SR))


def _ramp_doc(n: int = 1000) -> Document:
    return make_doc(np.arange(n, dtype=np.float32)[:, None])


def test_diff_range_finds_a_cut_a_paste_and_a_replacement():
    doc = _ramp_doc()
    before = doc.snapshot()
    doc.delete(300, 450)
    assert diff_range(before._pieces(), doc._pieces()) == (300, 450, 300)
    cut = doc.snapshot()
    doc.insert(100, Clip.from_source(Source.from_array(np.zeros((40, 1), np.float32), SR)))
    assert diff_range(cut._pieces(), doc._pieces()) == (100, 100, 140)
    pasted = doc.snapshot()
    doc.replace_with_source(500, 520, Source.from_array(np.ones((60, 1), np.float32), SR), "Replace")
    assert diff_range(pasted._pieces(), doc._pieces()) == (500, 520, 560)


def test_diff_range_works_backwards_for_undo_and_for_untouched_audio():
    doc = _ramp_doc()
    before = doc.snapshot()
    doc.delete(0, 10)
    doc.delete(900, 990)
    assert diff_range(doc._pieces(), before._pieces())[0] == 0
    after = doc.snapshot()
    doc.undo(2)
    assert diff_range(after._pieces(), doc._pieces()) == (0, 900, 1000)      # both ends moved: nothing is common
    same = doc.snapshot()
    assert diff_range(before._pieces(), same._pieces()) == (1000, 1000, 1000)
    assert diff_range([], []) == (0, 0, 0)


def test_diff_range_matches_what_really_changed_for_random_edits():
    rng = np.random.default_rng(3)
    doc = make_doc(rng.standard_normal((5000, 1)).astype(np.float32))
    for _ in range(60):
        n = doc.frames
        a = int(rng.integers(0, n))
        b = int(rng.integers(a, min(n, a + 400) + 1))
        before = doc.snapshot()
        flat_before = doc.read(0, n)
        if rng.random() < 0.5:
            doc.delete(a, b)
        else:
            doc.insert(int(rng.integers(0, n + 1)), doc.copy(a, b))
        flat_after = doc.read(0, doc.frames)
        lo, old_b, new_b = diff_range(before._pieces(), doc._pieces())
        np.testing.assert_array_equal(flat_before[:lo], flat_after[:lo])
        np.testing.assert_array_equal(flat_before[old_b:], flat_after[new_b:])
        assert old_b - new_b == n - doc.frames


def test_map_frame_follows_the_audio_through_an_edit():
    assert map_frame(100, 300, 450, 300) == 100            # before the edit: unchanged
    assert map_frame(700, 300, 450, 300) == 550            # after a cut: moves back by what was cut
    assert map_frame(350, 300, 450, 300) == 300            # inside what was cut: the join
    assert map_frame(700, 100, 100, 140) == 740            # after an insertion: moves on
    assert map_frame(510, 500, 520, 520) == 510            # an effect on a range keeps the position inside it
    assert map_frame(510, 500, 520, 560) == 530            # a replacement of another length: in proportion


def test_history_limit_drops_oldest_but_stays_consistent(monkeypatch):
    monkeypatch.setattr("tonewright.core.document.HISTORY_LIMIT", 5)
    doc = make_doc(np.arange(100, dtype=np.float32)[:, None])
    for _ in range(12):
        doc.delete(0, 1)
    assert doc.frames == 88 and len(doc.history_labels()) == 5 and doc.history_index == 4
    doc.undo(4)
    assert doc.frames == 92          # the four newest deletions undone, older ones are gone for good
    assert not doc.can_undo
    assert doc.modified               # the saved baseline fell out of history


def test_random_edits_match_numpy_model_and_undo(monkeypatch):
    monkeypatch.setattr("tonewright.core.document.HISTORY_LIMIT", 10_000)
    rng = np.random.default_rng(7)
    base = rng.standard_normal((6000, 2)).astype(np.float32)
    doc = make_doc(base)
    model = base.copy()
    snapshots = [model.copy()]
    for _ in range(250):
        n = len(model)
        op = int(rng.integers(0, 5))
        a = int(rng.integers(0, n + 1))
        b = int(rng.integers(a, min(n, a + 700) + 1))
        before = doc.history_index
        if op == 0:
            doc.delete(a, b)
            model = np.concatenate([model[:a], model[b:]])
        elif op == 1:
            clip = doc.copy(a, b)
            pos = int(rng.integers(0, n + 1))
            doc.insert(pos, clip)
            model = np.concatenate([model[:pos], model[a:b], model[pos:]])
        elif op == 2:
            doc.silence(a, b)
            model = model.copy()
            model[a:b] = 0
        elif op == 3 and n > 3000 and rng.random() < 0.3:
            doc.crop(a, b)
            model = model[a:b].copy()
        else:
            new = rng.standard_normal((int(rng.integers(0, 300)), 2)).astype(np.float32)
            doc.replace_with_source(a, b, Source.from_array(new, SR), "Replace")
            model = np.concatenate([model[:a], new, model[b:]])
        if doc.history_index != before:
            snapshots.append(model.copy())
        assert doc.frames == len(model)
        np.testing.assert_array_equal(doc.read(0, doc.frames), model)
    assert len(snapshots) == doc.history_index + 1
    for i in range(len(snapshots) - 1, -1, -1):
        doc.goto_history(i)
        np.testing.assert_array_equal(doc.read(0, doc.frames), snapshots[i])


def test_partial_reads_cross_piece_boundaries():
    rng = np.random.default_rng(3)
    base = rng.standard_normal((1000, 1)).astype(np.float32)
    doc = make_doc(base)
    doc.delete(200, 300)
    doc.insert(50, doc.copy(400, 450))
    flat = doc.read(0, doc.frames)
    for a, b in [(0, 10), (45, 120), (190, 700), (doc.frames - 5, doc.frames + 50), (-10, 5)]:
        np.testing.assert_array_equal(doc.read(a, b), flat[max(a, 0):max(min(b, len(flat)), 0)])


def test_modified_flag_and_labels():
    doc = make_doc(np.zeros((100, 1), np.float32))
    assert not doc.modified and not doc.can_undo
    doc.delete(10, 20)
    assert doc.modified and doc.can_undo and doc.undo_labels() == ["Delete"]
    doc.mark_saved()
    assert not doc.modified
    doc.undo()
    assert doc.modified and doc.can_redo and doc.redo_labels() == ["Delete"]
    doc.redo()
    assert not doc.modified


def test_edit_after_undo_discards_redo_branch():
    doc = make_doc(np.arange(100, dtype=np.float32)[:, None])
    doc.delete(0, 10)
    doc.delete(0, 10)
    doc.undo(2)
    assert doc.frames == 100 and len(doc.redo_labels()) == 2
    doc.silence(0, 5)
    assert doc.redo_labels() == []


def test_markers_follow_edits():
    doc = make_doc(np.zeros((1000, 1), np.float32))
    doc.add_marker(100, "a")
    doc.add_marker(500, "b")
    doc.add_marker(900, "c")
    doc.delete(200, 300)
    assert [(m.frame, m.name) for m in doc.markers] == [(100, "a"), (400, "b"), (800, "c")]
    doc.delete(350, 450)  # swallows "b"
    assert [m.name for m in doc.markers] == ["a", "c"]
    doc.insert(0, Clip.from_source(Source.from_array(np.zeros((50, 1), np.float32), SR)))
    assert doc.markers[0].frame == 150
    doc.crop(100, 700)
    assert [m.frame for m in doc.markers] == [50]
    doc.undo()
    assert len(doc.markers) == 2


def test_delete_ranges_is_one_undo_step_and_matches_sequential_deletes():
    rng = np.random.default_rng(9)
    base = rng.standard_normal((5000, 2)).astype(np.float32)
    doc = make_doc(base)
    for frame, name in [(100, "keep-a"), (600, "gone"), (1200, "keep-b"), (4500, "keep-c")]:
        doc.add_marker(frame, name)
    before = doc.history_index
    doc.delete_ranges([(2000, 2500), (500, 700), (650, 800), (4000, 4000), (9000, 9500)], "Remove silences")
    assert doc.history_index == before + 1
    expected = np.concatenate([base[:500], base[800:2000], base[2500:]])
    np.testing.assert_array_equal(doc.read(0, doc.frames), expected)
    assert [(m.name, m.frame) for m in doc.markers] == [("keep-a", 100), ("keep-b", 900), ("keep-c", 3700)]
    doc.undo()
    np.testing.assert_array_equal(doc.read(0, doc.frames), base)
    doc.delete_ranges([(0, 0)])
    assert not doc.can_undo or doc.history_index == before


def test_clip_is_zero_copy_and_format_checked():
    doc = make_doc(np.ones((100, 2), np.float32))
    clip = doc.copy(10, 40)
    assert clip.frames == 30
    other = Document.from_source(Source.from_array(np.ones((10, 1), np.float32), SR))
    with pytest.raises(ValueError):
        other.insert(0, clip)


def test_replace_all_changes_format():
    doc = make_doc(np.ones((100, 2), np.float32))
    doc.add_marker(50, "m")
    doc.replace_all(Source.from_array(np.zeros((200, 1), np.float32), 16000), "Resample")
    assert (doc.sr, doc.channels, doc.frames) == (16000, 1, 200)
    assert doc.markers[0].frame == 100


def test_listeners_report_kind_of_change():
    doc = make_doc(np.zeros((100, 1), np.float32))
    seen: list[str] = []
    doc.add_listener(seen.append)
    doc.delete(0, 10)
    doc.add_marker(5, "x")
    doc.undo()
    doc.mark_saved()
    assert seen == ["content", "markers", "markers", "saved"]


def _brute_peaks(flat: np.ndarray, start: int, stop: int, width: int):
    fpp = (stop - start) / width
    mins = np.zeros((flat.shape[1], width), np.float32)
    maxs = np.zeros_like(mins)
    for i in range(width):
        a = int(round(start + i * fpp))
        b = max(a + 1, int(round(start + (i + 1) * fpp)))
        seg = flat[a:b]
        mins[:, i] = seg.min(axis=0)
        maxs[:, i] = seg.max(axis=0)
    return mins, maxs


def test_peaks_exact_when_zoomed_in():
    rng = np.random.default_rng(5)
    doc = make_doc(rng.standard_normal((20000, 2)).astype(np.float32))
    doc.delete(3000, 4000)
    doc.insert(500, doc.copy(9000, 9200))
    flat = doc.read(0, doc.frames)
    for start, stop, width in [(0, 1000, 500), (100, 5100, 80), (0, doc.frames, 300)]:
        mn, mx = doc.peaks(start, stop, width)
        if (stop - start) / width < 128:
            bmn, bmx = _brute_peaks(flat, start, stop, width)
            np.testing.assert_allclose(mn, bmn)
            np.testing.assert_allclose(mx, bmx)
        assert mn.shape == mx.shape == (2, width)
        assert (mn <= mx).all()


def test_peaks_find_every_spike_at_all_zooms():
    n = 600_000
    sig = np.zeros((n, 2), np.float32)
    rng = np.random.default_rng(11)
    spikes = sorted(rng.choice(np.arange(1000, n - 1000), 12, replace=False))
    for i, f in enumerate(spikes):
        sig[f, i % 2] = 1.0 if i % 3 else -1.0
    doc = make_doc(sig)
    doc.delete(100_000, 120_000)           # pieces: spikes in the cut region vanish
    doc.insert(10, doc.copy(300_000, 301_000))
    flat = doc.read(0, doc.frames)
    for width in (50, 400, 1500, 4000):
        mn, mx = doc.peaks(0, doc.frames, width)
        fpp = doc.frames / width
        for f in np.nonzero(np.abs(flat).max(axis=1) > 0.5)[0]:
            ch = int(np.argmax(np.abs(flat[f])))
            p = min(width - 1, int(f / fpp))
            lo, hi = max(0, p - 1), min(width, p + 2)
            if flat[f, ch] > 0:
                assert mx[ch, lo:hi].max() > 0.99, (width, f)
            else:
                assert mn[ch, lo:hi].min() < -0.99, (width, f)
        assert mx.max() <= flat.max() + 1e-6 and mn.min() >= flat.min() - 1e-6


def test_peaks_follow_loud_and_quiet_passages_to_within_a_sliver_of_a_pixel():
    """Each pixel shows the audio under it, give or take a few percent of its width at either edge. (They used to be
    cut at the floor of blocks up to half a pixel wide, so on music with sharp transitions whole columns showed the
    neighbouring passage: the peaks were off by up to 15 % of full scale.)"""
    rng = np.random.default_rng(23)
    parts, total = [], 0
    while total < 1_500_000:
        n = int(rng.integers(3_000, 120_000))
        loud = float(rng.choice([0.03, 0.2, 0.9]))
        parts.append((rng.standard_normal((n, 2)) * loud * 0.4).clip(-1, 1).astype(np.float32))
        total += n
    doc = make_doc(np.concatenate(parts))
    doc.delete(250_000, 300_000)
    flat = doc.read(0, doc.frames)
    for width in (10, 100, 600, 1000, 1400):
        mn, mx = doc.peaks(0, doc.frames, width)
        fpp = doc.frames / width
        edges = np.rint(np.arange(width + 1) * fpp).astype(np.int64)
        for i in range(width):
            a, b = int(edges[i]), int(edges[i + 1])
            slack = -(-(b - a) // 16)
            if b - a < 1024:
                inner = outer = flat[a:b]
            else:
                inner, outer = flat[a + slack:b - slack], flat[max(0, a - slack):b + slack]
            assert np.all(mx[:, i] <= outer.max(axis=0) + 1e-6) and np.all(mn[:, i] >= outer.min(axis=0) - 1e-6), (width, i)
            assert np.all(mx[:, i] >= inner.max(axis=0) - 1e-6) and np.all(mn[:, i] <= inner.min(axis=0) + 1e-6), (width, i)


def test_peaks_handles_tiny_pieces():
    rng = np.random.default_rng(2)
    doc = make_doc(rng.standard_normal((50000, 1)).astype(np.float32))
    for pos in range(100, 40000, 997):
        doc.insert(pos, doc.copy(pos + 3, pos + 5))
    mn, mx = doc.peaks(0, doc.frames, 200)
    flat = doc.read(0, doc.frames)
    assert mx.max() == pytest.approx(flat.max()) and mn.min() == pytest.approx(flat.min())


def test_silence_source_costs_nothing_and_peaks():
    doc = make_doc(np.ones((1000, 1), np.float32))
    doc.silence(0, 1000)
    assert not doc.read(0, 1000).any()
    mn, mx = doc.peaks(0, 1000, 10)
    assert not mn.any() and not mx.any()


def test_source_writer_spills_to_disk_and_reads_back():
    w = SourceWriter(SR, 2, spill_bytes=1000)
    rng = np.random.default_rng(1)
    parts = [rng.standard_normal((300, 2)).astype(np.float32) for _ in range(5)]
    for p in parts:
        w.write(p)
    src = w.finish()
    assert type(src._store).__name__ == "_FileStore"
    flat = np.concatenate(parts)
    np.testing.assert_array_equal(src.read(0, src.frames), flat)
    np.testing.assert_array_equal(src.read(250, 700), flat[250:700])
    path = src._store.path
    assert path.exists()
    src.close()
    assert not path.exists()


def test_source_writer_rejects_wrong_channel_count():
    w = SourceWriter(SR, 2)
    with pytest.raises(ValueError):
        w.write(np.zeros((10, 1), np.float32))
