"""Unit tests for session start and device discovery on Hive."""

# pylint: disable=protected-access

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

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
_HUB_WITHOUT_PROPS = {"id": "hub-1", "type": "hub", "state": {"name": "Hub"}}
_TRV_WITHOUT_PROPS = {"id": "trv-1", "type": "trv", "state": {"name": "Bedroom"}}


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


class TestApplyDeviceData:
    """Tests for Hive._apply_device_data."""

    @pytest.mark.parametrize(
        "device_data",
        [
            pytest.param([], id="empty"),
            pytest.param(["group"], id="group-only"),
            pytest.param(["group", "key"], id="no-password"),
        ],
    )
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

    def test_a_saved_token_issue_time_is_ignored(self):
        hive = _make_hive()
        hive._apply_device_data(
            ["group", "key", "password", "2026-01-01T12:00:00+00:00"]
        )
        assert hive.auth.device_key == "key"
        assert hive.tokens.token_created == _EPOCH


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

    def test_tokens_only_config_stores_tokens_and_keeps_credentials(self):
        hive = _make_hive()
        hive._apply_config({"tokens": _TOKENS})
        assert hive.tokens.token_data == _TOKENS
        assert hive.tokens.token_created == _EPOCH
        assert hive.auth.username == "user@example.com"
        assert hive.auth.password == "pw"
        assert hive.auth.device_key is None

    def test_full_config_applies_credentials_and_device_data(self):
        hive = _make_hive()
        hive._apply_config(
            {
                "tokens": _TOKENS,
                "username": "real@user.com",
                "password": "secret",  # pragma: allowlist secret
                "device_data": ["group", "key", "password"],
            }
        )
        assert hive.auth.username == "real@user.com"
        assert hive.auth.password == "secret"  # pragma: allowlist secret
        assert hive.auth.device_group_key == "group"
        assert hive.auth.device_key == "key"
        assert hive.auth.device_password == "password"  # pragma: allowlist secret


class TestStartSession:
    """Tests for Hive.start_session."""

    @pytest.mark.parametrize(
        ("ctor_username", "config"),
        [
            pytest.param("use@file.com", {}, id="constructor-empty-config"),
            pytest.param("use@file.com", None, id="constructor-no-config"),
            pytest.param(None, {"username": "use@file.com"}, id="config-username"),
            pytest.param(
                "use@file.com",
                {"username": "use@file.com", "device_data": []},
                id="config-username-empty-device-data",
            ),
        ],
    )
    async def test_file_mode_needs_no_tokens(self, ctor_username, config):
        hive = Hive(username=ctor_username, password="")
        result = await hive.start_session(config)
        assert hive.config.file is True
        assert result is hive.device_list
        assert set(result) == {"parent", "climate", "water_heater"}
        assert hive.tokens.token_data == {}
        assert hive.auth.device_key is None

    async def test_debug_log_shows_no_part_of_the_credentials(self, caplog):
        hive = _make_hive()
        config = {
            "username": "user@example.com",
            "password": "PWHEADxxxxPWTAIL",  # pragma: allowlist secret
            "device_data": ["GKHEADxxxxGKTAIL", "DKHEADxxxxDKTAIL", "DPHEADxxxxDPTAIL"],
            "tokens": {
                "token": "IDHEADxxxxxxxxxxIDTAIL",
                "refreshToken": "RFHEADxxxxxxxxxxRFTAIL",
                "accessToken": "ACHEADxxxxxxxxxxACTAIL",
            },
        }
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            await hive.start_session(config)
        assert "start_session - Config" in caplog.text
        assert "user@example.com" in caplog.text
        for value in ("PW", "GK", "DK", "DP", "ID", "RF", "AC"):
            assert f"{value}HEAD" not in caplog.text
            assert f"{value}TAIL" not in caplog.text

    @pytest.mark.parametrize(
        ("scan_interval", "expected"),
        [
            pytest.param(300, timedelta(seconds=300), id="applied"),
            pytest.param(1, MIN_SCAN_INTERVAL, id="raised-to-the-minimum"),
        ],
    )
    async def test_scan_interval_option_is_applied(self, scan_interval, expected):
        hive = _make_hive()
        await hive.start_session(
            {"tokens": _TOKENS, "options": {"scan_interval": scan_interval}}
        )
        assert hive.config.scan_interval == expected

    @pytest.mark.parametrize(
        "options",
        [
            pytest.param(None, id="none"),
            pytest.param({}, id="empty"),
            pytest.param({"other": 1}, id="other-options-only"),
        ],
    )
    async def test_options_without_a_scan_interval_keep_the_default(self, options):
        hive = _make_hive()
        await hive.start_session({"tokens": _TOKENS, "options": options})
        assert hive.config.scan_interval == DEFAULT_SCAN_INTERVAL

    @pytest.mark.parametrize(
        "config",
        [
            pytest.param(None, id="none"),
            pytest.param({}, id="empty"),
            pytest.param({"username": "real@user.com"}, id="username-only"),
        ],
    )
    async def test_live_mode_without_tokens_raises_before_polling(self, config):
        hive = _make_hive()
        with pytest.raises(HiveReauthRequired, match="no tokens"):
            await hive.start_session(config)
        hive.api.get_all.assert_not_awaited()
        assert hive.data.products == {}
        assert hive.device_list == {}

    async def test_live_start_polls_once_and_returns_the_device_list(self):
        hive = _make_hive()
        hive.config.last_update = _EPOCH
        result = await hive.start_session({"tokens": _TOKENS})
        hive.hive_refresh_tokens.assert_awaited_once_with()
        hive.api.get_all.assert_awaited_once_with()
        assert set(hive.data.products) == {"heat-1", "hw-1", "trvc-1"}
        assert set(hive.data.devices) == {"hub-1", "thermo-1", "trv-1"}
        assert hive.data.user == {"temperatureUnit": "C"}
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
        hive.hive_refresh_tokens.side_effect = HiveReauthRequired("SMS code needed")
        with pytest.raises(HiveReauthRequired, match="SMS code needed"):
            await hive.start_session({"tokens": _TOKENS})
        assert hive.device_list == {}

    @pytest.mark.parametrize(
        "nodes",
        [
            pytest.param({}, id="empty"),
            pytest.param({"user": {"temperatureUnit": "C"}}, id="user-only"),
            pytest.param({"products": [_HEATING], "devices": []}, id="no-devices"),
            pytest.param({"products": [], "devices": [_HUB]}, id="no-products"),
        ],
    )
    async def test_a_poll_without_products_or_devices_is_transient(self, nodes):
        hive = _make_hive()
        hive.api.get_all = AsyncMock(return_value=nodes)
        with pytest.raises(HiveConnectionError, match="no products or devices"):
            await hive.start_session({"tokens": _TOKENS})
        assert hive.device_list == {}


class TestCreateDevices:
    """Tests for Hive.create_devices."""

    def test_device_list_has_exactly_the_three_entity_types(self, caplog):
        hive = _make_hive(
            products={"heat-1": _HEATING, "hw-1": _HOTWATER}, devices=_DEVICES
        )
        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            result = hive.create_devices()
        assert list(result) == ["parent", "climate", "water_heater"]
        assert result is hive.device_list
        assert [type(device) for device in result["parent"]] == [Device]
        assert [type(device) for device in result["climate"]] == [Device]
        assert [type(device) for device in result["water_heater"]] == [Device]
        assert (
            "Device discovery completed: 1 parent, 1 climate, 1 water_heater"
            in caplog.text
        )

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
        assert parent.parent_device is None
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

    def test_hub_that_cannot_be_added_is_not_a_parent(self, caplog):
        hub = {"id": "hub-1", "type": "hub", "props": {"online": True}}
        hive = _make_hive(
            products={"heat-1": _HEATING},
            devices={"hub-1": hub, "thermo-1": _THERMOSTAT},
        )
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            result = hive.create_devices()
        assert "Skipping hub hub-1" in caplog.text
        assert result["parent"] == []
        assert hive.hub_id is None
        assert result["climate"][0].parent_device is None

    def test_each_call_rebuilds_the_list_and_hub_id(self):
        hive = _make_hive(products={"heat-1": _HEATING}, devices=_DEVICES)
        first = hive.create_devices()
        assert hive.hub_id == "hub-1"
        hive.data.devices = {"thermo-1": _THERMOSTAT}
        second = hive.create_devices()
        assert second is not first
        assert second is hive.device_list
        assert hive.hub_id is None
        assert second["climate"][0].parent_device is None

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

    def test_group_heating_is_kept_and_group_hotwater_is_skipped(self, caplog):
        group_heating = {**_HEATING, "id": "g-heat", "isGroup": True}
        group_hotwater = {**_HOTWATER, "id": "g-hw", "isGroup": True}
        hive = _make_hive(
            products={"g-heat": group_heating, "g-hw": group_hotwater},
            devices=_DEVICES,
        )
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            result = hive.create_devices()
        assert [d.hive_id for d in result["climate"]] == ["g-heat"]
        assert result["water_heater"] == []
        assert "Skipping group product g-hw (type: hotwater)" in caplog.text


class TestAddDevice:
    """Tests for Hive._add_device."""

    def _hive(self, **kwargs):
        hive = _make_hive(**kwargs)
        hive.device_list = {"parent": [], "climate": [], "water_heater": []}
        hive.hub_id = "hub-1"
        return hive

    @pytest.mark.parametrize(
        ("product", "expected_name"),
        [
            pytest.param(
                {**_HOTWATER, "state": {"name": "Hot Water"}}, "Hot Water", id="named"
            ),
            pytest.param(_HOTWATER, "Hallway", id="no-state"),
            pytest.param(
                {**_HOTWATER, "state": {}}, "Hallway", id="state-without-name"
            ),
        ],
    )
    def test_entity_is_named_after_the_product_and_device_after_the_hardware(
        self, product, expected_name
    ):
        hive = self._hive(devices=_DEVICES)
        device = hive._add_device("water_heater", product)
        assert device.hive_name == expected_name
        assert device.ha_name == expected_name
        assert device.device_name == "Hallway"
        assert device.device_id == "thermo-1"

    def test_receiver_is_shown_as_heating_for_product_and_device(self):
        receiver = {**_THERMOSTAT, "state": {"name": "Receiver"}}
        hive = self._hive(devices={**_DEVICES, "thermo-1": receiver})
        product = {**_HEATING, "state": {"name": "Receiver"}}
        device = hive._add_device("climate", product)
        assert device.hive_name == device.ha_name == device.device_name == "Heating"

    @pytest.mark.parametrize(
        ("entity_type", "devices", "product", "expected_device_data"),
        [
            pytest.param(
                "climate",
                {"trv-1": _TRV_WITHOUT_PROPS},
                _TRVCONTROL,
                {"trvs": ["trv-1"]},
                id="product-props",
            ),
            pytest.param(
                "parent",
                {"hub-1": _HUB_WITHOUT_PROPS},
                _HUB_WITHOUT_PROPS,
                {},
                id="nobody-has-props",
            ),
        ],
    )
    def test_device_data_falls_back_to_the_product_props(
        self, entity_type, devices, product, expected_device_data
    ):
        hive = self._hive(devices=devices)
        device = hive._add_device(entity_type, product)
        assert device.device_data == expected_device_data

    @pytest.mark.parametrize(
        ("devices", "product", "log_fragment"),
        [
            pytest.param(
                _DEVICES,
                {"id": "trvc-9", "type": "trvcontrol", "props": {"trvs": ["missing"]}},
                "Skipping trvcontrol trvc-9: KeyError('missing')",
                id="missing-trv",
            ),
            pytest.param({}, {}, "Skipping unknown ?: KeyError('type')", id="no-type"),
            pytest.param(
                _DEVICES,
                {"id": "trvc-1", "type": "trvcontrol", "props": {"trvs": []}},
                "Skipping trvcontrol trvc-1: IndexError(",
                id="no-trvs",
            ),
            pytest.param(
                _DEVICES,
                {"id": "heat-9", "type": "heating", "props": None},
                "Skipping heating heat-9: AttributeError(",
                id="null-props",
            ),
            pytest.param(
                {"thermo-1": {**_THERMOSTAT, "state": ["not", "a", "dict"]}},
                _HEATING,
                "Skipping heating heat-1: TypeError(",
                id="state-not-a-dict",
            ),
        ],
    )
    def test_malformed_product_is_skipped_with_a_warning(
        self, devices, product, log_fragment, caplog
    ):
        hive = self._hive(devices=devices)
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            assert hive._add_device("climate", product) is None
        assert log_fragment in caplog.text
        assert hive.device_list["climate"] == []
