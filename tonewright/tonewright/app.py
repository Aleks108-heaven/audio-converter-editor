"""Application start-up."""
from __future__ import annotations

import logging
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from . import APP_NAME, __version__, i18n, paths
from .core import ffmpeg
from .core.settings import Settings
from .i18n import tr

log = logging.getLogger("tonewright")


def _set_app_id() -> None:
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Tonewright.AudioWorkbench")
    except Exception:
        pass


def _install_crash_handling() -> Path:
    """Log every uncaught error; keep the app running and tell the user instead of vanishing."""
    logfile = paths.config_dir() / "tonewright.log"
    handler = RotatingFileHandler(logfile, maxBytes=1_000_000, backupCount=2, encoding="utf-8")   # never grows without bound
    logging.basicConfig(handlers=[handler], level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    shown = {"busy": False}

    def hook(exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log.error("Uncaught exception:\n%s", text)
        if issubclass(exc_type, KeyboardInterrupt) or shown["busy"] or QApplication.instance() is None:
            return
        shown["busy"] = True
        try:
            box = QMessageBox(QMessageBox.Icon.Warning, APP_NAME, tr("Something went wrong, but Tonewright is still running."))
            box.setInformativeText(tr(f"{exc_type.__name__}: {exc}\n\nDetails were saved to:\n{logfile}"))
            box.exec()
        finally:
            shown["busy"] = False

    sys.excepthook = hook
    return logfile


class FileOpenRelay(QObject):
    """On macOS, files dropped on the Dock icon or opened with "Open With" arrive as events, not as arguments, and may
    arrive before the window exists. They are held until `deliver` is given somewhere to send them."""

    def __init__(self) -> None:
        super().__init__()
        self._pending: list[Path] = []
        self._target: Optional[Callable[[list[Path]], None]] = None

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt name
        if event.type() == QEvent.Type.FileOpen and event.file():
            path = Path(event.file())
            if path.is_file():
                if self._target is None:
                    self._pending.append(path)
                else:
                    self._target([path])
            return True
        return False

    def deliver(self, target: Callable[[list[Path]], None]) -> None:
        self._target = target
        if self._pending:
            target(self._pending)
            self._pending = []


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    selftest: str | None = None
    if "--selftest" in argv:                          # start up, draw once, save a screenshot, quit
        i = argv.index("--selftest")
        selftest = argv[i + 1]
        del argv[i:i + 2]
    _set_app_id()
    app = QApplication(argv)
    relay = FileOpenRelay()
    app.installEventFilter(relay)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    _install_crash_handling()
    log.info("Tonewright %s starting", __version__)
    from .ui import accessibility, icons, theme
    accessibility.install(app)                 # icon-only buttons take their tooltip as their name, for screen readers
    app.setWindowIcon(icons.app_icon())
    settings = Settings()
    log.info("Language: %s", i18n.set_language(settings.get("language")))
    i18n.install_qt_translations(app)
    theme.apply_theme(app, settings.get("theme"))
    if settings.get("ffmpeg_dir"):
        suffix = ".exe" if paths.IS_WINDOWS else ""
        ffmpeg.set_tool_override("ffmpeg", str(Path(settings.get("ffmpeg_dir")) / f"ffmpeg{suffix}"))
        ffmpeg.set_tool_override("ffprobe", str(Path(settings.get("ffmpeg_dir")) / f"ffprobe{suffix}"))
    if not ffmpeg.available():
        QMessageBox.critical(None, APP_NAME, tr(f"Tonewright needs FFmpeg to read and write audio files, and could not find it.\n\n"
                             f"Install it with:\n    {ffmpeg.install_hint()}\n\nthen start Tonewright again."))
        return 1
    paths.cleanup_stale_sessions()
    from .ui.main_window import MainWindow
    from .ui.state import AppState
    state = AppState(settings)
    win = MainWindow(state)
    win.show()
    files = [Path(a) for a in argv[1:] if Path(a).exists()]
    if files:
        win.ctl.open_paths(files)
    relay.deliver(win.ctl.open_paths)
    if selftest:
        def finish() -> None:
            win.grab().save(selftest)
            for item in state.items:
                if item.doc is not None:
                    item.doc.mark_saved()
            win.close()
            app.quit()
        QTimer.singleShot(1500, finish)
    return app.exec()
