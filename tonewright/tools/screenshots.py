"""Render the README screenshots from synthetic audio. Nothing is ever shown on screen.

    .venv\\Scripts\\python.exe tools\\screenshots.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

work = Path(tempfile.mkdtemp(prefix="tw-shots-"))
os.environ["APPDATA"] = str(work / "appdata")

from conftest import PNG_1X1, make_audio_file  # noqa: E402
from tonewright.core import effects as fx  # noqa: E402
from tonewright.core.settings import Favorites, Settings  # noqa: E402
from tonewright.ui import tasks, theme  # noqa: E402
from tonewright.ui.effect_dialog import EffectDialog  # noqa: E402
from tonewright.ui.main_window import MainWindow  # noqa: E402
from tonewright.ui.state import AppState  # noqa: E402

SR = 44100
rng = np.random.default_rng(3)


def speechy(seconds: float, f: float) -> np.ndarray:
    n = int(seconds * SR)
    t = np.arange(n) / SR
    env = np.zeros(n)
    pos = 0.0
    while pos < seconds - 1:
        d, g = rng.uniform(0.2, 1.4), rng.uniform(0.05, 0.8)
        a, b = int(pos * SR), min(n, int((pos + d) * SR))
        env[a:b] += np.hanning(b - a) * rng.uniform(0.3, 0.9)
        pos += d + g
    base = (np.sin(2 * np.pi * (f + 60 * np.sin(2 * np.pi * 0.3 * t)) * t) + 0.5 * np.sin(2 * np.pi * 900 * t)
            + 0.2 * rng.standard_normal(n))
    return np.stack([env * base * 0.5, env * np.roll(base, 100) * 0.4], 1).astype(np.float32)


def main() -> None:
    out = ROOT / "docs"
    out.mkdir(exist_ok=True)
    app = QApplication([])
    tasks.ProgressCard.show = lambda self: None
    theme.apply_theme(app, "dark")
    files = []
    for name, ext, sec, f in [("01 Middle Tun Waxing Moon", ".mp3", 40, 140), ("02 Heart Sutra", ".flac", 25, 180),
                              ("03 Acoustic guitar", ".m4a", 30, 220)]:
        files.append(make_audio_file(work / (name + ext), speechy(sec, f)))
    state = AppState(Settings(work / "s.json"), Favorites(work / "f.json"))
    win = MainWindow(state)
    win.resize(1500, 920)
    win.ctl.open_paths(files)
    for i, item in enumerate(state.items):
        item.tags.title = ["Middle Tun Waxing Moon", "Heart Sutra", "Acoustic guitar"][i]
        item.tags.artist, item.tags.album, item.tags.year, item.tags.cover = "Various", "Meditations", "2026", PNG_1X1
        item.tags.track, item.tags.track_total = str(i + 1), "3"

    def shot(widget, name: str) -> None:
        for _ in range(5):
            app.processEvents()
        widget.grab().save(str(out / f"{name}.png"))
        print("wrote", out / f"{name}.png")

    doc = win.ctl.doc
    doc.add_marker(SR * 5, "Intro")
    doc.add_marker(SR * 24, "Verse")
    win.stage.set_selection(SR * 8, SR * 14)
    shot(win, "edit")
    win.stage.set_mode("spectral")
    shot(win, "spectral")
    win.stage.set_mode("waveform")
    effect = fx.get("fade")
    dlg = EffectDialog(effect, {"direction": "out", "shape": "slow", "start": 100, "end": 0},
                       win.ctl._effect_context(effect), win)
    dlg.adjustSize()
    shot(dlg, "fade-dialog")
    win.set_mode("convert")
    state.items[0].status, state.items[0].progress = "converting", 0.45
    state.items[1].status = "done"
    state.touched(state.items[0])
    state.touched(state.items[1])
    shot(win, "convert")
    win.set_mode("tags")
    win.tags_page.set_items(state.items)
    shot(win, "tags")
    win.set_mode("audiobook")
    win.book_panel.author.setText("Various")
    win.preview_chapters()
    shot(win, "audiobook")
    for item in state.items:
        if item.doc is not None:
            item.doc.mark_saved()


if __name__ == "__main__":
    main()
