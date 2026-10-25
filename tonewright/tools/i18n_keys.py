"""Every English string that has to exist in a translation catalogue.

    .venv\\Scripts\\python.exe tools\\i18n_keys.py [--missing uk]

Two sources: the tr(...) calls in the code (found with the AST; an f-string becomes a template with {} for each
value) and the registries whose text is translated where it is displayed (effects, formats, option lists).
"""
from __future__ import annotations

import ast
import importlib
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "tonewright"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DOCUMENT_LABELS = ["Open", "Delete", "Paste", "Cut", "Trim to Selection", "Silence", "Add marker", "Remove marker",
                   "Rename marker", "Edit markers", "Untitled", "Original"]
ACTION_TEXTS = ["Waveform", "Spectral", "Envelope", "Waveform view", "Spectral view", "Envelope view",
                "Silence…", "Tone…", "Noise…", "Dial tones…", "Speech…", "B", "KB", "MB", "GB",
                # multitrack: drag labels used as undo steps, default line names
                "Move clip", "Trim clip", "Clip volume", "Move effect", "Resize effect", "Edit", "Line {}",
                # names a screen reader speaks for fields whose label sits beside them (set through a variable, so not found in the code)
                "Selection start", "Selection end", "Selection length", "Track number", "Total tracks", "Disc number",
                "Total discs",
                # words that need their own wording as a document name or a short button
                "document|Mix", "short|Mute", "menu|Mix", "mode|Edit"]
ERROR_CALLEES = {"ItemResult", "FileExistsError", "ValueError", "RuntimeError", "AudioDeviceError", "FFmpegError",
                 "FFmpegMissing", "KeyError", "OSError"}


def template(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    return None


def _callee(call: ast.Call) -> str:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")


def static_keys() -> dict[str, set[str]]:
    keys: dict[str, set[str]] = defaultdict(set)
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _callee(node)
            if name == "tr" and node.args:
                text = template(node.args[0])
                if text:
                    keys[text].add(f"{rel}:{node.lineno}")
            elif name == "begin_edit" and node.args:                    # undo-step labels, translated when shown
                text = template(node.args[0])
                if text:
                    keys[text].add(f"{rel}:{node.lineno}")
            elif name == "act" and path.name == "main_window.py":       # action text and tooltip are translated in _act
                picks = [node.args[i] for i in (1, 5) if i < len(node.args)] + [k.value for k in node.keywords if k.arg == "tip"]
                for arg in picks:
                    text = template(arg)
                    if text and not text.startswith("{"):
                        keys[text].add(f"{rel}:{node.lineno}")
            elif name in ERROR_CALLEES and path.parts[-2] in ("core", "audio", "ui"):
                args = list(node.args) + [k.value for k in node.keywords if k.arg == "message"]
                for arg in args[:1] if name != "ItemResult" else [k.value for k in node.keywords if k.arg == "message"]:
                    text = template(arg)
                    if text and any(c.isalpha() for c in text):
                        keys[text].add(f"{rel}:{node.lineno}")
    return keys


def _param_keys(params, where: str, keys: dict[str, set[str]]) -> None:
    for p in params:
        for text in (p.label, p.unit, p.help, p.group):
            if text:
                keys[text].add(where)
        if p.kind == "choice":
            for _val, label in p.choices:
                keys[label].add(where)


def registry_keys() -> dict[str, set[str]]:
    keys: dict[str, set[str]] = defaultdict(set)
    fx = importlib.import_module("tonewright.core.effects")
    for e in fx.all_effects():
        where = f"effect:{e.id}"
        for text in (e.name, e.category, e.description):
            keys[text].add(where)
        for preset in e.presets:
            keys[preset].add(where)
        _param_keys(e.params, where, keys)
    base = importlib.import_module("tonewright.core.effects.base")
    for cat in base.CATEGORY_ORDER:
        keys[cat].add("effect-category")
    formats = importlib.import_module("tonewright.core.formats")
    for spec in formats.FORMATS.values():
        for text in (spec.name, spec.description):
            if text:
                keys[text].add(f"format:{spec.key}")
        for c in spec.vbr_choices:
            keys[c[1]].add(f"format:{spec.key}")
    audiobook = importlib.import_module("tonewright.core.audiobook")
    for _k, label in audiobook.SPLIT_MODES:
        keys[label].add("audiobook:split")
    generate = importlib.import_module("tonewright.core.generate")
    for _k, label in (*generate.WAVEFORMS, *generate.NOISE_COLORS):
        keys[label].add("generate")
    dialogs = importlib.import_module("tonewright.ui.dialogs")
    for name in dir(dialogs):
        if name.endswith("_PARAMS"):
            _param_keys(getattr(dialogs, name), f"dialogs.{name}", keys)
    controller = importlib.import_module("tonewright.ui.controller")
    for label, _k in controller.TARGETS:
        keys[label].add("controller.TARGETS")
    convert_panel = importlib.import_module("tonewright.ui.convert_panel")
    for label, _p in convert_panel.PATTERNS:
        keys[label].add("convert_panel.PATTERNS")
    tags_panel = importlib.import_module("tonewright.ui.tags_panel")
    for _k, label in tags_panel.FIELDS:
        keys[label].add("tags_panel.FIELDS")
    main_window = importlib.import_module("tonewright.ui.main_window")
    for _k, label, _i in main_window.MODE_INFO:
        keys[label].add("main_window.MODE_INFO")
    for label in DOCUMENT_LABELS:
        keys[label].add("document")
    effect_dialog = importlib.import_module("tonewright.ui.effect_dialog")
    keys[effect_dialog.EffectDialog.CUSTOM].add("effect_dialog.CUSTOM")
    for label in ACTION_TEXTS:                      # menu entries built in loops, which the AST pass cannot see
        keys[label].add("main_window.actions")
    return keys


def all_keys() -> dict[str, set[str]]:
    merged: dict[str, set[str]] = defaultdict(set)
    for source in (static_keys(), registry_keys()):
        for k, v in source.items():
            merged[k] |= v
    return merged


def main() -> None:
    missing_for = sys.argv[sys.argv.index("--missing") + 1] if "--missing" in sys.argv else None
    keys = all_keys()
    if missing_for:
        catalogue = importlib.import_module(f"tonewright.locales.{missing_for}").MESSAGES
        keys = {k: v for k, v in keys.items() if k not in catalogue}
    for k in sorted(keys):
        print(repr(k), "   #", sorted(keys[k])[0])
    print(f"\n{len(keys)} keys", file=sys.stderr)


if __name__ == "__main__":
    main()
