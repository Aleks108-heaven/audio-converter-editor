"""Graphic equalizer and simple bass/treble shelving, via ffmpeg's audio
filter graph. Requires ffmpeg on PATH regardless of input format."""

from __future__ import annotations

from pathlib import Path

from .core import AudioToolError, run_ffmpeg_filter

# A standard 10-band graphic-EQ layout (Hz), as used by most consumer players.
BANDS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]

MIN_GAIN_DB = -12.0
MAX_GAIN_DB = 12.0


def apply_eq(in_path: str | Path, out_path: str | Path, gains_db: dict[float, float]) -> None:
    """gains_db maps a center frequency (Hz) to a boost/cut in dB, e.g. {60: 4, 1000: -2}.
    Frequencies not present default to 0 dB (no change) and are skipped."""
    filters = []
    for freq, gain in gains_db.items():
        if gain == 0:
            continue
        if not MIN_GAIN_DB <= gain <= MAX_GAIN_DB:
            raise AudioToolError(f"Gain for {freq}Hz must be between {MIN_GAIN_DB} and {MAX_GAIN_DB} dB.")
        filters.append(f"equalizer=f={freq}:width_type=o:width=1:g={gain}")

    if not filters:
        raise AudioToolError("No non-zero band gains given; nothing to do.")
    run_ffmpeg_filter(in_path, out_path, ",".join(filters))


def apply_bass_treble(in_path: str | Path, out_path: str | Path, bass_db: float = 0.0, treble_db: float = 0.0) -> None:
    if bass_db == 0 and treble_db == 0:
        raise AudioToolError("Bass and treble gains are both 0; nothing to do.")
    filters = []
    if bass_db:
        filters.append(f"bass=g={bass_db}")
    if treble_db:
        filters.append(f"treble=g={treble_db}")
    run_ffmpeg_filter(in_path, out_path, ",".join(filters))
