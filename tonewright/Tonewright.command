#!/bin/bash
# Start Tonewright from this folder on a Mac (double-click it in Finder, or run it in Terminal).
# The first time it makes a private Python environment here and installs what it needs (needs internet once).
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
  PY="$(command -v python3.13 || command -v python3.12 || command -v python3)"
  [ -n "$PY" ] || { echo "Python 3 is needed. Install it from https://www.python.org/downloads/ or with: brew install python"; read -r -p "Press Return to close"; exit 1; }
  "$PY" -m venv .venv || exit 1
  .venv/bin/python -m pip install --upgrade pip || exit 1
  .venv/bin/python -m pip install -r requirements.txt || exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1 && [ ! -x /opt/homebrew/bin/ffmpeg ] && [ ! -x /usr/local/bin/ffmpeg ]; then
  echo "FFmpeg is needed. Install it with: brew install ffmpeg"
  read -r -p "Press Return to close"
  exit 1
fi

exec .venv/bin/python -m tonewright "$@"
