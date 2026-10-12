"""Where Tonewright keeps settings and scratch audio."""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from . import APP_NAME

_session_dir: Optional[Path] = None
_lock_handle = None


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def windows_program(*parts: str) -> str:
    """Full path of a program that ships with Windows, so it is never looked up by bare name."""
    path = Path(os.environ.get("SystemRoot") or r"C:\Windows", *parts)
    return str(path) if path.is_file() else parts[-1]


def powershell_exe() -> str:
    return windows_program("System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def explorer_exe() -> str:
    return windows_program("explorer.exe")


def _scratch_root() -> Path:
    return Path(tempfile.gettempdir()) / APP_NAME


def session_dir() -> Path:
    """Per-process scratch folder. A lock file held open for the whole run lets
    later sessions tell a live folder from one left behind by a crash."""
    global _session_dir, _lock_handle
    if _session_dir is None:
        root = _scratch_root()
        root.mkdir(parents=True, exist_ok=True)
        _session_dir = Path(tempfile.mkdtemp(prefix="s", dir=root))
        _lock_handle = open(_session_dir / ".lock", "wb")
        atexit.register(_cleanup_own_session)
    return _session_dir


def new_scratch_file(suffix: str = ".f32") -> Path:
    fd, name = tempfile.mkstemp(suffix=suffix, dir=session_dir())
    os.close(fd)
    return Path(name)


def _cleanup_own_session() -> None:
    global _lock_handle
    if _lock_handle is not None:
        try:
            _lock_handle.close()
        except OSError:
            pass
        _lock_handle = None
    if _session_dir is not None:
        shutil.rmtree(_session_dir, ignore_errors=True)


def cleanup_stale_sessions() -> int:
    """Delete scratch folders from sessions that are no longer running."""
    root = _scratch_root()
    removed = 0
    if not root.exists():
        return 0
    for child in root.iterdir():
        if not child.is_dir() or child == _session_dir:
            continue
        lock = child / ".lock"
        try:
            if lock.exists():
                lock.unlink()  # fails on Windows while the owning process holds it open
            shutil.rmtree(child)
            removed += 1
        except OSError:
            continue
    return removed
