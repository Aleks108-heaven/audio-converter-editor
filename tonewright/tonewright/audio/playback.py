"""Real-time playback with loop, pause, seek, live swapping and level metering.

A feeder thread reads blocks from an immutable snapshot of the document ahead of
the sound card, so editing never disturbs what is playing and the audio callback
only copies memory. `swap` hands the player a newer snapshot while it plays (the
editor and the mixer use it to keep the sound going through an edit) and `seek`
jumps within the current one; both pre-fill the new queue before switching to it,
so the card never runs dry and nothing is heard but the change itself.
"""
from __future__ import annotations

import queue
import threading
from typing import Callable, Iterator, Optional

import numpy as np

from ..core.document import Readable
from .devices import AudioDeviceError, sd

BLOCK = 2048
QUEUE_BLOCKS = 24
PREFILL_BLOCKS = 4          # read on the calling thread before a seek or swap takes effect (about 190 ms at 44.1 kHz)


class Player:
    def __init__(self, stream_factory: Optional[Callable] = None) -> None:
        self._factory = stream_factory
        self._stream = None
        self._lock = threading.RLock()
        self._pos_lock = threading.Lock()                # only ever held for an assignment or two, also by the audio thread
        self._gen = 0
        self._feedq: tuple[int, queue.Queue] = (0, queue.Queue(maxsize=QUEUE_BLOCKS))   # (generation, blocks), swapped as one
        self._seq: Optional[Readable] = None
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
    def sequence(self) -> Optional[Readable]:
        """What is being played now (None when stopped); the audio `swap` replaced is what this returned before."""
        return self._seq if self._state != "stopped" else None

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

    @property
    def start(self) -> int:
        """The frame playback began (or last restarted) from."""
        return self._start

    @property
    def end(self) -> int:
        """The frame playback runs up to (the end of the audio, or of the selection that was played)."""
        return self._end

    @property
    def loop_range(self) -> Optional[tuple[int, int]]:
        return (self._loop_a, self._loop_b) if self._loop else None

    def consume_finished(self) -> bool:
        """True once after playback ran off the end."""
        done, self._finished = self._finished, False
        return done

    def consume_peak(self) -> tuple[float, float]:
        peak = self._peak.copy()
        self._peak[:] = 0
        return float(peak[0]), float(peak[1])

    # ------------------------------------------------------------ control
    def play(self, seq: Readable, start: int = 0, end: Optional[int] = None, *, loop: bool = False,
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
            self._open_stream(seq.sr)
            self._begin_feed(start)
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

    @staticmethod
    def _blocks(seq: Readable, pos: int, end: int, loop: bool, loop_a: int, loop_b: int) -> Iterator[tuple[np.ndarray, int]]:
        """(block, first frame) from `pos` to the end, wrapping at the loop end while looping."""
        stop_at = loop_b if loop else end
        while True:
            if pos >= stop_at:
                if loop:
                    pos = loop_a
                    continue
                return
            n = min(BLOCK, stop_at - pos)
            block = seq.read(pos, pos + n)
            if block.shape[1] == 1:
                block = np.repeat(block, 2, axis=1)
            yield block, pos
            pos += n

    def _begin_feed(self, from_frame: int) -> None:
        """Start a new run of blocks at `from_frame` and make the sound card take it from the next callback on.

        The first few blocks are read here, so the new queue is already full when it is switched in. The old feeder
        notices the new generation and quits; whatever it had queued is simply never heard."""
        self._gen += 1
        gen = self._gen
        q: queue.Queue = queue.Queue(maxsize=QUEUE_BLOCKS)
        blocks = self._blocks(self._seq, from_frame, self._end, self._loop, self._loop_a, self._loop_b)
        finished = False
        for _ in range(PREFILL_BLOCKS):
            item = next(blocks, None)
            if item is None:
                q.put_nowait((gen, None, 0))
                finished = True
                break
            q.put_nowait((gen, item[0], item[1]))
        self._ended = False
        with self._pos_lock:                                     # the queue and the position change together, or a callback
            self._feedq = (gen, q)                               # already under way cannot write its old position over the new one
            self._pos_src = from_frame
        if not finished:
            threading.Thread(target=self._feed, args=(gen, q, blocks), daemon=True).start()

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
            self._start = frame                                  # the new lower bound for `position`, or a jump back would be clamped
            self._begin_feed(frame)

    def swap(self, seq: Readable, *, frame: Optional[int] = None, end: Optional[int] = None,
             loop_range: Optional[tuple[int, int]] = None) -> bool:
        """Carry on playing `seq` (a newer version of the audio) from `frame`, or from where the sound card is now.

        `end` and `loop_range` default to the whole of `seq`. Returns False when nothing was playing."""
        with self._lock:
            if self._seq is None or self._state == "stopped":
                return False
            total = seq.frames
            frame = self._pos_src if frame is None else frame
            end = total if end is None else min(end, total)
            if end <= 0:
                return False
            frame = max(0, min(frame, end - 1))
            self._seq = seq
            self._end = end
            self._start = frame                                  # the new lower bound for `position`
            if self._loop:
                self._loop_a, self._loop_b = loop_range if loop_range else (0, end)
                if not 0 <= self._loop_a < self._loop_b <= end:
                    self._loop_a, self._loop_b = 0, end
            self._begin_feed(frame)                              # also sets the position
            return True

    def stop(self) -> int:
        """Stop playback; returns the frame where it stopped."""
        with self._lock:
            pos = self.position if self._state != "stopped" else self._pos_src
            self._gen += 1
            self._feedq = (self._gen, queue.Queue(maxsize=1))        # whatever was queued is stale now
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
        return pos

    # ------------------------------------------------------------ threads
    def _feed(self, gen: int, q: queue.Queue, blocks: Iterator[tuple[np.ndarray, int]]) -> None:
        for block, pos in blocks:
            if not self._put(gen, q, (gen, block, pos)):
                return
        self._put(gen, q, (gen, None, 0))

    def _put(self, gen: int, q: queue.Queue, item) -> bool:
        while gen == self._gen:
            try:
                q.put(item, timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def _callback(self, outdata, frames, time_info, status) -> None:
        # The audio thread owns the "current block" state; other threads only replace self._feedq, which makes the
        # block stale. Work on locals so a concurrent stop/seek/swap cannot pull it away.
        outdata[:] = 0
        if self._paused:
            return
        live_gen, q = self._feedq
        cur, off, src, cgen = self._cur, self._cur_off, self._cur_src, self._cur_gen
        if cur is not None and cgen != live_gen:
            cur = None
        filled = 0
        pos = None
        while filled < frames:
            if cur is None:
                try:
                    gen, block, start = q.get_nowait()
                except queue.Empty:
                    break
                if gen != live_gen:
                    continue
                if block is None:
                    self._ended = True
                    break
                cur, off, src, cgen = block, 0, start, gen
            take = min(frames - filled, len(cur) - off)
            outdata[filled:filled + take] = cur[off:off + take]
            off += take
            filled += take
            pos = src + off
            if off >= len(cur):
                cur = None
        if pos is not None:
            with self._pos_lock:
                if self._feedq[0] == live_gen:                    # otherwise a seek or swap just took over the position
                    self._pos_src = pos
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
