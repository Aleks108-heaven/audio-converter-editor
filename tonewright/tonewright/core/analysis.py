"""Measurements: statistics, spectra, loudness and silence detection."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from . import ffmpeg as ff
from .common import CancelToken, ProgressFn
from .document import PieceSeq
from .effects.base import Stats
from .timefmt import amp_to_db


@dataclass
class AudioStats:
    frames: int
    sr: int
    channels: int
    peak: list[float] = field(default_factory=list)
    rms: list[float] = field(default_factory=list)
    dc: list[float] = field(default_factory=list)
    clipped: int = 0
    loudness: Optional[float] = None      # integrated LUFS
    true_peak: Optional[float] = None     # dBTP
    loudness_range: Optional[float] = None

    @property
    def duration(self) -> float:
        return self.frames / self.sr if self.sr else 0.0

    @property
    def peak_db(self) -> list[float]:
        return [amp_to_db(p) for p in self.peak]

    @property
    def rms_db(self) -> list[float]:
        return [amp_to_db(r) for r in self.rms]


def measure(seq: PieceSeq, a: int, b: int, *, loudness: bool = True,
            progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None) -> AudioStats:
    total = max(1, b - a)
    st = Stats(seq.channels)
    clipped = 0
    for chunk in seq.iter_chunks(a, b):
        if cancel is not None:
            cancel.check()
        st.add(chunk)
        clipped += int(np.count_nonzero(np.abs(chunk) >= 0.9999))
        if progress:
            progress(min(0.5 if loudness else 1.0, (st.n / total) * (0.5 if loudness else 1.0)))
    out = AudioStats(frames=st.n, sr=seq.sr, channels=seq.channels,
                     peak=[float(x) for x in st.peak], rms=[float(x) for x in st.rms],
                     dc=[float(x) for x in st.mean], clipped=clipped)
    if loudness and st.n > seq.sr // 2:
        try:
            report = ff.analyze_stream(seq.iter_chunks(a, b), seq.sr, seq.channels,
                                       "loudnorm=print_format=json", total_frames=st.n,
                                       progress=lambda f: progress and progress(0.5 + f * 0.5), cancel=cancel)
            blocks = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", report, flags=re.S)
            if blocks:
                data = json.loads(blocks[-1])

                def val(key: str) -> Optional[float]:
                    try:
                        v = float(data[key])
                        return v if np.isfinite(v) else None
                    except (KeyError, ValueError):
                        return None
                out.loudness, out.true_peak, out.loudness_range = val("input_i"), val("input_tp"), val("input_lra")
        except ff.FFmpegError:
            pass
    return out


# ------------------------------------------------------------------ spectra
def _window_sum(win: np.ndarray) -> float:
    return float(win.sum())


def average_spectrum(seq: PieceSeq, a: int, b: int, n_fft: int = 8192,
                     max_windows: int = 600) -> tuple[np.ndarray, np.ndarray]:
    """Mean magnitude spectrum in dBFS: (frequencies, (channels, bins))."""
    n_fft = int(n_fft)
    win = np.hanning(n_fft).astype(np.float32)
    span = b - a
    if span < n_fft:
        pad = np.zeros((n_fft, seq.channels), np.float32)
        pad[:span] = seq.read(a, b)
        blocks = [pad]
    else:
        starts = np.unique(np.linspace(a, b - n_fft, min(max_windows, max(1, (span - n_fft) // (n_fft // 2) + 1))).astype(np.int64))
        blocks = [seq.read(int(s), int(s) + n_fft) for s in starts]
    power = np.zeros((seq.channels, n_fft // 2 + 1))
    for blk in blocks:
        spec = np.fft.rfft(blk * win[:, None], axis=0)
        power += (np.abs(spec) ** 2).T
    power /= len(blocks)
    mag = np.sqrt(power) * 2.0 / _window_sum(win)
    db = 20.0 * np.log10(np.maximum(mag, 1e-9))
    return np.fft.rfftfreq(n_fft, 1.0 / seq.sr), db.astype(np.float32)


def spectrogram(seq: PieceSeq, start: int, stop: int, width: int, n_fft: int = 2048,
                channel: int = 0) -> np.ndarray:
    """dBFS magnitudes as a (bins, width) array covering frames [start, stop)."""
    n_fft = int(n_fft)
    win = np.hanning(n_fft).astype(np.float32)
    span = max(1, stop - start)
    centres = start + (np.arange(width) + 0.5) * span / width
    first = np.rint(centres).astype(np.int64) - n_fft // 2
    channel = min(channel, seq.channels - 1)
    if span + n_fft <= 12_000_000:
        lo = int(first.min())
        data = seq.read(max(0, lo), int(first.max()) + n_fft)
        pad_left = max(0, -lo)
        if pad_left:
            data = np.concatenate([np.zeros((pad_left, seq.channels), np.float32), data])
        mono = data[:, channel]
        need = int(first.max()) - lo + n_fft
        if len(mono) < need:
            mono = np.concatenate([mono, np.zeros(need - len(mono), np.float32)])
        frames = mono[(first - lo)[:, None] + np.arange(n_fft)[None, :]]
    else:
        frames = np.zeros((width, n_fft), np.float32)
        for i, s in enumerate(first):
            chunk = seq.read(int(max(0, s)), int(s) + n_fft)[:, channel]
            off = max(0, -int(s))
            frames[i, off:off + len(chunk)] = chunk[: n_fft - off]
    spec = np.fft.rfft(frames * win, axis=1)
    mag = np.abs(spec).T * 2.0 / _window_sum(win)
    return (20.0 * np.log10(np.maximum(mag, 1e-9))).astype(np.float32)


# ------------------------------------------------------------------ silence
class EnvelopeBuilder:
    """Streaming RMS level (dBFS) per `hop_s` of audio, all channels combined."""

    def __init__(self, sr: int, channels: int, hop_s: float = 0.02) -> None:
        self.hop = max(1, int(round(hop_s * sr)))
        self.hop_s = self.hop / sr
        self.channels = channels
        self._carry = np.zeros((0, channels), np.float32)
        self._out: list[np.ndarray] = []

    def feed(self, chunk: np.ndarray) -> None:
        data = np.concatenate([self._carry, chunk]) if len(self._carry) else chunk
        n = (len(data) // self.hop) * self.hop
        if n:
            sq = (data[:n].astype(np.float64) ** 2).reshape(-1, self.hop, self.channels).mean(axis=(1, 2))
            self._out.append(10.0 * np.log10(np.maximum(sq, 1e-12)))
        self._carry = data[n:]

    def result(self) -> np.ndarray:
        out = list(self._out)
        if len(self._carry):
            sq = float((self._carry.astype(np.float64) ** 2).mean())
            out.append(np.array([10.0 * np.log10(max(sq, 1e-12))]))
        return np.concatenate(out) if out else np.zeros(0)


def frame_levels(seq: PieceSeq, a: int, b: int, hop_s: float = 0.02, *,
                 progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None) -> np.ndarray:
    """RMS level in dBFS for every `hop_s` of audio."""
    env = EnvelopeBuilder(seq.sr, seq.channels, hop_s)
    total = max(1, b - a)
    done = 0
    for chunk in seq.iter_chunks(a, b, size=env.hop * 4096):
        if cancel is not None:
            cancel.check()
        env.feed(chunk)
        done += len(chunk)
        if progress:
            progress(done / total)
    return env.result()


def find_silences(levels_db: np.ndarray, hop_s: float, threshold_db: float = -45.0,
                  min_length_s: float = 0.4) -> list[tuple[float, float]]:
    """Runs of frames below the threshold that last at least `min_length_s` (start, end in seconds)."""
    if len(levels_db) == 0:
        return []
    quiet = np.concatenate([[False], levels_db < threshold_db, [False]])
    edges = np.flatnonzero(np.diff(quiet.astype(np.int8)))
    runs = []
    for s, e in zip(edges[0::2], edges[1::2]):
        if (e - s) * hop_s >= min_length_s:
            runs.append((s * hop_s, e * hop_s))
    return runs
