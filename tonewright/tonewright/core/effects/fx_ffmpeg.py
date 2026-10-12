"""Effects that run through ffmpeg's audio filters."""
from __future__ import annotations

import json
import re

from .. import ffmpeg as ff
from ..timefmt import db_to_amp
from .base import Ctx, Effect, Param, register


def _n(x: float) -> str:
    return f"{x:.6g}"


def _max_freq(sr: int) -> float:
    return sr * 0.47


class FilterEffect(Effect):
    """An effect that is simply an ffmpeg -af chain."""

    def chain(self, p: dict, sr: int, channels: int) -> str:
        raise NotImplementedError

    def process(self, ctx: Ctx) -> None:
        ctx.ffmpeg(self.chain(ctx.params, ctx.sr, ctx.channels))


# ------------------------------------------------------------------ time & pitch
def _atempo_chain(factor: float) -> str:
    parts = []
    while factor < 0.5:
        parts.append("atempo=0.5")
        factor /= 0.5
    while factor > 2.0:
        parts.append("atempo=2.0")
        factor /= 2.0
    parts.append(f"atempo={factor:.6f}")
    return ",".join(parts)


@register
class Tempo(FilterEffect):
    id = "tempo"
    name = "Tempo Change"
    category = "Time & Pitch"
    description = ("Speeds the audio up or slows it down. In tempo mode the pitch stays where it is; "
                   "in speed mode the pitch moves with the speed, like a tape.")
    keywords = "speed faster slower stretch duration"
    length_preserving = False
    params = (
        Param("percent", "Change", "float", 10.0, -50.0, 200.0, 0.1, 1, "%",
              help="Positive is faster, negative is slower."),
        Param("mode", "Mode", "choice", "tempo",
              choices=(("tempo", "Keep the pitch"), ("speed", "Change the pitch too"))),
        Param("character", "Sound character", "choice", "mixed",
              choices=(("mixed", "Balanced"), ("crisp", "Crisp (speech, drums)"),
                       ("smooth", "Smooth (sustained music)")), show_if=("mode", "tempo")),
    )
    presets = {"10% faster": {"percent": 10.0}, "10% slower": {"percent": -10.0},
               "Double speed": {"percent": 100.0}, "Half speed": {"percent": -50.0}}

    @staticmethod
    def factor(p: dict) -> float:
        return 1.0 + p["percent"] / 100.0

    def out_frames(self, frames: int, sr: int, p: dict) -> int:
        return max(1, int(round(frames / self.factor(p))))

    def chain(self, p: dict, sr: int, channels: int) -> str:
        f = self.factor(p)
        if abs(f - 1.0) < 1e-9:
            return "anull"
        if p["mode"] == "speed":
            return f"asetrate={int(round(sr * f))},aresample={sr}"
        if ff.has_filter("rubberband"):
            return f"rubberband=tempo={_n(f)}:transients={p['character']}:pitchq=quality:channels=together"
        return _atempo_chain(f)


@register
class Pitch(FilterEffect):
    id = "pitch"
    name = "Pitch Shift"
    category = "Time & Pitch"
    description = "Raises or lowers the pitch without changing the duration."
    keywords = "key transpose semitone higher lower"
    params = (
        Param("semitones", "Shift", "float", 2.0, -24.0, 24.0, 0.01, 2, "semitones"),
        Param("formants", "Keep voice character (preserve formants)", "bool", False),
    )
    presets = {"Up one tone": {"semitones": 2.0}, "Down one tone": {"semitones": -2.0},
               "Up an octave": {"semitones": 12.0}, "Down an octave": {"semitones": -12.0}}

    def chain(self, p: dict, sr: int, channels: int) -> str:
        ratio = 2.0 ** (p["semitones"] / 12.0)
        if abs(ratio - 1.0) < 1e-9:
            return "anull"
        if ff.has_filter("rubberband"):
            formant = "preserved" if p["formants"] else "shifted"
            return (f"rubberband=pitch={_n(ratio)}:formant={formant}:transients=mixed:"
                    f"pitchq=quality:channels=together")
        return f"asetrate={int(round(sr * ratio))},aresample={sr},{_atempo_chain(1.0 / ratio)}"


# ------------------------------------------------------------------ modulation
@register
class Chorus(FilterEffect):
    id = "chorus"
    name = "Chorus"
    category = "Modulation"
    description = "Thickens the sound by mixing in slightly delayed, gently wobbling copies of itself."
    keywords = "thicken doubling ensemble"
    params = (
        Param("voices", "Voices", "int", 3, 1, 4, 1, 0),
        Param("delay", "Delay", "float", 40.0, 10.0, 100.0, 1.0, 0, "ms"),
        Param("depth", "Depth", "float", 2.0, 0.1, 8.0, 0.1, 1, "ms"),
        Param("speed", "Speed", "float", 0.5, 0.1, 5.0, 0.05, 2, "Hz"),
        Param("level", "Chorus level", "float", 40.0, 10.0, 90.0, 1.0, 0, "%"),
    )
    presets = {"Classic": {"voices": 3, "delay": 40.0, "depth": 2.0, "speed": 0.5, "level": 40.0},
               "Subtle": {"voices": 2, "delay": 30.0, "depth": 1.0, "speed": 0.3, "level": 25.0},
               "Wide ensemble": {"voices": 4, "delay": 55.0, "depth": 3.5, "speed": 0.8, "level": 50.0}}

    def chain(self, p: dict, sr: int, channels: int) -> str:
        n = p["voices"]
        delays = "|".join(_n(min(90.0, p["delay"] * (1 + 0.18 * i))) for i in range(n))
        decays = "|".join(_n(max(0.05, p["level"] / 100.0 * (1 - 0.12 * i))) for i in range(n))
        speeds = "|".join(_n(p["speed"] * (1 + 0.27 * i)) for i in range(n))
        depths = "|".join(_n(min(10.0, p["depth"] * (1 + 0.2 * i))) for i in range(n))
        return f"chorus=0.7:0.9:{delays}:{decays}:{speeds}:{depths}"


@register
class Flanger(FilterEffect):
    id = "flanger"
    name = "Flanger"
    category = "Modulation"
    description = "A swooshing, jet-plane effect made by sweeping a very short delay."
    keywords = "swoosh jet sweep"
    params = (
        Param("delay", "Base delay", "float", 2.0, 0.0, 30.0, 0.1, 1, "ms"),
        Param("depth", "Sweep depth", "float", 2.0, 0.0, 10.0, 0.1, 1, "ms"),
        Param("regen", "Feedback", "float", 0.0, -95.0, 95.0, 1.0, 0, "%"),
        Param("width", "Wet amount", "float", 71.0, 0.0, 100.0, 1.0, 0, "%"),
        Param("speed", "Speed", "float", 0.5, 0.1, 10.0, 0.05, 2, "Hz"),
        Param("shape", "Sweep shape", "choice", "sinusoidal",
              choices=(("sinusoidal", "Sine"), ("triangular", "Triangle"))),
        Param("phase", "Stereo phase", "float", 25.0, 0.0, 100.0, 1.0, 0, "%"),
    )

    def chain(self, p: dict, sr: int, channels: int) -> str:
        return (f"flanger=delay={_n(p['delay'])}:depth={_n(p['depth'])}:regen={_n(p['regen'])}:"
                f"width={_n(p['width'])}:speed={_n(p['speed'])}:shape={p['shape']}:phase={_n(p['phase'])}")


@register
class Phaser(FilterEffect):
    id = "phaser"
    name = "Phaser"
    category = "Modulation"
    description = "A sweeping, hollow whoosh created by moving notches through the spectrum."
    keywords = "sweep phase whoosh"
    params = (
        Param("speed", "Speed", "float", 0.5, 0.1, 2.0, 0.05, 2, "Hz"),
        Param("decay", "Intensity", "float", 0.4, 0.1, 0.99, 0.01, 2),
        Param("delay", "Delay", "float", 3.0, 0.0, 5.0, 0.1, 1, "ms"),
        Param("type", "Sweep shape", "choice", "triangular",
              choices=(("triangular", "Triangle"), ("sinusoidal", "Sine"))),
    )

    def chain(self, p: dict, sr: int, channels: int) -> str:
        return (f"aphaser=in_gain=0.6:out_gain=0.8:delay={_n(p['delay'])}:decay={_n(p['decay'])}:"
                f"speed={_n(p['speed'])}:type={p['type']}")


@register
class Vibrato(FilterEffect):
    id = "vibrato"
    name = "Vibrato"
    category = "Modulation"
    description = "Makes the pitch waver up and down."
    keywords = "wobble pitch modulation"
    params = (
        Param("freq", "Speed", "float", 5.0, 0.1, 20.0, 0.1, 1, "Hz"),
        Param("depth", "Depth", "float", 40.0, 0.0, 100.0, 1.0, 0, "%"),
    )

    def chain(self, p: dict, sr: int, channels: int) -> str:
        return f"vibrato=f={_n(p['freq'])}:d={_n(p['depth'] / 100.0)}"


@register
class Tremolo(FilterEffect):
    id = "tremolo"
    name = "Tremolo"
    category = "Modulation"
    description = "Makes the volume pulse up and down."
    keywords = "pulse volume modulation"
    params = (
        Param("freq", "Speed", "float", 5.0, 0.1, 20.0, 0.1, 1, "Hz"),
        Param("depth", "Depth", "float", 50.0, 0.0, 100.0, 1.0, 0, "%"),
    )

    def chain(self, p: dict, sr: int, channels: int) -> str:
        return f"tremolo=f={_n(p['freq'])}:d={_n(p['depth'] / 100.0)}"


# ------------------------------------------------------------------ space
@register
class Echo(FilterEffect):
    id = "echo"
    name = "Echo"
    category = "Space"
    description = "Repeats the sound after a short delay, each repeat quieter than the last."
    keywords = "delay repeat slapback"
    params = (
        Param("delay", "Delay", "float", 350.0, 10.0, 3000.0, 1.0, 0, "ms"),
        Param("decay", "Each repeat is", "float", 45.0, 5.0, 95.0, 1.0, 0, "% of the last"),
        Param("repeats", "Repeats", "int", 3, 1, 12, 1, 0),
        Param("tail", "Let the echoes ring out past the end", "bool", False),
    )
    presets = {"Slapback": {"delay": 90.0, "decay": 35.0, "repeats": 1},
               "Room": {"delay": 220.0, "decay": 40.0, "repeats": 3},
               "Canyon": {"delay": 700.0, "decay": 55.0, "repeats": 5}}

    def preserves_length(self, p: dict) -> bool:
        return not p["tail"]

    def out_frames(self, frames: int, sr: int, p: dict) -> int:
        return frames + (int(sr * p["delay"] * p["repeats"] / 1000.0) if p["tail"] else 0)

    def chain(self, p: dict, sr: int, channels: int) -> str:
        reps = range(1, p["repeats"] + 1)
        delays = "|".join(_n(p["delay"] * i) for i in reps)
        decays = "|".join(_n((p["decay"] / 100.0) ** i) for i in reps)
        chain = f"aecho=1:1:{delays}:{decays}"
        if p["tail"]:
            chain += f",apad=pad_dur={_n(p['delay'] * p['repeats'] / 1000.0)}"
        return chain


# ------------------------------------------------------------------ equalizer & filters
GRAPHIC_BANDS = ((31, "b31", "31"), (62, "b62", "62"), (125, "b125", "125"), (250, "b250", "250"),
                 (500, "b500", "500"), (1000, "b1k", "1k"), (2000, "b2k", "2k"), (4000, "b4k", "4k"),
                 (8000, "b8k", "8k"), (16000, "b16k", "16k"))


def _band_preset(*gains: float) -> dict:
    return {key: g for (_, key, _), g in zip(GRAPHIC_BANDS, gains)}


@register
class GraphicEQ(FilterEffect):
    id = "graphic_eq"
    name = "Graphic Equalizer"
    category = "Equalizer & Filters"
    description = "Ten sliders, one per octave from 31 Hz to 16 kHz, to shape the overall tone."
    keywords = "eq bands bass treble tone"
    params = tuple(Param(key, f"{label} Hz", "float", 0.0, -15.0, 15.0, 0.5, 1, "dB")
                   for _, key, label in GRAPHIC_BANDS) + (
        Param("output", "Output gain", "float", 0.0, -24.0, 24.0, 0.1, 1, "dB"),)
    presets = {
        "Flat": _band_preset(0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        "Bass boost": _band_preset(6, 5, 4, 2, 0, 0, 0, 0, 0, 0),
        "Treble boost": _band_preset(0, 0, 0, 0, 0, 0, 2, 4, 5, 6),
        "Vocal presence": _band_preset(-3, -2, 0, 0, 1, 3, 4, 3, 0, -2),
        "Loudness": _band_preset(5, 4, 2, 0, -1, -1, 0, 2, 4, 5),
        "Telephone": _band_preset(-15, -15, -15, -6, 3, 4, 4, -4, -15, -15),
        "Speech clarity": _band_preset(-6, -4, -2, 0, 0, 2, 3, 3, 0, -2),
    }
    editor = "graphic_eq"

    def chain(self, p: dict, sr: int, channels: int) -> str:
        parts = []
        for freq, key, _ in GRAPHIC_BANDS:
            if abs(p[key]) > 1e-6 and freq < _max_freq(sr):
                parts.append(f"equalizer=f={freq}:t=o:w=1:g={_n(p[key])}")
        if abs(p["output"]) > 1e-6:
            parts.append(f"volume={_n(p['output'])}dB")
        return ",".join(parts) or "anull"


@register
class ParametricEQ(FilterEffect):
    id = "parametric_eq"
    name = "Parametric Equalizer"
    category = "Equalizer & Filters"
    description = "Four fully adjustable bell filters: choose the frequency, the width and how much to boost or cut."
    keywords = "eq peak notch band"
    _defaults = ((120.0, "Low"), (500.0, "Low-mid"), (2500.0, "High-mid"), (8000.0, "High"))
    params = tuple(p for i, (f, name) in enumerate(_defaults, start=1) for p in (
        Param(f"f{i}", "Frequency", "float", f, 20.0, 20000.0, 1.0, 0, "Hz", log=True, group=f"Band {i} - {name}"),
        Param(f"g{i}", "Gain", "float", 0.0, -24.0, 24.0, 0.1, 1, "dB", group=f"Band {i} - {name}"),
        Param(f"q{i}", "Width (Q)", "float", 1.0, 0.1, 10.0, 0.05, 2, "", group=f"Band {i} - {name}"),
    ))

    def chain(self, p: dict, sr: int, channels: int) -> str:
        parts = []
        for i in range(1, 5):
            if abs(p[f"g{i}"]) > 1e-6 and p[f"f{i}"] < _max_freq(sr):
                parts.append(f"equalizer=f={_n(p[f'f{i}'])}:t=q:w={_n(p[f'q{i}'])}:g={_n(p[f'g{i}'])}")
        return ",".join(parts) or "anull"


@register
class Filter(FilterEffect):
    id = "filter"
    name = "Low / High / Band Filter"
    category = "Equalizer & Filters"
    description = "Removes everything below, above, inside or outside a frequency range."
    keywords = "lowpass highpass bandpass notch cut rumble hiss"
    params = (
        Param("type", "Filter type", "choice", "highpass", choices=(
            ("highpass", "High-pass (remove lows)"), ("lowpass", "Low-pass (remove highs)"),
            ("bandpass", "Band-pass (keep a band)"), ("bandreject", "Band-stop (remove a band)"))),
        Param("freq", "Frequency", "float", 80.0, 20.0, 20000.0, 1.0, 0, "Hz", log=True),
        Param("bandwidth", "Band width", "float", 500.0, 10.0, 10000.0, 1.0, 0, "Hz",
              log=True, show_if=("type", ("bandpass", "bandreject"))),
        Param("slope", "Steepness", "choice", 12,
              choices=((12, "12 dB / octave"), (24, "24 dB / octave"), (36, "36 dB / octave"), (48, "48 dB / octave"))),
    )
    presets = {"Remove rumble": {"type": "highpass", "freq": 80.0, "slope": 24},
               "Remove hiss": {"type": "lowpass", "freq": 9000.0, "slope": 24},
               "Telephone voice": {"type": "bandpass", "freq": 1800.0, "bandwidth": 3000.0, "slope": 24}}

    def chain(self, p: dict, sr: int, channels: int) -> str:
        freq = min(p["freq"], _max_freq(sr))
        stages = max(1, int(p["slope"]) // 12)
        if p["type"] in ("bandpass", "bandreject"):
            one = f"{p['type']}=f={_n(freq)}:width_type=h:w={_n(p['bandwidth'])}"
        else:
            one = f"{p['type']}=f={_n(freq)}:p=2"
        return ",".join([one] * stages)


@register
class ToneControl(FilterEffect):
    id = "tone"
    name = "Bass & Treble"
    category = "Equalizer & Filters"
    description = "Simple tone control: boost or cut the low end and the high end."
    keywords = "bass treble shelf tone"
    params = (
        Param("bass", "Bass", "float", 3.0, -20.0, 20.0, 0.5, 1, "dB"),
        Param("bass_freq", "Bass turnover", "float", 120.0, 40.0, 400.0, 1.0, 0, "Hz", log=True),
        Param("treble", "Treble", "float", 0.0, -20.0, 20.0, 0.5, 1, "dB"),
        Param("treble_freq", "Treble turnover", "float", 4000.0, 1000.0, 12000.0, 10.0, 0, "Hz", log=True),
    )

    def chain(self, p: dict, sr: int, channels: int) -> str:
        parts = []
        if abs(p["bass"]) > 1e-6:
            parts.append(f"bass=g={_n(p['bass'])}:f={_n(p['bass_freq'])}")
        if abs(p["treble"]) > 1e-6:
            parts.append(f"treble=g={_n(p['treble'])}:f={_n(min(p['treble_freq'], _max_freq(sr)))}")
        return ",".join(parts) or "anull"


# ------------------------------------------------------------------ dynamics
@register
class Compressor(FilterEffect):
    id = "compressor"
    name = "Compressor"
    category = "Dynamics"
    description = "Evens out the volume by turning down the loud parts, then lifting the whole sound back up."
    keywords = "dynamics leveler squash punch"
    params = (
        Param("threshold", "Threshold", "float", -18.0, -60.0, 0.0, 0.5, 1, "dB"),
        Param("ratio", "Ratio", "float", 3.0, 1.0, 20.0, 0.1, 1, ": 1"),
        Param("attack", "Attack", "float", 20.0, 0.1, 500.0, 0.1, 1, "ms", log=True),
        Param("release", "Release", "float", 250.0, 5.0, 3000.0, 1.0, 0, "ms", log=True),
        Param("knee", "Knee", "float", 6.0, 0.0, 18.0, 0.5, 1, "dB"),
        Param("makeup", "Make-up gain", "float", 3.0, 0.0, 30.0, 0.5, 1, "dB"),
    )
    presets = {"Gentle": {"threshold": -18.0, "ratio": 2.0, "attack": 25.0, "release": 300.0, "makeup": 3.0},
               "Voice": {"threshold": -20.0, "ratio": 4.0, "attack": 10.0, "release": 150.0, "makeup": 6.0},
               "Heavy": {"threshold": -26.0, "ratio": 8.0, "attack": 5.0, "release": 100.0, "makeup": 9.0}}
    editor = "compressor"

    def chain(self, p: dict, sr: int, channels: int) -> str:
        thr = max(0.000976563, min(1.0, db_to_amp(p["threshold"])))
        knee = max(1.0, min(8.0, db_to_amp(p["knee"])))
        makeup = max(1.0, min(64.0, db_to_amp(p["makeup"])))
        return (f"acompressor=threshold={_n(thr)}:ratio={_n(p['ratio'])}:attack={_n(p['attack'])}:"
                f"release={_n(p['release'])}:makeup={_n(makeup)}:knee={_n(knee)}:link=average:detection=rms")


@register
class Limiter(FilterEffect):
    id = "limiter"
    name = "Limiter"
    category = "Dynamics"
    description = "A brick wall: nothing gets louder than the ceiling, with no clipping distortion."
    keywords = "ceiling peak clip loud"
    params = (
        Param("ceiling", "Ceiling", "float", -1.0, -24.0, 0.0, 0.1, 1, "dB"),
        Param("attack", "Look-ahead", "float", 5.0, 0.1, 80.0, 0.1, 1, "ms"),
        Param("release", "Release", "float", 50.0, 1.0, 1000.0, 1.0, 0, "ms"),
    )

    def chain(self, p: dict, sr: int, channels: int) -> str:
        limit = max(0.0625, min(1.0, db_to_amp(p["ceiling"])))
        return (f"alimiter=limit={_n(limit)}:attack={_n(p['attack'])}:release={_n(p['release'])}:"
                f"level=0:latency=1")


@register
class NoiseGate(FilterEffect):
    id = "gate"
    name = "Noise Gate"
    category = "Dynamics"
    description = "Turns the sound down whenever it falls below the threshold, silencing hiss between phrases."
    keywords = "expander silence between hiss"
    params = (
        Param("threshold", "Threshold", "float", -45.0, -80.0, 0.0, 0.5, 1, "dB"),
        Param("reduction", "Reduction when closed", "float", 24.0, 3.0, 80.0, 1.0, 0, "dB"),
        Param("attack", "Attack", "float", 10.0, 0.5, 500.0, 0.5, 1, "ms", log=True),
        Param("release", "Release", "float", 200.0, 5.0, 3000.0, 1.0, 0, "ms", log=True),
    )

    def chain(self, p: dict, sr: int, channels: int) -> str:
        thr = max(0.0, min(1.0, db_to_amp(p["threshold"])))
        rng = max(0.0, min(1.0, db_to_amp(-p["reduction"])))
        return (f"agate=threshold={_n(thr)}:range={_n(rng)}:ratio=4:attack={_n(p['attack'])}:"
                f"release={_n(p['release'])}")


# ------------------------------------------------------------------ restoration
@register
class FFTDenoise(FilterEffect):
    id = "denoise_fft"
    name = "Quick Denoise"
    category = "Restoration"
    description = "Reduces steady background noise (hiss, fan, hum) with an adaptive FFT denoiser."
    keywords = "noise hiss reduce clean"
    params = (
        Param("reduction", "Noise reduction", "float", 12.0, 1.0, 60.0, 0.5, 1, "dB"),
        Param("floor", "Noise floor", "float", -50.0, -80.0, -20.0, 1.0, 0, "dB"),
        Param("track", "Follow changes in the noise", "bool", True),
    )

    def chain(self, p: dict, sr: int, channels: int) -> str:
        return (f"afftdn=nr={_n(p['reduction'])}:nf={_n(p['floor'])}:nt=w:"
                f"tn={1 if p['track'] else 0}")


@register
class ClickRepair(FilterEffect):
    id = "declick"
    name = "Click & Clip Repair"
    category = "Restoration"
    description = "Removes crackle and pops (vinyl, bad edits) or repairs clipped peaks."
    keywords = "pop crackle vinyl declip click"
    params = (
        Param("mode", "Repair", "choice", "click",
              choices=(("click", "Clicks and pops"), ("clip", "Clipped peaks"))),
        Param("threshold", "Sensitivity (lower finds more)", "float", 2.0, 1.0, 20.0, 0.5, 1),
        Param("burst", "Merge nearby clicks", "float", 2.0, 0.0, 10.0, 0.5, 1, show_if=("mode", "click")),
    )

    def chain(self, p: dict, sr: int, channels: int) -> str:
        if p["mode"] == "clip":
            return f"adeclip=w=55:o=75:a=8:t={_n(p['threshold'] * 5)}:n=1000"
        return f"adeclick=w=55:o=75:a=2:t={_n(p['threshold'])}:b={_n(p['burst'])}"


@register
class HumRemoval(FilterEffect):
    id = "hum"
    name = "Hum Removal"
    category = "Restoration"
    description = "Notches out mains hum (50 or 60 Hz) and its harmonics."
    keywords = "mains buzz 50hz 60hz"
    params = (
        Param("base", "Mains frequency", "choice", 50, choices=((50, "50 Hz (Europe, Asia)"), (60, "60 Hz (Americas)"))),
        Param("harmonics", "Harmonics to remove", "int", 4, 1, 8, 1, 0),
        Param("width", "Notch sharpness (Q)", "float", 30.0, 5.0, 100.0, 1.0, 0),
    )

    def chain(self, p: dict, sr: int, channels: int) -> str:
        notches = []
        for i in range(1, p["harmonics"] + 1):
            f = p["base"] * i
            if f < _max_freq(sr):
                notches.append(f"bandreject=f={f}:width_type=q:w={_n(p['width'])}")
        return ",".join(notches) or "anull"


@register
class VocalRemover(FilterEffect):
    id = "vocal_remover"
    name = "Vocal Remover"
    category = "Channels"
    description = "Cancels whatever is panned dead centre (usually the lead vocal) in a stereo mix."
    keywords = "karaoke centre center vocals instrumental"
    min_channels = 2
    params = (Param("mode", "Mode", "choice", "remove", choices=(
        ("remove", "Remove centre (karaoke)"), ("isolate", "Keep only the centre"))),)

    def chain(self, p: dict, sr: int, channels: int) -> str:
        sign = "-" if p["mode"] == "remove" else "+"
        side = f"0.5*c0{sign}0.5*c1"
        return f"pan=stereo|c0={side}|c1={side}"


# ------------------------------------------------------------------ loudness
@register
class LoudnessNormalize(Effect):
    id = "loudness"
    name = "Loudness Normalize (LUFS)"
    category = "Volume"
    description = ("Matches the perceived loudness to a standard target (EBU R128), so tracks "
                   "sound equally loud. Two passes: measure, then correct.")
    keywords = "lufs ebu r128 podcast streaming loud"
    length_preserving = True
    params = (
        Param("target", "Target loudness", "float", -16.0, -30.0, -5.0, 0.5, 1, "LUFS"),
        Param("peak", "True-peak ceiling", "float", -1.5, -9.0, 0.0, 0.1, 1, "dBTP"),
        Param("range", "Loudness range", "float", 11.0, 1.0, 30.0, 0.5, 1, "LU"),
    )
    presets = {"Podcast (-16 LUFS)": {"target": -16.0}, "Streaming (-14 LUFS)": {"target": -14.0},
               "Broadcast (-23 LUFS)": {"target": -23.0}}

    def process(self, ctx: Ctx) -> None:
        p = ctx.params
        base = f"loudnorm=I={_n(p['target'])}:TP={_n(p['peak'])}:LRA={_n(p['range'])}"
        report = ff.analyze_stream(ctx.full_chunks(0.0, 0.45), ctx.sr, ctx.channels,
                                   base + ":print_format=json", cancel=ctx.cancel)
        m = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", report, flags=re.S)
        chain = base
        if m:
            try:
                data = json.loads(m[-1])
                if all(data[k] not in ("-inf", "inf") for k in ("input_i", "input_tp", "input_lra", "input_thresh")):
                    chain += (f":measured_I={data['input_i']}:measured_TP={data['input_tp']}:"
                              f"measured_LRA={data['input_lra']}:measured_thresh={data['input_thresh']}:"
                              f"offset={data['target_offset']}:linear=true")
            except (ValueError, KeyError):
                pass
        ctx.ffmpeg(f"{chain},aresample={ctx.sr}", lo=0.5, hi=1.0)


# ------------------------------------------------------------------ advanced
@register
class CustomFilter(FilterEffect):
    id = "custom_filter"
    name = "Custom FFmpeg Filter"
    category = "Advanced"
    description = ("Type any ffmpeg audio filter chain, for example  lowpass=f=3000,volume=2  or  "
                   "aecho=0.8:0.9:1000:0.3 . The closest thing to a plug-in slot.")
    keywords = "ffmpeg plugin vst dx af filtergraph"
    length_preserving = False
    params = (Param("chain", "Filter chain", "text", "volume=1.0"),)

    def chain(self, p: dict, sr: int, channels: int) -> str:
        return p["chain"].strip() or "anull"
