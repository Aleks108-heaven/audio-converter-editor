"""Interface translation.

The English text in the code is the key, so nothing breaks when a translation is missing: ``tr("Save")`` simply
returns "Save" until a catalogue has an entry for it. Catalogues live in ``tonewright/locales/<code>.py`` as a
``MESSAGES`` dict:

* ``"Save": "Зберегти"`` - an exact match.
* ``"Opened {} file{}": "Відкрито файлів: {0}"`` - a template. Each ``{}`` in the key matches any text (it is how
  f-strings such as ``f"Opened {n} file{s}"`` are found again at run time) and ``{0}``, ``{1}``... in the value
  insert what was matched.
* a tuple of three values instead of a string selects the singular / few / many form from the first number matched
  (Ukrainian counts need all three: 1 файл, 2 файли, 5 файлів).
"""
from __future__ import annotations

import importlib
import os
import re
import sys
from functools import lru_cache
from typing import Callable, Optional, Union

DEFAULT_LANGUAGE = "en"
LANGUAGES: tuple[tuple[str, str], ...] = (("en", "English"), ("uk", "Українська"))
PSEUDO = "pseudo"                     # test aid: wraps every translated string in ⟦ ⟧ so unmarked text stands out

Value = Union[str, tuple]

_language = DEFAULT_LANGUAGE
_exact: dict[str, Value] = {}
_templates: list[tuple[re.Pattern, Value]] = []
_requested: set[str] = set()          # only filled in pseudo mode
_listeners: list[Callable[[str], None]] = []

_PLACEHOLDER = re.compile(r"\{(\d+)\}")


# ------------------------------------------------------------------ choosing a language
def language() -> str:
    return _language


def language_name(code: str) -> str:
    return dict(LANGUAGES).get(code, code)


def system_language() -> str:
    """The system display language when we have a catalogue for it, otherwise English."""
    names: list[str] = []
    try:
        import ctypes
        import locale
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()          # type: ignore[attr-defined]
        names.append(locale.windows_locale.get(langid, ""))
    except Exception:
        pass
    if sys.platform != "win32":
        try:
            from PySide6.QtCore import QLocale
            names += list(QLocale.system().uiLanguages())                  # macOS: the languages in System Settings, in order
        except Exception:
            pass
    names += [os.environ.get(v, "") for v in ("LC_ALL", "LC_MESSAGES", "LANG")]
    known = {code for code, _ in LANGUAGES}
    for name in names:
        code = name.replace("-", "_").split("_")[0].lower()
        if code in known:
            return code
    return DEFAULT_LANGUAGE


def resolve(setting: Optional[str]) -> str:
    """'auto' (or nothing) means follow Windows; unknown codes fall back to English."""
    if not setting or setting == "auto":
        return system_language()
    if setting == PSEUDO or setting in dict(LANGUAGES):
        return setting
    return DEFAULT_LANGUAGE


def set_language(setting: Optional[str]) -> str:
    """Switch the catalogue. Returns the language actually in use."""
    global _language, _exact, _templates
    code = resolve(setting)
    messages: dict[str, Value] = {}
    if code not in (DEFAULT_LANGUAGE, PSEUDO):
        module = importlib.import_module(f"{__package__}.locales.{code}")
        messages = dict(module.MESSAGES)
    _language = code
    _exact = {k: v for k, v in messages.items() if "{}" not in k}
    templates = [(k, v) for k, v in messages.items() if "{}" in k]
    templates.sort(key=lambda kv: -len(kv[0].replace("{}", "")))             # most specific wording first
    _templates = [(_compile(k), v) for k, v in templates]
    _lookup.cache_clear()
    _requested.clear()
    for listener in list(_listeners):
        listener(code)
    return code


def on_change(listener: Callable[[str], None]) -> None:
    _listeners.append(listener)


# ------------------------------------------------------------------ translating
def _compile(key: str) -> re.Pattern:
    return re.compile("^" + "(.*?)".join(re.escape(part) for part in key.split("{}")) + "$", re.DOTALL)


def plural_form(n: int, language_code: Optional[str] = None) -> int:
    """Index into a (one, few, many) triple."""
    n = abs(int(n))
    if (language_code or _language) == "uk":
        if n % 10 == 1 and n % 100 != 11:
            return 0
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return 1
        return 2
    return 0 if n == 1 else 2


def _fill(value: Value, groups: tuple[str, ...]) -> str:
    if isinstance(value, tuple):
        n = next((int(g) for g in groups if re.fullmatch(r"-?\d+", g.strip())), 0)
        value = value[min(plural_form(n), len(value) - 1)]
    return _PLACEHOLDER.sub(lambda m: groups[int(m.group(1))] if int(m.group(1)) < len(groups) else m.group(0), value)


@lru_cache(maxsize=8192)
def _lookup(text: str, context: str = "") -> str:
    if context:
        hit = _exact.get(f"{context}|{text}")
        if hit is not None:
            return _fill(hit, ())
    hit = _exact.get(text)
    if hit is not None:
        return _fill(hit, ())
    for pattern, value in _templates:
        m = pattern.match(text)
        if m:
            return _fill(value, m.groups())
    return text


def tr(text, context: str = ""):
    """Translate `text` into the current language (non-strings and empty text pass through untouched).

    `context` picks between translations of the same English word that need different wording, e.g.
    ``tr("Convert", "mode")`` (a tab: "Конвертер") versus ``tr("Convert")`` (a button: "Конвертувати")."""
    if _language == DEFAULT_LANGUAGE or not isinstance(text, str) or not text:
        return text
    if _language == PSEUDO:
        _requested.add(text)
        return "⟦" + text + "⟧"
    return _lookup(text, context)


_WIDTH_SCALE = {"uk": 1.15}


def width_scale() -> float:
    """How much wider fixed-width label columns should be: Ukrainian words run about a third longer."""
    return _WIDTH_SCALE.get(_language, 1.0)


def requested() -> frozenset[str]:
    """Every distinct string that went through tr() in pseudo mode (test aid)."""
    return frozenset(_requested)


def count(n: int, one: str, few: str, many: str) -> str:
    """'3 files' in the current language, for places that build the number themselves."""
    return (one, few, many)[plural_form(n)].format(n=n)


# ------------------------------------------------------------------ Qt's own texts (Yes/No/Cancel, file dialogs)
_qt_translators: list = []


def install_qt_translations(app) -> None:
    """Load Qt's bundled catalogue for the active language so standard buttons and dialogs match."""
    from PySide6.QtCore import QCoreApplication, QLibraryInfo, QLocale, QTranslator
    for translator in _qt_translators:
        QCoreApplication.removeTranslator(translator)
    _qt_translators.clear()
    if _language in (DEFAULT_LANGUAGE, PSEUDO):
        return
    folder = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    for name in ("qtbase", "qt"):
        translator = QTranslator(app)
        if translator.load(QLocale(_language), name, "_", folder):
            QCoreApplication.installTranslator(translator)
            _qt_translators.append(translator)
