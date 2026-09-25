"""Auto mode loop: watch the installer's screen and press what it needs.

Runs as a thread next to the focus-maintenance loop. All side effects (screen
observation, input, session updates) are injected so the decision flow can be
unit-tested without X11 or tesseract.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from config import INSTALL_AUTO_STUCK_SECONDS
from logger.logger import log

from .capture import active_window_box, click, grab_screen, press_key
from .catalog import Catalog
from .engine import Action, ScreenMemory, is_license_page, plan_action, same_screen
from .matcher import Word, find_matches, screen_lines
from .ocr import LIGHT_BAND_HEIGHT, ocr_bottom_blocks, ocr_words

STATUS_RUNNING = "running"
STATUS_NEEDS_MANUAL = "needs_manual"

POLL_INTERVAL = 2.0
# Give the installer this long to repaint after a click before looking again.
SETTLE_SECONDS = 1.5
# Hard cap so an OCR jitter loop can never click forever.
MAX_ACTIONS = 300


@dataclass
class AutoModeDriver:
    catalog: Catalog
    observe: Callable[[], list[Word] | None]
    act: Callable[[Action], None]
    enabled: Callable[[], bool]
    progress: Callable[[], int]
    report: Callable[[str | None, str | None], None]
    stuck_seconds: float = INSTALL_AUTO_STUCK_SECONDS
    clock: Callable[[], float] = time.monotonic

    memory: ScreenMemory = field(default_factory=ScreenMemory)
    actions_done: int = 0
    _idle_since: float | None = None
    _logged_lines: frozenset[str] = frozenset()
    _last_progress: int = -1
    _status: str | None = None
    _detail: str | None = None

    def _set(self, status: str | None, detail: str | None) -> None:
        if (status, detail) != (self._status, self._detail):
            self._status, self._detail = status, detail
            self.report(status, detail)

    def tick(self) -> bool:
        """One observe-decide-act step. True when an action was performed."""
        now = self.clock()
        if not self.enabled():
            self._idle_since = None
            self._set(None, None)
            return False
        if self._idle_since is None:
            self._idle_since = now

        words = self.observe()
        if words is None:
            return False

        lines = frozenset(screen_lines(words))
        if lines and not same_screen(lines, self.memory.lines):
            self.memory = ScreenMemory(lines=lines)
            self._idle_since = now
        progress = self.progress()
        if progress != self._last_progress:
            self._last_progress = progress
            self._idle_since = now

        action, matches = plan_action(
            words, self.catalog, self.memory, installing=self._last_progress > 0
        )
        if lines != self._logged_lines:
            self._logged_lines = lines
            found = [f"{m.entry.category}:{m.text}" for m in matches]
            log.info(
                f"Install auto mode: page with {len(lines)} text lines, "
                f"buttons found: {found or 'none'}"
            )
        if action is not None and self.actions_done < MAX_ACTIONS:
            self.memory.attempts[action.memory_key] = (
                self.memory.attempts.get(action.memory_key, 0) + 1
            )
            self.actions_done += 1
            log.info(f"Install auto mode: {action.describe()}")
            self.act(action)
            self._idle_since = now
            self._set(STATUS_RUNNING, action.describe())
            return True

        if now - self._idle_since >= self.stuck_seconds:
            self._set(STATUS_NEEDS_MANUAL, "No known button on screen")
        elif self._status is None:
            self._set(STATUS_RUNNING, None)
        return False

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                acted = self.tick()
            except Exception as e:  # noqa: BLE001 - never let auto mode kill the install
                log.warning(f"Install auto mode tick failed: {e}")
                acted = False
            stop.wait(SETTLE_SECONDS if acted else POLL_INTERVAL)


def _needs_deep_pass(words: list[Word], catalog: Catalog) -> bool:
    """Whole-page OCR often misses the wizard's bottom strip. Look closer
    when no advance button was read, or on a license page whose accept
    checkbox was not."""
    categories = {m.entry.category for m in find_matches(words, catalog)}
    if not categories & {"next", "install", "finish"}:
        return True
    return is_license_page(screen_lines(words), catalog) and "agree" not in categories


def make_x11_observer(
    display: str, catalog: Catalog
) -> Callable[[], list[Word] | None]:
    """OCR the focused window (or the whole screen) of ``display``, returning
    words in absolute screen coordinates."""

    def observe() -> list[Word] | None:
        screen = grab_screen(display)
        if screen is None:
            return None
        left, top, right, bottom = active_window_box(display, screen.size)
        crop = screen.crop((left, top, right, bottom))
        words = ocr_words(crop)
        if _needs_deep_pass(words, catalog):
            words += ocr_words(crop, light_text=True)
        if _needs_deep_pass(words, catalog):
            words += ocr_bottom_blocks(crop)
        if _needs_deep_pass(words, catalog):
            words += ocr_bottom_blocks(crop, band_height=LIGHT_BAND_HEIGHT, light=True)
        return [
            Word(
                w.text,
                w.left + left,
                w.top + top,
                w.width,
                w.height,
                w.conf,
                w.line_id,
            )
            for w in words
        ]

    return observe


def make_x11_actor(display: str) -> Callable[[Action], None]:
    def act(action: Action) -> None:
        if action.kind == "key" and action.key:
            press_key(display, action.key, action.alt)
        else:
            click(display, action.x, action.y)

    return act
