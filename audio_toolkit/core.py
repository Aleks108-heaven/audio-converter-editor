"""Core audio operations: convert, trim, cut, split, join, mix.

All I/O goes through pydub, which shells out to ffmpeg for every format
except raw WAV. ffmpeg must be installed and on PATH for MP3, FLAC, M4A,
WMA, AAC, MP2, AMR, OGG, etc.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from pydub import AudioSegment
from pydub.exceptions import CouldntDecodeError

SUPPORTED_FORMATS = {
    "mp3", "flac", "wav", "m4a", "wma", "aac", "mp2", "amr", "ogg",
}

# pydub/ffmpeg format name differs from a couple of file extensions.
_FORMAT_ALIASES = {
    "m4a": "ipod",  # ffmpeg's muxer name for .m4a containers
}


class AudioToolError(Exception):
    """Raised for any user-facing failure in the toolkit."""


def _format_for(path: str | Path) -> str:
    p = Path(path)
    if str(p) in ("", "."):  # Path("") normalizes to "." - both mean "no path given"
        raise AudioToolError("No output file chosen.")
    ext = p.suffix.lower().lstrip(".")
    if ext not in SUPPORTED_FORMATS:
        raise AudioToolError(
            f"Unsupported format '.{ext}'. Supported: {', '.join(sorted(SUPPORTED_FORMATS))}"
        )
    return ext


def ms_from_str(value: str) -> int:
    """Parse a timestamp like '90', '1:30', or '1:30.500' into milliseconds."""
    value = value.strip()
    match = re.fullmatch(r"(?:(\d+):)?(\d+(?:\.\d+)?)", value)
    if not match:
        raise AudioToolError(f"Invalid time '{value}'. Use seconds or mm:ss(.ms).")
    minutes_str, seconds_str = match.groups()
    minutes = int(minutes_str) if minutes_str else 0
    seconds = float(seconds_str)
    return int((minutes * 60 + seconds) * 1000)


def load(path: str | Path) -> AudioSegment:
    path = Path(path)
    if not path.is_file():
        raise AudioToolError(f"Input file not found: {path}")
    fmt = _format_for(path)
    try:
        return AudioSegment.from_file(path, format=_FORMAT_ALIASES.get(fmt, fmt))
    except FileNotFoundError as exc:
        raise AudioToolError(
            "ffmpeg was not found. Install it and ensure it's on PATH."
        ) from exc
    except CouldntDecodeError as exc:
        raise AudioToolError(f"Could not decode '{path}': {exc}") from exc


def export(audio: AudioSegment, out_path: str | Path, bitrate: str | None = None) -> None:
    out_path = Path(out_path)
    fmt = _format_for(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {"format": _FORMAT_ALIASES.get(fmt, fmt)}
    if bitrate:
        kwargs["bitrate"] = bitrate
    try:
        audio.export(out_path, **kwargs)
    except FileNotFoundError as exc:
        raise AudioToolError(
            "ffmpeg was not found. Install it and ensure it's on PATH."
        ) from exc


def run_ffmpeg_filter(in_path: str | Path, out_path: str | Path, filter_str: str) -> None:
    """Apply an ffmpeg -af filter graph directly, for filters pydub doesn't expose."""
    in_path, out_path = Path(in_path), Path(out_path)
    if not in_path.is_file():
        raise AudioToolError(f"Input file not found: {in_path}")
    _format_for(in_path)
    _format_for(out_path)
    if out_path.exists() and in_path.resolve() == out_path.resolve():
        raise AudioToolError(
            "Output file must be different from the input file (ffmpeg can't edit audio in place)."
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(in_path), "-af", filter_str, str(out_path)],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise AudioToolError("ffmpeg was not found. Install it and ensure it's on PATH.") from exc

    if result.returncode != 0:
        raise AudioToolError(f"ffmpeg filter failed: {result.stderr.strip()[-500:]}")


def convert(in_path: str | Path, out_path: str | Path, bitrate: str | None = None) -> None:
    audio = load(in_path)
    export(audio, out_path, bitrate=bitrate)


def trim(in_path: str | Path, out_path: str | Path, start_ms: int, end_ms: int) -> None:
    audio = load(in_path)
    if start_ms < 0 or end_ms > len(audio) or start_ms >= end_ms:
        raise AudioToolError(
            f"Invalid trim range [{start_ms}, {end_ms}] for a {len(audio)} ms file."
        )
    export(audio[start_ms:end_ms], out_path)


def cut(in_path: str | Path, out_path: str | Path, start_ms: int, end_ms: int) -> None:
    audio = load(in_path)
    if start_ms < 0 or end_ms > len(audio) or start_ms >= end_ms:
        raise AudioToolError(
            f"Invalid cut range [{start_ms}, {end_ms}] for a {len(audio)} ms file."
        )
    result = audio[:start_ms] + audio[end_ms:]
    export(result, out_path)


def split(in_path: str | Path, out_dir: str | Path, points_ms: list[int]) -> list[Path]:
    audio = load(in_path)
    points = sorted(p for p in points_ms if 0 < p < len(audio))
    bounds = [0, *points, len(audio)]
    if len(bounds) < 2:
        raise AudioToolError("No valid split points within the file's duration.")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = _format_for(in_path)
    stem = Path(in_path).stem

    outputs = []
    for i in range(len(bounds) - 1):
        segment = audio[bounds[i]:bounds[i + 1]]
        out_path = out_dir / f"{stem}_part{i + 1}.{fmt}"
        export(segment, out_path)
        outputs.append(out_path)
    return outputs


def join(in_paths: list[str | Path], out_path: str | Path, gap_ms: int = 0) -> None:
    if len(in_paths) < 2:
        raise AudioToolError("Join requires at least two input files.")
    result: AudioSegment | None = None
    gap = AudioSegment.silent(duration=gap_ms) if gap_ms > 0 else None
    for path in in_paths:
        segment = load(path)
        if result is None:
            result = segment
        else:
            result = result + gap + segment if gap is not None else result + segment
    export(result, out_path)


def mix(
    in_paths: list[str | Path],
    out_path: str | Path,
    gains_db: list[float] | None = None,
) -> None:
    if len(in_paths) < 2:
        raise AudioToolError("Mix requires at least two input files.")
    if gains_db is not None and len(gains_db) != len(in_paths):
        raise AudioToolError("Number of gains must match number of input files.")

    segments = [load(path) for path in in_paths]
    if gains_db:
        segments = [seg.apply_gain(gain) for seg, gain in zip(segments, gains_db)]

    max_len = max(len(seg) for seg in segments)
    padded = [
        seg + AudioSegment.silent(duration=max_len - len(seg)) if len(seg) < max_len else seg
        for seg in segments
    ]

    result = padded[0]
    for seg in padded[1:]:
        result = result.overlay(seg)
    export(result, out_path)
