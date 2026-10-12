"""Checks a built Tonewright.app without showing a window. Usage: python smoke_test.py path/to/Tonewright.app

Runs on the Mac that built the app. It starts the app's own program in Qt's offscreen mode, runs the bundled FFmpeg and
checks that only the Qt plugins we meant to ship are inside."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ALLOWED_IMAGE_PLUGINS = {"libqjpeg.dylib", "libqgif.dylib"}
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""), flush=True)


def find_one(app: Path, name: str) -> Path | None:
    hits = [p for p in app.rglob(name) if p.is_file() and not p.is_symlink()]
    return hits[0] if hits else None


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=180, **kw)


def main() -> int:
    app = Path(sys.argv[1]).resolve()
    exe = app / "Contents" / "MacOS" / "Tonewright"
    check("app bundle layout", exe.is_file() and (app / "Contents" / "Info.plist").is_file(), str(app))

    info = run(["/usr/libexec/PlistBuddy", "-c", "Print :CFBundleShortVersionString", app / "Contents" / "Info.plist"])
    check("version in Info.plist", info.returncode == 0 and info.stdout.strip() != "", info.stdout.strip())
    mic = run(["/usr/libexec/PlistBuddy", "-c", "Print :NSMicrophoneUsageDescription", app / "Contents" / "Info.plist"])
    check("microphone permission text present", mic.returncode == 0 and mic.stdout.strip() != "")

    arch = run(["/usr/bin/lipo", "-archs", exe])
    check("program is arm64", "arm64" in arch.stdout, arch.stdout.strip())

    ffmpeg, ffprobe = find_one(app, "ffmpeg"), find_one(app, "ffprobe")
    check("FFmpeg and FFprobe are bundled", ffmpeg is not None and ffprobe is not None, str(ffmpeg))
    if ffmpeg and ffprobe:
        v = run([ffmpeg, "-version"])
        check("FFmpeg runs", v.returncode == 0 and "ffmpeg version" in v.stdout, v.stdout.splitlines()[0][:50] if v.stdout else v.stderr[:80])
        check("FFprobe runs", run([ffprobe, "-version"]).returncode == 0)
        with tempfile.TemporaryDirectory(prefix="tw-smoke-") as tmp:
            tone = Path(tmp) / "tone.wav"
            made = run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-ar", "44100", tone])
            check("FFmpeg makes a test tone", made.returncode == 0 and tone.is_file(), made.stderr[:80])
            for ext, extra in (("mp3", []), ("m4a", []), ("flac", []), ("opus", []), ("ogg", [])):
                out = Path(tmp) / f"tone.{ext}"
                r = run([ffmpeg, "-v", "error", "-i", tone, *extra, out])
                probe = run([ffprobe, "-v", "error", "-show_format", "-of", "json", out])
                ok = r.returncode == 0 and probe.returncode == 0 and float(json.loads(probe.stdout)["format"]["duration"]) > 0.9
                check(f"convert to {ext}", ok, r.stderr[:80])

    portaudio = [p for p in app.rglob("libportaudio*.dylib") if p.is_file()]      # (the package also carries Windows .dll files)
    check("PortAudio (sound output and recording) is bundled", bool(portaudio), portaudio[0].name if portaudio else "")

    plugin_dirs = [p for p in app.rglob("imageformats") if p.is_dir()]
    shipped = {f.name for d in plugin_dirs for f in d.iterdir() if f.is_file()}
    check("only the needed picture decoders are inside", shipped <= ALLOWED_IMAGE_PLUGINS and bool(shipped), ", ".join(sorted(shipped)))

    with tempfile.TemporaryDirectory(prefix="tw-smoke-") as tmp:
        shot = Path(tmp) / "window.png"
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", HOME=tmp)          # a throw-away settings folder, no window
        r = subprocess.run([str(exe), "--selftest", str(shot)], capture_output=True, text=True, timeout=180, env=env)
        check("the app starts, draws its window and quits", r.returncode == 0 and shot.is_file() and shot.stat().st_size > 5000,
              (r.stderr or r.stdout)[-160:].replace("\n", " "))

    failed = [n for n, ok, _ in results if not ok]
    print(json.dumps({"checks": len(results), "failed": failed}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
