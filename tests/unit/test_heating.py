"""Tests for the Climate handler."""

# pylint: disable=protected-access

from unittest.mock import patch

from apyhiveapi.heating import MAX_TARGET_TEMPERATURE, MIN_TARGET_TEMPERATURE, Climate
from tests.conftest import make_device, make_session

_ONLINE_DEVICES = {
    "dev-1": {"props": {"online": True, "model": "SLT"}, "parent": "hub"}
}
_RAW_TEMP = 20.25
_ROUNDED_TEMP = 20.2
_TARGET = 21.0
_HEAT = 18.5
_BOOST_MINS = "30"
_VALID_BOOST_TEMP = 22
_RESTORE_TARGET = 19.0
_FALLBACK_TARGET = 7


def _make_climate(products=None, devices=None):
    """Return a Climate bound to a mock session; the device is online by default."""
    devices = _ONLINE_DEVICES if devices is None else devices
    return Climate(make_session(products=products, devices=devices))


def _device(**kwargs):
    return make_device(hive_id="heat-1", ha_name="Hallway", **kwargs)


class TestGetCurrentTemperature:
    async def test_happy_path_returns_rounded_float(self):
        climate = _make_climate({"heat-1": {"props": {"temperature": _RAW_TEMP}}})
        assert await climate.get_current_temperature(_device()) == _ROUNDED_TEMP

    async def test_non_numeric_returns_none(self):
        climate = _make_climate({"heat-1": {"props": {"temperature": "N/A"}}})
        assert await climate.get_current_temperature(_device()) is None

    async def test_missing_product_returns_none(self):
        climate = _make_climate(products={})
        assert await climate.get_current_temperature(_device()) is None


class TestGetTargetTemperature:
    async def test_reads_target_key(self):
        climate = _make_climate({"heat-1": {"state": {"target": _TARGET}}})
        assert await climate.get_target_temperature(_device()) == _TARGET

    async def test_falls_back_to_heat_key(self):
        climate = _make_climate({"heat-1": {"state": {"heat": _HEAT}}})
        assert await climate.get_target_temperature(_device()) == _HEAT

    async def test_both_absent_returns_none(self):
        climate = _make_climate({"heat-1": {"state": {}}})
        assert await climate.get_target_temperature(_device()) is None

    async def test_non_numeric_target_returns_none(self):
        climate = _make_climate({"heat-1": {"state": {"target": "N/A"}}})
        assert await climate.get_target_temperature(_device()) is None

    async def test_missing_product_returns_none(self):
        climate = _make_climate(products={})
        assert await climate.get_target_temperature(_device()) is None


class TestGetMode:
    async def test_schedule_mode_is_returned_as_is(self):
        climate = _make_climate({"heat-1": {"state": {"mode": "SCHEDULE"}}})
        assert await climate.get_mode(_device()) == "SCHEDULE"

    async def test_boost_reads_previous_mode(self):
        climate = _make_climate(
            {
                "heat-1": {
                    "state": {"mode": "BOOST"},
                    "props": {"previous": {"mode": "MANUAL"}},
                }
            }
        )
        assert await climate.get_mode(_device()) == "MANUAL"

    async def test_boost_missing_previous_returns_none_without_error_log(self):
        climate = _make_climate({"heat-1": {"state": {"mode": "BOOST"}, "props": {}}})
        with patch("apyhiveapi.heating._LOGGER") as mock_log:
            assert await climate.get_mode(_device()) is None
        mock_log.error.assert_not_called()

    async def test_missing_mode_returns_none_without_error_log(self):
        climate = _make_climate({"heat-1": {"state": {"target": 7}}})
        with patch("apyhiveapi.heating._LOGGER") as mock_log:
            assert await climate.get_mode(_device()) is None
        mock_log.error.assert_not_called()

    async def test_false_mode_maps_to_off(self):
        climate = _make_climate({"heat-1": {"state": {"mode": False}}})
        assert await climate.get_mode(_device()) == "OFF"


class TestGetCurrentOperation:
    async def test_returns_working_state(self):
        climate = _make_climate({"heat-1": {"props": {"working": True}}})
        assert await climate.get_current_operation(_device()) is True


class TestSetTargetTemperature:
    async def test_calls_execute_with_target(self):
        climate = _make_climate({"heat-1": {"type": "heating"}})
        assert await climate.set_target_temperature(_device(), _TARGET) is True
        _, kwargs = climate.session.api.set_state.call_args
        assert kwargs == {"target": _TARGET}


class TestSetMode:
    async def test_calls_execute_with_mode(self):
        climate = _make_climate({"heat-1": {"type": "heating"}})
        assert await climate.set_mode(_device(), "MANUAL") is True
        _, kwargs = climate.session.api.set_state.call_args
        assert kwargs == {"mode": "MANUAL"}


class TestSetBoostOn:
    async def test_valid_range_calls_execute(self):
        climate = _make_climate({"heat-1": {"type": "heating"}})
        result = await climate.set_boost_on(_device(), _BOOST_MINS, _VALID_BOOST_TEMP)
        assert result is True
        _, kwargs = climate.session.api.set_state.call_args
        assert kwargs == {"mode": "BOOST", "boost": _BOOST_MINS, "target": 22}

    async def test_out_of_range_temp_returns_none(self):
        climate = _make_climate({"heat-1": {"type": "heating"}})
        assert await climate.set_boost_on(_device(), _BOOST_MINS, 99) is None
        climate.session.api.set_state.assert_not_called()

    async def test_temp_fraction_above_max_returns_none(self):
        """32.9 exceeds the maximum and must not be truncated past validation."""
        climate = _make_climate({"heat-1": {"type": "heating"}})
        above_max = MAX_TARGET_TEMPERATURE + 0.9
        assert await climate.set_boost_on(_device(), _BOOST_MINS, above_max) is None

    async def test_minimum_temperature_is_accepted(self):
        climate = _make_climate({"heat-1": {"type": "heating"}})
        result = await climate.set_boost_on(_device(), 10, MIN_TARGET_TEMPERATURE)
        assert result is True

    async def test_zero_mins_returns_none(self):
        climate = _make_climate({"heat-1": {"type": "heating"}})
        assert await climate.set_boost_on(_device(), "0", _VALID_BOOST_TEMP) is None

    async def test_non_numeric_mins_returns_none(self):
        climate = _make_climate({"heat-1": {"type": "heating"}})
        assert await climate.set_boost_on(_device(), "abc", 20) is None

    async def test_non_numeric_temp_returns_none(self):
        climate = _make_climate({"heat-1": {"type": "heating"}})
        assert await climate.set_boost_on(_device(), "30", "hot") is None


class TestSetBoostOff:
    async def test_not_in_products_returns_false(self):
        climate = _make_climate(products={})
        assert await climate.set_boost_off(_device()) is False

    async def test_offline_returns_false(self):
        climate = _make_climate({"heat-1": {"state": {"boost": 5}}})
        assert (
            await climate.set_boost_off(_device(device_data={"online": False})) is False
        )

    async def test_not_boosting_returns_false(self):
        climate = _make_climate({"heat-1": {"state": {"boost": False}}})
        assert await climate.set_boost_off(_device()) is False

    async def test_previous_manual_restores_target(self):
        climate = _make_climate(
            {
                "heat-1": {
                    "type": "heating",
                    "state": {"boost": 5},
                    "props": {
                        "previous": {"mode": "MANUAL", "target": _RESTORE_TARGET}
                    },
                }
            }
        )
        assert await climate.set_boost_off(_device()) is True
        _, kwargs = climate.session.api.set_state.call_args
        assert kwargs == {"mode": "MANUAL", "target": _RESTORE_TARGET}

    async def test_previous_off_without_target_falls_back(self):
        climate = _make_climate(
            {
                "heat-1": {
                    "type": "heating",
                    "state": {"boost": 5},
                    "props": {"previous": {"mode": "OFF", "target": None}},
                }
            }
        )
        assert await climate.set_boost_off(_device()) is True
        _, kwargs = climate.session.api.set_state.call_args
        assert kwargs == {"mode": "OFF", "target": _FALLBACK_TARGET}

    async def test_previous_schedule_sends_no_target(self):
        climate = _make_climate(
            {
                "heat-1": {
                    "type": "heating",
                    "state": {"boost": 5},
                    "props": {"previous": {"mode": "SCHEDULE"}},
                }
            }
        )
        assert await climate.set_boost_off(_device()) is True
        _, kwargs = climate.session.api.set_state.call_args
        assert kwargs == {"mode": "SCHEDULE"}

    async def test_missing_previous_mode_returns_false(self):
        climate = _make_climate(
            {"heat-1": {"type": "heating", "state": {"boost": 5}, "props": {}}}
        )
        assert await climate.set_boost_off(_device()) is False
        climate.session.api.set_state.assert_not_called()


class TestGetClimate:
    async def test_online_device_is_fully_updated_and_cached(self):
        climate = _make_climate(
            {
                "heat-1": {
                    "state": {"mode": "MANUAL", "target": _TARGET, "boost": False},
                    "props": {"temperature": _RAW_TEMP, "working": True},
                }
            }
        )
        device = _device()
        result = await climate.get_climate(device)
        assert result is device
        assert device.status == {
            "current_temperature": _ROUNDED_TEMP,
            "target_temperature": _TARGET,
            "action": True,
            "mode": "MANUAL",
            "boost": "OFF",
        }
        assert device.min_temp == MIN_TARGET_TEMPERATURE
        assert device.max_temp == MAX_TARGET_TEMPERATURE
        assert device.device_data == {"online": True, "model": "SLT"}
        assert device.parent_device == "hub"
        climate.session.device_recovered.assert_called_once_with("dev-1")
        climate.session.set_cached_device.assert_called_once_with(device)

    async def test_device_data_not_dict_gets_reset(self):
        climate = _make_climate(products={"heat-1": {"props": {}, "state": {}}})
        device = _device(device_data=None)
        await climate.get_climate(device)
        assert isinstance(device.device_data, dict)

    async def test_offline_device_is_reported_with_default_status(self):
        climate = _make_climate(
            products={"heat-1": {}}, devices={"dev-1": {"props": {"online": False}}}
        )
        result = await climate.get_climate(_device())
        climate.session.report_device_offline.assert_called_once_with("dev-1")
        assert result.status["current_temperature"] is None
        assert result.status["mode"] is None

    async def test_cache_hit_returns_cached(self):
        climate = _make_climate()
        cached_device = _device(status={"current_temperature": 20.0})
        climate.session.should_use_cached_data.return_value = True
        climate.session.get_cached_device.return_value = cached_device
        with patch.object(climate, "_online") as online:
            assert await climate.get_climate(_device()) is cached_device
        online.assert_not_called()

    async def test_cache_miss_falls_through_to_normal_path(self):
        climate = _make_climate(
            {"heat-1": {"state": {"mode": "MANUAL"}, "props": {"temperature": 19.0}}}
        )
        climate.session.should_use_cached_data.return_value = True
        climate.session.get_cached_device.return_value = None
        result = await climate.get_climate(_device())
        assert result.status["mode"] == "MANUAL"
