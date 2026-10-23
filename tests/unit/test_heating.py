"""Unit tests for the Climate handler (hive.heating)."""

import logging

import pytest

from apyhiveapi.exceptions import HiveApiError, HiveDeviceOffline
from apyhiveapi.heating import MAX_TARGET_TEMPERATURE, MIN_TARGET_TEMPERATURE, Climate
from tests.conftest import make_device, make_session

_ONLINE_DEVICES = {
    "dev-1": {"props": {"online": True, "model": "SLT"}, "parent": "hub"}
}
_OFFLINE_DEVICES = {
    "dev-1": {"props": {"online": False, "model": "SLT"}, "parent": "hub"}
}
_OFFLINE_STATUS = {
    "current_temperature": None,
    "target_temperature": None,
    "action": None,
    "mode": None,
    "boost": None,
}
_BOOSTING = {"mode": "BOOST", "target": 22, "boost": 30}


def _make_climate(products=None, devices=None):
    """Return a Climate bound to a mock session; the device is online by default."""
    devices = _ONLINE_DEVICES if devices is None else devices
    return Climate(make_session(products=products, devices=devices))


def _heating(**product):
    """Return the products dict for one heating product with the given fields."""
    return {"heat-1": {"type": "heating", **product}}


def _device(**kwargs):
    return make_device(hive_id="heat-1", ha_name="Hallway", **kwargs)


class TestGetCurrentTemperature:
    @pytest.mark.parametrize(
        ("raw", "expected"), [(20.25, 20.2), (19.26, 19.3), (20, 20.0), ("19.2", 19.2)]
    )
    async def test_rounds_to_one_decimal_place(self, raw, expected):
        climate = _make_climate(_heating(props={"temperature": raw}))
        result = await climate.get_current_temperature(_device())
        assert result == expected
        assert isinstance(result, float)

    @pytest.mark.parametrize("raw", ["abc", "", None, {}])
    async def test_non_numeric_values_are_none(self, raw):
        climate = _make_climate(_heating(props={"temperature": raw}))
        assert await climate.get_current_temperature(_device()) is None

    async def test_missing_temperature_is_none(self):
        climate = _make_climate(_heating(props={"working": False}))
        assert await climate.get_current_temperature(_device()) is None

    async def test_missing_props_is_none(self):
        climate = _make_climate(_heating())
        assert await climate.get_current_temperature(_device()) is None

    async def test_unknown_product_is_none(self):
        climate = _make_climate({})
        assert await climate.get_current_temperature(_device()) is None


class TestGetTargetTemperature:
    async def test_reads_the_target_as_a_float(self):
        climate = _make_climate(_heating(state={"target": 21}))
        result = await climate.get_target_temperature(_device())
        assert result == 21.0
        assert isinstance(result, float)

    async def test_prefers_target_over_heat(self):
        climate = _make_climate(_heating(state={"target": 21, "heat": 18.5}))
        assert await climate.get_target_temperature(_device()) == 21.0

    @pytest.mark.parametrize("state", [{"heat": 18.5}, {"target": None, "heat": 18.5}])
    async def test_falls_back_to_heat(self, state):
        climate = _make_climate(_heating(state=state))
        assert await climate.get_target_temperature(_device()) == 18.5

    async def test_numeric_string_is_accepted(self):
        climate = _make_climate(_heating(state={"target": "20.5"}))
        assert await climate.get_target_temperature(_device()) == 20.5

    async def test_neither_key_is_none(self):
        climate = _make_climate(_heating(state={"mode": "SCHEDULE"}))
        assert await climate.get_target_temperature(_device()) is None

    async def test_non_numeric_target_is_none(self):
        climate = _make_climate(_heating(state={"target": "abc", "heat": 18.5}))
        assert await climate.get_target_temperature(_device()) is None

    async def test_unknown_product_is_none(self):
        climate = _make_climate({})
        assert await climate.get_target_temperature(_device()) is None


class TestGetMode:
    @pytest.mark.parametrize("mode", ["SCHEDULE", "MANUAL", "OFF"])
    async def test_plain_modes_are_returned_as_they_are(self, mode):
        climate = _make_climate(_heating(state={"mode": mode}))
        assert await climate.get_mode(_device()) == mode

    async def test_false_is_off(self):
        climate = _make_climate(_heating(state={"mode": False}))
        assert await climate.get_mode(_device()) == "OFF"

    @pytest.mark.parametrize("previous", ["SCHEDULE", "MANUAL", "OFF"])
    async def test_boost_reports_the_previous_mode(self, previous):
        climate = _make_climate(
            _heating(state={"mode": "BOOST"}, props={"previous": {"mode": previous}})
        )
        assert await climate.get_mode(_device()) == previous

    async def test_boost_with_previous_false_is_off(self):
        climate = _make_climate(
            _heating(state={"mode": "BOOST"}, props={"previous": {"mode": False}})
        )
        assert await climate.get_mode(_device()) == "OFF"

    @pytest.mark.parametrize(
        "props", [{}, {"previous": {}}, {"previous": {"mode": None}}]
    )
    async def test_boost_without_a_previous_mode_is_none(self, props):
        climate = _make_climate(_heating(state={"mode": "BOOST"}, props=props))
        assert await climate.get_mode(_device()) is None

    async def test_missing_mode_is_none(self):
        climate = _make_climate(_heating(state={"target": 7}))
        assert await climate.get_mode(_device()) is None

    async def test_unknown_product_is_none(self):
        climate = _make_climate({})
        assert await climate.get_mode(_device()) is None


class TestGetCurrentOperation:
    @pytest.mark.parametrize("working", [True, False])
    async def test_reports_whether_the_zone_is_calling_for_heat(self, working):
        climate = _make_climate(_heating(props={"working": working}))
        assert await climate.get_current_operation(_device()) is working

    async def test_missing_working_is_none(self):
        climate = _make_climate(_heating(props={}))
        assert await climate.get_current_operation(_device()) is None


class TestSetTargetTemperature:
    async def test_sends_the_target_then_polls(self):
        climate = _make_climate(_heating())
        assert await climate.set_target_temperature(_device(), 21.0) is None
        climate.session.set_state.assert_awaited_once_with(
            "heating", "heat-1", target=21.0
        )
        climate.session.get_devices.assert_awaited_once_with()

    async def test_offline_device_raises(self):
        climate = _make_climate(_heating(), _OFFLINE_DEVICES)
        with pytest.raises(HiveDeviceOffline, match="Hallway is offline"):
            await climate.set_target_temperature(_device(), 21.0)
        climate.session.set_state.assert_not_awaited()
        climate.session.get_devices.assert_not_awaited()

    async def test_unknown_product_raises(self):
        climate = _make_climate({})
        with pytest.raises(HiveDeviceOffline):
            await climate.set_target_temperature(_device(), 21.0)
        climate.session.set_state.assert_not_awaited()


class TestSetMode:
    @pytest.mark.parametrize("mode", ["SCHEDULE", "MANUAL", "OFF"])
    async def test_sends_the_mode_then_polls(self, mode):
        climate = _make_climate(_heating())
        assert await climate.set_mode(_device(), mode) is None
        climate.session.set_state.assert_awaited_once_with(
            "heating", "heat-1", mode=mode
        )
        climate.session.get_devices.assert_awaited_once_with()

    async def test_offline_device_raises(self):
        climate = _make_climate(_heating(), _OFFLINE_DEVICES)
        with pytest.raises(HiveDeviceOffline):
            await climate.set_mode(_device(), "MANUAL")
        climate.session.set_state.assert_not_awaited()

    async def test_api_error_propagates_without_polling(self):
        climate = _make_climate(_heating())
        climate.session.set_state.side_effect = HiveApiError("refused")
        with pytest.raises(HiveApiError, match="refused"):
            await climate.set_mode(_device(), "MANUAL")
        climate.session.get_devices.assert_not_awaited()


class TestSetBoostOn:
    async def test_sends_boost_minutes_and_target_then_polls(self):
        climate = _make_climate(_heating())
        assert await climate.set_boost_on(_device(), 30, 22) is None
        climate.session.set_state.assert_awaited_once_with(
            "heating", "heat-1", mode="BOOST", boost=30, target=22.0
        )
        kwargs = climate.session.set_state.await_args.kwargs
        assert isinstance(kwargs["boost"], int)
        assert isinstance(kwargs["target"], float)
        climate.session.get_devices.assert_awaited_once_with()

    async def test_numeric_strings_are_converted(self):
        climate = _make_climate(_heating())
        await climate.set_boost_on(_device(), "30", "21.5")
        kwargs = climate.session.set_state.await_args.kwargs
        assert kwargs == {"mode": "BOOST", "boost": 30, "target": 21.5}
        assert isinstance(kwargs["boost"], int)

    async def test_fractional_minutes_are_truncated(self):
        climate = _make_climate(_heating())
        await climate.set_boost_on(_device(), 45.9, 20)
        assert climate.session.set_state.await_args.kwargs["boost"] == 45

    @pytest.mark.parametrize(
        "temp", [MIN_TARGET_TEMPERATURE, MAX_TARGET_TEMPERATURE, "5", 32]
    )
    async def test_range_limits_are_accepted(self, temp):
        climate = _make_climate(_heating())
        await climate.set_boost_on(_device(), 30, temp)
        assert climate.session.set_state.await_args.kwargs["target"] == float(temp)

    @pytest.mark.parametrize("temp", [4.9, 32.1, 0, 99, -5, "40"])
    async def test_out_of_range_temperature_raises(self, temp):
        climate = _make_climate(_heating())
        with pytest.raises(ValueError, match="Boost temperature must be between"):
            await climate.set_boost_on(_device(), 30, temp)
        climate.session.set_state.assert_not_awaited()
        climate.session.get_devices.assert_not_awaited()

    @pytest.mark.parametrize("mins", [0, -1, "0", 0.5, "-10"])
    async def test_non_positive_minutes_raise(self, mins):
        climate = _make_climate(_heating())
        with pytest.raises(ValueError, match="Boost minutes must be positive"):
            await climate.set_boost_on(_device(), mins, 20)
        climate.session.set_state.assert_not_awaited()

    @pytest.mark.parametrize("mins", ["abc", "1e2", ""])
    async def test_non_numeric_minutes_raise_value_error(self, mins):
        climate = _make_climate(_heating())
        with pytest.raises(ValueError, match="invalid literal"):
            await climate.set_boost_on(_device(), mins, 20)
        climate.session.set_state.assert_not_awaited()

    async def test_non_numeric_temperature_raises_value_error(self):
        climate = _make_climate(_heating())
        with pytest.raises(ValueError, match="could not convert"):
            await climate.set_boost_on(_device(), 30, "hot")
        climate.session.set_state.assert_not_awaited()

    @pytest.mark.parametrize(("mins", "temp"), [(None, 20), (30, None), ([], 20)])
    async def test_non_numeric_types_raise_type_error(self, mins, temp):
        climate = _make_climate(_heating())
        with pytest.raises(TypeError):
            await climate.set_boost_on(_device(), mins, temp)
        climate.session.set_state.assert_not_awaited()

    async def test_offline_device_raises(self):
        climate = _make_climate(_heating(), _OFFLINE_DEVICES)
        with pytest.raises(HiveDeviceOffline):
            await climate.set_boost_on(_device(), 30, 20)
        climate.session.set_state.assert_not_awaited()

    async def test_input_is_validated_before_the_device_is_checked(self):
        climate = _make_climate(_heating(), _OFFLINE_DEVICES)
        with pytest.raises(ValueError, match="Boost minutes must be positive"):
            await climate.set_boost_on(_device(), 0, 20)


class TestSetBoostOff:
    @pytest.mark.parametrize("boost", [None, False, 0])
    async def test_not_boosting_does_nothing(self, boost, caplog):
        climate = _make_climate(
            _heating(state={"boost": boost}, props={"previous": {"mode": "MANUAL"}})
        )
        caplog.set_level(logging.DEBUG, logger="apyhiveapi.heating")
        assert await climate.set_boost_off(_device()) is None
        climate.session.set_state.assert_not_awaited()
        climate.session.get_devices.assert_not_awaited()
        assert "Hallway is not boosting" in caplog.text

    async def test_missing_boost_key_does_nothing(self):
        climate = _make_climate(_heating(state={"mode": "SCHEDULE"}))
        await climate.set_boost_off(_device())
        climate.session.set_state.assert_not_awaited()

    async def test_unknown_product_does_nothing(self, caplog):
        climate = _make_climate({})
        caplog.set_level(logging.DEBUG, logger="apyhiveapi.heating")
        await climate.set_boost_off(_device())
        climate.session.set_state.assert_not_awaited()
        assert "Hallway is not boosting" in caplog.text

    async def test_previous_manual_restores_its_target(self):
        climate = _make_climate(
            _heating(
                state=_BOOSTING, props={"previous": {"mode": "MANUAL", "target": 19.5}}
            )
        )
        assert await climate.set_boost_off(_device()) is None
        climate.session.set_state.assert_awaited_once_with(
            "heating", "heat-1", mode="MANUAL", target=19.5
        )
        climate.session.get_devices.assert_awaited_once_with()

    # Hive reports a previous mode of "off" either as "OFF" or as false.
    @pytest.mark.parametrize("previous", ["OFF", False])
    async def test_previous_off_restores_its_target(self, previous):
        climate = _make_climate(
            _heating(
                state=_BOOSTING, props={"previous": {"mode": previous, "target": 7}}
            )
        )
        await climate.set_boost_off(_device())
        climate.session.set_state.assert_awaited_once_with(
            "heating", "heat-1", mode="OFF", target=7
        )

    @pytest.mark.parametrize(
        "previous", [{"mode": "OFF"}, {"mode": "MANUAL", "target": None}]
    )
    async def test_previous_without_a_target_falls_back_to_seven(self, previous):
        climate = _make_climate(_heating(state=_BOOSTING, props={"previous": previous}))
        await climate.set_boost_off(_device())
        climate.session.set_state.assert_awaited_once_with(
            "heating", "heat-1", mode=previous["mode"], target=7
        )

    async def test_previous_schedule_sends_no_target(self):
        climate = _make_climate(
            _heating(
                state=_BOOSTING,
                props={"previous": {"mode": "SCHEDULE", "target": 19.5}},
            )
        )
        await climate.set_boost_off(_device())
        climate.session.set_state.assert_awaited_once_with(
            "heating", "heat-1", mode="SCHEDULE"
        )

    @pytest.mark.parametrize(
        "props", [{}, {"previous": {}}, {"previous": {"mode": None, "target": 20}}]
    )
    async def test_missing_previous_mode_raises_api_error(self, props):
        climate = _make_climate(_heating(state=_BOOSTING, props=props))
        with pytest.raises(HiveApiError, match="Hallway"):
            await climate.set_boost_off(_device())
        climate.session.set_state.assert_not_awaited()
        climate.session.get_devices.assert_not_awaited()

    async def test_offline_device_raises(self):
        climate = _make_climate(
            _heating(state=_BOOSTING, props={"previous": {"mode": "SCHEDULE"}}),
            _OFFLINE_DEVICES,
        )
        with pytest.raises(HiveDeviceOffline):
            await climate.set_boost_off(_device())
        climate.session.set_state.assert_not_awaited()


class TestGetClimate:
    async def test_online_device_gets_the_full_status(self):
        climate = _make_climate(
            _heating(
                state={"mode": "MANUAL", "target": 21, "boost": None},
                props={"temperature": 20.25, "working": True},
            )
        )
        device = _device(parent_device="old-hub")
        result = await climate.get_climate(device)
        assert result is device
        assert device.status == {
            "current_temperature": 20.2,
            "target_temperature": 21.0,
            "action": True,
            "mode": "MANUAL",
            "boost": "OFF",
        }
        assert device.min_temp == MIN_TARGET_TEMPERATURE
        assert device.max_temp == MAX_TARGET_TEMPERATURE
        assert device.device_data == {"online": True, "model": "SLT"}
        assert device.parent_device == "hub"
        climate.session.device_recovered.assert_called_once_with("dev-1")
        climate.session.report_device_offline.assert_not_called()

    async def test_boosting_device_reports_boost_and_the_previous_mode(self):
        climate = _make_climate(
            _heating(
                state=_BOOSTING,
                props={
                    "previous": {"mode": "SCHEDULE"},
                    "temperature": 19,
                    "working": True,
                },
            )
        )
        device = await climate.get_climate(_device())
        assert device.status == {
            "current_temperature": 19.0,
            "target_temperature": 22.0,
            "action": True,
            "mode": "SCHEDULE",
            "boost": "ON",
        }

    async def test_sparse_product_reports_unknown_values_while_online(self):
        climate = _make_climate(_heating())
        device = await climate.get_climate(_device())
        assert device.status == _OFFLINE_STATUS
        assert device.device_data["online"] is True
        assert device.min_temp == MIN_TARGET_TEMPERATURE
        assert device.max_temp == MAX_TARGET_TEMPERATURE
        climate.session.device_recovered.assert_called_once_with("dev-1")

    async def test_offline_device_gets_the_offline_status(self):
        climate = _make_climate(
            _heating(state={"mode": "MANUAL", "target": 21}, props={"temperature": 20}),
            _OFFLINE_DEVICES,
        )
        device = _device(status={"mode": "MANUAL", "boost": "OFF"})
        result = await climate.get_climate(device)
        assert result is device
        assert device.status == _OFFLINE_STATUS
        assert device.device_data == {"online": False, "model": "SLT"}
        assert device.parent_device == "hub"
        assert device.min_temp is None
        assert device.max_temp is None
        climate.session.report_device_offline.assert_called_once_with("dev-1")
        climate.session.device_recovered.assert_not_called()

    async def test_offline_statuses_are_independent_dicts(self):
        climate = _make_climate(_heating(), _OFFLINE_DEVICES)
        first = await climate.get_climate(_device())
        second = await climate.get_climate(_device())
        assert first.status is not second.status
        first.status["mode"] = "MANUAL"
        third = await climate.get_climate(_device())
        assert second.status == _OFFLINE_STATUS
        assert third.status == _OFFLINE_STATUS

    async def test_unknown_product_is_offline(self):
        climate = _make_climate({})
        device = await climate.get_climate(_device())
        assert device.status == _OFFLINE_STATUS
        assert device.device_data["online"] is False
        climate.session.report_device_offline.assert_called_once_with("dev-1")

    async def test_device_data_none_is_replaced(self):
        climate = _make_climate(_heating(state={"mode": "OFF"}))
        device = await climate.get_climate(_device(device_data=None))
        assert device.device_data == {"online": True, "model": "SLT"}
        assert device.status["mode"] == "OFF"

    async def test_status_is_restored_when_the_device_recovers(self):
        devices = {"dev-1": {"props": {"online": False}}}
        climate = _make_climate(
            _heating(state={"mode": "OFF", "target": 7}, props={"temperature": 18}),
            devices,
        )
        device = _device()
        await climate.get_climate(device)
        assert device.status == _OFFLINE_STATUS
        devices["dev-1"]["props"]["online"] = True
        await climate.get_climate(device)
        assert device.status["mode"] == "OFF"
        assert device.status["current_temperature"] == 18.0
        assert device.device_data["online"] is True
        climate.session.report_device_offline.assert_called_once_with("dev-1")
        climate.session.device_recovered.assert_called_once_with("dev-1")
