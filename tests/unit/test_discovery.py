"""Unit tests for session start and device discovery on Hive."""

# pylint: disable=protected-access

import logging
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from apyhiveapi.exceptions import HiveApiError, HiveConnectionError, HiveReauthRequired
from apyhiveapi.hive import MIN_SCAN_INTERVAL, Hive
from apyhiveapi.models import DEFAULT_SCAN_INTERVAL, Device

_LOGGER_NAME = "apyhiveapi.hive"
_EPOCH = datetime.min.replace(tzinfo=UTC)
_TOKENS = {
    "token": "id-token",
    "accessToken": "access-token",
    "refreshToken": "refresh-token",
}

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
_TRV = {
    "id": "trv-1",
    "type": "trv",
    "state": {"name": "Bedroom"},
    "props": {"model": "TRV001"},
}
_HEATING = {"id": "heat-1", "type": "heating", "props": {"zone": "zone-A"}}
_HOTWATER = {"id": "hw-1", "type": "hotwater", "props": {"zone": "zone-A"}}
_TRVCONTROL = {"id": "trvc-1", "type": "trvcontrol", "props": {"trvs": ["trv-1"]}}
_DEVICES = {"hub-1": _HUB, "thermo-1": _THERMOSTAT, "trv-1": _TRV}
_NODES = {
    "user": {"temperatureUnit": "C"},
    "products": [_HEATING, _HOTWATER, _TRVCONTROL],
    "devices": [_HUB, _THERMOSTAT, _TRV],
}


def _make_hive(products=None, devices=None, user=None):
    """Return a live-mode Hive holding the given data, with the network mocked."""
    hive = Hive(username="user@example.com", password="pw")
    hive.data.products = products if products is not None else {}
    hive.data.devices = devices if devices is not None else {}
    hive.data.user = user or {}
    hive.hive_refresh_tokens = AsyncMock()
    hive.api.get_all = AsyncMock(return_value=_NODES)
    return hive


class TestOpenFile:
    """Tests for Hive.open_file."""

    def test_returns_the_parsed_fixture(self):
        nodes = Hive().open_file("data.json")
        assert set(nodes) == {"user", "products", "devices"}
        assert isinstance(nodes["products"], list)
        assert isinstance(nodes["devices"], list)
        assert nodes["user"]["temperatureUnit"] == "C"

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            Hive().open_file("missing.json")


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

    def test_file_mode_is_never_switched_off_again(self):
        hive = Hive()
        hive._configure_file_mode("use@file.com")
        hive._configure_file_mode("real@user.com")
        assert hive.config.file is True


class TestParseTokenCreated:
    """Tests for Hive._parse_token_created."""

    def test_aware_datetime_is_returned_in_utc(self):
        value = datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=2)))
        result = Hive._parse_token_created(value)
        assert result == datetime(2026, 1, 1, 10, tzinfo=UTC)
        assert result.tzinfo is UTC

    def test_naive_datetime_is_taken_as_local_time(self):
        value = datetime.fromisoformat("2026-01-01T12:00:00")
        result = Hive._parse_token_created(value)
        assert result == value.astimezone(UTC)
        assert result.tzinfo is UTC

    def test_iso_string_is_parsed(self):
        result = Hive._parse_token_created("2026-01-01T12:00:00+01:00")
        assert result == datetime(2026, 1, 1, 11, tzinfo=UTC)

    def test_naive_iso_string_is_taken_as_local_time(self):
        result = Hive._parse_token_created("2026-01-01T12:00:00")
        local = datetime.fromisoformat("2026-01-01T12:00:00")
        assert result == local.astimezone(UTC)

    def test_round_trips_the_saved_isoformat(self):
        issued = datetime(2026, 1, 1, 12, 30, 15, tzinfo=UTC)
        assert Hive._parse_token_created(issued.isoformat()) == issued

    def test_invalid_string_returns_none_and_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            assert Hive._parse_token_created("not a date") is None
        assert "Ignoring the invalid token_created value in device_data" in caplog.text

    @pytest.mark.parametrize("value", [None, "", 0, 1.5, [], {}, b"2026"])
    def test_other_values_return_none_without_a_warning(self, value, caplog):
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            assert Hive._parse_token_created(value) is None
        assert not caplog.records


class TestApplyDeviceData:
    """Tests for Hive._apply_device_data."""

    @pytest.mark.parametrize("device_data", [[], ["group"], ["group", "key"]])
    def test_fewer_than_three_values_raises_without_applying(self, device_data):
        hive = _make_hive()
        with pytest.raises(HiveReauthRequired):
            hive._apply_device_data(device_data)
        assert hive.auth.device_group_key is None
        assert hive.auth.device_key is None
        assert hive.auth.device_password is None

    def test_three_values_set_the_device_credentials(self):
        hive = _make_hive()
        hive._apply_device_data(["group", "key", "password"])
        assert hive.auth.device_group_key == "group"
        assert hive.auth.device_key == "key"
        assert hive.auth.device_password == "password"  # pragma: allowlist secret
        assert hive.tokens.token_created == _EPOCH

    def test_a_tuple_is_accepted(self):
        hive = _make_hive()
        hive._apply_device_data(("group", "key", "password"))
        assert hive.auth.device_key == "key"

    def test_fourth_value_sets_token_created_from_a_string(self):
        hive = _make_hive()
        issued = datetime(2026, 1, 1, 12, tzinfo=UTC)
        hive._apply_device_data(["group", "key", "password", issued.isoformat()])
        assert hive.tokens.token_created == issued

    def test_fourth_value_sets_token_created_from_a_datetime(self):
        hive = _make_hive()
        issued = datetime(2026, 1, 1, 12, tzinfo=UTC)
        hive._apply_device_data(["group", "key", "password", issued])
        assert hive.tokens.token_created == issued

    @pytest.mark.parametrize("value", ["garbage", None, ""])
    def test_unusable_fourth_value_leaves_token_created_alone(self, value):
        hive = _make_hive()
        before = datetime(2025, 6, 1, tzinfo=UTC)
        hive.tokens.token_created = before
        hive._apply_device_data(["group", "key", "password", value])
        assert hive.tokens.token_created == before
        assert hive.auth.device_key == "key"


class TestApplyConfig:
    """Tests for Hive._apply_config."""

    def test_config_without_tokens_raises_before_anything_is_applied(self):
        hive = _make_hive()
        config = {
            "username": "real@user.com",
            "password": "secret",  # pragma: allowlist secret
            "device_data": ["group", "key", "password"],
        }
        with pytest.raises(HiveReauthRequired):
            hive._apply_config(config)
        assert hive.auth.username == "user@example.com"
        assert hive.auth.password == "pw"
        assert hive.auth.device_key is None
        assert hive.tokens.token_data == {}

    def test_tokens_are_stored_without_resetting_token_created(self):
        hive = _make_hive()
        hive._apply_config({"tokens": _TOKENS})
        assert hive.tokens.token_data == _TOKENS
        assert hive.tokens.token_created == _EPOCH

    def test_tokens_go_through_update_tokens_without_updating_the_expiry(self):
        hive = _make_hive()
        hive.update_tokens = MagicMock()
        hive._apply_config({"tokens": _TOKENS})
        hive.update_tokens.assert_called_once_with(_TOKENS, update_expiry_time=False)

    def test_username_and_password_update_auth(self):
        hive = _make_hive()
        hive._apply_config(
            {
                "tokens": _TOKENS,
                "username": "real@user.com",
                "password": "secret",  # pragma: allowlist secret
            }
        )
        assert hive.auth.username == "real@user.com"
        assert hive.auth.password == "secret"  # pragma: allowlist secret

    def test_credentials_are_kept_when_absent(self):
        hive = _make_hive()
        hive._apply_config({"tokens": _TOKENS})
        assert hive.auth.username == "user@example.com"
        assert hive.auth.password == "pw"
        assert hive.auth.device_key is None

    def test_device_data_is_applied(self):
        hive = _make_hive()
        issued = datetime(2026, 1, 1, 12, tzinfo=UTC)
        hive._apply_config(
            {
                "tokens": _TOKENS,
                "device_data": ["group", "key", "password", issued.isoformat()],
            }
        )
        assert hive.auth.device_group_key == "group"
        assert hive.auth.device_key == "key"
        assert hive.auth.device_password == "password"  # pragma: allowlist secret
        assert hive.tokens.token_created == issued

    def test_short_device_data_raises(self):
        hive = _make_hive()
        with pytest.raises(HiveReauthRequired):
            hive._apply_config({"tokens": _TOKENS, "device_data": ["group"]})


class TestStartSession:
    """Tests for Hive.start_session."""

    async def test_file_mode_from_the_constructor_with_an_empty_config(self):
        hive = Hive(username="use@file.com", password="")
        result = await hive.start_session({})
        assert hive.config.file is True
        assert set(result) == {"parent", "climate", "water_heater"}
        assert result is hive.device_list

    async def test_file_mode_without_any_config(self):
        hive = Hive(username="use@file.com", password="")
        result = await hive.start_session()
        assert hive.config.file is True
        assert len(result["climate"]) == 2

    async def test_file_mode_from_the_config_username(self):
        hive = Hive()
        result = await hive.start_session({"username": "use@file.com"})
        assert hive.config.file is True
        assert result["parent"][0].hive_name == "Hub"

    async def test_file_mode_needs_no_tokens(self):
        hive = Hive(username="use@file.com", password="")
        await hive.start_session({"username": "use@file.com", "device_data": []})
        assert hive.tokens.token_data == {}
        assert hive.auth.device_key is None

    async def test_scan_interval_option_is_applied(self):
        hive = _make_hive()
        await hive.start_session({"tokens": _TOKENS, "options": {"scan_interval": 300}})
        assert hive.config.scan_interval == timedelta(seconds=300)

    async def test_scan_interval_option_is_applied_in_file_mode_too(self):
        hive = Hive(username="use@file.com", password="")
        await hive.start_session({"options": {"scan_interval": 45}})
        assert hive.config.scan_interval == timedelta(seconds=45)

    async def test_scan_interval_below_the_minimum_is_raised_to_it(self):
        hive = _make_hive()
        await hive.start_session({"tokens": _TOKENS, "options": {"scan_interval": 1}})
        assert hive.config.scan_interval == MIN_SCAN_INTERVAL

    @pytest.mark.parametrize("options", [None, {}, {"other": 1}])
    async def test_options_without_a_scan_interval_keep_the_default(self, options):
        hive = _make_hive()
        await hive.start_session({"tokens": _TOKENS, "options": options})
        assert hive.config.scan_interval == DEFAULT_SCAN_INTERVAL

    async def test_live_config_without_tokens_raises_before_polling(self):
        hive = _make_hive()
        with pytest.raises(HiveReauthRequired):
            await hive.start_session({"username": "real@user.com"})
        hive.api.get_all.assert_not_awaited()
        assert hive.data.products == {}
        assert hive.device_list == {}

    @pytest.mark.parametrize("config", [None, {}])
    async def test_live_mode_without_a_config_raises_before_polling(self, config):
        hive = _make_hive()
        with pytest.raises(HiveReauthRequired, match="no tokens"):
            await hive.start_session(config)
        hive.api.get_all.assert_not_awaited()

    async def test_tokens_credentials_and_device_data_are_applied(self):
        hive = _make_hive()
        await hive.start_session(
            {
                "username": "real@user.com",
                "password": "secret",  # pragma: allowlist secret
                "tokens": _TOKENS,
                "device_data": ["group", "key", "password"],
            }
        )
        assert hive.tokens.token_data == _TOKENS
        assert hive.tokens.token_created == _EPOCH
        assert hive.auth.username == "real@user.com"
        assert hive.auth.password == "secret"  # pragma: allowlist secret
        assert hive.auth.device_key == "key"

    async def test_live_poll_fetches_all_nodes_with_fresh_tokens(self):
        hive = _make_hive()
        await hive.start_session({"tokens": _TOKENS})
        hive.hive_refresh_tokens.assert_awaited_once_with()
        hive.api.get_all.assert_awaited_once_with()
        assert set(hive.data.products) == {"heat-1", "hw-1", "trvc-1"}
        assert set(hive.data.devices) == {"hub-1", "thermo-1", "trv-1"}
        assert hive.data.user == {"temperatureUnit": "C"}

    async def test_success_returns_the_device_list(self):
        hive = _make_hive()
        hive.config.last_update = _EPOCH
        result = await hive.start_session({"tokens": _TOKENS})
        assert result is hive.device_list
        assert [d.hive_id for d in result["parent"]] == ["hub-1"]
        assert [d.hive_id for d in result["climate"]] == ["heat-1", "trvc-1"]
        assert [d.hive_id for d in result["water_heater"]] == ["hw-1"]
        assert datetime.now(UTC) - hive.config.last_update < timedelta(seconds=5)

    @pytest.mark.parametrize("error", [HiveConnectionError, HiveApiError])
    async def test_errors_from_the_api_propagate_unchanged(self, error):
        hive = _make_hive()
        hive.api.get_all = AsyncMock(side_effect=error("boom"))
        with pytest.raises(error, match="boom"):
            await hive.start_session({"tokens": _TOKENS})
        assert hive.data.products == {}
        assert hive.device_list == {}

    async def test_reauth_required_from_the_poll_propagates(self):
        hive = _make_hive()
        hive._poll = AsyncMock(side_effect=HiveReauthRequired("SMS code needed"))
        with pytest.raises(HiveReauthRequired, match="SMS code needed"):
            await hive.start_session({"tokens": _TOKENS})
        assert hive.device_list == {}

    @pytest.mark.parametrize(
        "nodes",
        [
            {},
            {"user": {"temperatureUnit": "C"}},
            {"products": [_HEATING], "devices": []},
            {"products": [], "devices": [_HUB]},
        ],
    )
    async def test_a_poll_without_products_or_devices_raises_api_error(self, nodes):
        hive = _make_hive()
        hive.api.get_all = AsyncMock(return_value=nodes)
        with pytest.raises(HiveApiError):
            await hive.start_session({"tokens": _TOKENS})
        assert hive.device_list == {}

    async def test_the_logged_config_is_sanitized(self, caplog):
        hive = _make_hive()
        config = {
            "username": "real@user.com",
            "password": "correct horse battery",  # pragma: allowlist secret
            "tokens": _TOKENS,
        }
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            await hive.start_session(config)
        assert "start_session - Config" in caplog.text
        assert "real@user.com" in caplog.text
        assert "correct horse battery" not in caplog.text
        assert "refresh-token" not in caplog.text


class TestCreateDevices:
    """Tests for Hive.create_devices."""

    def test_device_list_has_exactly_the_three_entity_types(self):
        hive = _make_hive(
            products={"heat-1": _HEATING, "hw-1": _HOTWATER}, devices=_DEVICES
        )
        result = hive.create_devices()
        assert list(result) == ["parent", "climate", "water_heater"]
        assert result is hive.device_list
        for devices in result.values():
            assert all(isinstance(device, Device) for device in devices)

    def test_hub_becomes_the_parent_device(self):
        hive = _make_hive(devices=_DEVICES)
        result = hive.create_devices()
        assert hive.hub_id == "hub-1"
        parent = result["parent"][0]
        assert parent.hive_id == "hub-1"
        assert parent.hive_type == "hub"
        assert parent.ha_type == "parent"
        assert parent.device_id == "hub-1"
        assert parent.hive_name == "Hive Hub"
        assert parent.parent_device == "hub-1"
        assert parent.device_data["model"] == "HUB"
        assert parent.temperature_unit is None
        assert result["climate"] == []
        assert result["water_heater"] == []

    def test_missing_hub_logs_a_warning(self, caplog):
        hive = _make_hive(
            products={"heat-1": _HEATING}, devices={"thermo-1": _THERMOSTAT}
        )
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            result = hive.create_devices()
        assert "No hub found among the Hive devices" in caplog.text
        assert result["parent"] == []
        assert hive.hub_id is None
        assert result["climate"][0].parent_device is None

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
        assert climate.device_name == "Hallway"
        assert climate.ha_name == "Hallway"
        assert climate.device_data == _THERMOSTAT["props"]
        assert climate.parent_device == "hub-1"
        assert climate.temperature_unit == "C"
        assert climate.status is None

    def test_hotwater_product_becomes_a_water_heater_without_a_unit(self):
        hive = _make_hive(
            products={"hw-1": _HOTWATER},
            devices=_DEVICES,
            user={"temperatureUnit": "C"},
        )
        heater = hive.create_devices()["water_heater"][0]
        assert heater.hive_id == "hw-1"
        assert heater.hive_type == "hotwater"
        assert heater.ha_type == "water_heater"
        assert heater.device_id == "thermo-1"
        assert heater.hive_name == "Hallway"
        assert heater.temperature_unit is None

    def test_temperature_unit_is_none_when_the_user_has_none(self):
        hive = _make_hive(products={"heat-1": _HEATING}, devices=_DEVICES)
        assert hive.create_devices()["climate"][0].temperature_unit is None

    def test_trv_control_uses_its_trv(self):
        hive = _make_hive(
            products={"trvc-1": _TRVCONTROL},
            devices=_DEVICES,
            user={"temperatureUnit": "F"},
        )
        climate = hive.create_devices()["climate"][0]
        assert climate.hive_id == "trvc-1"
        assert climate.hive_type == "trvcontrol"
        assert climate.device_id == "trv-1"
        assert climate.hive_name == "Bedroom"
        assert climate.device_data == {"model": "TRV001"}
        assert climate.temperature_unit == "F"

    def test_receiver_is_renamed_heating(self):
        receiver = {**_THERMOSTAT, "id": "rec-1", "state": {"name": "Receiver"}}
        hive = _make_hive(
            products={"heat-1": _HEATING}, devices={"hub-1": _HUB, "rec-1": receiver}
        )
        climate = hive.create_devices()["climate"][0]
        assert climate.hive_name == "Heating"
        assert climate.device_name == "Heating"
        assert climate.ha_name == "Heating"

    def test_product_with_an_error_is_skipped_with_a_warning(self, caplog):
        bad = {"id": "bad", "type": "heating", "error": "not found"}
        hive = _make_hive(products={"bad": bad, "heat-1": _HEATING}, devices=_DEVICES)
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            result = hive.create_devices()
        assert "Skipping product bad due to error: not found" in caplog.text
        assert [d.hive_id for d in result["climate"]] == ["heat-1"]

    def test_unsupported_product_types_are_skipped(self, caplog):
        lamp = {"id": "lamp", "type": "warmwhitelight", "state": {"name": "Lamp"}}
        hive = _make_hive(
            products={"lamp": lamp, "odd": {"id": "odd"}}, devices=_DEVICES
        )
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            result = hive.create_devices()
        assert result["climate"] == []
        assert result["water_heater"] == []
        assert "Skipping unsupported product lamp (type: warmwhitelight)" in caplog.text
        assert "Skipping unsupported product odd (type: Unknown)" in caplog.text

    def test_group_hotwater_product_is_skipped(self, caplog):
        group = {**_HOTWATER, "id": "g1", "isGroup": True}
        hive = _make_hive(products={"g1": group}, devices=_DEVICES)
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            result = hive.create_devices()
        assert result["water_heater"] == []
        assert "Skipping group product g1 (type: hotwater)" in caplog.text

    def test_group_heating_product_is_kept(self):
        group = {**_HEATING, "id": "g1", "isGroup": True}
        hive = _make_hive(products={"g1": group}, devices=_DEVICES)
        assert [d.hive_id for d in hive.create_devices()["climate"]] == ["g1"]

    def test_discovery_counts_are_logged(self, caplog):
        hive = _make_hive(
            products={"heat-1": _HEATING, "hw-1": _HOTWATER, "trvc-1": _TRVCONTROL},
            devices=_DEVICES,
        )
        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            hive.create_devices()
        assert (
            "Device discovery completed: 1 parent, 2 climate, 1 water_heater"
            in caplog.text
        )

    def test_device_list_is_rebuilt_on_each_call(self):
        hive = _make_hive(products={"heat-1": _HEATING}, devices=_DEVICES)
        first = hive.create_devices()
        second = hive.create_devices()
        assert second is not first
        assert second is hive.device_list
        assert len(second["parent"]) == 1
        assert len(second["climate"]) == 1


class TestAddDevice:
    """Tests for Hive._add_device."""

    def _hive(self, **kwargs):
        hive = _make_hive(**kwargs)
        hive.device_list = {"parent": [], "climate": [], "water_heater": []}
        hive.hub_id = "hub-1"
        return hive

    def test_returns_the_device_and_appends_it(self):
        hive = self._hive(devices=_DEVICES)
        device = hive._add_device("climate", _HEATING, temperature_unit="C")
        assert isinstance(device, Device)
        assert hive.device_list["climate"] == [device]
        assert hive.device_list["parent"] == []
        assert device.hive_id == "heat-1"
        assert device.hive_type == "heating"
        assert device.ha_type == "climate"
        assert device.device_id == "thermo-1"
        assert device.hive_name == "Hallway"
        assert device.device_name == "Hallway"
        assert device.ha_name == "Hallway"
        assert device.device_data == _THERMOSTAT["props"]
        assert device.parent_device == "hub-1"
        assert device.temperature_unit == "C"

    def test_temperature_unit_defaults_to_none(self):
        hive = self._hive(devices=_DEVICES)
        assert hive._add_device("water_heater", _HOTWATER).temperature_unit is None

    def test_device_data_falls_back_to_the_product_props(self):
        trv = {"id": "trv-1", "type": "trv", "state": {"name": "Bedroom"}}
        hive = self._hive(devices={"trv-1": trv})
        device = hive._add_device("climate", _TRVCONTROL)
        assert device.device_data == {"trvs": ["trv-1"]}

    def test_device_data_is_empty_when_nobody_has_props(self):
        hub = {"id": "hub-1", "type": "hub", "state": {"name": "Hub"}}
        hive = self._hive(devices={"hub-1": hub})
        device = hive._add_device("parent", hub)
        assert device.device_data == {}

    def test_product_without_an_id_gets_an_empty_hive_id(self):
        hive = self._hive(devices=_DEVICES)
        device = hive._add_device(
            "climate", {"type": "heating", "props": {"zone": "zone-A"}}
        )
        assert device.hive_id == ""
        assert device.device_id == "thermo-1"

    def test_missing_device_is_skipped_with_a_warning(self, caplog):
        hive = self._hive(devices=_DEVICES)
        product = {"id": "trvc-9", "type": "trvcontrol", "props": {"trvs": ["missing"]}}
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            assert hive._add_device("climate", product) is None
        assert "Skipping trvcontrol trvc-9: KeyError('missing')" in caplog.text
        assert hive.device_list["climate"] == []

    def test_missing_state_name_is_skipped(self, caplog):
        nameless = {**_THERMOSTAT, "state": {}}
        hive = self._hive(devices={"thermo-1": nameless})
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            assert hive._add_device("climate", _HEATING) is None
        assert "Skipping heating heat-1: KeyError('name')" in caplog.text

    def test_product_without_type_or_id_is_skipped(self, caplog):
        hive = self._hive()
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            assert hive._add_device("climate", {}) is None
        assert "Skipping unknown ?: KeyError('type')" in caplog.text

    def test_trv_control_without_trvs_is_skipped(self, caplog):
        hive = self._hive(devices=_DEVICES)
        product = {"id": "trvc-1", "type": "trvcontrol", "props": {"trvs": []}}
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            assert hive._add_device("climate", product) is None
        assert "Skipping trvcontrol trvc-1: IndexError(" in caplog.text
        assert hive.device_list["climate"] == []

    def test_product_with_null_props_is_skipped(self, caplog):
        hive = self._hive(devices=_DEVICES)
        product = {"id": "heat-9", "type": "heating", "props": None}
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            assert hive._add_device("climate", product) is None
        assert "Skipping heating heat-9: AttributeError(" in caplog.text
        assert hive.device_list["climate"] == []

    def test_state_that_is_not_a_dict_is_skipped(self, caplog):
        broken = {**_THERMOSTAT, "state": ["not", "a", "dict"]}
        hive = self._hive(devices={"thermo-1": broken})
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            assert hive._add_device("climate", _HEATING) is None
        assert "Skipping heating heat-1: TypeError(" in caplog.text

    def test_trv_control_props_that_are_not_a_dict_are_skipped(self, caplog):
        hive = self._hive(devices=_DEVICES)
        product = {"id": "trvc-1", "type": "trvcontrol", "props": ["trv-1"]}
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            assert hive._add_device("climate", product) is None
        assert "Skipping trvcontrol trvc-1: TypeError(" in caplog.text
        assert hive.device_list["climate"] == []
