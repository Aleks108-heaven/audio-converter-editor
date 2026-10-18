"""Why a file could not be written, in terms a person can act on.

When FFmpeg cannot open the file it was asked to write it says "No such file or directory", whatever the real reason.
On Windows 10 and 11 the usual real reason is Windows Security's *Controlled folder access* (ransomware protection):
it lets only approved programs change files in Documents, Music, Videos, Pictures and Desktop, and it blocks the rest
without a word. Each program is judged on its own, so FFmpeg can be blocked while Tonewright itself is allowed. The
fix is a setting only the user can change, so the job here is to recognise the situation and say exactly what to do.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .. import paths
from ..i18n import tr
from . import ffmpeg

CFA_LINK = "windowsdefender://ransomwareprotection"        # opens the Ransomware protection page of Windows Security
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CFA_MODES_THAT_BLOCK = (1, 3)                              # 1 = on, 3 = block disk modification only (2 and 4 only log)
_cfa_cache: tuple[float, Optional[bool]] = (0.0, None)


@dataclass(frozen=True)
class WriteProblem:
    kind: str                         # see KINDS
    folder: Path
    programs: tuple[str, ...] = ()    # what Windows Security has to be told to allow
    reason: str = ""                  # the operating system's own words, when it gave any


KINDS = ("folder-missing", "windows-security", "ffmpeg-blocked", "not-writable", "unknown")


def controlled_folder_access() -> Optional[bool]:
    """True if Controlled folder access is blocking, False if it is not (or not Windows), None if it cannot be told.
    Asks Windows Defender once and remembers the answer for half a minute."""
    global _cfa_cache
    if not paths.IS_WINDOWS:
        return False
    stamp, value = _cfa_cache
    if time.monotonic() - stamp < 30 and stamp:
        return value
    answer: Optional[bool] = None
    try:
        out = subprocess.run([paths.powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                              "-Command", "(Get-MpPreference).EnableControlledFolderAccess"],
                             capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW,
                             stdin=subprocess.DEVNULL).stdout.strip()
        answer = int(out) in _CFA_MODES_THAT_BLOCK
    except (OSError, ValueError, subprocess.SubprocessError):
        answer = None
    _cfa_cache = (time.monotonic(), answer)
    return answer


def _can_write_here(folder: Path) -> tuple[bool, str]:
    """Whether this program can create a file in `folder` (a probe file is made and removed at once)."""
    probe = folder / f".tonewright-write-test-{os.getpid()}"
    try:
        with open(probe, "wb") as handle:
            handle.write(b"x")
        probe.unlink()
        return True, ""
    except OSError as exc:
        return False, exc.strerror or str(exc)


def programs_to_allow() -> tuple[str, ...]:
    """The programs that write files, as Windows Security knows them: Tonewright's own Python and FFmpeg."""
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe" and exe.with_name("pythonw.exe").is_file():
        exe = exe.with_name("pythonw.exe")                  # the program that actually runs the app
    out = [str(exe)]
    try:
        out.append(ffmpeg.find_tool("ffmpeg"))
    except ffmpeg.FFmpegMissing:
        pass
    return tuple(out)


def diagnose(out_path: str | os.PathLike, detail: str = "") -> WriteProblem:
    """Work out why `out_path` could not be written. `detail` is what FFmpeg or the system said."""
    folder = Path(out_path).parent
    if not folder.is_dir():
        return WriteProblem("folder-missing", folder, reason=detail)
    writable, reason = _can_write_here(folder)
    blocking = controlled_folder_access() if paths.IS_WINDOWS else False
    if not writable:
        kind = "windows-security" if blocking else "not-writable"
        return WriteProblem(kind, folder, programs_to_allow() if blocking is not False else (), reason or detail)
    # this program can write there, so the other one (FFmpeg) is being stopped
    if ffmpeg.opening_output_failed(detail) and blocking is not False and paths.IS_WINDOWS:
        return WriteProblem("ffmpeg-blocked", folder, programs_to_allow(), detail)
    return WriteProblem("unknown", folder, reason=detail)


def one_line(problem: WriteProblem, name: str = "") -> str:
    """A single sentence for a list row or a status bar, in the interface language."""
    where = problem.folder.name or str(problem.folder)
    if problem.kind == "folder-missing":
        return tr(f"The folder {problem.folder} does not exist.")
    if problem.kind in ("windows-security", "ffmpeg-blocked"):
        return tr(f"Windows Security (Controlled folder access) is blocking Tonewright from saving to {where}. "
                  "Allow it under Ransomware protection, or save somewhere else.")
    if problem.kind == "not-writable":
        return tr(f"Tonewright is not allowed to write to {where}: {problem.reason}") if problem.reason \
            else tr(f"Tonewright is not allowed to write to {where}.")
    return tr(f"Could not write {name or tr('the file')}: {problem.reason}") if problem.reason \
        else tr(f"Could not write {name or tr('the file')}.")
