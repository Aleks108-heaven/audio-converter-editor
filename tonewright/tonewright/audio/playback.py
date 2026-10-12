"""Real-time playback with loop, pause, seek and level metering.

A feeder thread reads blocks from an immutable snapshot of the document ahead of
the sound card, so editing never disturbs what is playing and the audio callback
only copies memory.
"""
from __future__ import annotations

import queue
import threading
from typing import Callable, Optional

import numpy as np

from ..core.document import PieceSeq
from .devices import AudioDeviceError, sd

BLOCK = 2048
QUEUE_BLOCKS = 24


class Player:
    def __init__(self, stream_factory: Optional[Callable] = None) -> None:
        self._factory = stream_factory
        self._stream = None
        self._lock = threading.RLock()
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_BLOCKS)
        self._gen = 0
        self._seq: Optional[PieceSeq] = None
        self._start = 0
        self._end = 0
        self._loop = False
        self._loop_a = 0
        self._loop_b = 0
        self._paused = False
        self._state = "stopped"
        self._cur: Optional[np.ndarray] = None
        self._cur_off = 0
        self._cur_src = 0
        self._cur_gen = 0
        self._pos_src = 0
        self._ended = False
        self._finished = False
        self._peak = np.zeros(2, np.float32)
        self.volume = 1.0
        self.device: Optional[int] = None

    # ------------------------------------------------------------ state
    @property
    def state(self) -> str:
        return self._state

    @property
    def is_playing(self) -> bool:
        return self._state == "playing"

    @property
    def position(self) -> int:
        """Frame currently being heard (compensating for the card's output latency)."""
        if self._seq is None:
            return 0
        lat = 0
        try:
            lat = int((self._stream.latency or 0) * self._seq.sr) if self._stream is not None else 0
        except (AttributeError, TypeError):
            lat = 0
        return max(self._loop_a if self._loop else self._start, self._pos_src - lat)

    def consume_finished(self) -> bool:
        """True once after playback ran off the end."""
        done, self._finished = self._finished, False
        return done

    def consume_peak(self) -> tuple[float, float]:
        peak = self._peak.copy()
        self._peak[:] = 0
        return float(peak[0]), float(peak[1])

    # ------------------------------------------------------------ control
    def play(self, seq: PieceSeq, start: int = 0, end: Optional[int] = None, *, loop: bool = False,
             loop_range: Optional[tuple[int, int]] = None) -> None:
        self.stop()
        total = seq.frames
        end = total if end is None else min(end, total)
        start = max(0, start)
        if end <= 0 or start >= end:
            self._finished = True
            return
        with self._lock:
            self._seq = seq
            self._start, self._end = start, end
            self._loop = loop
            self._loop_a, self._loop_b = loop_range if loop_range else (0 if end == total else start, end)
            if self._loop_b <= self._loop_a:
                self._loop_a, self._loop_b = 0, end
            self._paused = False
            self._ended = False
            self._finished = False
            self._cur = None
            self._pos_src = start
            self._drain()
            self._open_stream(seq.sr)
            self._spawn_feeder(start)
            self._state = "playing"
            self._stream.start()

    def _open_stream(self, sr: int) -> None:
        factory = self._factory or (sd.OutputStream if sd is not None else None)
        if factory is None:
            raise AudioDeviceError("No audio output is available (PortAudio could not be loaded).")
        try:
            self._stream = factory(samplerate=sr, channels=2, dtype="float32", callback=self._callback,
                                   finished_callback=self._on_finished, device=self.device)
        except Exception as exc:  # PortAudioError and friends
            self._stream = None
            raise AudioDeviceError(f"Could not open the audio output: {exc}") from exc

    def _drain(self) -> None:
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    def _spawn_feeder(self, from_frame: int) -> None:
        self._gen += 1
        self._drain()
        self._ended = False
        threading.Thread(target=self._feed, args=(self._gen, self._seq, from_frame, self._end, self._loop,
                                                  self._loop_a, self._loop_b), daemon=True).start()

    def pause(self) -> None:
        if self._state == "playing":
            self._paused = True
            self._state = "paused"

    def resume(self) -> None:
        if self._state == "paused":
            self._paused = False
            self._state = "playing"

    def toggle_pause(self) -> None:
        self.resume() if self._state == "paused" else self.pause()

    def seek(self, frame: int) -> None:
        with self._lock:
            if self._seq is None or self._state == "stopped":
                return
            frame = max(0, min(frame, self._end - 1))
            self._pos_src = frame
            self._spawn_feeder(frame)

    def stop(self) -> int:
        """Stop playback; returns the frame where it stopped."""
        with self._lock:
            pos = self.position if self._state != "stopped" else self._pos_src
            self._gen += 1
            stream, self._stream = self._stream, None
            self._state = "stopped"
            self._paused = False
            self._cur = None
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        self._drain()
        return pos

    # ------------------------------------------------------------ threads
    def _feed(self, gen: int, seq: PieceSeq, pos: int, end: int, loop: bool, loop_a: int, loop_b: int) -> None:
        stop_at = loop_b if loop else end
        while gen == self._gen:
            if pos >= stop_at:
                if loop:
                    pos = loop_a
                    continue
                self._put(gen, (gen, None, 0))
                return
            n = min(BLOCK, stop_at - pos)
            block = seq.read(pos, pos + n)
            if block.shape[1] == 1:
                block = np.repeat(block, 2, axis=1)
            if not self._put(gen, (gen, block, pos)):
                return
            pos += n

    def _put(self, gen: int, item) -> bool:
        while gen == self._gen:
            try:
                self._queue.put(item, timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def _callback(self, outdata, frames, time_info, status) -> None:
        # The audio thread owns the "current block" state; other threads only bump self._gen,
        # which makes the block stale. Work on locals so a concurrent stop/seek cannot pull it away.
        outdata[:] = 0
        if self._paused:
            return
        cur, off, src, cgen = self._cur, self._cur_off, self._cur_src, self._cur_gen
        if cur is not None and cgen != self._gen:
            cur = None
        filled = 0
        while filled < frames:
            if cur is None:
                try:
                    gen, block, start = self._queue.get_nowait()
                except queue.Empty:
                    break
                if gen != self._gen:
                    continue
                if block is None:
                    self._ended = True
                    break
                cur, off, src, cgen = block, 0, start, gen
            take = min(frames - filled, len(cur) - off)
            outdata[filled:filled + take] = cur[off:off + take]
            off += take
            filled += take
            self._pos_src = src + off
            if off >= len(cur):
                cur = None
        self._cur, self._cur_off, self._cur_src, self._cur_gen = cur, off, src, cgen
        if self.volume != 1.0:
            outdata *= self.volume
        np.maximum(self._peak, np.abs(outdata).max(axis=0), out=self._peak)
        if self._ended and filled < frames:
            raise sd.CallbackStop if sd is not None else StopIteration
    def _on_finished(self) -> None:
        if self._ended and self._state != "stopped":
            self._state = "stopped"
            self._finished = True
