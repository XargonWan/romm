"""Throttled progress reporter.

Copying/hashing calls back on every chunk (every few MB); writing that
straight to the DB on every call would hammer it on a fast disk. Wraps a
sink so it fires at most once per `min_interval`, and always flushes the
final value via `finish()` so the last update isn't lost to throttling.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class ThrottledProgress:
    def __init__(self, sink: Callable[[int], None], *, min_interval: float = 1.0):
        self._sink = sink
        self._min_interval = min_interval
        # None (not 0.0) so the first call always fires regardless of the
        # monotonic clock's absolute value at construction time.
        self._last_sent_at: float | None = None
        self._last_value = 0

    def __call__(self, value: int) -> None:
        now = time.monotonic()
        if self._last_sent_at is None or now - self._last_sent_at >= self._min_interval:
            self._sink(value)
            self._last_sent_at = now
            self._last_value = value

    def finish(self, value: int) -> None:
        """Force one last update if `value` wasn't already the latest sent."""
        if value != self._last_value:
            self._sink(value)
            self._last_value = value
