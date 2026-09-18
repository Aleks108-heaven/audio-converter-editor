"""Per-channel editing: split a stereo file into mono channels, edit them
independently, and recombine into stereo."""

from __future__ import annotations

from pathlib import Path

from pydub import AudioSegment

from .core import AudioToolError, _format_for, export, load


def split_channels(in_path: str | Path, out_dir: str | Path) -> tuple[Path, Path]:
    audio = load(in_path)
    if audio.channels != 2:
        raise AudioToolError(f"'{in_path}' has {audio.channels} channel(s); channel split needs stereo.")

    left, right = audio.split_to_mono()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = _format_for(in_path)
    stem = Path(in_path).stem

    left_path = out_dir / f"{stem}_L.{fmt}"
    right_path = out_dir / f"{stem}_R.{fmt}"
    export(left, left_path)
    export(right, right_path)
    return left_path, right_path


def combine_channels(left_path: str | Path, right_path: str | Path, out_path: str | Path) -> None:
    left = load(left_path)
    right = load(right_path)
    if left.channels != 1 or right.channels != 1:
        raise AudioToolError("Both inputs to combine-channels must be mono.")

    if len(left) != len(right):
        max_len = max(len(left), len(right))
        left = left + AudioSegment.silent(duration=max_len - len(left))
        right = right + AudioSegment.silent(duration=max_len - len(right))

    stereo = AudioSegment.from_mono_audiosegments(left, right)
    export(stereo, out_path)


def channel_gain(
    in_path: str | Path, out_path: str | Path, left_db: float = 0.0, right_db: float = 0.0
) -> None:
    audio = load(in_path)
    if audio.channels != 2:
        raise AudioToolError(f"'{in_path}' has {audio.channels} channel(s); channel gain needs stereo.")

    left, right = audio.split_to_mono()
    left = left.apply_gain(left_db)
    right = right.apply_gain(right_db)
    export(AudioSegment.from_mono_audiosegments(left, right), out_path)
