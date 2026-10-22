"""Shared pytest fixtures."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from apyhiveapi import Hive
from apyhiveapi.models import Device, SessionConfig, SessionData


@pytest.fixture
async def file_session():
    """Hive session loaded from the bundled data.json fixture."""
    async with Hive(username="use@file.com", password="") as hive:
        await hive.start_session({})
        yield hive


def make_session(products=None, devices=None, user=None):
    """Return a mock Hive session with the attributes the handlers use."""
    session = MagicMock()
    session.data = SessionData(
        products=products or {}, devices=devices or {}, user=user or {}
    )
    session.config = SessionConfig()
    session.device_recovered = MagicMock()
    session.report_device_offline = MagicMock()
    session.api = MagicMock()
    session.api.set_state = AsyncMock(return_value={"original": 200, "parsed": {}})
    session.hive_refresh_tokens = AsyncMock()
    session.get_devices = AsyncMock(return_value=True)
    session.should_use_cached_data = MagicMock(return_value=False)
    session.get_cached_device = MagicMock(return_value=None)
    session.set_cached_device = MagicMock(side_effect=lambda d: d)
    return session


@pytest.fixture
def fake_session():
    """Lightweight stub session for module-level tests."""
    return make_session()


def make_device(hive_id="prod-1", device_id="dev-1", hive_type="heating", **kwargs):
    """Build a Device with sensible defaults for tests."""
    ha_type = kwargs.pop("ha_type", "climate")
    device_data = kwargs.pop("device_data", {"online": True})
    return Device(
        hive_id=hive_id,
        hive_name="Test",
        hive_type=hive_type,
        ha_type=ha_type,
        device_id=device_id,
        device_name="Test",
        device_data=device_data,
        **kwargs,
    )
