"""File-mode integration tests and tests for the retry helper."""

# pylint: disable=redefined-outer-name

from unittest.mock import AsyncMock, patch

import pytest

from apyhiveapi import Hive


class TestFileSession:
    """Integration tests using the bundled data.json file fixture."""

    async def test_start_session_returns_devices(self, file_session):
        """start_session populates device_list with entries from the fixture."""
        assert file_session.device_list, "device_list should not be empty"

    async def test_climate_devices_present(self, file_session):
        """Fixture contains heating products, so climate entries exist."""
        assert file_session.device_list["climate"], "expected climate devices"

    async def test_water_heater_devices_present(self, file_session):
        """Fixture contains hot water products, so water_heater entries exist."""
        assert file_session.device_list["water_heater"], "expected water_heater"

    async def test_get_climate_returns_status(self, file_session):
        """get_climate populates device.status with the heating fields."""
        device = file_session.device_list["climate"][0]
        updated = await file_session.heating.get_climate(device)
        assert updated.status is not None
        assert "current_temperature" in updated.status
        assert "target_temperature" in updated.status
        assert "mode" in updated.status

    async def test_device_has_hive_id(self, file_session):
        """Every device in device_list has a hive_id."""
        for entity_type, devices in file_session.device_list.items():
            for device in devices:
                assert device.hive_id, f"{entity_type} device missing hive_id"

    async def test_update_data_returns_bool(self, file_session):
        """update_data returns a bool without raising."""
        device = file_session.device_list["climate"][0]
        result = await file_session.update_data(device)
        assert isinstance(result, bool)


EXPECTED_ATTEMPTS = 2


class TestTokenRefreshRetry:
    """Tests for the retry/backoff path in Hive._retry_with_backoff."""

    async def test_succeeds_on_first_attempt(self):
        """A coroutine that succeeds immediately is called exactly once."""
        async with Hive(
            username="test@example.com",
            password="pass",  # pragma: allowlist secret
        ) as hive:
            calls = 0

            async def coro():
                nonlocal calls
                calls += 1
                return "ok"

            result = await hive._retry_with_backoff(coro)  # pylint: disable=protected-access
        assert result == "ok"
        assert calls == 1

    async def test_retries_on_failure_then_succeeds(self):
        """A coroutine that fails once is retried and its success is returned."""
        async with Hive(
            username="test@example.com",
            password="pass",  # pragma: allowlist secret
        ) as hive:
            attempts = []

            async def coro():
                attempts.append(1)
                if len(attempts) < EXPECTED_ATTEMPTS:
                    raise Exception("transient")  # pylint: disable=broad-exception-raised
                return "recovered"

            with patch("asyncio.sleep", new=AsyncMock()):
                result = await hive._retry_with_backoff(coro, delays=(0, 0, 0))  # pylint: disable=protected-access

        assert result == "recovered"
        assert len(attempts) == EXPECTED_ATTEMPTS

    async def test_raises_after_all_retries_exhausted(self):
        """A coroutine that always fails raises after all retries are exhausted."""
        async with Hive(
            username="test@example.com",
            password="pass",  # pragma: allowlist secret
        ) as hive:

            async def always_fails():
                raise Exception("permanent failure")  # pylint: disable=broad-exception-raised

            with (
                patch("asyncio.sleep", new=AsyncMock()),
                pytest.raises(Exception, match="permanent failure"),
            ):
                await hive._retry_with_backoff(always_fails, delays=(0, 0))  # pylint: disable=protected-access
