"""Small shared types: cancellation, pausing, progress."""
from __future__ import annotations

import threading
from typing import Callable, Optional

ProgressFn = Callable[[float], None]


class Cancelled(Exception):
    """Raised inside a worker when the user cancels the operation."""


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled()

    def wait(self, timeout: float) -> bool:
        """Sleep up to `timeout` seconds; True if the token was cancelled meanwhile."""
        return self._event.wait(timeout)


class PauseToken:
    """Lets the UI pause a running job; workers call wait() between steps."""

    def __init__(self) -> None:
        self._running = threading.Event()
        self._running.set()
        self._listeners: list[Callable[[bool], None]] = []

    def add_listener(self, fn: Callable[[bool], None]) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[bool], None]) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def pause(self) -> None:
        self._running.clear()
        for fn in list(self._listeners):
            fn(True)

    def resume(self) -> None:
        self._running.set()
        for fn in list(self._listeners):
            fn(False)

    def wait(self, cancel: Optional[CancelToken] = None) -> None:
        while not self._running.wait(0.1):
            if cancel is not None:
                cancel.check()


def scaled(progress: Optional[ProgressFn], lo: float, hi: float) -> ProgressFn:
    """Map a 0..1 sub-task onto the lo..hi slice of a parent progress bar."""
    def report(frac: float) -> None:
        if progress is not None:
            progress(lo + (hi - lo) * max(0.0, min(1.0, frac)))
    return report
