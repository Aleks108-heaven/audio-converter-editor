"""Output formats: what each one supports and how to ask ffmpeg for it."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Optional

from ..i18n import tr


@dataclass(frozen=True)
class FormatSpec:
    key: str
    name: str
    ext: str
    codec: str
    muxer: str
    lossless: bool = False
    bitrates: tuple = ()                  # constant-bitrate choices, kbps
    default_bitrate: float = 0.0
    vbr_label: str = ""                   # empty when there is no variable-bitrate mode
    vbr_choices: tuple = ()               # ((value, label, approx_kbps), ...)
    default_vbr: float = 0.0
    sample_rates: tuple = (8000, 11025, 16000, 22050, 32000, 44100, 48000)
    max_channels: int = 2
    fixed_channels: int = 0
    fixed_sample_rate: int = 0
    bit_depths: tuple = ()
    default_depth: int = 16
    compression: bool = False             # FLAC compression level
    description: str = ""


_MP3_VBR = tuple((q, f"V{q}  (about {kb} kbps)", kb) for q, kb in
                 enumerate((245, 225, 190, 175, 165, 130, 115, 100, 85, 65)))
_OGG_VBR = tuple((q, f"Quality {q}  (about {kb} kbps)", kb) for q, kb in
                 zip(range(-1, 11), (45, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 500)))
_AAC_RATES = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000, 64000, 88200, 96000)

FORMATS: dict[str, FormatSpec] = {f.key: f for f in (
    FormatSpec("mp3", "MP3", ".mp3", "libmp3lame", "mp3",
               bitrates=(32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320), default_bitrate=192,
               vbr_label="Quality", vbr_choices=_MP3_VBR, default_vbr=2,
               sample_rates=(8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000),
               description="Plays everywhere. Best all-round choice for music."),
    FormatSpec("m4a", "M4A (AAC)", ".m4a", "aac", "ipod",
               bitrates=(48, 64, 96, 112, 128, 160, 192, 224, 256, 320), default_bitrate=192,
               sample_rates=_AAC_RATES, description="Efficient AAC in an MP4 container (Apple, Android, most players)."),
    FormatSpec("m4b", "M4B (Audiobook)", ".m4b", "aac", "ipod",
               bitrates=(24, 32, 48, 64, 96, 128, 160, 192), default_bitrate=64, sample_rates=_AAC_RATES,
               description="AAC audiobook with chapter support."),
    FormatSpec("aac", "AAC (ADTS)", ".aac", "aac", "adts",
               bitrates=(48, 64, 96, 112, 128, 160, 192, 224, 256, 320), default_bitrate=192,
               sample_rates=_AAC_RATES, description="Raw AAC stream without a container."),
    FormatSpec("ogg", "OGG Vorbis", ".ogg", "libvorbis", "ogg",
               bitrates=(48, 64, 96, 112, 128, 160, 192, 224, 256, 320), default_bitrate=160,
               vbr_label="Quality", vbr_choices=_OGG_VBR, default_vbr=5,
               sample_rates=(8000, 11025, 16000, 22050, 32000, 44100, 48000, 96000),
               description="Open, high-quality lossy format."),
    FormatSpec("opus", "Opus", ".opus", "libopus", "opus",
               bitrates=(24, 32, 48, 64, 96, 128, 160, 192, 256), default_bitrate=96,
               sample_rates=(8000, 12000, 16000, 24000, 48000),
               description="The most efficient lossy codec; great for speech and music."),
    FormatSpec("wma", "WMA", ".wma", "wmav2", "asf",
               bitrates=(32, 48, 64, 96, 128, 160, 192, 256, 320), default_bitrate=128,
               sample_rates=(8000, 11025, 16000, 22050, 32000, 44100, 48000), description="Windows Media Audio."),
    FormatSpec("flac", "FLAC", ".flac", "flac", "flac", lossless=True,
               sample_rates=(8000, 11025, 16000, 22050, 32000, 44100, 48000, 88200, 96000, 192000),
               bit_depths=(16, 24), default_depth=16, compression=True,
               description="Lossless: bit-perfect and about half the size of WAV."),
    FormatSpec("wav", "WAV", ".wav", "pcm_s16le", "wav", lossless=True,
               sample_rates=(8000, 11025, 16000, 22050, 32000, 44100, 48000, 88200, 96000, 192000),
               bit_depths=(8, 16, 24, 32), default_depth=16, description="Uncompressed PCM."),
    FormatSpec("aiff", "AIFF", ".aiff", "pcm_s16be", "aiff", lossless=True,
               sample_rates=(8000, 11025, 16000, 22050, 32000, 44100, 48000, 88200, 96000, 192000),
               bit_depths=(16, 24), default_depth=16, description="Uncompressed PCM (Apple)."),
    FormatSpec("mp2", "MP2", ".mp2", "mp2", "mp2",
               bitrates=(64, 96, 128, 160, 192, 224, 256, 320, 384), default_bitrate=192,
               sample_rates=(16000, 22050, 24000, 32000, 44100, 48000), description="MPEG Layer II (broadcast)."),
    FormatSpec("amr", "AMR (narrowband)", ".amr", "libopencore_amrnb", "amr",
               bitrates=(4.75, 5.15, 5.9, 6.7, 7.4, 7.95, 10.2, 12.2), default_bitrate=12.2,
               sample_rates=(8000,), max_channels=1, fixed_channels=1, fixed_sample_rate=8000,
               description="Mobile voice recordings; always 8 kHz mono."),
    FormatSpec("amrwb", "AMR (wideband)", ".awb", "libvo_amrwbenc", "amr",
               bitrates=(6.6, 8.85, 12.65, 14.25, 15.85, 18.25, 19.85, 23.05, 23.85), default_bitrate=23.85,
               sample_rates=(16000,), max_channels=1, fixed_channels=1, fixed_sample_rate=16000,
               description="Wideband mobile voice; always 16 kHz mono."),
)}

DEFAULT_FORMAT = "mp3"
SAMPLE_FMT_FOR_DEPTH = {8: "pcm_u8", 16: "pcm_s16le", 24: "pcm_s24le", 32: "pcm_f32le"}
AIFF_FOR_DEPTH = {16: "pcm_s16be", 24: "pcm_s24be"}
TAG_CAPABLE = {"mp3", "m4a", "m4b", "ogg", "opus", "wma", "flac", "wav", "aiff"}


@dataclass
class EncodeSettings:
    format: str = DEFAULT_FORMAT
    mode: str = "cbr"          # cbr | vbr
    bitrate: float = 0.0       # kbps; 0 means the format's default
    quality: float = 0.0       # VBR quality value; meaning depends on the format
    sample_rate: int = 0       # 0 keeps the source rate
    channels: int = 0          # 0 keeps the source layout
    bit_depth: int = 0         # 0 means the format's default
    compression: int = 5       # FLAC compression level

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "EncodeSettings":
        names = {f.name for f in fields(cls)}
        clean = {k: v for k, v in (data or {}).items() if k in names}
        s = cls(**clean)
        if s.format not in FORMATS:
            s.format = DEFAULT_FORMAT
        return s

    @property
    def spec(self) -> FormatSpec:
        return FORMATS[self.format]


def available_keys() -> list[str]:
    """The formats the installed FFmpeg can write (a build without the AMR encoders, say, does not offer AMR)."""
    from . import ffmpeg as ff
    if not ff._capability_set("encoders"):          # FFmpeg missing or unreadable: offer everything
        return list(FORMATS)
    return [key for key, spec in FORMATS.items() if ff.has_encoder(spec.codec)] or list(FORMATS)


def nearest_rate(rate: int, allowed: tuple) -> int:
    if rate in allowed:
        return rate
    return min(allowed, key=lambda r: (abs(r - rate), -r))


def resolve(s: EncodeSettings, src_sr: int, src_channels: int) -> tuple[int, int]:
    """The (sample rate, channels) that will actually be written."""
    spec = s.spec
    channels = spec.fixed_channels or s.channels or min(src_channels, spec.max_channels)
    channels = max(1, min(channels, spec.max_channels))
    rate = spec.fixed_sample_rate or nearest_rate(s.sample_rate or src_sr, spec.sample_rates)
    return rate, channels


def _bits(kbps: float) -> str:
    return str(int(round(kbps * 1000)))


def effective_bitrate(s: EncodeSettings) -> float:
    return s.bitrate or s.spec.default_bitrate


def build_args(s: EncodeSettings, src_sr: int, src_channels: int) -> list[str]:
    """ffmpeg output arguments (codec, quality, rate, channels, muxer) for these settings."""
    spec = s.spec
    rate, channels = resolve(s, src_sr, src_channels)
    key = s.format
    args: list[str] = []
    vbr = s.mode == "vbr" and bool(spec.vbr_choices)
    if key == "mp3":
        args = ["-c:a", spec.codec, *(["-q:a", str(int(s.quality))] if vbr else ["-b:a", _bits(effective_bitrate(s))]),
                "-id3v2_version", "3"]
    elif key in ("m4a", "m4b"):
        args = ["-c:a", "aac", "-b:a", _bits(effective_bitrate(s)), "-movflags", "+faststart"]
    elif key == "aac":
        args = ["-c:a", "aac", "-b:a", _bits(effective_bitrate(s))]
    elif key == "ogg":
        args = ["-c:a", "libvorbis", *(["-q:a", str(int(s.quality))] if vbr else ["-b:a", _bits(effective_bitrate(s))])]
    elif key == "opus":
        args = ["-c:a", "libopus", "-b:a", _bits(effective_bitrate(s)), "-vbr", "on"]
    elif key == "wma":
        args = ["-c:a", "wmav2", "-b:a", _bits(effective_bitrate(s))]
    elif key == "flac":
        depth = s.bit_depth or spec.default_depth
        args = ["-c:a", "flac", "-compression_level", str(int(s.compression))]
        args += ["-sample_fmt", "s32", "-bits_per_raw_sample", "24"] if depth == 24 else ["-sample_fmt", "s16"]
    elif key == "wav":
        args = ["-c:a", SAMPLE_FMT_FOR_DEPTH.get(s.bit_depth or spec.default_depth, "pcm_s16le")]
    elif key == "aiff":
        args = ["-c:a", AIFF_FOR_DEPTH.get(s.bit_depth or spec.default_depth, "pcm_s16be")]
    elif key == "mp2":
        args = ["-c:a", "mp2", "-b:a", _bits(effective_bitrate(s))]
    elif key in ("amr", "amrwb"):
        args = ["-c:a", spec.codec, "-b:a", _bits(effective_bitrate(s))]
    return [*args, "-ar", str(rate), "-ac", str(channels), "-f", spec.muxer]


def _channel_name(n: int) -> str:
    return {1: tr("Mono"), 2: tr("Stereo")}.get(n) or tr(f"{n} channels")


def describe(s: EncodeSettings, src_sr: int = 44100, src_channels: int = 2) -> str:
    """One-line summary, e.g. 'MP3 · 44.1 kHz · Stereo · 192 kbps'."""
    spec = s.spec
    rate, channels = resolve(s, src_sr, src_channels)
    parts = [spec.name.split(" (")[0], tr(f"{rate / 1000:g} kHz"), _channel_name(channels)]
    if spec.lossless:
        parts.append(tr(f"{s.bit_depth or spec.default_depth}-bit"))
    elif s.mode == "vbr" and spec.vbr_choices:
        label = next((c[1] for c in spec.vbr_choices if c[0] == int(s.quality)), f"quality {s.quality:g}")
        parts.append(tr(label.split("  ")[0]))
    else:
        parts.append(tr(f"{effective_bitrate(s):g} kbps"))
    return " · ".join(parts)


def estimate_size(s: EncodeSettings, duration: float, src_sr: int = 44100, src_channels: int = 2) -> int:
    """Rough size of the output in bytes (used for the free-space check)."""
    spec = s.spec
    rate, channels = resolve(s, src_sr, src_channels)
    if spec.lossless:
        raw = rate * channels * ((s.bit_depth or spec.default_depth) / 8.0) * duration
        return int(raw * (0.6 if s.format == "flac" else 1.0))
    if s.mode == "vbr" and spec.vbr_choices:
        kbps = next((c[2] for c in spec.vbr_choices if c[0] == int(s.quality)), 128)
    else:
        kbps = effective_bitrate(s)
    return int(kbps * 1000 / 8.0 * duration)
