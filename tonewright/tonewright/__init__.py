"""Tonewright - a personal audio converter and editor."""
import os as _os

__version__ = "1.2.0"
APP_NAME = "Tonewright"

# By default Windows looks in the *current folder* before PATH when a program is started by bare name. Switch that
# off for this process and everything it starts, so a stray ffmpeg.exe or powershell.exe sitting next to a
# downloaded file can never be run in place of the real one.
_os.environ.setdefault("NoDefaultCurrentDirectoryInExePath", "1")
