"""Tests for session start and device discovery on Hive."""

# pylint: disable=protected-access

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from apyhiveapi.exceptions import HiveConnectionError, HiveUnknownConfiguration
from apyhiveapi.hive import Hive

_HUB = {
    "id": "hub-1",
    "type": "hub",
    "state": {"name": "Hive Hub"},
    "props": {"online": True, "model": "HUB", "version": "1", "manufacturer": "Hive"},
}
_THERMOSTAT = {
    "id": "thermo-1",
    "type": "thermostatui",
    "state": {"name": "Hallway"},
    "props": {"zone": "zone-A", "online": True, "model": "SLT3"},
}
_HEATING = {"id": "heat-1", "type": "heating", "props": {"zone": "zone-A"}}
_HOTWATER = {"id": "hw-1", "type": "hotwater", "props": {"zone": "zone-A"}}
_DEVICES = {"hub-1": _HUB, "thermo-1": _THERMOSTAT}


def _make_hive(products=None, devices=None, user=None):
    """Return a Hive with the given data loaded and the network mocked out."""
    hive = Hive(username="user@example.com", password="pw")
    hive.data.products = products if products is not None else {}
    hive.data.devices = devices if devices is not None else {}
    hive.data.user = user or {}
    hive.get_devices = AsyncMock(return_value=True)
    hive.update_tokens = AsyncMock()
    return hive


class TestOpenFile:
    """Tests for Hive.open_file."""

    def test_returns_the_fixture_with_original_and_parsed(self):
        result = Hive().open_file("data.json")
        assert "original" in result
        assert isinstance(result["parsed"], dict)


class TestConfigureFileMode:
    """Tests for Hive._configure_file_mode."""

    @pytest.mark.parametrize(
        ("username", "expected"),
        [("use@file.com", True), ("real@user.com", False), (None, False), ("", False)],
    )
    def test_only_the_magic_username_enables_file_mode(self, username, expected):
        hive = Hive()
        hive._configure_file_mode(username)
        assert hive.config.file is expected


class TestParseTokenCreated:
    """Tests for Hive._parse_token_created."""

    def test_aware_datetime_is_converted_to_utc(self):
        value = datetime(2026, 1, 1, 12, tzinfo=UTC)
        assert Hive._parse_token_created(value) == value

    def test_naive_datetime_is_taken_as_local_time(self):
        value = datetime(2026, 1, 1, 12)  # noqa: DTZ001 - naive input is the point
        result = Hive._parse_token_created(value)
        assert result == value.astimezone(UTC)
        assert result.tzinfo is UTC

    def test_iso_string_is_parsed(self):
        result = Hive._parse_token_created("2026-01-01T12:00:00+00:00")
        assert result == datetime(2026, 1, 1, 12, tzinfo=UTC)

    def test_invalid_string_returns_none_and_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="apyhiveapi.hive"):
            assert Hive._parse_token_created("not a date") is None
        assert "Ignoring invalid token_created" in caplog.text

    @pytest.mark.parametrize("value", [None, "", 0])
    def test_falsy_values_return_none(self, value):
        assert Hive._parse_token_created(value) is None


class TestApplyDeviceData:
    """Tests for Hive._apply_device_data."""

    def test_too_few_values_raises(self):
        hive = _make_hive()
        with pytest.raises(HiveUnknownConfiguration):
            hive._apply_device_data(["group", "key"])

    def test_three_values_set_the_device_credentials(self):
        hive = _make_hive()
        hive._apply_device_data(["group", "key", "password"])
        assert hive.auth.device_group_key == "group"
        assert hive.auth.device_key == "key"
        assert hive.auth.device_password == "password"
        assert hive.tokens.token_created == datetime.min.replace(tzinfo=UTC)

    def test_fourth_value_sets_token_created(self):
        hive = _make_hive()
        issued = datetime(2026, 1, 1, 12, tzinfo=UTC)
        hive._apply_device_data(["group", "key", "password", issued.isoformat()])
        assert hive.tokens.token_created == issued

    def test_invalid_fourth_value_leaves_token_created(self):
        hive = _make_hive()
        hive._apply_device_data(["group", "key", "password", "garbage"])
        assert hive.tokens.token_created == datetime.min.replace(tzinfo=UTC)


class TestStartSession:
    """Tests for Hive.start_session."""

    async def test_no_config_in_file_mode_polls_and_builds_devices(self):
        hive = _make_hive(products={"heat-1": _HEATING}, devices=_DEVICES)
        hive.config.username = "use@file.com"
        result = await hive.start_session()
        assert hive.config.file is True
        hive.get_devices.assert_awaited_once_with()
        hive.update_tokens.assert_not_called()
        assert [d.hive_id for d in result["climate"]] == ["heat-1"]

    async def test_file_mode_username_in_config_enables_file_mode(self):
        hive = _make_hive(products={"heat-1": _HEATING}, devices=_DEVICES)
        await hive.start_session({"username": "use@file.com"})
        assert hive.config.file is True

    async def test_scan_interval_option_is_applied(self):
        hive = _make_hive(products={"heat-1": _HEATING}, devices=_DEVICES)
        hive.config.username = "use@file.com"
        await hive.start_session({"options": {"scan_interval": 300}})
        assert hive.config.scan_interval == timedelta(seconds=300)

    async def test_tokens_credentials_and_device_data_are_applied(self):
        hive = _make_hive(products={"heat-1": _HEATING}, devices=_DEVICES)
        tokens = {"token": "t", "accessToken": "a", "refreshToken": "r"}
        await hive.start_session(
            {
                "username": "real@user.com",
                "password": "secret",  # pragma: allowlist secret
                "tokens": tokens,
                "device_data": ["group", "key", "password"],
            }
        )
        hive.update_tokens.assert_awaited_once_with(tokens, update_expiry_time=False)
        assert hive.auth.username == "real@user.com"
        assert hive.auth.password == "secret"  # pragma: allowlist secret
        assert hive.auth.device_key == "key"

    async def test_config_without_tokens_raises(self):
        hive = _make_hive(products={"heat-1": _HEATING}, devices=_DEVICES)
        with pytest.raises(HiveUnknownConfiguration):
            await hive.start_session({"username": "real@user.com"})
        hive.get_devices.assert_not_called()

    async def test_failed_poll_with_no_data_raises_connection_error(self):
        hive = _make_hive()
        hive.get_devices = AsyncMock(return_value=False)
        with pytest.raises(HiveConnectionError):
            await hive.start_session({"tokens": {"token": "t"}})

    async def test_successful_poll_with_no_data_raises_unknown_configuration(self):
        hive = _make_hive()
        with pytest.raises(HiveUnknownConfiguration):
            await hive.start_session({"tokens": {"token": "t"}})


class TestCreateDevices:
    """Tests for Hive.create_devices and Hive._add_device."""

    def test_hub_becomes_the_parent_device(self):
        hive = _make_hive(devices=_DEVICES)
        result = hive.create_devices()
        assert hive.hub_id == "hub-1"
        parent = result["parent"][0]
        assert parent.hive_type == "hub"
        assert parent.ha_type == "parent"
        assert parent.device_id == "hub-1"
        assert parent.parent_device == "hub-1"
        assert parent.device_data["model"] == "HUB"
        assert result["climate"] == []
        assert result["water_heater"] == []

    def test_missing_hub_logs_a_warning(self, caplog):
        hive = _make_hive(devices={"thermo-1": _THERMOSTAT})
        with caplog.at_level(logging.WARNING, logger="apyhiveapi.hive"):
            result = hive.create_devices()
        assert result["parent"] == []
        assert hive.hub_id is None
        assert "No hub device found" in caplog.text

    def test_heating_product_becomes_a_climate_device(self):
        hive = _make_hive(
            products={"heat-1": _HEATING},
            devices=_DEVICES,
            user={"temperatureUnit": "C"},
        )
        climate = hive.create_devices()["climate"][0]
        assert climate.hive_id == "heat-1"
        assert climate.hive_type == "heating"
        assert climate.ha_type == "climate"
        assert climate.device_id == "thermo-1"
        assert climate.hive_name == "Hallway"
        assert climate.ha_name == "Hallway"
        assert climate.device_data["model"] == "SLT3"
        assert climate.parent_device == "hub-1"
        assert climate.temperature_unit == "C"

    def test_hotwater_product_becomes_a_water_heater_without_unit(self):
        hive = _make_hive(
            products={"hw-1": _HOTWATER},
            devices=_DEVICES,
            user={"temperatureUnit": "C"},
        )
        heater = hive.create_devices()["water_heater"][0]
        assert heater.ha_type == "water_heater"
        assert heater.device_id == "thermo-1"
        assert heater.temperature_unit is None

    def test_trv_control_uses_its_trv(self):
        trv = {"id": "trv-1", "type": "trv", "state": {"name": "Bedroom"}, "props": {}}
        hive = _make_hive(
            products={
                "trvc-1": {
                    "id": "trvc-1",
                    "type": "trvcontrol",
                    "props": {"trvs": ["trv-1"]},
                }
            },
            devices={**_DEVICES, "trv-1": trv},
        )
        climate = hive.create_devices()["climate"][0]
        assert climate.device_id == "trv-1"
        assert climate.hive_type == "trvcontrol"
        assert climate.ha_name == "Bedroom"

    def test_receiver_is_renamed_heating(self):
        receiver = {
            "id": "rec-1",
            "type": "thermostatui",
            "state": {"name": "Receiver"},
            "props": {"zone": "zone-A"},
        }
        hive = _make_hive(products={"heat-1": _HEATING}, devices={"rec-1": receiver})
        climate = hive.create_devices()["climate"][0]
        assert climate.hive_name == "Heating"
        assert climate.ha_name == "Heating"

    def test_product_with_error_is_skipped(self):
        hive = _make_hive(
            products={"bad": {"id": "bad", "type": "heating", "error": "not found"}},
            devices=_DEVICES,
        )
        assert hive.create_devices()["climate"] == []

    def test_unsupported_product_type_is_skipped(self):
        hive = _make_hive(
            products={"lamp": {"id": "lamp", "type": "warmwhitelight"}},
            devices=_DEVICES,
        )
        result = hive.create_devices()
        assert result["climate"] == []
        assert result["water_heater"] == []

    def test_group_hotwater_product_is_skipped(self):
        hive = _make_hive(
            products={"g1": {**_HOTWATER, "id": "g1", "isGroup": True}},
            devices=_DEVICES,
        )
        assert hive.create_devices()["water_heater"] == []

    def test_group_heating_product_is_kept(self):
        hive = _make_hive(
            products={"g1": {**_HEATING, "id": "g1", "isGroup": True}},
            devices=_DEVICES,
        )
        assert len(hive.create_devices()["climate"]) == 1

    def test_product_with_missing_device_is_skipped_with_warning(self, caplog):
        trvc = {"id": "trvc-1", "type": "trvcontrol", "props": {"trvs": ["missing"]}}
        hive = _make_hive(products={"trvc-1": trvc}, devices=_DEVICES)
        with caplog.at_level(logging.WARNING, logger="apyhiveapi.hive"):
            result = hive.create_devices()
        assert result["climate"] == []
        assert "Skipping trvcontrol trvc-1" in caplog.text

    def test_malformed_device_state_is_skipped(self):
        broken = {**_THERMOSTAT, "state": ["not", "a", "dict"]}
        hive = _make_hive(products={"heat-1": _HEATING}, devices={"thermo-1": broken})
        assert hive.create_devices()["climate"] == []

    def test_device_list_is_rebuilt_on_each_call(self):
        hive = _make_hive(products={"heat-1": _HEATING}, devices=_DEVICES)
        hive.create_devices()
        assert len(hive.create_devices()["climate"]) == 1
