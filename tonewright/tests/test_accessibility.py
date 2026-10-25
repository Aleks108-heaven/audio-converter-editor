"""A screen reader needs a name for every control. Icon-only buttons and bare form fields were announced as nothing."""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import (QAbstractButton, QAbstractSlider, QAbstractSpinBox, QComboBox, QLabel, QLineEdit, QPlainTextEdit,
                               QMenuBar, QPushButton, QScrollBar, QToolButton, QWidget)

from tonewright.ui import accessibility

from test_ui import _no_hidden_errors, loaded, make_files, pump, qapp, ui, wait_until  # noqa: F401 - fixtures


@pytest.fixture(autouse=True)
def _naming_is_on(qapp):
    accessibility.install(qapp)                 # the program does this once at start-up


def test_plain_turns_a_tooltip_into_a_name():
    assert accessibility.plain("Search every command  (Ctrl+K)") == "Search every command"
    assert accessibility.plain("Remove from library  (Del)") == "Remove from library"
    assert accessibility.plain("Split the clip at the playhead (S)") == "Split the clip at the playhead (S)"   # not a shortcut hint we know
    assert accessibility.plain("&Open…") == "Open…" and accessibility.plain("Output folder:") == "Output folder"


def test_an_icon_only_button_takes_its_tooltip_as_its_name(qapp):
    button = QToolButton()
    button.setToolTip("Mute")
    button.ensurePolished()
    assert button.accessibleName() == "Mute"
    button.setToolTip("Unmute")                  # the tooltip changes with the state, and the name follows
    assert button.accessibleName() == "Unmute"


def test_a_name_somebody_chose_is_never_replaced(qapp):
    button = QToolButton()
    button.setAccessibleName("Volume")
    button.setToolTip("Playback volume")
    button.ensurePolished()
    assert button.accessibleName() == "Volume"


def test_a_button_with_text_keeps_the_text_as_its_name(qapp):
    button = QPushButton("Save")
    button.setToolTip("Write the file")
    button.ensurePolished()
    assert button.accessibleName() == ""         # the screen reader reads the text itself


def test_label_for_names_the_field_and_makes_the_label_its_buddy(qapp):
    from tonewright.ui.convert_panel import _row
    field = QLineEdit()
    row = _row("Pattern", field)
    label = row.findChild(QLabel)
    assert field.accessibleName() == "Pattern" and label.buddy() is field
    wrapper = QWidget()
    inner = QComboBox(wrapper)
    assert _row("Quality", wrapper) and inner.accessibleName() == "Quality"           # a field inside a wrapper is found


@pytest.mark.parametrize("mode", ["edit", "convert", "multitrack", "tags", "audiobook"])
def test_every_control_on_every_page_has_a_name(loaded, qapp, mode):
    loaded.set_mode(mode)
    pump(qapp, 50)
    loaded.grab()
    unnamed = []
    for w in loaded.findChildren(QWidget):
        if not w.isVisibleTo(loaded) or isinstance(w, QScrollBar) or isinstance(w.parentWidget(), QMenuBar):   # Qt's own overflow arrow
            continue
        if isinstance(w, QLineEdit) and (w.objectName() == "qt_spinbox_lineedit" or isinstance(w.parentWidget(), QComboBox)):
            continue                              # the inside of a spin box or an editable combo box: the outer one is named
        if isinstance(w, QAbstractButton):
            named = w.text().strip() or w.accessibleName().strip()
        elif isinstance(w, (QAbstractSlider, QAbstractSpinBox, QComboBox, QLineEdit, QPlainTextEdit)):
            named = w.accessibleName().strip()
        else:
            continue
        if not named:
            unnamed.append(f"{type(w).__name__} tooltip={w.toolTip()!r} in {type(w.parentWidget()).__name__}")
    assert not unnamed, "\n".join(unnamed)
