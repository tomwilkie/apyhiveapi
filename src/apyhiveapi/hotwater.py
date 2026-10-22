"""Hive hot water as a Home Assistant water heater entity."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from apyhiveapi.device_handler import DeviceHandler

if TYPE_CHECKING:
    from apyhiveapi.models import Device

_LOGGER = logging.getLogger(__name__)

# Hive hot water modes mapped to the operation Home Assistant shows.
_HIVE_TO_HA_MODE: dict[str | bool | None, str] = {
    "MANUAL": "ON",
    None: "OFF",
    False: "OFF",
}


class WaterHeater(DeviceHandler):
    """Hot water control."""

    async def get_mode(self, device: Device) -> str | None:
        """Return the hot water operation: SCHEDULE, ON or OFF.

        While a boost is active Hive reports BOOST, so the mode the boost will
        return to is reported instead.
        """
        mode = self._get_product_state(device, "state", "mode")
        if mode is None:
            _LOGGER.debug("get_mode - No mode in state for %s.", device.ha_name)
            return None
        if mode == "BOOST":
            mode = self._get_product_state(device, "props", "previous", "mode")
        return _HIVE_TO_HA_MODE.get(mode, mode)

    async def set_mode(self, device: Device, new_mode: str) -> bool:
        """Set the hot water mode: SCHEDULE, MANUAL or OFF.

        Returns:
            bool: True if Hive accepted the change.
        """
        _LOGGER.debug(
            "set_mode - Setting hot water mode to %s for %s.", new_mode, device.ha_name
        )
        return await self._execute_state_change(device, mode=new_mode)

    async def set_boost_on(self, device: Device, mins: float | str) -> bool:
        """Boost the hot water for *mins* minutes.

        Returns:
            bool: True if Hive accepted the change.
        """
        if int(mins) <= 0:
            return False
        _LOGGER.debug(
            "set_boost_on - Setting hot water boost ON for %s: %s mins.",
            device.ha_name,
            mins,
        )
        return await self._execute_state_change(device, mode="BOOST", boost=mins)

    async def set_boost_off(self, device: Device) -> bool:
        """End an active boost and restore the previous mode.

        Returns:
            bool: True if Hive accepted the change.
        """
        if (
            not self._is_available(device)
            or await self.get_boost_status(device) != "ON"
        ):
            return False
        _LOGGER.debug(
            "set_boost_off - Setting hot water boost OFF for %s.", device.ha_name
        )
        prev_mode = self._get_product_state(device, "props", "previous", "mode")
        if prev_mode is None:
            _LOGGER.warning(
                "set_boost_off - Cannot determine previous mode for %s, skipping.",
                device.ha_name,
            )
            return False
        return await self._execute_state_change(device, mode=prev_mode)

    async def get_water_heater(self, device: Device) -> Device:
        """Refresh the device's status from the latest poll.

        Returns:
            Device: The updated device, or the cached one during a slow or
            busy poll.
        """
        cached = self._cached_if_busy(device)
        if cached is not None:
            _LOGGER.debug(
                "get_water_heater - Returning cached state for"
                " water heater %s (slow/busy poll).",
                device.ha_name,
            )
            return cached
        online = self._update_online_status(device)
        if not online:
            return self._report_offline(device, {"current_operation": None})

        self.session.device_recovered(device.device_id)
        _LOGGER.debug(
            "get_water_heater - Updating hot water data for %s.", device.ha_name
        )
        data = self.session.data.devices[device.device_id]
        device.status = {"current_operation": await self.get_mode(device)}
        self._apply_device_props(device, data, online=online)
        _LOGGER.debug(
            "get_water_heater - Water heater device data for %s: %s",
            device.ha_name,
            device.status,
        )
        return self.session.set_cached_device(device)
