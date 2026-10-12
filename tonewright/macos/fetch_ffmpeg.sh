#!/bin/bash
# Downloads the pinned FFmpeg + FFprobe (static Apple Silicon build) into the folder given as the first argument and
# checks both against their SHA-256. Used by build_app.sh and by the test job, so the tests run against the same
# FFmpeg that ships in the app.
#
# FFmpeg 9.0.2 by Martin Riedl (https://ffmpeg.martin-riedl.de). The SHA-256 values were checked against the ones the
# site publishes. To move to a newer build, change the URL and both hashes together.
set -euo pipefail

DEST="${1:?usage: fetch_ffmpeg.sh <folder>}"
FF_BASE="https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2"
FF_SHA_FFMPEG="c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924"
FF_SHA_FFPROBE="fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6"

mkdir -p "$DEST"

fetch() {   # name expected-sha
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 "$FF_BASE/$1.zip" -o "$DEST/$1.zip"
  local got; got="$(shasum -a 256 "$DEST/$1.zip" | cut -d' ' -f1)"
  if [ "$got" != "$2" ]; then
    echo "ERROR: $1.zip does not match the pinned SHA-256 (got $got)" >&2
    exit 1
  fi
  unzip -q -o "$DEST/$1.zip" -d "$DEST"
  rm -f "$DEST/$1.zip"
  chmod 755 "$DEST/$1"
  xattr -c "$DEST/$1" 2>/dev/null || true       # a download can carry a quarantine flag; the tests must be able to run it
}

fetch ffmpeg "$FF_SHA_FFMPEG"
fetch ffprobe "$FF_SHA_FFPROBE"
"$DEST/ffmpeg" -version | head -1
"$DEST/ffprobe" -version | head -1
