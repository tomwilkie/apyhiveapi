"""Shared plumbing for the climate and water heater handlers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from apyhiveapi.exceptions import HiveDeviceOffline

if TYPE_CHECKING:
    from apyhiveapi.api import JsonValue
    from apyhiveapi.hive import Hive
    from apyhiveapi.models import Device

_LOGGER = logging.getLogger(__name__)


class DeviceHandler:
    """Common plumbing shared by the climate and water heater handlers.

    Readers (``get_*``) never raise: an unknown value is reported as ``None``.
    Commands (``set_*``) raise a :class:`~apyhiveapi.exceptions.HiveError`
    when the change could not be made.
    """

    def __init__(self, session: Hive) -> None:
        """Remember the session whose data and API the handler works with."""
        self.session = session

    def _get_product_state(
        self,
        device: Device,
        *path_keys: str,
        default: Any = None,  # noqa: ANN401 - caller chooses the fallback
    ) -> Any:  # noqa: ANN401 - product state is untyped JSON
        """Read a nested value from the device's product data.

        Returns *default* if any key in *path_keys* is missing rather than
        raising KeyError.
        """
        try:
            node = self.session.data.products[device.hive_id]
            for key in path_keys:
                node = node[key]
        except (KeyError, TypeError):
            return default
        return node

    @staticmethod
    def _as_float(value: Any) -> float | None:  # noqa: ANN401 - untyped JSON value
        """Return *value* as a float, or None when it is missing or not numeric."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _is_online(self, device: Device) -> bool:
        """Return True if the product is known and reports itself online.

        Heating and hot water products report the receiver that runs them, so
        they stay online while a battery thermostat in the zone is not. A TRV
        control has no hardware of its own and is online only while its TRV is.
        """
        product = self.session.data.products.get(device.hive_id) or {}
        if not (product.get("props") or {}).get("online"):
            return False
        if product.get("type") != "trvcontrol":
            return True
        data = self.session.data.devices.get(device.device_id) or {}
        return bool((data.get("props") or {}).get("online"))

    def _refresh_device(self, device: Device) -> bool:
        """Update the device's hardware details and return whether it is online.

        Offline products are reported through the session, which logs each one
        once until it recovers.
        """
        data = self.session.data.devices.get(device.device_id) or {}
        online = self._is_online(device)
        device.device_data = {**(data.get("props") or {}), "online": online}
        if online:
            self.session.device_recovered(device.hive_id)
        else:
            self.session.report_device_offline(device.hive_id)
        return online

    def _previous_mode(self, device: Device) -> str | None:
        """Return the mode a boost will return to, or None when Hive did not say.

        Hive reports a previous mode of "off" as ``false``.
        """
        mode = self._get_product_state(device, "props", "previous", "mode")
        if mode is False:
            return "OFF"
        return mode if isinstance(mode, str) else None

    def _boost_status(self, device: Device) -> str | None:
        """Return ``"ON"`` or ``"OFF"`` for the device's boost, or None when unknown."""
        state = self._get_product_state(device, "state")
        if not isinstance(state, dict):
            return None
        return "ON" if state.get("boost") else "OFF"

    async def _set_state(self, device: Device, **state: JsonValue) -> None:
        """Change the device's product state on Hive and poll for the result.

        Raises:
            HiveDeviceOffline: The device is offline or no longer on the account.
            HiveReauthRequired: The user has to log in again.
            HiveConnectionError: Hive could not be reached.
            HiveApiError: Hive refused the change.
        """
        if not self._is_online(device):
            raise HiveDeviceOffline(f"{device.ha_name} is offline")
        _LOGGER.debug("Setting %s: %s", device.ha_name, state)
        await self.session.set_state(device.hive_type, device.hive_id, **state)
        await self.session.get_devices()
