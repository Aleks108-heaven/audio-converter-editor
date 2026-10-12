"""Signal-processing helpers in plain numpy."""
from __future__ import annotations

import numpy as np

# ------------------------------------------------------------------ fades
FADE_SHAPES = (
    ("linear", "Linear"),
    ("slow", "Slow (exponential)"),
    ("fast", "Fast (logarithmic)"),
    ("smooth", "Smooth (S-curve)"),
    ("equal_power", "Equal power"),
)


def fade_shape(x: np.ndarray, shape: str) -> np.ndarray:
    """Monotonic 0..1 curve over x in 0..1."""
    x = np.clip(np.asarray(x, dtype=np.float64), 0.0, 1.0)
    if shape == "slow":      # 60 dB-linear ramp: a gentle start and a steep finish
        return (10.0 ** (-3.0 * (1.0 - x)) - 1e-3) / (1.0 - 1e-3)
    if shape == "fast":
        return 1.0 - fade_shape(1.0 - x, "slow")
    if shape == "smooth":
        return 0.5 - 0.5 * np.cos(np.pi * x)
    if shape == "equal_power":
        return np.sin(0.5 * np.pi * x)
    return x


def fade_gain(x: np.ndarray, shape: str, direction: str, start: float, end: float) -> np.ndarray:
    """Gain at positions x (0..1) of a fade running from `start` to `end` (0..1 levels)."""
    if direction == "out":
        return end + (start - end) * fade_shape(1.0 - np.asarray(x), shape)
    return start + (end - start) * fade_shape(x, shape)


# ------------------------------------------------------------------ convolution
def next_pow2(n: int) -> int:
    return 1 << max(0, int(n) - 1).bit_length()


class OlaConvolver:
    """Streaming FFT convolution (overlap-add). Feed blocks of at most `block` frames."""

    def __init__(self, ir: np.ndarray, block: int) -> None:
        self.length = ir.shape[0]
        self.block = block
        self.nfft = next_pow2(block + self.length - 1)
        self.spectrum = np.fft.rfft(ir.astype(np.float32), self.nfft, axis=0)
        self.tail = np.zeros((self.length - 1, ir.shape[1]), np.float32)

    def process(self, x: np.ndarray) -> np.ndarray:
        n = x.shape[0]
        if n > self.block:
            raise ValueError("block larger than planned")
        spec = np.fft.rfft(x.astype(np.float32), self.nfft, axis=0)
        y = np.fft.irfft(spec * self.spectrum, self.nfft, axis=0)[: n + self.length - 1].astype(np.float32)
        y[: self.length - 1] += self.tail
        self.tail = y[n:].copy()
        return y[:n].copy()

    def flush(self) -> np.ndarray:
        out, self.tail = self.tail, np.zeros_like(self.tail)
        return out


def _soft_lowpass(x: np.ndarray, sr: int, cutoff: float) -> np.ndarray:
    spec = np.fft.rfft(x, axis=0)
    freqs = np.fft.rfftfreq(x.shape[0], 1.0 / sr)
    spec *= (1.0 / (1.0 + (freqs / cutoff) ** 4))[:, None]
    out = np.fft.irfft(spec, x.shape[0], axis=0)
    return out / max(1e-12, float(out.std()))


def reverb_ir_length(sr: int, rt60: float, predelay_ms: float) -> int:
    return max(64, int(sr * rt60 * 1.1)) + int(sr * predelay_ms / 1000.0)


def make_reverb_ir(sr: int, channels: int, rt60: float, predelay_ms: float,
                   damping: float, width: float) -> np.ndarray:
    """Synthetic room response: decaying noise that darkens over time, plus early echoes."""
    rng = np.random.default_rng(20240607)
    n = reverb_ir_length(sr, rt60, 0.0)
    t = np.arange(n) / sr
    noise = rng.standard_normal((n, channels)).astype(np.float64)
    mean = noise.mean(axis=1, keepdims=True)
    noise = mean + width * (noise - mean)
    noise /= max(1e-12, float(noise.std()))
    bands = [noise] + [_soft_lowpass(noise, sr, fc) for fc in (6000.0, 2500.0, 900.0)]
    pos = np.clip(damping * 3.0 * np.minimum(t / min(rt60, 2.0), 1.0), 0.0, 3.0)
    ir = np.zeros_like(noise)
    for k in range(3):
        mask = (pos >= k) & ((pos < k + 1) | (k == 2))
        w = np.clip(pos[mask] - k, 0.0, 1.0)[:, None]
        ir[mask] = bands[k][mask] * (1.0 - w) + bands[k + 1][mask] * w
    for _ in range(12):
        for c in range(channels):
            at = int(rng.uniform(0.004, 0.08) * sr)
            if at < n:
                ir[at, c] += rng.choice([-1.0, 1.0]) * rng.uniform(3.0, 6.0)
    ir *= np.exp(-6.9078 * t / rt60)[:, None]
    pre = int(sr * predelay_ms / 1000.0)
    if pre:
        ir = np.concatenate([np.zeros((pre, channels)), ir])
    ir /= max(1e-12, float(np.sqrt((ir ** 2).sum(axis=0).mean())))
    return ir.astype(np.float32)


# ------------------------------------------------------------------ noise reduction
N_FFT = 2048
HOP = N_FFT // 4
_WINDOW = np.sqrt(np.hanning(N_FFT + 1)[:-1]).astype(np.float32)


def stft(x: np.ndarray) -> np.ndarray:
    """(frames, bins) complex spectrogram; the signal is zero-padded by one window each side."""
    x = np.asarray(x, dtype=np.float32)
    tail = (-len(x)) % HOP
    padded = np.concatenate([np.zeros(N_FFT, np.float32), x, np.zeros(N_FFT + tail, np.float32)])
    frames = np.lib.stride_tricks.sliding_window_view(padded, N_FFT)[::HOP]
    return np.fft.rfft(frames * _WINDOW, axis=1)


def istft(spec: np.ndarray, n: int) -> np.ndarray:
    frames = np.fft.irfft(spec, N_FFT, axis=1).astype(np.float32) * _WINDOW
    out = np.zeros((frames.shape[0] - 1) * HOP + N_FFT, np.float32)
    for phase in range(4):
        seg = frames[phase::4]
        if len(seg):
            flat = seg.reshape(-1)
            out[phase * HOP: phase * HOP + flat.size] += flat
    return out[N_FFT: N_FFT + n] / 2.0


def _smooth_profile(power: np.ndarray) -> np.ndarray:
    """A noise profile measured from a few frames is itself noisy; blur it across bins."""
    return _box(power[None, :].astype(np.float64), 5, 1)[0].astype(np.float32)


def noise_power(x: np.ndarray) -> np.ndarray:
    """Mean power per frequency bin of a noise-only recording."""
    spec = stft(x)
    inner = spec[4:-4] if len(spec) > 12 else spec
    return _smooth_profile((np.abs(inner) ** 2).mean(axis=0))


def estimate_noise_power(x: np.ndarray, quietest: float = 0.1) -> np.ndarray:
    """Guess the noise floor from the quietest tenth of the frames."""
    spec = stft(x)
    power = np.abs(spec) ** 2
    energy = power.sum(axis=1)
    live = energy > 1e-14
    if live.sum() < 8:
        return _smooth_profile(power.mean(axis=0) + 1e-12)
    order = np.argsort(np.where(live, energy, np.inf))
    take = max(4, int(live.sum() * quietest))
    return _smooth_profile(power[order[:take]].mean(axis=0))


def _box(a: np.ndarray, k: int, axis: int) -> np.ndarray:
    if k <= 1:
        return a
    pad = k // 2
    widths = [(0, 0)] * a.ndim
    widths[axis] = (pad, pad)
    padded = np.pad(a, widths, mode="edge")
    total = np.zeros_like(a)
    length = a.shape[axis]
    for i in range(k):
        total += np.take(padded, np.arange(i, i + length), axis=axis)
    return total / k


def reduce_noise(x: np.ndarray, npow: np.ndarray, reduction_db: float, alpha: float,
                 smoothing: float) -> np.ndarray:
    """Spectral-subtraction noise gate for one channel.

    The gain is computed from power averaged over a few frames. Steady tones keep their
    level, while the random spikes of the noise are averaged away, which is what causes
    the warbling "musical noise" of naive spectral subtraction.
    """
    spec = stft(x)
    power = _box((np.abs(spec) ** 2).astype(np.float32), 1 + 2 * int(round(smoothing * 3)), 0)
    floor = 10.0 ** (-reduction_db / 20.0)
    gain = 1.0 - alpha * np.sqrt(npow)[None, :] / (np.sqrt(power) + 1e-12)
    gain = np.maximum(gain, floor).astype(np.float32)
    return istft(spec * gain, len(x))
