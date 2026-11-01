"""Unit tests for parsing Hive's /nodes/all response into typed nodes."""

import pytest

from apyhiveapi.nodes import (
    Device,
    HeatingProduct,
    HotWaterProduct,
    User,
    parse_nodes,
)

_HEATING = {
    "id": "heat-1",
    "type": "heating",
    "props": {
        "online": True,
        "model": "SLR1",
        "version": "1009",
        "zone": "zone-A",
        "temperature": 19.2,
        "working": False,
        "previous": {"mode": "SCHEDULE", "target": 18},
    },
    "state": {"name": "Receiver", "mode": "BOOST", "target": 21.5, "boost": 30},
}
_HOTWATER = {
    "id": "hw-1",
    "type": "hotwater",
    "props": {
        "online": False,
        "model": "SLR2",
        "version": "0807",
        "zone": "zone-A",
        "previous": {"mode": False},
    },
    "state": {"name": "Hot Water", "mode": "MANUAL", "status": "ON", "boost": None},
}
_HUB = {
    "id": "hub-1",
    "type": "hub",
    "props": {
        "online": True,
        "model": "SENSE",
        "manufacturer": "AlertMe",
        "version": "1.0",
        "macAddress": "00:0A:11:BB:22:CC",
    },
    "state": {"name": "Hub"},
}
_THERMOSTAT = {
    "id": "thermo-1",
    "type": "thermostatui",
    "props": {"online": False, "zone": "zone-A", "battery": 80, "model": "SLT3"},
    "state": {"name": "Hallway"},
}


def _parse(products=(), devices=(), user=None):
    return parse_nodes(
        {"user": user or {}, "products": list(products), "devices": list(devices)}
    )


class TestParseNodes:
    def test_products_keep_hives_own_names_and_values(self):
        nodes = _parse([_HEATING, _HOTWATER])
        assert nodes.products == {
            "heat-1": HeatingProduct(
                id="heat-1",
                type="heating",
                name="Receiver",
                online=True,
                model="SLR1",
                version="1009",
                zone="zone-A",
                temperature=19.2,
                working=False,
                mode="BOOST",
                target=21.5,
                boost=30,
                previous_mode="SCHEDULE",
                previous_target=18.0,
            ),
            "hw-1": HotWaterProduct(
                id="hw-1",
                type="hotwater",
                name="Hot Water",
                online=False,
                model="SLR2",
                version="0807",
                zone="zone-A",
                mode="MANUAL",
                status="ON",
                boost=None,
                previous_mode=False,
            ),
        }

    def test_devices_and_user_are_parsed(self):
        nodes = _parse(devices=[_HUB, _THERMOSTAT], user={"temperatureUnit": "F"})
        assert nodes.user == User(temperature_unit="F")
        assert nodes.devices == {
            "hub-1": Device(
                id="hub-1",
                type="hub",
                name="Hub",
                online=True,
                model="SENSE",
                manufacturer="AlertMe",
                version="1.0",
                zone=None,
                battery=None,
                mac_address="00:0A:11:BB:22:CC",
            ),
            "thermo-1": Device(
                id="thermo-1",
                type="thermostatui",
                name="Hallway",
                online=False,
                model="SLT3",
                manufacturer=None,
                version=None,
                zone="zone-A",
                battery=80,
                mac_address=None,
            ),
        }

    @pytest.mark.parametrize(
        "product",
        [
            pytest.param({"id": "heat-1", "type": "heating"}, id="no-props-or-state"),
            pytest.param(
                {"id": "heat-1", "type": "heating", "props": [], "state": "broken"},
                id="props-and-state-not-dicts",
            ),
            pytest.param(
                {
                    "id": "heat-1",
                    "type": "heating",
                    "props": {
                        "online": "yes",
                        "temperature": "19.2",
                        "working": 1,
                        "previous": {"mode": 0, "target": True},
                    },
                    "state": {"name": 5, "mode": None, "target": False, "boost": True},
                },
                id="wrong-types",
            ),
        ],
    )
    def test_missing_or_wrong_typed_values_are_none(self, product):
        heating = _parse([product]).products["heat-1"]
        assert heating == HeatingProduct(
            id="heat-1",
            type="heating",
            name=None,
            online=None,
            model=None,
            version=None,
            zone=None,
            temperature=None,
            working=None,
            mode=None,
            target=None,
            boost=None,
            previous_mode=None,
            previous_target=None,
        )

    @pytest.mark.parametrize(
        "response",
        [
            pytest.param({}, id="empty"),
            pytest.param({"products": {}, "devices": "x", "user": []}, id="not-lists"),
            pytest.param(
                {
                    "products": [
                        None,
                        "junk",
                        {"type": "heating"},
                        {"id": 7, "type": "heating"},
                        {"id": "light-1", "type": "warmwhitelight"},
                    ],
                    "devices": [{"id": "plug-1", "type": "activeplug"}],
                },
                id="malformed-or-unsupported",
            ),
        ],
    )
    def test_malformed_and_unsupported_nodes_are_dropped(self, response):
        nodes = parse_nodes(response)
        assert nodes.products == {}
        assert nodes.devices == {}
        assert nodes.user == User(temperature_unit=None)
