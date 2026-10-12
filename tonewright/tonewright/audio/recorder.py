"""Recording from an input device straight into a Source."""
from __future__ import annotations

import queue
import threading
from typing import Callable, Optional

import numpy as np

from ..core.source import Source, SourceWriter
from .devices import AudioDeviceError, sd


class Recorder:
    def __init__(self, device: Optional[int] = None, sr: int = 44100, channels: int = 2,
                 stream_factory: Optional[Callable] = None) -> None:
        self.device = device
        self.sr = sr
        self.channels = channels
        self._factory = stream_factory
        self._stream = None
        self._queue: queue.Queue = queue.Queue()
        self._writer: Optional[SourceWriter] = None
        self._thread: Optional[threading.Thread] = None
        self._paused = False
        self._frames = 0
        self._peak = np.zeros(channels, np.float32)
        self.state = "idle"
        self.error: Optional[str] = None

    @property
    def seconds(self) -> float:
        return self._frames / self.sr

    def consume_peak(self) -> tuple[float, float]:
        peak = self._peak.copy()
        self._peak[:] = 0
        left = float(peak[0])
        return left, float(peak[1]) if self.channels > 1 else left

    def start(self) -> None:
        factory = self._factory or (sd.InputStream if sd is not None else None)
        if factory is None:
            raise AudioDeviceError("No audio input is available (PortAudio could not be loaded).")
        self._writer = SourceWriter(self.sr, self.channels, label="recording")
        try:
            self._stream = factory(samplerate=self.sr, channels=self.channels, dtype="float32",
                                   callback=self._callback, device=self.device)
        except Exception as exc:
            self._writer.abort()
            raise AudioDeviceError(f"Could not open the microphone: {exc}") from exc
        self._frames = 0
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()
        self.state = "recording"
        self._stream.start()

    def _callback(self, indata, frames, time_info, status) -> None:
        np.maximum(self._peak, np.abs(indata).max(axis=0), out=self._peak)
        if not self._paused:
            self._queue.put(indata.copy())

    def _drain(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self._writer.write(item)
                self._frames += len(item)
            except Exception as exc:  # disk full and the like
                self.error = str(exc)
                return

    def pause(self) -> None:
        if self.state == "recording":
            self._paused = True
            self.state = "paused"

    def resume(self) -> None:
        if self.state == "paused":
            self._paused = False
            self.state = "recording"

    def stop(self) -> Source:
        """Stop and return what was recorded."""
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=10)
        self.state = "idle"
        writer, self._writer = self._writer, None
        if self.error:
            if writer:
                writer.abort()
            raise AudioDeviceError(f"Recording failed: {self.error}")
        return writer.finish()

    def cancel(self) -> None:
        try:
            self.stop()
        except AudioDeviceError:
            pass
