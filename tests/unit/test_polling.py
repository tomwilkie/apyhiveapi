"""Unit tests for polling: the scan interval, update_data and storing nodes."""

# pylint: disable=protected-access

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from apyhiveapi.exceptions import HiveReauthRequired
from apyhiveapi.hive import (
    MIN_SCAN_INTERVAL,
    POLL_RETRY_DELAY,
    Hive,
)
from apyhiveapi.models import DEFAULT_SCAN_INTERVAL

_FAR_PAST = timedelta(days=1)


def _make_hive(*, due=True):
    """Return a Hive whose next poll is due or not, with get_devices mocked."""
    hive = Hive(username="use@file.com", password="")
    now = datetime.now(UTC)
    hive.config.last_update = now - _FAR_PAST if due else now

    async def _poll_done():
        # Yield once, as a real network call would, then mark the poll done.
        await asyncio.sleep(0)
        hive.config.last_update = datetime.now(UTC)
        return True

    hive.get_devices = AsyncMock(side_effect=_poll_done)
    return hive


def _product(product_id, state, product_type="heating"):
    """Return a fresh product dict as the all-nodes response carries it."""
    return {"id": product_id, "type": product_type, "state": state}


class TestUpdateInterval:
    """Tests for Hive.update_interval, which the options flow calls."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(300, timedelta(seconds=300), id="seconds"),
            pytest.param(90.5, timedelta(seconds=90.5), id="fractional-seconds"),
            pytest.param(timedelta(minutes=3), timedelta(minutes=3), id="timedelta"),
            pytest.param(
                MIN_SCAN_INTERVAL.total_seconds(), MIN_SCAN_INTERVAL, id="the-minimum"
            ),
            pytest.param(-5, MIN_SCAN_INTERVAL, id="negative"),
            pytest.param(0, MIN_SCAN_INTERVAL, id="zero"),
            pytest.param(14.9, MIN_SCAN_INTERVAL, id="just-below"),
            pytest.param(timedelta(seconds=1), MIN_SCAN_INTERVAL, id="timedelta-below"),
        ],
    )
    def test_interval_is_stored_as_a_timedelta_not_below_the_minimum(
        self, value, expected
    ):
        hive = _make_hive()
        hive.update_interval(value)
        assert hive.config.scan_interval == expected


class TestPollIsDue:
    """Tests for Hive._poll_is_due."""

    @pytest.mark.parametrize(
        ("age", "expected"),
        [
            pytest.param(_FAR_PAST, True, id="interval-passed"),
            pytest.param(timedelta(0), False, id="just-polled"),
            pytest.param(DEFAULT_SCAN_INTERVAL, True, id="exactly-the-interval"),
        ],
    )
    def test_due_once_the_interval_has_passed(self, age, expected):
        hive = _make_hive()
        hive.config.last_update = datetime.now(UTC) - age
        assert hive._poll_is_due() is expected

    def test_shortening_the_interval_makes_a_poll_due(self):
        hive = _make_hive(due=False)
        hive.config.last_update = datetime.now(UTC) - timedelta(seconds=20)
        assert hive._poll_is_due() is False
        hive.update_interval(15)
        assert hive._poll_is_due() is True


class TestUpdateData:
    """Tests for Hive.update_data."""

    @pytest.mark.parametrize("polled", [True, False])
    async def test_polls_when_due_and_returns_the_result_of_get_devices(self, polled):
        hive = _make_hive(due=True)
        hive.get_devices = AsyncMock(return_value=polled)
        assert await hive.update_data() is polled
        hive.get_devices.assert_awaited_once_with()

    async def test_skips_the_poll_when_not_due(self):
        hive = _make_hive(due=False)
        assert await hive.update_data() is False
        hive.get_devices.assert_not_awaited()

    async def test_concurrent_callers_poll_once(self):
        hive = _make_hive(due=True)
        results = await asyncio.gather(hive.update_data(), hive.update_data())
        assert sorted(results) == [False, True]
        hive.get_devices.assert_awaited_once_with()

    async def test_a_second_call_after_a_poll_is_rate_limited(self):
        hive = _make_hive(due=True)
        assert await hive.update_data() is True
        assert await hive.update_data() is False
        hive.get_devices.assert_awaited_once_with()

    async def test_reauth_required_propagates_and_releases_the_lock(self):
        hive = _make_hive(due=True)
        hive.get_devices = AsyncMock(side_effect=HiveReauthRequired("log in again"))
        with pytest.raises(HiveReauthRequired):
            await hive.update_data()
        assert not hive.update_lock.locked()


class TestSchedulePollRetry:
    """Tests for Hive._schedule_poll_retry."""

    def test_poll_is_not_due_until_the_delay_has_passed(self):
        hive = _make_hive(due=True)
        hive._schedule_poll_retry()
        assert hive._poll_is_due() is False
        hive.config.last_update -= POLL_RETRY_DELAY
        assert hive._poll_is_due() is True


class TestStoreNodes:
    """Tests for Hive._store_nodes."""

    @pytest.mark.parametrize(
        ("nodes", "expected_user"),
        [
            pytest.param(
                {"user": {"id": "u2", "temperatureUnit": "C"}},
                {"id": "u2", "temperatureUnit": "C"},
                id="present",
            ),
            pytest.param(
                {"products": [_product("heat-1", {"mode": "OFF"})]},
                {"id": "u1"},
                id="absent",
            ),
        ],
    )
    def test_user_is_replaced_when_present_and_kept_when_absent(
        self, nodes, expected_user
    ):
        hive = _make_hive()
        hive.data.user = {"id": "u1"}
        hive._store_nodes(nodes)
        assert hive.data.user == expected_user

    def test_products_and_devices_are_keyed_by_id_and_replace_the_old_ones(self):
        hive = _make_hive()
        hive.data.products = {"old": _product("old", {"mode": "OFF"})}
        heating = _product("heat-1", {"mode": "OFF"})
        hub = {"id": "hub-1", "type": "hub"}
        hive._store_nodes({"products": [heating], "devices": [hub]})
        assert hive.data.products == {"heat-1": heating}
        assert hive.data.devices == {"hub-1": hub}

    def test_nodes_without_an_id_are_dropped(self):
        hive = _make_hive()
        heating = _product("heat-1", {"mode": "OFF"})
        hive._store_nodes(
            {"products": [heating, {"type": "heating"}], "devices": [None, "junk"]}
        )
        assert hive.data.products == {"heat-1": heating}
        assert hive.data.devices == {}

    @pytest.mark.parametrize(
        "nodes",
        [
            pytest.param({}, id="missing-lists"),
            pytest.param({"products": [], "devices": []}, id="empty-lists"),
        ],
    )
    def test_empty_or_missing_lists_keep_the_previous_data(self, nodes):
        hive = _make_hive()
        hive.data.products = {"heat-1": _product("heat-1", {"mode": "OFF"})}
        hive.data.devices = {"hub-1": {"id": "hub-1", "type": "hub"}}
        hive._store_nodes(nodes)
        assert set(hive.data.products) == {"heat-1"}
        assert set(hive.data.devices) == {"hub-1"}
