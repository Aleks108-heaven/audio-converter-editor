"""Text-to-speech and audiobook creation, via pyttsx3 (offline: SAPI5 on
Windows, NSSpeechSynthesizer on macOS, espeak on Linux)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from . import core
from .core import AudioToolError


def list_voices() -> list[tuple[str, str]]:
    import pyttsx3

    engine = pyttsx3.init()
    try:
        return [(v.id, v.name) for v in engine.getProperty("voices")]
    finally:
        engine.stop()


def synthesize(
    text: str,
    out_path: str | Path,
    voice_id: str | None = None,
    rate: int | None = None,
) -> None:
    if not text.strip():
        raise AudioToolError("No text to synthesize.")

    import pyttsx3

    engine = pyttsx3.init()
    try:
        if voice_id:
            engine.setProperty("voice", voice_id)
        if rate:
            engine.setProperty("rate", rate)

        out_path = Path(out_path)
        fmt = out_path.suffix.lower().lstrip(".")
        if fmt == "wav":
            engine.save_to_file(text, str(out_path))
            engine.runAndWait()
        else:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_wav = Path(tmp_dir) / "speech.wav"
                engine.save_to_file(text, str(tmp_wav))
                engine.runAndWait()
                core.convert(tmp_wav, out_path)
    except AudioToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - pyttsx3/SAPI errors vary by platform
        raise AudioToolError(f"Text-to-speech failed: {exc}") from exc
    finally:
        engine.stop()


def create_audiobook(
    text: str,
    out_path: str | Path,
    voice_id: str | None = None,
    rate: int | None = None,
    pause_s: float = 1.0,
    split_on_blank_lines: bool = True,
) -> None:
    chapters = [c.strip() for c in text.split("\n\n")] if split_on_blank_lines else [text]
    chapters = [c for c in chapters if c]
    if not chapters:
        raise AudioToolError("No text to synthesize.")

    if len(chapters) == 1:
        synthesize(chapters[0], out_path, voice_id=voice_id, rate=rate)
        return

    with tempfile.TemporaryDirectory() as tmp_dir:
        chapter_paths = []
        for i, chapter in enumerate(chapters):
            chapter_path = Path(tmp_dir) / f"chapter_{i}.wav"
            synthesize(chapter, chapter_path, voice_id=voice_id, rate=rate)
            chapter_paths.append(chapter_path)
        core.join(chapter_paths, out_path, gap_ms=int(pause_s * 1000))
