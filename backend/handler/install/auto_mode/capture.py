"""Screenshot and input on the sandbox's Xvfb display."""

from __future__ import annotations

import os
import subprocess

from PIL import Image, ImageGrab

from logger.logger import log

# Kept around the active window so a wrongly reported frame offset still
# leaves its buttons inside the crop.
WINDOW_PAD = 60
MIN_WINDOW_SIDE = 80
XDOTOOL_TIMEOUT = 5


def _xdotool(display: str, *args: str) -> str:
    try:
        result = subprocess.run(
            ["xdotool", *args],
            env={**os.environ, "DISPLAY": display},
            capture_output=True,
            text=True,
            timeout=XDOTOOL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout


_capture_failing = False


def grab_screen(display: str) -> Image.Image | None:
    global _capture_failing
    try:
        image = ImageGrab.grab(xdisplay=display)
    except Exception as e:  # noqa: BLE001 - X server gone or not ready yet
        # Once per streak: the display vanishes when the installer exits.
        if not _capture_failing:
            log.warning(f"Install auto mode: cannot capture display {display}: {e}")
        _capture_failing = True
        return None
    _capture_failing = False
    return image


def active_window_box(
    display: str, screen: tuple[int, int]
) -> tuple[int, int, int, int]:
    """Padded box of the focused window, or the whole screen when unknown."""
    out = _xdotool(display, "getactivewindow", "getwindowgeometry", "--shell")
    dims = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    try:
        x, y, w, h = (int(dims[k]) for k in ("X", "Y", "WIDTH", "HEIGHT"))
    except (KeyError, ValueError):
        return 0, 0, screen[0], screen[1]
    if w < MIN_WINDOW_SIDE or h < MIN_WINDOW_SIDE:
        return 0, 0, screen[0], screen[1]
    return (
        max(0, x - WINDOW_PAD),
        max(0, y - WINDOW_PAD),
        min(screen[0], x + w + WINDOW_PAD),
        min(screen[1], y + h + WINDOW_PAD),
    )


def click(display: str, x: int, y: int) -> None:
    _xdotool(display, "mousemove", str(x), str(y), "click", "1")


def press_key(display: str, key: str, alt: bool = True) -> None:
    _xdotool(display, "key", f"alt+{key}" if alt else key)
