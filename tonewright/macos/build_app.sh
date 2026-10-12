#!/bin/bash
# Builds dist/Tonewright.app and dist/Tonewright-<version>-arm64.dmg on an Apple Silicon Mac.
#   macos/build_app.sh            use the Python on PATH (a virtual environment with requirements.lock.txt + pyinstaller)
# It downloads one pinned FFmpeg build, checks its SHA-256, bundles it, ad-hoc signs the app and tests the result.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
WORK="$ROOT/macos/build"
DIST="$ROOT/macos/dist"
VERSION="$(cd "$ROOT" && python -c 'import tonewright; print(tonewright.__version__)')"
export TONEWRIGHT_VERSION="$VERSION"

step() { printf '\n==> %s\n' "$1"; }
fail() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "This builds the macOS app and has to run on a Mac."
[ "$(uname -m)" = "arm64" ] || fail "This build is for Apple Silicon; run it on an M1 or newer Mac."

step "Cleaning"
rm -rf "$WORK" "$DIST"
mkdir -p "$WORK/ffmpeg" "$DIST"

step "FFmpeg (pinned download, checked before use)"
bash "$HERE/fetch_ffmpeg.sh" "$WORK/ffmpeg"

step "Packaging with PyInstaller"
export FFMPEG_DIR="$WORK/ffmpeg"
(cd "$ROOT" && python -m PyInstaller --noconfirm --clean --distpath "$DIST" --workpath "$WORK/pyi" macos/tonewright.spec)
APP="$DIST/Tonewright.app"
[ -d "$APP" ] || fail "PyInstaller did not produce $APP"

step "Signing (ad hoc: required for Apple Silicon to run it; not a Developer ID signature)"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

step "Testing the app"
python "$HERE/smoke_test.py" "$APP"

step "Disk image"
STAGE="$WORK/dmg"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
DMG="$DIST/Tonewright-$VERSION-arm64.dmg"
hdiutil create -volname "Tonewright" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
echo "$DMG"
shasum -a 256 "$DMG"
