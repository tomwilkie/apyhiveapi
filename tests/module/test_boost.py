"""Module-level tests of boosting heating and hot water against the file-mode session.

The bundled fixture is read-only: ``Hive.set_state`` sends nothing in file
mode and every command re-reads the fixture, so a boost reported by Hive is
imitated by editing the session data and the command sent is observed by
replacing ``set_state`` with an ``AsyncMock``.
"""

from unittest.mock import AsyncMock

_HEATING_ID = "heating-0000-0000-0000-000000000001"
_HOTWATER_ID = "hotwater-0000-0000-0000-000000000001"
_TRV_CONTROL_ID = "trvcontrol-0000-0000-0000-000000000001"


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


class TestHeatingBoost:
    async def test_boost_on_sends_the_command_and_re_polls(self, file_session):
        device = _find(file_session, "climate", _HEATING_ID)
        set_state = _capture_commands(file_session)
        before = file_session.config.last_update
        assert await file_session.heating.set_boost_on(device, "30", 20) is None
        set_state.assert_awaited_once_with(
            "heating", _HEATING_ID, mode="BOOST", boost=30, target=20.0
        )
        assert file_session.config.last_update > before
        updated = await file_session.heating.get_climate(device)
        assert updated.status["boost"] == "OFF"

    async def test_trv_control_boost_targets_its_own_product(self, file_session):
        device = _find(file_session, "climate", _TRV_CONTROL_ID)
        set_state = _capture_commands(file_session)
        await file_session.heating.set_boost_on(device, 15, 18)
        set_state.assert_awaited_once_with(
            "trvcontrol", _TRV_CONTROL_ID, mode="BOOST", boost=15, target=18.0
        )

    async def test_boost_off_restores_the_fixture_mode_and_clears_the_status(
        self, file_session
    ):
        device = _find(file_session, "climate", _HEATING_ID)
        _start_boost(file_session, _HEATING_ID)
        updated = await file_session.heating.get_climate(device)
        assert updated.status["boost"] == "ON"
        assert updated.status["mode"] == "OFF"
        set_state = _capture_commands(file_session)
        await file_session.heating.set_boost_off(device)
        set_state.assert_awaited_once_with("heating", _HEATING_ID, mode="OFF", target=7)
        updated = await file_session.heating.get_climate(device)
        assert updated.status["boost"] == "OFF"
        assert updated.status["mode"] == "OFF"


class TestHotWaterBoost:
    async def test_boost_on_sends_the_command_and_re_polls(self, file_session):
        device = _find(file_session, "water_heater", _HOTWATER_ID)
        set_state = _capture_commands(file_session)
        before = file_session.config.last_update
        assert await file_session.hotwater.set_boost_on(device, "60") is None
        set_state.assert_awaited_once_with(
            "hotwater", _HOTWATER_ID, mode="BOOST", boost=60
        )
        assert file_session.config.last_update > before
        updated = await file_session.hotwater.get_water_heater(device)
        assert updated.status == {"current_operation": "SCHEDULE", "boost": "OFF"}

    async def test_boost_off_restores_the_fixture_mode_and_clears_the_status(
        self, file_session
    ):
        device = _find(file_session, "water_heater", _HOTWATER_ID)
        _start_boost(file_session, _HOTWATER_ID, 60)
        updated = await file_session.hotwater.get_water_heater(device)
        assert updated.status == {"current_operation": "OFF", "boost": "ON"}
        set_state = _capture_commands(file_session)
        await file_session.hotwater.set_boost_off(device)
        set_state.assert_awaited_once_with("hotwater", _HOTWATER_ID, mode="OFF")
        updated = await file_session.hotwater.get_water_heater(device)
        assert updated.status == {"current_operation": "SCHEDULE", "boost": "OFF"}
