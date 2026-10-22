"""Unit tests for the DeviceHandler shared plumbing."""

# pylint: disable=protected-access

import asyncio
from unittest.mock import AsyncMock

from apyhiveapi.device_handler import DeviceHandler
from tests.conftest import make_device, make_session


def _make_handler(products=None, devices=None):
    """Return a DeviceHandler bound to a mock session."""
    return DeviceHandler(make_session(products=products, devices=devices))


class TestGetProductState:
    """Tests for DeviceHandler._get_product_state."""

    def test_happy_path(self):
        h = _make_handler({"prod-1": {"state": {"mode": "SCHEDULE"}}})
        assert h._get_product_state(make_device(), "state", "mode") == "SCHEDULE"

    def test_first_key_missing_returns_default(self):
        h = _make_handler({"prod-1": {}})
        assert h._get_product_state(make_device(), "missing_key") is None

    def test_nested_key_missing_returns_default(self):
        h = _make_handler({"prod-1": {"state": {}}})
        assert h._get_product_state(make_device(), "state", "mode") is None

    def test_explicit_default_param(self):
        h = _make_handler({"prod-1": {}})
        assert h._get_product_state(make_device(), "missing", default="x") == "x"

    def test_product_not_in_data_returns_default(self):
        h = _make_handler({})
        assert h._get_product_state(make_device(), "state") is None

    def test_non_dict_node_returns_default(self):
        h = _make_handler({"prod-1": {"state": "SCHEDULE"}})
        assert h._get_product_state(make_device(), "state", "mode") is None


class TestAvailability:
    """Tests for _is_available, _online and _update_online_status."""

    def test_available_when_product_known_and_online(self):
        h = _make_handler({"prod-1": {}})
        assert h._is_available(make_device()) is True

    def test_unavailable_when_product_unknown(self):
        h = _make_handler({})
        assert h._is_available(make_device()) is False

    def test_unavailable_when_offline(self):
        h = _make_handler({"prod-1": {}})
        assert h._is_available(make_device(device_data={"online": False})) is False

    def test_unavailable_when_device_data_not_a_dict(self):
        h = _make_handler({"prod-1": {}})
        assert h._is_available(make_device(device_data=None)) is False

    def test_online_reads_device_props(self):
        h = _make_handler(devices={"dev-1": {"props": {"online": True}}})
        assert h._online("dev-1") is True

    def test_online_false_when_device_offline(self):
        h = _make_handler(devices={"dev-1": {"props": {"online": False}}})
        assert h._online("dev-1") is False

    def test_online_none_when_device_unknown(self):
        h = _make_handler()
        assert h._online("dev-1") is None

    def test_update_online_status_stores_result(self):
        h = _make_handler(devices={"dev-1": {"props": {"online": False}}})
        device = make_device()
        assert h._update_online_status(device) is False
        assert device.device_data["online"] is False

    def test_update_online_status_repairs_missing_device_data(self):
        h = _make_handler(devices={"dev-1": {"props": {"online": True}}})
        device = make_device(device_data=None)
        assert h._update_online_status(device) is True
        assert device.device_data == {"online": True}


class TestCachedIfBusy:
    """Tests for DeviceHandler._cached_if_busy."""

    def test_returns_none_when_session_not_busy(self):
        h = _make_handler()
        assert h._cached_if_busy(make_device()) is None
        h.session.get_cached_device.assert_not_called()

    def test_returns_cached_device_when_busy(self):
        h = _make_handler()
        cached = make_device()
        h.session.should_use_cached_data.return_value = True
        h.session.get_cached_device.return_value = cached
        assert h._cached_if_busy(make_device()) is cached


class TestApplyDeviceProps:
    """Tests for DeviceHandler._apply_device_props."""

    def test_copies_props_and_parent(self):
        device = make_device()
        data = {"props": {"model": "SLT3"}, "parent": "hub-1"}
        DeviceHandler._apply_device_props(device, data, online=True)
        assert device.device_data == {"model": "SLT3", "online": True}
        assert device.parent_device == "hub-1"

    def test_missing_props_become_online_only(self):
        device = make_device()
        DeviceHandler._apply_device_props(device, {}, online=False)
        assert device.device_data == {"online": False}
        assert device.parent_device is None


class TestReportOffline:
    """Tests for DeviceHandler._report_offline."""

    def test_reports_and_sets_default_status(self):
        h = _make_handler()
        device = make_device()
        result = h._report_offline(device, {"mode": None})
        assert result is device
        assert device.status == {"mode": None}
        h.session.report_device_offline.assert_called_once_with("dev-1")

    def test_keeps_existing_status(self):
        h = _make_handler()
        device = make_device(status={"mode": "SCHEDULE"})
        h._report_offline(device, {"mode": None})
        assert device.status == {"mode": "SCHEDULE"}


class TestExecuteStateChange:
    """Tests for DeviceHandler._execute_state_change."""

    async def test_product_not_in_data_returns_false(self):
        h = _make_handler({})
        assert await h._execute_state_change(make_device(), mode="MANUAL") is False

    async def test_device_offline_returns_false(self):
        h = _make_handler({"prod-1": {"type": "heating"}})
        device = make_device(device_data={"online": False})
        assert await h._execute_state_change(device, mode="MANUAL") is False

    async def test_device_data_not_dict_returns_false(self):
        h = _make_handler({"prod-1": {"type": "heating"}})
        device = make_device(device_data=None)
        assert await h._execute_state_change(device, mode="MANUAL") is False

    async def test_http_200_returns_true_and_calls_get_devices(self):
        h = _make_handler({"prod-1": {"type": "heating"}})
        result = await h._execute_state_change(make_device(), mode="MANUAL")
        assert result is True
        h.session.hive_refresh_tokens.assert_called_once()
        h.session.api.set_state.assert_awaited_once_with(
            "heating", "prod-1", mode="MANUAL"
        )
        h.session.get_devices.assert_called_once_with()

    async def test_non_200_returns_false(self):
        h = _make_handler({"prod-1": {"type": "heating"}})
        h.session.api.set_state = AsyncMock(return_value={"original": 500})
        assert await h._execute_state_change(make_device(), mode="MANUAL") is False
        h.session.get_devices.assert_not_called()

    async def test_malformed_set_state_response_returns_false(self):
        h = _make_handler({"prod-1": {"type": "heating"}})
        h.session.api.set_state = AsyncMock(return_value={"parsed": {}})
        assert await h._execute_state_change(make_device(), mode="MANUAL") is False
        h.session.get_devices.assert_not_called()

    async def test_set_state_timeout_returns_false(self):
        h = _make_handler({"prod-1": {"type": "heating"}})
        h.session.api.set_state = AsyncMock(side_effect=asyncio.TimeoutError)
        assert await h._execute_state_change(make_device(), mode="MANUAL") is False
        h.session.get_devices.assert_not_called()
