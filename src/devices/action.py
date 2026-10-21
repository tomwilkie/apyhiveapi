"""Hive Action Module."""

import json
import logging

from ..helper.compat_aliases import ActionCompatMixin
from ..helper.const import HTTP_OK
from ..helper.device_handler_base import BaseDeviceHandler
from ..helper.hivedataclasses import Device

_LOGGER = logging.getLogger(__name__)


class HiveAction(ActionCompatMixin, BaseDeviceHandler):
    """Hive Action Code.

    Returns:
        object: Return hive action object.
    """

    action_type = "Actions"

    async def get_action(self, device: Device):
        """Action device to update.

        Args:
            device (dict): Device to be updated.

        Returns:
            dict: Updated device.
        """
        cached = self._cached_if_busy(device)
        if cached is not None:
            _LOGGER.debug(
                "Returning cached state for action %s (slow/busy poll).",
                device.ha_name,
            )
            return cached
        if device.hive_id in self.session.data.actions:
            device.status = {"state": await self.get_state(device)}
            device.device_data = {}
            return self.session.set_cached_device(device)
        return "REMOVE"

    async def get_state(self, device: Device):
        """Get action state.

        Args:
            device (dict): Device to get state of.

        Returns:
            str: Return state.
        """
        try:
            return self.session.data.actions[device.hive_id]["enabled"]
        except KeyError as e:
            _LOGGER.error(e)
        return None

    async def _set_action_state(self, device: Device, enabled: bool) -> bool:
        """Set action enabled/disabled state.

        Args:
            device (dict): Device to set state of.
            enabled (bool): True to enable, False to disable.

        Returns:
            bool: True if successful.
        """
        if device.hive_id not in self.session.data.actions:
            return False

        _LOGGER.debug(
            "%s action %s.",
            "Enabling" if enabled else "Disabling",
            device.ha_name,
        )
        await self.session.hive_refresh_tokens()
        data = self.session.data.actions[device.hive_id].copy()
        data["enabled"] = enabled
        resp = await self.session.api.set_action(device.hive_id, json.dumps(data))
        if resp["original"] != HTTP_OK:
            return False
        await self.session.get_devices(device.hive_id)
        return True

    async def set_status_on(self, device: Device):
        """Set action turn on.

        Args:
            device (dict): Device to set state of.

        Returns:
            bool: True if successful.
        """
        return await self._set_action_state(device, True)

    async def set_status_off(self, device: Device):
        """Set action to turn off.

        Args:
            device (dict): Device to set state of.

        Returns:
            bool: True if successful.
        """
        return await self._set_action_state(device, False)
