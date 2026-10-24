"""Unit tests for the DeviceHandler plumbing shared by Climate and WaterHeater."""

import logging
from unittest.mock import MagicMock, call

import pytest

from apyhiveapi.device_handler import DeviceHandler
from apyhiveapi.exceptions import HiveApiError, HiveDeviceOffline, HiveReauthRequired
from tests.conftest import make_device, make_session

_HEATING = {
    "prod-1": {
        "type": "heating",
        "state": {"mode": "SCHEDULE"},
        "props": {"online": True},
    }
}
_OFFLINE_HEATING = {"prod-1": {"type": "heating", "props": {"online": False}}}
_TRV_CONTROL = {"prod-1": {"type": "trvcontrol", "props": {"online": True}}}
_ONLINE_DEVICES = {"dev-1": {"props": {"online": True, "model": "SLT3", "battery": 80}}}
_OFFLINE_DEVICES = {"dev-1": {"props": {"online": False, "model": "SLT3"}}}


def _make_handler(products=None, devices=None):
    """Return a DeviceHandler bound to a mock session."""
    return DeviceHandler(make_session(products=products, devices=devices))


class TestGetProductState:
    def test_reads_a_nested_value(self):
        handler = _make_handler({"prod-1": {"state": {"mode": "SCHEDULE"}}})
        assert handler._get_product_state(make_device(), "state", "mode") == "SCHEDULE"

    def test_no_keys_returns_the_product(self):
        product = {"state": {"mode": "SCHEDULE"}}
        handler = _make_handler({"prod-1": product})
        assert handler._get_product_state(make_device()) is product

    @pytest.mark.parametrize("value", [0, False, "", None])
    def test_falsy_values_are_returned_as_they_are(self, value):
        handler = _make_handler({"prod-1": {"state": {"boost": value}}})
        result = handler._get_product_state(
            make_device(), "state", "boost", default="x"
        )
        assert result is value

    def test_missing_first_key_returns_none(self):
        handler = _make_handler({"prod-1": {}})
        assert handler._get_product_state(make_device(), "state") is None

    def test_missing_nested_key_returns_none(self):
        handler = _make_handler({"prod-1": {"state": {}}})
        assert handler._get_product_state(make_device(), "state", "mode") is None

    def test_unknown_product_returns_none(self):
        handler = _make_handler({})
        assert handler._get_product_state(make_device(), "state") is None

    def test_explicit_default_for_a_missing_key(self):
        handler = _make_handler({"prod-1": {"state": {}}})
        result = handler._get_product_state(make_device(), "state", "boost", default=0)
        assert result == 0

    def test_explicit_default_for_an_unknown_product(self):
        handler = _make_handler({})
        assert handler._get_product_state(make_device(), "state", default="x") == "x"

    @pytest.mark.parametrize(
        "node",
        [
            pytest.param("SCHEDULE", id="string"),
            pytest.param(None, id="none"),
            pytest.param(7, id="int"),
            pytest.param(["SCHEDULE"], id="list"),
        ],
    )
    def test_node_without_string_keys_returns_the_default(self, node):
        handler = _make_handler({"prod-1": {"state": node}})
        result = handler._get_product_state(make_device(), "state", "mode", default="x")
        assert result == "x"


class TestAsFloat:
    @pytest.mark.parametrize(
        ("value", "expected"), [(20, 20.0), (19.5, 19.5), ("21", 21.0), ("18.5", 18.5)]
    )
    def test_numeric_values_become_floats(self, value, expected):
        result = DeviceHandler._as_float(value)
        assert result == expected
        assert isinstance(result, float)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(None, id="none"),
            pytest.param("abc", id="text"),
            pytest.param("", id="empty-string"),
            pytest.param({}, id="dict"),
            pytest.param([], id="list"),
            pytest.param(object(), id="object"),
        ],
    )
    def test_non_numeric_values_are_none(self, value):
        assert DeviceHandler._as_float(value) is None


class TestIsOnline:
    @pytest.mark.parametrize(
        "devices",
        [
            pytest.param(_ONLINE_DEVICES, id="thermostat-online"),
            pytest.param(_OFFLINE_DEVICES, id="thermostat-offline"),
            pytest.param({}, id="no-thermostat"),
        ],
    )
    def test_heating_follows_its_own_online_flag(self, devices):
        handler = _make_handler(_HEATING, devices)
        assert handler._is_online(make_device()) is True

    def test_a_truthy_online_value_counts_as_online(self):
        handler = _make_handler({"prod-1": {"props": {"online": 1}}})
        assert handler._is_online(make_device()) is True

    def test_false_when_the_product_is_unknown(self):
        handler = _make_handler({}, _ONLINE_DEVICES)
        assert handler._is_online(make_device()) is False

    @pytest.mark.parametrize(
        "product",
        [
            pytest.param({}, id="no-props"),
            pytest.param({"props": None}, id="props-none"),
            pytest.param({"props": {}}, id="no-online-flag"),
            pytest.param({"props": {"online": False}}, id="online-false"),
            pytest.param({"props": {"online": None}}, id="online-none"),
            pytest.param({"props": {"online": 0}}, id="online-zero"),
        ],
    )
    def test_false_without_a_truthy_online_flag(self, product):
        handler = _make_handler({"prod-1": product}, _ONLINE_DEVICES)
        assert handler._is_online(make_device()) is False

    def test_trv_control_is_online_while_its_trv_is(self):
        handler = _make_handler(_TRV_CONTROL, _ONLINE_DEVICES)
        assert handler._is_online(make_device(hive_type="trvcontrol")) is True

    @pytest.mark.parametrize(
        "devices",
        [
            pytest.param(_OFFLINE_DEVICES, id="trv-offline"),
            pytest.param({}, id="no-trv"),
            pytest.param({"dev-1": {"props": None}}, id="trv-props-none"),
        ],
    )
    def test_trv_control_is_offline_without_an_online_trv(self, devices):
        handler = _make_handler(_TRV_CONTROL, devices)
        assert handler._is_online(make_device(hive_type="trvcontrol")) is False

    def test_offline_trv_control_is_offline_with_an_online_trv(self):
        handler = _make_handler(
            {"prod-1": {"type": "trvcontrol", "props": {"online": False}}},
            _ONLINE_DEVICES,
        )
        assert handler._is_online(make_device(hive_type="trvcontrol")) is False


class TestRefreshDevice:
    def test_online_device_is_updated_and_recovered(self):
        handler = _make_handler(_HEATING, _ONLINE_DEVICES)
        device = make_device()
        assert handler._refresh_device(device) is True
        assert device.device_data == {"online": True, "model": "SLT3", "battery": 80}
        handler.session.device_recovered.assert_called_once_with("prod-1")
        handler.session.report_device_offline.assert_not_called()

    def test_device_data_is_a_new_dict_and_the_props_are_left_alone(self):
        props = {"online": True, "model": "SLT3"}
        handler = _make_handler(_HEATING, {"dev-1": {"props": props}})
        device = make_device()
        handler._refresh_device(device)
        assert device.device_data is not props
        device.device_data["battery"] = 10
        assert props == {"online": True, "model": "SLT3"}

    @pytest.mark.parametrize(
        "products",
        [
            pytest.param(_OFFLINE_HEATING, id="product-offline"),
            pytest.param({}, id="unknown-product"),
        ],
    )
    def test_offline_product_is_updated_and_reported(self, products):
        handler = _make_handler(products, _ONLINE_DEVICES)
        device = make_device()
        assert handler._refresh_device(device) is False
        assert device.device_data == {"online": False, "model": "SLT3", "battery": 80}
        handler.session.report_device_offline.assert_called_once_with("prod-1")
        handler.session.device_recovered.assert_not_called()

    @pytest.mark.parametrize(
        "devices",
        [
            pytest.param({}, id="unknown-device"),
            pytest.param({"dev-1": {}}, id="no-props"),
            pytest.param({"dev-1": {"props": None}}, id="props-none"),
        ],
    )
    def test_device_without_props_gives_the_online_flag_only(self, devices):
        handler = _make_handler(_HEATING, devices)
        device = make_device()
        assert handler._refresh_device(device) is True
        assert device.device_data == {"online": True}

    def test_parent_device_is_left_alone(self):
        handler = _make_handler(
            _HEATING, {"dev-1": {"props": {"online": True}, "parent": "other"}}
        )
        device = make_device(parent_device="hub-1")
        handler._refresh_device(device)
        assert device.parent_device == "hub-1"

    def test_device_data_none_is_replaced(self):
        handler = _make_handler(_HEATING, _ONLINE_DEVICES)
        device = make_device(device_data=None)
        handler._refresh_device(device)
        assert device.device_data == {"online": True, "model": "SLT3", "battery": 80}

    def test_recovery_is_reported_after_going_offline(self):
        products = {"prod-1": {"type": "heating", "props": {"online": False}}}
        handler = _make_handler(products, {})
        device = make_device()
        assert handler._refresh_device(device) is False
        products["prod-1"]["props"]["online"] = True
        assert handler._refresh_device(device) is True
        assert device.device_data == {"online": True}
        handler.session.report_device_offline.assert_called_once_with("prod-1")
        handler.session.device_recovered.assert_called_once_with("prod-1")


class TestBoostStatus:
    @pytest.mark.parametrize("boost", [1, 30, 120])
    def test_positive_minutes_are_on(self, boost):
        handler = _make_handler({"prod-1": {"state": {"boost": boost}}})
        assert handler._boost_status(make_device()) == "ON"

    @pytest.mark.parametrize("boost", [0, False, None])
    def test_falsy_boost_is_off(self, boost):
        handler = _make_handler({"prod-1": {"state": {"boost": boost}}})
        assert handler._boost_status(make_device()) == "OFF"

    def test_missing_boost_key_is_off(self):
        handler = _make_handler({"prod-1": {"state": {"mode": "SCHEDULE"}}})
        assert handler._boost_status(make_device()) == "OFF"

    def test_missing_state_is_unknown(self):
        handler = _make_handler({"prod-1": {"props": {"online": True}}})
        assert handler._boost_status(make_device()) is None

    def test_state_that_is_not_a_dict_is_unknown(self):
        handler = _make_handler({"prod-1": {"state": "BOOST"}})
        assert handler._boost_status(make_device()) is None

    def test_unknown_product_is_unknown(self):
        handler = _make_handler({})
        assert handler._boost_status(make_device()) is None


class TestSetState:
    async def test_unknown_product_raises_offline_without_sending(self):
        handler = _make_handler({}, _ONLINE_DEVICES)
        device = make_device(ha_name="Hallway")
        with pytest.raises(HiveDeviceOffline) as excinfo:
            await handler._set_state(device, mode="MANUAL")
        assert "Hallway" in str(excinfo.value)
        handler.session.set_state.assert_not_awaited()
        handler.session.get_devices.assert_not_awaited()

    async def test_offline_product_raises_offline_without_sending(self):
        handler = _make_handler(_OFFLINE_HEATING, _ONLINE_DEVICES)
        device = make_device(ha_name="Hallway")
        with pytest.raises(HiveDeviceOffline, match="Hallway is offline"):
            await handler._set_state(device, mode="MANUAL")
        handler.session.set_state.assert_not_awaited()
        handler.session.get_devices.assert_not_awaited()

    async def test_offline_trv_raises_offline(self):
        handler = _make_handler(_TRV_CONTROL, _OFFLINE_DEVICES)
        with pytest.raises(HiveDeviceOffline):
            await handler._set_state(make_device(hive_type="trvcontrol"), mode="MANUAL")
        handler.session.set_state.assert_not_awaited()

    async def test_online_device_sends_the_state_then_polls(self):
        handler = _make_handler(_HEATING, _ONLINE_DEVICES)
        calls = MagicMock()
        calls.attach_mock(handler.session.set_state, "set_state")
        calls.attach_mock(handler.session.get_devices, "get_devices")
        result = await handler._set_state(
            make_device(), mode="BOOST", boost=30, target=20.0
        )
        assert result is None
        assert calls.mock_calls == [
            call.set_state("heating", "prod-1", mode="BOOST", boost=30, target=20.0),
            call.get_devices(),
        ]
        handler.session.set_state.assert_awaited_once()
        handler.session.get_devices.assert_awaited_once()

    @pytest.mark.parametrize("product_type", ["heating", "hotwater", "trvcontrol"])
    async def test_sends_the_product_type(self, product_type):
        handler = _make_handler(
            {"prod-1": {"type": product_type, "props": {"online": True}}},
            _ONLINE_DEVICES,
        )
        await handler._set_state(make_device(hive_type=product_type), mode="MANUAL")
        handler.session.set_state.assert_awaited_once_with(
            product_type, "prod-1", mode="MANUAL"
        )

    async def test_error_from_set_state_propagates_without_polling(self):
        handler = _make_handler(_HEATING, _ONLINE_DEVICES)
        handler.session.set_state.side_effect = HiveApiError("refused")
        with pytest.raises(HiveApiError, match="refused"):
            await handler._set_state(make_device(), mode="MANUAL")
        handler.session.get_devices.assert_not_awaited()

    async def test_error_from_the_poll_propagates(self):
        handler = _make_handler(_HEATING, _ONLINE_DEVICES)
        handler.session.get_devices.side_effect = HiveReauthRequired("log in again")
        with pytest.raises(HiveReauthRequired):
            await handler._set_state(make_device(), mode="MANUAL")
        handler.session.set_state.assert_awaited_once()

    async def test_logs_the_change_at_debug_level(self, caplog):
        handler = _make_handler(_HEATING, _ONLINE_DEVICES)
        caplog.set_level(logging.DEBUG, logger="apyhiveapi.device_handler")
        await handler._set_state(make_device(ha_name="Hallway"), mode="MANUAL")
        assert "Setting Hallway: {'mode': 'MANUAL'}" in caplog.text
