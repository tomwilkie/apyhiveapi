"""Unit tests for the Hive entity cache, rate limiting and update_data."""

# pylint: disable=protected-access

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from apyhiveapi.hive import MIN_SCAN_INTERVAL, Hive
from tests.conftest import make_device

_FAR_PAST = timedelta(seconds=9999)


def _make_hive(*, stale=True):
    """Return a Hive whose last poll is controllably old or fresh."""
    hive = Hive(username="use@file.com", password="")
    now = datetime.now(UTC)
    hive.config.last_update = now - _FAR_PAST if stale else now
    hive._poll_devices = AsyncMock(return_value=True)
    return hive


class TestEntityCacheKey:
    """Tests for Hive._entity_cache_key."""

    def test_key_format(self):
        d = make_device(ha_type="climate", hive_id="h1", hive_type="heating")
        assert Hive._entity_cache_key(d) == "climate|h1|heating"

    def test_different_devices_produce_different_keys(self):
        d1 = make_device(hive_id="h1")
        d2 = make_device(hive_id="h2")
        assert Hive._entity_cache_key(d1) != Hive._entity_cache_key(d2)

    def test_ha_type_included_in_key(self):
        d1 = make_device(ha_type="climate", hive_id="h1")
        d2 = make_device(ha_type="water_heater", hive_id="h1")
        assert Hive._entity_cache_key(d1) != Hive._entity_cache_key(d2)


class TestCacheRoundTrip:
    """Tests for Hive.set_cached_device / get_cached_device."""

    def test_set_then_get_returns_device(self):
        hive = _make_hive()
        d = make_device()
        assert hive.set_cached_device(d) is d
        assert hive.get_cached_device(d) is d

    def test_get_unknown_returns_none(self):
        hive = _make_hive()
        assert hive.get_cached_device(make_device()) is None

    def test_overwrite_replaces_entry(self):
        hive = _make_hive()
        d1 = make_device(hive_id="h1")
        d2 = make_device(hive_id="h1")
        hive.set_cached_device(d1)
        hive.set_cached_device(d2)
        assert hive.get_cached_device(d1) is d2

    def test_different_devices_cached_independently(self):
        hive = _make_hive()
        d1 = make_device(hive_id="h1")
        d2 = make_device(hive_id="h2")
        hive.set_cached_device(d1)
        hive.set_cached_device(d2)
        assert hive.get_cached_device(d1) is d1
        assert hive.get_cached_device(d2) is d2


class TestShouldUseCachedData:
    """Tests for Hive.should_use_cached_data."""

    def test_last_poll_slow_returns_true(self):
        hive = _make_hive()
        hive._last_poll_slow = True
        assert hive.should_use_cached_data() is True

    def test_not_locked_not_slow_returns_false(self):
        hive = _make_hive()
        assert hive.should_use_cached_data() is False

    async def test_lock_held_without_update_task_returns_true(self):
        hive = _make_hive()
        async with hive.update_lock:
            hive._update_task = None
            assert hive.should_use_cached_data() is True

    async def test_lock_held_by_update_task_itself_returns_false(self):
        hive = _make_hive()
        async with hive.update_lock:
            hive._update_task = asyncio.current_task()
            assert hive.should_use_cached_data() is False

    async def test_lock_held_by_another_task_returns_true(self):
        hive = _make_hive()
        async with hive.update_lock:
            hive._update_task = asyncio.create_task(asyncio.sleep(0))
            assert hive.should_use_cached_data() is True
            await hive._update_task


class TestPollDevices:
    """Tests for Hive._poll_devices."""

    async def test_poll_devices_delegates_to_get_devices(self):
        hive = Hive(username="use@file.com", password="")
        hive.get_devices = AsyncMock(return_value=True)
        assert await hive._poll_devices() is True
        hive.get_devices.assert_awaited_once_with()


class TestUpdateData:
    """Tests for Hive.update_data."""

    async def test_stale_last_update_triggers_poll_returns_true(self):
        hive = _make_hive(stale=True)
        assert await hive.update_data(make_device()) is True
        hive._poll_devices.assert_called_once()

    async def test_fresh_last_update_skips_poll_returns_false(self):
        hive = _make_hive(stale=False)
        assert await hive.update_data(make_device()) is False
        hive._poll_devices.assert_not_called()

    async def test_lock_held_by_other_returns_false_without_polling(self):
        hive = _make_hive(stale=True)
        async with hive.update_lock:
            hive._update_task = None
            assert await hive.update_data(make_device()) is False
        hive._poll_devices.assert_not_called()

    async def test_update_task_cleared_after_successful_poll(self):
        hive = _make_hive(stale=True)
        await hive.update_data(make_device())
        assert hive._update_task is None

    async def test_failed_poll_returns_false(self):
        hive = _make_hive(stale=True)
        hive._poll_devices = AsyncMock(return_value=False)
        assert await hive.update_data(make_device()) is False

    async def test_fresh_after_acquiring_lock_skips_poll(self):
        """Another task polled while we waited for the lock: skip the poll."""
        hive = _make_hive(stale=True)
        await hive.update_lock.acquire()

        async def _refresh_and_release():
            await asyncio.sleep(0)
            hive.config.last_update = datetime.now(UTC)
            hive.update_lock.release()

        release_task = asyncio.create_task(_refresh_and_release())
        result = await hive.update_data(make_device())
        await release_task

        hive._poll_devices.assert_not_called()
        assert result is False

    async def test_update_task_set_to_current_during_poll(self):
        hive = _make_hive(stale=True)
        captured = []

        async def _capture_task():
            captured.append(hive._update_task)
            return True

        hive._poll_devices = _capture_task
        await hive.update_data(make_device())
        assert hive._update_task is None
        assert captured == [asyncio.current_task()]

    async def test_inner_recheck_returns_early_via_mocked_clock(self):
        """The outer check passes but the re-check inside the lock does not."""
        hive = _make_hive(stale=True)
        hive.config.last_update = datetime(2020, 1, 1, 12, 0, 0, tzinfo=UTC)
        hive.config.scan_interval = timedelta(seconds=60)
        clock = iter(
            [
                datetime(2020, 1, 1, 12, 2, 0, tzinfo=UTC),
                datetime(2020, 1, 1, 12, 0, 30, tzinfo=UTC),
            ]
        )

        with patch("apyhiveapi.hive.datetime") as mock_dt:
            mock_dt.now = lambda _tz=None: next(clock)
            result = await hive.update_data(make_device())

        hive._poll_devices.assert_not_called()
        assert result is False

    async def test_update_task_changed_during_poll_skips_reset_in_finally(self):
        """When another coroutine takes over _update_task, finally leaves it alone."""
        hive = _make_hive(stale=True)
        sentinel = object()

        async def poll_that_replaces_task():
            hive._update_task = sentinel
            return True

        hive._poll_devices = poll_that_replaces_task
        assert await hive.update_data(make_device()) is True
        assert hive._update_task is sentinel


class TestUpdateInterval:
    """Tests for Hive.update_interval, which the options flow calls."""

    async def test_seconds_are_stored_as_a_timedelta(self):
        hive = _make_hive()
        assert await hive.update_interval(120) is True
        assert hive.config.scan_interval == timedelta(seconds=120)

    async def test_timedelta_is_stored_as_given(self):
        hive = _make_hive()
        await hive.update_interval(timedelta(minutes=3))
        assert hive.config.scan_interval == timedelta(minutes=3)

    async def test_values_below_the_minimum_are_raised_to_it(self):
        hive = _make_hive()
        await hive.update_interval(5)
        assert hive.config.scan_interval == MIN_SCAN_INTERVAL
