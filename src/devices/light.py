"""Hive Light Module."""

import logging

from ..helper.const import HIVETOHA
from ..helper.device_handler_base import BaseDeviceHandler
from ..helper.hivedataclasses import Device
from .color import LightColorHandler

_LOGGER = logging.getLogger(__name__)


class HiveLight(LightColorHandler, BaseDeviceHandler):
    """Hive Light Code.

    Returns:
        object: Hivelight
    """

    light_type = "Light"

    async def get_state(self, device: Device):
        """Get light current state.

        Args:
            device (dict): Device to get the state of.

        Returns:
            str: State of the light.
        """
        try:
            state = self.session.data.products[device.hive_id]["state"]["status"]
            return HIVETOHA[self.light_type].get(state, state)
        except KeyError as e:
            _LOGGER.error(
                "KeyError getting light state for %s: %s", device.ha_name, str(e)
            )
        return None

    async def get_brightness(self, device: Device):
        """Get light current brightness.

        Args:
            device (dict): Device to get the brightness of.

        Returns:
            int: Brightness value.
        """
        try:
            data = self.session.data.products[device.hive_id]
            return int((data["state"]["brightness"] / 100) * 255)
        except (KeyError, TypeError) as e:
            _LOGGER.error(
                "Error getting light brightness for %s: %s", device.ha_name, str(e)
            )
        return None

    async def set_status_off(self, device: Device):
        """Set light to turn off.

        Args:
            device (dict): Device to turn off.

        Returns:
            boolean: True/False if successful
        """
        _LOGGER.info("Turning off light %s", device.ha_name)
        return await self._execute_state_change(device, status="OFF")

    async def set_status_on(self, device: Device):
        """Set light to turn on.

        Args:
            device (dict): Device to turn on.

        Returns:
            boolean: True/False if successful
        """
        _LOGGER.info("Turning on light %s", device.ha_name)
        return await self._execute_state_change(device, status="ON")

    async def set_brightness(self, device: Device, n_brightness: int):
        """Set brightness of the light.

        Args:
            device (dict): Device to set brightness of.
            n_brightness (int): Brightness value to set the light to.

        Returns:
            boolean: True/False if successful
        """
        _LOGGER.info(
            "Setting brightness to %s for light %s", n_brightness, device.ha_name
        )
        return await self._execute_state_change(
            device, status="ON", brightness=n_brightness
        )


class Light(HiveLight):
    """Home Assistant Light Code.

    Args:
        HiveLight (object): HiveLight Code.
    """

    async def get_light(self, device: Device):
        """Get light data.

        Args:
            device (dict): Device to update.

        Returns:
            dict: Updated device.
        """
        cached = self._cached_if_busy(device)
        if cached is not None:
            _LOGGER.debug(
                "get_light - Returning cached state for light %s (slow/busy poll).",
                device.ha_name,
            )
            return cached
        online = await self._update_online_status(device)
        if not online:
            return await self._report_offline(device, {"state": None})

        self.session.helper.device_recovered(device.device_id)
        _LOGGER.debug("get_light - Updating light data for %s.", device.ha_name)
        data = self.session.data.devices[device.device_id]
        device.status = {
            "state": await self.get_state(device),
            "brightness": await self.get_brightness(device),
        }
        self._apply_device_props(device, data, online)
        device.attributes = await self.session.attr.state_attributes(
            device.device_id, device.hive_type
        )

        if device.hive_type in ("tuneablelight", "colourtuneablelight"):
            device.status["color_temp"] = await self.get_color_temp(device)
        if device.hive_type == "colourtuneablelight":
            mode = await self.get_color_mode(device)
            device.status["mode"] = mode
            if mode == "COLOUR":
                device.status["hs_color"] = await self.get_color(device)

        _LOGGER.debug(
            "get_light - Light device data for %s: %s",
            device.ha_name,
            device.status,
        )
        return self.session.set_cached_device(device)

    async def turn_on(
        self,
        device: Device,
        brightness: int | None,
        color_temp: int | None,
        color: list | None,
    ):
        """Set light to turn on.

        Args:
            device (dict): Device to turn on
            brightness (int): Brightness value to set the light to.
            color_temp (int): Color Temp value to set the light to.
            color (list): colour values to set the light to.

        Returns:
            boolean: True/False if successful.
        """
        if brightness is not None:
            return await self.set_brightness(device, brightness)
        if color_temp is not None:
            return await self.set_color_temp(device, color_temp)
        if color is not None:
            return await self.set_color(device, color)

        return await self.set_status_on(device)

    async def turn_off(self, device: Device):
        """Set light to turn off.

        Args:
            device (dict): Device to be turned off.

        Returns:
            boolean: True/False if successful.
        """
        return await self.set_status_off(device)
