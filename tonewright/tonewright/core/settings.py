"""Persistent settings, favourite effect presets and recent files."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

from .. import paths

DEFAULTS: dict[str, Any] = {
    "theme": "dark",
    "language": "auto",                 # auto = follow Windows; otherwise a code from i18n.LANGUAGES
    "output_dir": "",
    "same_folder": False,
    "recent_files": [],
    "recent_effects": [],
    "last_dir": {"open": "", "save": "", "image": "", "export": ""},
    "convert": {},
    "audiobook": {},
    "volume": 0.9,
    "follow_playhead": True,
    "snap_zero_crossings": False,
    "show_db_scale": True,
    "mix_delete_outside": True,         # dragging a volume-curve point out of its clip deletes it
    "spectrogram_fft": 2048,
    "preview_seconds": 8,
    "window": {},
    "ffmpeg_dir": "",
}
MAX_RECENT_FILES = 12
MAX_RECENT_EFFECTS = 8


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class Settings:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else paths.config_dir() / "settings.json"
        self._lock = threading.Lock()
        self.data: dict[str, Any] = json.loads(json.dumps(DEFAULTS))
        self.load()

    def load(self) -> None:
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(stored, dict):
            for key, value in stored.items():
                if key in DEFAULTS and isinstance(value, type(DEFAULTS[key])):
                    self.data[key] = value

    def save(self) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(self.path, json.dumps(self.data, indent=2, ensure_ascii=False))
            except OSError:
                pass

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, DEFAULTS.get(key) if default is None else default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    def last_dir(self, kind: str) -> str:
        path = self.data["last_dir"].get(kind) or ""
        return path if path and Path(path).exists() else str(Path.home())

    def remember_dir(self, kind: str, file_or_dir: str | Path) -> None:
        p = Path(file_or_dir)
        folder = p if p.is_dir() else p.parent
        self.data["last_dir"][kind] = str(folder)
        self.save()

    def add_recent_file(self, path: str | Path) -> None:
        recent = [r for r in self.data["recent_files"] if r != str(path)]
        self.data["recent_files"] = ([str(path)] + recent)[:MAX_RECENT_FILES]
        self.save()

    def recent_files(self) -> list[str]:
        return [r for r in self.data["recent_files"] if Path(r).exists()]

    def clear_recent_files(self) -> None:
        self.data["recent_files"] = []
        self.save()

    def used_effect(self, effect_id: str) -> None:
        recent = [e for e in self.data["recent_effects"] if e != effect_id]
        self.data["recent_effects"] = ([effect_id] + recent)[:MAX_RECENT_EFFECTS]
        self.save()


class Favorites:
    """Named effect presets saved from the effect dialogs ("To Favorites")."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else paths.config_dir() / "favorites.json"
        self.items: list[dict] = []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self.items = [d for d in data if isinstance(d, dict) and {"name", "effect", "params"} <= set(d)]
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.path, json.dumps(self.items, indent=2, ensure_ascii=False))
        except OSError:
            pass

    def add(self, name: str, effect_id: str, params: dict) -> None:
        name = name.strip() or effect_id
        self.items = [i for i in self.items if i["name"] != name]
        self.items.append({"name": name, "effect": effect_id, "params": params})
        self._save()

    def remove(self, name: str) -> None:
        self.items = [i for i in self.items if i["name"] != name]
        self._save()

    def rename(self, old: str, new: str) -> None:
        for item in self.items:
            if item["name"] == old:
                item["name"] = new.strip() or old
        self._save()

    def get(self, name: str) -> Optional[dict]:
        return next((i for i in self.items if i["name"] == name), None)
