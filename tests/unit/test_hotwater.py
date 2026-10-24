"""Unit tests for the WaterHeater handler (hive.hotwater)."""

import logging

import pytest

from apyhiveapi.exceptions import HiveApiError, HiveDeviceOffline
from apyhiveapi.hotwater import WaterHeater
from tests.conftest import make_device, make_session

_THERMOSTAT = {"dev-1": {"props": {"online": True, "model": "SLT"}}}
_BOOSTING = {"mode": "BOOST", "boost": 60}


def _make_hotwater(products=None):
    """Return a WaterHeater bound to a mock session with the zone's thermostat."""
    return WaterHeater(make_session(products=products, devices=_THERMOSTAT))


def _hotwater(online=True, **product):
    """Return the products dict for one hot water product with the given fields."""
    props = {"online": online, **product.pop("props", {})}
    return {"hw-1": {"type": "hotwater", **product, "props": props}}


def _device(**kwargs):
    return make_device(
        hive_id="hw-1",
        hive_type="hotwater",
        ha_type="water_heater",
        ha_name="Hot Water",
        **kwargs,
    )


class TestGetMode:
    @pytest.mark.parametrize(
        ("mode", "expected"),
        [("SCHEDULE", "SCHEDULE"), ("MANUAL", "ON"), ("OFF", "OFF"), (False, "OFF")],
    )
    async def test_maps_hive_modes_to_operations(self, mode, expected):
        heater = _make_hotwater(_hotwater(state={"mode": mode}))
        assert await heater.get_mode(_device()) == expected

    async def test_none_mode_is_none(self):
        heater = _make_hotwater(_hotwater(state={"mode": None}))
        assert await heater.get_mode(_device()) is None

    async def test_missing_mode_is_none(self):
        heater = _make_hotwater(_hotwater(state={"status": "OFF"}))
        assert await heater.get_mode(_device()) is None

    async def test_missing_state_is_none(self):
        heater = _make_hotwater(_hotwater())
        assert await heater.get_mode(_device()) is None

    async def test_unknown_product_is_none(self):
        heater = _make_hotwater({})
        assert await heater.get_mode(_device()) is None

    @pytest.mark.parametrize(
        ("previous", "expected"),
        [("SCHEDULE", "SCHEDULE"), ("MANUAL", "ON"), ("OFF", "OFF"), (False, "OFF")],
    )
    async def test_boost_reports_the_mapped_previous_mode(self, previous, expected):
        heater = _make_hotwater(
            _hotwater(state=_BOOSTING, props={"previous": {"mode": previous}})
        )
        assert await heater.get_mode(_device()) == expected

    @pytest.mark.parametrize(
        "props",
        [
            pytest.param({}, id="no-previous"),
            pytest.param({"previous": {}}, id="previous-without-mode"),
            pytest.param({"previous": {"mode": None}}, id="previous-mode-none"),
        ],
    )
    async def test_boost_without_a_previous_mode_is_unknown(self, props):
        heater = _make_hotwater(_hotwater(state=_BOOSTING, props=props))
        assert await heater.get_mode(_device()) is None


class TestSetMode:
    @pytest.mark.parametrize("mode", ["SCHEDULE", "MANUAL", "OFF"])
    async def test_sends_the_mode_then_polls(self, mode):
        heater = _make_hotwater(_hotwater())
        assert await heater.set_mode(_device(), mode) is None
        heater.session.set_state.assert_awaited_once_with("hotwater", "hw-1", mode=mode)
        heater.session.get_devices.assert_awaited_once_with()

    async def test_offline_device_raises(self):
        heater = _make_hotwater(_hotwater(online=False))
        with pytest.raises(HiveDeviceOffline, match="Hot Water is offline"):
            await heater.set_mode(_device(), "MANUAL")
        heater.session.set_state.assert_not_awaited()
        heater.session.get_devices.assert_not_awaited()

    async def test_unknown_product_raises(self):
        heater = _make_hotwater({})
        with pytest.raises(HiveDeviceOffline):
            await heater.set_mode(_device(), "MANUAL")
        heater.session.set_state.assert_not_awaited()

    async def test_api_error_propagates_without_polling(self):
        heater = _make_hotwater(_hotwater())
        heater.session.set_state.side_effect = HiveApiError("refused")
        with pytest.raises(HiveApiError, match="refused"):
            await heater.set_mode(_device(), "MANUAL")
        heater.session.get_devices.assert_not_awaited()


class TestSetBoostOn:
    async def test_sends_boost_minutes_without_a_target_then_polls(self):
        heater = _make_hotwater(_hotwater())
        assert await heater.set_boost_on(_device(), 60) is None
        heater.session.set_state.assert_awaited_once_with(
            "hotwater", "hw-1", mode="BOOST", boost=60
        )
        heater.session.get_devices.assert_awaited_once_with()

    @pytest.mark.parametrize(("mins", "expected"), [("30", 30), (45.9, 45), (1, 1)])
    async def test_minutes_are_converted_to_int(self, mins, expected):
        heater = _make_hotwater(_hotwater())
        await heater.set_boost_on(_device(), mins)
        boost = heater.session.set_state.await_args.kwargs["boost"]
        assert boost == expected
        assert isinstance(boost, int)

    @pytest.mark.parametrize("mins", [0, -1, "0", 0.9])
    async def test_non_positive_minutes_raise(self, mins):
        heater = _make_hotwater(_hotwater())
        with pytest.raises(ValueError, match="Boost minutes must be positive"):
            await heater.set_boost_on(_device(), mins)
        heater.session.set_state.assert_not_awaited()
        heater.session.get_devices.assert_not_awaited()

    async def test_non_numeric_minutes_raise_value_error(self):
        heater = _make_hotwater(_hotwater())
        with pytest.raises(ValueError, match="invalid literal"):
            await heater.set_boost_on(_device(), "abc")
        heater.session.set_state.assert_not_awaited()

    async def test_none_minutes_raise_type_error(self):
        heater = _make_hotwater(_hotwater())
        with pytest.raises(TypeError):
            await heater.set_boost_on(_device(), None)
        heater.session.set_state.assert_not_awaited()

    async def test_offline_device_raises(self):
        heater = _make_hotwater(_hotwater(online=False))
        with pytest.raises(HiveDeviceOffline):
            await heater.set_boost_on(_device(), 60)
        heater.session.set_state.assert_not_awaited()


class TestSetBoostOff:
    @pytest.mark.parametrize("boost", [None, False, 0])
    async def test_not_boosting_does_nothing(self, boost, caplog):
        heater = _make_hotwater(
            _hotwater(state={"boost": boost}, props={"previous": {"mode": "MANUAL"}})
        )
        caplog.set_level(logging.DEBUG, logger="apyhiveapi.hotwater")
        assert await heater.set_boost_off(_device()) is None
        heater.session.set_state.assert_not_awaited()
        heater.session.get_devices.assert_not_awaited()
        assert "Hot Water is not boosting" in caplog.text

    async def test_missing_boost_key_does_nothing(self):
        heater = _make_hotwater(_hotwater(state={"mode": "SCHEDULE"}))
        await heater.set_boost_off(_device())
        heater.session.set_state.assert_not_awaited()

    async def test_unknown_product_does_nothing(self, caplog):
        heater = _make_hotwater({})
        caplog.set_level(logging.DEBUG, logger="apyhiveapi.hotwater")
        await heater.set_boost_off(_device())
        heater.session.set_state.assert_not_awaited()
        assert "Hot Water is not boosting" in caplog.text

    # Hive reports a previous mode of "off" either as "OFF" or as false.
    @pytest.mark.parametrize(
        ("previous", "sent"),
        [
            ("SCHEDULE", "SCHEDULE"),
            ("MANUAL", "MANUAL"),
            ("OFF", "OFF"),
            (False, "OFF"),
        ],
    )
    async def test_restores_the_previous_mode_without_a_target(self, previous, sent):
        heater = _make_hotwater(
            _hotwater(state=_BOOSTING, props={"previous": {"mode": previous}})
        )
        assert await heater.set_boost_off(_device()) is None
        heater.session.set_state.assert_awaited_once_with("hotwater", "hw-1", mode=sent)
        heater.session.get_devices.assert_awaited_once_with()

    @pytest.mark.parametrize(
        "props",
        [
            pytest.param({}, id="no-previous"),
            pytest.param({"previous": {}}, id="previous-without-mode"),
            pytest.param({"previous": {"mode": None}}, id="previous-mode-none"),
        ],
    )
    async def test_missing_previous_mode_raises_api_error(self, props):
        heater = _make_hotwater(_hotwater(state=_BOOSTING, props=props))
        with pytest.raises(HiveApiError, match="Hot Water"):
            await heater.set_boost_off(_device())
        heater.session.set_state.assert_not_awaited()
        heater.session.get_devices.assert_not_awaited()

    async def test_offline_device_raises(self):
        heater = _make_hotwater(
            _hotwater(
                state=_BOOSTING, props={"previous": {"mode": "SCHEDULE"}}, online=False
            )
        )
        with pytest.raises(HiveDeviceOffline):
            await heater.set_boost_off(_device())
        heater.session.set_state.assert_not_awaited()


class TestGetWaterHeater:
    async def test_online_device_gets_its_status(self):
        heater = _make_hotwater(_hotwater(state={"mode": "SCHEDULE"}))
        device = _device()
        result = await heater.get_water_heater(device)
        assert result is device
        assert device.status == {"current_operation": "SCHEDULE"}
        assert device.device_data == {"online": True, "model": "SLT"}
        heater.session.device_recovered.assert_called_once_with("hw-1")
        heater.session.report_device_offline.assert_not_called()

    async def test_manual_reports_on(self):
        heater = _make_hotwater(_hotwater(state={"mode": "MANUAL"}))
        device = await heater.get_water_heater(_device())
        assert device.status == {"current_operation": "ON"}

    async def test_boosting_device_reports_the_previous_mode(self):
        heater = _make_hotwater(
            _hotwater(state=_BOOSTING, props={"previous": {"mode": "MANUAL"}})
        )
        device = await heater.get_water_heater(_device())
        assert device.status == {"current_operation": "ON"}

    async def test_sparse_product_reports_unknown_while_online(self):
        heater = _make_hotwater(_hotwater())
        device = await heater.get_water_heater(_device())
        assert device.status == {"current_operation": None}
        assert device.device_data["online"] is True

    async def test_offline_device_gets_the_offline_status(self):
        heater = _make_hotwater(_hotwater(state={"mode": "SCHEDULE"}, online=False))
        device = _device(status={"current_operation": "SCHEDULE"})
        result = await heater.get_water_heater(device)
        assert result is device
        assert device.status == {"current_operation": None}
        assert device.device_data == {"online": False, "model": "SLT"}
        heater.session.report_device_offline.assert_called_once_with("hw-1")
        heater.session.device_recovered.assert_not_called()

    async def test_unknown_product_is_offline(self):
        heater = _make_hotwater({})
        device = await heater.get_water_heater(_device())
        assert device.status == {"current_operation": None}
        assert device.device_data["online"] is False
        heater.session.report_device_offline.assert_called_once_with("hw-1")

    async def test_device_data_none_is_replaced(self):
        heater = _make_hotwater(_hotwater(state={"mode": "OFF"}))
        device = await heater.get_water_heater(_device(device_data=None))
        assert device.device_data == {"online": True, "model": "SLT"}
        assert device.status == {"current_operation": "OFF"}

    async def test_status_is_restored_when_the_device_recovers(self):
        products = _hotwater(state={"mode": "SCHEDULE"}, online=False)
        heater = _make_hotwater(products)
        device = _device()
        await heater.get_water_heater(device)
        assert device.status == {"current_operation": None}
        products["hw-1"]["props"]["online"] = True
        await heater.get_water_heater(device)
        assert device.status == {"current_operation": "SCHEDULE"}
        assert device.device_data["online"] is True
        heater.session.report_device_offline.assert_called_once_with("hw-1")
        heater.session.device_recovered.assert_called_once_with("hw-1")
