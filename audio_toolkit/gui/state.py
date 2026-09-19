"""The one "current file" every single-file tool panel operates on."""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QObject, Signal
from pydub import AudioSegment

from .. import core


class AppState(QObject):
    file_changed = Signal()

    def __init__(self):
        super().__init__()
        self.path: str | None = None
        self.duration_ms: int = 0
        self.channels: int = 0
        self.frame_rate: int = 0
        self.samples: np.ndarray | None = None
        self._audio: AudioSegment | None = None

    def set_file(self, path: str) -> None:
        audio = core.load(path)  # raises AudioToolError, left for the caller to show
        self.path = path
        self.duration_ms = len(audio)
        self.channels = audio.channels
        self.frame_rate = audio.frame_rate
        self._audio = audio
        samples = np.array(audio.get_array_of_samples()).astype(np.float32)
        if audio.channels > 1:
            samples = samples.reshape((-1, audio.channels)).mean(axis=1)
        self.samples = samples
        self.file_changed.emit()

    def require_path(self) -> str:
        if not self.path:
            raise core.AudioToolError('Open a file first ("Open Audio" above).')
        return self.path

    def slice_ms(self, start_ms: int, end_ms: int) -> AudioSegment:
        if self._audio is None:
            raise core.AudioToolError('Open a file first ("Open Audio" above).')
        return self._audio[start_ms:end_ms]
