"""Unit tests for hive.py module-level functions and the Hive class."""

# pylint: disable=protected-access,too-few-public-methods

from apyhiveapi.hive import Hive


class TestHiveInit:
    """Tests for Hive.__init__ device module composition."""

    async def test_initializes_heating(self):
        from apyhiveapi.devices.heating import Climate

        async with Hive(username="use@file.com", password="") as hive:
            assert isinstance(hive.heating, Climate)

    async def test_initializes_hotwater(self):
        from apyhiveapi.devices.hotwater import WaterHeater

        async with Hive(username="use@file.com", password="") as hive:
            assert isinstance(hive.hotwater, WaterHeater)

    async def test_initializes_hub(self):
        from apyhiveapi.devices.hub import HiveHub

        async with Hive(username="use@file.com", password="") as hive:
            assert isinstance(hive.hub, HiveHub)

    async def test_initializes_light(self):
        from apyhiveapi.devices.light import Light

        async with Hive(username="use@file.com", password="") as hive:
            assert isinstance(hive.light, Light)

    async def test_initializes_switch(self):
        from apyhiveapi.devices.plug import Switch

        async with Hive(username="use@file.com", password="") as hive:
            assert isinstance(hive.switch, Switch)

    async def test_initializes_sensor(self):
        from apyhiveapi.devices.sensor import Sensor

        async with Hive(username="use@file.com", password="") as hive:
            assert isinstance(hive.sensor, Sensor)

    async def test_session_is_self(self):
        async with Hive(username="use@file.com", password="") as hive:
            assert hive.session is hive
