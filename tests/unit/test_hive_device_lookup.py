"""Unit tests for the device lookup and offline bookkeeping on Hive."""

# pylint: disable=protected-access

import logging

import pytest

from apyhiveapi.hive import Hive

_LOGGER_NAME = "apyhiveapi.hive"
_HUB = {"id": "hub-1", "type": "hub", "state": {"name": "Hub"}}
_THERMOSTAT = {
    "id": "thermo-1",
    "type": "thermostatui",
    "state": {"name": "Thermostat"},
    "props": {"zone": "zone-A"},
}
_SECOND_THERMOSTAT = {**_THERMOSTAT, "id": "thermo-2"}
_TRV = {
    "id": "trv-1",
    "type": "trv",
    "state": {"name": "TRV"},
    "props": {"zone": "zone-A"},
}
_BOILER = {
    "id": "boiler-1",
    "type": "boilermodule",
    "state": {"name": "Boiler"},
    "props": {"zone": "zone-A"},
}
_DEVICE_NAME = {"state": {"name": "DeviceName"}}


def _make_hive(products=None, devices=None):
    """Return a Hive whose data holds the given products and devices."""
    hive = Hive(username="use@file.com", password="")
    hive.data.products = products or {}
    hive.data.devices = devices or {}
    return hive


class TestDeviceForProduct:
    """Tests for Hive._device_for_product."""

    @pytest.mark.parametrize(
        ("devices", "product_type", "expected"),
        [
            pytest.param(
                {"hub-1": _HUB, "thermo-1": _THERMOSTAT},
                "heating",
                _THERMOSTAT,
                id="thermostat-heating",
            ),
            pytest.param(
                {"hub-1": _HUB, "thermo-1": _THERMOSTAT},
                "hotwater",
                _THERMOSTAT,
                id="thermostat-hotwater",
            ),
            pytest.param({"hub-1": _HUB, "trv-1": _TRV}, "heating", _TRV, id="trv"),
            pytest.param(
                {"boiler-1": _BOILER, "thermo-1": _THERMOSTAT},
                "heating",
                _THERMOSTAT,
                id="boiler-ignored",
            ),
            pytest.param(
                {"thermo-1": _THERMOSTAT, "thermo-2": _SECOND_THERMOSTAT},
                "hotwater",
                _THERMOSTAT,
                id="first-wins",
            ),
        ],
    )
    def test_zone_products_use_the_first_thermostat_or_trv_in_their_zone(
        self, devices, product_type, expected
    ):
        hive = _make_hive(devices=devices)
        product = {"id": "p-1", "type": product_type, "props": {"zone": "zone-A"}}
        assert hive._device_for_product(product) is expected

    @pytest.mark.parametrize(
        "devices",
        [
            pytest.param(
                {"thermo-1": {**_THERMOSTAT, "props": {"zone": "zone-Z"}}},
                id="zone-mismatch",
            ),
            pytest.param(
                {"trv-1": {"id": "trv-1", "type": "trv"}}, id="device-without-props"
            ),
        ],
    )
    def test_no_device_in_the_zone_keeps_the_product_as_its_own_device(self, devices):
        hive = _make_hive(devices=devices)
        product = {"id": "p-1", "type": "heating", "props": {"zone": "zone-A"}}
        assert hive._device_for_product(product) is product

    @pytest.mark.parametrize(
        "product",
        [
            pytest.param({"id": "p-1", "type": "hotwater", "props": {}}, id="no-zone"),
            pytest.param(
                {"id": "p-1", "type": "hotwater", "props": {"zone": None}},
                id="zone-none",
            ),
            pytest.param(
                {"id": "p-1", "type": "hotwater", "props": {"zone": ""}},
                id="zone-empty",
            ),
            pytest.param({"id": "p-1", "type": "heating"}, id="no-props"),
        ],
    )
    def test_product_without_a_zone_is_its_own_device(self, product):
        unzoned = {**_THERMOSTAT, "props": {}}
        hive = _make_hive(devices={"thermo-1": unzoned})
        assert hive._device_for_product(product) is product

    def test_trv_control_uses_its_first_trv(self):
        second = {**_TRV, "id": "trv-2"}
        hive = _make_hive(devices={"trv-2": second, "trv-1": _TRV})
        product = {
            "id": "trvc-1",
            "type": "trvcontrol",
            "props": {"trvs": ["trv-1", "trv-2"]},
        }
        assert hive._device_for_product(product) is _TRV

    def test_hub_is_its_own_device(self):
        hive = _make_hive(devices={"hub-1": _HUB})
        assert hive._device_for_product({"id": "hub-1", "type": "hub"}) is _HUB

    @pytest.mark.parametrize(
        ("devices", "product", "exc"),
        [
            pytest.param(
                {"trv-1": _TRV},
                {"id": "trvc-1", "type": "trvcontrol", "props": {"trvs": []}},
                IndexError,
                id="no-trvs",
            ),
            pytest.param(
                {"trv-1": _TRV},
                {"id": "trvc-1", "type": "trvcontrol", "props": {"trvs": ["missing"]}},
                KeyError,
                id="unknown-trv",
            ),
            pytest.param(
                {}, {"id": "hub-1", "type": "hub"}, KeyError, id="missing-device"
            ),
            pytest.param({"hub-1": _HUB}, {"id": "hub-1"}, KeyError, id="no-type"),
            pytest.param(
                {"thermo-1": _THERMOSTAT},
                {"id": "p-1", "type": "heating", "props": None},
                AttributeError,
                id="product-props-none",
            ),
            pytest.param(
                {"thermo-1": {**_THERMOSTAT, "props": None}},
                {"id": "p-1", "type": "heating", "props": {"zone": "zone-A"}},
                AttributeError,
                id="device-props-none",
            ),
        ],
    )
    def test_malformed_data_raises(self, devices, product, exc):
        hive = _make_hive(devices=devices)
        with pytest.raises(exc):
            hive._device_for_product(product)


class TestDeviceName:
    """Tests for Hive._device_name."""

    @pytest.mark.parametrize(
        ("products", "devices", "expected"),
        [
            pytest.param(
                {"x1": {"state": {"name": "Hallway"}}}, {}, "Hallway", id="product-name"
            ),
            pytest.param({}, {"x1": _DEVICE_NAME}, "DeviceName", id="device-name"),
            pytest.param(
                {"x1": {"state": {"name": "ProductName"}}},
                {"x1": _DEVICE_NAME},
                "ProductName",
                id="product-wins",
            ),
            pytest.param(
                {"x1": {"state": {"name": ""}}},
                {"x1": _DEVICE_NAME},
                "DeviceName",
                id="empty-product-name",
            ),
            pytest.param(
                {"x1": {"state": None}},
                {"x1": _DEVICE_NAME},
                "DeviceName",
                id="null-product-state",
            ),
            pytest.param(
                {"x1": {"state": {"name": 42}}}, {}, "42", id="non-string-name"
            ),
        ],
    )
    def test_product_name_wins_unless_empty_or_missing(
        self, products, devices, expected
    ):
        hive = _make_hive(products=products, devices=devices)
        assert hive._device_name("x1") == expected

    @pytest.mark.parametrize(
        "products",
        [
            pytest.param({}, id="unknown-id"),
            pytest.param({"p1": {}}, id="no-state"),
            pytest.param({"p1": {"state": {}}}, id="state-without-name"),
            pytest.param({"p1": None}, id="product-none"),
            pytest.param({"p1": {"state": None}}, id="state-none"),
        ],
    )
    def test_unknown_or_nameless_id_is_returned_as_is(self, products):
        hive = _make_hive(products=products)
        assert hive._device_name("p1") == "p1"


class TestReportDeviceOffline:
    """Tests for Hive.report_device_offline and Hive.device_recovered."""

    def test_first_report_logs_a_warning_with_the_product_name(self, caplog):
        hive = _make_hive(products={"d1": {"state": {"name": "Hallway"}}})
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            hive.report_device_offline("d1")
        assert caplog.messages == ["Hallway is offline"]

    def test_logs_once_per_device_until_it_recovers(self, caplog):
        hive = _make_hive(
            products={
                "d1": {"state": {"name": "Hallway"}},
                "d2": {"state": {"name": "Kitchen"}},
            }
        )
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            hive.report_device_offline("d1")
            hive.report_device_offline("d2")
            hive.device_recovered("d1")
            hive.report_device_offline("d2")
            hive.report_device_offline("d1")
        assert caplog.messages == [
            "Hallway is offline",
            "Kitchen is offline",
            "Hallway is offline",
        ]
