"""The library on the left and the panel on the right can be folded away and brought back."""
from __future__ import annotations

from tonewright.audio.playback import Player
from tonewright.core.settings import Favorites
from tonewright.ui import icons
from tonewright.ui.main_window import MainWindow
from tonewright.ui.state import AppState

from test_audio_io import FakeOutput
from test_ui import _no_hidden_errors, loaded, make_files, pump, qapp, ui, wait_until  # noqa: F401 - fixtures


def shown(widget) -> bool:
    """Whether the widget is on (the window itself is never shown in tests, so `isVisible` would always be False)."""
    return not widget.isHidden()


def test_both_panels_start_open_with_a_button_for_each(ui):
    assert shown(ui.library) and shown(ui.right_wrap)
    assert list(ui.panel_buttons) == ["toggle_library", "toggle_side"]
    assert all(a.isCheckable() and a.isChecked() for a in (ui.actions["toggle_library"], ui.actions["toggle_side"]))
    assert not ui.actions["toggle_library"].icon().isNull() and not ui.actions["toggle_side"].icon().isNull()
    assert "panel-left" in icons.names() and "panel-right" in icons.names()


def test_the_buttons_fold_the_panels_away_and_bring_them_back(ui):
    ui.panel_buttons["toggle_library"].click()
    assert not shown(ui.library) and shown(ui.right_wrap) and not ui.actions["toggle_library"].isChecked()
    ui.panel_buttons["toggle_side"].click()
    assert not shown(ui.right_wrap) and not ui.actions["toggle_side"].isChecked()
    assert ui.state.settings.get("panels") == {"library": False, "side": False}
    ui.panel_buttons["toggle_library"].click()
    ui.panel_buttons["toggle_side"].click()
    assert shown(ui.library) and shown(ui.right_wrap)
    assert ui.state.settings.get("panels") == {"library": True, "side": True}


def test_the_keyboard_shortcuts_and_the_view_menu_do_the_same(ui):
    assert ui.actions["toggle_library"].shortcut().toString() == "Ctrl+B"
    assert ui.actions["toggle_side"].shortcut().toString() == "Ctrl+Shift+B"
    ui.actions["toggle_library"].trigger()
    assert not shown(ui.library)
    ui.actions["toggle_side"].trigger()
    assert not shown(ui.right_wrap)
    view = next(a.menu() for a in ui.menu_bar.actions() if a.menu() is not None and a.text().replace("&", "") in ("View", "Вигляд"))
    assert {ui.actions["toggle_library"], ui.actions["toggle_side"]} <= set(view.actions())
    ui.actions["toggle_library"].trigger()
    ui.actions["toggle_side"].trigger()
    assert shown(ui.library) and shown(ui.right_wrap)


def test_the_panels_stay_folded_when_the_mode_changes(ui):
    ui.panel_buttons["toggle_side"].click()
    for mode in ("convert", "multitrack", "audiobook", "edit"):
        ui.set_mode(mode)
        assert not shown(ui.right_wrap), mode
    ui.panel_buttons["toggle_library"].click()
    ui.set_mode("tags")
    assert not shown(ui.library)


def test_tags_mode_has_no_side_panel_and_the_choice_comes_back_afterwards(ui):
    ui.set_mode("edit")
    assert shown(ui.right_wrap) and ui.actions["toggle_side"].isEnabled()
    ui.set_mode("tags")
    assert not shown(ui.right_wrap) and not ui.actions["toggle_side"].isEnabled()
    assert ui.actions["toggle_side"].isChecked()                          # the choice itself was not touched
    ui.set_mode("edit")
    assert shown(ui.right_wrap) and ui.actions["toggle_side"].isEnabled()


def test_the_choice_and_the_widths_are_remembered_for_the_next_start(ui, qapp, tmp_path):
    ui.panel_buttons["toggle_library"].click()
    ui.splitter.setSizes([310, 700, 420])
    ui.close()
    saved = ui.state.settings.get("window")["sizes"]
    assert all(s > 0 for s in saved)                                      # a folded panel keeps the width it will open at
    again = MainWindow(AppState(ui.state.settings, Favorites(tmp_path / "fav2.json"), Player(stream_factory=FakeOutput)))
    try:
        assert not shown(again.library) and shown(again.right_wrap)
        assert not again.actions["toggle_library"].isChecked() and again.actions["toggle_side"].isChecked()
        assert again._panel_width["library"] > 0
        again.panel_buttons["toggle_library"].click()
        assert shown(again.library)
    finally:
        again.close()
        again.deleteLater()


def test_a_folded_panel_does_not_stop_the_work_in_the_middle(loaded, qapp):
    loaded.panel_buttons["toggle_library"].click()
    loaded.panel_buttons["toggle_side"].click()
    loaded.ctl.toggle_play()
    assert loaded.state.player.state == "playing"
    loaded.stage.set_selection(0, 1000)
    loaded.ctl.copy()
    assert loaded.state.clipboard is not None
    loaded.ctl.stop_playback()
