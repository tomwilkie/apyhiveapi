"""Polling and entity-cache mixin for HiveSession."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from aiohttp.web import HTTPException

from ..helper.hive_exceptions import HiveApiError, HiveAuthError, HiveReauthRequired
from ..helper.hivedataclasses import Device

_LOGGER = logging.getLogger(__name__)

# Consecutive polls for which state keys missing from a response are carried
# over from the previous poll before the response is accepted as it is.
MAX_STALE_STATE_POLLS = 5

# State keys Hive always sends for a product that has them. Only these are
# carried over: other keys (``boost``, the colour keys) are legitimately
# omitted at times, so keeping their old value would report stale state.
REQUIRED_STATE_KEYS = frozenset({"mode", "name", "status", "target"})

# Shortest polling interval allowed, to stay within Hive's API rate limits.
MIN_SCAN_INTERVAL = timedelta(seconds=15)


class PollingMixin:
    """Device polling, rate-limiting, and entity-cache methods.

    Expects ``self.config``, ``self.tokens``, ``self.api``,
    ``self.update_lock``, ``self._update_task``,
    ``self._last_poll_slow``, ``self._slow_poll_threshold``, and
    ``self._stale_state_polls`` to be set up by the owning class's
    ``__init__``.
    """

    # Attributes provided by HiveSession.__init__
    config: Any
    tokens: Any
    api: Any
    data: Any
    entity_cache: dict
    update_lock: asyncio.Lock
    _update_task: asyncio.Task | None
    _last_poll_slow: bool
    _slow_poll_threshold: int
    _stale_state_polls: dict[str, int]

    @staticmethod
    def _entity_cache_key(device) -> str:
        """Build a stable cache key for an entity instance."""
        return "|".join(
            [
                str(getattr(device, "ha_type", "")),
                str(getattr(device, "hive_id", "")),
                str(getattr(device, "hive_type", "")),
            ]
        )

    def get_cached_device(self, device):
        """Get cached state for a specific entity."""
        return self.entity_cache.get(self._entity_cache_key(device))

    def set_cached_device(self, device):
        """Store device state in cache and return it."""
        self.entity_cache[self._entity_cache_key(device)] = device
        return device

    def should_use_cached_data(self):
        """Determine whether callers should use cached entity state.

        Returns:
            bool: True when the last poll was slow or another task is currently polling.
        """
        if self._last_poll_slow:
            return True
        if self.update_lock.locked():
            current_task = asyncio.current_task()
            return self._update_task is None or current_task is not self._update_task
        return False

    def _keep_missing_state(self, new_products: dict) -> None:
        """Carry over required state keys that a poll response has dropped.

        Hive sometimes answers a poll with products whose ``state`` block is
        missing keys it normally has. Keep the last known value for the keys
        in ``REQUIRED_STATE_KEYS``, for up to ``MAX_STALE_STATE_POLLS``
        consecutive polls.

        Args:
            new_products (dict): Products from the latest response, keyed by ID.
        """
        incomplete = []
        for product_id, product in new_products.items():
            previous = self.data.products.get(product_id) or {}
            previous_state = previous.get("state")
            if not isinstance(previous_state, dict):
                previous_state = {}
            new_state = product.get("state")
            if not isinstance(new_state, dict):
                new_state = {}
            missing = sorted(
                (previous_state.keys() & REQUIRED_STATE_KEYS) - new_state.keys()
            )
            if not missing:
                self._stale_state_polls.pop(product_id, None)
                continue
            name = previous_state.get("name", product_id)
            product_type = product.get("type")
            _LOGGER.debug(
                "Hive API state for %s %s arrived with keys: %s",
                product_type,
                product_id,
                ", ".join(sorted(new_state)),
            )
            polls = self._stale_state_polls.get(product_id, 0) + 1
            self._stale_state_polls[product_id] = polls
            if polls > MAX_STALE_STATE_POLLS:
                _LOGGER.warning(
                    "Hive API state for %s (%s %s) is still missing %s after %d "
                    "polls — no longer keeping the last known values.",
                    name,
                    product_type,
                    product_id,
                    ", ".join(missing),
                    MAX_STALE_STATE_POLLS,
                )
                continue
            product["state"] = {
                **{key: previous_state[key] for key in missing},
                **new_state,
            }
            incomplete.append(
                f"{name} ({product_type} {product_id}) missing {', '.join(missing)}"
            )
        for product_id in self._stale_state_polls.keys() - new_products.keys():
            del self._stale_state_polls[product_id]
        if incomplete:
            _LOGGER.warning(
                "Hive API returned incomplete state — keeping the last known "
                "values: %s",
                "; ".join(sorted(incomplete)),
            )

    async def update_interval(self, new_interval: int | float | timedelta) -> bool:
        """Set the polling interval.

        Args:
            new_interval: Seconds, or a timedelta. Values below 15 seconds are
                raised to 15 seconds.

        Returns:
            bool: True once the interval is set.
        """
        if not isinstance(new_interval, timedelta):
            new_interval = timedelta(seconds=new_interval)
        self.config.scan_interval = max(new_interval, MIN_SCAN_INTERVAL)
        return True

    async def _poll_devices(self) -> bool:
        """Fetch latest device state from the Hive API."""
        return await self.get_devices("No_ID")

    async def update_data(self, _device: Device):
        """Get latest data for Hive nodes - rate limiting.

        Args:
            _device (Device): Device requesting the update.

        Returns:
            boolean: True/False if update was successful
        """
        if datetime.now() < self.config.last_update + self.config.scan_interval:
            return False
        current_task = asyncio.current_task()
        if self.update_lock.locked() and (
            self._update_task is None or current_task is not self._update_task
        ):
            _LOGGER.debug("update_data - Update poll already in progress")
            return False
        async with self.update_lock:
            # Re-check after acquiring lock — another caller may have already updated
            if datetime.now() < self.config.last_update + self.config.scan_interval:
                return False
            self._update_task = current_task
            try:
                _LOGGER.debug("Polling Hive API for device updates.")
                updated = await self._poll_devices()
                if updated:
                    _LOGGER.debug("update_data - Device update completed successfully.")
                else:
                    _LOGGER.debug(
                        "update_data - Device update failed, will retry after scan interval."
                    )
            finally:
                if self._update_task is current_task:
                    self._update_task = None
        return updated

    async def _fetch_all_nodes(self) -> dict | None:
        """Refresh tokens, then fetch every product, device and action from the API.

        Re-logs in and retries when the API rejects the token, and records
        whether the call was slow.

        Raises:
            HTTPException: The API answered with a non-2xx status.
            HiveApiError: The API answered without a parsed body.

        Returns:
            dict | None: The ``{"original": ..., "parsed": ...}`` response.
        """
        _LOGGER.debug("get_devices - Refreshing tokens before fetching devices.")
        await self.hive_refresh_tokens()  # type: ignore[attr-defined]
        _LOGGER.debug("get_devices - Fetching all devices from Hive API.")
        api_call_start = time.monotonic()
        try:
            api_resp_d = await self.api.get_all()
            api_call_duration = time.monotonic() - api_call_start
            if api_call_duration > self._slow_poll_threshold:
                _LOGGER.debug(
                    "get_devices - Hive API response took %.1fs — marking poll as slow.",
                    api_call_duration,
                )
                self._last_poll_slow = True
            else:
                self._last_poll_slow = False
        except HiveAuthError:
            self._last_poll_slow = False
            _LOGGER.warning(
                "Auth error (401/403) after token refresh, "
                "falling back to full device re-login."
            )
            await self._retry_login()  # type: ignore[attr-defined]
            api_resp_d = await self._retry_with_backoff(  # type: ignore[attr-defined]
                self.api.get_all,
                reraise_as=HiveReauthRequired,
            )
        if api_resp_d is None:
            return None
        if not str(api_resp_d["original"]).startswith("2"):
            raise HTTPException
        if api_resp_d["parsed"] is None:
            raise HiveApiError
        return api_resp_d

    def _store_nodes(self, nodes: Any) -> None:
        """Store the user, home, products, devices and actions from a poll.

        Args:
            nodes: The parsed JSON body of the all-nodes response.
        """
        products = {}
        devices = {}
        actions = {}
        for key in nodes:
            if key == "user":
                self.data.user = nodes[key]
                self.config.user_id = nodes[key].get("id")
            elif key == "products":
                for product in nodes[key]:
                    products[product["id"]] = product
            elif key == "devices":
                for device in nodes[key]:
                    devices[device["id"]] = device
            elif key == "actions":
                for action in nodes[key]:
                    actions[action["id"]] = action
            elif key == "homes":
                homes_data = nodes[key]
                if isinstance(homes_data, dict) and homes_data.get("homes"):
                    self.config.home_id = homes_data["homes"][0]["id"]

        _LOGGER.debug(
            "get_devices - API returned %d products, %d devices, %d actions.",
            len(products),
            len(devices),
            len(actions),
        )
        if products:
            self._keep_missing_state(products)
            self.data.products = products
        if devices:
            self.data.devices = devices
        self.data.actions = actions

    async def get_devices(self, _n_id: str):
        """Get latest data for Hive nodes.

        Args:
            _n_id (str): ID of the device requesting data.

        Raises:
            HTTPException: HTTP error has occurred updating the devices.
            HiveApiError: An API error code has been returned.

        Returns:
            boolean: True/False if update was successful.
        """
        try:
            if self.config.file:
                _LOGGER.debug("get_devices - Loading device data from file.")
                api_resp_d = self.open_file("data.json")  # type: ignore[attr-defined]
            elif self.tokens is not None:
                api_resp_d = await self._fetch_all_nodes()
            else:
                api_resp_d = None
            if api_resp_d is None:
                return False
            self._store_nodes(api_resp_d["parsed"])
            self.config.last_update = datetime.now()
            return True
        except HiveReauthRequired:
            _LOGGER.error("Reauthentication required, propagating to caller.")
            self.config.last_update = datetime.now()
            raise
        except asyncio.TimeoutError:
            _LOGGER.warning("Hive API request timed out — keeping cached device data.")
            self._last_poll_slow = True
            self.config.last_update = (
                datetime.now() - self.config.scan_interval + timedelta(seconds=30)
            )
            return False
        except (
            OSError,
            RuntimeError,
            HiveApiError,
            ConnectionError,
            HTTPException,
            KeyError,
        ) as err:
            _LOGGER.error("Failed to fetch devices: %s", err)
            self.config.last_update = (
                datetime.now() - self.config.scan_interval + timedelta(seconds=30)
            )
            return False
