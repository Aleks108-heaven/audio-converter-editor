"""Folders and file names the way real libraries have them: spaces, '&', Cyrillic, CJK, accents."""
from __future__ import annotations

import numpy as np
import pytest

from tonewright.core import audiobook, ffmpeg, formats, tags
from tonewright.core.convert import ConvertOptions, convert_all, probe_item
from tonewright.core.tags import TagSet

from conftest import PNG_1X1, SR, make_audio_file, sine

NAMES = ["на гитаре.mp3", "Dalai Lama reciting ... & more.mp3", "心经 Heart Sutra.mp3", "Café – Déjà vu #1 (live).mp3",
         "100% [raw] {take 2}; final.mp3"]


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "Audio Converter & Editor" / "Музыка (copy) 2026"
    d.mkdir(parents=True)
    return d


@pytest.mark.parametrize("name", NAMES)
def test_probe_tag_and_convert_with_awkward_names(folder, name):
    src = make_audio_file(folder / name, np.stack([sine(440, 1.5), sine(660, 1.5)], axis=1))
    tags.write_tags(src, TagSet(title="Звук гитары", artist="Acoustic guitar", year="2026", cover=PNG_1X1))
    item = probe_item(src)
    assert item.tags.title == "Звук гитары" and item.duration == pytest.approx(1.5, abs=0.1)
    out_dir = folder / "out & converted"
    for fmt in ("flac", "m4a", "ogg"):
        res = convert_all([item], ConvertOptions(settings=formats.EncodeSettings(format=fmt), out_dir=out_dir,
                                                 pattern="{artist} - {title}"))[0]
        assert res.status == "done", res.message
        assert res.output.name == f"Acoustic guitar - Звук гитары{formats.FORMATS[fmt].ext}"
        got = tags.read_tags(res.output)
        assert (got.title, got.year, got.cover) == ("Звук гитары", "2026", PNG_1X1)


def test_audiobook_from_awkward_paths(folder):
    files = [make_audio_file(folder / n, np.stack([sine(300 + 50 * i, 1.0)] * 2, axis=1)) for i, n in enumerate(NAMES[:3])]
    parts = [audiobook.Part(probe_item(f), f.stem) for f in files]
    out, chapters = audiobook.create_audiobook(parts, audiobook.BookOptions(title="Книга & Co", split="files"),
                                               folder / "Книга & Co.m4b")
    assert [c.title for c in chapters] == [f.stem for f in files]
    assert [t for _, _, t in ffmpeg.probe(out).chapters] == [f.stem for f in files]
    assert out.name == "Книга & Co.m4b"
