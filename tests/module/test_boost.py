"""Tests for DeviceHandler.get_boost_status, shared by heating and hot water."""

from apyhiveapi.device_handler import DeviceHandler
from tests.conftest import make_device, make_session

_BOOST_ON_MINUTES = 30


def _make_handler(products):
    """Create a DeviceHandler bound to a mocked session holding *products*."""
    return DeviceHandler(make_session(products=products))


class TestGetBoostStatus:
    """Tests for DeviceHandler.get_boost_status()."""

    async def test_int_minutes_returns_on(self):
        """Boost with minutes remaining returns ON."""
        h = _make_handler({"prod-1": {"state": {"boost": _BOOST_ON_MINUTES}}})
        assert await h.get_boost_status(make_device()) == "ON"

    async def test_false_returns_off(self):
        """Boost value False returns OFF."""
        h = _make_handler({"prod-1": {"state": {"boost": False}}})
        assert await h.get_boost_status(make_device()) == "OFF"

    async def test_none_returns_off(self):
        """Boost value None returns OFF."""
        h = _make_handler({"prod-1": {"state": {"boost": None}}})
        assert await h.get_boost_status(make_device()) == "OFF"

    async def test_missing_boost_returns_off(self):
        """Missing boost key returns OFF."""
        h = _make_handler({"prod-1": {"state": {}}})
        assert await h.get_boost_status(make_device()) == "OFF"

    async def test_missing_product_returns_none(self):
        """Missing product ID returns None."""
        h = _make_handler({})
        assert await h.get_boost_status(make_device()) is None

    async def test_zero_minutes_returns_off(self):
        """Boost with 0 minutes returns OFF."""
        h = _make_handler({"prod-1": {"state": {"boost": 0}}})
        assert await h.get_boost_status(make_device()) == "OFF"

    async def test_missing_state_returns_none(self):
        """Missing state dict returns None."""
        h = _make_handler({"prod-1": {}})
        assert await h.get_boost_status(make_device()) is None
