"""Synthetic sound generation: tones, noise, and silence."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from pydub import AudioSegment

from .core import AudioToolError, export


def _array_to_segment(samples: np.ndarray, samplerate: int) -> AudioSegment:
    return AudioSegment(samples.astype("<i2").tobytes(), frame_rate=samplerate, sample_width=2, channels=1)


def tone(
    out_path: str | Path,
    frequency_hz: float,
    duration_s: float,
    samplerate: int = 44100,
    amplitude: float = 0.3,
) -> None:
    if duration_s <= 0:
        raise AudioToolError("Duration must be greater than zero.")
    if not 0 < amplitude <= 1:
        raise AudioToolError("Amplitude must be between 0 and 1.")
    n = int(duration_s * samplerate)
    t = np.arange(n) / samplerate
    samples = amplitude * 32767 * np.sin(2 * np.pi * frequency_hz * t)
    export(_array_to_segment(samples, samplerate), out_path)


def white_noise(out_path: str | Path, duration_s: float, samplerate: int = 44100, amplitude: float = 0.3) -> None:
    if duration_s <= 0:
        raise AudioToolError("Duration must be greater than zero.")
    n = int(duration_s * samplerate)
    samples = amplitude * 32767 * np.random.uniform(-1, 1, n)
    export(_array_to_segment(samples, samplerate), out_path)


def pink_noise(out_path: str | Path, duration_s: float, samplerate: int = 44100, amplitude: float = 0.3) -> None:
    """Voss-McCartney approximation: sum of octave-spaced white noise sources."""
    if duration_s <= 0:
        raise AudioToolError("Duration must be greater than zero.")
    n = int(duration_s * samplerate)
    n_sources = 16
    values = np.zeros(n)
    for i in range(n_sources):
        step = 2**i
        source = np.random.uniform(-1, 1, n // step + 1)
        values += np.repeat(source, step)[:n]
    values /= n_sources
    values = values / np.max(np.abs(values)) * amplitude * 32767
    export(_array_to_segment(values, samplerate), out_path)


def silence(out_path: str | Path, duration_s: float, samplerate: int = 44100, channels: int = 1) -> None:
    if duration_s <= 0:
        raise AudioToolError("Duration must be greater than zero.")
    audio = AudioSegment.silent(duration=int(duration_s * 1000), frame_rate=samplerate)
    if channels == 2:
        audio = audio.set_channels(2)
    export(audio, out_path)
