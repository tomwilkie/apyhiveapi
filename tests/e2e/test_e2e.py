"""End-to-end tests over the file-mode session (use@file.com) and its fixture."""

from apyhiveapi import HeatingProduct, HotWaterProduct

_HUB_ID = "hub-0000-0000-0000-000000000001"
_THERMOSTAT_ID = "thermostatui-0000-0000-0000-000000000001"
_BOILER_MODULE_ID = "boilermodule-0000-0000-0000-000000000001"
_HEATING_ID = "heating-0000-0000-0000-000000000001"
_HOTWATER_ID = "hotwater-0000-0000-0000-000000000001"


class TestFixture:
    async def test_only_the_heating_and_hot_water_nodes_are_returned(
        self, file_session
    ):
        nodes = await file_session.get_nodes()
        assert {(p.type, p.id) for p in nodes.products.values()} == {
            ("heating", _HEATING_ID),
            ("hotwater", _HOTWATER_ID),
        }
        assert {(d.type, d.id) for d in nodes.devices.values()} == {
            ("hub", _HUB_ID),
            ("thermostatui", _THERMOSTAT_ID),
            ("boilermodule", _BOILER_MODULE_ID),
        }
        assert nodes.user.temperature_unit == "C"

    async def test_products_link_to_their_hardware(self, file_session):
        # What the fork needs to attach each product to a device: heating and
        # hot water share the thermostat's zone.
        nodes = await file_session.get_nodes()
        heating = nodes.products[_HEATING_ID]
        hot_water = nodes.products[_HOTWATER_ID]
        assert isinstance(heating, HeatingProduct)
        assert isinstance(hot_water, HotWaterProduct)
        assert heating.zone == hot_water.zone == nodes.devices[_THERMOSTAT_ID].zone
        assert nodes.devices[_THERMOSTAT_ID].battery == 80
        assert nodes.devices[_HUB_ID].mac_address is not None

    async def test_commands_send_nothing_and_leave_the_fixture_unchanged(
        self, file_session
    ):
        before = await file_session.get_nodes()
        heating = before.products[_HEATING_ID]
        hot_water = before.products[_HOTWATER_ID]
        assert isinstance(heating, HeatingProduct)
        assert isinstance(hot_water, HotWaterProduct)
        await file_session.set_target_temperature(heating, 21)
        await file_session.set_heating_boost(heating, 30, 22)
        await file_session.set_mode(hot_water, "MANUAL")
        await file_session.set_hot_water_boost(hot_water, 60)
        assert await file_session.get_nodes() == before
