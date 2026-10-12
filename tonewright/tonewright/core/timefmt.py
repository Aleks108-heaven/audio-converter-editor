"""Formatting and parsing of times, sizes and decibels."""
from __future__ import annotations

import math
import re
from typing import Optional

from ..i18n import tr


def format_time(seconds: float, *, ms: bool = True, short: bool = False) -> str:
    """00:22:41.757; with short=True leading zero hours are dropped (22:41.757)."""
    if seconds is None or not math.isfinite(seconds):
        seconds = 0.0
    neg = seconds < 0
    total_ms = int(round(abs(seconds) * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    if short and h == 0:
        text = f"{m:d}:{s:02d}"
    else:
        text = f"{h:02d}:{m:02d}:{s:02d}"
    if ms:
        text += f".{milli:03d}"
    return ("-" if neg else "") + text


_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([hms])", re.I)


def parse_time(text: str) -> Optional[float]:
    """Accepts 90, 90.5, 1:30, 1:30.25, 01:02:03.5 and 1h2m3.5s. None if invalid."""
    if text is None:
        return None
    t = text.strip().replace(",", ".")
    if not t:
        return None
    if re.search(r"[hms]", t, re.I):
        parts = _UNIT_RE.findall(t)
        if not parts or _UNIT_RE.sub("", t).strip():
            return None
        scale = {"h": 3600, "m": 60, "s": 1}
        return sum(float(n) * scale[u.lower()] for n, u in parts)
    fields = t.split(":")
    if len(fields) > 3:
        return None
    try:
        values = [float(f) for f in fields]
    except ValueError:
        return None
    if any(v < 0 for v in values):
        return None
    total = 0.0
    for v in values:
        total = total * 60 + v
    return total


def format_size(num_bytes: float) -> str:
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {tr(unit)}" if unit == "B" else f"{n:.1f} {tr(unit)}"
        n /= 1024
    return f"{n:.1f} {tr('GB')}"


def db_to_amp(db: float) -> float:
    return 10.0 ** (db / 20.0)


def amp_to_db(amp: float, floor: float = -120.0) -> float:
    if amp <= 0:
        return floor
    return max(floor, 20.0 * math.log10(amp))
