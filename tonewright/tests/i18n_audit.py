"""Find user-visible English text that does not go through tr().

With the pseudo language active every translated string is wrapped in ⟦ ⟧, so any visible text that still reads as
plain English was missed. Used by test_i18n.py and runnable by hand (see the test for how the windows are built).
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QAbstractButton, QComboBox, QGroupBox, QLabel, QLineEdit, QListWidget, QMenu,
                               QTableWidget, QTabBar, QTreeWidget, QWidget)

WORDS = re.compile(r"[A-Za-z]{3,}")
TAGS = re.compile(r"<[^>]+>")
# text that is data or a product name, not interface wording
IGNORE_EXACT = {"Tonewright", "FFmpeg", "PySide6", "NumPy", "MP3", "M4A", "M4B", "AAC", "OGG", "Opus", "WMA", "FLAC", "WAV",
                "AIFF", "MP2", "AMR", "AMR-WB", "LUFS", "dBFS", "dBTP", "RMS", "PNG", "JPG", "OK"}
IGNORE_RE = [re.compile(p) for p in (
    r"^[\d\s:.,%\-+/·×x→–—\[\]()#*]+$", r"^track\d+(\.\w+)?$", r"^\w+ ?\d*\.(wav|mp3|flac|m4a|ogg)$",
    r"^(Ctrl|Shift|Alt)\+", r"^-?\d+(\.\d+)?\s?(Hz|kHz|dB|dBFS|dBTP|ms|s|kbps|%|LUFS|LU)$", r"^Tonewright \d", r"^\W*$",
)]


def _texts(widget: QWidget) -> Iterable[tuple[str, str]]:
    kind = type(widget).__name__
    if isinstance(widget, QLabel):
        yield kind, widget.text()
    if isinstance(widget, QAbstractButton):
        yield kind, widget.text()
    if isinstance(widget, QLineEdit):
        yield kind + ".placeholder", widget.placeholderText()
    if isinstance(widget, QComboBox):
        for i in range(widget.count()):
            yield kind + ".item", widget.itemText(i)
    if isinstance(widget, QGroupBox):
        yield kind, widget.title()
    if isinstance(widget, QTabBar):
        for i in range(widget.count()):
            yield kind + ".tab", widget.tabText(i)
    if isinstance(widget, QTableWidget):
        for i in range(widget.columnCount()):
            h = widget.horizontalHeaderItem(i)
            if h is not None:
                yield kind + ".header", h.text()
    if isinstance(widget, QTreeWidget):
        h = widget.headerItem()
        for i in range(widget.columnCount()):
            yield kind + ".header", h.text(i)
    if isinstance(widget, QListWidget):
        for i in range(widget.count()):
            yield kind + ".item", widget.item(i).text()
    yield kind + ".tooltip", widget.toolTip()
    if widget.isWindow():
        yield kind + ".title", widget.windowTitle()


def _menu_texts(menu: QMenu, path: str) -> Iterable[tuple[str, str]]:
    for act in menu.actions():
        if act.isSeparator():
            continue
        yield "QAction " + path, act.text()
        yield "QAction.tooltip " + path, act.toolTip()
        if act.menu() is not None:
            yield from _menu_texts(act.menu(), path + "/" + act.text())


def collect(roots: Iterable[QWidget], actions: Iterable[QAction] = ()) -> dict[str, set[str]]:
    """{text: {where, ...}} for every visible string that looks like English and is not marked."""
    found: dict[str, set[str]] = defaultdict(set)

    def consider(where: str, text: str) -> None:
        text = TAGS.sub(" ", text or "").replace("&&", "&").replace("&", "").strip()
        if not text or "⟦" in text or not WORDS.search(text) or text in IGNORE_EXACT:
            return
        if any(r.match(text) for r in IGNORE_RE):
            return
        found[text].add(where)

    for root in roots:
        for w in [root, *root.findChildren(QWidget)]:
            for where, text in _texts(w):
                consider(where, text)
            if isinstance(w, QMenu):
                for where, text in _menu_texts(w, w.title()):
                    consider(where, text)
        bar = getattr(root, "menuBar", None)
        if bar is not None:
            for top in bar().actions():
                consider("QMenuBar", top.text())
                if top.menu() is not None:
                    for where, text in _menu_texts(top.menu(), top.text()):
                        consider(where, text)
    for act in actions:
        consider("QAction(window)", act.text())
        consider("QAction(window).tooltip", act.toolTip())
    return found
