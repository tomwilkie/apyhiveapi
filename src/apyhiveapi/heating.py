"""Hive heating: thermostats and TRV controls as Home Assistant climate entities."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from apyhiveapi.device_handler import DeviceHandler
from apyhiveapi.exceptions import HiveApiError

if TYPE_CHECKING:
    from apyhiveapi.api import JsonValue
    from apyhiveapi.models import Device

_LOGGER = logging.getLogger(__name__)

MIN_TARGET_TEMPERATURE = 5.0
MAX_TARGET_TEMPERATURE = 32.0

# Target temperature to restore when the previous mode has none recorded.
_FALLBACK_TARGET_TEMPERATURE = 7

_OFFLINE_STATUS = {
    "current_temperature": None,
    "target_temperature": None,
    "action": None,
    "mode": None,
    "boost": None,
}


class Climate(DeviceHandler):
    """Heating zones and TRV controls."""

    async def get_current_temperature(self, device: Device) -> float | None:
        """Return the measured temperature rounded to one decimal place, or None."""
        temperature = self._as_float(
            self._get_product_state(device, "props", "temperature")
        )
        return None if temperature is None else round(temperature, 1)

    async def get_target_temperature(self, device: Device) -> float | None:
        """Return the target temperature, or None if missing or invalid."""
        target = self._get_product_state(device, "state", "target")
        if target is None:
            target = self._get_product_state(device, "state", "heat")
        return self._as_float(target)

    async def get_mode(self, device: Device) -> str | None:
        """Return the heating mode: SCHEDULE, MANUAL or OFF.

        While a boost is active Hive reports BOOST, so the mode the boost will
        return to is reported instead.
        """
        mode = self._get_product_state(device, "state", "mode")
        if mode == "BOOST":
            mode = self._get_product_state(device, "props", "previous", "mode")
        return "OFF" if mode is False else mode

    async def get_current_operation(self, device: Device) -> bool | str | None:
        """Return whether the zone is calling for heat."""
        return self._get_product_state(device, "props", "working")

    async def set_target_temperature(self, device: Device, new_temp: float) -> None:
        """Set the target temperature."""
        await self._set_state(device, target=new_temp)

    async def set_mode(self, device: Device, new_mode: str) -> None:
        """Set the heating mode: SCHEDULE, MANUAL or OFF."""
        await self._set_state(device, mode=new_mode)

    async def set_boost_on(self, device: Device, mins: float, temp: float) -> None:
        """Boost the heating to *temp* degrees for *mins* minutes.

        Raises:
            ValueError: *mins* is not positive or *temp* is out of range.
        """
        minutes = int(mins)
        temperature = float(temp)
        if minutes <= 0:
            raise ValueError(f"Boost minutes must be positive, not {mins}")
        if not MIN_TARGET_TEMPERATURE <= temperature <= MAX_TARGET_TEMPERATURE:
            raise ValueError(
                f"Boost temperature must be between {MIN_TARGET_TEMPERATURE} and "
                f"{MAX_TARGET_TEMPERATURE}, not {temp}"
            )
        await self._set_state(device, mode="BOOST", boost=minutes, target=temperature)

    async def set_boost_off(self, device: Device) -> None:
        """End an active boost and restore the previous mode.

        Does nothing when the heating is not boosting.

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
        state: dict[str, JsonValue] = {"mode": previous_mode}
        if previous_mode in ("MANUAL", "OFF"):
            state["target"] = (
                self._get_product_state(device, "props", "previous", "target")
                or _FALLBACK_TARGET_TEMPERATURE
            )
        await self._set_state(device, **state)

    async def get_climate(self, device: Device) -> Device:
        """Refresh the device's status from the latest poll.

        Returns:
            The updated device. An offline device gets a status of ``None``
            values and ``device_data["online"]`` set to False.
        """
        if not self._refresh_device(device):
            device.status = dict(_OFFLINE_STATUS)
            return device
        device.min_temp = MIN_TARGET_TEMPERATURE
        device.max_temp = MAX_TARGET_TEMPERATURE
        device.status = {
            "current_temperature": await self.get_current_temperature(device),
            "target_temperature": await self.get_target_temperature(device),
            "action": await self.get_current_operation(device),
            "mode": await self.get_mode(device),
            "boost": self._boost_status(device),
        }
        return device
