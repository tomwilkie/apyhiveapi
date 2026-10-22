"""Hive heating: thermostats and TRV controls as Home Assistant climate entities."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from apyhiveapi.device_handler import DeviceHandler

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
    "state": None,
}


class Climate(DeviceHandler):
    """Heating zones and TRV controls."""

    async def get_current_temperature(self, device: Device) -> float | None:
        """Return the measured temperature, rounded to one decimal place."""
        try:
            data = self.session.data.products[device.hive_id]
            temperature = data["props"]["temperature"]
        except KeyError as err:
            _LOGGER.warning(
                "get_current_temperature - KeyError getting temperature for %s: %s",
                device.ha_name,
                err,
            )
            return None
        try:
            return round(float(temperature), 1)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "get_current_temperature - Non-numeric temperature value '%s' for %s.",
                temperature,
                device.ha_name,
            )
            return None

    async def get_target_temperature(self, device: Device) -> float | None:
        """Return the target temperature, or None if missing or invalid."""
        try:
            state = self.session.data.products[device.hive_id]["state"]
            target = state.get("target")
            if target is None:
                target = state.get("heat")
        except (KeyError, TypeError) as err:
            _LOGGER.warning(
                "get_target_temperature - Error getting target temperature for %s: %s",
                device.ha_name,
                err,
            )
            return None

        if target is None:
            return None
        try:
            return float(target)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "get_target_temperature - Non-numeric target temperature"
                " value '%s' for %s.",
                target,
                device.ha_name,
            )
            return None

    async def get_mode(self, device: Device) -> str | None:
        """Return the heating mode: SCHEDULE, MANUAL or OFF.

        While a boost is active Hive reports BOOST, so the mode the boost will
        return to is reported instead.
        """
        mode = self._get_product_state(device, "state", "mode")
        if mode is None:
            _LOGGER.debug("get_mode - No mode in state for %s.", device.ha_name)
            return None
        if mode == "BOOST":
            mode = self._get_product_state(device, "props", "previous", "mode")
        return "OFF" if mode is False else mode

    async def get_current_operation(self, device: Device) -> bool | str | None:
        """Return whether the zone is calling for heat."""
        return self._get_product_state(device, "props", "working")

    async def set_target_temperature(self, device: Device, new_temp: float) -> bool:
        """Set the target temperature.

        Returns:
            bool: True if Hive accepted the change.
        """
        _LOGGER.info(
            "set_target_temperature - Setting target temperature to %s for %s",
            new_temp,
            device.ha_name,
        )
        return await self._execute_state_change(device, target=new_temp)

    async def set_mode(self, device: Device, new_mode: str) -> bool:
        """Set the heating mode: SCHEDULE, MANUAL or OFF.

        Returns:
            bool: True if Hive accepted the change.
        """
        _LOGGER.info(
            "set_mode - Setting heating mode to %s for %s", new_mode, device.ha_name
        )
        return await self._execute_state_change(device, mode=new_mode)

    async def set_boost_on(
        self, device: Device, mins: float | str, temp: float
    ) -> bool | None:
        """Boost the heating to *temp* for *mins* minutes.

        Returns:
            bool | None: True if Hive accepted the change, None if the inputs
            are out of range.
        """
        try:
            mins_value = int(mins)
            temp_value = float(temp)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "set_boost_on - Invalid boost inputs for %s: mins=%r temp=%r",
                device.ha_name,
                mins,
                temp,
            )
            return None
        if not (
            mins_value > 0
            and MIN_TARGET_TEMPERATURE <= temp_value <= MAX_TARGET_TEMPERATURE
        ):
            return None
        _LOGGER.debug(
            "set_boost_on - Setting heating boost ON for %s: %s mins at %s degrees.",
            device.ha_name,
            mins,
            temp,
        )
        return await self._execute_state_change(
            device, mode="BOOST", boost=mins, target=temp
        )

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
            "set_boost_off - Setting heating boost OFF for %s.", device.ha_name
        )
        prev_mode = self._get_product_state(device, "props", "previous", "mode")
        if prev_mode is None:
            _LOGGER.warning(
                "set_boost_off - Cannot determine previous mode for %s, skipping.",
                device.ha_name,
            )
            return False
        kwargs: dict[str, JsonValue] = {"mode": prev_mode}
        if prev_mode in ("MANUAL", "OFF"):
            kwargs["target"] = (
                self._get_product_state(device, "props", "previous", "target")
                or _FALLBACK_TARGET_TEMPERATURE
            )
        return await self._execute_state_change(device, **kwargs)

    async def get_climate(self, device: Device) -> Device:
        """Refresh the device's status from the latest poll.

        Returns:
            Device: The updated device, or the cached one during a slow or
            busy poll.
        """
        cached = self._cached_if_busy(device)
        if cached is not None:
            _LOGGER.debug(
                "get_climate - Returning cached state for climate %s (slow/busy poll).",
                device.ha_name,
            )
            return cached
        online = self._update_online_status(device)
        if not online:
            return self._report_offline(device, dict(_OFFLINE_STATUS))

        self.session.device_recovered(device.device_id)
        _LOGGER.debug("get_climate - Updating climate data for %s.", device.ha_name)
        data = self.session.data.devices[device.device_id]
        device.min_temp = MIN_TARGET_TEMPERATURE
        device.max_temp = MAX_TARGET_TEMPERATURE
        device.status = {
            "current_temperature": await self.get_current_temperature(device),
            "target_temperature": await self.get_target_temperature(device),
            "action": await self.get_current_operation(device),
            "mode": await self.get_mode(device),
            "boost": await self.get_boost_status(device),
        }
        self._apply_device_props(device, data, online=online)
        _LOGGER.debug(
            "get_climate - Heating device data for %s: %s",
            device.ha_name,
            device.status,
        )
        return self.session.set_cached_device(device)
