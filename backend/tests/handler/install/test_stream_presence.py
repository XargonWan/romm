import pytest

from handler.install import stream_presence
from handler.redis_handler import async_cache


@pytest.fixture(autouse=True)
async def _clear_presence_state():
    """Redis (fakeredis under pytest) is a single shared store across the
    whole test session - clear this module's keys between tests."""

    async def _clear():
        keys = [
            k
            async for k in async_cache.scan_iter(
                match=f"{stream_presence._KEY_PREFIX}*"
            )
        ]
        keys += [
            k
            async for k in async_cache.scan_iter(
                match=f"{stream_presence._INDEX_PREFIX}*"
            )
        ]
        if keys:
            await async_cache.delete(*keys)

    await _clear()
    yield
    await _clear()


class TestHeartbeatAndCountViewers:
    @pytest.mark.asyncio
    async def test_no_heartbeats_counts_zero(self):
        assert await stream_presence.count_viewers(1) == 0

    @pytest.mark.asyncio
    async def test_one_heartbeat_counts_one(self):
        await stream_presence.heartbeat(1, user_id=1, device_id="web")
        assert await stream_presence.count_viewers(1) == 1

    @pytest.mark.asyncio
    async def test_repeated_heartbeats_from_the_same_client_dont_double_count(self):
        await stream_presence.heartbeat(1, user_id=1, device_id="web")
        await stream_presence.heartbeat(1, user_id=1, device_id="web")
        await stream_presence.heartbeat(1, user_id=1, device_id="web")
        assert await stream_presence.count_viewers(1) == 1

    @pytest.mark.asyncio
    async def test_distinct_devices_count_separately(self):
        await stream_presence.heartbeat(1, user_id=1, device_id="web")
        await stream_presence.heartbeat(1, user_id=1, device_id="steamdeck")
        assert await stream_presence.count_viewers(1) == 2

    @pytest.mark.asyncio
    async def test_sessions_dont_leak_into_each_others_count(self):
        await stream_presence.heartbeat(1, user_id=1, device_id="web")
        await stream_presence.heartbeat(2, user_id=1, device_id="web")
        assert await stream_presence.count_viewers(1) == 1
        assert await stream_presence.count_viewers(2) == 1

    @pytest.mark.asyncio
    async def test_expired_viewer_key_self_heals_out_of_the_index(self):
        await stream_presence.heartbeat(1, user_id=1, device_id="web")
        # Simulate the per-viewer key expiring (TTL elapsed) without its
        # index-set membership having been cleaned up yet.
        await async_cache.delete(stream_presence._viewer_key(1, 1, "web"))

        assert await stream_presence.count_viewers(1) == 0
        # The stale member must have been dropped from the index, not just
        # skipped this one time.
        assert await async_cache.scard(stream_presence._index_key(1)) == 0
