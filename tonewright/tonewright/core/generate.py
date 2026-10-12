"""Sound generators: tones, noise, DTMF and speech."""
from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from .. import paths
from . import ffmpeg as ff
from .common import CancelToken, ProgressFn
from .source import Source, SourceWriter
from .timefmt import db_to_amp

WAVEFORMS = (("sine", "Sine"), ("square", "Square"), ("triangle", "Triangle"), ("sawtooth", "Sawtooth"))
NOISE_COLORS = (("white", "White"), ("pink", "Pink"), ("brown", "Brown"), ("blue", "Blue"), ("violet", "Violet"))
_CHUNK = 1 << 18


@dataclass
class ToneSpec:
    waveform: str = "sine"
    freq: float = 440.0
    freq_end: Optional[float] = None      # set to sweep from freq to freq_end
    sweep: str = "linear"                 # linear | log
    amp_db: float = -6.0
    seconds: float = 1.0
    fade_ms: float = 5.0                  # tiny fades at both ends avoid clicks


def _phase(spec: ToneSpec, t: np.ndarray) -> np.ndarray:
    f0 = spec.freq
    if spec.freq_end is None or abs(spec.freq_end - f0) < 1e-9 or spec.seconds <= 0:
        return 2 * np.pi * f0 * t
    f1, total = spec.freq_end, spec.seconds
    if spec.sweep == "log" and f0 > 0 and f1 > 0:
        k = f1 / f0
        return 2 * np.pi * f0 * total / np.log(k) * (np.power(k, t / total) - 1.0)
    return 2 * np.pi * (f0 * t + (f1 - f0) * t * t / (2 * total))


def _wave(kind: str, phase: np.ndarray) -> np.ndarray:
    if kind == "square":
        return np.where(np.sin(phase) >= 0, 1.0, -1.0)
    if kind == "triangle":
        return (2 / np.pi) * np.arcsin(np.sin(phase))
    if kind == "sawtooth":
        return 2.0 * ((phase / (2 * np.pi)) % 1.0) - 1.0
    return np.sin(phase)


def generate_tone(spec: ToneSpec, sr: int, channels: int, *, progress: Optional[ProgressFn] = None,
                  cancel: Optional[CancelToken] = None) -> Source:
    total = max(1, int(round(spec.seconds * sr)))
    amp = db_to_amp(spec.amp_db)
    fade = min(total // 2, int(spec.fade_ms / 1000.0 * sr))
    writer = SourceWriter(sr, channels, label="tone")
    for start in range(0, total, _CHUNK):
        if cancel is not None:
            cancel.check()
        n = min(_CHUNK, total - start)
        idx = np.arange(start, start + n, dtype=np.float64)
        sig = amp * _wave(spec.waveform, _phase(spec, idx / sr))
        if fade:
            sig *= np.clip(idx / fade, 0.0, 1.0) * np.clip((total - 1 - idx) / fade, 0.0, 1.0)
        writer.write(np.repeat(sig.astype(np.float32)[:, None], channels, axis=1))
        if progress:
            progress((start + n) / total)
    return writer.finish()


def generate_silence(seconds: float, sr: int, channels: int) -> Source:
    return Source.silence(max(1, int(round(seconds * sr))), sr, channels)


def generate_noise(color: str, amp_db: float, seconds: float, sr: int, channels: int, *,
                   progress: Optional[ProgressFn] = None, cancel: Optional[CancelToken] = None) -> Source:
    """Coloured noise at a given RMS level (dBFS). Each channel is independent."""
    total = max(1, int(round(seconds * sr)))
    inputs = ";".join(f"anoisesrc=c={color}:r={sr}:a=1:d={seconds:.6f}:seed={7 + i}[n{i}]" for i in range(channels))
    graph = inputs + (f";[n0]anull[out]" if channels == 1 else
                      f";{''.join(f'[n{i}]' for i in range(channels))}amerge=inputs={channels}[out]")
    raw = ff.render_graph(graph, sr, channels, label=f"{color} noise", cancel=cancel,
                          progress=lambda f: progress and progress(f * 0.7), expected_frames=total)
    sumsq = 0.0
    count = 0
    for start in range(0, raw.frames, _CHUNK):
        chunk = raw.read(start, start + _CHUNK)
        sumsq += float(np.einsum("ij,ij->", chunk, chunk, dtype=np.float64))
        count += chunk.size
    rms = (sumsq / max(1, count)) ** 0.5
    gain = np.float32(db_to_amp(amp_db) / rms) if rms > 0 else np.float32(1.0)
    writer = SourceWriter(sr, channels, label=f"{color} noise")
    try:
        for start in range(0, raw.frames, _CHUNK):
            if cancel is not None:
                cancel.check()
            writer.write(np.clip(raw.read(start, start + _CHUNK) * gain, -1.0, 1.0))
            if progress:
                progress(0.7 + 0.3 * min(1.0, (start + _CHUNK) / raw.frames))
    except BaseException:
        writer.abort()
        raise
    finally:
        raw.close()
    return writer.finish()


DTMF = {"1": (697, 1209), "2": (697, 1336), "3": (697, 1477), "A": (697, 1633),
        "4": (770, 1209), "5": (770, 1336), "6": (770, 1477), "B": (770, 1633),
        "7": (852, 1209), "8": (852, 1336), "9": (852, 1477), "C": (852, 1633),
        "*": (941, 1209), "0": (941, 1336), "#": (941, 1477), "D": (941, 1633)}


def generate_dtmf(digits: str, tone_ms: float, gap_ms: float, amp_db: float, sr: int, channels: int) -> Source:
    """Telephone keypad tones. Spaces and commas add a longer pause."""
    amp = db_to_amp(amp_db) / 2.0
    parts: list[np.ndarray] = []
    tone_n = max(1, int(sr * tone_ms / 1000.0))
    gap = np.zeros((max(0, int(sr * gap_ms / 1000.0)), 1), np.float32)
    ramp = min(tone_n // 2, int(0.003 * sr))
    t = np.arange(tone_n) / sr
    env = np.ones(tone_n)
    if ramp:
        env[:ramp] = np.linspace(0, 1, ramp)
        env[-ramp:] = np.linspace(1, 0, ramp)
    for ch in digits.upper():
        if ch in DTMF:
            lo, hi = DTMF[ch]
            sig = amp * env * (np.sin(2 * np.pi * lo * t) + np.sin(2 * np.pi * hi * t))
            parts += [sig.astype(np.float32)[:, None], gap]
        elif ch in " ,":
            parts += [gap, gap]
    if not parts:
        raise ValueError("Enter at least one of 0-9, *, #, A-D.")
    data = np.concatenate(parts)
    return Source.from_array(np.repeat(data, channels, axis=1), sr, "dtmf")


# ------------------------------------------------------------------ speech
_PS = [paths.powershell_exe(), "-NoProfile", "-NonInteractive", "-Command"]   # inline command: no script file, no policy bypass
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def speech_available() -> bool:
    return os.name == "nt"


def list_voices() -> list[str]:
    if not speech_available():
        return []
    script = ("Add-Type -AssemblyName System.Speech; "
              "(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() | "
              "ForEach-Object { $_.VoiceInfo.Name }")
    try:
        res = subprocess.run([*_PS, script], capture_output=True, timeout=30, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    return [v.strip() for v in res.stdout.decode("utf-8", "replace").splitlines() if v.strip()]


def synthesize_speech(text: str, voice: str = "", rate: int = 0, volume: int = 100, *,
                      cancel: Optional[CancelToken] = None) -> Source:
    """Speak `text` with an installed Windows voice (rate -10..10, volume 0..100)."""
    if not speech_available():
        raise RuntimeError("Speech generation needs the voices that come with Windows.")
    if not text.strip():
        raise ValueError("Type some text to speak.")
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "if ($env:TW_VOICE) { $s.SelectVoice($env:TW_VOICE) }; "
        "$s.Rate = [int]$env:TW_RATE; $s.Volume = [int]$env:TW_VOLUME; "
        "$s.SetOutputToWaveFile($env:TW_OUT); "
        "$s.Speak([IO.File]::ReadAllText($env:TW_TEXT, [Text.Encoding]::UTF8)); $s.Dispose()")
    with tempfile.TemporaryDirectory(prefix="tw-tts-") as tmp:
        text_file, wav_file = Path(tmp) / "text.txt", Path(tmp) / "speech.wav"
        text_file.write_text(text, encoding="utf-8")
        env = dict(os.environ, TW_VOICE=voice, TW_RATE=str(int(rate)), TW_VOLUME=str(int(volume)),
                   TW_OUT=str(wav_file), TW_TEXT=str(text_file))
        proc = subprocess.run([*_PS, script], capture_output=True, env=env, timeout=600,
                              creationflags=_NO_WINDOW)
        if cancel is not None:
            cancel.check()
        if proc.returncode != 0 or not wav_file.exists():
            raise RuntimeError("The Windows speech engine failed: "
                               + proc.stderr.decode("utf-8", "replace").strip()[:300])
        src = ff.decode_to_source(wav_file, cancel=cancel)
    src.label = "speech"
    return src
