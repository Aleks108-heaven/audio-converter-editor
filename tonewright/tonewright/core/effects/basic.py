"""Volume, fade and channel effects, implemented directly in numpy."""
from __future__ import annotations

import numpy as np

from ..timefmt import db_to_amp
from . import dsp
from .base import Ctx, Effect, Param, register


@register
class Amplify(Effect):
    id = "amplify"
    name = "Amplify"
    category = "Volume"
    description = "Makes the audio louder or quieter by a fixed amount."
    keywords = "gain volume louder quieter boost"
    params = (
        Param("gain", "Gain", "float", 6.0, -60.0, 60.0, 0.1, 1, "dB"),
        Param("prevent_clipping", "Prevent clipping", "bool", True,
              help="Never push the loudest peak above 0 dBFS."),
    )
    presets = {"+3 dB": {"gain": 3.0}, "+6 dB": {"gain": 6.0},
               "-3 dB": {"gain": -3.0}, "-6 dB": {"gain": -6.0}}

    def process(self, ctx: Ctx) -> None:
        gain = db_to_amp(ctx.params["gain"])
        start = 0.0
        if ctx.params["prevent_clipping"] and gain > 1.0:
            peak = float(ctx.stats(0.0, 0.5).peak.max())
            if peak * gain > 1.0:
                gain = max(1.0, 1.0 / peak)
            start = 0.5
        g = np.float32(gain)
        for chunk in ctx.chunks(start, 1.0):
            ctx.write(chunk * g)


@register
class Fade(Effect):
    id = "fade"
    name = "Fade In/Out"
    category = "Fades & Envelope"
    description = ("Changes the volume gradually across the selection: fade in from silence, "
                   "fade out to silence, or ramp between any two levels.")
    keywords = "fade in fade out ramp swell"
    params = (
        Param("direction", "Direction", "choice", "in", choices=(("in", "Fade in"), ("out", "Fade out"))),
        Param("shape", "Curve", "choice", "slow", choices=dsp.FADE_SHAPES),
        Param("start", "Level at start", "float", 0.0, 0.0, 100.0, 1.0, 0, "%"),
        Param("end", "Level at end", "float", 100.0, 0.0, 100.0, 1.0, 0, "%"),
    )
    presets = {
        "Fade in": {"direction": "in", "start": 0.0, "end": 100.0},
        "Fade out": {"direction": "out", "start": 100.0, "end": 0.0},
        "Gentle fade in": {"direction": "in", "shape": "smooth", "start": 0.0, "end": 100.0},
        "Gentle fade out": {"direction": "out", "shape": "smooth", "start": 100.0, "end": 0.0},
    }
    editor = "fade_curve"

    def process(self, ctx: Ctx) -> None:
        p = ctx.params
        s, e = p["start"] / 100.0, p["end"] / 100.0
        pos = 0
        for chunk in ctx.chunks():
            x = ctx.positions(pos, len(chunk))
            gain = dsp.fade_gain(x, p["shape"], p["direction"], s, e).astype(np.float32)
            ctx.write(chunk * gain[:, None])
            pos += len(chunk)


@register
class Envelope(Effect):
    id = "envelope"
    name = "Volume Envelope"
    category = "Fades & Envelope"
    description = "Draws the volume over time: click the line to add points and drag them up or down."
    keywords = "automation curve draw volume points"
    params = (Param("points", "Envelope", "points", ((0.0, 100.0), (1.0, 100.0)), 0.0, 200.0),)
    editor = "envelope"

    def process(self, ctx: Ctx) -> None:
        pts = np.asarray(ctx.params["points"], dtype=np.float64)
        px, py = pts[:, 0], pts[:, 1] / 100.0
        pos = 0
        for chunk in ctx.chunks():
            x = ctx.positions(pos, len(chunk))
            gain = np.interp(x, px, py).astype(np.float32)
            ctx.write(chunk * gain[:, None])
            pos += len(chunk)


@register
class Normalize(Effect):
    id = "normalize"
    name = "Normalize"
    category = "Volume"
    description = "Scales the audio so its loudest peak (or its average loudness) reaches a target level."
    keywords = "level loudness peak rms maximize"
    params = (
        Param("target", "Target level", "float", -1.0, -30.0, 0.0, 0.1, 1, "dB"),
        Param("mode", "Measure", "choice", "peak",
              choices=(("peak", "Peak level"), ("rms", "Average loudness (RMS)"))),
        Param("independent", "Normalize each channel separately", "bool", False),
        Param("remove_dc", "Remove DC offset first", "bool", False),
    )
    presets = {"Peak to -1 dB": {"target": -1.0, "mode": "peak"},
               "Peak to 0 dB": {"target": 0.0, "mode": "peak"},
               "Speech (RMS -20 dB)": {"target": -20.0, "mode": "rms"}}

    def process(self, ctx: Ctx) -> None:
        p = ctx.params
        st = ctx.stats(0.0, 0.5)
        mean = st.mean if p["remove_dc"] else np.zeros(ctx.channels)
        peak = np.maximum(st.max - mean, mean - st.min)
        independent = p["independent"]
        linked_peak = peak if independent else np.full(ctx.channels, peak.max())
        if p["mode"] == "peak":
            level = linked_peak
        else:
            var = np.maximum(st.sumsq / max(1, st.n) - mean ** 2, 0.0)
            level = np.sqrt(var if independent else np.full(ctx.channels, var.mean()))
        target = db_to_amp(p["target"])
        with np.errstate(divide="ignore", invalid="ignore"):
            gains = target / level
            if p["mode"] == "rms":
                gains = np.minimum(gains, 1.0 / linked_peak)   # average-level targets must not clip
        gains = np.where(np.isfinite(gains) & (level > 1e-9), gains, 1.0).astype(np.float32)
        offset = mean.astype(np.float32)
        for chunk in ctx.chunks(0.5, 1.0):
            ctx.write((chunk - offset) * gains)


@register
class RemoveDC(Effect):
    id = "dc_offset"
    name = "Remove DC Offset"
    category = "Restoration"
    description = "Centres the waveform on zero. A DC offset wastes headroom and can cause clicks at edits."
    keywords = "dc bias centre center"

    def process(self, ctx: Ctx) -> None:
        mean = ctx.stats(0.0, 0.5).mean.astype(np.float32)
        for chunk in ctx.chunks(0.5, 1.0):
            ctx.write(chunk - mean)


@register
class Invert(Effect):
    id = "invert"
    name = "Invert"
    category = "Volume"
    description = "Flips the waveform upside down (polarity). Useful for fixing out-of-phase recordings."
    keywords = "phase polarity flip"

    def process(self, ctx: Ctx) -> None:
        for chunk in ctx.chunks():
            ctx.write(-chunk)


@register
class Reverse(Effect):
    id = "reverse"
    name = "Reverse"
    category = "Time & Pitch"
    description = "Plays the selection backwards."
    keywords = "backwards"

    def process(self, ctx: Ctx) -> None:
        for chunk in ctx.chunks_reversed():
            ctx.write(chunk[::-1])


@register
class Balance(Effect):
    id = "balance"
    name = "Balance"
    category = "Channels"
    description = "Shifts a stereo sound towards the left or right speaker."
    keywords = "pan stereo left right"
    min_channels = 2
    params = (Param("balance", "Balance", "float", 0.0, -100.0, 100.0, 1.0, 0, "%",
                    help="Negative moves the sound left, positive moves it right."),)

    def process(self, ctx: Ctx) -> None:
        b = ctx.params["balance"] / 100.0
        gains = np.array([min(1.0, 1.0 - b), min(1.0, 1.0 + b)], np.float32)
        for chunk in ctx.chunks():
            ctx.write(chunk * gains)


@register
class ChannelMixer(Effect):
    id = "channel_mixer"
    name = "Channel Mixer"
    category = "Channels"
    description = "Swaps the stereo channels, copies one channel to both, or blends them to mono."
    keywords = "swap mono left right stereo mix"
    min_channels = 2
    params = (Param("mode", "Operation", "choice", "swap", choices=(
        ("swap", "Swap left and right"),
        ("left", "Copy left channel to both"),
        ("right", "Copy right channel to both"),
        ("mono", "Blend both channels (mono, still two channels)"),
        ("invert_left", "Invert left channel"),
        ("invert_right", "Invert right channel"),
    )),)

    def process(self, ctx: Ctx) -> None:
        mode = ctx.params["mode"]
        for chunk in ctx.chunks():
            if mode == "swap":
                out = chunk[:, ::-1]
            elif mode == "left":
                out = np.repeat(chunk[:, :1], 2, axis=1)
            elif mode == "right":
                out = np.repeat(chunk[:, 1:], 2, axis=1)
            elif mode == "mono":
                out = np.repeat(chunk.mean(axis=1, keepdims=True), 2, axis=1)
            elif mode == "invert_left":
                out = chunk * np.array([-1.0, 1.0], np.float32)
            else:
                out = chunk * np.array([1.0, -1.0], np.float32)
            ctx.write(out)
