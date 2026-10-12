"""Interface translation: the mechanism, the Ukrainian catalogue, and that no English text slips past tr()."""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from tonewright import i18n
from tonewright.audio import devices
from tonewright.core import analysis, generate
from tonewright.core import effects as fx
from tonewright.core.formats import EncodeSettings
from tonewright.ui import dialogs
from tonewright.ui.effect_dialog import EffectDialog

import i18n_audit
from test_ui import _no_hidden_errors, loaded, make_files, pump, qapp, ui  # noqa: F401 - fixtures

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import i18n_keys  # noqa: E402


@pytest.fixture(autouse=True)
def _english_afterwards():
    yield
    i18n.set_language("en")


@pytest.fixture(scope="module")
def keys() -> dict[str, set[str]]:
    return i18n_keys.all_keys()


@pytest.fixture
def pseudo_language():
    """List this fixture before `loaded`/`ui` so the window is built in the language under test."""
    i18n.set_language("pseudo")


@pytest.fixture
def ukrainian():
    i18n.set_language("uk")


def uk_catalogue() -> dict:
    from tonewright.locales import uk
    return uk.MESSAGES


# ------------------------------------------------------------------ the mechanism
def test_english_is_a_no_op():
    i18n.set_language("en")
    assert i18n.tr("Save") == "Save" and i18n.tr("") == "" and i18n.tr(None) is None


def test_exact_template_and_context_lookups():
    i18n.set_language("uk")
    assert i18n.tr("Save") == "Зберегти"
    assert i18n.tr("Convert") == "Конвертувати" and i18n.tr("Convert", "mode") == "Конвертація"
    assert i18n.tr("Edit", "mode") == "Редагування"                       # no special entry: falls back to the plain one
    assert i18n.tr("Applied Reverb") == "Застосовано: Reverb"             # template; the inserted text is kept as is
    assert i18n.tr("This text has no translation") == "This text has no translation"
    assert i18n.tr("44100 Hz") == "44100 Гц" and i18n.tr("16k Hz") == "16 кГц"


@pytest.mark.parametrize("n,form", [(0, 2), (1, 0), (2, 1), (4, 1), (5, 2), (11, 2), (12, 2), (14, 2), (21, 0), (22, 1),
                                    (25, 2), (101, 0), (111, 2), (112, 2)])
def test_ukrainian_plural_forms(n, form):
    assert i18n.plural_form(n, "uk") == form


def test_plural_templates_choose_the_right_word():
    i18n.set_language("uk")
    assert i18n.tr("Opened 1 file") == "Відкрито 1 файл"
    assert i18n.tr("Opened 3 files") == "Відкрито 3 файли"
    assert i18n.tr("Opened 5 files") == "Відкрито 5 файлів"
    assert i18n.tr("Opened 21 files") == "Відкрито 21 файл"
    assert i18n.tr("2 chapters.") == "2 розділи."
    assert i18n.tr("12 file(s) selected") == "12 file(s) selected"        # unknown wording is left alone


def test_language_resolution(monkeypatch):
    assert i18n.resolve("uk") == "uk" and i18n.resolve("en") == "en"
    assert i18n.resolve("xx") == "en" and i18n.resolve(None) in ("en", "uk")
    monkeypatch.setattr(i18n, "system_language", lambda: "uk")
    assert i18n.resolve("auto") == "uk"
    assert i18n.set_language("auto") == "uk"


def test_pseudo_language_marks_and_records():
    i18n.set_language("pseudo")
    assert i18n.tr("Save") == "⟦Save⟧" and "Save" in i18n.requested()


def test_qt_translations_install_for_ukrainian(qapp):
    i18n.set_language("uk")
    i18n.install_qt_translations(qapp)
    assert i18n._qt_translators, "qtbase_uk.qm (standard buttons) should have loaded"
    from PySide6.QtCore import QCoreApplication
    assert QCoreApplication.translate("QPlatformTheme", "Cancel") != "Cancel"
    i18n.set_language("en")
    i18n.install_qt_translations(qapp)
    assert not i18n._qt_translators


# ------------------------------------------------------------------ the Ukrainian catalogue
def test_every_string_the_app_shows_is_translated(keys):
    i18n.set_language("uk")
    catalogue = uk_catalogue()
    missing = sorted(k for k in keys if k not in catalogue and i18n.tr(k) == k)
    assert not missing, f"{len(missing)} strings have no Ukrainian text, e.g. {missing[:8]}"


def test_placeholders_and_plural_forms_are_consistent():
    problems = []
    for key, value in uk_catalogue().items():
        slots = key.count("{}")
        forms = value if isinstance(value, tuple) else (value,)
        if isinstance(value, tuple) and len(value) != 3:
            problems.append(f"{key!r}: plural needs 3 forms")
        for form in forms:
            if not form.strip():
                problems.append(f"{key!r}: empty translation")
            used = {int(n) for n in re.findall(r"\{(\d+)\}", form)}
            if any(n >= slots for n in used):
                problems.append(f"{key!r}: uses {{{max(used)}}} but the English has {slots} placeholders")
            if slots and not used and not isinstance(value, tuple) and "{" not in key.replace("{}", ""):
                if key.strip("{}") and "{}" in key and not any(k in key for k in ("{} file{}", "{} name{}", "{} edit{}")):
                    problems.append(f"{key!r}: no placeholder used")
        if key.count("{}") and "\n" in key and key.count("\n") != forms[0].count("\n"):
            problems.append(f"{key!r}: line breaks differ")
        if not isinstance(value, tuple) and key.endswith(("…", "...")) != value.endswith(("…", "...")) \
                and not key.endswith(("\n\n", ":")):
            problems.append(f"{key!r}: ellipsis differs")
    assert not problems, problems[:10]


def test_translated_menu_and_button_texts_have_no_dangling_ampersands():
    for key, value in uk_catalogue().items():
        if isinstance(value, str) and "&" in value and "&&" not in value:
            pytest.fail(f"{key!r} -> {value!r}: a single & becomes a keyboard accelerator in Qt")


def test_no_translation_runs_at_import_time():
    """tr() at module level or in a default argument would freeze English before the language is chosen."""
    bad = []
    for path in sorted((ROOT / "tonewright").rglob("*.py")):
        if path.name == "i18n.py":
            continue
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        parents = {id(c): n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "tr":
                cur, at_runtime = parents.get(id(n)), False
                while cur is not None:
                    if isinstance(cur, ast.arguments):
                        break
                    if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                        at_runtime = True
                        break
                    cur = parents.get(id(cur))
                if not at_runtime:
                    bad.append(f"{path.name}:{n.lineno}")
    assert not bad, bad


def test_every_module_that_calls_tr_imports_it():
    for path in sorted((ROOT / "tonewright").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        uses = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "tr" for n in ast.walk(tree))
        has = any(isinstance(n, ast.ImportFrom) and any(a.name == "tr" for a in n.names) for n in ast.walk(tree))
        assert not uses or has or path.name == "i18n.py", f"{path.name} calls tr() without importing it"


# ------------------------------------------------------------------ the running interface
LATIN_OK = {
    "Tonewright", "FFmpeg", "ffmpeg", "PySide6", "NumPy", "Windows", "PortAudio", "Qt", "Apple", "Android", "Vorbis", "Opus",
    "MPEG", "Layer", "PCM", "ADTS", "AIFF", "FLAC", "WAV", "WMA", "MP3", "MP2", "AAC", "AMR", "OGG", "M4A", "M4B", "RMS", "LUFS",
    "dBFS", "dBTP", "LU", "Ctrl", "Shift", "Alt", "Space", "Esc", "Home", "End", "Del", "PgUp", "PgDown", "Left", "Right",
    "DTMF", "FFT", "EBU", "DC", "Media", "Audio", "Hz", "kHz", "kbps", "mp4", "Test", "Voice", "Mic", "Input", "float", "bit",
}
LATIN = re.compile(r"[A-Za-z]{3,}")


DATA = re.compile(r"\{\w+\}|\S*[\\/_=]\S*|\w*\d\w*|\bwww\b|\bfull\b|\bbuild\b|\bgyan\b|\bdev\b")      # pattern tokens, paths, numbers


def _leaks(found: dict[str, set[str]]) -> dict[str, set[str]]:
    out = {}
    for text, where in found.items():
        words = [w for w in LATIN.findall(DATA.sub(" ", text)) if w not in LATIN_OK and w.lower() not in ("track", "lowpass", "volume", "aecho")]
        if words:
            out[text] = where
    return out


def _fake_devices(monkeypatch):
    monkeypatch.setattr(devices, "input_devices", lambda: [])
    monkeypatch.setattr(generate, "list_voices", lambda: ["Test Voice"])


def _all_dialogs(win, tmp_path, titles) -> list:
    made = []
    for effect in fx.all_effects():
        made.append(EffectDialog(effect, None, win.ctl._effect_context(effect), win))
    doc = win.ctl.doc
    t = titles
    made += [
        dialogs.ParamsDialog(t("Tone"), t("Generates a steady tone, a sweep or a test signal."), dialogs.TONE_PARAMS, None, win,
                             targets=win.ctl._target_list()),
        dialogs.ParamsDialog(t("Silence"), "", dialogs.SILENCE_PARAMS, None, win),
        dialogs.ParamsDialog(t("Noise"), "", dialogs.NOISE_PARAMS, None, win),
        dialogs.ParamsDialog(t("Dial tones"), "", dialogs.DTMF_PARAMS, None, win),
        dialogs.ParamsDialog(t("Find silence"), "", dialogs.SILENCE_TOOL_PARAMS, None, win),
        dialogs.ParamsDialog(t("Convert file format"), "", dialogs.CONVERT_PARAMS, None, win),
        dialogs.ParamsDialog(t("Mix paste"), "", dialogs.MIX_PARAMS, None, win),
        dialogs.SpeechDialog(win), dialogs.RecordDialog(win), dialogs.AboutDialog(win),
        dialogs.ExportDialog(win, tmp_path / "x.mp3", EncodeSettings(), 44100, 2, has_selection=True),
        dialogs.AnalyzeDialog(analysis.measure(doc.snapshot(), 0, doc.frames), "x", win),
        dialogs.SpectrumDialog(doc.snapshot(), 0, doc.frames, "x", win),
    ]
    return made


def _walk_interface(win, qapp, tmp_path, titles) -> dict[str, set[str]]:
    seen: dict[str, set[str]] = {}

    def merge(label: str, found: dict[str, set[str]]) -> None:
        for text, where in found.items():
            seen.setdefault(text, set()).update(f"{label}:{w}" for w in where)

    for mode in ("edit", "convert", "tags", "audiobook"):
        win.set_mode(mode)
        if mode == "tags":
            win.library.tree.selectAll()
        if mode == "audiobook":
            win.preview_chapters()
        pump(qapp, 30)
        merge(mode, i18n_audit.collect([win], win.actions.values()))
    win.set_mode("edit")
    win.stage.set_selection(1000, 40000)
    pump(qapp, 30)
    merge("selection", i18n_audit.collect([win], win.actions.values()))
    for dlg in _all_dialogs(win, tmp_path, titles):
        merge(type(dlg).__name__, i18n_audit.collect([dlg]))
        dlg.deleteLater()
    return seen


def test_no_visible_english_bypasses_tr(pseudo_language, loaded, qapp, tmp_path, monkeypatch):
    """In the pseudo language every translated string carries ⟦ ⟧, so any English left unmarked was missed."""
    _fake_devices(monkeypatch)
    seen = _walk_interface(loaded, qapp, tmp_path, titles=i18n.tr)
    ignored = {"English", "Test Voice"}
    left = {t: w for t, w in seen.items() if t not in ignored}
    assert not left, "\n".join(f"{t!r}  {sorted(w)[:2]}" for t, w in sorted(left.items()))


def test_ukrainian_interface_has_no_english_left(ukrainian, loaded, qapp, tmp_path, monkeypatch):
    """Run the real interface in Ukrainian and look for Latin-letter words that are not names or units."""
    _fake_devices(monkeypatch)
    seen = _walk_interface(loaded, qapp, tmp_path, titles=i18n.tr)
    leaks = _leaks({t: w for t, w in seen.items() if t not in {"English"}})
    assert not leaks, "\n".join(f"{t!r}  {sorted(w)[:2]}" for t, w in sorted(leaks.items()))


def test_main_window_speaks_ukrainian(ukrainian, loaded):
    win = loaded
    assert [b.text() for b in win.mode_buttons.values()] == ["Конвертація", "Редагування", "Теги", "Аудіокнига"]
    assert win.actions["save"].text() == "Зберегти" and win.actions["undo"].text() == "Скасувати"
    assert [a.text() for a in win.menu_bar.actions()][:3] == ["Файл", "Редагування", "Вигляд"]


def test_language_menu_saves_the_choice(loaded, qapp, monkeypatch):
    win = loaded
    shown = []
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: shown.append(a[2])))
    win._set_language("uk")
    assert win.state.settings.get("language") == "uk" and shown
    shown.clear()
    win._set_language("uk")                       # choosing the same language again does nothing
    assert not shown


def test_cover_art_filters_do_not_offer_webp():
    for path in (ROOT / "tonewright" / "ui").glob("*.py"):
        assert "*.webp" not in path.read_text(encoding="utf-8"), path.name
