"""Batch conversion of files, or of edited documents, to any output format."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

import numpy as np

from . import access
from . import ffmpeg as ff
from . import formats, tags as tagmod
from .common import Cancelled, CancelToken, PauseToken, ProgressFn
from .document import PieceSeq
from .effects.base import Stats
from .source import SourceWriter


@dataclass
class ConvertItem:
    path: Optional[Path]
    name: str
    duration: float
    sr: int = 44100
    channels: int = 2
    stream_index: Optional[int] = None
    tags: tagmod.TagSet = field(default_factory=tagmod.TagSet)
    doc: Optional[PieceSeq] = None          # edited audio is encoded instead of the file
    frame_range: Optional[tuple[int, int]] = None
    key: object = None

    @property
    def source_sr(self) -> int:
        return self.doc.sr if self.doc is not None else self.sr

    @property
    def source_channels(self) -> int:
        return self.doc.channels if self.doc is not None else self.channels

    @property
    def length(self) -> float:
        if self.doc is not None:
            a, b = self.frame_range or (0, self.doc.frames)
            return (b - a) / self.doc.sr
        return self.duration


@dataclass
class ConvertOptions:
    settings: formats.EncodeSettings = field(default_factory=formats.EncodeSettings)
    out_dir: Optional[Path] = None           # None puts each file next to its source
    pattern: str = "{name}"
    conflict: str = "rename"                 # rename | overwrite | skip
    normalize: str = "off"                   # off | peak | lufs
    peak_target_db: float = -1.0
    lufs_target: float = -16.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    keep_tags: bool = True
    merge: bool = False
    merge_name: str = "Merged"
    gap_seconds: float = 0.0


@dataclass
class ItemResult:
    item: ConvertItem
    status: str                              # done | skipped | error | cancelled
    output: Optional[Path] = None
    message: str = ""
    seconds: float = 0.0
    problem: Optional[access.WriteProblem] = None   # why the file could not be written, when that was the reason
    warning: str = ""                                # the file was written, but something short of that failed (its tags)


def probe_item(path: str | Path, *, read_tags: bool = True) -> ConvertItem:
    info = ff.probe(path)
    st = info.first_audio
    path = Path(path)
    return ConvertItem(path=path, name=path.stem, duration=st.duration or info.duration, sr=st.sample_rate,
                       channels=st.channels, stream_index=st.index,
                       tags=tagmod.read_tags(path) if read_tags else tagmod.TagSet())


# ------------------------------------------------------------------ output naming
def plan_output(item: ConvertItem, opts: ConvertOptions, taken: set[Path],
                name_override: Optional[str] = None) -> Optional[Path]:
    """Pick the output path, honouring the conflict policy. None means 'skip this one'."""
    spec = opts.settings.spec
    folder = opts.out_dir or (item.path.parent if item.path else Path.cwd())
    base = item.path.stem if item.path else item.name
    stem = name_override or tagmod.name_from_pattern(opts.pattern, item.tags, tagmod.sanitize_filename(base))
    out = folder / f"{stem}{spec.ext}"
    same_as_source = item.path is not None and _same_file(out, item.path)
    clash = out.exists() or out in taken
    if same_as_source and opts.conflict == "skip":
        return None
    # the original file is never replaced, even when asked to overwrite
    if same_as_source or (clash and opts.conflict == "rename") or (out in taken):
        suffix = " (converted)" if same_as_source else ""
        candidate = folder / f"{stem}{suffix}{spec.ext}"
        n = 2
        while candidate.exists() or candidate in taken:
            candidate = folder / f"{stem}{suffix} ({n}){spec.ext}"
            n += 1
        return candidate
    if clash and opts.conflict == "skip":
        return None
    return out


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve() or (a.exists() and b.exists() and a.samefile(b))
    except OSError:
        return False


# ------------------------------------------------------------------ analysis passes
_MAX_VOL = re.compile(r"max_volume:\s*(-?[\d.]+|-inf)\s*dB")
_LN_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


def _peak_db(item: ConvertItem, cancel: Optional[CancelToken]) -> Optional[float]:
    if item.doc is not None:
        a, b = item.frame_range or (0, item.doc.frames)
        st = Stats(item.doc.channels)
        for chunk in item.doc.iter_chunks(a, b):
            if cancel:
                cancel.check()
            st.add(chunk)
        peak = float(st.peak.max()) if st.n else 0.0
        return 20 * np.log10(peak) if peak > 0 else None
    idx = item.stream_index if item.stream_index is not None else 0
    text = ff.run_capture(["-i", str(item.path), "-map", f"0:{idx}" if item.stream_index is not None else "0:a:0",
                           "-vn", "-af", "volumedetect", "-f", "null", "-"], cancel=cancel)
    m = _MAX_VOL.search(text)
    if not m or m.group(1) == "-inf":
        return None
    return float(m.group(1))


def _loudnorm_chain(item: ConvertItem, target: float, cancel: Optional[CancelToken]) -> str:
    base = f"loudnorm=I={target:g}:TP=-1.5:LRA=11"
    if item.doc is not None:
        a, b = item.frame_range or (0, item.doc.frames)
        report = ff.analyze_stream(item.doc.iter_chunks(a, b), item.doc.sr, item.doc.channels,
                                   base + ":print_format=json", cancel=cancel)
    else:
        report = ff.run_capture(["-i", str(item.path), "-map",
                                 f"0:{item.stream_index}" if item.stream_index is not None else "0:a:0",
                                 "-vn", "-af", base + ":print_format=json", "-f", "null", "-"], cancel=cancel)
    blocks = _LN_JSON.findall(report)
    if blocks:
        try:
            d = json.loads(blocks[-1])
            if all(d[k] not in ("-inf", "inf") for k in ("input_i", "input_tp", "input_lra", "input_thresh")):
                return (f"{base}:measured_I={d['input_i']}:measured_TP={d['input_tp']}:"
                        f"measured_LRA={d['input_lra']}:measured_thresh={d['input_thresh']}:"
                        f"offset={d['target_offset']}:linear=true")
        except (ValueError, KeyError):
            pass
    return base


def build_filter(item: ConvertItem, opts: ConvertOptions, length: float,
                 cancel: Optional[CancelToken]) -> Optional[str]:
    parts: list[str] = []
    if opts.normalize == "peak":
        peak = _peak_db(item, cancel)
        if peak is not None:
            parts.append(f"volume={opts.peak_target_db - peak:.4f}dB")
    elif opts.normalize == "lufs":
        parts.append(_loudnorm_chain(item, opts.lufs_target, cancel))
    fade_in = min(max(opts.fade_in, 0.0), length / 2)
    fade_out = min(max(opts.fade_out, 0.0), length / 2)
    if fade_in > 0:
        parts.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0 and length > 0:
        parts.append(f"afade=t=out:st={max(0.0, length - fade_out):.3f}:d={fade_out:.3f}")
    return ",".join(parts) or None


# ------------------------------------------------------------------ tags
def _post_process_tags(out: Path, tags: tagmod.TagSet, opts: ConvertOptions) -> str:
    """Write the tags into the converted file. Tagging is best-effort, the audio itself converted fine, so a failure
    does not fail the conversion; it comes back as one sentence for the caller to show, and "" means all went well."""
    if not opts.keep_tags or tags.is_empty():
        return ""
    key = opts.settings.format
    if key not in formats.TAG_CAPABLE or key == "wav" or not tagmod.can_write(out):
        return ""
    try:
        tagmod.write_tags(out, tags)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        if isinstance(exc, OSError):
            return access.one_line(access.diagnose_modify(out, exc), out.name)
        return str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)
    return ""


def _ffmpeg_metadata(tags: tagmod.TagSet, opts: ConvertOptions) -> Optional[dict]:
    if opts.keep_tags and opts.settings.format == "wav":
        return tags.to_ffmpeg()
    return None


# ------------------------------------------------------------------ single item
def convert_item(item: ConvertItem, out: Path, opts: ConvertOptions, *,
                 progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None,
                 pause: Optional[PauseToken] = None, on_warning: Optional[Callable[[str], None]] = None) -> Path:
    """Convert one item to `out`. `on_warning` hears about anything that went wrong short of failing (tags not written)."""
    s = opts.settings
    args = formats.build_args(s, item.source_sr, item.source_channels)
    chain = build_filter(item, opts, item.length, cancel)
    meta = _ffmpeg_metadata(item.tags, opts)
    out.parent.mkdir(parents=True, exist_ok=True)
    if item.doc is not None:
        a, b = item.frame_range or (0, item.doc.frames)
        ff.encode_pcm(item.doc.iter_chunks(a, b), item.doc.sr, item.doc.channels, out, args,
                      total_frames=b - a, progress=progress, cancel=cancel, pause=pause,
                      audio_filter=chain, metadata=meta)
    else:
        ff.convert_file(item.path, out, args, stream_index=item.stream_index, audio_filter=chain,
                        duration=item.duration, metadata=meta, progress=progress, cancel=cancel, pause=pause)
    warning = _post_process_tags(out, item.tags, opts)
    if warning and on_warning:
        on_warning(warning)
    return out


# ------------------------------------------------------------------ merging
def item_pcm(item: ConvertItem, sr: int, channels: int, cancel: Optional[CancelToken]) -> Iterator[np.ndarray]:
    """The item's audio as float32 chunks at exactly (sr, channels)."""
    if item.doc is not None:
        a, b = item.frame_range or (0, item.doc.frames)
        if item.doc.sr == sr and item.doc.channels == channels:
            yield from item.doc.iter_chunks(a, b)
            return
        writer = SourceWriter(sr, channels)
        chain = f"aresample={sr}"
        if item.doc.channels != channels:
            chain += ",pan=mono|c0=0.5*c0+0.5*c1" if channels == 1 else ",pan=stereo|c0=c0|c1=c0"
        ff.filter_stream(item.doc.iter_chunks(a, b), item.doc.sr, item.doc.channels, chain, writer,
                         out_sr=sr, out_channels=channels, cancel=cancel)
        src = writer.finish()
        for start in range(0, src.frames, 1 << 18):
            yield src.read(start, start + (1 << 18))
        src.close()
        return
    yield from ff.convert_whole_file_to_pcm(item.path, sr, channels, stream_index=item.stream_index, cancel=cancel)


def convert_merged(items: list[ConvertItem], out: Path, opts: ConvertOptions, *,
                   progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None,
                   pause: Optional[PauseToken] = None, on_warning: Optional[Callable[[str], None]] = None) -> Path:
    """Join several items into one file."""
    first = items[0]
    s = opts.settings
    sr, channels = formats.resolve(s, first.source_sr, first.source_channels)
    args = formats.build_args(s, first.source_sr, first.source_channels)
    total_len = sum(i.length for i in items) + opts.gap_seconds * (len(items) - 1)
    gap = np.zeros((int(opts.gap_seconds * sr), channels), np.float32)

    def stream() -> Iterator[np.ndarray]:
        for n, item in enumerate(items):
            if n and len(gap):
                yield gap
            yield from item_pcm(item, sr, channels, cancel)

    merged_opts = ConvertOptions(**{**opts.__dict__, "normalize": "off"})
    chain = build_filter(first, merged_opts, total_len, cancel)
    tags = first.tags.copy()
    tags.title = opts.merge_name or tags.title
    tags.track = tags.track_total = ""
    out.parent.mkdir(parents=True, exist_ok=True)
    ff.encode_pcm(stream(), sr, channels, out, args, total_frames=int(total_len * sr), progress=progress,
                  cancel=cancel, pause=pause, audio_filter=chain, metadata=_ffmpeg_metadata(tags, opts))
    warning = _post_process_tags(out, tags, opts)
    if warning and on_warning:
        on_warning(warning)
    return out


# ------------------------------------------------------------------ batch
def convert_all(items: list[ConvertItem], opts: ConvertOptions, *,
                on_item_progress: Optional[Callable[[int, float], None]] = None,
                on_overall: Optional[ProgressFn] = None,
                on_result: Optional[Callable[[int, ItemResult], None]] = None,
                cancel: Optional[CancelToken] = None,
                pause: Optional[PauseToken] = None) -> list[ItemResult]:
    """Convert every item, carrying on after individual failures."""
    results: list[ItemResult] = []
    if not items:
        return results
    if opts.merge and len(items) > 1:
        return _run_merged(items, opts, on_item_progress, on_overall, on_result, cancel, pause)
    weights = [max(i.length, 0.001) for i in items]
    total_w = sum(weights)
    done_w = 0.0
    taken: set[Path] = set()
    for n, item in enumerate(items):
        if cancel is not None and cancel.cancelled:
            res = ItemResult(item, "cancelled", message="Cancelled")
        else:
            res = _run_one(n, item, opts, taken, weights[n], done_w, total_w,
                           on_item_progress, on_overall, cancel, pause)
        done_w += weights[n]
        results.append(res)
        if on_result:
            on_result(n, res)
        if on_overall:
            on_overall(done_w / total_w)
    return results


def _explain(exc: BaseException) -> tuple[str, Optional[access.WriteProblem]]:
    """One line saying why a conversion failed, and the write problem behind it when there was one. A file that could
    not be written gets the real reason (a missing folder, no permission, Windows Security) in place of FFmpeg's bare
    "No such file or directory"."""
    if isinstance(exc, ff.OutputError):
        problem = access.diagnose_output_error(exc)
        return access.one_line(problem, exc.path.name), problem
    return (str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)), None


def _reason(exc: BaseException) -> str:
    return _explain(exc)[0]


def _run_one(n: int, item: ConvertItem, opts: ConvertOptions, taken: set[Path], weight: float,
             done_w: float, total_w: float, on_item_progress, on_overall, cancel, pause) -> ItemResult:
    started = time.time()
    try:
        out = plan_output(item, opts, taken)
        if out is None:
            return ItemResult(item, "skipped", message="File already exists")
        taken.add(out)

        def prog(frac: float) -> None:
            if on_item_progress:
                on_item_progress(n, frac)
            if on_overall:
                on_overall((done_w + weight * frac) / total_w)

        warnings: list[str] = []
        convert_item(item, out, opts, progress=prog, cancel=cancel, pause=pause, on_warning=warnings.append)
        return ItemResult(item, "done", out, seconds=time.time() - started, warning=" ".join(warnings))
    except Cancelled:
        return ItemResult(item, "cancelled", message="Cancelled")
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
        message, problem = _explain(exc)
        return ItemResult(item, "error", message=message, problem=problem)


def _run_merged(items, opts, on_item_progress, on_overall, on_result, cancel, pause) -> list[ItemResult]:
    started = time.time()
    first = items[0]
    out = plan_output(first, opts, set(), name_override=tagmod.sanitize_filename(opts.merge_name, "Merged"))
    try:
        if out is None:
            raise FileExistsError("File already exists")

        def prog(frac: float) -> None:
            if on_overall:
                on_overall(frac)
            if on_item_progress:
                for n in range(len(items)):
                    on_item_progress(n, frac)

        warnings: list[str] = []
        convert_merged(items, out, opts, progress=prog, cancel=cancel, pause=pause, on_warning=warnings.append)
        results = [ItemResult(i, "done", out, seconds=time.time() - started, warning=" ".join(warnings)) for i in items]
    except Cancelled:
        results = [ItemResult(i, "cancelled", message="Cancelled") for i in items]
    except Exception as exc:  # noqa: BLE001
        msg, problem = _explain(exc)
        results = [ItemResult(i, "error", message=msg, problem=problem) for i in items]
    for n, res in enumerate(results):
        if on_result:
            on_result(n, res)
    return results
