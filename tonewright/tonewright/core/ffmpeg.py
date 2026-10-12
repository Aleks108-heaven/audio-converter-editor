"""Thin wrapper around ffmpeg / ffprobe.

Audio travels to and from ffmpeg as raw interleaved float32 over pipes, so large
files never have to fit in memory and no temporary WAV files are needed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

import numpy as np

from .common import Cancelled, CancelToken, PauseToken, ProgressFn
from .source import Source, SourceWriter

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_overrides: dict[str, str] = {}
_found: dict[str, str] = {}
_IO_CHUNK = 1 << 18


class FFmpegMissing(RuntimeError):
    pass


class FFmpegError(RuntimeError):
    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message if not detail else f"{message}\n{detail}")
        self.detail = detail


# ---------------------------------------------------------------- locating
def set_tool_override(name: str, path: str) -> None:
    _overrides[name] = path
    _found.pop(name, None)


def _in_cwd(candidate: str) -> bool:
    """A program sitting in the current folder may have arrived with a download, so it is never picked up by name."""
    try:
        return Path(candidate).resolve().parent == Path.cwd().resolve()
    except OSError:
        return False


def _candidate_paths(name: str) -> list[str]:
    exe = name + (".exe" if os.name == "nt" else "")
    out: list[str] = []
    if name in _overrides:
        out.append(_overrides[name])
    env_dir = os.environ.get("TONEWRIGHT_FFMPEG_DIR")
    if env_dir:
        out.append(os.path.join(env_dir, exe))
    out.append(str(Path(__file__).resolve().parents[2] / "bin" / exe))   # the copy shipped with the installer wins
    which = shutil.which(name)
    if which and not _in_cwd(which):
        out.append(which)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        home = os.path.expanduser("~")
        out.append(os.path.join(local, "Microsoft", "WinGet", "Links", exe))
        pkg_root = Path(local) / "Microsoft" / "WinGet" / "Packages"
        if pkg_root.exists():
            for hit in pkg_root.glob(f"*FFmpeg*/**/bin/{exe}"):
                out.append(str(hit))
        out += [os.path.join(home, "scoop", "shims", exe), rf"C:\ffmpeg\bin\{exe}",
                rf"C:\Program Files\ffmpeg\bin\{exe}", rf"C:\ProgramData\chocolatey\bin\{exe}"]
    return out


def find_tool(name: str) -> str:
    cached = _found.get(name)
    if cached and os.path.isfile(cached):
        return cached
    for cand in _candidate_paths(name):
        if cand and os.path.isfile(cand):
            _found[name] = cand
            return cand
    raise FFmpegMissing(
        f"{name} was not found. Install FFmpeg (winget install Gyan.FFmpeg) and restart Tonewright.")


def available() -> bool:
    try:
        find_tool("ffmpeg")
        find_tool("ffprobe")
        return True
    except FFmpegMissing:
        return False


_caps: dict[str, set[str]] = {}


def _capability_set(kind: str) -> set[str]:
    if kind not in _caps:
        flag = {"filters": "-filters", "encoders": "-encoders", "decoders": "-decoders"}[kind]
        try:
            text = subprocess.run([find_tool("ffmpeg"), "-hide_banner", flag], capture_output=True,
                                  creationflags=_NO_WINDOW, timeout=30).stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError, FFmpegMissing):
            text = ""
        names: set[str] = set()
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 2 and re.fullmatch(r"[A-Z.]{2,7}", parts[0]) and parts[1] != "=":
                names.add(parts[1])
        _caps[kind] = names
    return _caps[kind]


def has_filter(name: str) -> bool:
    return name in _capability_set("filters")


def has_encoder(name: str) -> bool:
    return name in _capability_set("encoders")


# ---------------------------------------------------------------- probing
@dataclass
class StreamInfo:
    index: int
    codec: str
    sample_rate: int
    channels: int
    layout: str = ""
    bits: int = 0
    bit_rate: int = 0
    duration: float = 0.0
    language: str = ""
    title: str = ""


@dataclass
class MediaInfo:
    path: str
    format_name: str = ""
    duration: float = 0.0
    size: int = 0
    bit_rate: int = 0
    audio: list[StreamInfo] = field(default_factory=list)
    has_video: bool = False
    has_cover: bool = False
    tags: dict[str, str] = field(default_factory=dict)
    chapters: list[tuple[float, float, str]] = field(default_factory=list)

    @property
    def first_audio(self) -> StreamInfo:
        if not self.audio:
            raise FFmpegError("This file has no audio stream.")
        return self.audio[0]


_SAMPLE_FMT_BITS = {"u8": 8, "u8p": 8, "s16": 16, "s16p": 16, "s32": 32, "s32p": 32,
                    "flt": 32, "fltp": 32, "dbl": 64, "dblp": 64, "s64": 64, "s64p": 64}


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def probe(path: str | os.PathLike) -> MediaInfo:
    cmd = [find_tool("ffprobe"), "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", "-show_chapters", str(path)]
    res = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)
    if res.returncode != 0:
        raise FFmpegError(f"Cannot read {Path(path).name}", res.stderr.decode("utf-8", "replace").strip())
    data = json.loads(res.stdout.decode("utf-8", "replace") or "{}")
    fmt = data.get("format", {})
    info = MediaInfo(
        path=str(path),
        format_name=fmt.get("format_name", ""),
        duration=_to_float(fmt.get("duration")),
        size=_to_int(fmt.get("size")),
        bit_rate=_to_int(fmt.get("bit_rate")),
        tags={k.lower(): str(v) for k, v in (fmt.get("tags") or {}).items()},
    )
    for st in data.get("streams", []):
        kind = st.get("codec_type")
        if kind == "audio":
            bits = _to_int(st.get("bits_per_raw_sample")) or _to_int(st.get("bits_per_sample")) \
                or _SAMPLE_FMT_BITS.get(st.get("sample_fmt", ""), 0)
            tags = {k.lower(): v for k, v in (st.get("tags") or {}).items()}
            info.audio.append(StreamInfo(
                index=_to_int(st.get("index")), codec=st.get("codec_name", ""),
                sample_rate=_to_int(st.get("sample_rate"), 44100), channels=_to_int(st.get("channels"), 2),
                layout=st.get("channel_layout", ""), bits=bits, bit_rate=_to_int(st.get("bit_rate")),
                duration=_to_float(st.get("duration")), language=tags.get("language", ""),
                title=tags.get("title", "")))
        elif kind == "video":
            if (st.get("disposition") or {}).get("attached_pic"):
                info.has_cover = True
            else:
                info.has_video = True
    for ch in data.get("chapters", []):
        title = (ch.get("tags") or {}).get("title", "")
        info.chapters.append((_to_float(ch.get("start_time")), _to_float(ch.get("end_time")), title))
    if info.duration <= 0:
        info.duration = max((a.duration for a in info.audio), default=0.0)
    for a in info.audio:
        if a.duration <= 0:
            a.duration = info.duration
    return info


# ---------------------------------------------------------------- processes
def _suspend(proc: subprocess.Popen, suspend: bool) -> None:
    try:
        if os.name == "nt":
            import ctypes
            fn = ctypes.windll.ntdll.NtSuspendProcess if suspend else ctypes.windll.ntdll.NtResumeProcess
            fn(ctypes.c_void_p(int(proc._handle)))  # type: ignore[attr-defined]
        else:
            os.kill(proc.pid, signal.SIGSTOP if suspend else signal.SIGCONT)
    except Exception:
        pass


class _Proc:
    """A running ffmpeg: drains stderr, honours cancel, and can be suspended."""

    def __init__(self, args: Sequence[str], *, stdin: bool = False, stdout: bool = False,
                 cancel: Optional[CancelToken] = None, pause: Optional[PauseToken] = None) -> None:
        self.args = list(args)
        self.p = subprocess.Popen(
            self.args,
            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE if stdout else subprocess.DEVNULL,
            stderr=subprocess.PIPE, creationflags=_NO_WINDOW, bufsize=0)
        self._tail: deque[str] = deque(maxlen=60)
        self._cancel = cancel
        self._pause = pause
        self._done = threading.Event()
        self._err_thread = threading.Thread(target=self._drain, daemon=True)
        self._err_thread.start()
        self._watch_thread = threading.Thread(target=self._watch, daemon=True)
        self._watch_thread.start()
        if pause is not None:
            pause.add_listener(self._on_pause)
            if pause.paused:
                self._on_pause(True)

    def _drain(self) -> None:
        pending = b""
        while True:
            data = self.p.stderr.read(4096)  # type: ignore[union-attr]
            if not data:
                break
            pending += data
            *lines, pending = pending.split(b"\n")
            for line in lines:
                self._tail.append(line.decode("utf-8", "replace").rstrip())
        if pending:
            self._tail.append(pending.decode("utf-8", "replace").rstrip())

    def _watch(self) -> None:
        while not self._done.wait(0.1):
            if self._cancel is not None and self._cancel.cancelled:
                self.kill()
                return

    def _on_pause(self, paused: bool) -> None:
        if self.p.poll() is None:
            _suspend(self.p, paused)

    @property
    def stderr_text(self) -> str:
        self._err_thread.join(timeout=1.0)
        return "\n".join(line for line in self._tail if line)

    def kill(self) -> None:
        try:
            if self.p.poll() is None:
                _suspend(self.p, False)
                self.p.kill()
        except OSError:
            pass

    def finish(self) -> int:
        """Wait for exit and release helpers."""
        try:
            rc = self.p.wait()
        finally:
            self._done.set()
            if self._pause is not None:
                self._pause.remove_listener(self._on_pause)
            self._err_thread.join(timeout=2.0)
            for stream in (self.p.stdin, self.p.stdout, self.p.stderr):
                try:
                    if stream:
                        stream.close()
                except OSError:
                    pass
        return rc


def _tmp_sibling(out_path: str | os.PathLike) -> Path:
    p = Path(out_path)
    return p.with_name(f"{p.stem}.tonewright-part{p.suffix}")


def _remove_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def run_capture(args: Sequence[str], *, cancel: Optional[CancelToken] = None) -> str:
    """Run ffmpeg with no outputs of interest and return its stderr (analysis filters)."""
    proc = _Proc([find_tool("ffmpeg"), "-hide_banner", "-nostdin", *args], cancel=cancel)
    rc = proc.finish()
    text = proc.stderr_text
    if cancel is not None:
        cancel.check()
    if rc != 0:
        raise FFmpegError("ffmpeg failed", text)
    return text


# ---------------------------------------------------------------- decoding
def decode_to_source(path: str | os.PathLike, *, stream: Optional[StreamInfo] = None,
                     info: Optional[MediaInfo] = None, progress: Optional[ProgressFn] = None,
                     cancel: Optional[CancelToken] = None) -> Source:
    """Decode a file's audio to a float32 Source at its native rate (max 2 channels)."""
    info = info or probe(path)
    st = stream or info.first_audio
    channels = 1 if st.channels == 1 else 2
    sr = st.sample_rate
    expected = max(1, int(st.duration * sr)) if st.duration else 0
    cmd = [find_tool("ffmpeg"), "-v", "error", "-nostdin", "-i", str(path),
           "-map", f"0:{st.index}", "-vn", "-sn", "-dn", "-f", "f32le", "-acodec", "pcm_f32le",
           "-ac", str(channels), "pipe:1"]
    writer = SourceWriter(sr, channels, label=Path(path).name)
    proc = _Proc(cmd, stdout=True, cancel=cancel)
    frame_bytes = channels * 4
    buf = bytearray()
    try:
        while True:
            data = proc.p.stdout.read(_IO_CHUNK)  # type: ignore[union-attr]
            if not data:
                break
            buf += data
            if len(buf) >= (1 << 20):
                n = (len(buf) // frame_bytes) * frame_bytes
                writer.write(np.frombuffer(bytes(buf[:n]), dtype="<f4").reshape(-1, channels))
                del buf[:n]
                if progress and expected:
                    progress(min(0.99, writer.frames / expected))
            if cancel is not None:
                cancel.check()
        n = (len(buf) // frame_bytes) * frame_bytes
        if n:
            writer.write(np.frombuffer(bytes(buf[:n]), dtype="<f4").reshape(-1, channels))
        proc.finish()
        if cancel is not None:
            cancel.check()
        if writer.frames == 0:
            raise FFmpegError(f"No audio could be decoded from {Path(path).name}", proc.stderr_text)
        # a non-zero exit after some audio decoded (e.g. a truncated file) keeps what we got
    except BaseException:
        proc.kill()
        proc.finish()
        writer.abort()
        raise
    if progress:
        progress(1.0)
    return writer.finish()


# ---------------------------------------------------------------- streaming filter
def _feed(proc: _Proc, chunks: Iterable[np.ndarray], holder: dict,
          total_frames: Optional[int], progress: Optional[ProgressFn],
          cancel: Optional[CancelToken], pause: Optional[PauseToken]) -> None:
    sent = 0
    try:
        for chunk in chunks:
            if cancel is not None:
                cancel.check()
            if pause is not None:
                pause.wait(cancel)
            arr = np.ascontiguousarray(chunk, dtype="<f4")
            proc.p.stdin.write(memoryview(arr).cast("B"))  # type: ignore[union-attr]
            sent += arr.shape[0]
            if progress and total_frames:
                progress(min(0.99, sent / total_frames))
    except (BrokenPipeError, OSError):
        pass  # ffmpeg exited early; the reader reports why
    except BaseException as exc:  # noqa: BLE001 - forwarded to the caller thread
        holder["error"] = exc
    finally:
        try:
            proc.p.stdin.close()  # type: ignore[union-attr]
        except OSError:
            pass


def analyze_stream(chunks: Iterable[np.ndarray], sr: int, channels: int, audio_filter: str, *,
                   total_frames: Optional[int] = None, progress: Optional[ProgressFn] = None,
                   cancel: Optional[CancelToken] = None) -> str:
    """Feed PCM through an analysis filter (loudnorm, volumedetect...) and return its report."""
    cmd = [find_tool("ffmpeg"), "-hide_banner", "-f", "f32le", "-ar", str(sr), "-ac", str(channels),
           "-i", "pipe:0", "-af", audio_filter, "-f", "null", "-"]
    proc = _Proc(cmd, stdin=True, cancel=cancel)
    holder: dict = {}
    try:
        _feed(proc, chunks, holder, total_frames, progress, cancel, None)
        rc = proc.finish()
        if "error" in holder:
            raise holder["error"]
        if cancel is not None:
            cancel.check()
        text = proc.stderr_text
        if rc != 0:
            raise FFmpegError("The audio analysis failed", text)
    except BaseException:
        proc.kill()
        proc.finish()
        raise
    return text


def filter_stream(chunks: Iterable[np.ndarray], sr: int, channels: int, audio_filter: str,
                  writer: SourceWriter, *, out_sr: Optional[int] = None,
                  out_channels: Optional[int] = None,
                  total_frames: Optional[int] = None, progress: Optional[ProgressFn] = None,
                  cancel: Optional[CancelToken] = None) -> None:
    """Run PCM through an ffmpeg -af chain and append the result to `writer`."""
    out_sr = out_sr or sr
    channels_out = out_channels or channels
    cmd = [find_tool("ffmpeg"), "-v", "error", "-f", "f32le", "-ar", str(sr), "-ac", str(channels),
           "-i", "pipe:0", "-af", audio_filter, "-f", "f32le", "-ar", str(out_sr),
           "-ac", str(channels_out), "pipe:1"]
    proc = _Proc(cmd, stdin=True, stdout=True, cancel=cancel)
    holder: dict = {}
    feeder = threading.Thread(target=_feed, args=(proc, chunks, holder, total_frames, progress, cancel, None),
                              daemon=True)
    feeder.start()
    frame_bytes = channels_out * 4
    buf = bytearray()
    try:
        while True:
            data = proc.p.stdout.read(_IO_CHUNK)  # type: ignore[union-attr]
            if not data:
                break
            buf += data
            n = (len(buf) // frame_bytes) * frame_bytes
            if n >= (1 << 20):
                writer.write(np.frombuffer(bytes(buf[:n]), dtype="<f4").reshape(-1, channels_out))
                del buf[:n]
        n = (len(buf) // frame_bytes) * frame_bytes
        if n:
            writer.write(np.frombuffer(bytes(buf[:n]), dtype="<f4").reshape(-1, channels_out))
        feeder.join()
        rc = proc.finish()
    except BaseException:
        proc.kill()
        proc.finish()
        raise
    if "error" in holder:
        raise holder["error"]
    if cancel is not None:
        cancel.check()
    if rc != 0:
        raise FFmpegError("The audio filter failed", proc.stderr_text)
    if progress:
        progress(1.0)


# ---------------------------------------------------------------- encoding
def _metadata_args(metadata: Optional[dict[str, str]]) -> list[str]:
    args: list[str] = []
    for key, value in (metadata or {}).items():
        if value not in (None, ""):
            args += ["-metadata", f"{key}={value}"]
    return args


def encode_pcm(chunks: Iterable[np.ndarray], sr: int, channels: int, out_path: str | os.PathLike,
               out_args: Sequence[str], *, total_frames: Optional[int] = None,
               progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None,
               pause: Optional[PauseToken] = None, audio_filter: Optional[str] = None,
               metadata: Optional[dict[str, str]] = None) -> Path:
    """Encode a stream of float32 frames to `out_path` (written atomically)."""
    out_path = Path(out_path)
    tmp = _tmp_sibling(out_path)
    cmd = [find_tool("ffmpeg"), "-y", "-v", "error", "-f", "f32le", "-ar", str(sr),
           "-ac", str(channels), "-i", "pipe:0"]
    if audio_filter:
        cmd += ["-af", audio_filter]
    cmd += ["-vn", *_metadata_args(metadata), *out_args, str(tmp)]
    proc = _Proc(cmd, stdin=True, cancel=cancel)
    holder: dict = {}
    try:
        _feed(proc, chunks, holder, total_frames, progress, cancel, pause)
        rc = proc.finish()
        if "error" in holder:
            raise holder["error"]
        if cancel is not None:
            cancel.check()
        if rc != 0:
            raise FFmpegError(f"Could not write {out_path.name}", proc.stderr_text)
        os.replace(tmp, out_path)
    except BaseException:
        proc.kill()
        proc.finish()
        _remove_quiet(tmp)
        raise
    if progress:
        progress(1.0)
    return out_path


_TIME_RE = re.compile(r"out_time_us=(\d+)")


def convert_file(in_path: str | os.PathLike, out_path: str | os.PathLike, out_args: Sequence[str], *,
                 stream_index: Optional[int] = None, audio_filter: Optional[str] = None,
                 duration: float = 0.0, metadata: Optional[dict[str, str]] = None,
                 progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None,
                 pause: Optional[PauseToken] = None) -> Path:
    """Transcode one file with ffmpeg doing the decoding (no tags/cover are copied)."""
    out_path = Path(out_path)
    tmp = _tmp_sibling(out_path)
    cmd = [find_tool("ffmpeg"), "-y", "-v", "error", "-nostdin", "-progress", "pipe:1", "-nostats",
           "-i", str(in_path), "-map", f"0:{stream_index}" if stream_index is not None else "0:a:0",
           "-vn", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1"]
    if audio_filter:
        cmd += ["-af", audio_filter]
    cmd += [*_metadata_args(metadata), *out_args, str(tmp)]
    proc = _Proc(cmd, stdout=True, cancel=cancel, pause=pause)
    try:
        for raw in iter(proc.p.stdout.readline, b""):  # type: ignore[union-attr]
            m = _TIME_RE.search(raw.decode("ascii", "ignore"))
            if m and progress and duration > 0:
                progress(min(0.99, int(m.group(1)) / 1e6 / duration))
        rc = proc.finish()
        if cancel is not None:
            cancel.check()
        if rc != 0:
            raise FFmpegError(f"Could not convert {Path(in_path).name}", proc.stderr_text)
        os.replace(tmp, out_path)
    except BaseException:
        proc.kill()
        proc.finish()
        _remove_quiet(tmp)
        raise
    if progress:
        progress(1.0)
    return out_path


def render_graph(graph: str, sr: int, channels: int, *, label: str = "generated",
                 progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None,
                 expected_frames: int = 0) -> Source:
    """Render an ffmpeg -filter_complex graph with a [out] pad (a generator) to a Source."""
    cmd = [find_tool("ffmpeg"), "-v", "error", "-nostdin", "-filter_complex", graph, "-map", "[out]",
           "-f", "f32le", "-ar", str(sr), "-ac", str(channels), "pipe:1"]
    writer = SourceWriter(sr, channels, label=label)
    proc = _Proc(cmd, stdout=True, cancel=cancel)
    frame_bytes = channels * 4
    buf = bytearray()
    try:
        while True:
            data = proc.p.stdout.read(_IO_CHUNK)  # type: ignore[union-attr]
            if not data:
                break
            buf += data
            if len(buf) >= (1 << 20):
                n = (len(buf) // frame_bytes) * frame_bytes
                writer.write(np.frombuffer(bytes(buf[:n]), dtype="<f4").reshape(-1, channels))
                del buf[:n]
                if progress and expected_frames:
                    progress(min(0.99, writer.frames / expected_frames))
        n = (len(buf) // frame_bytes) * frame_bytes
        if n:
            writer.write(np.frombuffer(bytes(buf[:n]), dtype="<f4").reshape(-1, channels))
        rc = proc.finish()
        if cancel is not None:
            cancel.check()
        if rc != 0 or writer.frames == 0:
            raise FFmpegError("Could not generate audio", proc.stderr_text)
    except BaseException:
        proc.kill()
        proc.finish()
        writer.abort()
        raise
    return writer.finish()


def convert_whole_file_to_pcm(in_path: str | os.PathLike, sr: int, channels: int, *,
                              stream_index: Optional[int] = None,
                              cancel: Optional[CancelToken] = None) -> Iterable[np.ndarray]:
    """Generator of float32 chunks of a file resampled/remixed to (sr, channels)."""
    cmd = [find_tool("ffmpeg"), "-v", "error", "-nostdin", "-i", str(in_path),
           "-map", f"0:{stream_index}" if stream_index is not None else "0:a:0",
           "-vn", "-sn", "-dn", "-f", "f32le", "-acodec", "pcm_f32le", "-ar", str(sr),
           "-ac", str(channels), "pipe:1"]
    proc = _Proc(cmd, stdout=True, cancel=cancel)
    frame_bytes = channels * 4
    buf = bytearray()
    try:
        while True:
            data = proc.p.stdout.read(_IO_CHUNK)  # type: ignore[union-attr]
            if not data:
                break
            buf += data
            n = (len(buf) // frame_bytes) * frame_bytes
            if n >= (1 << 19):
                yield np.frombuffer(bytes(buf[:n]), dtype="<f4").reshape(-1, channels)
                del buf[:n]
            if cancel is not None:
                cancel.check()
        n = (len(buf) // frame_bytes) * frame_bytes
        if n:
            yield np.frombuffer(bytes(buf[:n]), dtype="<f4").reshape(-1, channels)
        rc = proc.finish()
        if cancel is not None:
            cancel.check()
        if rc != 0:
            raise FFmpegError(f"Could not read {Path(in_path).name}", proc.stderr_text)
    finally:
        proc.kill()
        proc.finish()
