"""Unit tests for the DeviceHandler plumbing shared by Climate and WaterHeater."""

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

    @pytest.mark.parametrize(
        ("products", "keys", "default"),
        [
            pytest.param({"prod-1": {}}, ("state",), None, id="first-key-missing"),
            pytest.param(
                {"prod-1": {"state": {}}},
                ("state", "mode"),
                None,
                id="nested-key-missing",
            ),
            pytest.param(
                {"prod-1": {"state": {}}},
                ("state", "boost"),
                0,
                id="nested-key-missing-explicit-default",
            ),
            pytest.param({}, ("state",), None, id="unknown-product"),
            pytest.param({}, ("state",), "x", id="unknown-product-explicit-default"),
        ],
    )
    def test_missing_key_or_product_returns_the_default(self, products, keys, default):
        handler = _make_handler(products)
        assert (
            handler._get_product_state(make_device(), *keys, default=default) is default
        )

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

    def test_false_when_the_product_is_unknown(self):
        handler = _make_handler({}, _ONLINE_DEVICES)
        assert handler._is_online(make_device()) is False

    @pytest.mark.parametrize(
        "product",
        [
            pytest.param({}, id="no-props"),
            pytest.param({"props": None}, id="props-none"),
            pytest.param({"props": {"online": False}}, id="online-false"),
            pytest.param(
                {"type": "trvcontrol", "props": {"online": False}},
                id="offline-trv-control-with-online-trv",
            ),
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


class TestRefreshDevice:
    def test_online_device_is_updated_and_recovered(self):
        handler = _make_handler(_HEATING, _ONLINE_DEVICES)
        device = make_device()
        assert handler._refresh_device(device) is True
        assert device.device_data == {
            "online": True,
            "device_online": True,
            "model": "SLT3",
            "battery": 80,
        }
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
        assert device.device_data == {
            "online": False,
            "device_online": True,
            "model": "SLT3",
            "battery": 80,
        }
        handler.session.report_device_offline.assert_called_once_with("prod-1")
        handler.session.device_recovered.assert_not_called()

    @pytest.mark.parametrize(
        "devices",
        [
            pytest.param(_OFFLINE_DEVICES, id="thermostat-offline"),
            pytest.param({"dev-1": {"props": {"model": "SLT3"}}}, id="no-online-prop"),
        ],
    )
    def test_device_online_is_the_hardware_flag_not_the_product(self, devices):
        handler = _make_handler(_HEATING, devices)
        device = make_device()
        assert handler._refresh_device(device) is True
        assert device.device_data["online"] is True
        assert device.device_data["device_online"] is False

    @pytest.mark.parametrize(
        ("devices", "expected"),
        [
            pytest.param(_ONLINE_DEVICES, True, id="trv-online"),
            pytest.param(_OFFLINE_DEVICES, False, id="trv-offline"),
        ],
    )
    def test_trv_control_flags_agree_with_its_trv(self, devices, expected):
        handler = _make_handler(_TRV_CONTROL, devices)
        device = make_device(hive_type="trvcontrol")
        assert handler._refresh_device(device) is expected
        assert device.device_data["online"] is expected
        assert device.device_data["device_online"] is expected

    @pytest.mark.parametrize("online", [True, False], ids=["online", "offline"])
    def test_product_without_a_thermostat_is_its_own_device(self, online):
        products = {
            "prod-1": {"type": "heating", "props": {"online": online, "model": "SLR2"}}
        }
        handler = _make_handler(products, {})
        device = make_device(device_id="prod-1")
        assert handler._refresh_device(device) is online
        assert device.device_data == {
            "online": online,
            "device_online": online,
            "model": "SLR2",
        }

    @pytest.mark.parametrize(
        "devices",
        [
            pytest.param({}, id="unknown-device"),
            pytest.param({"dev-1": {}}, id="no-props"),
            pytest.param({"dev-1": {"props": None}}, id="props-none"),
        ],
    )
    def test_device_without_props_gives_the_flags_only(self, devices):
        handler = _make_handler(_HEATING, devices)
        device = make_device()
        assert handler._refresh_device(device) is True
        assert device.device_data == {"online": True, "device_online": False}

    def test_recovery_is_reported_after_going_offline(self):
        products = {"prod-1": {"type": "heating", "props": {"online": False}}}
        handler = _make_handler(products, {})
        device = make_device()
        assert handler._refresh_device(device) is False
        products["prod-1"]["props"]["online"] = True
        assert handler._refresh_device(device) is True
        assert device.device_data == {"online": True, "device_online": False}
        handler.session.report_device_offline.assert_called_once_with("prod-1")
        handler.session.device_recovered.assert_called_once_with("prod-1")


class TestBoostStatus:
    @pytest.mark.parametrize("boost", [1, 30, 120])
    def test_positive_minutes_are_on(self, boost):
        handler = _make_handler({"prod-1": {"state": {"boost": boost}}})
        assert handler._boost_status(make_device()) == "ON"

    @pytest.mark.parametrize(
        "state",
        [
            pytest.param({"boost": 0}, id="boost-zero"),
            pytest.param({"boost": False}, id="boost-false"),
            pytest.param({"boost": None}, id="boost-none"),
            pytest.param({"mode": "SCHEDULE"}, id="no-boost-key"),
        ],
    )
    def test_falsy_boost_is_off(self, state):
        handler = _make_handler({"prod-1": {"state": state}})
        assert handler._boost_status(make_device()) == "OFF"

    @pytest.mark.parametrize(
        "products",
        [
            pytest.param({"prod-1": {"props": {"online": True}}}, id="no-state"),
            pytest.param({"prod-1": {"state": "BOOST"}}, id="state-not-a-dict"),
            pytest.param({}, id="unknown-product"),
        ],
    )
    def test_unknown_without_a_state_dict(self, products):
        handler = _make_handler(products)
        assert handler._boost_status(make_device()) is None


class TestSetState:
    @pytest.mark.parametrize(
        ("products", "devices", "hive_type"),
        [
            pytest.param({}, _ONLINE_DEVICES, "heating", id="unknown-product"),
            pytest.param(
                _OFFLINE_HEATING, _ONLINE_DEVICES, "heating", id="offline-product"
            ),
            pytest.param(
                _TRV_CONTROL, _OFFLINE_DEVICES, "trvcontrol", id="offline-trv"
            ),
        ],
    )
    async def test_offline_or_unknown_product_raises_without_sending(
        self, products, devices, hive_type
    ):
        handler = _make_handler(products, devices)
        device = make_device(hive_type=hive_type, ha_name="Hallway")
        with pytest.raises(HiveDeviceOffline, match="Hallway is offline"):
            await handler._set_state(device, mode="MANUAL")
        handler.session.set_state.assert_not_awaited()
        handler.session.get_devices.assert_not_awaited()

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
