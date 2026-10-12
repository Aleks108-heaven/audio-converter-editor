"""Importing the entry-point module must never start the application (it once opened a real window from a test tool)."""
from __future__ import annotations

import importlib
import sys

from tonewright import app


def test_importing_main_does_not_start_the_app(monkeypatch):
    started = []
    monkeypatch.setattr(app, "main", lambda *a, **k: started.append(True) or 0)
    sys.modules.pop("tonewright.__main__", None)
    importlib.import_module("tonewright.__main__")
    assert started == []
    sys.modules.pop("tonewright.__main__", None)
