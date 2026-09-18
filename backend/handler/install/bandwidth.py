"""Global bandwidth cap for in-progress stream-install downloads.

One shared limit for the whole server (like a torrent client's global rate
cap): every concurrent "still installing" file transfer draws from the same
budget, admin-configurable at runtime via Settings. Backed by Redis (not an
in-process counter) so the cap stays correct even if WEB_SERVER_CONCURRENCY
runs more than one gunicorn worker - a limit set by whichever worker handled
the settings write must apply to every other worker's requests too.

Fixed-window admission control, not a literal token bucket: each wall-clock
second is its own budget (INCRBY into a key that expires after 2s); a caller
that would push the window over budget backs its reservation out and waits
for the next window. Race-safe (INCRBY is atomic) without needing a Lua
script - matches this codebase's existing hand-rolled-Redis-primitive idiom
(see endpoints/roms/upload.py's chunk-index set).
"""

from __future__ import annotations

import asyncio
import time

from handler.redis_handler import async_cache

_LIMIT_KEY = "install:bandwidth:limit_bytes_per_sec"
_WINDOW_KEY_PREFIX = "install:bandwidth:window:"

# Window keys are read/written well within this many seconds of being
# created; the expiry is just a safety net so a crashed process's windows
# don't linger in Redis forever.
_WINDOW_KEY_TTL = 2


async def set_bytes_per_second(value: int | None) -> None:
    """Update the configured global cap so every worker process picks it up.

    Called right after ConfigManager.update_install_settings persists the
    new value to config.yml (from the config endpoint) and once at boot to
    seed it from whatever was already persisted (from startup.py) - either
    way, this is what makes the change visible to every worker process
    immediately, not just the one that happened to write it.
    """
    if value is None or value <= 0:
        await async_cache.delete(_LIMIT_KEY)
    else:
        await async_cache.set(_LIMIT_KEY, value)


async def get_bytes_per_second() -> int | None:
    """The currently configured cap, or None when unlimited."""
    raw = await async_cache.get(_LIMIT_KEY)
    return int(raw) if raw else None


async def acquire(n_bytes: int) -> None:
    """Block until `n_bytes` fit inside the current global budget.

    No-op when no limit is configured. Never partially grants: a caller
    either gets its whole reservation for the current second or waits for
    the next one, so a chunk's bytes are never split across two windows.
    """
    if n_bytes <= 0:
        return
    while True:
        limit = await get_bytes_per_second()
        if limit is None:
            return

        window = int(time.time())
        key = f"{_WINDOW_KEY_PREFIX}{window}"
        used = await async_cache.incrby(key, n_bytes)
        if used == n_bytes:
            # First writer into this window - arm its expiry.
            await async_cache.expire(key, _WINDOW_KEY_TTL)
        if used <= limit:
            return

        # This reservation pushed the window over budget - give it back and
        # wait for a fresh window before retrying.
        await async_cache.decrby(key, n_bytes)
        await asyncio.sleep(max(window + 1 - time.time(), 0.01))
