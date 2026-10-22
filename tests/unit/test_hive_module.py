"""Unit tests for the Hive class composition."""

from apyhiveapi.heating import Climate
from apyhiveapi.hive import Hive
from apyhiveapi.hotwater import WaterHeater


class TestHiveInit:
    """Tests for the handlers Hive.__init__ attaches."""

    async def test_initializes_heating(self):
        async with Hive(username="use@file.com", password="") as hive:
            assert isinstance(hive.heating, Climate)
            assert hive.heating.session is hive

    async def test_initializes_hotwater(self):
        async with Hive(username="use@file.com", password="") as hive:
            assert isinstance(hive.hotwater, WaterHeater)
            assert hive.hotwater.session is hive

    async def test_blank_credentials_default_to_empty_strings(self):
        async with Hive() as hive:
            assert hive.auth.username == ""
            assert hive.auth.password == ""
            assert hive.config.username is None
