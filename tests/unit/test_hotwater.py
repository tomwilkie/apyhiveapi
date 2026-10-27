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

    @pytest.mark.parametrize(
        "products",
        [
            pytest.param(_hotwater(state={"mode": None}), id="mode-none"),
            pytest.param(_hotwater(state={"status": "OFF"}), id="missing-mode"),
            pytest.param(_hotwater(), id="missing-state"),
            pytest.param({}, id="unknown-product"),
        ],
    )
    async def test_missing_mode_is_none(self, products):
        heater = _make_hotwater(products)
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


class TestSetBoostOn:
    @pytest.mark.parametrize(("mins", "boost"), [(60, 60), ("30", 30), (45.9, 45)])
    async def test_sends_boost_minutes_without_a_target_then_polls(self, mins, boost):
        heater = _make_hotwater(_hotwater())
        assert await heater.set_boost_on(_device(), mins) is None
        heater.session.set_state.assert_awaited_once_with(
            "hotwater", "hw-1", mode="BOOST", boost=boost
        )
        assert isinstance(heater.session.set_state.await_args.kwargs["boost"], int)
        heater.session.get_devices.assert_awaited_once_with()

    @pytest.mark.parametrize("mins", [0, -1, "0", 0.9])
    async def test_non_positive_minutes_raise(self, mins):
        heater = _make_hotwater(_hotwater())
        with pytest.raises(ValueError, match="Boost minutes must be positive"):
            await heater.set_boost_on(_device(), mins)
        heater.session.set_state.assert_not_awaited()
        heater.session.get_devices.assert_not_awaited()


class TestSetBoostOff:
    # Unlike every other command, set_boost_off on an unknown product is a
    # no-op rather than HiveDeviceOffline: there is no boost to end.
    @pytest.mark.parametrize(
        "products",
        [
            pytest.param(
                _hotwater(
                    state={"boost": None}, props={"previous": {"mode": "MANUAL"}}
                ),
                id="boost-none",
            ),
            pytest.param(
                _hotwater(
                    state={"boost": False}, props={"previous": {"mode": "MANUAL"}}
                ),
                id="boost-false",
            ),
            pytest.param(
                _hotwater(state={"boost": 0}, props={"previous": {"mode": "MANUAL"}}),
                id="boost-zero",
            ),
            pytest.param(_hotwater(state={"mode": "SCHEDULE"}), id="no-boost-key"),
            pytest.param({}, id="unknown-product"),
        ],
    )
    async def test_not_boosting_does_nothing(self, products, caplog):
        heater = _make_hotwater(products)
        caplog.set_level(logging.DEBUG, logger="apyhiveapi.hotwater")
        assert await heater.set_boost_off(_device()) is None
        heater.session.set_state.assert_not_awaited()
        heater.session.get_devices.assert_not_awaited()
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


class TestCommands:
    @pytest.mark.parametrize(
        ("products", "command", "args"),
        [
            pytest.param(
                _hotwater(
                    state=_BOOSTING,
                    props={"previous": {"mode": "SCHEDULE"}},
                    online=False,
                ),
                "set_mode",
                ("MANUAL",),
                id="set_mode",
            ),
            pytest.param(
                _hotwater(
                    state=_BOOSTING,
                    props={"previous": {"mode": "SCHEDULE"}},
                    online=False,
                ),
                "set_boost_on",
                (60,),
                id="set_boost_on",
            ),
            pytest.param(
                _hotwater(
                    state=_BOOSTING,
                    props={"previous": {"mode": "SCHEDULE"}},
                    online=False,
                ),
                "set_boost_off",
                (),
                id="set_boost_off",
            ),
            pytest.param({}, "set_mode", ("MANUAL",), id="unknown-product"),
        ],
    )
    async def test_offline_or_unknown_product_raises_before_sending(
        self, products, command, args
    ):
        heater = _make_hotwater(products)
        with pytest.raises(HiveDeviceOffline, match="Hot Water is offline"):
            await getattr(heater, command)(_device(), *args)
        heater.session.set_state.assert_not_awaited()
        heater.session.get_devices.assert_not_awaited()


class TestGetWaterHeater:
    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            pytest.param(
                {"mode": "SCHEDULE"},
                {"current_operation": "SCHEDULE", "boost": "OFF"},
                id="scheduled",
            ),
            pytest.param(
                {**_BOOSTING},
                {"current_operation": None, "boost": "ON"},
                id="boosting",
            ),
        ],
    )
    async def test_online_device_gets_its_status(self, state, expected):
        heater = _make_hotwater(_hotwater(state=state))
        device = _device()
        result = await heater.get_water_heater(device)
        assert result is device
        assert device.status == expected
        assert device.device_data == {"online": True, "model": "SLT"}
        heater.session.device_recovered.assert_called_once_with("hw-1")
        heater.session.report_device_offline.assert_not_called()

    async def test_sparse_product_reports_unknown_while_online(self):
        heater = _make_hotwater(_hotwater())
        device = await heater.get_water_heater(_device())
        assert device.status == {"current_operation": None, "boost": None}
        assert device.device_data["online"] is True

    @pytest.mark.parametrize(
        "products",
        [
            pytest.param(
                _hotwater(state={"mode": "SCHEDULE"}, online=False),
                id="offline-product",
            ),
            pytest.param({}, id="unknown-product"),
        ],
    )
    async def test_offline_device_gets_the_offline_status(self, products):
        heater = _make_hotwater(products)
        device = _device(status={"current_operation": "SCHEDULE"})
        result = await heater.get_water_heater(device)
        assert result is device
        assert device.status == {"current_operation": None, "boost": None}
        assert device.device_data == {"online": False, "model": "SLT"}
        heater.session.report_device_offline.assert_called_once_with("hw-1")
        heater.session.device_recovered.assert_not_called()
