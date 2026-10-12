from __future__ import annotations

import numpy as np
import pytest
from mutagen.mp4 import MP4

from tonewright.core import ffmpeg, tags
from tonewright.core.audiobook import (BookOptions, Chapter, Part, analyze_pauses, book_length, create_audiobook,
                                       part_offsets, plan_chapters, write_ffmetadata)
from tonewright.core.common import CancelToken, Cancelled
from tonewright.core.convert import ConvertItem, probe_item
from tonewright.core.document import Document
from tonewright.core.source import Source
from tonewright.core.tags import TagSet

from conftest import PNG_1X1, SR, make_audio_file, sine, write_wav


def fake_parts(durations, titles=None, markers=None):
    parts = []
    for i, d in enumerate(durations):
        item = ConvertItem(None, f"f{i}", d)
        parts.append(Part(item, (titles or [""] * len(durations))[i], list((markers or {}).get(i, []))))
    return parts


# ------------------------------------------------------------------ planning
def test_offsets_and_length_include_gaps():
    assert part_offsets([10, 20, 30], 2.0) == [0, 12, 34]
    assert book_length([10, 20, 30], 2.0) == 64.0
    assert book_length([], 1.0) == 0.0


def test_split_none_and_files():
    parts = fake_parts([60, 90, 30], titles=["Intro", "", "End"])
    o = BookOptions(title="My Book", split="none")
    assert plan_chapters([60, 90, 30], parts, o) == [Chapter(0.0, "My Book")]
    o = BookOptions(split="files", keep_title_tags=True)
    got = plan_chapters([60, 90, 30], parts, o)
    assert [(c.start, c.title) for c in got] == [(0, "Intro"), (60, "Chapter 2"), (150, "End")]
    o = BookOptions(split="files", keep_title_tags=False, chapter_label="Part")
    assert [c.title for c in plan_chapters([60, 90, 30], parts, o)] == ["Part 1", "Part 2", "Part 3"]


def test_split_count_and_time_are_even():
    parts = fake_parts([600])
    got = plan_chapters([600], parts, BookOptions(split="count", chapter_count=4, pause_detection=False))
    assert [c.start for c in got] == [0, 150, 300, 450]
    got = plan_chapters([600], parts, BookOptions(split="time", chapter_seconds=180, pause_detection=False))
    assert [c.start for c in got] == [0, 180, 360, 540] or [c.start for c in got] == [0, 180, 360]
    got = plan_chapters([620], parts, BookOptions(split="time", chapter_seconds=180, pause_detection=False))
    assert [c.start for c in got] == [0, 180, 360, 540]
    assert [c.start for c in plan_chapters([600], parts, BookOptions(split="count", chapter_count=1))] == [0]


def test_tiny_last_chapter_is_merged_away():
    got = plan_chapters([370], fake_parts([370]), BookOptions(split="time", chapter_seconds=180, pause_detection=False))
    assert [c.start for c in got] == [0, 180]           # a 10 s tail does not get its own chapter


def test_boundaries_snap_to_the_nearest_pause():
    pauses = [(17.0, 19.0), (38.0, 40.5), (55.0, 56.0)]
    o = BookOptions(split="count", chapter_count=3, pause_detection=True)
    got = plan_chapters([60], fake_parts([60]), o, pauses)
    assert [c.start for c in got] == [0, pytest.approx(18.0), pytest.approx(39.25)]
    far = [(5.0, 6.0)]                                   # nothing within reach: ideal times are kept
    got = plan_chapters([60], fake_parts([60]), o, far)
    assert [c.start for c in got] == [0, pytest.approx(20.0), pytest.approx(40.0)]
    o.pause_detection = False
    assert [c.start for c in plan_chapters([60], fake_parts([60]), o, pauses)] == [0, 20, 40]


def test_markers_become_chapters():
    parts = fake_parts([100, 100], markers={0: [(30.0, "Act I")], 1: [(10.0, ""), (50.0, "Finale")]})
    got = plan_chapters([100, 100], parts, BookOptions(split="markers"))
    assert [(c.start, c.title) for c in got] == [(0, "Chapter 1"), (30, "Act I"), (110, "Chapter 3"), (150, "Finale")]
    got = plan_chapters([100], fake_parts([100], markers={0: [(0.0, "Start")]}), BookOptions(split="markers"))
    assert [(c.start, c.title) for c in got] == [(0, "Start")]
    assert plan_chapters([100], fake_parts([100]), BookOptions(split="markers"))[0].start == 0


def test_close_or_trailing_chapters_are_dropped():
    parts = fake_parts([10, 10, 10])
    got = plan_chapters([10, 10, 10], parts, BookOptions(split="files", gap_seconds=0))
    assert len(got) == 3
    marks = fake_parts([60], markers={0: [(10.0, "a"), (10.4, "b"), (59.9, "tail")]})
    got = plan_chapters([60], marks, BookOptions(split="markers"))
    assert [c.title for c in got] == ["Chapter 1", "a"]


# ------------------------------------------------------------------ pause analysis
def speech_like(segments, total):
    """Tone bursts at (start, end) seconds with silence elsewhere."""
    data = np.zeros(int(total * SR), np.float32)
    for s, e in segments:
        data[int(s * SR):int(e * SR)] = sine(300, e - s, amp=0.3)
    return data


def test_analyze_pauses_finds_silences_in_files_and_documents(tmp_path):
    sig = speech_like([(0, 4), (6, 10)], 10)
    path = write_wav(tmp_path / "s.wav", sig[:, None])
    o = BookOptions(silence_min_seconds=1.0)
    pauses, durations = analyze_pauses([Part(probe_item(path))], o)
    assert durations[0] == pytest.approx(10.0, abs=0.05)
    assert len(pauses) == 1 and pauses[0][0] == pytest.approx(4.0, abs=0.1) and pauses[0][1] == pytest.approx(6.0, abs=0.1)
    doc = Document.from_source(Source.from_array(sig[:, None], SR))
    item = ConvertItem(None, "d", 10.0, SR, 1, doc=doc.snapshot())
    pauses2, _ = analyze_pauses([Part(item), Part(item)], BookOptions(silence_min_seconds=1.0, gap_seconds=1.0))
    assert len(pauses2) == 3                             # one inside each part and the gap between them
    assert pauses2[1][0] == pytest.approx(10.0, abs=0.1)


# ------------------------------------------------------------------ building real files
def build(tmp_path, durations, opts, tones=None, titles=None, markers=None, **kw):
    parts = []
    for i, d in enumerate(durations):
        f = tones[i] if tones else 220 * (i + 1)
        path = write_wav(tmp_path / f"p{i}.wav", np.stack([sine(f, d, amp=0.3)] * 2, axis=1))
        item = probe_item(path)
        item.tags = TagSet(title=(titles or [""] * len(durations))[i])
        parts.append(Part(item, item.tags.title, list((markers or {}).get(i, []))))
    return create_audiobook(parts, opts, tmp_path / "book.m4b", **kw)


def test_m4b_has_chapters_tags_cover_and_audiobook_flag(tmp_path):
    o = BookOptions(title="The Book", author="A. Writer", narrator="N. Reader", year="2026", cover=PNG_1X1,
                    split="files", bitrate=64)
    out, chapters = build(tmp_path, [1.5, 2.0, 1.0], o, titles=["Prologue", "Middle", "Epilogue"])
    info = ffmpeg.probe(out)
    assert info.first_audio.codec == "aac" and "mp4" in info.format_name
    assert info.duration == pytest.approx(4.5, abs=0.15)
    assert [(round(s, 1), t) for s, e, t in info.chapters] == [(0.0, "Prologue"), (1.5, "Middle"), (3.5, "Epilogue")]
    assert [round(c.start, 1) for c in chapters] == [0.0, 1.5, 3.5]
    mp4 = MP4(out)
    assert mp4["stik"] == [2]
    assert mp4["\xa9nam"] == ["The Book"] and mp4["\xa9alb"] == ["The Book"]
    assert mp4["\xa9ART"] == ["A. Writer"] and mp4["aART"] == ["A. Writer"] and mp4["\xa9wrt"] == ["N. Reader"]
    assert mp4["\xa9day"] == ["2026"] and mp4["\xa9gen"] == ["Audiobook"]
    assert bytes(mp4["covr"][0]) == PNG_1X1
    assert not list(tmp_path.glob("*tonewright*"))


def test_gap_between_files_shifts_chapters(tmp_path):
    o = BookOptions(title="B", split="files", gap_seconds=1.0, keep_title_tags=False)
    out, chapters = build(tmp_path, [1.0, 1.0, 1.0], o)
    assert [round(c.start, 1) for c in chapters] == [0.0, 2.0, 4.0]
    assert ffmpeg.probe(out).duration == pytest.approx(5.0, abs=0.15)


def test_mono_and_sample_rate_options(tmp_path):
    o = BookOptions(title="B", split="none", mono=True, sample_rate=22050, bitrate=32)
    out, _ = build(tmp_path, [1.0, 1.0], o)
    st = ffmpeg.probe(out).first_audio
    assert (st.channels, st.sample_rate) == (1, 22050)


def test_chapter_titles_with_special_characters_survive(tmp_path):
    o = BookOptions(title="Tough; title = #1 \\ test", split="files")
    nasty = ["Ch. 1: a=b; c #d \\e", "Глава 2 — «конец»"]
    out, _ = build(tmp_path, [1.0, 1.0], o, titles=nasty)
    info = ffmpeg.probe(out)
    assert [t for _, _, t in info.chapters] == nasty
    assert MP4(out)["\xa9nam"] == ["Tough; title = #1 \\ test"]


def test_split_by_count_snaps_to_real_pauses(tmp_path):
    sig = speech_like([(0, 17), (19, 38), (40.5, 60)], 60)
    path = write_wav(tmp_path / "long.wav", sig[:, None])
    o = BookOptions(title="Long", split="count", chapter_count=3, pause_detection=True,
                    bitrate=32, mono=True, sample_rate=16000)
    out, chapters = create_audiobook([Part(probe_item(path))], o, tmp_path / "book.m4b")
    assert [round(c.start, 1) for c in chapters] == [0.0, pytest.approx(18.0, abs=0.3), pytest.approx(39.25, abs=0.3)]
    assert len(ffmpeg.probe(out).chapters) == 3


def test_edited_documents_and_markers_go_into_the_book(tmp_path):
    doc = Document.from_source(Source.from_array(np.stack([sine(300, 4.0)] * 2, axis=1), SR))
    doc.delete(0, SR)                                    # 3 s remain
    item = ConvertItem(None, "doc", 3.0, SR, 2, doc=doc.snapshot())
    other = probe_item(write_wav(tmp_path / "o.wav", np.stack([sine(500, 2.0)] * 2, axis=1)))
    parts = [Part(item, "Edited", [(1.0, "Midpoint")]), Part(other, "Plain")]
    o = BookOptions(title="Mixed", split="markers")
    out, chapters = create_audiobook(parts, o, tmp_path / "m.m4b")
    assert [(round(c.start, 1), c.title) for c in chapters] == [(0.0, "Chapter 1"), (1.0, "Midpoint")]
    assert ffmpeg.probe(out).duration == pytest.approx(5.0, abs=0.15)


def test_a_reviewed_chapter_list_is_used_verbatim(tmp_path):
    mine = [Chapter(0.0, "Start"), Chapter(1.2, "Second")]
    out, chapters = build(tmp_path, [1.0, 1.0], BookOptions(title="B", split="files"), chapters=mine)
    assert chapters == mine
    assert [(round(s, 1), t) for s, e, t in ffmpeg.probe(out).chapters] == [(0.0, "Start"), (1.2, "Second")]


def test_cancel_leaves_no_files_behind(tmp_path):
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        build(tmp_path, [1.0, 1.0], BookOptions(title="B"), cancel=token)
    assert not (tmp_path / "book.m4b").exists()
    assert not list(tmp_path.glob("*tonewright*"))


def test_empty_book_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        create_audiobook([], BookOptions(), tmp_path / "x.m4b")


def test_ffmetadata_format(tmp_path):
    path = tmp_path / "m.txt"
    write_ffmetadata(path, BookOptions(title="T", author="A"), [Chapter(0, "One"), Chapter(2.5, "Two")], 6.0)
    text = path.read_text(encoding="utf-8")
    assert text.startswith(";FFMETADATA1") and "START=2500" in text and "END=6000" in text
    assert text.count("[CHAPTER]") == 2 and "artist=A" in text
