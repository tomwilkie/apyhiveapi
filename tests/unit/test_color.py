"""Extended branch-coverage tests for LightColorHandler (devices/color.py)."""

# pylint: disable=protected-access

from unittest.mock import MagicMock

from apyhiveapi.devices.color import LightColorHandler
from apyhiveapi.helper.hivedataclasses import Device
from apyhiveapi.helper.map import Map


def _make_session(products=None):
    session = MagicMock()
    session.data = Map(
        {
            "products": products or {},
            "devices": {},
            "user": {},
        }
    )
    return session


def _make_handler(session):
    """Return a concrete LightColorHandler bound to *session*."""

    class ConcreteColorHandler(LightColorHandler):
        """Minimal concrete subclass for testing."""

    h = ConcreteColorHandler()
    h.session = session
    return h


def _make_device(hive_id="light-1"):
    return Device(
        hive_id=hive_id,
        hive_name="Test Light",
        hive_type="tuneablelight",
        ha_type="light",
        device_id="dev-1",
        device_name="Test Light",
        device_data={"online": True},
        ha_name="Test Light",
    )


class TestZeroDivisionGuards:
    """Colour-temperature methods must return None instead of raising ZeroDivisionError."""

    async def test_get_color_temp_zero_returns_none(self):
        """state colourTemperature == 0 must return None, not raise ZeroDivisionError."""
        session = _make_session(
            products={"light-1": {"state": {"colourTemperature": 0}}}
        )
        h = _make_handler(session)
        device = _make_device()
        result = await h.get_color_temp(device)
        assert result is None


# ---------------------------------------------------------------------------
# get_color — must return an RGB 3-tuple: Home Assistant's light.py calls
# color_RGB_to_hs(*status["hs_color"])
# ---------------------------------------------------------------------------


class TestGetColorReturnsRGBTuple:
    """get_color must return an (r, g, b) 3-tuple for Home Assistant."""

    async def test_get_color_returns_three_tuple(self):
        """get_color returns a 3-tuple that Home Assistant can unpack as RGB."""
        session = _make_session(
            {"light-1": {"state": {"hue": 120, "saturation": 75, "value": 100}}}
        )
        h = _make_handler(session)
        device = _make_device()
        result = await h.get_color(device)
        assert result is not None
        assert len(result) == 3, f"Expected 3-tuple (r, g, b), got {result!r}"

    async def test_get_color_converts_hsv_to_rgb(self):
        """get_color converts the API's hue/saturation/value to RGB."""
        session = _make_session(
            {"light-1": {"state": {"hue": 180, "saturation": 50, "value": 80}}}
        )
        h = _make_handler(session)
        device = _make_device()
        result = await h.get_color(device)
        assert result == (102, 204, 204)

    async def test_get_color_rounds_channels(self):
        """Channels are rounded, not truncated (2.55 -> 3, not 2)."""
        session = _make_session(
            {"light-1": {"state": {"hue": 0, "saturation": 99, "value": 100}}}
        )
        h = _make_handler(session)
        result = await h.get_color(_make_device())
        assert result == (255, 3, 3)
