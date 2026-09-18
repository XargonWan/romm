from datetime import datetime, timezone

import pytest

import utils.install_cache as install_cache
from config import INSTALL_CACHE_DEFAULT_TTL
from utils.install_cache import (
    UNLIMITED_TTL,
    cache_size_bytes,
    clear_session_cache,
    ensure_session_cache_dir,
    resolve_expires_at,
    session_cache_dir,
)


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    root = tmp_path / "installs"
    monkeypatch.setattr(install_cache, "INSTALL_CACHE_PATH", str(root))
    return root


class TestResolveExpiresAt:
    def test_none_uses_default_ttl(self):
        before = datetime.now(timezone.utc)
        expires = resolve_expires_at(None)
        assert expires is not None
        delta = (expires - before).total_seconds()
        assert INSTALL_CACHE_DEFAULT_TTL - 5 <= delta <= INSTALL_CACHE_DEFAULT_TTL + 5

    def test_unlimited_returns_none(self):
        assert resolve_expires_at(UNLIMITED_TTL) is None

    def test_zero_returns_none(self):
        assert resolve_expires_at(0) is None

    def test_positive_ttl_in_future(self):
        before = datetime.now(timezone.utc)
        expires = resolve_expires_at(3600)
        assert expires is not None
        delta = (expires - before).total_seconds()
        assert 3595 <= delta <= 3605


class TestSessionCacheDir:
    def test_path_under_cache_root(self, cache_root):
        assert session_cache_dir(42) == cache_root / "42"

    def test_ensure_creates_dir(self, cache_root):
        path = ensure_session_cache_dir(7)
        assert path.exists()
        assert path.is_dir()


class TestClearSessionCache:
    def test_missing_returns_false(self, cache_root):
        assert clear_session_cache(99) is False

    def test_removes_existing(self, cache_root):
        path = ensure_session_cache_dir(1)
        (path / "file.bin").write_bytes(b"x" * 10)
        assert clear_session_cache(1) is True
        assert not path.exists()


class TestCacheSizeBytes:
    def test_empty_when_missing(self, cache_root):
        assert cache_size_bytes(123) == 0

    def test_sums_nested_files(self, cache_root):
        path = ensure_session_cache_dir(2)
        (path / "a.bin").write_bytes(b"x" * 100)
        nested = path / "sub"
        nested.mkdir()
        (nested / "b.bin").write_bytes(b"y" * 50)
        assert cache_size_bytes(2) == 150
