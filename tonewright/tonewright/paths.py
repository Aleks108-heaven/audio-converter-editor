"""Where Tonewright keeps settings and scratch audio."""
from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from . import APP_NAME

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

_session_dir: Optional[Path] = None
_lock_handle = None


def config_dir() -> Path:
    """Settings, favourites, the log and cached images: always in the user's own folder, never next to the program."""
    if IS_MAC:
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def system_program(*candidates: str) -> Optional[str]:
    """The first of these absolute paths that exists (macOS/Linux programs are never looked up by bare name)."""
    return next((c for c in candidates if os.path.isfile(c)), None)


def reveal_in_file_manager(path: Path) -> None:
    """Show a file selected in Explorer / Finder."""
    if IS_WINDOWS:
        subprocess.Popen([explorer_exe(), "/select,", str(path)])
    elif IS_MAC:
        subprocess.Popen(["/usr/bin/open", "-R", str(path)])
    else:
        opener = system_program("/usr/bin/xdg-open")
        if opener:
            subprocess.Popen([opener, str(Path(path).parent)])


def open_folder(folder: Path) -> None:
    """Open a folder in Explorer / Finder."""
    if IS_WINDOWS:
        os.startfile(str(folder))  # type: ignore[attr-defined]
    elif IS_MAC:
        subprocess.Popen(["/usr/bin/open", str(folder)])
    else:
        opener = system_program("/usr/bin/xdg-open")
        if opener:
            subprocess.Popen([opener, str(folder)])


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
        if not IS_WINDOWS:
            import fcntl
            fcntl.flock(_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)     # held until this process ends
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


def _session_is_running(lock: Path) -> bool:
    """True while another process still holds the session's lock file."""
    if not lock.exists():
        return False
    if IS_WINDOWS:
        try:
            lock.unlink()           # fails on Windows while the owning process holds it open
            return False
        except OSError:
            return True
    import fcntl                    # elsewhere a file can be deleted while open, so ask the lock instead
    try:
        with open(lock, "rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
    except BlockingIOError:
        return True
    except OSError:
        return False


def cleanup_stale_sessions() -> int:
    """Delete scratch folders from sessions that are no longer running."""
    root = _scratch_root()
    removed = 0
    if not root.exists():
        return 0
    for child in root.iterdir():
        if not child.is_dir() or child == _session_dir:
            continue
        try:
            if _session_is_running(child / ".lock"):
                continue
            shutil.rmtree(child)
            removed += 1
        except OSError:
            continue
    return removed
