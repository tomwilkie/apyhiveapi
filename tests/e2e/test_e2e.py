"""End-to-end tests over the file-mode session (use@file.com) and its fixture."""

from datetime import timedelta

import pytest

from apyhiveapi.exceptions import HiveDeviceOffline
from apyhiveapi.heating import MAX_TARGET_TEMPERATURE, MIN_TARGET_TEMPERATURE
from apyhiveapi.hive import MIN_SCAN_INTERVAL

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
    async def test_only_supported_entity_types_are_created(self, file_session):
        assert set(file_session.device_list) == {"parent", "climate", "water_heater"}

    async def test_heating_products_become_the_climate_devices(self, file_session):
        hive_ids = {device.hive_id for device in file_session.device_list["climate"]}
        assert hive_ids == {_HEATING_ID, _TRV_CONTROL_ID}

    async def test_hot_water_becomes_the_water_heater_device(self, file_session):
        hive_ids = [
            device.hive_id for device in file_session.device_list["water_heater"]
        ]
        assert hive_ids == [_HOTWATER_ID]

    async def test_unsupported_products_are_skipped(self, file_session):
        hive_ids = {
            device.hive_id
            for devices in file_session.device_list.values()
            for device in devices
        }
        unsupported = [
            hive_id
            for hive_id in hive_ids
            if hive_id.startswith(("plug-", "light-", "sensor-"))
        ]
        assert unsupported == []

    @_EVERY_DEVICE
    async def test_all_devices_have_ha_name(self, file_session, entity_type, index):
        assert file_session.device_list[entity_type][index].ha_name

    async def test_hub_is_the_parent_device(self, file_session):
        parents = file_session.device_list["parent"]
        assert len(parents) == 1
        assert parents[0].hive_type == "hub"
        assert parents[0].device_id == file_session.hub_id == _HUB_ID
        assert {"model", "manufacturer", "version"} <= set(parents[0].device_data)

    async def test_heating_and_hot_water_attach_to_the_thermostat(self, file_session):
        heating = _find(file_session, "climate", _HEATING_ID)
        hotwater = _find(file_session, "water_heater", _HOTWATER_ID)
        assert heating.device_id == _THERMOSTAT_ID
        assert hotwater.device_id == _THERMOSTAT_ID
        assert heating.ha_name == "Heating Zone 1"
        assert hotwater.ha_name == "Hotwater"
        assert heating.device_name == hotwater.device_name == "Heating Zone 1"
        assert heating.parent_device == _HUB_ID
        assert heating.device_data["model"] == "SLT3"

    async def test_trv_control_attaches_to_its_trv(self, file_session):
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
        assert updated.status == {"current_operation": "SCHEDULE"}
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
        assert hotwater.status == {"current_operation": "SCHEDULE"}
        assert hotwater.device_data["online"] is True

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

    async def test_commands_work_while_the_thermostat_is_offline(self, file_session):
        file_session.data.devices[_THERMOSTAT_ID]["props"]["online"] = False
        hotwater = _find(file_session, "water_heater", _HOTWATER_ID)
        assert await file_session.hotwater.set_mode(hotwater, "MANUAL") is None


class TestCommands:
    async def test_set_mode_completes_and_re_polls(self, file_session):
        device = _find(file_session, "climate", _HEATING_ID)
        before = file_session.config.last_update
        assert await file_session.heating.set_mode(device, "MANUAL") is None
        assert file_session.config.last_update > before

    async def test_file_mode_sends_nothing_so_the_state_is_unchanged(
        self, file_session
    ):
        device = _find(file_session, "climate", _HEATING_ID)
        await file_session.heating.set_target_temperature(device, 21)
        updated = await file_session.heating.get_climate(device)
        assert updated.status["target_temperature"] == 7.0

    async def test_hot_water_set_mode_completes(self, file_session):
        device = _find(file_session, "water_heater", _HOTWATER_ID)
        assert await file_session.hotwater.set_mode(device, "MANUAL") is None
        updated = await file_session.hotwater.get_water_heater(device)
        assert updated.status == {"current_operation": "SCHEDULE"}


class TestRateLimiting:
    async def test_update_data_is_rate_limited_within_the_scan_interval(
        self, file_session
    ):
        assert await file_session.update_data() is False

    async def test_update_data_polls_once_the_interval_has_passed(self, file_session):
        file_session.config.last_update -= file_session.config.scan_interval
        assert await file_session.update_data() is True
        assert await file_session.update_data() is False

    async def test_update_interval_is_never_below_the_minimum(self, file_session):
        file_session.update_interval(1)
        assert file_session.config.scan_interval == MIN_SCAN_INTERVAL
        file_session.update_interval(300)
        assert file_session.config.scan_interval == timedelta(seconds=300)
