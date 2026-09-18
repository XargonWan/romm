from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import INSTALL_CACHE_DEFAULT_TTL, INSTALL_CACHE_PATH
from logger.formatter import highlight as hl
from logger.logger import log

# TTL sentinels for the API surface. A positive value is seconds-from-now,
# ``UNLIMITED_TTL`` maps to a NULL ``expires_at`` (never auto-evicted).
UNLIMITED_TTL = -1


def resolve_expires_at(ttl_seconds: int | None) -> datetime | None:
    """Translate a requested TTL into an absolute ``expires_at``.

    ``None`` uses the configured default TTL, ``UNLIMITED_TTL`` (or any value
    <= 0 other than the default request) yields ``None`` (unlimited).
    """
    if ttl_seconds is None:
        ttl_seconds = INSTALL_CACHE_DEFAULT_TTL
    if ttl_seconds <= 0:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)


def session_cache_dir(session_id: int) -> Path:
    """Absolute working directory for a single install session's files."""
    return Path(INSTALL_CACHE_PATH) / str(session_id)


def ensure_session_cache_dir(session_id: int) -> Path:
    """Create and return the session's cache directory."""
    path = session_cache_dir(session_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def clear_session_cache(session_id: int) -> bool:
    """Remove a session's cached files. Returns True if anything was removed."""
    path = session_cache_dir(session_id)
    if not path.exists():
        return False
    shutil.rmtree(path, ignore_errors=True)
    log.info(f"Cleared install cache for session {hl(str(session_id))}")
    return True


def cache_size_bytes(session_id: int) -> int:
    """Total size on disk of a session's cached files."""
    path = session_cache_dir(session_id)
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def cleanup_expired_installs() -> int:
    """Evict install caches whose TTL has elapsed and mark them EXPIRED.

    Unlimited sessions (``expires_at IS NULL``) are never evicted. Returns the
    number of sessions cleaned up.
    """
    # Imported here to avoid a circular import at module load time.
    from handler.database import db_install_session_handler
    from models.install_session import InstallSessionState

    expired = db_install_session_handler.get_expired_sessions()
    for session in expired:
        clear_session_cache(session.id)
        db_install_session_handler.update_session(
            session.id, {"state": InstallSessionState.EXPIRED}
        )

    if expired:
        log.info(f"Evicted {hl(str(len(expired)))} expired install caches")
    return len(expired)
