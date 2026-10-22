"""E2E integration tests using the bundled data.json fixture (use@file.com)."""

# pylint: disable=redefined-outer-name


class TestGetDeviceStatus:
    """Tests that device get_* methods return populated status dicts."""

    async def test_get_climate_returns_all_fields(self, file_session):
        """Climate status has current_temperature, target_temperature, mode, boost."""
        device = file_session.device_list["climate"][0]
        updated = await file_session.heating.get_climate(device)
        assert updated.status is not None
        for field in ("current_temperature", "target_temperature", "mode", "boost"):
            assert field in updated.status

    async def test_get_water_heater_returns_current_operation(self, file_session):
        """Hot-water status has current_operation key."""
        device = file_session.device_list["water_heater"][0]
        updated = await file_session.hotwater.get_water_heater(device)
        assert updated.status is not None
        assert "current_operation" in updated.status


class TestRateLimitingAndCaching:
    """Tests for polling rate-limit and entity cache behaviour."""

    async def test_update_data_rate_limited_within_scan_interval(self, file_session):
        """Second update_data call within scan interval returns False (no re-poll)."""
        device = file_session.device_list["climate"][0]
        await file_session.heating.get_climate(device)
        result = await file_session.update_data(device)
        assert result is False

    async def test_entity_cache_round_trip(self, file_session):
        """Device stored by get_climate can be retrieved from entity cache."""
        device = file_session.device_list["climate"][0]
        await file_session.heating.get_climate(device)
        cached = file_session.get_cached_device(device)
        assert cached is not None
        assert cached.hive_id == device.hive_id


class TestDeviceListIntegrity:
    """Tests that create_devices populated the expected entity types."""

    async def test_only_supported_entity_types_are_created(self, file_session):
        assert set(file_session.device_list) == {"parent", "climate", "water_heater"}

    async def test_all_devices_have_ha_name(self, file_session):
        """Every device in every entity-type list has a non-empty ha_name."""
        for entity_type, devices in file_session.device_list.items():
            for device in devices:
                assert device.ha_name, (
                    f"{entity_type} device {device.hive_id} missing ha_name"
                )

    async def test_hub_is_the_parent_device(self, file_session):
        parents = file_session.device_list["parent"]
        assert len(parents) == 1
        assert parents[0].hive_type == "hub"
        assert parents[0].device_id == file_session.hub_id
        for key in ("model", "manufacturer", "version"):
            assert key in parents[0].device_data

    async def test_climate_devices_present(self, file_session):
        """Fixture produces at least one climate device."""
        assert file_session.device_list["climate"]

    async def test_water_heater_devices_present(self, file_session):
        """Fixture produces at least one water_heater device."""
        assert file_session.device_list["water_heater"]
