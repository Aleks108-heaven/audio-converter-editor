from __future__ import annotations

import numpy as np
import pytest
from mutagen.mp4 import MP4

from tonewright.core import ffmpeg, formats, tags
from tonewright.core.tags import KEEP, TagSet

from conftest import PNG_1X1, SR, make_audio_file, sine


def tone(seconds: float = 1.0, channels: int = 2) -> np.ndarray:
    return np.repeat(sine(440, seconds)[:, None], channels, axis=1)


# ------------------------------------------------------------------ formats
EXPECTED_CODEC = {"mp3": "mp3", "m4a": "aac", "m4b": "aac", "aac": "aac", "ogg": "vorbis", "opus": "opus",
                  "wma": "wmav2", "flac": "flac", "wav": "pcm_s16le", "aiff": "pcm_s16be", "mp2": "mp2",
                  "amr": "amr_nb", "amrwb": "amr_wb"}


def _writable(key: str):
    """A format, skipped when the installed FFmpeg has no encoder for it (the macOS build has no AMR)."""
    missing = not ffmpeg.has_encoder(formats.FORMATS[key].codec)
    return pytest.param(key, marks=pytest.mark.skipif(missing, reason=f"this FFmpeg cannot encode {key}"))


@pytest.mark.parametrize("key", [_writable(k) for k in formats.FORMATS])
def test_every_format_encodes_and_probes_as_expected(tmp_path, key):
    spec = formats.FORMATS[key]
    path = tmp_path / f"t{spec.ext}"
    make_audio_file(path, tone(1.0), SR, fmt=key)
    info = ffmpeg.probe(path)
    st = info.first_audio
    rate, channels = formats.resolve(formats.EncodeSettings(format=key), SR, 2)
    assert st.codec == EXPECTED_CODEC[key]
    assert (st.sample_rate, st.channels) == (rate, channels)
    assert info.duration == pytest.approx(1.0, abs=0.12)


def test_resolve_picks_supported_rates_and_layouts():
    s = formats.EncodeSettings(format="mp3")
    assert formats.resolve(s, 96000, 2) == (48000, 2)
    assert formats.resolve(formats.EncodeSettings(format="opus"), 44100, 2) == (48000, 2)
    assert formats.resolve(formats.EncodeSettings(format="amr"), 44100, 2) == (8000, 1)
    assert formats.resolve(formats.EncodeSettings(format="amrwb"), 48000, 2) == (16000, 1)
    assert formats.resolve(formats.EncodeSettings(format="mp3", sample_rate=22050, channels=1), 44100, 2) == (22050, 1)
    assert formats.resolve(formats.EncodeSettings(format="flac"), 96000, 6) == (96000, 2)


def test_bitrate_choice_is_honoured(tmp_path):
    data = np.random.default_rng(0).standard_normal((SR * 4, 2)).astype(np.float32) * 0.1
    sizes = {}
    for kbps in (64, 192):
        s = formats.EncodeSettings(format="mp3", bitrate=kbps)
        out = tmp_path / f"{kbps}.mp3"
        ffmpeg.encode_pcm([data], SR, 2, out, formats.build_args(s, SR, 2))
        sizes[kbps] = out.stat().st_size
        assert ffmpeg.probe(out).first_audio.bit_rate == pytest.approx(kbps * 1000, rel=0.1)
        assert sizes[kbps] == pytest.approx(formats.estimate_size(s, 4.0), rel=0.2)
    assert sizes[192] > 2.5 * sizes[64]


def test_vbr_modes_and_lossless_depth(tmp_path):
    data = np.random.default_rng(1).standard_normal((SR * 3, 2)).astype(np.float32) * 0.1
    sizes = {}
    for q in (0, 9):
        s = formats.EncodeSettings(format="mp3", mode="vbr", quality=q)
        out = tmp_path / f"v{q}.mp3"
        ffmpeg.encode_pcm([data], SR, 2, out, formats.build_args(s, SR, 2))
        sizes[q] = out.stat().st_size
    assert sizes[0] > 1.8 * sizes[9]
    for depth, codec in ((16, "pcm_s16le"), (24, "pcm_s24le"), (32, "pcm_f32le"), (8, "pcm_u8")):
        out = tmp_path / f"d{depth}.wav"
        s = formats.EncodeSettings(format="wav", bit_depth=depth)
        ffmpeg.encode_pcm([data[:SR]], SR, 2, out, formats.build_args(s, SR, 2))
        assert ffmpeg.probe(out).first_audio.codec == codec
    out = tmp_path / "d24.flac"
    ffmpeg.encode_pcm([data[:SR]], SR, 2, out, formats.build_args(formats.EncodeSettings(format="flac", bit_depth=24), SR, 2))
    assert ffmpeg.probe(out).first_audio.bits == 24


def test_describe_and_settings_roundtrip():
    s = formats.EncodeSettings(format="mp3", bitrate=128)
    assert formats.describe(s, 44100, 2) == "MP3 · 44.1 kHz · Stereo · 128 kbps"
    assert "V2" in formats.describe(formats.EncodeSettings(format="mp3", mode="vbr", quality=2))
    assert formats.describe(formats.EncodeSettings(format="flac", bit_depth=24)).endswith("24-bit")
    assert formats.describe(formats.EncodeSettings(format="amr"), 44100, 2).startswith("AMR · 8 kHz · Mono")
    again = formats.EncodeSettings.from_dict(s.to_dict())
    assert again == s
    assert formats.EncodeSettings.from_dict({"format": "nope", "junk": 1}).format == "mp3"
    assert formats.EncodeSettings.from_dict(None).format == "mp3"


# ------------------------------------------------------------------ tags
FULL = TagSet(title="Звук акустической гитары", artist="Acoustic guitar", album="Сборник №1", album_artist="Various",
              year="2026", track="3", track_total="12", disc="1", disc_total="2", genre="Folk",
              composer="J. Smith", comment="Тест; with = odd # chars")


@pytest.mark.parametrize("ext", [".mp3", ".flac", ".ogg", ".opus", ".m4a", ".m4b", ".wma", ".wav", ".aiff"])
def test_tag_roundtrip_with_unicode_and_cover(tmp_path, ext):
    path = make_audio_file(tmp_path / f"song{ext}", tone(0.5), SR)
    want = FULL.copy()
    want.cover = PNG_1X1
    tags.write_tags(path, want)
    got = tags.read_tags(path)
    assert got.cover == PNG_1X1
    for key in tags.TEXT_KEYS:
        assert getattr(got, key) == getattr(want, key), (ext, key)
    ffmpeg.probe(path)  # the file must still be a valid, playable file


def test_partial_update_keeps_other_fields_and_cover(tmp_path):
    path = make_audio_file(tmp_path / "a.mp3", tone(0.5), SR)
    full = FULL.copy()
    full.cover = PNG_1X1
    tags.write_tags(path, full)
    tags.write_tags(path, {"title": "New title"})
    got = tags.read_tags(path)
    assert got.title == "New title" and got.artist == full.artist and got.cover == PNG_1X1
    tags.write_tags(path, {"artist": ""}, cover=None)
    got = tags.read_tags(path)
    assert got.artist == "" and got.title == "New title" and got.cover is None
    tags.write_tags(path, {"track": "7"})
    got = tags.read_tags(path)
    assert (got.track, got.track_total) == ("7", "12")


@pytest.mark.parametrize("ext", [".flac", ".m4a", ".ogg", ".wma"])
def test_clearing_fields_and_cover_on_other_containers(tmp_path, ext):
    path = make_audio_file(tmp_path / f"a{ext}", tone(0.5), SR)
    full = FULL.copy()
    full.cover = PNG_1X1
    tags.write_tags(path, full)
    tags.write_tags(path, {"title": "", "genre": ""}, cover=None)
    got = tags.read_tags(path)
    assert got.title == "" and got.genre == "" and got.cover is None and got.artist == full.artist
    tags.write_tags(path, {"album": "Only album"}, cover=KEEP)
    assert tags.read_tags(path).album == "Only album"


def test_unwritable_formats_fall_back_to_probe(tmp_path):
    path = make_audio_file(tmp_path / "a.aac", tone(0.5), SR)
    assert not tags.can_write(path)
    assert tags.read_tags(path).is_empty()
    with pytest.raises(ValueError):
        tags.write_tags(path, {"title": "x"})
    assert tags.can_write("x.MP3") and not tags.can_write("x.amr")


def test_read_tags_of_a_file_without_any(tmp_path):
    path = make_audio_file(tmp_path / "plain.mp3", tone(0.5), SR)
    assert tags.read_tags(path).is_empty()


def test_image_sniffing():
    assert tags.sniff_image_mime(b"\xff\xd8\xff\xe0abc") == "image/jpeg"
    assert tags.sniff_image_mime(PNG_1X1) == "image/png"
    assert tags.sniff_image_mime(b"GIF89a....") == "image/gif"


def test_to_ffmpeg_vocabulary():
    d = FULL.to_ffmpeg()
    assert d["track"] == "3/12" and d["date"] == "2026" and d["album_artist"] == "Various"


# ------------------------------------------------------------------ file names
def test_sanitize_filename():
    assert tags.sanitize_filename('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"
    assert tags.sanitize_filename("  trailing dots... ") == "trailing dots"
    assert tags.sanitize_filename("CON") == "_CON"
    assert tags.sanitize_filename("") == "untitled"
    assert tags.sanitize_filename("на гитаре") == "на гитаре"
    assert len(tags.sanitize_filename("x" * 500)) <= 120


def test_name_from_pattern():
    t = TagSet(title="Song", artist="Band", track="3", album="LP", year="1999")
    assert tags.name_from_pattern("{artist} - {title}", t, "orig") == "Band - Song"
    assert tags.name_from_pattern("{track} - {artist} - {title}", t, "orig") == "03 - Band - Song"
    assert tags.name_from_pattern("{name}", t, "orig") == "orig"
    empty = TagSet(title="Song")
    assert tags.name_from_pattern("{artist} - {title}", empty, "orig") == "Song"
    assert tags.name_from_pattern("{track} - {artist} - {title}", empty, "orig") == "Song"
    assert tags.name_from_pattern("{artist} - {title}", TagSet(), "orig") == "orig"
    assert tags.name_from_pattern("{title}: {album}?", t, "orig") == "Song_ LP_"


# ------------------------------------------------------------------ a file that cannot be changed
def test_a_file_the_system_refuses_raises_the_system_error_not_mutagens_wrapper(tmp_path):
    """mutagen reports "could not open it" as its own MutagenError; callers need the OSError to say why (see core/access)."""
    import os
    import stat
    path = make_audio_file(tmp_path / "ro.flac", np.stack([sine(220, 0.5)] * 2, axis=1))
    before = path.read_bytes()
    os.chmod(path, stat.S_IREAD)
    try:
        with pytest.raises(OSError) as caught:
            tags.write_tags(path, {"title": "nope"})
        assert tags.os_error_of(caught.value) is caught.value
    finally:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    assert path.read_bytes() == before                                     # a refused write changes nothing


def test_a_file_that_is_not_audio_stays_a_mutagen_error(tmp_path):
    import mutagen
    path = tmp_path / "fake.mp3"
    path.write_bytes(b"this is not audio at all" * 40)
    try:
        tags.write_tags(path, {"title": "x"})
    except mutagen.MutagenError as exc:
        assert tags.os_error_of(exc) is None
    except OSError:
        pytest.fail("a damaged file is not an operating-system refusal")


@pytest.mark.parametrize("ext", ["mp3", "m4a", "flac", "ogg", "opus"])
def test_writing_tags_never_changes_the_audio(tmp_path, ext):
    """Decode before and after: the sound must be bit-identical however the tags grow, shrink or go."""
    import hashlib
    import subprocess
    path = make_audio_file(tmp_path / f"a.{ext}", np.stack([sine(330, 1.5, amp=0.4)] * 2, axis=1))

    def decoded() -> str:
        out = subprocess.run([ffmpeg.find_tool("ffmpeg"), "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "s16le", "-"],
                             capture_output=True, check=True)
        return hashlib.sha256(out.stdout).hexdigest()
    before = decoded()
    tags.write_tags(path, {"title": "Song", "artist": "Band", "comment": "x" * 3000}, cover=PNG_1X1)
    assert decoded() == before
    tags.write_tags(path, {"title": "A much longer title " * 20, "artist": ""}, cover=None)
    assert decoded() == before and tags.read_tags(path).artist == ""
