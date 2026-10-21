"""Hive Heating Module."""

import logging

from ..helper.const import HIVETOHA
from ..helper.device_handler_base import BaseDeviceHandler
from ..helper.hivedataclasses import Device
from .boost import BoostMixin

_LOGGER = logging.getLogger(__name__)


class HiveHeating(BoostMixin, BaseDeviceHandler):
    """Hive Heating Code.

    Returns:
        object: heating
    """

    heating_type = "Heating"

    async def get_min_temperature(self, device: Device):
        """Get heating minimum target temperature.

        Args:
            device (dict): Device to get min temp for.

        Returns:
            int: Minimum temperature
        """
        if device.hive_type == "nathermostat":
            return self._get_product_state(device, "props", "minHeat")
        return 5

    async def get_max_temperature(self, device: Device):
        """Get heating maximum target temperature.

        Args:
            device (dict): Device to get max temp for.

        Returns:
            int: Maximum temperature
        """
        if device.hive_type == "nathermostat":
            return self._get_product_state(device, "props", "maxHeat")
        return 32

    async def get_current_temperature(self, device: Device):
        """Get heating current temperature.

        Args:
            device (dict): Device to get current temperature for.

        Returns:
            float: current temperature
        """
        try:
            data = self.session.data.products[device.hive_id]
            temperature = data["props"]["temperature"]
            try:
                temperature = float(temperature)
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "get_current_temperature - Non-numeric temperature value '%s' for %s.",
                    temperature,
                    device.ha_name,
                )
                return None
            return round(temperature, 1)
        except KeyError as e:
            _LOGGER.error(
                "get_current_temperature - KeyError getting temperature for %s: %s",
                device.ha_name,
                str(e),
            )
        return None

    async def get_target_temperature(self, device: Device):
        """Get heating target temperature.

        Args:
            device (dict): Device to get target temperature for.

        Returns:
            float: Target temperature or None if invalid
        """
        try:
            data = self.session.data.products[device.hive_id]
            target = data["state"].get("target")
            if target is None:
                target = data["state"].get("heat")
        except (KeyError, TypeError) as e:
            _LOGGER.error(
                "get_target_temperature - Error getting target temperature for %s: %s",
                device.ha_name,
                str(e),
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

    async def get_mode(self, device: Device):
        """Get heating current mode.

        Args:
            device (dict): Device to get current mode for.

        Returns:
            str: Current Mode
        """
        state = self._get_product_state(device, "state", "mode")
        if state is None:
            _LOGGER.debug("get_mode - No mode in state for %s.", device.ha_name)
            return None
        if state == "BOOST":
            state = self._get_product_state(device, "props", "previous", "mode")
        return HIVETOHA[self.heating_type].get(state, state)

    async def get_state(self, device: Device):
        """Get heating current state.

        Args:
            device (dict): Device to get state for.

        Returns:
            str: Current state.
        """
        try:
            current_temp = await self.get_current_temperature(device)
            target_temp = await self.get_target_temperature(device)
            if current_temp is not None and target_temp is not None:
                return "ON" if current_temp < target_temp else "OFF"
        except (KeyError, TypeError) as e:
            _LOGGER.error(e)
        return None

    async def get_current_operation(self, device: Device):
        """Get heating current operation.

        Args:
            device (dict): Device to get current operation for.

        Returns:
            str: Current operation.
        """
        return self._get_product_state(device, "props", "working")

    async def get_heat_on_demand(self, device: Device):
        """Get heat on demand status.

        Args:
            device ([dictionary]): [Get Heat on Demand status for Thermostat device.]

        Returns:
            str: [Return True or False for the Heat on Demand status.]
        """
        return self._get_product_state(device, "props", "autoBoost", "active")

    async def set_target_temperature(self, device: Device, new_temp: str):
        """Set heating target temperature.

        Args:
            device (dict): Device to set target temperature for.
            new_temp (str): New temperature.

        Returns:
            boolean: True/False if successful
        """
        _LOGGER.info(
            "set_target_temperature - Setting target temperature to %s°C for %s",
            new_temp,
            device.ha_name,
        )
        return await self._execute_state_change(device, target=new_temp)

    async def set_mode(self, device: Device, new_mode: str):
        """Set heating mode.

        Args:
            device (dict): Device to set mode for.
            new_mode (str): New mode to be set.

        Returns:
            boolean: True/False if successful
        """
        _LOGGER.info(
            "set_mode - Setting heating mode to %s for %s", new_mode, device.ha_name
        )
        return await self._execute_state_change(device, mode=new_mode)

    async def set_boost_on(self, device: Device, mins: str, temp: float):
        """Turn heating boost on.

        Args:
            device (dict): Device to boost.
            mins (str): Number of minutes to boost for.
            temp (float): Temperature to boost to.

        Returns:
            boolean: True/False if successful, None if inputs are out of range
        """
        min_temp = await self.get_min_temperature(device)
        max_temp = await self.get_max_temperature(device)
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
        if not (mins_value > 0 and min_temp <= temp_value <= max_temp):
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

    async def set_boost_off(self, device: Device):
        """Turn heating boost off.

        Args:
            device (dict): Device to update boost for.

        Returns:
            boolean: True/False if successful
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
        kwargs = {"mode": prev_mode}
        if prev_mode in ("MANUAL", "OFF"):
            kwargs["target"] = (
                self._get_product_state(device, "props", "previous", "target") or 7
            )
        return await self._execute_state_change(device, **kwargs)

    async def set_heat_on_demand(self, device: Device, state: str):
        """Enable or disable Heat on Demand for a Thermostat.

        Args:
            device ([dictionary]): [This is the Thermostat device you want to update.]
            state ([str]): [This is the state you want to set. (Either "ENABLED" or "DISABLED")]

        Returns:
            [boolean]: [Return True or False if the Heat on Demand was set successfully.]
        """
        _LOGGER.debug(
            "set_heat_on_demand - Setting heat on demand to %s for %s.",
            state,
            device.ha_name,
        )
        return await self._execute_state_change(device, autoBoost=state)


class Climate(HiveHeating):
    """Climate class for Home Assistant.

    Args:
        Heating (object): Heating class
    """

    async def get_climate(self, device: Device):
        """Get heating data.

        Args:
            device (dict): Device to update.

        Returns:
            dict: Updated device.
        """
        cached = self._cached_if_busy(device)
        if cached is not None:
            _LOGGER.debug(
                "get_climate - Returning cached state for climate %s (slow/busy poll).",
                device.ha_name,
            )
            return cached
        online = await self._update_online_status(device)
        if not online:
            return await self._report_offline(
                device,
                {
                    "current_temperature": None,
                    "target_temperature": None,
                    "action": None,
                    "mode": None,
                    "boost": None,
                    "state": None,
                },
            )

        self.session.helper.device_recovered(device.device_id)
        _LOGGER.debug("get_climate - Updating climate data for %s.", device.ha_name)
        data = self.session.data.devices[device.device_id]
        device.min_temp = await self.get_min_temperature(device)
        device.max_temp = await self.get_max_temperature(device)
        device.status = {
            "current_temperature": await self.get_current_temperature(device),
            "target_temperature": await self.get_target_temperature(device),
            "action": await self.get_current_operation(device),
            "mode": await self.get_mode(device),
            "boost": await self.get_boost_status(device),
        }
        self._apply_device_props(device, data, online)
        device.attributes = await self.session.attr.state_attributes(
            device.device_id, device.hive_type
        )
        _LOGGER.debug(
            "get_climate - Heating device data for %s: %s",
            device.ha_name,
            device.status,
        )
        return self.session.set_cached_device(device)
