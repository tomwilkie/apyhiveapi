"""Tests for the WaterHeater handler."""

# pylint: disable=protected-access

from unittest.mock import patch

from apyhiveapi.hotwater import WaterHeater
from tests.conftest import make_device, make_session

_ONLINE_DEVICES = {"dev-1": {"props": {"online": True}, "parent": "hub"}}
_BOOST_MINS = 30


def _make_hotwater(products=None, devices=None):
    """Return a WaterHeater bound to a mock session; the device is online by default."""
    devices = _ONLINE_DEVICES if devices is None else devices
    return WaterHeater(make_session(products=products, devices=devices))


def _device(**kwargs):
    return make_device(
        hive_id="hw-1",
        hive_type="hotwater",
        ha_type="water_heater",
        ha_name="Hot Water",
        **kwargs,
    )


class TestGetMode:
    async def test_schedule_mode_is_returned_as_is(self):
        hw = _make_hotwater({"hw-1": {"state": {"mode": "SCHEDULE"}}})
        assert await hw.get_mode(_device()) == "SCHEDULE"

    async def test_manual_maps_to_on(self):
        hw = _make_hotwater({"hw-1": {"state": {"mode": "MANUAL"}}})
        assert await hw.get_mode(_device()) == "ON"

    async def test_boost_reads_previous_mode(self):
        hw = _make_hotwater(
            {
                "hw-1": {
                    "state": {"mode": "BOOST"},
                    "props": {"previous": {"mode": "MANUAL"}},
                }
            }
        )
        assert await hw.get_mode(_device()) == "ON"

    async def test_boost_missing_previous_returns_off_without_error_log(self):
        hw = _make_hotwater({"hw-1": {"state": {"mode": "BOOST"}, "props": {}}})
        with patch("apyhiveapi.hotwater._LOGGER") as mock_log:
            assert await hw.get_mode(_device()) == "OFF"
        mock_log.error.assert_not_called()

    async def test_missing_mode_returns_none_without_error_log(self):
        hw = _make_hotwater({"hw-1": {"state": {"status": "OFF"}}})
        with patch("apyhiveapi.hotwater._LOGGER") as mock_log:
            assert await hw.get_mode(_device()) is None
        mock_log.error.assert_not_called()

    async def test_missing_state_returns_none(self):
        hw = _make_hotwater({"hw-1": {}})
        assert await hw.get_mode(_device()) is None


class TestSetMode:
    async def test_calls_execute_with_mode(self):
        hw = _make_hotwater({"hw-1": {"type": "hotwater"}})
        assert await hw.set_mode(_device(), "MANUAL") is True
        _, kwargs = hw.session.api.set_state.call_args
        assert kwargs == {"mode": "MANUAL"}


class TestSetBoostOn:
    async def test_valid_mins_calls_execute(self):
        hw = _make_hotwater({"hw-1": {"type": "hotwater"}})
        assert await hw.set_boost_on(_device(), _BOOST_MINS) is True
        _, kwargs = hw.session.api.set_state.call_args
        assert kwargs == {"mode": "BOOST", "boost": _BOOST_MINS}

    async def test_string_minutes_are_accepted(self):
        hw = _make_hotwater({"hw-1": {"type": "hotwater"}})
        assert await hw.set_boost_on(_device(), "30") is True

    async def test_zero_mins_returns_false(self):
        hw = _make_hotwater({"hw-1": {"type": "hotwater"}})
        assert await hw.set_boost_on(_device(), 0) is False
        hw.session.api.set_state.assert_not_called()


class TestSetBoostOff:
    async def test_not_in_products_returns_false(self):
        hw = _make_hotwater()
        assert await hw.set_boost_off(_device()) is False

    async def test_not_boosting_returns_false(self):
        hw = _make_hotwater({"hw-1": {"state": {"boost": False}}})
        assert await hw.set_boost_off(_device()) is False

    async def test_boosting_restores_previous_mode(self):
        hw = _make_hotwater(
            {
                "hw-1": {
                    "type": "hotwater",
                    "state": {"boost": _BOOST_MINS},
                    "props": {"previous": {"mode": "SCHEDULE"}},
                }
            }
        )
        assert await hw.set_boost_off(_device()) is True
        _, kwargs = hw.session.api.set_state.call_args
        assert kwargs == {"mode": "SCHEDULE"}

    async def test_missing_previous_mode_returns_false(self):
        hw = _make_hotwater(
            {"hw-1": {"type": "hotwater", "state": {"boost": _BOOST_MINS}, "props": {}}}
        )
        assert await hw.set_boost_off(_device()) is False
        hw.session.api.set_state.assert_not_called()


class TestGetWaterHeater:
    async def test_online_device_is_updated_and_cached(self):
        hw = _make_hotwater({"hw-1": {"state": {"mode": "SCHEDULE"}}})
        device = _device()
        result = await hw.get_water_heater(device)
        assert result is device
        assert device.status == {"current_operation": "SCHEDULE"}
        assert device.device_data == {"online": True}
        assert device.parent_device == "hub"
        hw.session.device_recovered.assert_called_once_with("dev-1")
        hw.session.set_cached_device.assert_called_once_with(device)

    async def test_device_data_not_dict_gets_reset(self):
        hw = _make_hotwater(products={"hw-1": {"state": {"mode": "SCHEDULE"}}})
        device = _device(device_data=None)
        await hw.get_water_heater(device)
        assert isinstance(device.device_data, dict)

    async def test_offline_device_is_reported_with_default_status(self):
        hw = _make_hotwater(
            products={"hw-1": {}}, devices={"dev-1": {"props": {"online": False}}}
        )
        result = await hw.get_water_heater(_device())
        hw.session.report_device_offline.assert_called_once_with("dev-1")
        assert result.status == {"current_operation": None}

    async def test_cache_hit_returns_cached(self):
        hw = _make_hotwater()
        cached_device = _device(status={"current_operation": "SCHEDULE"})
        hw.session.should_use_cached_data.return_value = True
        hw.session.get_cached_device.return_value = cached_device
        with patch.object(hw, "_online") as online:
            assert await hw.get_water_heater(_device()) is cached_device
        online.assert_not_called()

    async def test_cache_miss_falls_through(self):
        hw = _make_hotwater({"hw-1": {"state": {"mode": "MANUAL"}}})
        hw.session.should_use_cached_data.return_value = True
        hw.session.get_cached_device.return_value = None
        result = await hw.get_water_heater(_device())
        assert result.status == {"current_operation": "ON"}
