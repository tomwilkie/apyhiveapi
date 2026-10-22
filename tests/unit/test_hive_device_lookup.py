"""Unit tests for the device bookkeeping on Hive."""

# pylint: disable=protected-access

import logging

import pytest

from apyhiveapi.hive import Hive


def _make_hive(products=None, devices=None):
    """Return a Hive whose data holds the given products and devices."""
    hive = Hive(username="use@file.com", password="")
    hive.data.products = products or {}
    hive.data.devices = devices or {}
    return hive


class TestDeviceRecovered:
    """Tests for Hive.device_recovered."""

    def test_removes_from_error_list(self):
        hive = _make_hive()
        hive.config.error_list["dev-1"] = "2026-01-01"
        hive.device_recovered("dev-1")
        assert "dev-1" not in hive.config.error_list

    def test_no_op_when_not_in_error_list(self):
        hive = _make_hive()
        hive.device_recovered("not-there")  # must not raise
        assert not hive.config.error_list

    def test_only_target_removed(self):
        hive = _make_hive()
        hive.config.error_list["dev-1"] = "2026-01-01"
        hive.config.error_list["dev-2"] = "2026-01-01"
        hive.device_recovered("dev-1")
        assert "dev-1" not in hive.config.error_list
        assert "dev-2" in hive.config.error_list


class TestDeviceName:
    """Tests for Hive._device_name."""

    def test_found_in_products(self):
        hive = _make_hive(products={"p1": {"state": {"name": "Hallway"}}})
        assert hive._device_name("p1") == "Hallway"

    def test_found_in_devices(self):
        hive = _make_hive(devices={"d1": {"state": {"name": "Thermostat"}}})
        assert hive._device_name("d1") == "Thermostat"

    def test_product_takes_priority_over_device(self):
        hive = _make_hive(
            products={"x1": {"state": {"name": "ProductName"}}},
            devices={"x1": {"state": {"name": "DeviceName"}}},
        )
        assert hive._device_name("x1") == "ProductName"

    def test_not_found_returns_id_and_warns(self, caplog):
        hive = _make_hive()
        with caplog.at_level(logging.WARNING, logger="apyhiveapi.hive"):
            assert hive._device_name("unknown-id") == "unknown-id"
        assert "unknown-id" in caplog.text


class TestReportDeviceOffline:
    """Tests for Hive.report_device_offline."""

    def test_first_report_logs_and_records(self, caplog):
        hive = _make_hive(products={"d1": {"state": {"name": "Device"}}})
        with caplog.at_level(logging.WARNING, logger="apyhiveapi.hive"):
            hive.report_device_offline("d1")
        assert "d1" in hive.config.error_list
        assert "Device offline could not update entity - Device" in caplog.text

    def test_repeat_report_is_silent(self, caplog):
        hive = _make_hive(products={"d1": {"state": {"name": "Device"}}})
        hive.report_device_offline("d1")
        first = hive.config.error_list["d1"]
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="apyhiveapi.hive"):
            hive.report_device_offline("d1")
        assert hive.config.error_list["d1"] is first
        assert not caplog.records


class TestDeviceForProduct:
    """Tests for Hive._device_for_product."""

    def test_trvcontrol_no_trvs_raises_key_error(self):
        hive = _make_hive()
        product = {"id": "trv-1", "type": "trvcontrol", "props": {"trvs": []}}
        with pytest.raises(KeyError):
            hive._device_for_product(product)

    def test_trvcontrol_with_trv_returns_device(self):
        devices = {
            "trv-device-1": {
                "id": "trv-device-1",
                "type": "trv",
                "state": {"name": "TRV"},
            },
        }
        hive = _make_hive(devices=devices)
        product = {
            "id": "trv-1",
            "type": "trvcontrol",
            "props": {"trvs": ["trv-device-1"]},
        }
        assert hive._device_for_product(product)["id"] == "trv-device-1"

    def test_heating_matches_by_zone(self):
        devices = {
            "thermo-1": {
                "id": "thermo-1",
                "type": "thermostatui",
                "state": {"name": "Thermostat"},
                "props": {"zone": "zone-A"},
            },
        }
        hive = _make_hive(devices=devices)
        product = {"id": "heating-1", "type": "heating", "props": {"zone": "zone-A"}}
        assert hive._device_for_product(product)["id"] == "thermo-1"

    def test_hotwater_matches_by_zone(self):
        devices = {
            "thermo-1": {
                "id": "thermo-1",
                "type": "thermostatui",
                "state": {"name": "Thermostat"},
                "props": {"zone": "zone-A"},
            },
        }
        hive = _make_hive(devices=devices)
        product = {"id": "hw-1", "type": "hotwater", "props": {"zone": "zone-A"}}
        assert hive._device_for_product(product)["id"] == "thermo-1"

    def test_zone_mismatch_keeps_product_as_device(self):
        hive = _make_hive(
            devices={"thermo-1": {"type": "thermostatui", "props": {"zone": "zone-B"}}}
        )
        product = {"type": "heating", "id": "prod-1", "props": {"zone": "zone-A"}}
        assert hive._device_for_product(product) is product

    def test_trv_without_zone_does_not_log_warning(self, caplog):
        hive = _make_hive(devices={"trv-1": {"type": "trv", "props": {"online": True}}})
        product = {"type": "heating", "id": "prod-1", "props": {"zone": "zone-A"}}
        with caplog.at_level(logging.WARNING, logger="apyhiveapi.hive"):
            assert hive._device_for_product(product) is product
        assert not caplog.records

    def test_hub_is_its_own_device(self):
        devices = {"hub-1": {"id": "hub-1", "type": "hub", "state": {"name": "Hub"}}}
        hive = _make_hive(devices=devices)
        assert hive._device_for_product({"id": "hub-1", "type": "hub"})["id"] == "hub-1"

    def test_missing_device_raises_key_error(self):
        hive = _make_hive()
        with pytest.raises(KeyError):
            hive._device_for_product({"id": "hub-1", "type": "hub"})
