"""Effects with their own DSP: convolution reverb and spectral noise reduction."""
from __future__ import annotations

import numpy as np

from ..timefmt import db_to_amp
from . import dsp
from .base import CHUNK, Ctx, Effect, Param, register


@register
class Reverb(Effect):
    id = "reverb"
    name = "Reverb"
    category = "Space"
    description = "Places the sound in a room: from a small studio to a cathedral."
    keywords = "room hall cathedral plate ambience space"
    params = (
        Param("decay", "Decay time", "float", 1.8, 0.2, 10.0, 0.1, 1, "s"),
        Param("predelay", "Pre-delay", "float", 20.0, 0.0, 250.0, 1.0, 0, "ms"),
        Param("damping", "High-frequency damping", "float", 50.0, 0.0, 100.0, 1.0, 0, "%"),
        Param("width", "Stereo width", "float", 100.0, 0.0, 100.0, 1.0, 0, "%"),
        Param("wet", "Reverb level", "float", -9.0, -40.0, 6.0, 0.5, 1, "dB"),
        Param("dry", "Original level", "float", 0.0, -60.0, 0.0, 0.5, 1, "dB"),
        Param("tail", "Let the reverb ring out past the end", "bool", False),
    )
    presets = {
        "Small room": {"decay": 0.6, "predelay": 5.0, "damping": 60.0, "wet": -10.0},
        "Studio": {"decay": 1.0, "predelay": 10.0, "damping": 55.0, "wet": -9.0},
        "Hall": {"decay": 2.4, "predelay": 25.0, "damping": 45.0, "wet": -8.0},
        "Cathedral": {"decay": 5.5, "predelay": 40.0, "damping": 35.0, "wet": -7.0},
        "Bright plate": {"decay": 1.6, "predelay": 0.0, "damping": 10.0, "wet": -10.0},
    }

    def preserves_length(self, p: dict) -> bool:
        return not p["tail"]

    def out_frames(self, frames: int, sr: int, p: dict) -> int:
        if not p["tail"]:
            return frames
        return frames + dsp.reverb_ir_length(sr, p["decay"], p["predelay"]) - 1

    def process(self, ctx: Ctx) -> None:
        p = ctx.params
        ir = dsp.make_reverb_ir(ctx.sr, ctx.channels, p["decay"], p["predelay"],
                                p["damping"] / 100.0, p["width"] / 100.0)
        conv = dsp.OlaConvolver(ir, CHUNK)
        wet = np.float32(db_to_amp(p["wet"]))
        dry = np.float32(db_to_amp(p["dry"]))
        for chunk in ctx.chunks():
            ctx.write(chunk * dry + conv.process(chunk) * wet)
        if p["tail"]:
            ctx.write(conv.flush() * wet)


@register
class NoiseReduction(Effect):
    id = "noise_reduction"
    name = "Noise Reduction"
    category = "Restoration"
    description = ("Removes steady background noise such as hiss, hum or room tone. For best results "
                   "select a stretch of pure noise and capture it as the noise sample.")
    keywords = "denoise hiss hum clean spectral gate profile"
    params = (
        Param("reduction", "Noise reduction", "float", 15.0, 3.0, 60.0, 0.5, 1, "dB"),
        Param("sensitivity", "Sensitivity", "float", 5.0, 1.0, 10.0, 0.1, 1),
        Param("smoothing", "Smoothing", "float", 50.0, 0.0, 100.0, 1.0, 0, "%",
              help="Higher values sound smoother but can blur fast sounds."),
        Param("profile", "Noise profile", "choice", "auto",
              choices=(("auto", "Estimate from the quietest parts"),
                       ("sample", "Use the captured noise sample"))),
    )
    editor = "noise_profile"

    _BLOCK = 1 << 20
    _OVERLAP = 2 * dsp.N_FFT

    def process(self, ctx: Ctx) -> None:
        p = ctx.params
        ch = ctx.channels
        noise = ctx.extra.get("noise") if p["profile"] == "sample" else None
        if noise is not None and len(noise) > 4 * dsp.N_FFT:
            profiles = [dsp.noise_power(noise[:, c]) for c in range(ch)]
        else:
            probe = ctx.read_full(0, min(ctx.span, 90 * ctx.sr))
            profiles = [dsp.estimate_noise_power(probe[:, c]) for c in range(ch)]
        alpha = 0.6 + 0.35 * p["sensitivity"]
        smoothing = p["smoothing"] / 100.0
        for start in range(0, ctx.frames, self._BLOCK):
            ctx.check()
            lo = max(0, start - self._OVERLAP)
            hi = min(ctx.frames, start + self._BLOCK + self._OVERLAP)
            block = ctx.read(lo, hi)
            out = np.empty_like(block)
            for c in range(ch):
                out[:, c] = dsp.reduce_noise(block[:, c], profiles[c], p["reduction"], alpha, smoothing)
            keep = min(self._BLOCK, ctx.frames - start)
            ctx.write(out[start - lo: start - lo + keep])
            ctx.report((start + keep) / max(1, ctx.frames))
