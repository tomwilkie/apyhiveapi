"""Shared base class for all Hive device handlers."""

import logging
from typing import Any

from .const import HIVETOHA, HTTP_OK
from .hivedataclasses import Device

_LOGGER = logging.getLogger(__name__)


class BaseDeviceHandler:  # pylint: disable=too-few-public-methods
    """Common plumbing shared across all Hive device handler classes."""

    def __init__(self, session: Any = None):
        """Initialise the handler.

        Args:
            session (object, optional): Session to interact with the Hive account.
                Defaults to None.
        """
        self.session = session

    def _is_available(self, device: Device) -> bool:
        """Return True if the device's product is known and the device is online."""
        return device.hive_id in self.session.data.products and bool(
            isinstance(device.device_data, dict) and device.device_data.get("online")
        )

    def _cached_if_busy(self, device: Device) -> Device | None:
        """Return the cached entity while the session prefers cached state.

        The session prefers it while the last poll was slow or another task
        is polling; otherwise this returns None.
        """
        if self.session.should_use_cached_data():
            return self.session.get_cached_device(device)
        return None

    async def _update_online_status(self, device: Device) -> Any:
        """Store whether the device is online in ``device_data`` and return it."""
        online = await self.session.attr.online_offline(device.device_id)
        if not isinstance(device.device_data, dict):
            device.device_data = {}
        device.device_data["online"] = online
        return online

    @staticmethod
    def _apply_device_props(device: Device, data: dict, online: Any) -> None:
        """Replace ``device_data`` with the latest props and set the parent."""
        props = data.get("props") or {}
        props["online"] = online
        device.device_data = props
        device.parent_device = data.get("parent", None)

    async def _report_offline(self, device: Device, default_status: dict) -> Device:
        """Report an offline device and give it *default_status* if it has none."""
        await self.session.helper.error_check(
            device.device_id, "ERROR", device.device_data["online"]
        )
        device.status = device.status or default_status
        return device

    async def _execute_state_change(self, device: Device, **state_kwargs) -> bool:
        """Check online → refresh tokens → set_state → get_devices.

        Returns True on HTTP 200, False on failure or when device is
        unavailable.  Uses the product type stored in session.data so callers
        never need to read it themselves.
        """
        if device.hive_id not in self.session.data.products:
            _LOGGER.debug(
                "_execute_state_change - %s not found in products", device.ha_name
            )
            return False
        if not (
            isinstance(device.device_data, dict) and device.device_data.get("online")
        ):
            _LOGGER.debug(
                "_execute_state_change - %s is offline or device_data not initialised",
                device.ha_name,
            )
            return False
        await self.session.hive_refresh_tokens()
        data = self.session.data.products[device.hive_id]
        try:
            resp = await self.session.api.set_state(
                data["type"], device.hive_id, **state_kwargs
            )
        except TimeoutError:
            # Honour the documented False-on-failure contract so one slow
            # device doesn't abort a multi-entity service call.
            _LOGGER.warning(
                "_execute_state_change - set_state timed out for %s", device.ha_name
            )
            return False
        if resp.get("original") == HTTP_OK:
            await self.session.get_devices(device.hive_id)
            return True
        _LOGGER.error(
            "_execute_state_change - set_state failed for %s: HTTP %s",
            device.ha_name,
            resp.get("original"),
        )
        return False

    def _get_product_state(self, device: Device, *path_keys, default=None):
        """Read a nested value from session.data.products[device.hive_id].

        Returns *default* (None by default) if any key in *path_keys* is
        missing rather than raising KeyError.
        """
        try:
            node = self.session.data.products[device.hive_id]
            for key in path_keys:
                node = node[key]
            return node
        except (KeyError, TypeError):
            return default

    def _map_hive_to_ha(self, mapping_key: str, value, fallback=None):
        """Translate a raw Hive API value through HIVETOHA[mapping_key].

        Returns *fallback* when the key is absent from the mapping, or the
        original *value* when *fallback* is None.
        """
        mapping = HIVETOHA.get(mapping_key, {})
        if value in mapping:
            return mapping[value]
        return fallback if fallback is not None else value
