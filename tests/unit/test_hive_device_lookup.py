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
_TRV = {
    "id": "trv-1",
    "type": "trv",
    "state": {"name": "TRV"},
    "props": {"zone": "zone-B"},
}
_BOILER = {
    "id": "boiler-1",
    "type": "boilermodule",
    "state": {"name": "Boiler"},
    "props": {"zone": "zone-A"},
}


def _make_hive(products=None, devices=None):
    """Return a Hive whose data holds the given products and devices."""
    hive = Hive(username="use@file.com", password="")
    hive.data.products = products or {}
    hive.data.devices = devices or {}
    return hive


class TestDeviceForProduct:
    """Tests for Hive._device_for_product."""

    @pytest.mark.parametrize("product_type", ["heating", "hotwater"])
    def test_zone_products_use_the_thermostat_in_their_zone(self, product_type):
        hive = _make_hive(devices={"hub-1": _HUB, "thermo-1": _THERMOSTAT})
        product = {"id": "p-1", "type": product_type, "props": {"zone": "zone-A"}}
        assert hive._device_for_product(product) is _THERMOSTAT

    def test_a_trv_in_the_zone_also_qualifies(self):
        hive = _make_hive(devices={"hub-1": _HUB, "trv-1": _TRV})
        product = {"id": "p-1", "type": "heating", "props": {"zone": "zone-B"}}
        assert hive._device_for_product(product) is _TRV

    def test_other_device_types_in_the_zone_are_ignored(self):
        hive = _make_hive(devices={"boiler-1": _BOILER, "thermo-1": _THERMOSTAT})
        product = {"id": "p-1", "type": "heating", "props": {"zone": "zone-A"}}
        assert hive._device_for_product(product) is _THERMOSTAT

    def test_first_matching_device_wins(self):
        second = {**_THERMOSTAT, "id": "thermo-2"}
        hive = _make_hive(devices={"thermo-1": _THERMOSTAT, "thermo-2": second})
        product = {"id": "p-1", "type": "hotwater", "props": {"zone": "zone-A"}}
        assert hive._device_for_product(product) is _THERMOSTAT

    def test_zone_mismatch_keeps_the_product_as_its_own_device(self):
        hive = _make_hive(devices={"thermo-1": _THERMOSTAT})
        product = {"id": "p-1", "type": "heating", "props": {"zone": "zone-Z"}}
        assert hive._device_for_product(product) is product

    @pytest.mark.parametrize(
        "props",
        [
            pytest.param({}, id="no-zone"),
            pytest.param({"zone": None}, id="zone-none"),
            pytest.param({"zone": ""}, id="zone-empty"),
        ],
    )
    def test_product_without_a_zone_is_its_own_device(self, props):
        unzoned = {**_THERMOSTAT, "props": {}}
        hive = _make_hive(devices={"thermo-1": unzoned})
        product = {"id": "p-1", "type": "hotwater", "props": props}
        assert hive._device_for_product(product) is product

    def test_product_without_props_is_its_own_device(self):
        hive = _make_hive(devices={"thermo-1": _THERMOSTAT})
        product = {"id": "p-1", "type": "heating"}
        assert hive._device_for_product(product) is product

    def test_devices_without_props_are_skipped(self):
        hive = _make_hive(devices={"trv-1": {"id": "trv-1", "type": "trv"}})
        product = {"id": "p-1", "type": "heating", "props": {"zone": "zone-A"}}
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

    def test_trv_control_without_trvs_raises_index_error(self):
        hive = _make_hive(devices={"trv-1": _TRV})
        product = {"id": "trvc-1", "type": "trvcontrol", "props": {"trvs": []}}
        with pytest.raises(IndexError):
            hive._device_for_product(product)

    def test_trv_control_with_an_unknown_trv_raises_key_error(self):
        hive = _make_hive(devices={"trv-1": _TRV})
        product = {"id": "trvc-1", "type": "trvcontrol", "props": {"trvs": ["missing"]}}
        with pytest.raises(KeyError, match="missing"):
            hive._device_for_product(product)

    def test_hub_is_its_own_device(self):
        hive = _make_hive(devices={"hub-1": _HUB})
        assert hive._device_for_product({"id": "hub-1", "type": "hub"}) is _HUB

    def test_other_products_use_the_device_with_their_id(self):
        plug = {"id": "plug-1", "type": "activeplug", "state": {"name": "Plug"}}
        hive = _make_hive(devices={"plug-1": plug})
        assert hive._device_for_product({"id": "plug-1", "type": "activeplug"}) is plug

    def test_missing_device_raises_key_error(self):
        hive = _make_hive()
        with pytest.raises(KeyError, match="hub-1"):
            hive._device_for_product({"id": "hub-1", "type": "hub"})

    def test_product_without_a_type_raises_key_error(self):
        hive = _make_hive(devices={"hub-1": _HUB})
        with pytest.raises(KeyError, match="type"):
            hive._device_for_product({"id": "hub-1"})

    @pytest.mark.parametrize(
        "props",
        [
            pytest.param(None, id="none"),
            pytest.param("zone-A", id="string"),
            pytest.param(["zone-A"], id="list"),
        ],
    )
    def test_malformed_product_props_raise_attribute_error(self, props):
        hive = _make_hive(devices={"thermo-1": _THERMOSTAT})
        with pytest.raises(AttributeError):
            hive._device_for_product({"id": "p-1", "type": "heating", "props": props})

    def test_malformed_device_props_raise_attribute_error(self):
        hive = _make_hive(devices={"thermo-1": {**_THERMOSTAT, "props": None}})
        product = {"id": "p-1", "type": "heating", "props": {"zone": "zone-A"}}
        with pytest.raises(AttributeError):
            hive._device_for_product(product)


class TestDeviceName:
    """Tests for Hive._device_name."""

    def test_product_name_is_used(self):
        hive = _make_hive(products={"p1": {"state": {"name": "Hallway"}}})
        assert hive._device_name("p1") == "Hallway"

    def test_device_name_is_used_when_there_is_no_product(self):
        hive = _make_hive(devices={"d1": {"state": {"name": "Thermostat"}}})
        assert hive._device_name("d1") == "Thermostat"

    def test_product_name_takes_priority(self):
        hive = _make_hive(
            products={"x1": {"state": {"name": "ProductName"}}},
            devices={"x1": {"state": {"name": "DeviceName"}}},
        )
        assert hive._device_name("x1") == "ProductName"

    def test_empty_product_name_falls_through_to_the_device(self):
        hive = _make_hive(
            products={"x1": {"state": {"name": ""}}},
            devices={"x1": {"state": {"name": "DeviceName"}}},
        )
        assert hive._device_name("x1") == "DeviceName"

    def test_non_string_name_is_converted(self):
        hive = _make_hive(products={"p1": {"state": {"name": 42}}})
        assert hive._device_name("p1") == "42"

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

    def test_null_product_state_falls_through_to_the_device(self):
        hive = _make_hive(
            products={"x1": {"state": None}},
            devices={"x1": {"state": {"name": "DeviceName"}}},
        )
        assert hive._device_name("x1") == "DeviceName"


class TestReportDeviceOffline:
    """Tests for Hive.report_device_offline and Hive.device_recovered."""

    def test_first_report_logs_a_warning_with_the_product_name(self, caplog):
        hive = _make_hive(products={"d1": {"state": {"name": "Hallway"}}})
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            hive.report_device_offline("d1")
        assert caplog.messages == ["Hallway is offline"]

    def test_falls_back_to_the_device_name(self, caplog):
        hive = _make_hive(devices={"d1": {"state": {"name": "Thermostat"}}})
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            hive.report_device_offline("d1")
        assert caplog.messages == ["Thermostat is offline"]

    def test_falls_back_to_the_id(self, caplog):
        hive = _make_hive()
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            hive.report_device_offline("d1")
        assert caplog.messages == ["d1 is offline"]

    def test_repeat_reports_are_silent(self, caplog):
        hive = _make_hive(products={"d1": {"state": {"name": "Hallway"}}})
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            hive.report_device_offline("d1")
            hive.report_device_offline("d1")
            hive.report_device_offline("d1")
        assert caplog.messages == ["Hallway is offline"]

    def test_logs_again_after_the_device_recovered(self, caplog):
        hive = _make_hive(products={"d1": {"state": {"name": "Hallway"}}})
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            hive.report_device_offline("d1")
            hive.device_recovered("d1")
            hive.report_device_offline("d1")
        assert caplog.messages == ["Hallway is offline", "Hallway is offline"]

    def test_devices_are_tracked_independently(self, caplog):
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

    def test_recovering_a_device_never_reported_is_a_no_op(self):
        hive = _make_hive()
        hive.device_recovered("never-reported")
        assert not hive._offline_devices
