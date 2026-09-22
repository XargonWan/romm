"""Whether the install-sandbox worker may stream a large file's bytes as
they're written, instead of waiting for scan_live_manifest's normal
"observed stable" confirmation before exposing them.

Experimental and disabled by default (see Settings -> Library Management ->
Stream Install's "Stream uncompleted files" toggle). Backed by Redis, not an
in-process flag, for the same reason as handler.install.bandwidth: the
process that actually runs the live manifest loop (romm-install-sandbox) is
a separate container from the one that persists config.yml (romm-dev), so a
plain ConfigManager field alone would never reach it.

Read synchronously (plain redis_client, not async_cache) because the only
consumer - runner._live_manifest_loop - is a background OS thread, not an
asyncio context; matches handler.install.queue_status's own sync usage from
inside async endpoint handlers elsewhere in this codebase.
"""

from __future__ import annotations

from handler.redis_handler import redis_client

_KEY = "install:stream_uncompleted_files"


def set_stream_uncompleted_files(enabled: bool) -> None:
    """Update the flag so the install-sandbox worker picks it up immediately.

    Called right after ConfigManager.update_install_settings persists the
    new value to config.yml (from the config endpoint), and once at boot to
    seed it from whatever was already persisted (from startup.py) - same
    idiom as handler.install.bandwidth.set_bytes_per_second.
    """
    if enabled:
        redis_client.set(_KEY, "1")
    else:
        redis_client.delete(_KEY)


def stream_uncompleted_files_enabled() -> bool:
    """The currently configured value, defaulting to disabled (the key is
    simply absent until explicitly turned on or seeded at boot)."""
    return bool(redis_client.exists(_KEY))
