"""Audio effects and filters.

fade_in/fade_out/normalize/reverse run through pydub directly. echo/chorus/
flanger/reverb aren't exposed by pydub, so those shell out to ffmpeg's audio
filter graph (-af) directly and require ffmpeg on PATH regardless of format.
"""

from __future__ import annotations

from pathlib import Path

from pydub import effects as pydub_effects

from .core import export, load, run_ffmpeg_filter


def fade_in(in_path: str | Path, out_path: str | Path, duration_ms: int) -> None:
    audio = load(in_path)
    export(audio.fade_in(duration_ms), out_path)


def fade_out(in_path: str | Path, out_path: str | Path, duration_ms: int) -> None:
    audio = load(in_path)
    export(audio.fade_out(duration_ms), out_path)


def normalize(in_path: str | Path, out_path: str | Path) -> None:
    audio = load(in_path)
    export(pydub_effects.normalize(audio), out_path)


def reverse(in_path: str | Path, out_path: str | Path) -> None:
    audio = load(in_path)
    export(audio.reverse(), out_path)


def echo(in_path: str | Path, out_path: str | Path, delay_ms: int = 500, decay: float = 0.5) -> None:
    run_ffmpeg_filter(in_path, out_path, f"aecho=0.8:0.9:{delay_ms}:{decay}")


def chorus(in_path: str | Path, out_path: str | Path) -> None:
    run_ffmpeg_filter(
        in_path, out_path, "chorus=0.5:0.9:50|60|40:0.4|0.32|0.3:0.25|0.4|0.3:2|2.3|1.3"
    )


def flanger(in_path: str | Path, out_path: str | Path) -> None:
    run_ffmpeg_filter(in_path, out_path, "flanger")


def reverb(in_path: str | Path, out_path: str | Path, amount: float = 0.5) -> None:
    # Approximate a room reverb by layering a few short, decaying echo taps.
    decay = max(0.1, min(amount, 0.9))
    run_ffmpeg_filter(
        in_path,
        out_path,
        f"aecho=0.8:0.88:60:{decay},aecho=0.7:0.7:150:{decay * 0.7}",
    )
