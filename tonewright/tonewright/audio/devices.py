"""Audio device discovery (PortAudio via sounddevice)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

try:
    import sounddevice as sd
except Exception:  # PortAudio missing or unusable
    sd = None


class AudioDeviceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Device:
    index: int
    name: str
    api: str
    channels: int
    default_rate: float
    is_default: bool = False

    @property
    def label(self) -> str:
        return self.name


def available() -> bool:
    return sd is not None


def _list(kind: str) -> list[Device]:
    if sd is None:
        return []
    try:
        devices = sd.query_devices()
        apis = [a["name"] for a in sd.query_hostapis()]
        default = sd.default.device[0 if kind == "input" else 1]
    except Exception:
        return []
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    # one entry per physical name, preferring the MME API (resamples any rate for us)
    best: dict[str, Device] = {}
    for i, d in enumerate(devices):
        if d[key] <= 0 or "Sound Mapper" in d["name"] or "Primary Sound" in d["name"]:
            continue
        api = apis[d["hostapi"]] if d["hostapi"] < len(apis) else ""
        dev = Device(i, d["name"], api, int(d[key]), float(d["default_samplerate"]), i == default)
        rank = {"MME": 0, "Windows DirectSound": 1, "Windows WASAPI": 2}.get(api, 3)
        old = best.get(d["name"][:30])
        if old is None or rank < {"MME": 0, "Windows DirectSound": 1, "Windows WASAPI": 2}.get(old.api, 3):
            best[d["name"][:30]] = dev
    return sorted(best.values(), key=lambda dv: (not dv.is_default, dv.name))


def output_devices() -> list[Device]:
    return _list("output")


def input_devices() -> list[Device]:
    return _list("input")


def default_input() -> Optional[Device]:
    devs = input_devices()
    return devs[0] if devs else None
