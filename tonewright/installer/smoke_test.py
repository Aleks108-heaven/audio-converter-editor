"""Check a staged or installed Tonewright using ITS OWN Python. No window is ever shown.

    <install>\\python\\python.exe smoke_test.py --stage <install folder>
"""
from __future__ import annotations

import importlib
import json
import os
import pkgutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

args = sys.argv[1:]
stage = Path(args[args.index("--stage") + 1]).resolve()
work = Path(tempfile.mkdtemp(prefix="tw-smoke-"))
os.environ["APPDATA"] = str(work / "appdata")          # never touch the real settings
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""), flush=True)


def inside(path) -> bool:
    try:
        Path(path).resolve().relative_to(stage)
        return True
    except ValueError:
        return False


# ---- the interpreter is isolated from the rest of the machine
check("isolated interpreter", sys.flags.isolated == 1 and sys.flags.safe_path and sys.flags.ignore_environment)
check("python lives in the install", inside(sys.executable), sys.executable)

# ---- the program and its libraries come from the install, not from somewhere else on the PC
import tonewright  # noqa: E402
import numpy  # noqa: E402
import mutagen  # noqa: E402
import PySide6  # noqa: E402
check("tonewright is the installed copy", inside(tonewright.__file__), tonewright.__file__)
for mod in (numpy, mutagen, PySide6):
    check(f"{mod.__name__} is the installed copy", inside(mod.__file__), mod.__file__)

try:
    import sounddevice  # noqa: F401
    check("sounddevice and PortAudio load", True)
except Exception as exc:  # noqa: BLE001
    check("sounddevice and PortAudio load", False, repr(exc))

# ---- every module imports
bad = []
for info in pkgutil.walk_packages(tonewright.__path__, "tonewright."):
    if info.name.endswith(".__main__"):          # the entry point: only meant to be run, never imported by a test
        continue
    try:
        importlib.import_module(info.name)
    except Exception as exc:  # noqa: BLE001
        bad.append(f"{info.name}: {exc!r}")
check("every tonewright module imports", not bad, "; ".join(bad[:3]))

# ---- FFmpeg is the bundled one and runs
from tonewright.core import ffmpeg  # noqa: E402
exe = ffmpeg.find_tool("ffmpeg")
check("FFmpeg is the bundled copy", inside(exe), exe)
version = subprocess.run([exe, "-version"], capture_output=True, text=True, creationflags=0x08000000).stdout.splitlines()[0]
check("FFmpeg runs", version.startswith("ffmpeg version"), version[:60])
check("FFprobe is bundled", inside(ffmpeg.find_tool("ffprobe")))
check("rubberband (tempo/pitch) is available", ffmpeg.has_filter("rubberband"))

# ---- Qt: platform plugin, image formats, fonts
from PySide6.QtGui import QImageReader  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
app = QApplication([])
check("Qt starts with the Windows platform plugin", app.platformName() == "windows", app.platformName())
formats = {bytes(f).decode().lower() for f in QImageReader.supportedImageFormats()}
check("cover-art formats are present", {"jpg", "png", "gif", "bmp"} <= formats, ",".join(sorted(formats)))
check("rarely needed picture decoders are absent", not ({"tiff", "webp", "tga", "icns", "wbmp"} & formats), ",".join(sorted(formats)))

# ---- the real window, in both languages, drawn off screen
from tonewright import i18n  # noqa: E402
from tonewright.core.settings import Favorites, Settings  # noqa: E402
from tonewright.ui import theme  # noqa: E402
from tonewright.ui.main_window import MainWindow  # noqa: E402
from tonewright.ui.state import AppState  # noqa: E402

for lang, expected_tab in (("en", "Convert"), ("uk", "Конвертер")):
    i18n.set_language(lang)
    i18n.install_qt_translations(app)
    theme.apply_theme(app, "dark")
    state = AppState(Settings(work / f"settings-{lang}.json"), Favorites(work / f"fav-{lang}.json"))
    win = MainWindow(state)
    win.resize(1500, 900)
    tabs = [b.text() for b in win.mode_buttons.values()]
    shot = work / f"window-{lang}.png"
    image = win.grab().toImage()
    image.save(str(shot))
    colours = {image.pixel(x, y) for x in range(0, image.width(), 97) for y in range(0, image.height(), 61)}
    check(f"main window draws ({lang})", shot.stat().st_size > 20_000 and len(colours) > 4, f"{image.width()}x{image.height()}")
    check(f"interface language ({lang})", expected_tab in tabs, ", ".join(tabs))
    win.close()
    win.deleteLater()
i18n.set_language("en")

# ---- decode, convert and tag a real file end to end
import numpy as np  # noqa: E402
from tonewright.core import formats as fmt, tags  # noqa: E402
from tonewright.core.convert import ConvertOptions, convert_all, probe_item  # noqa: E402

sr = 44100
t = np.arange(sr * 2) / sr
samples = (np.sin(2 * np.pi * 440 * t) * 0.4 * 32767).astype("<i2")
src = work / "tone.wav"
with wave.open(str(src), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(samples.tobytes())
item = probe_item(src)
item.tags.title, item.tags.artist = "Тест", "Tonewright"
for key in ("mp3", "m4a", "flac", "opus"):
    res = convert_all([item], ConvertOptions(settings=fmt.EncodeSettings(format=key), out_dir=work / "out"))[0]
    ok = res.status == "done" and abs(ffmpeg.probe(res.output).duration - 2.0) < 0.2
    check(f"convert to {key}", ok, res.message or "")
    if key == "mp3" and ok:
        check("tags survive (Unicode)", tags.read_tags(res.output).title == "Тест")

failed = [name for name, ok, _ in results if not ok]
print(json.dumps({"checks": len(results), "failed": failed}, ensure_ascii=False))
sys.exit(1 if failed else 0)
