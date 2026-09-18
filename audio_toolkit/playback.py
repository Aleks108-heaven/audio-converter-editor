"""Microphone recording and speaker playback via sounddevice.

Playback/recording work directly on raw PCM (int16), independent of ffmpeg,
so they work even without ffmpeg installed as long as the file itself is a
format pydub/ffmpeg can decode (or WAV, which needs neither).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import sounddevice as sd
from pydub import AudioSegment

from .core import AudioToolError, export, load


def _segment_to_array(audio: AudioSegment) -> np.ndarray:
    samples = np.array(audio.get_array_of_samples())
    if audio.channels > 1:
        samples = samples.reshape((-1, audio.channels))
    return samples


def play(path: str | Path, blocking: bool = True) -> None:
    play_segment(load(path), blocking=blocking)


def play_segment(audio: AudioSegment, blocking: bool = True) -> None:
    """Play an already-loaded segment directly, e.g. a waveform selection slice."""
    try:
        sd.play(_segment_to_array(audio), audio.frame_rate)
    except Exception as exc:  # noqa: BLE001 - portaudio/device errors vary by OS
        raise AudioToolError(f"Could not play audio: {exc}") from exc
    if blocking:
        sd.wait()


def stop_playback() -> None:
    sd.stop()


def record(out_path: str | Path, duration_s: float, samplerate: int = 44100, channels: int = 1) -> None:
    if duration_s <= 0:
        raise AudioToolError("Recording duration must be greater than zero.")
    try:
        frames = sd.rec(int(duration_s * samplerate), samplerate=samplerate, channels=channels, dtype="int16")
        sd.wait()
    except Exception as exc:  # noqa: BLE001
        raise AudioToolError(f"Could not record audio: {exc}") from exc
    audio = AudioSegment(frames.tobytes(), frame_rate=samplerate, sample_width=2, channels=channels)
    export(audio, out_path)


class Recorder:
    """Start/stop recording for GUI use, where duration isn't known up front."""

    def __init__(self, samplerate: int = 44100, channels: int = 1):
        self.samplerate = samplerate
        self.channels = channels
        self._frames: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None

    def start(self) -> None:
        self._frames = []

        def callback(indata, frame_count, time_info, status):  # noqa: ARG001
            self._frames.append(indata.copy())

        try:
            self._stream = sd.InputStream(
                samplerate=self.samplerate, channels=self.channels, dtype="int16", callback=callback
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001
            raise AudioToolError(f"Could not start recording: {exc}") from exc

    def stop(self, out_path: str | Path) -> None:
        if self._stream is None:
            raise AudioToolError("Recording was never started.")
        self._stream.stop()
        self._stream.close()
        self._stream = None

        data = (
            np.concatenate(self._frames, axis=0)
            if self._frames
            else np.zeros((0, self.channels), dtype="int16")
        )
        audio = AudioSegment(data.tobytes(), frame_rate=self.samplerate, sample_width=2, channels=self.channels)
        export(audio, out_path)
