"""The editable document: a piece table over immutable Sources, with history.

Edits never copy audio. Deleting, inserting or pasting only rearranges
(source, start, length) references, so they are instant and every history step
is a cheap immutable snapshot.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, NamedTuple, Optional, Sequence

import numpy as np

from .source import Source

HISTORY_LIMIT = 200


@dataclass(frozen=True, eq=False)
class Piece:
    src: Source
    start: int
    length: int

    @property
    def end(self) -> int:
        return self.start + self.length


class Marker(NamedTuple):
    frame: int
    name: str


def normalize_pieces(pieces: Iterable[Piece]) -> tuple[Piece, ...]:
    """Drop empty pieces and merge neighbours that are contiguous in one source."""
    out: list[Piece] = []
    for p in pieces:
        if p.length <= 0:
            continue
        if out and out[-1].src is p.src and out[-1].end == p.start:
            out[-1] = Piece(p.src, out[-1].start, out[-1].length + p.length)
        else:
            out.append(p)
    return tuple(out)


def slice_pieces(pieces: Sequence[Piece], a: int, b: int) -> list[Piece]:
    """The pieces that make up frames [a, b) of a piece list."""
    out: list[Piece] = []
    pos = 0
    for p in pieces:
        p0, p1 = pos, pos + p.length
        pos = p1
        if p1 <= a:
            continue
        if p0 >= b:
            break
        lo, hi = max(a, p0), min(b, p1)
        out.append(Piece(p.src, p.start + lo - p0, hi - lo))
    return out


class PieceSeq:
    """Read access shared by Document, Clip and snapshots."""

    sr: int
    channels: int

    def _pieces(self) -> tuple[Piece, ...]:
        raise NotImplementedError

    @property
    def frames(self) -> int:
        return sum(p.length for p in self._pieces())

    @property
    def duration(self) -> float:
        return self.frames / self.sr if self.sr else 0.0

    def read(self, a: int, b: int) -> np.ndarray:
        """Frames [a, b) as a fresh (n, channels) float32 array."""
        pieces = self._pieces()
        total = sum(p.length for p in pieces)
        a, b = max(0, a), min(total, b)
        if b <= a:
            return np.zeros((0, self.channels), np.float32)
        parts: list[np.ndarray] = []
        pos = 0
        for p in pieces:
            p0, p1 = pos, pos + p.length
            pos = p1
            if p1 <= a:
                continue
            if p0 >= b:
                break
            lo, hi = max(a, p0), min(b, p1)
            parts.append(p.src.read(p.start + lo - p0, p.start + hi - p0))
        if len(parts) == 1:
            return np.array(parts[0], dtype=np.float32)
        return np.concatenate(parts)

    def iter_chunks(self, a: int = 0, b: Optional[int] = None, size: int = 1 << 18) -> Iterator[np.ndarray]:
        total = self.frames
        b = total if b is None else min(b, total)
        pos = max(0, a)
        while pos < b:
            n = min(size, b - pos)
            yield self.read(pos, pos + n)
            pos += n

    def peaks(self, start: int, stop: int, width: int) -> tuple[np.ndarray, np.ndarray]:
        """Per-pixel (min, max) as two (channels, width) arrays for frames [start, stop).
        Needs at least one frame per pixel; zoomed-in views should read() instead."""
        ch = self.channels
        total = stop - start
        if total <= 0 or width <= 0:
            z = np.zeros((ch, max(width, 0)), np.float32)
            return z, z.copy()
        fpp = total / width
        if fpp < 1:
            raise ValueError("fewer frames than pixels; use read() instead")
        mins = np.full((ch, width), np.inf, np.float32)
        maxs = np.full((ch, width), -np.inf, np.float32)
        pos = 0
        for p in self._pieces():
            p0, p1 = pos, pos + p.length
            pos = p1
            if p1 <= start:
                continue
            if p0 >= stop:
                break
            a, b = max(p0, start), min(p1, stop)
            if p.length <= max(8.0, 4 * fpp):
                seg = p.src.read(p.start + a - p0, p.start + b - p0)
                idx = np.clip(((np.arange(a, a + len(seg)) - start) / fpp).astype(np.int64), 0, width - 1)
                for c in range(ch):
                    np.minimum.at(mins[c], idx, seg[:, c])
                    np.maximum.at(maxs[c], idx, seg[:, c])
                continue
            px0 = int((a - start) // fpp)
            px1 = min(width, int(math.ceil((b - start) / fpp)))
            edges = start + np.arange(px0, px1 + 1) * fpp
            edges = np.rint(np.clip(edges, a, b)).astype(np.int64)
            mn, mx = p.src.minmax(p.start + (edges - p0))
            mins[:, px0:px1] = np.minimum(mins[:, px0:px1], mn.T)
            maxs[:, px0:px1] = np.maximum(maxs[:, px0:px1], mx.T)
        mins[~np.isfinite(mins)] = 0.0
        maxs[~np.isfinite(maxs)] = 0.0
        return mins, maxs


class Clip(PieceSeq):
    """An immutable run of audio: the clipboard payload and a read-only snapshot."""

    def __init__(self, pieces: Iterable[Piece], sr: int, channels: int) -> None:
        self.pieces = normalize_pieces(pieces)
        self.sr = sr
        self.channels = channels

    def _pieces(self) -> tuple[Piece, ...]:
        return self.pieces

    @classmethod
    def from_source(cls, src: Source) -> "Clip":
        return cls([Piece(src, 0, src.frames)], src.sr, src.channels)


_state_ids = itertools.count(1)


@dataclass(frozen=True, eq=False)
class DocState:
    pieces: tuple[Piece, ...]
    markers: tuple[Marker, ...]
    sr: int
    channels: int
    label: str
    frames: int
    id: int


def _shift_markers(markers: Sequence[Marker], a: int, b: int, inserted: int) -> tuple[Marker, ...]:
    """Markers after replacing frames [a, b) with `inserted` new frames."""
    delta = inserted - (b - a)
    out = []
    for m in markers:
        if m.frame < a:
            out.append(m)
        elif m.frame >= b:
            out.append(Marker(m.frame + delta, m.name))
        # markers strictly inside a removed range disappear with it
    return tuple(out)


class Document(PieceSeq):
    def __init__(self, sr: int, channels: int, pieces: Iterable[Piece] = (), *,
                 name: str = "Untitled", path: Optional[Path] = None,
                 meta: Optional[dict] = None) -> None:
        self.name = name
        self.path = Path(path) if path else None
        self.meta: dict = dict(meta or {})
        self._listeners: list[Callable[[str], None]] = []
        first = self._make_state(normalize_pieces(pieces), (), sr, channels, "Open")
        self._history: list[DocState] = [first]
        self._index = 0
        self._saved_id: Optional[int] = first.id

    @classmethod
    def from_source(cls, src: Source, **kw) -> "Document":
        return cls(src.sr, src.channels, [Piece(src, 0, src.frames)], **kw)

    # ---- state access -------------------------------------------------
    @staticmethod
    def _make_state(pieces, markers, sr, channels, label) -> DocState:
        return DocState(pieces, tuple(markers), sr, channels, label,
                        sum(p.length for p in pieces), next(_state_ids))

    @property
    def state(self) -> DocState:
        return self._history[self._index]

    def _pieces(self) -> tuple[Piece, ...]:
        return self.state.pieces

    @property
    def sr(self) -> int:  # type: ignore[override]
        return self.state.sr

    @property
    def channels(self) -> int:  # type: ignore[override]
        return self.state.channels

    @property
    def frames(self) -> int:
        return self.state.frames

    @property
    def markers(self) -> tuple[Marker, ...]:
        return self.state.markers

    def snapshot(self) -> Clip:
        """Frozen view of the current audio, safe to hand to a worker thread."""
        st = self.state
        return Clip(st.pieces, st.sr, st.channels)

    # ---- observers ----------------------------------------------------
    def add_listener(self, fn: Callable[[str], None]) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[str], None]) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def _notify(self, kind: str) -> None:
        for fn in list(self._listeners):
            fn(kind)

    # ---- history ------------------------------------------------------
    @property
    def modified(self) -> bool:
        return self.state.id != self._saved_id

    def mark_saved(self) -> None:
        self._saved_id = self.state.id
        self._notify("saved")

    @property
    def can_undo(self) -> bool:
        return self._index > 0

    @property
    def can_redo(self) -> bool:
        return self._index < len(self._history) - 1

    def undo_labels(self) -> list[str]:
        """Most recent first: what each successive undo would revert."""
        return [self._history[i].label for i in range(self._index, 0, -1)]

    def redo_labels(self) -> list[str]:
        return [self._history[i].label for i in range(self._index + 1, len(self._history))]

    def history_labels(self) -> list[str]:
        return [s.label for s in self._history]

    @property
    def history_index(self) -> int:
        return self._index

    def goto_history(self, index: int) -> None:
        index = max(0, min(index, len(self._history) - 1))
        if index != self._index:
            old = self.state
            self._index = index
            self._notify(self._kind_of_change(old, self.state))

    def undo(self, steps: int = 1) -> None:
        self.goto_history(self._index - steps)

    def redo(self, steps: int = 1) -> None:
        self.goto_history(self._index + steps)

    @staticmethod
    def _kind_of_change(old: DocState, new: DocState) -> str:
        if old.sr != new.sr or old.channels != new.channels:
            return "format"
        if old.pieces != new.pieces:
            return "content"
        return "markers"

    def _commit(self, pieces: Iterable[Piece], label: str, *,
                markers: Optional[Sequence[Marker]] = None,
                sr: Optional[int] = None, channels: Optional[int] = None) -> None:
        cur = self.state
        new = self._make_state(
            normalize_pieces(pieces),
            cur.markers if markers is None else markers,
            cur.sr if sr is None else sr,
            cur.channels if channels is None else channels,
            label)
        del self._history[self._index + 1:]
        self._history.append(new)
        if len(self._history) > HISTORY_LIMIT:
            drop = len(self._history) - HISTORY_LIMIT
            del self._history[:drop]
        self._index = len(self._history) - 1
        self._notify(self._kind_of_change(cur, new))

    # ---- editing ------------------------------------------------------
    def _clamp(self, a: int, b: int) -> tuple[int, int]:
        a, b = int(a), int(b)
        if a > b:
            a, b = b, a
        return max(0, a), min(self.frames, b)

    def replace(self, a: int, b: int, new: Iterable[Piece], label: str) -> None:
        """Swap frames [a, b) for `new` pieces; markers inside the range are dropped."""
        a, b = self._clamp(a, b)
        new = list(new)
        inserted = sum(p.length for p in new)
        if a == b and inserted == 0:
            return
        pieces = self.state.pieces
        merged = slice_pieces(pieces, 0, a) + new + slice_pieces(pieces, b, self.frames)
        self._commit(merged, label, markers=_shift_markers(self.markers, a, b, inserted))

    def delete(self, a: int, b: int, label: str = "Delete") -> None:
        self.replace(a, b, (), label)

    def delete_ranges(self, ranges: Iterable[tuple[int, int]], label: str = "Delete") -> None:
        """Delete several ranges as one undoable step (overlapping ranges are merged)."""
        total = self.frames
        merged: list[list[int]] = []
        for a, b in sorted((max(0, min(x, y)), min(total, max(x, y))) for x, y in ranges):
            if b <= a:
                continue
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        if not merged:
            return
        pieces = self.state.pieces
        keep: list[Piece] = []
        pos = 0
        for a, b in merged:
            keep += slice_pieces(pieces, pos, a)
            pos = b
        keep += slice_pieces(pieces, pos, total)
        markers: list[Marker] = []
        for m in self.markers:
            removed_before, inside = 0, False
            for a, b in merged:
                if m.frame >= b:
                    removed_before += b - a
                elif m.frame >= a:
                    inside = True
                    break
            if not inside:
                markers.append(Marker(m.frame - removed_before, m.name))
        self._commit(keep, label, markers=markers)

    def insert(self, pos: int, clip: PieceSeq, label: str = "Paste") -> None:
        self._require_matching(clip)
        pos = max(0, min(int(pos), self.frames))
        self.replace(pos, pos, clip._pieces(), label)

    def replace_with_source(self, a: int, b: int, src: Source, label: str) -> None:
        if src.channels != self.channels or src.sr != self.sr:
            raise ValueError("source format does not match the document")
        self.replace(a, b, [Piece(src, 0, src.frames)], label)

    def crop(self, a: int, b: int, label: str = "Trim to Selection") -> None:
        a, b = self._clamp(a, b)
        if b <= a:
            return
        kept = slice_pieces(self.state.pieces, a, b)
        markers = [Marker(m.frame - a, m.name) for m in self.markers if a <= m.frame < b]
        self._commit(kept, label, markers=markers)

    def silence(self, a: int, b: int, label: str = "Silence") -> None:
        a, b = self._clamp(a, b)
        if b <= a:
            return
        zero = Source.silence(b - a, self.sr, self.channels)
        pieces = self.state.pieces
        merged = slice_pieces(pieces, 0, a) + [Piece(zero, 0, b - a)] + slice_pieces(pieces, b, self.frames)
        self._commit(merged, label)

    def copy(self, a: int, b: int) -> Clip:
        a, b = self._clamp(a, b)
        return Clip(slice_pieces(self.state.pieces, a, b), self.sr, self.channels)

    def cut(self, a: int, b: int) -> Clip:
        clip = self.copy(a, b)
        self.delete(a, b, "Cut")
        return clip

    def replace_all(self, src: Source, label: str, *, keep_markers_scale: bool = True) -> None:
        """Replace the whole document with a (possibly different-format) source."""
        old = self.state
        markers: Sequence[Marker] = ()
        if keep_markers_scale and old.sr and old.frames:
            ratio = src.sr / old.sr
            markers = [Marker(int(round(m.frame * ratio)), m.name) for m in old.markers]
        self._commit([Piece(src, 0, src.frames)], label, markers=markers,
                     sr=src.sr, channels=src.channels)

    def _require_matching(self, clip: PieceSeq) -> None:
        if clip.sr != self.sr or clip.channels != self.channels:
            raise ValueError("clip format does not match the document")

    # ---- markers ------------------------------------------------------
    def add_marker(self, frame: int, name: str = "") -> None:
        frame = max(0, min(int(frame), self.frames))
        marks = sorted(self.markers + (Marker(frame, name or f"Marker {len(self.markers) + 1}"),))
        self._commit(self.state.pieces, "Add marker", markers=marks)

    def remove_marker(self, index: int) -> None:
        marks = list(self.markers)
        if 0 <= index < len(marks):
            del marks[index]
            self._commit(self.state.pieces, "Remove marker", markers=marks)

    def rename_marker(self, index: int, name: str) -> None:
        marks = list(self.markers)
        if 0 <= index < len(marks):
            marks[index] = Marker(marks[index].frame, name)
            self._commit(self.state.pieces, "Rename marker", markers=marks)

    def set_markers(self, markers: Iterable[Marker], label: str = "Edit markers") -> None:
        marks = sorted((Marker(max(0, min(int(m.frame), self.frames)), m.name) for m in markers),
                       key=lambda m: m.frame)
        self._commit(self.state.pieces, label, markers=marks)
