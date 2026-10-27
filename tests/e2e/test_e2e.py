"""End-to-end tests over the file-mode session (use@file.com) and its fixture."""

import pytest

from apyhiveapi.exceptions import HiveDeviceOffline
from apyhiveapi.heating import MAX_TARGET_TEMPERATURE, MIN_TARGET_TEMPERATURE
from apyhiveapi.models import Device

_HUB_ID = "hub-0000-0000-0000-000000000001"
_THERMOSTAT_ID = "thermostatui-0000-0000-0000-000000000001"
_TRV_ID = "trv-0000-0000-0000-000000000001"
_HEATING_ID = "heating-0000-0000-0000-000000000001"
_HOTWATER_ID = "hotwater-0000-0000-0000-000000000001"
_TRV_CONTROL_ID = "trvcontrol-0000-0000-0000-000000000001"
_EVERY_DEVICE = pytest.mark.parametrize(
    ("entity_type", "index"),
    [
        pytest.param("parent", 0, id="hub"),
        pytest.param("climate", 0, id="heating"),
        pytest.param("climate", 1, id="trv-control"),
        pytest.param("water_heater", 0, id="hot-water"),
    ],
)
_OFFLINE_CLIMATE_STATUS = {
    "current_temperature": None,
    "target_temperature": None,
    "action": None,
    "mode": None,
    "boost": None,
}


def _find(session, entity_type, hive_id):
    """Return the device for *hive_id* from the session's device list."""
    return next(
        device
        for device in session.device_list[entity_type]
        if device.hive_id == hive_id
    )


class TestDeviceListIntegrity:
    async def test_the_fixture_yields_the_hub_two_climates_and_the_hot_water(
        self, file_session
    ):
        assert {
            key: [device.hive_id for device in devices]
            for key, devices in file_session.device_list.items()
        } == {
            "parent": [_HUB_ID],
            "climate": [_HEATING_ID, _TRV_CONTROL_ID],
            "water_heater": [_HOTWATER_ID],
        }

    @_EVERY_DEVICE
    async def test_every_device_is_complete(self, file_session, entity_type, index):
        device = file_session.device_list[entity_type][index]
        assert isinstance(device, Device)
        assert device.hive_id
        assert device.ha_name
        assert device.status is None

    async def test_hub_is_the_parent_device(self, file_session):
        parents = file_session.device_list["parent"]
        assert len(parents) == 1
        hub = parents[0]
        assert hub.hive_type == "hub"
        assert hub.hive_id == hub.device_id == file_session.hub_id == _HUB_ID
        assert hub.hive_name == "Hub"
        assert hub.device_data["model"] == "SENSE"
        assert {"model", "manufacturer", "version"} <= set(hub.device_data)
        assert hub.parent_device is None
        products = [
            *file_session.device_list["climate"],
            *file_session.device_list["water_heater"],
        ]
        assert [device.parent_device for device in products] == [_HUB_ID] * 3

    async def test_products_attach_to_their_hardware(self, file_session):
        heating = _find(file_session, "climate", _HEATING_ID)
        assert heating.device_id == _THERMOSTAT_ID
        assert heating.ha_name == "Heating Zone 1"
        assert heating.device_name == "Heating Zone 1"
        assert heating.device_data["model"] == "SLT3"

        hotwater = _find(file_session, "water_heater", _HOTWATER_ID)
        assert hotwater.hive_type == "hotwater"
        assert hotwater.ha_type == "water_heater"
        assert hotwater.ha_name == "Hotwater"
        # The hardware behind it is the zone's thermostat.
        assert hotwater.device_id == _THERMOSTAT_ID
        assert hotwater.device_name == "Heating Zone 1"

        trv = _find(file_session, "climate", _TRV_CONTROL_ID)
        assert trv.device_id == _TRV_ID
        assert trv.ha_name == "TRV Zone 1"
        assert trv.device_data["model"] == "TRV001"

    async def test_climate_devices_carry_the_account_temperature_unit(
        self, file_session
    ):
        climate = file_session.device_list["climate"]
        water_heater = file_session.device_list["water_heater"]
        assert [device.temperature_unit for device in climate] == ["C", "C"]
        assert [device.temperature_unit for device in water_heater] == [None]


class TestGetDeviceStatus:
    async def test_get_climate_reports_the_heating_zone(self, file_session):
        device = _find(file_session, "climate", _HEATING_ID)
        updated = await file_session.heating.get_climate(device)
        assert updated is device
        assert updated.status == {
            "current_temperature": 19.2,
            "target_temperature": 7.0,
            "action": False,
            "mode": "OFF",
            "boost": "OFF",
        }
        assert updated.min_temp == MIN_TARGET_TEMPERATURE
        assert updated.max_temp == MAX_TARGET_TEMPERATURE
        assert updated.device_data["online"] is True
        assert updated.device_data["model"] == "SLT3"
        assert updated.parent_device == _HUB_ID

    async def test_get_climate_reports_the_trv_zone(self, file_session):
        device = _find(file_session, "climate", _TRV_CONTROL_ID)
        updated = await file_session.heating.get_climate(device)
        assert updated.status == {
            "current_temperature": 17.6,
            "target_temperature": 7.0,
            "action": False,
            "mode": "SCHEDULE",
            "boost": "OFF",
        }
        assert updated.device_data["online"] is True
        assert updated.device_data["model"] == "TRV001"

    async def test_get_water_heater_reports_the_hot_water(self, file_session):
        device = _find(file_session, "water_heater", _HOTWATER_ID)
        updated = await file_session.hotwater.get_water_heater(device)
        assert updated is device
        assert updated.status == {"current_operation": "SCHEDULE", "boost": "OFF"}
        assert updated.device_data["online"] is True


class TestOfflineDevices:
    async def test_offline_heating_is_reported_once_until_it_recovers(
        self, file_session, caplog
    ):
        device = _find(file_session, "climate", _HEATING_ID)
        props = file_session.data.products[_HEATING_ID]["props"]
        props["online"] = False
        updated = await file_session.heating.get_climate(device)
        assert updated.status == _OFFLINE_CLIMATE_STATUS
        assert updated.device_data["online"] is False
        await file_session.heating.get_climate(device)
        assert caplog.text.count("Heating Zone 1 is offline") == 1
        props["online"] = True
        updated = await file_session.heating.get_climate(device)
        assert updated.status["mode"] == "OFF"
        assert updated.device_data["online"] is True
        props["online"] = False
        await file_session.heating.get_climate(device)
        assert caplog.text.count("Heating Zone 1 is offline") == 2

    async def test_offline_thermostat_leaves_heating_and_hot_water_online(
        self, file_session
    ):
        heating = _find(file_session, "climate", _HEATING_ID)
        hotwater = _find(file_session, "water_heater", _HOTWATER_ID)
        file_session.data.devices[_THERMOSTAT_ID]["props"]["online"] = False
        await file_session.heating.get_climate(heating)
        await file_session.hotwater.get_water_heater(hotwater)
        assert heating.status["mode"] == "OFF"
        assert heating.device_data["online"] is True
        assert hotwater.status == {"current_operation": "SCHEDULE", "boost": "OFF"}
        assert hotwater.device_data["online"] is True
        assert await file_session.hotwater.set_mode(hotwater, "MANUAL") is None

    async def test_offline_trv_takes_its_zone_offline(self, file_session):
        device = _find(file_session, "climate", _TRV_CONTROL_ID)
        file_session.data.devices[_TRV_ID]["props"]["online"] = False
        updated = await file_session.heating.get_climate(device)
        assert updated.status == _OFFLINE_CLIMATE_STATUS
        assert updated.device_data["online"] is False

    async def test_commands_to_an_offline_product_raise(self, file_session):
        file_session.data.products[_HEATING_ID]["props"]["online"] = False
        file_session.data.products[_HOTWATER_ID]["props"]["online"] = False
        heating = _find(file_session, "climate", _HEATING_ID)
        hotwater = _find(file_session, "water_heater", _HOTWATER_ID)
        with pytest.raises(HiveDeviceOffline, match="Heating Zone 1 is offline"):
            await file_session.heating.set_mode(heating, "MANUAL")
        with pytest.raises(HiveDeviceOffline, match="Hotwater is offline"):
            await file_session.hotwater.set_mode(hotwater, "MANUAL")


class TestCommands:
    async def test_commands_complete_re_poll_and_leave_the_fixture_unchanged(
        self, file_session
    ):
        heating = _find(file_session, "climate", _HEATING_ID)
        hotwater = _find(file_session, "water_heater", _HOTWATER_ID)
        before = file_session.config.last_update
        assert await file_session.heating.set_target_temperature(heating, 21) is None
        assert await file_session.hotwater.set_mode(hotwater, "MANUAL") is None
        assert file_session.config.last_update > before
        updated = await file_session.heating.get_climate(heating)
        assert updated.status["target_temperature"] == 7.0
        updated = await file_session.hotwater.get_water_heater(hotwater)
        assert updated.status == {"current_operation": "SCHEDULE", "boost": "OFF"}


class TestRateLimiting:
    async def test_update_data_polls_once_per_interval_and_reloads_the_fixture(
        self, file_session
    ):
        assert await file_session.update_data() is False
        state = file_session.data.products[_HEATING_ID]["state"]
        state["target"] = 99
        file_session.config.last_update -= file_session.config.scan_interval
        assert await file_session.update_data() is True
        assert file_session.data.products[_HEATING_ID]["state"]["target"] == 7
        assert await file_session.update_data() is False
