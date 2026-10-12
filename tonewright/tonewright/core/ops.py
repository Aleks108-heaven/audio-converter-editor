"""Operations that produce new audio for the document: format conversion and mixing."""
from __future__ import annotations

from typing import Optional

import numpy as np

from . import ffmpeg as ff
from .common import CancelToken, ProgressFn
from .document import PieceSeq
from .source import Source, SourceWriter
from .timefmt import db_to_amp

CHUNK = 1 << 18


def convert_format(seq: PieceSeq, sr: int, channels: int, *, a: int = 0, b: Optional[int] = None,
                   progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None) -> Source:
    """Resample and/or remix audio to (sr, channels). Returns it as one new Source."""
    b = seq.frames if b is None else b
    writer = SourceWriter(sr, channels, label="converted")
    if seq.sr == sr and seq.channels == channels:
        try:
            for start in range(a, b, CHUNK):
                if cancel is not None:
                    cancel.check()
                writer.write(seq.read(start, min(b, start + CHUNK)))
                if progress:
                    progress((min(b, start + CHUNK) - a) / max(1, b - a))
        except BaseException:
            writer.abort()
            raise
        return writer.finish()
    chain = [f"aresample={sr}"] if seq.sr != sr else []
    if seq.channels != channels:
        chain.append("pan=mono|c0=0.5*c0+0.5*c1" if channels == 1 else "pan=stereo|c0=c0|c1=c0")
    try:
        ff.filter_stream(seq.iter_chunks(a, b), seq.sr, seq.channels, ",".join(chain) or "anull", writer,
                         out_sr=sr, out_channels=channels, total_frames=b - a, progress=progress, cancel=cancel)
    except BaseException:
        writer.abort()
        raise
    return writer.finish()


def mix_clip(seq: PieceSeq, clip: PieceSeq, pos: int, *, clip_gain_db: float = 0.0, base_gain_db: float = 0.0,
             progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None) -> Source:
    """Overlay `clip` onto `seq` starting at `pos`. The result covers [pos, pos + clip.frames);
    where that runs past the end of `seq` the original is treated as silence."""
    if clip.sr != seq.sr or clip.channels != seq.channels:
        raise ValueError("clip format does not match the document")
    n = clip.frames
    cg, bg = np.float32(db_to_amp(clip_gain_db)), np.float32(db_to_amp(base_gain_db))
    writer = SourceWriter(seq.sr, seq.channels, label="mix")
    try:
        for start in range(0, n, CHUNK):
            if cancel is not None:
                cancel.check()
            end = min(n, start + CHUNK)
            top = clip.read(start, end) * cg
            base = np.zeros_like(top)
            have = seq.read(pos + start, pos + end)
            base[: len(have)] = have
            writer.write(base * bg + top)
            if progress:
                progress(end / max(1, n))
    except BaseException:
        writer.abort()
        raise
    return writer.finish()


