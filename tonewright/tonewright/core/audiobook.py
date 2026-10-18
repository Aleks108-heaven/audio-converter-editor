"""Audiobooks: join files into one M4B with chapters, tags and a cover."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

import numpy as np

from . import analysis
from . import ffmpeg as ff
from . import formats
from . import tags as tagmod
from .common import CancelToken, PauseToken, ProgressFn
from .convert import ConvertItem, item_pcm

SPLIT_MODES = (
    ("none", "No split (one chapter)"),
    ("files", "One chapter per file"),
    ("count", "Split into a number of chapters"),
    ("time", "Split by time"),
    ("markers", "Split at the markers set in the editor"),
)


@dataclass
class Part:
    """One source of the book: a file, or an edited document."""
    item: ConvertItem
    title: str = ""
    markers: list[tuple[float, str]] = field(default_factory=list)    # (seconds in this part, name)


@dataclass
class Chapter:
    start: float
    title: str


@dataclass
class BookOptions:
    title: str = ""
    author: str = ""
    narrator: str = ""
    year: str = ""
    genre: str = "Audiobook"
    comment: str = ""
    cover: Optional[bytes] = None
    bitrate: int = 64
    mono: bool = False
    sample_rate: int = 0
    split: str = "files"
    chapter_count: int = 5
    chapter_seconds: float = 180.0
    pause_detection: bool = True
    keep_title_tags: bool = True
    gap_seconds: float = 0.0
    chapter_label: str = "Chapter"
    silence_threshold_db: float = -45.0
    silence_min_seconds: float = 0.4
    snap_window_seconds: float = 30.0


def part_offsets(durations: list[float], gap: float) -> list[float]:
    offsets, t = [], 0.0
    for i, d in enumerate(durations):
        offsets.append(t)
        t += d + (gap if i < len(durations) - 1 else 0.0)
    return offsets


def book_length(durations: list[float], gap: float) -> float:
    return sum(durations) + gap * max(0, len(durations) - 1)


def _snap(target: float, pauses: list[tuple[float, float]], window: float) -> float:
    best, best_dist = target, window
    for start, end in pauses:
        centre = (start + end) / 2.0
        dist = abs(centre - target)
        if dist <= best_dist:
            best, best_dist = centre, dist
    return best


def plan_chapters(durations: list[float], parts: list[Part], opts: BookOptions,
                  pauses: Optional[list[tuple[float, float]]] = None) -> list[Chapter]:
    """Chapter starts for the chosen split mode. `pauses` are silences (start, end) in book time."""
    offsets = part_offsets(durations, opts.gap_seconds)
    total = book_length(durations, opts.gap_seconds)

    def label(k: int) -> str:
        return f"{opts.chapter_label} {k}"

    if opts.split == "none" or not parts:
        chapters = [Chapter(0.0, opts.title or label(1))]
    elif opts.split == "files":
        chapters = []
        for i, (part, off) in enumerate(zip(parts, offsets), start=1):
            title = part.title if (opts.keep_title_tags and part.title) else label(i)
            chapters.append(Chapter(off, title))
    elif opts.split == "markers":
        found = sorted((off + t, name) for part, off in zip(parts, offsets) for t, name in part.markers)
        chapters = [Chapter(t, name) for t, name in found]
        if not chapters or chapters[0].start > 0.5:
            chapters.insert(0, Chapter(0.0, ""))
        for i, ch in enumerate(chapters, start=1):
            ch.title = ch.title or label(i)
    else:
        if opts.split == "count":
            n = max(1, int(opts.chapter_count))
            bounds = [total * k / n for k in range(1, n)]
            chapter_len = total / n
        else:
            step = max(5.0, float(opts.chapter_seconds))
            bounds = []
            t = step
            while t < total - step * 0.25:
                bounds.append(t)
                t += step
            chapter_len = step
        if opts.pause_detection and pauses:
            window = min(opts.snap_window_seconds, 0.4 * chapter_len)
            bounds = [_snap(b, pauses, window) for b in bounds]
        chapters = [Chapter(0.0, label(1))] + [Chapter(b, label(i)) for i, b in enumerate(bounds, start=2)]
    cleaned: list[Chapter] = []
    for ch in sorted(chapters, key=lambda c: c.start):
        if cleaned and ch.start - cleaned[-1].start < 1.0:
            continue
        if total and ch.start >= total - 0.5 and cleaned:
            continue
        cleaned.append(ch)
    return cleaned


def pauses_from_envelopes(envelopes: list[np.ndarray], hop_s: float, durations: list[float],
                          opts: BookOptions) -> list[tuple[float, float]]:
    offsets = part_offsets(durations, opts.gap_seconds)
    pauses: list[tuple[float, float]] = []
    for env, off in zip(envelopes, offsets):
        for s, e in analysis.find_silences(env, hop_s, opts.silence_threshold_db, opts.silence_min_seconds):
            pauses.append((off + s, off + e))
    if opts.gap_seconds > 0:
        for off, dur in zip(offsets[:-1], durations[:-1]):
            pauses.append((off + dur, off + dur + opts.gap_seconds))
    return sorted(pauses)


def analyze_pauses(parts: list[Part], opts: BookOptions, *, progress: Optional[ProgressFn] = None,
                   cancel: Optional[CancelToken] = None) -> tuple[list[tuple[float, float]], list[float]]:
    """Find the pauses in every part without encoding. Returns (pauses, part durations)."""
    envelopes, durations = [], []
    hop_s = 0.02
    for i, part in enumerate(parts):
        item = part.item
        if item.doc is not None:
            a, b = item.frame_range or (0, item.doc.frames)
            env = analysis.frame_levels(item.doc, a, b, hop_s, cancel=cancel)
            durations.append((b - a) / item.doc.sr)
        else:
            builder = analysis.EnvelopeBuilder(16000, 1, hop_s)
            frames = 0
            for chunk in ff.convert_whole_file_to_pcm(item.path, 16000, 1, stream_index=item.stream_index,
                                                      cancel=cancel):
                builder.feed(chunk)
                frames += len(chunk)
            env = builder.result()
            durations.append(frames / 16000)
        envelopes.append(env)
        if progress:
            progress((i + 1) / len(parts))
    return pauses_from_envelopes(envelopes, hop_s, durations, opts), durations


# ------------------------------------------------------------------ building the file
def _escape(text: str) -> str:
    """One metadata value on one line. A newline would let a hostile title start a section of its own
    ("[CHAPTER]", another key...), so line breaks are flattened to spaces rather than escaped."""
    out = re.sub(r"[\r\n\x00]+", " ", text).replace("\\", "\\\\")
    for ch in ("=", ";", "#"):
        out = out.replace(ch, "\\" + ch)
    return out


def write_ffmetadata(path: Path, opts: BookOptions, chapters: list[Chapter], total: float) -> None:
    lines = [";FFMETADATA1", f"title={_escape(opts.title)}", f"album={_escape(opts.title)}"]
    if opts.author:
        lines += [f"artist={_escape(opts.author)}", f"album_artist={_escape(opts.author)}"]
    if opts.narrator:
        lines.append(f"composer={_escape(opts.narrator)}")
    if opts.genre:
        lines.append(f"genre={_escape(opts.genre)}")
    if opts.year:
        lines.append(f"date={_escape(opts.year)}")
    if opts.comment:
        lines.append(f"comment={_escape(opts.comment)}")
    for i, ch in enumerate(chapters):
        end = chapters[i + 1].start if i + 1 < len(chapters) else total
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(round(ch.start * 1000))}",
                  f"END={max(int(round(end * 1000)), int(round(ch.start * 1000)) + 1)}",
                  f"title={_escape(ch.title)}"]
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))      # bytes: Windows must not turn LF into CRLF here


def create_audiobook(parts: list[Part], opts: BookOptions, out_path: Path, *,
                     chapters: Optional[list[Chapter]] = None, progress: Optional[ProgressFn] = None,
                     cancel: Optional[CancelToken] = None, pause: Optional[PauseToken] = None
                     ) -> tuple[Path, list[Chapter]]:
    """Encode all parts into one M4B. Pass `chapters` to use a reviewed plan, otherwise
    they are worked out from the real decoded lengths (and pauses) after encoding."""
    if not parts:
        raise ValueError("Add at least one file to the audiobook.")
    out_path = Path(out_path)
    first = parts[0].item
    sr = formats.nearest_rate(opts.sample_rate or first.source_sr, formats.FORMATS["m4b"].sample_rates)
    channels = 1 if opts.mono else min(2, first.source_channels)
    settings = formats.EncodeSettings(format="m4b", bitrate=opts.bitrate, sample_rate=sr, channels=channels)
    args = formats.build_args(settings, sr, channels)
    gap = np.zeros((int(opts.gap_seconds * sr), channels), np.float32)
    want_pauses = chapters is None and opts.pause_detection and opts.split in ("count", "time")
    frames_per_part = [0] * len(parts)
    envelopes = [analysis.EnvelopeBuilder(sr, channels) for _ in parts]
    estimate = book_length([p.item.length for p in parts], opts.gap_seconds)

    def stream() -> Iterator[np.ndarray]:
        for n, part in enumerate(parts):
            if n and len(gap):
                yield gap
            for chunk in item_pcm(part.item, sr, channels, cancel):
                frames_per_part[n] += len(chunk)
                if want_pauses:
                    envelopes[n].feed(chunk)
                yield chunk

    work = out_path.with_name(f"{out_path.stem}.tonewright-book.m4a")
    meta = out_path.with_name(f"{out_path.stem}.tonewright-meta.txt")
    final_tmp = out_path.with_name(f"{out_path.stem}.tonewright-final{out_path.suffix}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        ff.encode_pcm(stream(), sr, channels, work, args, total_frames=int(estimate * sr),
                      progress=(lambda f: progress and progress(f * 0.95)), cancel=cancel, pause=pause)
        durations = [n / sr for n in frames_per_part]
        total = book_length(durations, opts.gap_seconds)
        if chapters is None:
            pauses = None
            if want_pauses:
                pauses = pauses_from_envelopes([e.result() for e in envelopes], envelopes[0].hop_s,
                                               durations, opts)
            chapters = plan_chapters(durations, parts, opts, pauses)
        write_ffmetadata(meta, opts, chapters, total)
        ff.run_capture(["-y", "-v", "error", "-i", str(work), "-i", str(meta), "-map", "0:a",
                        "-map_metadata", "1", "-map_chapters", "1", "-c", "copy",
                        "-movflags", "+faststart", "-f", "ipod", str(final_tmp)], cancel=cancel)
        ff.move_into_place(final_tmp, out_path)
        _finish_tags(out_path, opts)
    finally:
        for tmp in (work, meta, final_tmp):
            try:
                tmp.unlink()
            except OSError:
                pass
    if progress:
        progress(1.0)
    return out_path, chapters


def _finish_tags(path: Path, opts: BookOptions) -> None:
    """Add the cover and mark the file as an audiobook for players that care (iTunes, Books)."""
    tagmod.write_tags(path, {"title": opts.title, "album": opts.title, "artist": opts.author,
                             "album_artist": opts.author, "composer": opts.narrator, "year": opts.year,
                             "genre": opts.genre, "comment": opts.comment}, cover=opts.cover)
    from mutagen.mp4 import MP4
    f = MP4(path)
    f["stik"] = [2]
    f.save()
