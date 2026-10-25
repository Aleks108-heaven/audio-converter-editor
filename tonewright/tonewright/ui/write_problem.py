"""The explanation shown when a file could not be written: the real reason and, for Windows Security, what to allow."""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox, QWidget

from .. import paths
from ..core import access
from ..i18n import tr


def show_write_problem(parent: Optional[QWidget], title: str, problem: access.WriteProblem) -> None:
    """A file could not be written. Say why, and for Windows Security (Controlled folder access) say what to allow and
    offer to open the page where that is done."""
    folder = problem.folder
    blocked = problem.kind in access.BLOCKED_KINDS
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(tr(title))
    box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    if problem.kind == "folder-missing":
        box.setText(tr(f"The folder {folder} does not exist."))
        box.setInformativeText(tr("Choose another folder, or create this one."))
    elif blocked:
        box.setText(tr(f"Windows Security blocked Tonewright from saving to {folder}."))
        box.setInformativeText(
            tr("This is Controlled folder access, the ransomware protection in Windows Security. It only lets programs "
               "you have approved change files in Documents, Music, Videos, Pictures and Desktop.\n\nTo allow "
               "Tonewright: open Windows Security, then Virus & threat protection > Ransomware protection > Allow an "
               "app through Controlled folder access > Add an allowed app > Browse all apps, and add these two "
               "programs:") + "\n\n" + "\n".join(problem.programs)
            + "\n\n" + tr("Then save again. You can also save into a folder that is not protected."))
    elif problem.kind == "not-writable":
        box.setText(tr(f"Tonewright is not allowed to write to {folder}."))
        hint = (tr("On a Mac, allow it under System Settings > Privacy & Security > Files and Folders.")
                if paths.IS_MAC else tr("Check that the folder is not read-only, or choose another folder."))
        box.setInformativeText((problem.reason + "\n\n" if problem.reason else "") + hint)
    else:
        box.setText(tr(f"Could not write to {folder}."))
        box.setInformativeText((problem.reason or "").strip()[:700])
    open_security = box.addButton(tr("Open Windows Security"), QMessageBox.ButtonRole.ActionRole) if blocked else None
    box.addButton(QMessageBox.StandardButton.Ok)
    box.exec()
    if open_security is not None and box.clickedButton() is open_security:
        try:
            os.startfile(access.CFA_LINK)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass
