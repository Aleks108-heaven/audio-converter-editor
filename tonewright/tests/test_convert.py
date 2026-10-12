from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pytest

from tonewright.core import analysis, ffmpeg, formats, tags
from tonewright.core.common import CancelToken, PauseToken
from tonewright.core.convert import ConvertItem, ConvertOptions, convert_all, plan_output, probe_item
from tonewright.core.document import Document
from tonewright.core.source import Source
from tonewright.core.tags import TagSet

from conftest import PNG_1X1, SR, make_audio_file, sine, write_wav


def opts(fmt="mp3", out_dir=None, **kw) -> ConvertOptions:
    return ConvertOptions(settings=formats.EncodeSettings(format=fmt, **kw.pop("enc", {})), out_dir=out_dir, **kw)


def decode(path: Path) -> np.ndarray:
    src = ffmpeg.decode_to_source(path)
    return src.read(0, src.frames)


@pytest.fixture
def tagged_flac(tmp_path) -> Path:
    folder = tmp_path / "src"
    folder.mkdir()
    path = make_audio_file(folder / "song.flac", np.stack([sine(440, 2), sine(660, 2)], axis=1))
    t = TagSet(title="Звук гитары", artist="Acoustic guitar", album="Album", year="2026", track="2",
               track_total="9", genre="Folk", cover=PNG_1X1)
    tags.write_tags(path, t)
    return path


@pytest.mark.parametrize("fmt", ["mp3", "m4a", "ogg", "opus", "flac", "wma"])
def test_tags_and_cover_survive_conversion(tagged_flac, tmp_path, fmt):
    out_dir = tmp_path / "out"
    res = convert_all([probe_item(tagged_flac)], opts(fmt, out_dir))[0]
    assert res.status == "done", res.message
    assert res.output.suffix == formats.FORMATS[fmt].ext
    got = tags.read_tags(res.output)
    assert (got.title, got.artist, got.album, got.year) == ("Звук гитары", "Acoustic guitar", "Album", "2026")
    assert (got.track, got.track_total) == ("2", "9")
    assert got.cover == PNG_1X1
    assert ffmpeg.probe(res.output).duration == pytest.approx(2.0, abs=0.15)


def test_keep_tags_off_and_tag_overrides(tagged_flac, tmp_path):
    item = probe_item(tagged_flac)
    item.tags.title = "Edited in the tag editor"
    res = convert_all([item], opts("mp3", tmp_path / "o", keep_tags=False))[0]
    assert tags.read_tags(res.output).is_empty()
    res = convert_all([item], opts("mp3", tmp_path / "o2"))[0]
    assert tags.read_tags(res.output).title == "Edited in the tag editor"


def test_wav_gets_ffmpeg_info_tags(tagged_flac, tmp_path):
    res = convert_all([probe_item(tagged_flac)], opts("wav", tmp_path / "o"))[0]
    assert res.status == "done"
    assert ffmpeg.probe(res.output).tags.get("title") == "Звук гитары"


def test_peak_normalize_from_file_and_from_document(tmp_path):
    quiet = write_wav(tmp_path / "quiet.wav", np.stack([sine(300, 2, amp=0.1)] * 2, axis=1))
    o = opts("wav", tmp_path / "o", normalize="peak", peak_target_db=-3.0)
    out = convert_all([probe_item(quiet)], o)[0].output
    assert float(np.abs(decode(out)).max()) == pytest.approx(10 ** (-3 / 20), rel=0.02)
    doc = Document.from_source(Source.from_array(np.stack([sine(300, 2, amp=0.2)] * 2, axis=1), SR))
    item = ConvertItem(None, "edited", 2.0, SR, 2, doc=doc.snapshot())
    o2 = opts("wav", tmp_path / "o2", normalize="peak", peak_target_db=-6.0)
    out = convert_all([item], o2)[0].output
    assert float(np.abs(decode(out)).max()) == pytest.approx(10 ** (-6 / 20), rel=0.02)


def test_lufs_normalize_reaches_target(tmp_path):
    rng = np.random.default_rng(3)
    src = write_wav(tmp_path / "noise.wav", (rng.standard_normal((SR * 8, 2)) * 0.02).astype(np.float32))
    out = convert_all([probe_item(src)], opts("wav", tmp_path / "o", normalize="lufs", lufs_target=-16.0))[0].output
    doc = Document.from_source(ffmpeg.decode_to_source(out))
    stats = analysis.measure(doc, 0, doc.frames)
    assert stats.loudness == pytest.approx(-16.0, abs=1.0)


def test_fades_applied_to_ends(tmp_path):
    src = write_wav(tmp_path / "loud.wav", np.full((SR * 4, 1), 0.5, np.float32))
    out = convert_all([probe_item(src)], opts("wav", tmp_path / "o", fade_in=1.0, fade_out=1.0))[0].output
    data = decode(out)[:, 0]
    assert abs(data[0]) < 0.01 and abs(data[-1]) < 0.01
    assert data[SR * 2] == pytest.approx(0.5, abs=0.01)
    assert 0.2 < data[SR // 2] < 0.3                 # halfway up the fade-in


def test_edited_document_is_what_gets_converted(tmp_path):
    base = np.stack([sine(440, 4.0), sine(440, 4.0)], axis=1)
    doc = Document.from_source(Source.from_array(base, SR))
    doc.delete(SR, 2 * SR)                      # 3 s remain
    item = ConvertItem(None, "edit", doc.duration, SR, 2, doc=doc.snapshot())
    res = convert_all([item], opts("wav", tmp_path / "o"))[0]
    assert res.status == "done"
    out = decode(res.output)
    assert len(out) == 3 * SR
    np.testing.assert_allclose(out, doc.read(0, doc.frames), atol=2 / 32768)
    item.frame_range = (0, SR)
    out2 = decode(convert_all([item], opts("wav", tmp_path / "o2"))[0].output)
    assert len(out2) == SR


def test_merge_joins_files_with_different_formats_and_gaps(tmp_path):
    a = write_wav(tmp_path / "a.wav", sine(440, 1.0, 22050)[:, None], 22050)               # mono 22.05k
    b = write_wav(tmp_path / "b.wav", np.stack([sine(660, 1.5)] * 2, axis=1), SR)           # stereo 44.1k
    items = [probe_item(a), probe_item(b)]
    o = opts("wav", tmp_path / "o", merge=True, merge_name="Both", gap_seconds=0.5)
    results = convert_all(items, o)
    assert all(r.status == "done" for r in results)
    out = results[0].output
    assert out.name == "Both.wav" and results[1].output == out
    info = ffmpeg.probe(out)
    assert info.first_audio.channels == 1 and info.first_audio.sample_rate == 22050   # taken from the first file
    assert info.duration == pytest.approx(1.0 + 0.5 + 1.5, abs=0.05)
    data = decode(out)[:, 0]
    assert float(np.abs(data[int(1.1 * 22050):int(1.4 * 22050)]).max()) < 1e-3          # the gap is silent


def test_merge_uses_edited_documents(tmp_path):
    doc = Document.from_source(Source.from_array(np.stack([sine(440, 2.0)] * 2, axis=1), SR))
    doc.delete(0, SR)
    other = write_wav(tmp_path / "o.wav", np.stack([sine(660, 1.0)] * 2, axis=1), SR)
    items = [ConvertItem(None, "doc", 1.0, SR, 2, doc=doc.snapshot()), probe_item(other)]
    out = convert_all(items, opts("wav", tmp_path / "out", merge=True))[0].output
    assert ffmpeg.probe(out).duration == pytest.approx(2.0, abs=0.03)


def test_conflict_policies_and_the_source_is_never_replaced(tmp_path):
    src = write_wav(tmp_path / "song.wav", sine(300, 1.0)[:, None])
    # converting a wav to wav next to itself must not touch the original
    out = convert_all([probe_item(src)], opts("wav", None))[0].output
    assert out.name == "song (converted).wav" and src.exists()
    assert decode(src).shape == decode(out).shape
    out_dir = tmp_path / "out"
    first = convert_all([probe_item(src)], opts("mp3", out_dir))[0].output
    stamp = first.stat().st_mtime_ns
    renamed = convert_all([probe_item(src)], opts("mp3", out_dir, conflict="rename"))[0].output
    assert renamed.name == "song (2).mp3" and first.stat().st_mtime_ns == stamp
    skipped = convert_all([probe_item(src)], opts("mp3", out_dir, conflict="skip"))[0]
    assert skipped.status == "skipped" and first.stat().st_mtime_ns == stamp
    overwritten = convert_all([probe_item(src)], opts("mp3", out_dir, conflict="overwrite"))[0]
    assert overwritten.output == first and first.stat().st_mtime_ns != stamp


def test_same_names_in_one_batch_do_not_collide(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    a = write_wav(tmp_path / "x" / "t.wav", sine(300, 0.5)[:, None])
    b = write_wav(tmp_path / "y" / "t.wav", sine(400, 0.5)[:, None])
    res = convert_all([probe_item(a), probe_item(b)], opts("mp3", tmp_path / "out"))
    assert {r.output.name for r in res} == {"t.mp3", "t (2).mp3"}


def test_naming_pattern_uses_tags(tagged_flac, tmp_path):
    res = convert_all([probe_item(tagged_flac)], opts("mp3", tmp_path / "o", pattern="{track} - {artist} - {title}"))[0]
    assert res.output.name == "02 - Acoustic guitar - Звук гитары.mp3"


def test_one_bad_file_does_not_stop_the_batch(tmp_path):
    good = write_wav(tmp_path / "good.wav", sine(300, 0.5)[:, None])
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"this is not audio at all" * 100)
    items = [probe_item(good), ConvertItem(bad, "bad", 1.0), probe_item(good)]
    res = convert_all(items, opts("mp3", tmp_path / "o"))
    assert [r.status for r in res] == ["done", "error", "done"]
    assert res[1].message
    assert not list((tmp_path / "o").glob("*part*"))


def test_progress_callbacks_are_ordered(tmp_path):
    files = [probe_item(write_wav(tmp_path / f"{i}.wav", sine(300, 1.0 + i)[:, None])) for i in range(3)]
    overall, results, per_item = [], [], []
    convert_all(files, opts("mp3", tmp_path / "o"), on_overall=overall.append,
                on_item_progress=lambda n, f: per_item.append((n, f)),
                on_result=lambda n, r: results.append((n, r.status)))
    assert overall == sorted(overall) and overall[-1] == pytest.approx(1.0)
    assert results == [(0, "done"), (1, "done"), (2, "done")]
    assert {n for n, _ in per_item} == {0, 1, 2}


def test_cancel_marks_remaining_items_and_leaves_no_partials(tmp_path):
    big = write_wav(tmp_path / "big.wav", np.tile(sine(200, 1.0), 400)[:, None])
    items = [probe_item(big), probe_item(big), probe_item(big)]
    token = CancelToken()
    res = convert_all(items, opts("flac", tmp_path / "o", enc={"compression": 12}),
                      on_item_progress=lambda n, f: token.cancel() if f > 0.05 else None, cancel=token)
    assert [r.status for r in res].count("done") <= 1
    assert all(r.status in ("done", "cancelled") for r in res)
    assert not list((tmp_path / "o").glob("*part*"))


def test_pause_stalls_progress_and_resume_completes(tmp_path):
    rng = np.random.default_rng(1)
    big = write_wav(tmp_path / "big.wav", (rng.standard_normal((SR * 600, 1)) * 0.1).astype(np.float32))
    item = probe_item(big)
    token = PauseToken()
    marks: list[float] = []
    box: dict = {}

    def work():
        box["res"] = convert_all([item], opts("flac", tmp_path / "o", enc={"compression": 8}),
                                 on_item_progress=lambda n, f: marks.append(f), pause=token)

    t = threading.Thread(target=work)
    t.start()
    deadline = time.time() + 30
    while not marks and time.time() < deadline:
        time.sleep(0.02)
    token.pause()
    time.sleep(0.4)
    frozen = len(marks)
    time.sleep(0.8)
    assert len(marks) <= frozen + 1, "progress kept advancing while paused"
    assert t.is_alive()
    token.resume()
    t.join(60)
    assert not t.is_alive() and box["res"][0].status == "done"


def test_plan_output_is_pure_and_respects_extensions(tmp_path):
    item = ConvertItem(tmp_path / "a b.wav", "a b", 1.0)
    o = opts("opus", tmp_path / "out")
    assert plan_output(item, o, set()) == tmp_path / "out" / "a b.opus"
    assert plan_output(item, opts("m4b", None), set()) == tmp_path / "a b.m4b"


def test_estimated_size_is_close_to_reality(tmp_path):
    src = write_wav(tmp_path / "t.wav", np.random.default_rng(2).standard_normal((SR * 5, 2)).astype(np.float32) * 0.1)
    for fmt in ("mp3", "m4a", "ogg", "wav"):
        o = opts(fmt, tmp_path / fmt)
        out = convert_all([probe_item(src)], o)[0].output
        est = formats.estimate_size(o.settings, 5.0, SR, 2)
        assert out.stat().st_size == pytest.approx(est, rel=0.35), fmt
