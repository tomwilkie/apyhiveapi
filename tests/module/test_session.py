"""Module tests for the Hive session against the bundled data.json fixture."""

from datetime import UTC, datetime, timedelta

import pytest

from apyhiveapi import Hive
from apyhiveapi.models import Device

_HUB_ID = "hub-0000-0000-0000-000000000001"
_HEATING_ID = "heating-0000-0000-0000-000000000001"
_THERMOSTAT_ID = "thermostatui-0000-0000-0000-000000000001"
_TRV_ID = "trv-0000-0000-0000-000000000001"

_EVERY_DEVICE = pytest.mark.parametrize(
    ("entity_type", "index"),
    [
        pytest.param("parent", 0, id="hub"),
        pytest.param("climate", 0, id="heating"),
        pytest.param("climate", 1, id="trv-control"),
        pytest.param("water_heater", 0, id="hot-water"),
    ],
)


class TestFileSessionDevices:
    """The devices start_session discovers in the fixture."""

    async def test_runs_in_file_mode(self, file_session):
        assert file_session.config.file is True
        assert file_session.config.username == "use@file.com"
        assert file_session.api.websession is None

    async def test_loads_the_fixture(self, file_session):
        assert len(file_session.data.products) == 17
        assert len(file_session.data.devices) == 18
        assert file_session.data.user["temperatureUnit"] == "C"

    async def test_device_list_has_exactly_the_three_entity_types(self, file_session):
        assert list(file_session.device_list) == ["parent", "climate", "water_heater"]

    async def test_discovers_one_hub_two_climates_and_one_water_heater(
        self, file_session
    ):
        counts = {
            key: len(devices) for key, devices in file_session.device_list.items()
        }
        assert counts == {"parent": 1, "climate": 2, "water_heater": 1}

    async def test_hub_is_the_parent_device(self, file_session):
        parent = file_session.device_list["parent"][0]
        assert file_session.hub_id == _HUB_ID
        assert parent.hive_id == _HUB_ID
        assert parent.device_id == _HUB_ID
        assert parent.hive_type == "hub"
        assert parent.ha_type == "parent"
        assert parent.hive_name == "Hub"
        assert parent.device_data["model"] == "SENSE"

    async def test_heating_and_trv_control_become_climate_devices(self, file_session):
        by_type = {d.hive_type: d for d in file_session.device_list["climate"]}
        assert set(by_type) == {"heating", "trvcontrol"}
        heating = by_type["heating"]
        assert heating.hive_id == _HEATING_ID
        assert heating.hive_name == "Heating Zone 1"
        assert heating.device_id == _THERMOSTAT_ID
        assert heating.device_data["model"] == "SLT3"
        trv = by_type["trvcontrol"]
        assert trv.hive_name == "TRV Zone 1"
        assert trv.device_id == _TRV_ID
        assert trv.device_data["model"] == "TRV001"

    async def test_water_heater_is_named_after_its_product(self, file_session):
        heater = file_session.device_list["water_heater"][0]
        assert heater.hive_type == "hotwater"
        assert heater.ha_type == "water_heater"
        assert heater.hive_name == "Hotwater"
        assert heater.ha_name == "Hotwater"
        # The hardware behind it is the zone's thermostat.
        assert heater.device_id == _THERMOSTAT_ID
        assert heater.device_name == "Heating Zone 1"

    async def test_temperature_unit_is_set_on_climate_devices_only(self, file_session):
        units = [
            device.temperature_unit for device in file_session.device_list["climate"]
        ]
        assert units == ["C", "C"]
        assert file_session.device_list["water_heater"][0].temperature_unit is None
        assert file_session.device_list["parent"][0].temperature_unit is None

    @_EVERY_DEVICE
    async def test_every_device_is_complete(self, file_session, entity_type, index):
        device = file_session.device_list[entity_type][index]
        assert isinstance(device, Device)
        assert device.hive_id
        assert device.ha_name
        assert device.parent_device == _HUB_ID
        assert device.status is None

    async def test_get_climate_returns_status(self, file_session):
        device = file_session.device_list["climate"][0]
        updated = await file_session.heating.get_climate(device)
        assert updated.status is not None
        assert "current_temperature" in updated.status
        assert "target_temperature" in updated.status
        assert "mode" in updated.status


class TestFileSessionPolling:
    """Polling the fixture through update_data."""

    async def test_update_data_is_rate_limited_right_after_start(self, file_session):
        assert await file_session.update_data() is False

    async def test_update_data_polls_once_the_interval_has_passed(self, file_session):
        file_session.config.last_update = datetime.now(UTC) - timedelta(days=1)
        assert await file_session.update_data() is True
        assert datetime.now(UTC) - file_session.config.last_update < timedelta(
            seconds=5
        )
        assert await file_session.update_data() is False

    async def test_a_poll_reloads_the_fixture(self, file_session):
        file_session.data.products[_HEATING_ID]["state"]["target"] = 99
        file_session.config.last_update = datetime.now(UTC) - timedelta(days=1)
        assert await file_session.update_data() is True
        assert file_session.data.products[_HEATING_ID]["state"]["target"] == 7
        assert len(file_session.data.products) == 17


class TestStartFileSession:
    """Starting sessions in file mode."""

    async def test_scan_interval_option_is_applied(self):
        async with Hive(username="use@file.com", password="") as hive:
            await hive.start_session({"options": {"scan_interval": 60}})
            assert hive.config.scan_interval == timedelta(seconds=60)

    async def test_file_mode_from_the_config_needs_no_tokens(self):
        async with Hive() as hive:
            result = await hive.start_session({"username": "use@file.com"})
            assert hive.config.file is True
            assert len(result["climate"]) == 2

    async def test_start_session_twice_rebuilds_the_same_devices(self, file_session):
        before = [d.hive_id for d in file_session.device_list["climate"]]
        result = await file_session.start_session({})
        assert [d.hive_id for d in result["climate"]] == before
        assert result is file_session.device_list

    async def test_close_is_safe_without_a_websession(self):
        hive = Hive(username="use@file.com", password="")
        await hive.start_session()
        await hive.close()
        assert hive.api.websession is None
