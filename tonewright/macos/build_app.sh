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

# FFmpeg 9.0.2, static arm64 release build by Martin Riedl (https://ffmpeg.martin-riedl.de). The SHA-256 values were
# checked against the ones the site publishes. To move to a newer build, change the URL and both hashes together.
FF_BASE="https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2"
FF_SHA_FFMPEG="c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924"
FF_SHA_FFPROBE="fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6"

step() { printf '\n==> %s\n' "$1"; }
fail() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "This builds the macOS app and has to run on a Mac."
[ "$(uname -m)" = "arm64" ] || fail "This build is for Apple Silicon; run it on an M1 or newer Mac."

step "Cleaning"
rm -rf "$WORK" "$DIST"
mkdir -p "$WORK/ffmpeg" "$DIST"

step "FFmpeg (pinned download, checked before use)"
fetch() {   # name expected-sha
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 "$FF_BASE/$1.zip" -o "$WORK/ffmpeg/$1.zip"
  local got; got="$(shasum -a 256 "$WORK/ffmpeg/$1.zip" | cut -d' ' -f1)"
  [ "$got" = "$2" ] || fail "$1.zip does not match the pinned SHA-256 (got $got)"
  unzip -q -o "$WORK/ffmpeg/$1.zip" -d "$WORK/ffmpeg"
  chmod 755 "$WORK/ffmpeg/$1"
  # a download carries a quarantine flag on some systems; the program must be runnable by the build and by its tests
  xattr -c "$WORK/ffmpeg/$1" 2>/dev/null || true
}
fetch ffmpeg "$FF_SHA_FFMPEG"
fetch ffprobe "$FF_SHA_FFPROBE"
"$WORK/ffmpeg/ffmpeg" -version | head -1
"$WORK/ffmpeg/ffprobe" -version | head -1

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
