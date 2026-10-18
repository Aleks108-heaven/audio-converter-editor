"""Multi-track mixing: lines (tracks) of clips with level, pan, mute and solo, fades for transitions, effect spans
over the mix, and a streaming renderer that builds the whole file.

Everything here is plain Python and numpy (no Qt). Clips only *reference* audio (`PieceSeq`, the same zero-copy
snapshots the editor uses), so adding, moving, trimming and splitting never copy samples.
"""
from __future__ import annotations

import copy
import json
import math
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import numpy as np

from . import effects as fx
from .common import Cancelled, CancelToken, ProgressFn, scaled
from .document import Clip, PieceSeq, slice_pieces
from .source import SourceWriter
from .timefmt import db_to_amp

CHANNELS = 2
DEFAULT_SR = 44100
BLOCK = 1 << 16
MIN_CLIP = 64                       # frames; a clip can never be trimmed away completely
VOLUME_RANGE = (-60.0, 12.0)         # dB, per line
CLIP_GAIN_RANGE = (-30.0, 12.0)      # dB, per clip
HISTORY = 100
PROJECT_VERSION = 1
CUSTOM_FILTER_EFFECTS = frozenset({"custom_filter"})


def custom_filters_in(data) -> list[str]:
    """The free-text FFmpeg filter chains a loaded project file asks to run (empty for anything malformed)."""
    if not isinstance(data, dict) or not isinstance(data.get("effects"), list):
        return []
    chains = []
    for e in data["effects"]:
        if isinstance(e, dict) and e.get("effect") in CUSTOM_FILTER_EFFECTS:
            params = e.get("params")
            chains.append(str(params.get("chain", "")) if isinstance(params, dict) else "")
    return chains


@dataclass
class Line:
    id: int
    name: str
    volume_db: float = 0.0
    pan: float = 0.0                 # -1 left .. +1 right (a balance control)
    mute: bool = False
    solo: bool = False


@dataclass
class MixClip:
    id: int
    line: int                        # Line.id
    seq: PieceSeq                    # audio at the project's sample rate, stereo
    start: int                       # where it begins in the mix (frames)
    offset: int = 0                  # first frame of `seq` that is used
    length: int = 0                  # frames used
    gain_db: float = 0.0
    fade_in: int = 0                 # frames; sine ramp, so two overlapping clips cross-fade at equal power
    fade_out: int = 0
    name: str = ""
    path: Optional[str] = None       # the file it came from, when there is one (needed to save a project)

    @property
    def end(self) -> int:
        return self.start + self.length


@dataclass
class EffectSpan:
    id: int
    effect: str                      # id in the effects registry
    params: dict
    start: int
    end: int
    mute: bool = False


# ------------------------------------------------------------------ helpers
def balance_gains(pan: float) -> tuple[float, float]:
    """Left/right gain for a balance control: the louder side stays at unity, the other fades out."""
    pan = max(-1.0, min(1.0, pan))
    return (1.0 - pan if pan > 0 else 1.0), (1.0 + pan if pan < 0 else 1.0)


def fade_envelope(first: int, count: int, length: int, fade_in: int, fade_out: int) -> Optional[np.ndarray]:
    """Gain for clip frames [first, first+count) of a clip `length` frames long, or None when it is all 1."""
    fade_in, fade_out = max(0, min(fade_in, length)), max(0, min(fade_out, length))
    if not fade_in and not fade_out:
        return None
    k = np.arange(first, first + count, dtype=np.float64)
    env = np.ones(count, np.float64)
    if fade_in:
        env = np.where(k < fade_in, np.sin(0.5 * math.pi * np.clip(k / fade_in, 0.0, 1.0)), env)
    if fade_out:
        t = np.clip((k - (length - fade_out)) / fade_out, 0.0, 1.0)
        env = env * np.where(k >= length - fade_out, np.cos(0.5 * math.pi * t), 1.0)
    return env.astype(np.float32)


def _mix_range(jobs: list[tuple[MixClip, np.ndarray]], a: int, b: int, master: float = 1.0) -> np.ndarray:
    """Frames [a, b) of the lines mixed together, as a fresh (b - a, 2) array."""
    mix = np.zeros((b - a, CHANNELS), np.float32)
    for c, gains in jobs:
        lo, hi = max(a, c.start), min(b, c.end)
        if hi <= lo:
            continue
        data = c.seq.read(c.offset + lo - c.start, c.offset + hi - c.start)
        n = len(data)
        if not n:
            continue
        data = data * gains
        env = fade_envelope(lo - c.start, n, c.length, c.fade_in, c.fade_out)
        if env is not None:
            data = data * env[:, None]
        mix[lo - a: lo - a + n] += data
    if master != 1.0:
        mix *= np.float32(master)
    return mix


# ------------------------------------------------------------------ the project
class MixProject:
    def __init__(self, sr: int = DEFAULT_SR) -> None:
        self.sr = sr
        self.lines: list[Line] = []
        self.clips: list[MixClip] = []
        self.effects: list[EffectSpan] = []
        self.version = 0                          # bumps on every change; caches key on it
        self.name = ""
        self.skipped_effects: list[str] = []      # effects a loaded project asked for but was not allowed to run
        self._ids = 0
        self._undo: list[tuple[str, tuple]] = []
        self._redo: list[tuple[str, tuple]] = []

    # ---- ids and bookkeeping
    def _next_id(self) -> int:
        self._ids += 1
        return self._ids

    def touch(self) -> None:
        self.version += 1

    @property
    def empty(self) -> bool:
        return not self.clips

    @property
    def total_frames(self) -> int:
        return max((c.end for c in self.clips), default=0)

    @property
    def duration(self) -> float:
        return self.total_frames / self.sr

    def line(self, line_id: int) -> Line:
        return next(l for l in self.lines if l.id == line_id)

    def clip(self, clip_id: int) -> MixClip:
        return next(c for c in self.clips if c.id == clip_id)

    def effect_span(self, span_id: int) -> EffectSpan:
        return next(e for e in self.effects if e.id == span_id)

    def clips_on(self, line_id: int) -> list[MixClip]:
        return sorted((c for c in self.clips if c.line == line_id), key=lambda c: (c.start, c.id))

    # ---- history (snapshots of the small bookkeeping objects; audio is shared, never copied)
    def _state(self) -> tuple:
        return (tuple(replace(l) for l in self.lines), tuple(replace(c) for c in self.clips),
                tuple(replace(e, params=copy.deepcopy(e.params)) for e in self.effects), self._ids)

    def _restore(self, state: tuple) -> None:
        lines, clips, effects, ids = state
        self.lines = [replace(l) for l in lines]
        self.clips = [replace(c) for c in clips]
        self.effects = [replace(e, params=copy.deepcopy(e.params)) for e in effects]
        self._ids = max(self._ids, ids)
        self.touch()

    def checkpoint(self, label: str) -> None:
        """Call before a change so it can be undone as one step."""
        self._undo.append((label, self._state()))
        del self._undo[:-HISTORY]
        self._redo.clear()

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_label(self) -> str:
        return self._undo[-1][0] if self._undo else ""

    @property
    def redo_label(self) -> str:
        return self._redo[-1][0] if self._redo else ""

    def undo(self) -> Optional[str]:
        if not self._undo:
            return None
        label, state = self._undo.pop()
        self._redo.append((label, self._state()))
        self._restore(state)
        return label

    def redo(self) -> Optional[str]:
        if not self._redo:
            return None
        label, state = self._redo.pop()
        self._undo.append((label, self._state()))
        self._restore(state)
        return label

    # ---- lines
    def add_line(self, name: Optional[str] = None) -> Line:
        number = 1
        taken = {l.name for l in self.lines}
        while f"Line {number}" in taken:
            number += 1
        line = Line(self._next_id(), name or f"Line {number}")
        self.lines.append(line)
        self.touch()
        return line

    def remove_line(self, line_id: int) -> None:
        self.lines = [l for l in self.lines if l.id != line_id]
        self.clips = [c for c in self.clips if c.line != line_id]
        self.touch()

    def set_line(self, line_id: int, *, name: Optional[str] = None, volume_db: Optional[float] = None,
                 pan: Optional[float] = None, mute: Optional[bool] = None, solo: Optional[bool] = None) -> None:
        line = self.line(line_id)
        if name is not None:
            line.name = name
        if volume_db is not None:
            line.volume_db = max(VOLUME_RANGE[0], min(VOLUME_RANGE[1], float(volume_db)))
        if pan is not None:
            line.pan = max(-1.0, min(1.0, float(pan)))
        if mute is not None:
            line.mute = bool(mute)
        if solo is not None:
            line.solo = bool(solo)
        self.touch()

    # ---- clips
    def add_clip(self, line_id: int, seq: PieceSeq, start: int, name: str = "", path: Optional[str] = None, *,
                 gain_db: float = 0.0) -> MixClip:
        if seq.sr != self.sr or seq.channels != CHANNELS:
            raise ValueError("clip format does not match the mix")
        if seq.frames <= 0:
            raise ValueError("The file contains no audio.")
        self.line(line_id)                                    # must exist
        clip = MixClip(self._next_id(), line_id, seq, max(0, int(start)), 0, seq.frames, gain_db, 0, 0, name,
                       str(path) if path else None)
        self.clips.append(clip)
        self.touch()
        return clip

    def move_clip(self, clip_id: int, start: int, line_id: Optional[int] = None) -> None:
        c = self.clip(clip_id)
        c.start = max(0, int(start))
        if line_id is not None and line_id != c.line:
            self.line(line_id)
            c.line = line_id
        self.touch()

    def trim_start(self, clip_id: int, new_start: int) -> None:
        """Drag the left edge: later start, or earlier as far as the audio goes."""
        c = self.clip(clip_id)
        new_start = int(min(max(new_start, c.start - c.offset), c.end - MIN_CLIP))
        delta = new_start - c.start
        c.start, c.offset, c.length = new_start, c.offset + delta, c.length - delta
        c.fade_in = min(c.fade_in, c.length)
        c.fade_out = min(c.fade_out, c.length)
        self.touch()

    def trim_end(self, clip_id: int, new_end: int) -> None:
        c = self.clip(clip_id)
        room = c.seq.frames - c.offset
        new_end = int(min(max(new_end, c.start + MIN_CLIP), c.start + room))
        c.length = new_end - c.start
        c.fade_in = min(c.fade_in, c.length)
        c.fade_out = min(c.fade_out, c.length)
        self.touch()

    def split_clip(self, clip_id: int, at: int) -> Optional[tuple[MixClip, MixClip]]:
        c = self.clip(clip_id)
        if not (c.start + MIN_CLIP <= at <= c.end - MIN_CLIP):
            return None
        cut = at - c.start
        right = replace(c, id=self._next_id(), start=at, offset=c.offset + cut, length=c.length - cut,
                        fade_in=0, fade_out=c.fade_out)
        c.length = cut
        c.fade_out = 0
        c.fade_in = min(c.fade_in, c.length)
        self.clips.append(right)
        self.touch()
        return c, right

    def duplicate_clip(self, clip_id: int, *, to_line: Optional[int] = None, gap: int = 0) -> MixClip:
        c = self.clip(clip_id)
        copy_ = replace(c, id=self._next_id(), start=c.end + gap, line=to_line if to_line is not None else c.line)
        self.clips.append(copy_)
        self.touch()
        return copy_

    def delete_clip(self, clip_id: int) -> None:
        self.clips = [c for c in self.clips if c.id != clip_id]
        self.touch()

    def set_clip(self, clip_id: int, *, gain_db: Optional[float] = None, fade_in: Optional[int] = None,
                 fade_out: Optional[int] = None, name: Optional[str] = None) -> None:
        c = self.clip(clip_id)
        if gain_db is not None:
            c.gain_db = max(CLIP_GAIN_RANGE[0], min(CLIP_GAIN_RANGE[1], float(gain_db)))
        if fade_in is not None:
            c.fade_in = max(0, min(int(fade_in), c.length))
        if fade_out is not None:
            c.fade_out = max(0, min(int(fade_out), c.length))
        if name is not None:
            c.name = name
        self.touch()

    def overlapping(self, clip_id: int) -> list[MixClip]:
        """Other clips that overlap this one in time, earliest first (any line)."""
        c = self.clip(clip_id)
        return sorted((o for o in self.clips if o.id != c.id and o.start < c.end and o.end > c.start),
                      key=lambda o: (o.start, o.id))

    def crossfade(self, clip_id: int) -> bool:
        """Make `clip_id` fade in over the end of the clip it overlaps, and that clip fade out under it."""
        c = self.clip(clip_id)
        earlier = [o for o in self.overlapping(clip_id) if o.start <= c.start and o.end < c.end + 1 and o.end > c.start]
        if not earlier:
            earlier = [o for o in self.overlapping(clip_id) if o.start < c.start < o.end]
        if not earlier:
            return False
        other = max(earlier, key=lambda o: o.end)
        span = max(1, min(other.end, c.end) - c.start)
        c.fade_in = min(span, c.length)
        other.fade_out = min(span, other.length)
        self.touch()
        return True

    # ---- effect spans
    def add_effect(self, effect_id: str, params: Optional[dict], start: int, end: int) -> EffectSpan:
        effect = fx.get(effect_id)
        cleaned = effect.clean(params)
        if not effect.preserves_length(cleaned):
            raise ValueError(f"{effect.name} changes the length of the sound, so it cannot be used on a mix.")
        start = max(0, int(start))
        span = EffectSpan(self._next_id(), effect_id, cleaned, start, max(start + MIN_CLIP, int(end)))
        self.effects.append(span)
        self.touch()
        return span

    def move_effect(self, span_id: int, start: int) -> None:
        e = self.effect_span(span_id)
        length = e.end - e.start
        e.start = max(0, int(start))
        e.end = e.start + length
        self.touch()

    def resize_effect(self, span_id: int, *, start: Optional[int] = None, end: Optional[int] = None) -> None:
        e = self.effect_span(span_id)
        if start is not None:
            e.start = int(min(max(0, start), e.end - MIN_CLIP))
        if end is not None:
            e.end = int(max(e.start + MIN_CLIP, end))
        self.touch()

    def set_effect(self, span_id: int, *, params: Optional[dict] = None, mute: Optional[bool] = None) -> None:
        e = self.effect_span(span_id)
        if params is not None:
            e.params = fx.get(e.effect).clean(params)
        if mute is not None:
            e.mute = bool(mute)
        self.touch()

    def delete_effect(self, span_id: int) -> None:
        self.effects = [e for e in self.effects if e.id != span_id]
        self.touch()

    # ---- what is audible
    def audible_lines(self) -> set[int]:
        soloed = {l.id for l in self.lines if l.solo and not l.mute}
        if any(l.solo for l in self.lines):
            return soloed
        return {l.id for l in self.lines if not l.mute}

    # ---- rendering
    def _track_jobs(self, a: int, b: int) -> list[tuple[MixClip, np.ndarray]]:
        """(clip, [left gain, right gain]) for every audible clip that touches frames [a, b)."""
        audible = self.audible_lines()
        lines = {l.id: l for l in self.lines}
        jobs = []
        for c in self.clips:
            if c.line in audible and c.end > a and c.start < b:
                l = lines[c.line]
                lg, rg = balance_gains(l.pan)
                level = db_to_amp(l.volume_db) * db_to_amp(c.gain_db)
                jobs.append((c, np.array([lg * level, rg * level], np.float32)))
        return jobs

    def render_tracks(self, a: int = 0, b: Optional[int] = None, *, master: float = 1.0,
                      progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None) -> Clip:
        """The lines mixed together (no effect spans), frames [a, b) of the mix. `master` scales the sum."""
        b = self.total_frames if b is None else b
        jobs = self._track_jobs(a, b)
        writer = SourceWriter(self.sr, CHANNELS, label="mix")
        try:
            for blk in range(a, b, BLOCK):
                if cancel is not None:
                    cancel.check()
                end = min(b, blk + BLOCK)
                writer.write(_mix_range(jobs, blk, end, master))
                if progress:
                    progress((end - a) / max(1, b - a))
        except BaseException:
            writer.abort()
            raise
        return Clip.from_source(writer.finish())

    def live(self, cache: Optional[dict] = None) -> "LiveMix":
        """A frozen, instantly playable copy of the mix as it is now (see LiveMix)."""
        return LiveMix(self, cache)

    def render(self, *, effects: bool = True, master: float = 1.0, progress: Optional[ProgressFn] = None,
               cancel: Optional[CancelToken] = None) -> Clip:
        """The whole mix: every audible line, then the effect spans applied where they sit."""
        spans = [e for e in self.effects if effects and not e.mute] if effects else []
        track_share = 0.7 if spans else 1.0
        current = self.render_tracks(master=master, progress=scaled(progress, 0.0, track_share), cancel=cancel)
        total = current.frames
        for i, span in enumerate(sorted(spans, key=lambda e: (e.start, e.id))):
            a, b = max(0, span.start), min(total, span.end)
            if b <= a:
                continue
            effect = fx.get(span.effect)
            lo = track_share + (1 - track_share) * i / len(spans)
            hi = track_share + (1 - track_share) * (i + 1) / len(spans)
            piece = fx.run_effect(effect, span.params, current, a, b, progress=scaled(progress, lo, hi), cancel=cancel)
            pieces = slice_pieces(current.pieces, 0, a) + list(piece.pieces) + slice_pieces(current.pieces, b, total)
            current = Clip(pieces, self.sr, CHANNELS)
        if progress:
            progress(1.0)
        return current

    def peak_estimate(self, *, cancel: Optional[CancelToken] = None) -> float:
        """Loudest sample of the track mix (before effects), to warn about clipping."""
        peak = 0.0
        for chunk in self.render_tracks(cancel=cancel).iter_chunks():
            if len(chunk):
                peak = max(peak, float(np.abs(chunk).max()))
        return peak

    # ---- saving and loading
    def to_dict(self) -> dict:
        missing = [c.name or f"clip {c.id}" for c in self.clips if not c.path]
        if missing:
            raise ValueError(f"These clips come from audio that is not saved to a file yet: {', '.join(missing[:4])}. "
                             "Save those files first.")
        return {
            "format": "tonewright-mix", "version": PROJECT_VERSION, "sample_rate": self.sr, "name": self.name,
            "lines": [{"id": l.id, "name": l.name, "volume_db": l.volume_db, "pan": l.pan, "mute": l.mute, "solo": l.solo}
                      for l in self.lines],
            "clips": [{"id": c.id, "line": c.line, "path": c.path, "name": c.name, "start": c.start, "offset": c.offset,
                       "length": c.length, "gain_db": c.gain_db, "fade_in": c.fade_in, "fade_out": c.fade_out}
                      for c in self.clips],
            "effects": [{"id": e.id, "effect": e.effect, "params": e.params, "start": e.start, "end": e.end, "mute": e.mute}
                        for e in self.effects],
        }

    def save(self, path: Path) -> None:
        text = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
        path = Path(path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def from_dict(cls, data: dict, load_audio: Callable[[str, int], PieceSeq], *,
                  trust_custom_filters: bool = False) -> "MixProject":
        """`load_audio(path, sample_rate)` returns the file's audio as a stereo PieceSeq at that rate.

        A project file is data from outside the program. The custom FFmpeg filter effect runs free text as an FFmpeg
        filter graph, which can open files and network addresses, so it is only loaded when `trust_custom_filters`."""
        if not isinstance(data, dict) or data.get("format") != "tonewright-mix":
            raise ValueError("This is not a Tonewright mix project.")
        if int(data.get("version", 0)) > PROJECT_VERSION:
            raise ValueError("This mix project was made by a newer version of Tonewright.")
        project = cls(int(data.get("sample_rate") or DEFAULT_SR))
        project.name = str(data.get("name", ""))
        for l in data.get("lines", []):
            project.lines.append(Line(int(l["id"]), str(l.get("name", "")), float(l.get("volume_db", 0.0)),
                                      float(l.get("pan", 0.0)), bool(l.get("mute", False)), bool(l.get("solo", False))))
        cache: dict[str, PieceSeq] = {}
        known_lines = {l.id for l in project.lines}
        for c in data.get("clips", []):
            if int(c["line"]) not in known_lines:
                continue
            path = str(c["path"])
            if path not in cache:
                cache[path] = load_audio(path, project.sr)
            seq = cache[path]
            offset = max(0, min(int(c.get("offset", 0)), seq.frames - 1))
            length = max(1, min(int(c.get("length", seq.frames)), seq.frames - offset))
            project.clips.append(MixClip(int(c["id"]), int(c["line"]), seq, max(0, int(c["start"])), offset, length,
                                         float(c.get("gain_db", 0.0)), int(c.get("fade_in", 0)), int(c.get("fade_out", 0)),
                                         str(c.get("name", "")), path))
        for e in data.get("effects", []):
            try:
                effect = fx.get(str(e["effect"]))
            except KeyError:
                continue
            if effect.id in CUSTOM_FILTER_EFFECTS and not trust_custom_filters:
                project.skipped_effects.append(effect.id)
                continue
            project.effects.append(EffectSpan(int(e["id"]), effect.id, effect.clean(e.get("params")), int(e["start"]),
                                              int(e["end"]), bool(e.get("mute", False))))
        project._ids = max([0, *(l.id for l in project.lines), *(c.id for c in project.clips),
                            *(e.id for e in project.effects)])
        project.touch()
        return project


# ------------------------------------------------------------------ playing the mix while it is being edited
class _LiveSpan:
    def __init__(self, index: int, effect: "fx.Effect", params: dict, a: int, b: int, key: tuple) -> None:
        self.index, self.effect, self.params, self.a, self.b, self.key = index, effect, params, a, b, key


class _SpanInput:
    """What an effect span is fed: the lines mixed, with the spans before it already applied (as `render` does)."""
    channels = CHANNELS

    def __init__(self, live: "LiveMix", index: int) -> None:
        self._live, self._index = live, index
        self.sr, self.frames = live.sr, live.frames

    def read(self, a: int, b: int) -> np.ndarray:
        return self._live._compose(a, b, self._index)


EFFECT_CACHE = 6                     # rendered effect spans kept for the next LiveMix (a drag makes dozens of them)


class LiveMix:
    """What the Multitrack page plays: the lines mixed block by block as the sound card asks for them, with the effect
    spans applied where they sit. It is built from a frozen copy of the project, so the project can keep changing while
    this plays (the player is then handed a fresh LiveMix, see `Player.swap`).

    Mixing the lines costs next to nothing, so a moved clip, a fade or a level is heard at once and starting to play
    takes no time. An effect needs its stretch of the mix rendered first; that happens on a background thread, spans
    in order, and until a span is ready its stretch plays without the effect. Finished spans go into `cache` (give
    every LiveMix the same dict) so a change that does not touch a span does not render it again."""

    channels = CHANNELS

    def __init__(self, project: MixProject, cache: Optional[dict] = None) -> None:
        self.sr = project.sr
        self.frames = project.total_frames
        self._jobs = [(replace(c), gains) for c, gains in project._track_jobs(0, self.frames)]
        self._cache = cache if cache is not None else {}
        self._results: dict[int, Clip] = {}
        self._lock = threading.Lock()
        self._cancel = CancelToken()
        self._thread: Optional[threading.Thread] = None
        self.error: Optional[BaseException] = None
        self.error_reported = False                      # for whoever shows the error: only once
        self._spans: list[_LiveSpan] = []
        for e in sorted((e for e in project.effects if not e.mute), key=lambda e: (e.start, e.id)):
            a, b = max(0, e.start), min(self.frames, e.end)
            if b <= a:
                continue
            clips = tuple((c.seq, c.start, c.offset, c.length, tuple(g.tolist()), c.fade_in, c.fade_out)
                          for c, g in self._jobs if c.end > a and c.start < b)
            earlier = tuple(s.key for s in self._spans if s.b > a and s.a < b)
            key = (e.effect, json.dumps(e.params, sort_keys=True, default=str), a, b, clips, earlier)
            self._spans.append(_LiveSpan(len(self._spans), fx.get(e.effect), copy.deepcopy(e.params), a, b, key))
            hit = self._cache.get(key)
            if hit is not None:
                self._results[len(self._spans) - 1] = hit

    # ---- what the player calls
    def read(self, a: int, b: int) -> np.ndarray:
        """Frames [a, b) of the mix: (b - a, 2) float32. Never waits for an effect."""
        a, b = max(0, a), min(self.frames, b)
        if b <= a:
            return np.zeros((0, CHANNELS), np.float32)
        return self._compose(a, b, len(self._spans))

    def _compose(self, a: int, b: int, upto: int) -> np.ndarray:
        out = _mix_range(self._jobs, a, b)
        with self._lock:
            ready = dict(self._results)
        for span in self._spans[:upto]:
            lo, hi = max(a, span.a), min(b, span.b)
            result = ready.get(span.index)
            if hi > lo and result is not None:
                out[lo - a: hi - a] = result.read(lo - span.a, hi - span.a)
        return out

    # ---- effects
    @property
    def pending(self) -> bool:
        """True while some effect span is not rendered yet (its stretch is playing without the effect)."""
        with self._lock:
            return len(self._results) < len(self._spans) and self.error is None

    def pending_at(self, frame: int, ahead: int = 0) -> bool:
        """True if an effect span covering [frame, frame + ahead] (or one it depends on) is not rendered yet."""
        with self._lock:
            return self.error is None and any(s.index not in self._results and s.b > frame and s.a <= frame + ahead
                                              for s in self._spans)

    def start_effects(self, delay: float = 0.15) -> None:
        """Render the effect spans in the background. `delay` lets a burst of edits pass before any work is started."""
        if self._thread is not None or not self.pending:
            return
        self._thread = threading.Thread(target=self._render_spans, args=(delay,), daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Abandon unfinished work (the project changed again, or playback stopped)."""
        self._cancel.cancel()

    def _render_spans(self, delay: float) -> None:
        if self._cancel.wait(delay):
            return
        try:
            for span in self._spans:
                self._cancel.check()
                with self._lock:
                    if span.index in self._results:
                        continue
                clip = fx.run_effect(span.effect, span.params, _SpanInput(self, span.index), span.a, span.b,
                                     cancel=self._cancel)
                with self._lock:
                    self._results[span.index] = clip
                self._cache[span.key] = clip
                while len(self._cache) > EFFECT_CACHE:
                    self._cache.pop(next(iter(self._cache)), None)
        except Cancelled:
            return
        except Exception as exc:  # noqa: BLE001 - reported by whoever is playing
            self.error = exc