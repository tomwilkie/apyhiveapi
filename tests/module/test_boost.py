"""Module-level tests of boosting heating and hot water against the file-mode session.

The bundled fixture is read-only: ``Hive.set_state`` sends nothing in file
mode and every command re-reads the fixture, so a boost reported by Hive is
imitated by editing the session data and the command sent is observed by
replacing ``set_state`` with an ``AsyncMock``.
"""

import logging
from unittest.mock import AsyncMock

import pytest

from apyhiveapi.exceptions import HiveApiError, HiveDeviceOffline
from tests.conftest import make_device

_HEATING_ID = "heating-0000-0000-0000-000000000001"
_HOTWATER_ID = "hotwater-0000-0000-0000-000000000001"
_TRV_CONTROL_ID = "trvcontrol-0000-0000-0000-000000000001"
_THERMOSTAT_ID = "thermostatui-0000-0000-0000-000000000001"


def _find(session, entity_type, hive_id):
    """Return the device for *hive_id* from the session's device list."""
    return next(
        device
        for device in session.device_list[entity_type]
        if device.hive_id == hive_id
    )


def _start_boost(session, hive_id, minutes=30):
    """Make the fixture product look boosted, the way Hive reports it."""
    state = session.data.products[hive_id]["state"]
    state["boost"] = minutes
    state["mode"] = "BOOST"


def _capture_commands(session):
    """Replace the file-mode no-op set_state with a mock that records the call."""
    session.set_state = AsyncMock()
    return session.set_state


class TestBoostStatus:
    @pytest.mark.parametrize("hive_id", [_HEATING_ID, _TRV_CONTROL_ID])
    async def test_fixture_climate_devices_are_not_boosting(
        self, file_session, hive_id
    ):
        device = _find(file_session, "climate", hive_id)
        assert file_session.heating._boost_status(device) == "OFF"

    async def test_fixture_hot_water_is_not_boosting(self, file_session):
        device = _find(file_session, "water_heater", _HOTWATER_ID)
        assert file_session.hotwater._boost_status(device) == "OFF"

    async def test_boost_minutes_report_on(self, file_session):
        device = _find(file_session, "climate", _HEATING_ID)
        _start_boost(file_session, _HEATING_ID)
        assert file_session.heating._boost_status(device) == "ON"

    async def test_unknown_product_is_unknown(self, file_session):
        device = make_device(hive_id="missing", device_id=_THERMOSTAT_ID)
        assert file_session.heating._boost_status(device) is None


class TestHeatingBoost:
    async def test_boost_on_sends_boost_minutes_and_target(self, file_session):
        device = _find(file_session, "climate", _HEATING_ID)
        set_state = _capture_commands(file_session)
        await file_session.heating.set_boost_on(device, "30", 20)
        set_state.assert_awaited_once_with(
            "heating", _HEATING_ID, mode="BOOST", boost=30, target=20.0
        )

    async def test_trv_control_boost_targets_its_own_product(self, file_session):
        device = _find(file_session, "climate", _TRV_CONTROL_ID)
        set_state = _capture_commands(file_session)
        await file_session.heating.set_boost_on(device, 15, 18)
        set_state.assert_awaited_once_with(
            "trvcontrol", _TRV_CONTROL_ID, mode="BOOST", boost=15, target=18.0
        )

    async def test_boost_on_re_polls_the_fixture(self, file_session):
        device = _find(file_session, "climate", _HEATING_ID)
        before = file_session.config.last_update
        assert await file_session.heating.set_boost_on(device, 30, 20) is None
        assert file_session.config.last_update > before
        updated = await file_session.heating.get_climate(device)
        assert updated.status["boost"] == "OFF"

    @pytest.mark.parametrize(
        ("mins", "temp", "message"),
        [
            (0, 20, "Boost minutes must be positive"),
            (30, 40, "Boost temperature must be between"),
            ("abc", 20, "invalid literal"),
        ],
    )
    async def test_boost_on_rejects_invalid_input(
        self, file_session, mins, temp, message
    ):
        device = _find(file_session, "climate", _HEATING_ID)
        set_state = _capture_commands(file_session)
        with pytest.raises(ValueError, match=message):
            await file_session.heating.set_boost_on(device, mins, temp)
        set_state.assert_not_awaited()

    async def test_boost_on_offline_heating_raises(self, file_session):
        device = _find(file_session, "climate", _HEATING_ID)
        file_session.data.products[_HEATING_ID]["props"]["online"] = False
        set_state = _capture_commands(file_session)
        with pytest.raises(HiveDeviceOffline, match="Heating Zone 1 is offline"):
            await file_session.heating.set_boost_on(device, 30, 20)
        set_state.assert_not_awaited()

    async def test_boost_off_restores_the_previous_mode_with_the_fallback_target(
        self, file_session
    ):
        device = _find(file_session, "climate", _HEATING_ID)
        _start_boost(file_session, _HEATING_ID)
        set_state = _capture_commands(file_session)
        await file_session.heating.set_boost_off(device)
        set_state.assert_awaited_once_with("heating", _HEATING_ID, mode="OFF", target=7)

    async def test_boost_off_restores_a_manual_target(self, file_session):
        device = _find(file_session, "climate", _HEATING_ID)
        _start_boost(file_session, _HEATING_ID)
        file_session.data.products[_HEATING_ID]["props"]["previous"] = {
            "mode": "MANUAL",
            "target": 21,
        }
        set_state = _capture_commands(file_session)
        await file_session.heating.set_boost_off(device)
        set_state.assert_awaited_once_with(
            "heating", _HEATING_ID, mode="MANUAL", target=21
        )

    async def test_boost_round_trip_through_the_status(self, file_session):
        device = _find(file_session, "climate", _HEATING_ID)
        _start_boost(file_session, _HEATING_ID)
        updated = await file_session.heating.get_climate(device)
        assert updated.status["boost"] == "ON"
        assert updated.status["mode"] == "OFF"
        await file_session.heating.set_boost_off(device)
        updated = await file_session.heating.get_climate(device)
        assert updated.status["boost"] == "OFF"
        assert updated.status["mode"] == "OFF"

    async def test_boost_off_when_not_boosting_is_a_no_op(self, file_session, caplog):
        device = _find(file_session, "climate", _HEATING_ID)
        set_state = _capture_commands(file_session)
        caplog.set_level(logging.DEBUG, logger="apyhiveapi.heating")
        await file_session.heating.set_boost_off(device)
        set_state.assert_not_awaited()
        assert "Heating Zone 1 is not boosting" in caplog.text

    async def test_boost_off_without_a_previous_mode_raises(self, file_session):
        device = _find(file_session, "climate", _TRV_CONTROL_ID)
        _start_boost(file_session, _TRV_CONTROL_ID)
        set_state = _capture_commands(file_session)
        with pytest.raises(HiveApiError, match="TRV Zone 1"):
            await file_session.heating.set_boost_off(device)
        set_state.assert_not_awaited()

    async def test_boost_off_on_offline_heating_raises(self, file_session):
        device = _find(file_session, "climate", _HEATING_ID)
        _start_boost(file_session, _HEATING_ID)
        file_session.data.products[_HEATING_ID]["props"]["online"] = False
        set_state = _capture_commands(file_session)
        with pytest.raises(HiveDeviceOffline):
            await file_session.heating.set_boost_off(device)
        set_state.assert_not_awaited()


class TestHotWaterBoost:
    async def test_boost_on_sends_boost_minutes_only(self, file_session):
        device = _find(file_session, "water_heater", _HOTWATER_ID)
        set_state = _capture_commands(file_session)
        await file_session.hotwater.set_boost_on(device, "60")
        set_state.assert_awaited_once_with(
            "hotwater", _HOTWATER_ID, mode="BOOST", boost=60
        )

    async def test_boost_on_re_polls_the_fixture(self, file_session):
        device = _find(file_session, "water_heater", _HOTWATER_ID)
        before = file_session.config.last_update
        assert await file_session.hotwater.set_boost_on(device, 60) is None
        assert file_session.config.last_update > before
        updated = await file_session.hotwater.get_water_heater(device)
        assert updated.status == {"current_operation": "SCHEDULE"}

    @pytest.mark.parametrize("mins", [0, -5, "0"])
    async def test_boost_on_rejects_non_positive_minutes(self, file_session, mins):
        device = _find(file_session, "water_heater", _HOTWATER_ID)
        set_state = _capture_commands(file_session)
        with pytest.raises(ValueError, match="Boost minutes must be positive"):
            await file_session.hotwater.set_boost_on(device, mins)
        set_state.assert_not_awaited()

    async def test_boost_on_offline_hot_water_raises(self, file_session):
        device = _find(file_session, "water_heater", _HOTWATER_ID)
        file_session.data.products[_HOTWATER_ID]["props"]["online"] = False
        set_state = _capture_commands(file_session)
        with pytest.raises(HiveDeviceOffline, match="is offline"):
            await file_session.hotwater.set_boost_on(device, 60)
        set_state.assert_not_awaited()

    async def test_boost_off_restores_the_previous_mode(self, file_session):
        device = _find(file_session, "water_heater", _HOTWATER_ID)
        _start_boost(file_session, _HOTWATER_ID, 60)
        set_state = _capture_commands(file_session)
        await file_session.hotwater.set_boost_off(device)
        set_state.assert_awaited_once_with("hotwater", _HOTWATER_ID, mode="OFF")

    async def test_boost_round_trip_through_the_status(self, file_session):
        device = _find(file_session, "water_heater", _HOTWATER_ID)
        _start_boost(file_session, _HOTWATER_ID, 60)
        updated = await file_session.hotwater.get_water_heater(device)
        assert updated.status == {"current_operation": "OFF"}
        await file_session.hotwater.set_boost_off(device)
        updated = await file_session.hotwater.get_water_heater(device)
        assert updated.status == {"current_operation": "SCHEDULE"}

    async def test_boost_off_when_not_boosting_is_a_no_op(self, file_session, caplog):
        device = _find(file_session, "water_heater", _HOTWATER_ID)
        set_state = _capture_commands(file_session)
        caplog.set_level(logging.DEBUG, logger="apyhiveapi.hotwater")
        await file_session.hotwater.set_boost_off(device)
        set_state.assert_not_awaited()
        assert f"{device.ha_name} is not boosting" in caplog.text

    async def test_boost_off_without_a_previous_mode_raises(self, file_session):
        device = _find(file_session, "water_heater", _HOTWATER_ID)
        _start_boost(file_session, _HOTWATER_ID, 60)
        file_session.data.products[_HOTWATER_ID]["props"]["previous"] = {}
        set_state = _capture_commands(file_session)
        with pytest.raises(HiveApiError, match="did not report the mode to restore"):
            await file_session.hotwater.set_boost_off(device)
        set_state.assert_not_awaited()
