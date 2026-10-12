from __future__ import annotations

import json

from tonewright.core.settings import MAX_RECENT_EFFECTS, MAX_RECENT_FILES, Favorites, Settings


def test_defaults_and_persistence(tmp_path):
    path = tmp_path / "s.json"
    s = Settings(path)
    assert s.get("theme") == "dark" and s.get("volume") == 0.9
    s.set("theme", "light")
    s.set("convert", {"format": "flac"})
    again = Settings(path)
    assert again.get("theme") == "light" and again.get("convert") == {"format": "flac"}


def test_corrupt_or_foreign_files_are_ignored(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{ not json", encoding="utf-8")
    assert Settings(path).get("theme") == "dark"
    path.write_text(json.dumps({"theme": 5, "unknown": 1, "volume": "loud", "same_folder": True}), encoding="utf-8")
    s = Settings(path)
    assert s.get("theme") == "dark" and s.get("volume") == 0.9 and s.get("same_folder") is True
    assert "unknown" not in s.data


def test_recent_files_are_unique_capped_and_must_exist(tmp_path):
    s = Settings(tmp_path / "s.json")
    files = []
    for i in range(MAX_RECENT_FILES + 4):
        f = tmp_path / f"{i}.wav"
        f.write_bytes(b"x")
        files.append(f)
        s.add_recent_file(f)
    s.add_recent_file(files[-3])
    recent = s.recent_files()
    assert len(recent) == MAX_RECENT_FILES and recent[0] == str(files[-3])
    assert len(set(recent)) == len(recent)
    files[-1].unlink()
    assert str(files[-1]) not in s.recent_files()
    s.clear_recent_files()
    assert s.recent_files() == []


def test_recent_effects_most_recent_first(tmp_path):
    s = Settings(tmp_path / "s.json")
    for e in ["a", "b", "c", "a"] + [f"x{i}" for i in range(10)]:
        s.used_effect(e)
    assert len(s.get("recent_effects")) == MAX_RECENT_EFFECTS and s.get("recent_effects")[0] == "x9"


def test_last_dir_falls_back_when_missing(tmp_path):
    s = Settings(tmp_path / "s.json")
    s.remember_dir("open", tmp_path / "file.wav")
    assert s.last_dir("open") == str(tmp_path)
    s.remember_dir("save", tmp_path / "gone" / "x.wav")
    assert s.last_dir("save") != str(tmp_path / "gone")


def test_favorites_crud_and_persistence(tmp_path):
    path = tmp_path / "f.json"
    fav = Favorites(path)
    fav.add("Warm echo", "echo", {"delay": 200.0})
    fav.add("Big hall", "reverb", {"decay": 4.0})
    fav.add("Warm echo", "echo", {"delay": 250.0})            # same name replaces
    assert [i["name"] for i in Favorites(path).items] == ["Big hall", "Warm echo"]
    assert Favorites(path).get("Warm echo")["params"] == {"delay": 250.0}
    fav.rename("Big hall", "Cathedral")
    fav.remove("Warm echo")
    assert [i["name"] for i in Favorites(path).items] == ["Cathedral"]
    path.write_text("[1, 2, {\"name\": \"ok\"}]", encoding="utf-8")
    assert Favorites(path).items == []
