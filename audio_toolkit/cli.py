"""Command-line interface for audio_toolkit."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from . import batch as batch_module
from . import channels, core, effects, equalizer, generate, tags


@click.group()
def cli() -> None:
    """Convert, cut, trim, split, join and mix audio files."""


@cli.command()
@click.argument("in_path")
@click.argument("out_path")
@click.option("--bitrate", default=None, help="Output bitrate, e.g. 192k")
def convert(in_path: str, out_path: str, bitrate: str | None) -> None:
    """Convert IN_PATH to OUT_PATH (format inferred from extension)."""
    core.convert(in_path, out_path, bitrate=bitrate)
    click.echo(f"Converted -> {out_path}")


@cli.command()
@click.argument("in_path")
@click.argument("out_path")
@click.option("--start", required=True, help="Start time, e.g. 0:05 or 5.5")
@click.option("--end", required=True, help="End time, e.g. 1:30")
def trim(in_path: str, out_path: str, start: str, end: str) -> None:
    """Keep only the [START, END] portion of IN_PATH."""
    core.trim(in_path, out_path, core.ms_from_str(start), core.ms_from_str(end))
    click.echo(f"Trimmed -> {out_path}")


@cli.command()
@click.argument("in_path")
@click.argument("out_path")
@click.option("--start", required=True, help="Start of the section to remove")
@click.option("--end", required=True, help="End of the section to remove")
def cut(in_path: str, out_path: str, start: str, end: str) -> None:
    """Remove the [START, END] section from IN_PATH and splice the rest."""
    core.cut(in_path, out_path, core.ms_from_str(start), core.ms_from_str(end))
    click.echo(f"Cut -> {out_path}")


@cli.command()
@click.argument("in_path")
@click.argument("out_dir")
@click.option("--at", "at_times", multiple=True, required=True, help="Split point, repeatable")
def split(in_path: str, out_dir: str, at_times: tuple[str, ...]) -> None:
    """Split IN_PATH into parts at each --at timestamp, written to OUT_DIR."""
    points = [core.ms_from_str(t) for t in at_times]
    outputs = core.split(in_path, out_dir, points)
    for path in outputs:
        click.echo(f"  -> {path}")
    click.echo(f"Split into {len(outputs)} file(s)")


@cli.command()
@click.argument("in_paths", nargs=-1, required=True)
@click.option("-o", "--out", "out_path", required=True, help="Output file path")
@click.option("--gap", default=0.0, help="Silence gap in seconds between clips")
def join(in_paths: tuple[str, ...], out_path: str, gap: float) -> None:
    """Concatenate IN_PATHS in order into OUT_PATH."""
    core.join(list(in_paths), out_path, gap_ms=int(gap * 1000))
    click.echo(f"Joined -> {out_path}")


@cli.command()
@click.argument("in_paths", nargs=-1, required=True)
@click.option("-o", "--out", "out_path", required=True, help="Output file path")
@click.option("--gain", "gains", multiple=True, type=float, help="Gain in dB per input, in order")
def mix(in_paths: tuple[str, ...], out_path: str, gains: tuple[float, ...]) -> None:
    """Overlay IN_PATHS into a single mixed-down OUT_PATH."""
    gains_list = list(gains) if gains else None
    core.mix(list(in_paths), out_path, gains_db=gains_list)
    click.echo(f"Mixed -> {out_path}")


@cli.command("get-tags")
@click.argument("in_path")
def get_tags_cmd(in_path: str) -> None:
    """Print metadata tags for IN_PATH."""
    values = tags.get_tags(in_path)
    if not values:
        click.echo("(no tags set)")
    for field, value in values.items():
        click.echo(f"{field}: {value}")


@cli.command("set-tags")
@click.argument("in_path")
@click.option("--title", default=None)
@click.option("--artist", default=None)
@click.option("--album", default=None)
@click.option("--genre", default=None)
@click.option("--date", default=None)
@click.option("--tracknumber", default=None)
def set_tags_cmd(in_path: str, **fields: str | None) -> None:
    """Set one or more metadata tags on IN_PATH."""
    values = {k: v for k, v in fields.items() if v is not None}
    if not values:
        raise click.UsageError("Provide at least one tag option to set.")
    tags.set_tags(in_path, **values)
    click.echo(f"Updated tags on {in_path}")


@cli.command()
@click.argument("in_paths", nargs=-1, required=True)
@click.option("--pattern", required=True, help="e.g. '{artist} - {title}' or '{tracknumber} {original}'")
@click.option("--out-dir", default=None, help="Optional directory to move renamed files into")
def rename(in_paths: tuple[str, ...], pattern: str, out_dir: str | None) -> None:
    """Batch-rename files from their tags using PATTERN."""
    outputs = tags.rename_from_tags(list(in_paths), pattern, out_dir=out_dir)
    for path in outputs:
        click.echo(f"  -> {path}")
    click.echo(f"Renamed {len(outputs)} file(s)")


@cli.command()
@click.argument("effect_name")
@click.argument("in_path")
@click.argument("out_path")
@click.option("--duration", default=1000, help="Fade duration in ms (fade-in/fade-out only)")
@click.option("--delay", default=500, help="Echo delay in ms (echo only)")
@click.option("--decay", default=0.5, help="Echo/reverb decay 0-1 (echo/reverb only)")
def effect(effect_name: str, in_path: str, out_path: str, duration: int, delay: int, decay: float) -> None:
    """Apply an EFFECT_NAME to IN_PATH: fade-in, fade-out, normalize, reverse, echo, chorus, flanger, reverb."""
    dispatch = {
        "fade-in": lambda: effects.fade_in(in_path, out_path, duration),
        "fade-out": lambda: effects.fade_out(in_path, out_path, duration),
        "normalize": lambda: effects.normalize(in_path, out_path),
        "reverse": lambda: effects.reverse(in_path, out_path),
        "echo": lambda: effects.echo(in_path, out_path, delay_ms=delay, decay=decay),
        "chorus": lambda: effects.chorus(in_path, out_path),
        "flanger": lambda: effects.flanger(in_path, out_path),
        "reverb": lambda: effects.reverb(in_path, out_path, amount=decay),
    }
    if effect_name not in dispatch:
        raise click.UsageError(f"Unknown effect '{effect_name}'. Choices: {', '.join(dispatch)}")
    dispatch[effect_name]()
    click.echo(f"Applied {effect_name} -> {out_path}")


@cli.command()
@click.argument("in_path")
def play(in_path: str) -> None:
    """Play IN_PATH through the default output device (blocks until done)."""
    from . import playback  # lazy: sounddevice/portaudio not needed for other commands

    click.echo("Playing... (Ctrl+C to stop early)")
    try:
        playback.play(in_path, blocking=True)
    except KeyboardInterrupt:
        playback.stop_playback()
        click.echo("Stopped.")


@cli.command()
@click.argument("out_path")
@click.option("--duration", required=True, type=float, help="Recording length in seconds")
@click.option("--samplerate", default=44100, help="Sample rate in Hz")
@click.option("--channels", "n_channels", default=1, help="1 = mono, 2 = stereo")
def record(out_path: str, duration: float, samplerate: int, n_channels: int) -> None:
    """Record DURATION seconds from the default microphone to OUT_PATH."""
    from . import playback

    click.echo(f"Recording {duration}s...")
    playback.record(out_path, duration, samplerate=samplerate, channels=n_channels)
    click.echo(f"Recorded -> {out_path}")


@cli.command("split-channels")
@click.argument("in_path")
@click.argument("out_dir")
def split_channels_cmd(in_path: str, out_dir: str) -> None:
    """Split a stereo IN_PATH into separate left/right mono files in OUT_DIR."""
    left, right = channels.split_channels(in_path, out_dir)
    click.echo(f"  -> {left}")
    click.echo(f"  -> {right}")


@cli.command("combine-channels")
@click.argument("left_path")
@click.argument("right_path")
@click.argument("out_path")
def combine_channels_cmd(left_path: str, right_path: str, out_path: str) -> None:
    """Combine two mono LEFT_PATH/RIGHT_PATH files into a stereo OUT_PATH."""
    channels.combine_channels(left_path, right_path, out_path)
    click.echo(f"Combined -> {out_path}")


@cli.command("channel-gain")
@click.argument("in_path")
@click.argument("out_path")
@click.option("--left", "left_db", default=0.0, help="Left channel gain in dB")
@click.option("--right", "right_db", default=0.0, help="Right channel gain in dB")
def channel_gain_cmd(in_path: str, out_path: str, left_db: float, right_db: float) -> None:
    """Adjust left/right channel gain independently on a stereo IN_PATH."""
    channels.channel_gain(in_path, out_path, left_db=left_db, right_db=right_db)
    click.echo(f"Applied channel gain -> {out_path}")


@cli.command("generate-tone")
@click.argument("out_path")
@click.option("--freq", required=True, type=float, help="Frequency in Hz")
@click.option("--duration", required=True, type=float, help="Duration in seconds")
@click.option("--samplerate", default=44100, help="Sample rate in Hz")
@click.option("--amplitude", default=0.3, help="Amplitude 0-1")
def generate_tone(out_path: str, freq: float, duration: float, samplerate: int, amplitude: float) -> None:
    """Generate a sine-wave tone at OUT_PATH."""
    generate.tone(out_path, freq, duration, samplerate=samplerate, amplitude=amplitude)
    click.echo(f"Generated tone -> {out_path}")


@cli.command("generate-noise")
@click.argument("out_path")
@click.option("--type", "noise_type", type=click.Choice(["white", "pink"]), default="white")
@click.option("--duration", required=True, type=float, help="Duration in seconds")
@click.option("--samplerate", default=44100, help="Sample rate in Hz")
@click.option("--amplitude", default=0.3, help="Amplitude 0-1")
def generate_noise(out_path: str, noise_type: str, duration: float, samplerate: int, amplitude: float) -> None:
    """Generate white or pink noise at OUT_PATH."""
    func = generate.white_noise if noise_type == "white" else generate.pink_noise
    func(out_path, duration, samplerate=samplerate, amplitude=amplitude)
    click.echo(f"Generated {noise_type} noise -> {out_path}")


@cli.command("generate-silence")
@click.argument("out_path")
@click.option("--duration", required=True, type=float, help="Duration in seconds")
@click.option("--samplerate", default=44100, help="Sample rate in Hz")
@click.option("--channels", "n_channels", default=1, help="1 = mono, 2 = stereo")
def generate_silence(out_path: str, duration: float, samplerate: int, n_channels: int) -> None:
    """Generate silence at OUT_PATH."""
    generate.silence(out_path, duration, samplerate=samplerate, channels=n_channels)
    click.echo(f"Generated silence -> {out_path}")


@cli.command("list-voices")
def list_voices_cmd() -> None:
    """List installed text-to-speech voices."""
    from . import audiobook

    for voice_id, name in audiobook.list_voices():
        click.echo(f"{name}\n  {voice_id}")


@cli.command()
@click.argument("out_path")
@click.option("--text", default=None, help="Text to speak")
@click.option("--text-file", default=None, type=click.Path(exists=True), help="Read text from a file instead")
@click.option("--voice", default=None, help="Voice id from list-voices")
@click.option("--rate", default=None, type=int, help="Speech rate (words per minute)")
def tts(out_path: str, text: str | None, text_file: str | None, voice: str | None, rate: int | None) -> None:
    """Convert TEXT (or --text-file) to speech at OUT_PATH."""
    from . import audiobook

    if not text and not text_file:
        raise click.UsageError("Provide --text or --text-file.")
    content = Path(text_file).read_text(encoding="utf-8") if text_file else text
    audiobook.synthesize(content, out_path, voice_id=voice, rate=rate)
    click.echo(f"Synthesized -> {out_path}")


@cli.command("audiobook")
@click.argument("text_file", type=click.Path(exists=True))
@click.argument("out_path")
@click.option("--voice", default=None, help="Voice id from list-voices")
@click.option("--rate", default=None, type=int, help="Speech rate (words per minute)")
@click.option("--pause", default=1.0, help="Silence in seconds between chapters (blank-line-separated)")
def audiobook_cmd(text_file: str, out_path: str, voice: str | None, rate: int | None, pause: float) -> None:
    """Create an audiobook from TEXT_FILE (paragraphs split on blank lines become chapters)."""
    from . import audiobook

    content = Path(text_file).read_text(encoding="utf-8")
    audiobook.create_audiobook(content, out_path, voice_id=voice, rate=rate, pause_s=pause)
    click.echo(f"Audiobook -> {out_path}")


@cli.group()
def batch() -> None:
    """Run an operation across many files, continuing past per-file errors."""


def _echo_progress(result) -> None:
    status = "OK" if result.success else f"FAILED: {result.error}"
    click.echo(f"  {result.label}: {status}")


@batch.command("convert")
@click.argument("in_paths", nargs=-1, required=True)
@click.option("-o", "--out-dir", required=True, help="Directory to write converted files into")
@click.option("--format", "fmt", required=True, help="Output format, e.g. mp3")
@click.option("--bitrate", default=None, help="Output bitrate, e.g. 192k")
def batch_convert_cmd(in_paths: tuple[str, ...], out_dir: str, fmt: str, bitrate: str | None) -> None:
    """Convert every IN_PATH to --format in --out-dir."""
    result = batch_module.batch_convert(list(in_paths), out_dir, fmt, bitrate=bitrate, on_progress=_echo_progress)
    click.echo(f"{len(result.succeeded)}/{len(result.results)} succeeded")
    if result.failed:
        sys.exit(1)


@batch.command("effect")
@click.argument("effect_name")
@click.argument("in_paths", nargs=-1, required=True)
@click.option("-o", "--out-dir", required=True, help="Directory to write processed files into")
@click.option("--duration", default=1000, help="Fade duration in ms (fade-in/fade-out only)")
@click.option("--delay", default=500, help="Echo delay in ms (echo only)")
@click.option("--decay", default=0.5, help="Echo/reverb decay 0-1 (echo/reverb only)")
def batch_effect_cmd(
    effect_name: str, in_paths: tuple[str, ...], out_dir: str, duration: int, delay: int, decay: float
) -> None:
    """Apply EFFECT_NAME to every IN_PATH, writing results into --out-dir."""
    result = batch_module.batch_effect(
        effect_name,
        list(in_paths),
        out_dir,
        on_progress=_echo_progress,
        duration=duration,
        delay=delay,
        decay=decay,
    )
    click.echo(f"{len(result.succeeded)}/{len(result.results)} succeeded")
    if result.failed:
        sys.exit(1)


@cli.command()
@click.argument("in_path")
@click.argument("out_path")
@click.option(
    "--gain",
    "gains",
    multiple=True,
    required=True,
    help="FREQ:GAIN_DB pair, repeatable, e.g. --gain 60:4 --gain 1000:-2. Bands: "
    + ", ".join(str(b) for b in equalizer.BANDS),
)
def eq(in_path: str, out_path: str, gains: tuple[str, ...]) -> None:
    """Apply a graphic equalizer to IN_PATH using one or more FREQ:GAIN_DB bands."""
    gain_map = {}
    for pair in gains:
        try:
            freq_str, gain_str = pair.split(":")
            gain_map[float(freq_str)] = float(gain_str)
        except ValueError as exc:
            raise click.UsageError(f"Invalid --gain '{pair}'. Use FREQ:GAIN_DB, e.g. 1000:-2.") from exc
    equalizer.apply_eq(in_path, out_path, gain_map)
    click.echo(f"Equalized -> {out_path}")


@cli.command("bass-treble")
@click.argument("in_path")
@click.argument("out_path")
@click.option("--bass", "bass_db", default=0.0, help="Bass gain in dB")
@click.option("--treble", "treble_db", default=0.0, help="Treble gain in dB")
def bass_treble(in_path: str, out_path: str, bass_db: float, treble_db: float) -> None:
    """Apply simple bass/treble shelving to IN_PATH."""
    equalizer.apply_bass_treble(in_path, out_path, bass_db=bass_db, treble_db=treble_db)
    click.echo(f"Applied bass/treble -> {out_path}")


def main() -> None:
    try:
        cli()
    except core.AudioToolError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
