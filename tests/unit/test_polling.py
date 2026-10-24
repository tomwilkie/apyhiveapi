"""Unit tests for polling: the scan interval, update_data and storing nodes."""

# pylint: disable=protected-access

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from apyhiveapi.exceptions import HiveReauthRequired
from apyhiveapi.hive import (
    MAX_STALE_STATE_POLLS,
    MIN_SCAN_INTERVAL,
    POLL_RETRY_DELAY,
    Hive,
)
from apyhiveapi.models import DEFAULT_SCAN_INTERVAL

_FAR_PAST = timedelta(days=1)
_LOGGER_NAME = "apyhiveapi.hive"


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

    def test_seconds_are_stored_as_a_timedelta(self):
        hive = _make_hive()
        assert hive.update_interval(300) is None
        assert hive.config.scan_interval == timedelta(seconds=300)

    def test_fractional_seconds_are_accepted(self):
        hive = _make_hive()
        hive.update_interval(90.5)
        assert hive.config.scan_interval == timedelta(seconds=90.5)

    def test_timedelta_is_stored_as_given(self):
        hive = _make_hive()
        hive.update_interval(timedelta(minutes=3))
        assert hive.config.scan_interval == timedelta(minutes=3)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(-5, id="negative"),
            pytest.param(0, id="zero"),
            pytest.param(5, id="five-seconds"),
            pytest.param(14.9, id="just-below"),
            pytest.param(timedelta(seconds=1), id="timedelta"),
        ],
    )
    def test_values_below_the_minimum_are_raised_to_it(self, value):
        hive = _make_hive()
        hive.update_interval(value)
        assert hive.config.scan_interval == MIN_SCAN_INTERVAL

    def test_the_minimum_itself_is_kept(self):
        hive = _make_hive()
        hive.update_interval(MIN_SCAN_INTERVAL.total_seconds())
        assert hive.config.scan_interval == MIN_SCAN_INTERVAL

    def test_default_interval_before_any_change(self):
        assert _make_hive().config.scan_interval == DEFAULT_SCAN_INTERVAL


class TestPollIsDue:
    """Tests for Hive._poll_is_due."""

    def test_due_once_the_interval_has_passed(self):
        assert _make_hive(due=True)._poll_is_due() is True

    def test_not_due_right_after_a_poll(self):
        assert _make_hive(due=False)._poll_is_due() is False

    def test_due_exactly_at_the_end_of_the_interval(self):
        hive = _make_hive()
        hive.config.last_update = datetime.now(UTC) - hive.config.scan_interval
        assert hive._poll_is_due() is True

    def test_shortening_the_interval_makes_a_poll_due(self):
        hive = _make_hive(due=False)
        hive.config.last_update = datetime.now(UTC) - timedelta(seconds=20)
        assert hive._poll_is_due() is False
        hive.update_interval(15)
        assert hive._poll_is_due() is True


class TestUpdateData:
    """Tests for Hive.update_data."""

    async def test_polls_when_due_and_returns_true(self):
        hive = _make_hive(due=True)
        assert await hive.update_data() is True
        hive.get_devices.assert_awaited_once_with()

    async def test_skips_the_poll_when_not_due(self):
        hive = _make_hive(due=False)
        assert await hive.update_data() is False
        hive.get_devices.assert_not_awaited()

    async def test_returns_false_while_another_caller_holds_the_lock(self):
        hive = _make_hive(due=True)
        async with hive.update_lock:
            assert await hive.update_data() is False
        hive.get_devices.assert_not_awaited()

    async def test_rechecks_whether_the_poll_is_due_after_taking_the_lock(self):
        hive = _make_hive(due=True)
        hive._poll_is_due = MagicMock(side_effect=[True, False])
        assert await hive.update_data() is False
        assert hive._poll_is_due.call_count == 2
        hive.get_devices.assert_not_awaited()

    async def test_returns_the_result_of_get_devices(self):
        hive = _make_hive(due=True)
        hive.get_devices = AsyncMock(return_value=False)
        assert await hive.update_data() is False
        hive.get_devices.assert_awaited_once_with()

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

    async def test_the_lock_is_released_after_a_poll(self):
        hive = _make_hive(due=True)
        await hive.update_data()
        assert not hive.update_lock.locked()

    async def test_the_lock_is_released_when_the_poll_raises(self):
        hive = _make_hive(due=True)
        hive.get_devices = AsyncMock(side_effect=HiveReauthRequired("log in again"))
        with pytest.raises(HiveReauthRequired):
            await hive.update_data()
        assert not hive.update_lock.locked()


class TestSchedulePollRetry:
    """Tests for Hive._schedule_poll_retry."""

    def test_next_poll_is_allowed_after_the_retry_delay(self):
        hive = _make_hive(due=True)
        hive._schedule_poll_retry()
        expected = datetime.now(UTC) - hive.config.scan_interval + POLL_RETRY_DELAY
        assert abs(hive.config.last_update - expected) < timedelta(seconds=1)

    def test_poll_is_not_due_until_the_delay_has_passed(self):
        hive = _make_hive(due=True)
        hive._schedule_poll_retry()
        assert hive._poll_is_due() is False
        hive.config.last_update -= POLL_RETRY_DELAY
        assert hive._poll_is_due() is True

    def test_retry_comes_sooner_than_a_full_interval(self):
        hive = _make_hive(due=False)
        before = hive.config.last_update
        hive._schedule_poll_retry()
        assert hive.config.last_update < before


class TestStoreNodes:
    """Tests for Hive._store_nodes."""

    def test_user_is_stored_when_present(self):
        hive = _make_hive()
        hive._store_nodes({"user": {"id": "u1", "temperatureUnit": "C"}})
        assert hive.data.user == {"id": "u1", "temperatureUnit": "C"}

    def test_user_is_kept_when_absent(self):
        hive = _make_hive()
        hive.data.user = {"id": "u1"}
        hive._store_nodes({"products": [_product("heat-1", {"mode": "OFF"})]})
        assert hive.data.user == {"id": "u1"}

    def test_products_and_devices_are_keyed_by_id(self):
        hive = _make_hive()
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

    def test_new_products_replace_the_old_ones(self):
        hive = _make_hive()
        hive.data.products = {"heat-1": _product("heat-1", {"mode": "OFF"})}
        hive._store_nodes({"products": [_product("heat-2", {"mode": "OFF"})]})
        assert set(hive.data.products) == {"heat-2"}

    def test_missing_required_state_is_carried_over(self):
        hive = _make_hive()
        hive.data.products = {
            "heat-1": _product("heat-1", {"mode": "OFF", "target": 7})
        }
        hive._store_nodes({"products": [_product("heat-1", {"target": 20})]})
        assert hive.data.products["heat-1"]["state"] == {"mode": "OFF", "target": 20}

    def test_counts_are_logged_at_debug(self, caplog):
        hive = _make_hive()
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            hive._store_nodes({"products": [_product("heat-1", {})], "devices": []})
        assert "Hive returned 1 products and 0 devices" in caplog.text


class TestKeepMissingState:
    """Required state keys dropped by a poll are carried over for a while."""

    def _hive(self, state):
        hive = _make_hive()
        hive.data.products = {"heat-1": _product("heat-1", dict(state))}
        return hive

    def test_missing_required_key_keeps_the_previous_value(self, caplog):
        hive = self._hive({"name": "Hallway", "mode": "OFF", "target": 7})
        new = {"heat-1": _product("heat-1", {"name": "Hallway", "target": 20})}
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            hive._keep_missing_state(new)
        assert new["heat-1"]["state"] == {
            "name": "Hallway",
            "mode": "OFF",
            "target": 20,
        }
        expected = (
            "Hive API returned incomplete state - keeping the last known values: "
            "Hallway (heating heat-1) missing mode"
        )
        assert caplog.messages == [expected]
        assert hive._stale_state_polls == {"heat-1": 1}

    def test_warning_lists_every_product_and_missing_key(self, caplog):
        hive = self._hive({"mode": "OFF", "target": 7, "boost": None})
        hive.data.products["hw-1"] = _product("hw-1", {"mode": "OFF"}, "hotwater")
        new = {
            "heat-1": _product("heat-1", {"boost": None}),
            "hw-1": {"id": "hw-1", "type": "hotwater"},
        }
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            hive._keep_missing_state(new)
        assert len(caplog.records) == 1
        assert caplog.messages[0].endswith(
            "heat-1 (heating heat-1) missing mode, target; "
            "hw-1 (hotwater hw-1) missing mode"
        )

    def test_missing_state_block_gets_the_previous_required_keys(self):
        hive = self._hive({"mode": "OFF", "target": 7, "boost": 30})
        new = {"heat-1": {"id": "heat-1", "type": "heating"}}
        hive._keep_missing_state(new)
        assert new["heat-1"]["state"] == {"mode": "OFF", "target": 7}

    def test_null_value_is_not_treated_as_missing(self, caplog):
        hive = self._hive({"mode": "OFF", "boost": 30})
        new = {"heat-1": _product("heat-1", {"mode": "OFF", "boost": None})}
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            hive._keep_missing_state(new)
        assert new["heat-1"]["state"] == {"mode": "OFF", "boost": None}
        assert not caplog.records

    def test_new_product_is_accepted_as_is(self, caplog):
        hive = self._hive({"mode": "OFF"})
        new = {
            "heat-1": _product("heat-1", {"mode": "OFF"}),
            "heat-2": _product("heat-2", {"target": 7}),
        }
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            hive._keep_missing_state(new)
        assert new["heat-2"]["state"] == {"target": 7}
        assert not caplog.records
        assert not hive._stale_state_polls

    def test_optional_keys_are_never_carried_over(self, caplog):
        hive = self._hive({"mode": "MANUAL", "target": 20, "boost": 30, "hue": 10})
        new = {"heat-1": _product("heat-1", {"mode": "MANUAL", "target": 20})}
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            hive._keep_missing_state(new)
        assert new["heat-1"]["state"] == {"mode": "MANUAL", "target": 20}
        assert not caplog.records
        assert not hive._stale_state_polls

    def test_only_required_keys_are_carried_over(self):
        hive = self._hive(
            {
                "name": "Hall",
                "mode": "MANUAL",
                "target": 20,
                "status": "ON",
                "boost": 30,
            }
        )
        new = {"heat-1": _product("heat-1", {})}
        hive._keep_missing_state(new)
        assert new["heat-1"]["state"] == {
            "name": "Hall",
            "mode": "MANUAL",
            "target": 20,
            "status": "ON",
        }

    def test_gives_up_after_max_consecutive_incomplete_polls(self, caplog):
        hive = self._hive({"name": "Hallway", "mode": "OFF", "target": 7})
        incomplete = {"name": "Hallway", "target": 7}
        for _ in range(MAX_STALE_STATE_POLLS):
            hive._store_nodes({"products": [_product("heat-1", dict(incomplete))]})
            assert hive.data.products["heat-1"]["state"]["mode"] == "OFF"
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            hive._store_nodes({"products": [_product("heat-1", dict(incomplete))]})
        assert hive.data.products["heat-1"]["state"] == incomplete
        expected = (
            "Hive API state for Hallway (heating heat-1) is still missing mode "
            f"after {MAX_STALE_STATE_POLLS} polls - no longer keeping the last "
            "known values."
        )
        assert caplog.messages == [expected]

    def test_after_giving_up_the_shorter_state_is_the_baseline(self, caplog):
        hive = self._hive({"mode": "OFF", "target": 7})
        for _ in range(MAX_STALE_STATE_POLLS + 1):
            hive._store_nodes({"products": [_product("heat-1", {"target": 7})]})
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            hive._store_nodes({"products": [_product("heat-1", {"target": 7})]})
        assert not caplog.records
        assert not hive._stale_state_polls
        assert hive.data.products["heat-1"]["state"] == {"target": 7}

    def test_complete_response_resets_the_count(self):
        hive = self._hive({"mode": "OFF"})
        hive._store_nodes({"products": [_product("heat-1", {})]})
        assert hive._stale_state_polls == {"heat-1": 1}
        hive._store_nodes({"products": [_product("heat-1", {"mode": "MANUAL"})]})
        assert not hive._stale_state_polls
        hive._store_nodes({"products": [_product("heat-1", {})]})
        assert hive._stale_state_polls == {"heat-1": 1}
        assert hive.data.products["heat-1"]["state"] == {"mode": "MANUAL"}

    def test_count_is_dropped_for_products_no_longer_returned(self):
        hive = self._hive({"mode": "OFF"})
        hive._store_nodes({"products": [_product("heat-1", {})]})
        assert hive._stale_state_polls == {"heat-1": 1}
        hive._store_nodes({"products": [_product("heat-2", {"mode": "OFF"})]})
        assert not hive._stale_state_polls

    def test_previous_state_not_a_dict_is_ignored(self, caplog):
        hive = _make_hive()
        hive.data.products = {"heat-1": _product("heat-1", "broken")}
        new = {"heat-1": _product("heat-1", {"mode": "OFF"})}
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            hive._keep_missing_state(new)
        assert new["heat-1"]["state"] == {"mode": "OFF"}
        assert not caplog.records

    def test_new_state_not_a_dict_gets_the_previous_required_keys(self):
        hive = self._hive({"mode": "OFF", "boost": 30})
        new = {"heat-1": _product("heat-1", "broken")}
        hive._keep_missing_state(new)
        assert new["heat-1"]["state"] == {"mode": "OFF"}

    def test_debug_logs_the_state_keys_that_arrived(self, caplog):
        hive = self._hive({"mode": "OFF", "target": 7})
        new = {"heat-1": _product("heat-1", {"target": 20, "boost": 5})}
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            hive._keep_missing_state(new)
        assert (
            "Hive API state for heating heat-1 arrived with keys: boost, target"
            in caplog.text
        )
