"""Reading and writing tags and cover art, one adapter per container family."""
from __future__ import annotations

import base64
import re
import struct
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Mapping, Optional, Union

import mutagen
from mutagen import id3

from . import ffmpeg as ff

TAG_FIELDS = (
    ("title", "Title"), ("artist", "Artist"), ("album", "Album"), ("album_artist", "Album artist"),
    ("year", "Year"), ("track", "Track"), ("track_total", "Of"), ("disc", "Disc"), ("disc_total", "Of"),
    ("genre", "Genre"), ("composer", "Composer"), ("comment", "Comment"),
)
TEXT_KEYS = tuple(k for k, _ in TAG_FIELDS)
KEEP = ...        # pass as `cover` to leave the existing artwork untouched
WRITABLE = {".mp3", ".m4a", ".m4b", ".mp4", ".ogg", ".oga", ".opus", ".wma", ".flac", ".wav", ".aiff", ".aif"}


@dataclass
class TagSet:
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    year: str = ""
    track: str = ""
    track_total: str = ""
    disc: str = ""
    disc_total: str = ""
    genre: str = ""
    composer: str = ""
    comment: str = ""
    cover: Optional[bytes] = None

    def text(self) -> dict[str, str]:
        return {k: getattr(self, k) for k in TEXT_KEYS}

    def copy(self) -> "TagSet":
        return TagSet(**{f.name: getattr(self, f.name) for f in fields(self)})

    def is_empty(self) -> bool:
        return not any(self.text().values()) and not self.cover

    def to_ffmpeg(self) -> dict[str, str]:
        """Basic tags in ffmpeg's -metadata vocabulary (used for formats mutagen cannot write)."""
        track = f"{self.track}/{self.track_total}" if self.track and self.track_total else self.track
        disc = f"{self.disc}/{self.disc_total}" if self.disc and self.disc_total else self.disc
        return {"title": self.title, "artist": self.artist, "album": self.album,
                "album_artist": self.album_artist, "date": self.year, "track": track, "disc": disc,
                "genre": self.genre, "composer": self.composer, "comment": self.comment}


def can_write(path: Union[str, Path]) -> bool:
    return Path(path).suffix.lower() in WRITABLE


def sniff_image_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:2] == b"BM":
        return "image/bmp"
    return "image/jpeg"


def _split_pair(value: str) -> tuple[str, str]:
    m = re.match(r"\s*(\d+)\s*(?:/\s*(\d+))?", value or "")
    return (m.group(1), m.group(2) or "") if m else ("", "")


def _first(values) -> str:
    if not values:
        return ""
    v = values[0] if isinstance(values, (list, tuple)) else values
    return str(getattr(v, "value", v)).strip()


# ------------------------------------------------------------------ ID3 (mp3, wav, aiff)
_ID3_TEXT = {"title": "TIT2", "artist": "TPE1", "album": "TALB", "album_artist": "TPE2",
             "year": "TDRC", "genre": "TCON", "composer": "TCOM"}


def _id3_read(tags: id3.ID3) -> TagSet:
    out = TagSet()
    for key, frame in _ID3_TEXT.items():
        f = tags.get(frame)
        if f is not None and f.text:
            setattr(out, key, str(f.text[0]))
    out.year = out.year[:4] if re.match(r"\d{4}", out.year) else out.year
    out.track, out.track_total = _split_pair(_first(tags.get("TRCK").text) if tags.get("TRCK") else "")
    out.disc, out.disc_total = _split_pair(_first(tags.get("TPOS").text) if tags.get("TPOS") else "")
    comms = tags.getall("COMM")
    if comms and comms[0].text:
        out.comment = str(comms[0].text[0])
    pics = tags.getall("APIC")
    if pics:
        front = next((p for p in pics if p.type == 3), pics[0])
        out.cover = bytes(front.data)
    return out


def _id3_write(tags: id3.ID3, values: Mapping[str, str], cover) -> None:
    for key, frame_id in _ID3_TEXT.items():
        if key in values:
            tags.delall(frame_id)
            if values[key]:
                tags.add(getattr(id3, frame_id)(encoding=1, text=[values[key]]))
    for key, frame_id, total in (("track", "TRCK", "track_total"), ("disc", "TPOS", "disc_total")):
        if key in values or total in values:
            cur = _split_pair(_first(tags.get(frame_id).text) if tags.get(frame_id) else "")
            num = values.get(key, cur[0])
            tot = values.get(total, cur[1])
            tags.delall(frame_id)
            if num:
                tags.add(getattr(id3, frame_id)(encoding=1, text=[f"{num}/{tot}" if tot else num]))
    if "comment" in values:
        tags.delall("COMM")
        if values["comment"]:
            tags.add(id3.COMM(encoding=1, lang="eng", desc="", text=[values["comment"]]))
    if cover is not KEEP:
        tags.delall("APIC")
        if cover:
            tags.add(id3.APIC(encoding=0, mime=sniff_image_mime(cover), type=3, desc="Cover", data=cover))


# ------------------------------------------------------------------ Vorbis comments (flac, ogg, opus)
_VORBIS = {"title": "title", "artist": "artist", "album": "album", "album_artist": "albumartist",
           "year": "date", "genre": "genre", "composer": "composer", "comment": "comment",
           "track": "tracknumber", "track_total": "tracktotal", "disc": "discnumber", "disc_total": "disctotal"}


def _vorbis_read(f) -> TagSet:
    out = TagSet()
    for key, name in _VORBIS.items():
        setattr(out, key, _first(f.get(name)))
    out.year = out.year[:4] if re.match(r"\d{4}", out.year) else out.year
    if not out.track_total:
        out.track, out.track_total = _split_pair(out.track)
    if not out.disc_total:
        out.disc, out.disc_total = _split_pair(out.disc)
    return out


def _vorbis_write(f, values: Mapping[str, str]) -> None:
    for key, name in _VORBIS.items():
        if key in values:
            if values[key]:
                f[name] = [values[key]]
            elif name in f:
                del f[name]


def _flac_picture(data: bytes):
    from mutagen.flac import Picture
    pic = Picture()
    pic.type, pic.mime, pic.desc, pic.data = 3, sniff_image_mime(data), "Cover", data
    return pic


# ------------------------------------------------------------------ MP4 (m4a, m4b)
_MP4_TEXT = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "album_artist": "aART",
             "year": "\xa9day", "genre": "\xa9gen", "composer": "\xa9wrt", "comment": "\xa9cmt"}


def _mp4_read(f) -> TagSet:
    out = TagSet()
    for key, atom in _MP4_TEXT.items():
        setattr(out, key, _first(f.get(atom)))
    out.year = out.year[:4] if re.match(r"\d{4}", out.year) else out.year
    for key, atom, tot in (("track", "trkn", "track_total"), ("disc", "disk", "disc_total")):
        pair = (f.get(atom) or [(0, 0)])[0]
        if pair and pair[0]:
            setattr(out, key, str(pair[0]))
        if pair and len(pair) > 1 and pair[1]:
            setattr(out, tot, str(pair[1]))
    covers = f.get("covr")
    if covers:
        out.cover = bytes(covers[0])
    return out


def _mp4_write(f, values: Mapping[str, str], cover) -> None:
    from mutagen.mp4 import MP4Cover
    for key, atom in _MP4_TEXT.items():
        if key in values:
            if values[key]:
                f[atom] = [values[key]]
            elif atom in f:
                del f[atom]
    for key, atom, tot in (("track", "trkn", "track_total"), ("disc", "disk", "disc_total")):
        if key in values or tot in values:
            cur = (f.get(atom) or [(0, 0)])[0]
            num = values.get(key, str(cur[0] or ""))
            total = values.get(tot, str(cur[1] if len(cur) > 1 and cur[1] else ""))
            if num.isdigit() and int(num) > 0:
                f[atom] = [(int(num), int(total) if total.isdigit() else 0)]
            elif atom in f:
                del f[atom]
    if cover is not KEEP:
        if cover:
            fmt = MP4Cover.FORMAT_PNG if sniff_image_mime(cover) == "image/png" else MP4Cover.FORMAT_JPEG
            f["covr"] = [MP4Cover(cover, imageformat=fmt)]
        elif "covr" in f:
            del f["covr"]


# ------------------------------------------------------------------ ASF (wma)
_ASF_TEXT = {"title": "Title", "artist": "Author", "album": "WM/AlbumTitle", "album_artist": "WM/AlbumArtist",
             "year": "WM/Year", "genre": "WM/Genre", "composer": "WM/Composer", "comment": "Description",
             "track": "WM/TrackNumber", "disc": "WM/PartOfSet",
             "track_total": "TotalTracks", "disc_total": "TotalDiscs"}


def _asf_picture_bytes(data: bytes) -> bytes:
    mime = sniff_image_mime(data).encode("utf-16-le") + b"\x00\x00"
    desc = "Cover".encode("utf-16-le") + b"\x00\x00"
    return b"\x03" + struct.pack("<I", len(data)) + mime + desc + data


def _asf_picture_decode(blob: bytes) -> Optional[bytes]:
    try:
        size = struct.unpack("<I", blob[1:5])[0]
        pos = 5
        for _ in range(2):                       # skip two UTF-16 null-terminated strings
            while blob[pos:pos + 2] != b"\x00\x00":
                pos += 2
            pos += 2
        return bytes(blob[pos:pos + size])
    except (struct.error, IndexError):
        return None


def _asf_read(f) -> TagSet:
    out = TagSet()
    for key, name in _ASF_TEXT.items():
        setattr(out, key, _first(f.get(name)))
    track, track_total = _split_pair(out.track)          # tolerate "3/12" written by other tools
    disc, disc_total = _split_pair(out.disc)
    out.track, out.track_total = track, out.track_total or track_total
    out.disc, out.disc_total = disc, out.disc_total or disc_total
    pics = f.get("WM/Picture")
    if pics:
        out.cover = _asf_picture_decode(bytes(pics[0].value))
    return out


def _asf_write(f, values: Mapping[str, str], cover) -> None:
    from mutagen.asf import ASFByteArrayAttribute
    for key, name in _ASF_TEXT.items():
        if key in values:
            if values[key]:
                f[name] = [values[key]]
            elif name in f:
                del f[name]
    if cover is not KEEP:
        if cover:
            f["WM/Picture"] = [ASFByteArrayAttribute(_asf_picture_bytes(cover))]
        elif "WM/Picture" in f:
            del f["WM/Picture"]


# ------------------------------------------------------------------ public API
def _open(path: Path):
    suffix = path.suffix.lower()
    if suffix in (".mp3",):
        try:
            return "id3", id3.ID3(path), None
        except id3.ID3NoHeaderError:
            return "id3", id3.ID3(), None
    if suffix in (".wav",):
        from mutagen.wave import WAVE
        f = WAVE(path)
        if f.tags is None:
            f.add_tags()
        return "id3", f.tags, f
    if suffix in (".aiff", ".aif"):
        from mutagen.aiff import AIFF
        f = AIFF(path)
        if f.tags is None:
            f.add_tags()
        return "id3", f.tags, f
    if suffix == ".flac":
        from mutagen.flac import FLAC
        f = FLAC(path)
        return "flac", f, f
    if suffix in (".ogg", ".oga"):
        from mutagen.oggvorbis import OggVorbis
        f = OggVorbis(path)
        return "ogg", f, f
    if suffix == ".opus":
        from mutagen.oggopus import OggOpus
        f = OggOpus(path)
        return "ogg", f, f
    if suffix in (".m4a", ".m4b", ".mp4"):
        from mutagen.mp4 import MP4
        f = MP4(path)
        if f.tags is None:
            f.add_tags()
        return "mp4", f, f
    if suffix == ".wma":
        from mutagen.asf import ASF
        f = ASF(path)
        return "asf", f, f
    raise ValueError(f"Tags cannot be edited for {suffix or 'this'} files")


def read_tags(path: Union[str, Path]) -> TagSet:
    """Tags and cover of a file; falls back to ffprobe for formats mutagen cannot open."""
    path = Path(path)
    try:
        kind, obj, _ = _open(path)
        if kind == "id3":
            return _id3_read(obj)
        if kind == "flac":
            out = _vorbis_read(obj)
            if obj.pictures:
                out.cover = bytes(obj.pictures[0].data)
            return out
        if kind == "ogg":
            out = _vorbis_read(obj)
            blocks = obj.get("metadata_block_picture")
            if blocks:
                from mutagen.flac import Picture
                out.cover = bytes(Picture(base64.b64decode(blocks[0])).data)
            return out
        if kind == "mp4":
            return _mp4_read(obj)
        return _asf_read(obj)
    except (ValueError, mutagen.MutagenError, OSError):
        pass
    try:
        raw = ff.probe(path).tags
    except ff.FFmpegError:
        return TagSet()
    track, track_total = _split_pair(raw.get("track", ""))
    disc, disc_total = _split_pair(raw.get("disc", ""))
    date = raw.get("date", raw.get("year", ""))
    return TagSet(title=raw.get("title", ""), artist=raw.get("artist", ""), album=raw.get("album", ""),
                  album_artist=raw.get("album_artist", ""), year=date[:4] if re.match(r"\d{4}", date) else date,
                  track=track, track_total=track_total, disc=disc, disc_total=disc_total,
                  genre=raw.get("genre", ""), composer=raw.get("composer", ""), comment=raw.get("comment", ""))


def write_tags(path: Union[str, Path], values: Union[Mapping[str, str], TagSet], *,
               cover: Union[bytes, None, type(...)] = KEEP) -> None:
    """Write the given text fields (an empty string removes a field). `cover` may be image
    bytes, None to remove the artwork, or KEEP (the default) to leave it alone."""
    path = Path(path)
    if isinstance(values, TagSet):
        if cover is KEEP:
            cover = values.cover
        values = values.text()
    kind, obj, container = _open(path)
    if kind == "id3":
        _id3_write(obj, values, cover)
        if container is not None:
            container.save()
        else:
            obj.save(path, v2_version=3)
    elif kind == "flac":
        _vorbis_write(obj, values)
        if cover is not KEEP:
            obj.clear_pictures()
            if cover:
                obj.add_picture(_flac_picture(cover))
        obj.save()
    elif kind == "ogg":
        _vorbis_write(obj, values)
        if cover is not KEEP:
            if cover:
                obj["metadata_block_picture"] = [base64.b64encode(_flac_picture(cover).write()).decode("ascii")]
            elif "metadata_block_picture" in obj:
                del obj["metadata_block_picture"]
        obj.save()
    elif kind == "mp4":
        _mp4_write(obj, values, cover)
        obj.save()
    else:
        _asf_write(obj, values, cover)
        obj.save()


# ------------------------------------------------------------------ file names
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitize_filename(name: str, fallback: str = "untitled") -> str:
    name = _BAD_CHARS.sub("_", name).strip().rstrip(". ")
    name = re.sub(r"\s+", " ", name)[:120].rstrip(". ")
    if not name:
        return fallback
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    return name


def name_from_pattern(pattern: str, tags: TagSet, stem: str) -> str:
    """Fill {title} {artist} {album} {album_artist} {year} {track} {disc} {genre} {name} in a pattern."""
    track = tags.track.zfill(2) if tags.track.isdigit() else tags.track
    values = {"title": tags.title, "artist": tags.artist, "album": tags.album,
              "album_artist": tags.album_artist, "year": tags.year, "track": track,
              "disc": tags.disc, "genre": tags.genre, "name": stem}

    def sub(m: re.Match) -> str:
        return values.get(m.group(1).lower(), m.group(0))

    result = re.sub(r"\{(\w+)\}", sub, pattern)
    result = re.sub(r"(?:\s*[-–]\s*){2,}", " - ", result)      # an empty field must not leave "- -" behind
    result = re.sub(r"^[\s\-–]+|[\s\-–]+$", "", result)
    return sanitize_filename(result, stem)
