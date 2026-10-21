"""Hive Hotwater Module."""

import logging

from ..helper.compat_aliases import WaterHeaterCompatMixin
from ..helper.const import HIVETOHA
from ..helper.device_handler_base import BaseDeviceHandler
from ..helper.hivedataclasses import Device
from .boost import BoostMixin

_LOGGER = logging.getLogger(__name__)


class HiveHotwater(BoostMixin, BaseDeviceHandler):
    """Hive Hotwater Code.

    Returns:
        object: Hotwater Object.
    """

    hotwater_type = "Hotwater"

    async def get_mode(self, device: Device):
        """Get hotwater current mode.

        Args:
            device (dict): Device to get the mode for.

        Returns:
            str: Return mode.
        """
        state = self._get_product_state(device, "state", "mode")
        if state is None:
            _LOGGER.debug("get_mode - No mode in state for %s.", device.ha_name)
            return None
        if state == "BOOST":
            state = self._get_product_state(device, "props", "previous", "mode")
        return HIVETOHA[self.hotwater_type].get(state, state)

    @staticmethod
    async def get_operation_modes():
        """Get hot water list of possible modes.

        Returns:
            list: Return list of operation modes.
        """
        return ["SCHEDULE", "ON", "OFF"]

    async def get_state(self, device: Device):
        """Get hot water current state.

        Args:
            device (dict): Device to get the state for.

        Returns:
            str: return state of device.
        """
        try:
            data = self.session.data.products[device.hive_id]
            state = data["state"]["status"]
            if await self.get_mode(device) == "SCHEDULE":
                if await self.get_boost_status(device) == "ON":
                    state = "ON"
                else:
                    now_next_later = self.session.helper.get_schedule_nnl(
                        data["state"]["schedule"]
                    )
                    if now_next_later and "now" in now_next_later:
                        state = now_next_later["now"]["value"]["status"]
            return HIVETOHA[self.hotwater_type].get(state, state)
        except KeyError as e:
            _LOGGER.error(e)
        return None

    async def set_mode(self, device: Device, new_mode: str):
        """Set hot water mode.

        Args:
            device (dict): device to update mode.
            new_mode (str): Mode to set the device to.

        Returns:
            boolean: return True/False if boost was successful.
        """
        _LOGGER.debug(
            "set_mode - Setting hot water mode to %s for %s.", new_mode, device.ha_name
        )
        return await self._execute_state_change(device, mode=new_mode)

    async def set_boost_on(self, device: Device, mins: int):
        """Turn hot water boost on.

        Args:
            device (dict): Deice to boost.
            mins (int): Number of minutes to boost it for.

        Returns:
            boolean: return True/False if boost was successful.
        """
        if int(mins) <= 0:
            return False
        _LOGGER.debug(
            "set_boost_on - Setting hot water boost ON for %s: %s mins.",
            device.ha_name,
            mins,
        )
        return await self._execute_state_change(device, mode="BOOST", boost=mins)

    async def set_boost_off(self, device: Device):
        """Turn hot water boost off.

        Args:
            device (dict): device to set boost off

        Returns:
            boolean: return True/False if boost was successful.
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


class WaterHeater(WaterHeaterCompatMixin, HiveHotwater):
    """Water heater class.

    Args:
        Hotwater (object): Hotwater class.
    """

    async def get_water_heater(self, device: Device):
        """Update water heater device.

        Args:
            device (dict): device to update.

        Returns:
            dict: Updated device.
        """
        cached = self._cached_if_busy(device)
        if cached is not None:
            _LOGGER.debug(
                "get_water_heater - Returning cached state for"
                " water heater %s (slow/busy poll).",
                device.ha_name,
            )
            return cached
        online = await self._update_online_status(device)
        if not online:
            return await self._report_offline(device, {"current_operation": None})

        self.session.helper.device_recovered(device.device_id)
        _LOGGER.debug(
            "get_water_heater - Updating hot water data for %s.", device.ha_name
        )
        data = self.session.data.devices[device.device_id]
        device.status = {"current_operation": await self.get_mode(device)}
        self._apply_device_props(device, data, online)
        device.attributes = await self.session.attr.state_attributes(
            device.device_id, device.hive_type
        )
        _LOGGER.debug(
            "get_water_heater - Water heater device data for %s: %s",
            device.ha_name,
            device.status,
        )
        return self.session.set_cached_device(device)

    async def get_schedule_now_next_later(self, device: Device):
        """Hive get hotwater schedule now, next and later.

        Args:
            device (dict): device to get schedule for.

        Returns:
            dict: return now, next and later schedule.
        """
        mode_current = await self.get_mode(device)
        if mode_current == "SCHEDULE":
            schedule = self._get_product_state(device, "state", "schedule")
            if schedule is not None:
                return self.session.helper.get_schedule_nnl(schedule)
        return None
