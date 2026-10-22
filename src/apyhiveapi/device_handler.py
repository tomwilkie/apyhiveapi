"""Shared plumbing for the climate and water heater handlers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from apyhiveapi.const import HTTP_OK

if TYPE_CHECKING:
    from apyhiveapi.api import JsonValue
    from apyhiveapi.hive import Hive
    from apyhiveapi.models import Device

_LOGGER = logging.getLogger(__name__)


class DeviceHandler:
    """Common plumbing shared by the climate and water heater handlers."""

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

    def _online(self, device_id: str) -> bool | None:
        """Return whether the Hive device is online, or None when unknown."""
        try:
            online: bool = self.session.data.devices[device_id]["props"]["online"]
        except KeyError as err:
            _LOGGER.warning("Online state unknown for %s: missing %s", device_id, err)
            return None
        return online

    def _update_online_status(self, device: Device) -> bool | None:
        """Store whether the device is online in ``device_data`` and return it."""
        online = self._online(device.device_id)
        if not isinstance(device.device_data, dict):
            device.device_data = {}
        device.device_data["online"] = online
        return online

    @staticmethod
    def _apply_device_props(
        device: Device, data: dict[str, Any], *, online: bool | None
    ) -> None:
        """Replace ``device_data`` with the latest props and set the parent."""
        props = data.get("props") or {}
        props["online"] = online
        device.device_data = props
        device.parent_device = data.get("parent")

    def _report_offline(self, device: Device, default_status: dict[str, Any]) -> Device:
        """Report an offline device and give it *default_status* if it has none."""
        self.session.report_device_offline(device.device_id)
        device.status = device.status or default_status
        return device

    async def _execute_state_change(
        self, device: Device, **state_kwargs: JsonValue
    ) -> bool:
        """Check online, refresh tokens, call set_state and re-poll.

        Returns True on HTTP 200, False on failure or when the device is
        unavailable. Uses the product type stored in session.data so callers
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
            await self.session.get_devices()
            return True
        _LOGGER.error(
            "_execute_state_change - set_state failed for %s: HTTP %s",
            device.ha_name,
            resp.get("original"),
        )
        return False

    async def get_boost_status(self, device: Device) -> str | None:
        """Return ``"ON"`` or ``"OFF"`` for the device's boost, or None on error."""
        try:
            state = self.session.data.products[device.hive_id]["state"]
        except KeyError as err:
            _LOGGER.warning(
                "get_boost_status - KeyError for %s: %s", device.ha_name, err
            )
            return None
        return "ON" if state.get("boost") else "OFF"
