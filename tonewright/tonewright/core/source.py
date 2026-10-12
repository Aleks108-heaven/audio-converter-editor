"""Immutable blocks of audio and the writer that builds them.

A Source is never modified once created. The Document edits by rearranging
references to Sources (a piece table), which is what makes cut, paste and undo
instant even on hour-long files. Large Sources live in scratch files on disk
rather than in RAM.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from .. import paths

SPILL_BYTES = 48 * 1024 * 1024   # sources larger than this are kept on disk
PEAK_BASE = 64                   # frames summarised by one level-0 peak block
PEAK_FACTOR = 8                  # every further level summarises 8 blocks of the last
_SCAN_CHUNK = 1 << 21            # frames per bulk read while scanning a source


class _RamStore:
    def __init__(self, data: np.ndarray) -> None:
        self.data = data

    def read(self, a: int, b: int) -> np.ndarray:
        return self.data[a:b]

    def close(self) -> None:
        self.data = np.zeros((0, self.data.shape[1]), np.float32)


class _ZeroStore:
    """Silence that costs no storage."""

    def __init__(self, channels: int) -> None:
        self.channels = channels

    def read(self, a: int, b: int) -> np.ndarray:
        return np.zeros((b - a, self.channels), np.float32)

    def close(self) -> None:
        pass


class _FileStore:
    """Float32 frames in a scratch file. Reads are seek+readinto under a lock, so no
    memory map is left open (which would block deleting the file on Windows)."""

    def __init__(self, path: Path, channels: int) -> None:
        self.path = Path(path)
        self.channels = channels
        self._fh = open(self.path, "rb", buffering=0)
        self._lock = threading.Lock()

    def read(self, a: int, b: int) -> np.ndarray:
        out = np.zeros((b - a, self.channels), np.float32)
        raw = out.reshape(-1).view(np.uint8)
        want = raw.size
        got = 0
        with self._lock:
            if self._fh is None:
                return out
            self._fh.seek(a * self.channels * 4)
            while got < want:
                n = self._fh.readinto(raw[got:])
                if not n:
                    break
                got += n
        return out

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
        try:
            self.path.unlink()
        except OSError:
            pass


class Peaks:
    """Min/max pyramid used to draw a waveform at any zoom without touching raw audio."""

    def __init__(self, mins: list[np.ndarray], maxs: list[np.ndarray]) -> None:
        self.mins = mins
        self.maxs = maxs

    @staticmethod
    def block(level: int) -> int:
        return PEAK_BASE * PEAK_FACTOR ** level

    @property
    def levels(self) -> int:
        return len(self.mins)

    @classmethod
    def build(cls, src: "Source") -> "Peaks":
        ch = src.channels
        n0 = max(1, -(-src.frames // PEAK_BASE))
        mn0 = np.zeros((n0, ch), np.float32)
        mx0 = np.zeros((n0, ch), np.float32)
        step = (_SCAN_CHUNK // PEAK_BASE) * PEAK_BASE
        pos = blk = 0
        silent = isinstance(src._store, _ZeroStore)
        while pos < src.frames and not silent:
            chunk = src.read(pos, min(src.frames, pos + step))
            n = chunk.shape[0]
            full, rest = divmod(n, PEAK_BASE)
            if full:
                # channel-major, so each 64-frame block is contiguous: ~13x faster than reducing
                # over the middle axis of an interleaved (blocks, 64, channels) view
                v = np.ascontiguousarray(chunk[: full * PEAK_BASE].T).reshape(ch, full, PEAK_BASE)
                mn0[blk:blk + full] = v.min(axis=2).T
                mx0[blk:blk + full] = v.max(axis=2).T
            if rest:
                tail = chunk[full * PEAK_BASE:]
                mn0[blk + full] = tail.min(axis=0)
                mx0[blk + full] = tail.max(axis=0)
            blk += full + (1 if rest else 0)
            pos += n
        mins, maxs = [mn0], [mx0]
        while mins[-1].shape[0] > 64:
            mins.append(_reduce(mins[-1], np.min, np.inf))
            maxs.append(_reduce(maxs[-1], np.max, -np.inf))
        return cls(mins, maxs)


def _reduce(arr: np.ndarray, fn, pad_value: float) -> np.ndarray:
    n, ch = arr.shape
    pad = (-n) % PEAK_FACTOR
    if pad:
        arr = np.concatenate([arr, np.full((pad, ch), pad_value, np.float32)])
    return fn(arr.reshape(-1, PEAK_FACTOR, ch), axis=1)


class Source:
    def __init__(self, store, sr: int, frames: int, channels: int, label: str = "") -> None:
        self._store = store
        self.sr = sr
        self.frames = frames
        self.channels = channels
        self.label = label
        self._peaks: Optional[Peaks] = None
        self._peak_lock = threading.Lock()

    @classmethod
    def from_array(cls, data: np.ndarray, sr: int, label: str = "") -> "Source":
        arr = np.ascontiguousarray(data, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr[:, None]
        return cls(_RamStore(arr), sr, arr.shape[0], arr.shape[1], label)

    @classmethod
    def silence(cls, frames: int, sr: int, channels: int) -> "Source":
        return cls(_ZeroStore(channels), sr, frames, channels, "silence")

    def read(self, a: int, b: int) -> np.ndarray:
        """Frames [a, b) as (n, channels) float32. Treat the result as read-only."""
        a = max(0, a)
        b = min(self.frames, b)
        if b <= a:
            return np.zeros((0, self.channels), np.float32)
        return self._store.read(a, b)

    def peaks(self) -> Peaks:
        with self._peak_lock:
            if self._peaks is None:
                self._peaks = Peaks.build(self)
            return self._peaks

    @property
    def has_peaks(self) -> bool:
        return self._peaks is not None

    def minmax(self, edges: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Per-interval (min, max) over consecutive frame edges, as (k, channels) arrays."""
        edges = np.asarray(edges, dtype=np.int64)
        k = len(edges) - 1
        if k <= 0:
            z = np.zeros((0, self.channels), np.float32)
            return z, z
        per = (edges[-1] - edges[0]) / k
        level = -1
        if per / 2 >= PEAK_BASE:
            level = int(np.floor(np.log(per / 2 / PEAK_BASE) / np.log(PEAK_FACTOR)))
            level = min(level, self.peaks().levels - 1)
        if level < 0:
            seg = self.read(int(edges[0]), int(edges[-1]))
            if len(seg) == 0:
                z = np.zeros((k, self.channels), np.float32)
                return z, z
            starts = np.minimum(edges[:-1] - edges[0], len(seg) - 1)
            return (np.minimum.reduceat(seg, starts, axis=0),
                    np.maximum.reduceat(seg, starts, axis=0))
        pk = self.peaks()
        size = Peaks.block(level)
        mn, mx = pk.mins[level], pk.maxs[level]
        n_blocks = mn.shape[0]
        lo = np.minimum(edges // size, n_blocks - 1)
        hi = min(n_blocks, -(-int(edges[-1]) // size))
        hi = max(hi, int(lo[:-1].max()) + 1)
        first = int(lo[0])
        starts = lo[:-1] - first
        sl = slice(first, hi)
        return (np.minimum.reduceat(mn[sl], starts, axis=0),
                np.maximum.reduceat(mx[sl], starts, axis=0))

    def close(self) -> None:
        store, self._store = self._store, _RamStore(np.zeros((0, self.channels), np.float32))
        try:
            store.close()
        except Exception:
            pass
        self.frames = 0

    def __del__(self) -> None:
        try:
            if isinstance(self._store, _FileStore):
                self._store.close()
        except Exception:
            pass


class SourceWriter:
    """Collects float32 frames, spilling to a scratch file once they get large."""

    def __init__(self, sr: int, channels: int, label: str = "", spill_bytes: int = SPILL_BYTES) -> None:
        self.sr = sr
        self.channels = channels
        self.label = label
        self.frames = 0
        self._spill_bytes = spill_bytes
        self._chunks: list[np.ndarray] = []
        self._path: Optional[Path] = None
        self._fh = None

    def write(self, data: np.ndarray) -> None:
        arr = np.asarray(data, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr[:, None]
        if arr.shape[1] != self.channels:
            raise ValueError(f"expected {self.channels} channels, got {arr.shape[1]}")
        if arr.shape[0] == 0:
            return
        self.frames += arr.shape[0]
        if self._fh is not None:
            self._fh.write(memoryview(np.ascontiguousarray(arr)).cast("B"))
            return
        # the caller may reuse its buffer, so keep our own copy while in RAM
        self._chunks.append(np.array(arr, dtype=np.float32, order="C", copy=True))
        if self.frames * self.channels * 4 > self._spill_bytes:
            self._path = paths.new_scratch_file()
            self._fh = open(self._path, "wb")
            for c in self._chunks:
                self._fh.write(memoryview(c).cast("B"))
            self._chunks = []

    def finish(self) -> Source:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            store = _FileStore(self._path, self.channels)
            return Source(store, self.sr, self.frames, self.channels, self.label)
        if self._chunks:
            data = self._chunks[0] if len(self._chunks) == 1 else np.concatenate(self._chunks)
        else:
            data = np.zeros((0, self.channels), np.float32)
        self._chunks = []
        return Source.from_array(data, self.sr, self.label)

    def abort(self) -> None:
        self._chunks = []
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        if self._path is not None:
            try:
                os.unlink(self._path)
            except OSError:
                pass
            self._path = None
