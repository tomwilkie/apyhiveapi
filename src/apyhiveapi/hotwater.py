"""Hive hot water as a Home Assistant water heater entity."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from apyhiveapi.device_handler import DeviceHandler
from apyhiveapi.exceptions import HiveApiError

if TYPE_CHECKING:
    from apyhiveapi.models import Device

_LOGGER = logging.getLogger(__name__)


class WaterHeater(DeviceHandler):
    """Hot water control."""

    async def get_mode(self, device: Device) -> str | None:
        """Return the hot water operation: SCHEDULE, ON or OFF.

        Hive's MANUAL mode is reported as ON. While a boost is active Hive
        reports BOOST, so the mode the boost will return to is reported instead.
        """
        mode = self._get_product_state(device, "state", "mode")
        if mode == "BOOST":
            mode = self._previous_mode(device)
        elif mode is False:
            mode = "OFF"
        return "ON" if mode == "MANUAL" else mode

    async def set_mode(self, device: Device, new_mode: str) -> None:
        """Set the hot water mode: SCHEDULE, MANUAL or OFF."""
        await self._set_state(device, mode=new_mode)

    async def set_boost_on(self, device: Device, mins: float) -> None:
        """Boost the hot water for *mins* minutes.

        Raises:
            ValueError: *mins* is not positive.
        """
        minutes = int(mins)
        if minutes <= 0:
            raise ValueError(f"Boost minutes must be positive, not {mins}")
        await self._set_state(device, mode="BOOST", boost=minutes)

    async def set_boost_off(self, device: Device) -> None:
        """End an active boost and restore the previous mode.

        Does nothing when the hot water is not boosting.

        Raises:
            HiveApiError: Hive did not report the mode to restore.
        """
        if self._boost_status(device) != "ON":
            _LOGGER.debug("%s is not boosting", device.ha_name)
            return
        previous_mode = self._previous_mode(device)
        if previous_mode is None:
            raise HiveApiError(
                f"Hive did not report the mode to restore for {device.ha_name}"
            )
        await self._set_state(device, mode=previous_mode)

    async def get_water_heater(self, device: Device) -> Device:
        """Refresh the device's status from the latest poll.

        Returns:
            The updated device. An offline device gets a status of ``None``
            values and ``device_data["online"]`` set to False.
        """
        if not self._refresh_device(device):
            device.status = {"current_operation": None, "boost": None}
            return device
        device.status = {
            "current_operation": await self.get_mode(device),
            "boost": self._boost_status(device),
        }
        return device
