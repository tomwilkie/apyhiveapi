"""Unit tests for the Climate handler (hive.heating)."""

import logging

import pytest

from apyhiveapi.exceptions import HiveApiError, HiveDeviceOffline
from apyhiveapi.heating import MAX_TARGET_TEMPERATURE, MIN_TARGET_TEMPERATURE, Climate
from tests.conftest import make_device, make_session

_THERMOSTAT = {"dev-1": {"props": {"online": True, "model": "SLT"}}}
_OFFLINE_STATUS = {
    "current_temperature": None,
    "target_temperature": None,
    "action": None,
    "mode": None,
    "boost": None,
}
_BOOSTING = {"mode": "BOOST", "target": 22, "boost": 30}


def _make_climate(products=None):
    """Return a Climate bound to a mock session with the zone's thermostat."""
    return Climate(make_session(products=products, devices=_THERMOSTAT))


def _heating(online=True, **product):
    """Return the products dict for one heating product with the given fields."""
    props = {"online": online, **product.pop("props", {})}
    return {"heat-1": {"type": "heating", **product, "props": props}}


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

    @pytest.mark.parametrize(
        "products",
        [
            pytest.param(_heating(props={"temperature": "abc"}), id="non-numeric"),
            pytest.param(_heating(props={"working": False}), id="missing-temperature"),
            pytest.param(_heating(), id="missing-props"),
            pytest.param({}, id="unknown-product"),
        ],
    )
    async def test_missing_or_invalid_temperature_is_none(self, products):
        climate = _make_climate(products)
        assert await climate.get_current_temperature(_device()) is None


class TestGetTargetTemperature:
    async def test_reads_the_target_as_a_float(self):
        climate = _make_climate(_heating(state={"target": 21, "heat": 18.5}))
        result = await climate.get_target_temperature(_device())
        assert result == 21.0
        assert isinstance(result, float)

    @pytest.mark.parametrize(
        "state",
        [
            pytest.param({"heat": 18.5}, id="no-target"),
            pytest.param({"target": None, "heat": 18.5}, id="target-none"),
        ],
    )
    async def test_falls_back_to_heat(self, state):
        climate = _make_climate(_heating(state=state))
        assert await climate.get_target_temperature(_device()) == 18.5

    @pytest.mark.parametrize(
        "products",
        [
            pytest.param(_heating(state={"mode": "SCHEDULE"}), id="neither-key"),
            pytest.param({}, id="unknown-product"),
        ],
    )
    async def test_missing_target_is_none(self, products):
        climate = _make_climate(products)
        assert await climate.get_target_temperature(_device()) is None

    async def test_non_numeric_target_is_none(self):
        climate = _make_climate(_heating(state={"target": "abc", "heat": 18.5}))
        assert await climate.get_target_temperature(_device()) is None


class TestGetMode:
    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            ("SCHEDULE", "SCHEDULE"),
            ("MANUAL", "MANUAL"),
            ("OFF", "OFF"),
            (False, "OFF"),
        ],
    )
    async def test_plain_modes_are_returned_as_they_are(self, mode, expected):
        climate = _make_climate(_heating(state={"mode": mode}))
        assert await climate.get_mode(_device()) == expected

    @pytest.mark.parametrize(
        ("previous", "expected"),
        [
            ("SCHEDULE", "SCHEDULE"),
            ("MANUAL", "MANUAL"),
            ("OFF", "OFF"),
            (False, "OFF"),
        ],
    )
    async def test_boost_reports_the_previous_mode(self, previous, expected):
        climate = _make_climate(
            _heating(state={"mode": "BOOST"}, props={"previous": {"mode": previous}})
        )
        assert await climate.get_mode(_device()) == expected

    @pytest.mark.parametrize(
        "props",
        [
            pytest.param({}, id="no-previous"),
            pytest.param({"previous": {}}, id="previous-without-mode"),
            pytest.param({"previous": {"mode": None}}, id="previous-mode-none"),
        ],
    )
    async def test_boost_without_a_previous_mode_is_none(self, props):
        climate = _make_climate(_heating(state={"mode": "BOOST"}, props=props))
        assert await climate.get_mode(_device()) is None

    @pytest.mark.parametrize(
        "products",
        [
            pytest.param(_heating(state={"target": 7}), id="missing-mode"),
            pytest.param({}, id="unknown-product"),
        ],
    )
    async def test_missing_mode_is_none(self, products):
        climate = _make_climate(products)
        assert await climate.get_mode(_device()) is None


class TestGetCurrentOperation:
    @pytest.mark.parametrize(
        ("props", "expected"),
        [({"working": True}, True), ({"working": False}, False), ({}, None)],
    )
    async def test_reports_whether_the_zone_is_calling_for_heat(self, props, expected):
        climate = _make_climate(_heating(props=props))
        assert await climate.get_current_operation(_device()) is expected


class TestSetTargetTemperature:
    async def test_sends_the_target_then_polls(self):
        climate = _make_climate(_heating())
        assert await climate.set_target_temperature(_device(), 21.0) is None
        climate.session.set_state.assert_awaited_once_with(
            "heating", "heat-1", target=21.0
        )
        climate.session.get_devices.assert_awaited_once_with()


class TestSetMode:
    @pytest.mark.parametrize("mode", ["SCHEDULE", "MANUAL", "OFF"])
    async def test_sends_the_mode_then_polls(self, mode):
        climate = _make_climate(_heating())
        assert await climate.set_mode(_device(), mode) is None
        climate.session.set_state.assert_awaited_once_with(
            "heating", "heat-1", mode=mode
        )
        climate.session.get_devices.assert_awaited_once_with()


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

    @pytest.mark.parametrize(
        ("mins", "temp", "boost", "target"),
        [
            pytest.param("30", "21.5", 30, 21.5, id="numeric-strings"),
            pytest.param(45.9, 20, 45, 20.0, id="fractional-minutes"),
            pytest.param(30, MIN_TARGET_TEMPERATURE, 30, 5.0, id="min-temperature"),
            pytest.param(30, MAX_TARGET_TEMPERATURE, 30, 32.0, id="max-temperature"),
            pytest.param(30, "5", 30, 5.0, id="min-temperature-string"),
        ],
    )
    async def test_minutes_and_temperature_are_normalised(
        self, mins, temp, boost, target
    ):
        climate = _make_climate(_heating())
        await climate.set_boost_on(_device(), mins, temp)
        kwargs = climate.session.set_state.await_args.kwargs
        assert kwargs == {"mode": "BOOST", "boost": boost, "target": target}
        assert isinstance(kwargs["boost"], int)
        assert isinstance(kwargs["target"], float)

    @pytest.mark.parametrize("temp", [4.9, 32.1, "40"])
    async def test_out_of_range_temperature_raises(self, temp):
        climate = _make_climate(_heating())
        with pytest.raises(ValueError, match="Boost temperature must be between"):
            await climate.set_boost_on(_device(), 30, temp)
        climate.session.set_state.assert_not_awaited()
        climate.session.get_devices.assert_not_awaited()

    @pytest.mark.parametrize("mins", [0, -1, "0", 0.5, "-10"])
    async def test_non_positive_minutes_raise_before_the_device_is_checked(self, mins):
        climate = _make_climate(_heating(online=False))
        with pytest.raises(ValueError, match="Boost minutes must be positive"):
            await climate.set_boost_on(_device(), mins, 20)
        climate.session.set_state.assert_not_awaited()

    @pytest.mark.parametrize(
        ("mins", "temp", "match"),
        [
            ("abc", 20, "invalid literal"),
            ("1e2", 20, "invalid literal"),
            (30, "hot", "could not convert"),
        ],
    )
    async def test_non_numeric_input_raises_value_error(self, mins, temp, match):
        climate = _make_climate(_heating())
        with pytest.raises(ValueError, match=match):
            await climate.set_boost_on(_device(), mins, temp)
        climate.session.set_state.assert_not_awaited()

    @pytest.mark.parametrize(
        ("mins", "temp"),
        [
            pytest.param(None, 20, id="minutes-none"),
            pytest.param(30, None, id="temperature-none"),
            pytest.param([], 20, id="minutes-list"),
        ],
    )
    async def test_non_numeric_types_raise_type_error(self, mins, temp):
        climate = _make_climate(_heating())
        with pytest.raises(TypeError):
            await climate.set_boost_on(_device(), mins, temp)
        climate.session.set_state.assert_not_awaited()


class TestSetBoostOff:
    # Unlike every other command, set_boost_off on an unknown product is a
    # no-op rather than HiveDeviceOffline: there is no boost to end.
    @pytest.mark.parametrize(
        "products",
        [
            pytest.param(
                _heating(state={"boost": None}, props={"previous": {"mode": "MANUAL"}}),
                id="boost-none",
            ),
            pytest.param(
                _heating(
                    state={"boost": False}, props={"previous": {"mode": "MANUAL"}}
                ),
                id="boost-false",
            ),
            pytest.param(
                _heating(state={"boost": 0}, props={"previous": {"mode": "MANUAL"}}),
                id="boost-zero",
            ),
            pytest.param(_heating(state={"mode": "SCHEDULE"}), id="no-boost-key"),
            pytest.param({}, id="unknown-product"),
        ],
    )
    async def test_not_boosting_does_nothing(self, products, caplog):
        climate = _make_climate(products)
        caplog.set_level(logging.DEBUG, logger="apyhiveapi.heating")
        assert await climate.set_boost_off(_device()) is None
        climate.session.set_state.assert_not_awaited()
        climate.session.get_devices.assert_not_awaited()
        assert "Hallway is not boosting" in caplog.text

    # Hive reports a previous mode of "off" either as "OFF" or as false.
    @pytest.mark.parametrize(
        ("previous", "sent"),
        [
            pytest.param(
                {"mode": "MANUAL", "target": 19.5},
                {"mode": "MANUAL", "target": 19.5},
                id="manual",
            ),
            pytest.param(
                {"mode": "OFF", "target": 7}, {"mode": "OFF", "target": 7}, id="off"
            ),
            pytest.param(
                {"mode": False, "target": 7},
                {"mode": "OFF", "target": 7},
                id="off-false",
            ),
            pytest.param(
                {"mode": "OFF"}, {"mode": "OFF", "target": 7}, id="off-without-target"
            ),
            pytest.param(
                {"mode": "MANUAL", "target": None},
                {"mode": "MANUAL", "target": 7},
                id="manual-target-none",
            ),
            pytest.param(
                {"mode": "SCHEDULE", "target": 19.5},
                {"mode": "SCHEDULE"},
                id="schedule",
            ),
        ],
    )
    async def test_restores_the_previous_mode_and_target(self, previous, sent):
        climate = _make_climate(_heating(state=_BOOSTING, props={"previous": previous}))
        assert await climate.set_boost_off(_device()) is None
        climate.session.set_state.assert_awaited_once_with("heating", "heat-1", **sent)
        climate.session.get_devices.assert_awaited_once_with()

    @pytest.mark.parametrize(
        "props",
        [
            pytest.param({}, id="no-previous"),
            pytest.param({"previous": {}}, id="previous-without-mode"),
            pytest.param(
                {"previous": {"mode": None, "target": 20}}, id="previous-mode-none"
            ),
        ],
    )
    async def test_missing_previous_mode_raises_api_error(self, props):
        climate = _make_climate(_heating(state=_BOOSTING, props=props))
        with pytest.raises(HiveApiError, match="Hallway"):
            await climate.set_boost_off(_device())
        climate.session.set_state.assert_not_awaited()
        climate.session.get_devices.assert_not_awaited()


class TestCommands:
    @pytest.mark.parametrize(
        ("products", "command", "args"),
        [
            pytest.param(
                _heating(
                    state=_BOOSTING,
                    props={"previous": {"mode": "SCHEDULE"}},
                    online=False,
                ),
                "set_target_temperature",
                (21.0,),
                id="set_target_temperature",
            ),
            pytest.param(
                _heating(
                    state=_BOOSTING,
                    props={"previous": {"mode": "SCHEDULE"}},
                    online=False,
                ),
                "set_mode",
                ("MANUAL",),
                id="set_mode",
            ),
            pytest.param(
                _heating(
                    state=_BOOSTING,
                    props={"previous": {"mode": "SCHEDULE"}},
                    online=False,
                ),
                "set_boost_on",
                (30, 20),
                id="set_boost_on",
            ),
            pytest.param(
                _heating(
                    state=_BOOSTING,
                    props={"previous": {"mode": "SCHEDULE"}},
                    online=False,
                ),
                "set_boost_off",
                (),
                id="set_boost_off",
            ),
            pytest.param({}, "set_target_temperature", (21.0,), id="unknown-product"),
        ],
    )
    async def test_offline_or_unknown_product_raises_before_sending(
        self, products, command, args
    ):
        climate = _make_climate(products)
        with pytest.raises(HiveDeviceOffline, match="Hallway is offline"):
            await getattr(climate, command)(_device(), *args)
        climate.session.set_state.assert_not_awaited()
        climate.session.get_devices.assert_not_awaited()


class TestGetClimate:
    async def test_online_device_gets_the_full_status(self):
        climate = _make_climate(
            _heating(
                state={"mode": "MANUAL", "target": 21, "boost": None},
                props={"temperature": 20.25, "working": True},
            )
        )
        device = _device()
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
        climate.session.device_recovered.assert_called_once_with("heat-1")
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
        climate.session.device_recovered.assert_called_once_with("heat-1")

    @pytest.mark.parametrize(
        "products",
        [
            pytest.param(
                _heating(
                    state={"mode": "MANUAL", "target": 21},
                    props={"temperature": 20},
                    online=False,
                ),
                id="offline-product",
            ),
            pytest.param({}, id="unknown-product"),
        ],
    )
    async def test_offline_device_gets_the_offline_status(self, products):
        climate = _make_climate(products)
        device = _device(status={"mode": "MANUAL", "boost": "OFF"})
        result = await climate.get_climate(device)
        assert result is device
        assert device.status == _OFFLINE_STATUS
        assert device.device_data == {"online": False, "model": "SLT"}
        assert device.min_temp is None
        assert device.max_temp is None
        climate.session.report_device_offline.assert_called_once_with("heat-1")
        climate.session.device_recovered.assert_not_called()

    async def test_offline_statuses_are_independent_dicts(self):
        climate = _make_climate(_heating(online=False))
        first = await climate.get_climate(_device())
        second = await climate.get_climate(_device())
        assert first.status is not second.status
        first.status["mode"] = "MANUAL"
        third = await climate.get_climate(_device())
        assert second.status == _OFFLINE_STATUS
        assert third.status == _OFFLINE_STATUS
