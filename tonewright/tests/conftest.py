from __future__ import annotations

import base64
import wave
from pathlib import Path

import numpy as np
import pytest

from tonewright.core import ffmpeg, formats

SR = 44100

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def write_wav(path: Path, data: np.ndarray, sr: int = SR) -> Path:
    data = np.asarray(data, dtype=np.float32)
    if data.ndim == 1:
        data = data[:, None]
    pcm = (np.clip(data, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(data.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


def sine(freq: float, seconds: float, sr: int = SR, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def make_audio_file(path: Path, data: np.ndarray, sr: int = SR, fmt: str | None = None) -> Path:
    """Encode float32 audio (n, ch) into the file type given by the extension (or `fmt`)."""
    data = np.asarray(data, dtype=np.float32)
    if data.ndim == 1:
        data = data[:, None]
    key = fmt or next(k for k, f in formats.FORMATS.items() if f.ext == path.suffix.lower())
    settings = formats.EncodeSettings(format=key)
    args = formats.build_args(settings, sr, data.shape[1])
    rate, channels = formats.resolve(settings, sr, data.shape[1])
    if channels != data.shape[1]:
        data = data[:, :channels] if channels < data.shape[1] else np.repeat(data, channels, axis=1)
    ffmpeg.encode_pcm([data], sr, data.shape[1], path, args)
    return path


@pytest.fixture(scope="session", autouse=True)
def _need_ffmpeg():
    if not ffmpeg.available():
        pytest.skip("ffmpeg not installed", allow_module_level=True)


@pytest.fixture
def stereo_wav(tmp_path) -> Path:
    """3 s stereo: 440 Hz left, 660 Hz right."""
    data = np.stack([sine(440, 3), sine(660, 3)], axis=1)
    return write_wav(tmp_path / "stereo.wav", data)


@pytest.fixture
def mono_wav(tmp_path) -> Path:
    return write_wav(tmp_path / "mono.wav", sine(300, 2.0, amp=0.4))
