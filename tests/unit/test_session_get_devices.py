"""Tests for Hive.get_devices: file mode, the API path and error handling."""

# pylint: disable=protected-access

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apyhiveapi.exceptions import HiveAuthError, HiveReauthRequired
from apyhiveapi.hive import MAX_STALE_STATE_POLLS, Hive

_FAR_PAST = timedelta(seconds=9999)
_RESPONSE = {"original": 200, "parsed": {"user": {"id": "u1"}}}


def _make_hive():
    """Return a Hive with the API, auth and file access mocked."""
    hive = Hive(username="use@file.com", password="")
    hive.config.last_update = datetime.now(UTC) - _FAR_PAST
    hive.api = MagicMock()
    hive.api.get_all = AsyncMock(return_value=dict(_RESPONSE))
    hive.hive_refresh_tokens = AsyncMock()
    hive._retry_login = AsyncMock()
    hive._retry_with_backoff = AsyncMock(return_value=dict(_RESPONSE))
    hive.open_file = MagicMock(return_value=dict(_RESPONSE))
    return hive


def _products_response(*products):
    return {"original": 200, "parsed": {"products": list(products)}}


class TestGetDevicesFileMode:
    """Tests for get_devices when config.file is True."""

    async def test_file_mode_loads_from_file_and_succeeds(self):
        hive = _make_hive()
        hive.config.file = True
        hive.open_file.return_value = {
            "original": 200,
            "parsed": {"user": {"id": "file-user"}},
        }
        assert await hive.get_devices() is True
        hive.open_file.assert_called_once_with("data.json")
        assert hive.data.user["id"] == "file-user"

    async def test_file_mode_does_not_call_api(self):
        hive = _make_hive()
        hive.config.file = True
        await hive.get_devices()
        hive.api.get_all.assert_not_called()
        hive.hive_refresh_tokens.assert_not_called()


class TestGetDevicesApiPath:
    """Tests for get_devices against the (mocked) API."""

    async def test_successful_poll_returns_true_and_updates_last_update(self):
        hive = _make_hive()
        before = datetime.now(UTC)
        assert await hive.get_devices() is True
        hive.hive_refresh_tokens.assert_called_once()
        assert hive.config.last_update >= before

    async def test_slow_api_call_sets_last_poll_slow(self):
        hive = _make_hive()
        hive._slow_poll_threshold = 0  # every call is "slow"
        await hive.get_devices()
        assert hive._last_poll_slow is True

    async def test_fast_api_call_clears_last_poll_slow(self):
        hive = _make_hive()
        hive._last_poll_slow = True
        hive._slow_poll_threshold = 9999
        await hive.get_devices()
        assert hive._last_poll_slow is False

    async def test_api_returning_none_returns_false(self):
        hive = _make_hive()
        hive.api.get_all = AsyncMock(return_value=None)
        last_update = hive.config.last_update
        assert await hive.get_devices() is False
        assert hive.config.last_update == last_update

    async def test_non_2xx_response_returns_false(self):
        hive = _make_hive()
        hive.api.get_all.return_value = {"original": 400, "parsed": {}}
        assert await hive.get_devices() is False

    async def test_parsed_none_returns_false(self):
        hive = _make_hive()
        hive.api.get_all.return_value = {"original": 200, "parsed": None}
        assert await hive.get_devices() is False

    async def test_hive_auth_error_triggers_retry_and_continues(self):
        hive = _make_hive()
        hive._last_poll_slow = True
        hive.api.get_all.side_effect = HiveAuthError()
        hive._retry_with_backoff.return_value = {
            "original": 200,
            "parsed": {"user": {"id": "retry-user"}},
        }
        assert await hive.get_devices() is True
        hive._retry_login.assert_called_once()
        hive._retry_with_backoff.assert_called_once()
        assert hive._last_poll_slow is False
        assert hive.data.user["id"] == "retry-user"


class TestGetDevicesMalformedResponse:
    """Partial response shapes must not escape as raw errors."""

    async def test_user_without_id_still_succeeds(self):
        hive = _make_hive()
        hive.api.get_all.return_value = {"original": 200, "parsed": {"user": {}}}
        assert await hive.get_devices() is True
        assert hive.config.user_id is None

    async def test_product_without_id_returns_false(self):
        hive = _make_hive()
        hive.api.get_all.return_value = _products_response({"type": "heating"})
        assert await hive.get_devices() is False


class TestGetDevicesDataParsing:
    """Tests for the stored data after a poll."""

    async def _run_with_parsed(self, parsed):
        hive = _make_hive()
        hive.api.get_all.return_value = {"original": 200, "parsed": parsed}
        assert await hive.get_devices() is True
        return hive

    async def test_user_data_sets_user_and_user_id(self):
        hive = await self._run_with_parsed({"user": {"id": "my-user"}})
        assert hive.data.user["id"] == "my-user"
        assert hive.config.user_id == "my-user"

    async def test_products_are_keyed_by_id(self):
        hive = await self._run_with_parsed(
            {"products": [{"id": "p1", "type": "heating"}]}
        )
        assert hive.data.products["p1"]["type"] == "heating"

    async def test_devices_are_keyed_by_id(self):
        hive = await self._run_with_parsed({"devices": [{"id": "d1", "type": "hub"}]})
        assert hive.data.devices["d1"]["type"] == "hub"

    async def test_homes_sets_config_home_id(self):
        hive = await self._run_with_parsed({"homes": {"homes": [{"id": "home-1"}]}})
        assert hive.config.home_id == "home-1"

    @pytest.mark.parametrize("homes", [{"homes": None}, {"homes": []}, [{"id": "x"}]])
    async def test_unusable_homes_leave_home_id_unset(self, homes):
        hive = await self._run_with_parsed({"homes": homes})
        assert hive.config.home_id is None

    async def test_empty_lists_keep_previous_data(self):
        hive = _make_hive()
        hive.data.products = {"p1": {"id": "p1"}}
        hive.data.devices = {"d1": {"id": "d1"}}
        hive.api.get_all.return_value = {
            "original": 200,
            "parsed": {"products": [], "devices": []},
        }
        await hive.get_devices()
        assert hive.data.products == {"p1": {"id": "p1"}}
        assert hive.data.devices == {"d1": {"id": "d1"}}


class TestGetDevicesExceptions:
    """Tests for the exception handling in get_devices."""

    async def test_hive_reauth_required_propagates(self):
        hive = _make_hive()
        hive.api.get_all.side_effect = HiveReauthRequired()
        with pytest.raises(HiveReauthRequired):
            await hive.get_devices()

    async def test_timeout_error_marks_slow_and_schedules_retry(self):
        hive = _make_hive()
        hive.api.get_all.side_effect = TimeoutError()
        assert await hive.get_devices() is False
        assert hive._last_poll_slow is True
        assert hive.config.last_update > datetime.now(UTC) - hive.config.scan_interval

    @pytest.mark.parametrize(
        "error",
        [
            OSError("network gone"),
            RuntimeError("unexpected"),
            ConnectionError("refused"),
        ],
    )
    async def test_request_errors_return_false(self, error):
        hive = _make_hive()
        hive.api.get_all.side_effect = error
        assert await hive.get_devices() is False


class TestGetDevicesIncompleteState:
    """A poll that drops state keys must not wipe the last known values."""

    def _stub(self, state):
        hive = _make_hive()
        hive.data.products = {"heat-1": {"id": "heat-1", "state": dict(state)}}
        return hive

    async def test_missing_state_key_keeps_previous_value(self):
        hive = self._stub({"name": "Hallway", "mode": "OFF", "target": 7})
        hive.api.get_all = AsyncMock(
            return_value=_products_response(
                {
                    "id": "heat-1",
                    "type": "heating",
                    "state": {"name": "Hallway", "target": 20},
                }
            )
        )
        with patch("apyhiveapi.hive._LOGGER") as mock_log:
            assert await hive.get_devices() is True
        assert hive.data.products["heat-1"]["state"] == {
            "name": "Hallway",
            "mode": "OFF",
            "target": 20,
        }
        mock_log.warning.assert_called_once()
        assert mock_log.warning.call_args.args[1] == (
            "Hallway (heating heat-1) missing mode"
        )

    async def test_warning_lists_every_product_and_missing_key(self):
        hive = self._stub({"mode": "OFF", "target": 7, "boost": None})
        hive.data.products["hw-1"] = {"id": "hw-1", "state": {"mode": "OFF"}}
        hive.api.get_all = AsyncMock(
            return_value=_products_response(
                {"id": "heat-1", "type": "heating", "state": {"boost": None}},
                {"id": "hw-1", "type": "hotwater"},
            )
        )
        with patch("apyhiveapi.hive._LOGGER") as mock_log:
            await hive.get_devices()
        mock_log.warning.assert_called_once()
        assert mock_log.warning.call_args.args[1] == (
            "heat-1 (heating heat-1) missing mode, target; "
            "hw-1 (hotwater hw-1) missing mode"
        )

    async def test_debug_logs_the_state_keys_that_arrived(self):
        hive = self._stub({"mode": "OFF", "target": 7, "boost": None})
        hive.api.get_all = AsyncMock(
            return_value=_products_response(
                {"id": "heat-1", "type": "heating", "state": {"target": 20, "boost": 5}}
            )
        )
        with patch("apyhiveapi.hive._LOGGER") as mock_log:
            await hive.get_devices()
        shape_calls = [
            c.args for c in mock_log.debug.call_args_list if "arrived" in c.args[0]
        ]
        assert len(shape_calls) == 1
        assert shape_calls[0][1:] == ("heating", "heat-1", "boost, target")

    async def test_missing_state_block_keeps_previous_state(self):
        hive = self._stub({"mode": "OFF", "target": 7})
        hive.api.get_all = AsyncMock(return_value=_products_response({"id": "heat-1"}))
        await hive.get_devices()
        assert hive.data.products["heat-1"]["state"] == {"mode": "OFF", "target": 7}

    async def test_null_value_is_not_treated_as_missing(self):
        hive = self._stub({"mode": "OFF", "boost": 30})
        hive.api.get_all = AsyncMock(
            return_value=_products_response(
                {"id": "heat-1", "state": {"mode": "OFF", "boost": None}}
            )
        )
        with patch("apyhiveapi.hive._LOGGER") as mock_log:
            await hive.get_devices()
        assert hive.data.products["heat-1"]["state"] == {"mode": "OFF", "boost": None}
        mock_log.warning.assert_not_called()

    async def test_new_product_is_accepted_as_is(self):
        hive = self._stub({"mode": "OFF"})
        hive.api.get_all = AsyncMock(
            return_value=_products_response(
                {"id": "heat-1", "state": {"mode": "OFF"}},
                {"id": "heat-2", "state": {"target": 7}},
            )
        )
        await hive.get_devices()
        assert hive.data.products["heat-2"]["state"] == {"target": 7}

    async def test_gives_up_after_max_consecutive_incomplete_polls(self):
        hive = self._stub({"mode": "OFF", "target": 7})
        hive.api.get_all = AsyncMock(
            side_effect=lambda: _products_response(
                {"id": "heat-1", "state": {"target": 7}}
            )
        )
        for _ in range(MAX_STALE_STATE_POLLS):
            await hive.get_devices()
            assert hive.data.products["heat-1"]["state"]["mode"] == "OFF"
        await hive.get_devices()
        assert hive.data.products["heat-1"]["state"] == {"target": 7}
        # The shorter state is now the baseline, so the next poll is complete.
        with patch("apyhiveapi.hive._LOGGER") as mock_log:
            await hive.get_devices()
        mock_log.warning.assert_not_called()
        assert not hive._stale_state_polls

    async def test_complete_response_resets_the_count(self):
        hive = self._stub({"mode": "OFF"})
        hive.api.get_all = AsyncMock(
            side_effect=[
                _products_response({"id": "heat-1", "state": {}}),
                _products_response({"id": "heat-1", "state": {"mode": "MANUAL"}}),
                _products_response({"id": "heat-1", "state": {}}),
            ]
        )
        await hive.get_devices()
        assert hive._stale_state_polls == {"heat-1": 1}
        await hive.get_devices()
        assert not hive._stale_state_polls
        await hive.get_devices()
        assert hive._stale_state_polls == {"heat-1": 1}
        assert hive.data.products["heat-1"]["state"] == {"mode": "MANUAL"}

    async def test_optional_key_dropped_by_hive_is_not_carried_over(self):
        hive = self._stub({"mode": "MANUAL", "target": 20, "boost": 30, "hue": 10})
        hive.api.get_all = AsyncMock(
            return_value=_products_response(
                {"id": "heat-1", "state": {"mode": "MANUAL", "target": 20}}
            )
        )
        with patch("apyhiveapi.hive._LOGGER") as mock_log:
            await hive.get_devices()
        assert hive.data.products["heat-1"]["state"] == {"mode": "MANUAL", "target": 20}
        mock_log.warning.assert_not_called()
        assert not hive._stale_state_polls

    async def test_only_required_keys_are_carried_over(self):
        hive = self._stub({"mode": "MANUAL", "target": 20, "boost": 30})
        hive.api.get_all = AsyncMock(
            return_value=_products_response({"id": "heat-1", "state": {}})
        )
        await hive.get_devices()
        assert hive.data.products["heat-1"]["state"] == {"mode": "MANUAL", "target": 20}

    async def test_count_is_dropped_for_products_no_longer_returned(self):
        hive = self._stub({"mode": "OFF"})
        hive.api.get_all = AsyncMock(
            side_effect=[
                _products_response({"id": "heat-1", "state": {}}),
                _products_response({"id": "heat-2", "state": {"mode": "OFF"}}),
            ]
        )
        await hive.get_devices()
        assert hive._stale_state_polls == {"heat-1": 1}
        await hive.get_devices()
        assert not hive._stale_state_polls

    async def test_previous_state_not_a_dict_is_ignored(self):
        hive = _make_hive()
        hive.data.products = {"heat-1": {"id": "heat-1", "state": "broken"}}
        hive.api.get_all = AsyncMock(
            return_value=_products_response({"id": "heat-1", "state": {"mode": "OFF"}})
        )
        await hive.get_devices()
        assert hive.data.products["heat-1"]["state"] == {"mode": "OFF"}
