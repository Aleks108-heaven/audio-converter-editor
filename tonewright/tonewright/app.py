"""Application start-up."""
from __future__ import annotations

import logging
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QTimer
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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    selftest: str | None = None
    if "--selftest" in argv:                          # start up, draw once, save a screenshot, quit
        i = argv.index("--selftest")
        selftest = argv[i + 1]
        del argv[i:i + 2]
    _set_app_id()
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    _install_crash_handling()
    log.info("Tonewright %s starting", __version__)
    from .ui import icons, theme
    app.setWindowIcon(icons.app_icon())
    settings = Settings()
    log.info("Language: %s", i18n.set_language(settings.get("language")))
    i18n.install_qt_translations(app)
    theme.apply_theme(app, settings.get("theme"))
    if settings.get("ffmpeg_dir"):
        ffmpeg.set_tool_override("ffmpeg", str(Path(settings.get("ffmpeg_dir")) / "ffmpeg.exe"))
        ffmpeg.set_tool_override("ffprobe", str(Path(settings.get("ffmpeg_dir")) / "ffprobe.exe"))
    if not ffmpeg.available():
        QMessageBox.critical(None, APP_NAME, tr("Tonewright needs FFmpeg to read and write audio files, and could not find it.\n\n"
                             "Install it with:\n    winget install Gyan.FFmpeg\n\nthen start Tonewright again."))
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
