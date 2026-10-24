"""Effect framework: parameter specs, the run context, and the runner.

An effect reads audio through a Ctx a chunk at a time and writes its result
back through it, so the same code handles a two-second selection and a
three-hour file without holding either in memory.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Optional, Sequence

import numpy as np

from .. import ffmpeg as ff
from ..common import CancelToken, ProgressFn, scaled
from ..document import Clip, Piece, Readable
from ..source import Source, SourceWriter

CHUNK = 1 << 18


@dataclass(frozen=True)
class Param:
    """One user-adjustable value. The UI builds its controls from these."""
    key: str
    label: str
    kind: str = "float"            # float | int | choice | bool | text | points
    default: Any = 0
    minimum: float = 0.0
    maximum: float = 100.0
    step: float = 1.0
    decimals: int = 1
    unit: str = ""
    choices: tuple = ()            # ((value, label), ...)
    help: str = ""
    log: bool = False              # slider travels logarithmically (frequencies)
    show_if: Optional[tuple] = None  # (other_key, value | tuple_of_values)
    group: str = ""


class Effect:
    id = ""
    name = ""
    category = ""
    description = ""
    keywords = ""
    params: tuple[Param, ...] = ()
    presets: dict[str, dict] = {}
    editor: Optional[str] = None   # name of a custom UI control (curve, EQ sliders...)
    length_preserving = True
    min_channels = 1               # 2 for stereo-only effects

    def defaults(self) -> dict:
        return {p.key: p.default for p in self.params}

    def clean(self, values: Optional[Mapping[str, Any]]) -> dict:
        """Fill in defaults and clamp every value into its declared range."""
        values = dict(values or {})
        out: dict = {}
        for p in self.params:
            v = values.get(p.key, p.default)
            try:
                if p.kind == "float":
                    v = min(p.maximum, max(p.minimum, float(v)))
                elif p.kind == "int":
                    v = int(min(p.maximum, max(p.minimum, round(float(v)))))
                elif p.kind == "bool":
                    v = bool(v)
                elif p.kind == "choice":
                    if v not in [c[0] for c in p.choices]:
                        v = p.default
                elif p.kind == "text":
                    v = str(v)
                elif p.kind == "points":
                    v = _clean_points(v, p)
            except (TypeError, ValueError):
                v = p.default
            out[p.key] = v
        return out

    def preserves_length(self, params: Mapping[str, Any]) -> bool:
        return self.length_preserving

    def out_frames(self, frames: int, sr: int, params: Mapping[str, Any]) -> int:
        return frames

    def process(self, ctx: "Ctx") -> None:
        raise NotImplementedError


def _clean_points(value, p: Param) -> list:
    pts = []
    for item in value:
        x, y = float(item[0]), float(item[1])
        pts.append([min(1.0, max(0.0, x)), min(p.maximum, max(p.minimum, y))])
    pts.sort(key=lambda t: t[0])
    return pts if len(pts) >= 2 else [list(t) for t in p.default]


class Ctx:
    """What an effect sees while it runs.

    `frames` is how much will actually be processed; `span` is the length of the
    whole selection. They differ only while previewing the start of a long
    selection, so position-dependent effects (fades, envelopes) still preview
    the way they will finally sound.
    """

    def __init__(self, effect: Effect, params: dict, sr: int, channels: int, frames: int,
                 span: int, reader, full_reader, writer: SourceWriter,
                 progress: Optional[ProgressFn], cancel: Optional[CancelToken],
                 extra: Optional[dict]) -> None:
        self.effect = effect
        self.params = params
        self.sr = sr
        self.channels = channels
        self.frames = frames
        self.span = span
        self.offset = 0
        self._reader = reader
        self._full_reader = full_reader
        self.writer = writer
        self._progress = progress
        self.cancel = cancel
        self.extra = extra or {}

    # ---- housekeeping
    def check(self) -> None:
        if self.cancel is not None:
            self.cancel.check()

    def report(self, frac: float) -> None:
        if self._progress is not None:
            self._progress(min(1.0, max(0.0, frac)))

    # ---- reading
    def read(self, a: int, b: int) -> np.ndarray:
        return self._reader(max(0, a), min(self.frames, b))

    def read_all(self) -> np.ndarray:
        return self._reader(0, self.frames)

    def read_full(self, a: int, b: int) -> np.ndarray:
        """Frames of the whole selection, even while previewing only its start."""
        return self._full_reader(max(0, a), min(self.span, b))

    def chunks(self, lo: float = 0.0, hi: float = 1.0, size: int = CHUNK) -> Iterator[np.ndarray]:
        for start in range(0, self.frames, size):
            self.check()
            end = min(self.frames, start + size)
            yield self._reader(start, end)
            self.report(lo + (hi - lo) * end / max(1, self.frames))

    def chunks_reversed(self, lo: float = 0.0, hi: float = 1.0, size: int = CHUNK) -> Iterator[np.ndarray]:
        starts = list(range(0, self.frames, size))
        for i, start in enumerate(reversed(starts)):
            self.check()
            yield self._reader(start, min(self.frames, start + size))
            self.report(lo + (hi - lo) * (i + 1) / max(1, len(starts)))

    def full_chunks(self, lo: float = 0.0, hi: float = 1.0, size: int = CHUNK) -> Iterator[np.ndarray]:
        """The whole selection, even while previewing only its start."""
        for start in range(0, self.span, size):
            self.check()
            end = min(self.span, start + size)
            yield self._full_reader(start, end)
            self.report(lo + (hi - lo) * end / max(1, self.span))

    def positions(self, start: int, n: int) -> np.ndarray:
        """0..1 position of frames [start, start+n) within the whole selection."""
        return (self.offset + start + np.arange(n, dtype=np.float64)) / max(1, self.span - 1)

    def stats(self, lo: float = 0.0, hi: float = 0.5) -> "Stats":
        st = Stats(self.channels)
        for chunk in self.full_chunks(lo, hi):
            st.add(chunk)
        return st

    # ---- writing
    def write(self, data: np.ndarray) -> None:
        self.writer.write(data)

    def ffmpeg(self, chain: str, *, lo: float = 0.0, hi: float = 1.0) -> None:
        """Stream the whole input through an ffmpeg audio filter chain into the output."""
        ff.filter_stream(self.chunks(lo, hi), self.sr, self.channels, chain, self.writer,
                         cancel=self.cancel)


class Stats:
    """Per-channel min/max/sum/sum-of-squares, gathered in one pass."""

    def __init__(self, channels: int) -> None:
        self.n = 0
        self.min = np.full(channels, np.inf)
        self.max = np.full(channels, -np.inf)
        self.sum = np.zeros(channels)
        self.sumsq = np.zeros(channels)

    def add(self, chunk: np.ndarray) -> None:
        if not len(chunk):
            return
        self.n += len(chunk)
        t = np.ascontiguousarray(chunk.T)          # channel-major: reductions over a long contiguous axis are ~10x faster
        self.min = np.minimum(self.min, t.min(axis=1))
        self.max = np.maximum(self.max, t.max(axis=1))
        self.sum += t.sum(axis=1, dtype=np.float64)
        self.sumsq += np.einsum("ij,ij->i", t, t, dtype=np.float64)

    @property
    def mean(self) -> np.ndarray:
        return self.sum / max(1, self.n)

    @property
    def rms(self) -> np.ndarray:
        return np.sqrt(self.sumsq / max(1, self.n))

    @property
    def peak(self) -> np.ndarray:
        if not self.n:
            return np.zeros_like(self.max)
        return np.maximum(np.abs(self.min), np.abs(self.max))


# --------------------------------------------------------------------- registry
_REGISTRY: dict[str, Effect] = {}
CATEGORY_ORDER = ["Volume", "Fades & Envelope", "Time & Pitch", "Modulation", "Space",
                  "Equalizer & Filters", "Dynamics", "Restoration", "Channels", "Advanced"]


def register(cls):
    inst = cls()
    if not inst.id:
        raise ValueError(f"{cls.__name__} has no id")
    _REGISTRY[inst.id] = inst
    return cls


def get(effect_id: str) -> Effect:
    return _REGISTRY[effect_id]


def all_effects() -> list[Effect]:
    return list(_REGISTRY.values())


def by_category() -> list[tuple[str, list[Effect]]]:
    groups: dict[str, list[Effect]] = {}
    for fx in _REGISTRY.values():
        groups.setdefault(fx.category, []).append(fx)
    ordered = [c for c in CATEGORY_ORDER if c in groups] + sorted(set(groups) - set(CATEGORY_ORDER))
    return [(c, groups[c]) for c in ordered]


# --------------------------------------------------------------------- runner
def _run_core(effect: Effect, params: dict, sr: int, channels: int, frames: int, span: int,
              reader, full_reader, progress, cancel, extra) -> Source:
    writer = SourceWriter(sr, channels, label=effect.name)
    ctx = Ctx(effect, params, sr, channels, frames, span, reader, full_reader, writer,
              progress, cancel, extra)
    try:
        effect.process(ctx)
    except BaseException:
        writer.abort()
        raise
    return writer.finish()


def _fit(src: Source, frames: int, preserve: bool) -> Clip:
    """Make a length-preserving effect's output exactly `frames` long."""
    if not preserve or src.frames == frames:
        return Clip.from_source(src)
    if src.frames > frames:
        return Clip([Piece(src, 0, frames)], src.sr, src.channels)
    pad = Source.silence(frames - src.frames, src.sr, src.channels)
    return Clip([Piece(src, 0, src.frames), Piece(pad, 0, pad.frames)], src.sr, src.channels)


def run_effect(effect: Effect, params: Optional[Mapping[str, Any]], seq: Readable, a: int, b: int, *,
               channels: Optional[Sequence[int]] = None, window: Optional[int] = None,
               progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None,
               extra: Optional[dict] = None) -> Clip:
    """Apply an effect to frames [a, b) of `seq` and return the replacement audio.

    `channels` limits the effect to some channels (length-preserving effects only);
    `window` processes just the first N frames, for previews.
    """
    params = effect.clean(params)
    span = max(0, b - a)
    frames = span if window is None else min(span, window)
    sr, nch = seq.sr, seq.channels
    preserve = effect.preserves_length(params)

    def full_reader(x: int, y: int) -> np.ndarray:
        return seq.read(a + x, a + y)

    idx = sorted(set(channels)) if channels is not None else list(range(nch))
    if idx == list(range(nch)) or not preserve:
        src = _run_core(effect, params, sr, nch, frames, span, full_reader, full_reader,
                        progress, cancel, extra)
        return _fit(src, frames, preserve)

    def sub_reader(x: int, y: int) -> np.ndarray:
        return full_reader(x, y)[:, idx]

    sub_extra = dict(extra or {})
    if "noise" in sub_extra:
        sub_extra["noise"] = np.asarray(sub_extra["noise"])[:, idx]
    src = _run_core(effect, params, sr, len(idx), frames, span, sub_reader, sub_reader,
                    scaled(progress, 0.0, 0.9), cancel, sub_extra)
    processed = _fit(src, frames, True)
    writer = SourceWriter(sr, nch, label=effect.name)
    try:
        for start in range(0, frames, CHUNK):
            if cancel is not None:
                cancel.check()
            end = min(frames, start + CHUNK)
            merged = full_reader(start, end).copy()
            merged[:, idx] = processed.read(start, end)
            writer.write(merged)
            if progress:
                progress(0.9 + 0.1 * end / max(1, frames))
    except BaseException:
        writer.abort()
        raise
    return Clip.from_source(writer.finish())


def render_preview(effect: Effect, params: Optional[Mapping[str, Any]], seq: Readable, a: int, b: int, *,
                   seconds: float = 8.0, bypass: bool = False, channels: Optional[Sequence[int]] = None,
                   progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None,
                   extra: Optional[dict] = None) -> np.ndarray:
    """The first few seconds of the selection with the effect applied (or untouched)."""
    window = max(1, int(seconds * seq.sr))
    if bypass:
        return seq.read(a, min(b, a + window))
    clip = run_effect(effect, params, seq, a, b, channels=channels, window=window,
                      progress=progress, cancel=cancel, extra=extra)
    return clip.read(0, clip.frames)
