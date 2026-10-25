# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for Tonewright.app (Apple Silicon). Run through macos/build_app.sh, which supplies FFMPEG_DIR."""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
FFMPEG_DIR = Path(os.environ["FFMPEG_DIR"])
VERSION = os.environ.get("TONEWRIGHT_VERSION", "1.2.0")

# Qt plugins are what decode pictures and talk to the window system, so only the ones the program uses are shipped
# (the same short list as the Windows installer). Cover art is also restricted to JPEG/PNG/GIF/BMP in the code.
KEEP_PLUGINS = {
    "platforms": {"libqcocoa.dylib", "libqoffscreen.dylib"},   # offscreen lets the build's own test run without a window
    "styles": None,                                              # every style
    "imageformats": {"libqjpeg.dylib", "libqgif.dylib"},
    "iconengines": {"libqsvgicon.dylib"},
}
KEEP_TRANSLATIONS = {"qtbase_uk.qm", "qt_uk.qm"}


def wanted(dest: str) -> bool:
    d = dest.replace("\\", "/")
    if "/plugins/" in d:
        kind, _, rest = d.split("/plugins/", 1)[1].partition("/")
        if kind not in KEEP_PLUGINS:
            return False
        allowed = KEEP_PLUGINS[kind]
        return allowed is None or rest.rsplit("/", 1)[-1] in allowed
    if "/translations/" in d:
        return d.rsplit("/", 1)[-1] in KEEP_TRANSLATIONS
    return True


a = Analysis(
    [str(ROOT / "macos" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "tonewright" / "assets"), "tonewright/assets"),
        (str(FFMPEG_DIR / "ffmpeg"), "bin"),
        (str(FFMPEG_DIR / "ffprobe"), "bin"),
        (str(ROOT / "macos" / "THIRD-PARTY-NOTICES.txt"), "licenses"),
        (str(ROOT / "README.md"), "licenses"),
    ],
    hiddenimports=collect_submodules("tonewright"),         # the language catalogues are imported by name
    excludes=["tkinter", "pytest", "unittest", "pydoc", "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick",
              "PySide6.QtPdf", "PySide6.QtOpenGL", "PySide6.QtDBus"],
    noarchive=False,
)
a.binaries = [b for b in a.binaries if wanted(b[0])]
a.datas = [d for d in a.datas if wanted(d[0])]

pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Tonewright", console=False, argv_emulation=False,
          target_arch="arm64")
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Tonewright")
app = BUNDLE(
    coll,
    name="Tonewright.app",
    icon=str(ROOT / "tonewright" / "assets" / "tonewright.icns"),
    bundle_identifier="app.tonewright.audio",
    version=VERSION,
    info_plist={
        "CFBundleName": "Tonewright",
        "CFBundleDisplayName": "Tonewright",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "11.0",
        "LSApplicationCategoryType": "public.app-category.music",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription": "Tonewright records audio from the microphone when you use Record.",
        "CFBundleLocalizations": ["en", "uk"],
        # lets Finder offer "Open With" and the Dock accept dropped audio files (they arrive as file-open events)
        "CFBundleDocumentTypes": [{
            "CFBundleTypeName": "Audio",
            "CFBundleTypeRole": "Editor",
            "LSHandlerRank": "Alternate",
            "LSItemContentTypes": ["public.audio", "public.mp3", "com.apple.m4a-audio", "public.aiff-audio",
                                   "com.microsoft.waveform-audio", "org.xiph.flac", "public.mpeg-4-audio"],
        }],
    },
)
