"""The Hive session: login, token refresh, polling and device discovery."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from apyhiveapi.api import HiveApi
from apyhiveapi.auth import HiveAuth
from apyhiveapi.const import PRODUCT_ENTITY_TYPES, THERMOSTAT_DEVICE_TYPES
from apyhiveapi.exceptions import (
    HiveApiError,
    HiveConnectionError,
    HiveError,
    HiveReauthRequired,
)
from apyhiveapi.heating import Climate
from apyhiveapi.helpers import sanitize_payload
from apyhiveapi.hotwater import WaterHeater
from apyhiveapi.models import Device, SessionConfig, SessionData, SessionTokens

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from aiohttp import ClientSession

    from apyhiveapi.api import JsonValue

_LOGGER = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent / "data"

# Consecutive polls for which state keys missing from a response are carried
# over from the previous poll before the response is accepted as it is.
MAX_STALE_STATE_POLLS = 5

# State keys Hive always sends for a product that has them. Only these are
# carried over: other keys (such as ``boost``) are legitimately omitted at
# times, so keeping their old value would report stale state.
REQUIRED_STATE_KEYS = frozenset({"mode", "name", "status", "target"})

# Shortest polling interval allowed, to stay within Hive's API rate limits.
MIN_SCAN_INTERVAL = timedelta(seconds=15)

# Tokens are refreshed once this share of their lifetime has passed.
REFRESH_THRESHOLD = 0.90

# How long after a failed poll the next attempt may run.
POLL_RETRY_DELAY = timedelta(seconds=30)

# device_data holds device_group_key, device_key and device_password. Entries
# saved by older versions add the token issue time, which is ignored.
_DEVICE_DATA_FIELDS = 3


def _display_name(name: str) -> str:
    """Return the name to show; Hive calls a heating-only thermostat "Receiver"."""
    return "Heating" if name == "Receiver" else name


class Hive:
    """A Hive account session.

    Create one per account, call :meth:`start_session` with the saved
    tokens and device credentials, then poll with :meth:`update_data` and
    read entity state through :attr:`heating` and :attr:`hotwater`.

    Every error raised is a :class:`~apyhiveapi.exceptions.HiveError`:
    :class:`~apyhiveapi.exceptions.HiveReauthRequired` when the user has to
    log in again, :class:`~apyhiveapi.exceptions.HiveConnectionError` for
    transient failures and :class:`~apyhiveapi.exceptions.HiveApiError` when
    Hive refuses a request.
    """

    def __init__(
        self,
        websession: ClientSession | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        """Set up the session.

        Args:
            websession: aiohttp session for API calls. Home Assistant passes
                its shared session; when omitted the session creates and owns
                one, closed by :meth:`close`.
            username: Hive account email. ``"use@file.com"`` switches to the
                bundled fixture data instead of the live API.
            password: Hive account password.
        """
        self.auth = HiveAuth(username or "", password or "", websession=websession)
        self._owns_websession = websession is None
        self.api = HiveApi(self, websession)
        self.tokens = SessionTokens()
        self.config = SessionConfig(username=username)
        self.data = SessionData()
        self.device_list: dict[str, list[Device]] = {}
        self.hub_id: str | None = None
        self.update_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._stale_state_polls: dict[str, int] = {}
        self._offline_devices: set[str] = set()
        # The login failure that needs the user, kept so later calls fail fast.
        self._reauth_error: HiveReauthRequired | None = None
        self.heating = Climate(self)
        self.hotwater = WaterHeater(self)

    async def close(self) -> None:
        """Close the aiohttp session if this object created it."""
        websession = self.api.websession
        if self._owns_websession and websession is not None and not websession.closed:
            await websession.close()

    async def __aenter__(self) -> Self:
        """Return the session for use in an ``async with`` block."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close the session on leaving the ``async with`` block."""
        await self.close()

    # --- Tokens and login --------------------------------------------------

    def update_tokens(
        self, tokens: dict[str, Any], *, update_expiry_time: bool = True
    ) -> None:
        """Store tokens from a Cognito response or a saved config entry.

        Args:
            tokens: Either a Cognito response with an ``AuthenticationResult``
                or a flat ``{"token": ..., "refreshToken": ..., "accessToken": ...}``.
            update_expiry_time: Whether the tokens count as freshly issued.
        """
        _LOGGER.debug("update_tokens - Input tokens: %s", sanitize_payload(tokens))
        if "AuthenticationResult" in tokens:
            data = tokens.get("AuthenticationResult") or {}
            mapping = {
                "IdToken": "token",
                "RefreshToken": "refreshToken",
                "AccessToken": "accessToken",
            }
        elif "token" in tokens:
            data = tokens
            mapping = {key: key for key in ("token", "refreshToken", "accessToken")}
        else:
            return
        stored = {
            target: data[source] for source, target in mapping.items() if source in data
        }
        self.tokens.token_data.update(stored)
        if stored:
            self._reauth_error = None
        if stored and update_expiry_time:
            self.tokens.token_created = datetime.now(UTC)
        if "ExpiresIn" in data:
            self.tokens.token_expiry = timedelta(seconds=data["ExpiresIn"])
        _LOGGER.debug(
            "Tokens updated, they expire at %s",
            self.tokens.token_created + self.tokens.token_expiry,
        )

    def _raise_if_reauth_required(self) -> None:
        """Fail fast once a login has shown that the user has to log in again.

        Logging in again would not help: Hive would send another SMS code, or
        count another wrong password towards locking the account. New tokens
        from :meth:`update_tokens` clear the failure.

        Raises:
            HiveReauthRequired: The earlier login failure.
        """
        if self._reauth_error is not None:
            raise self._reauth_error.with_traceback(None)

    async def login(self) -> None:
        """Log in with the stored username, password and device credentials.

        After a login fails with :class:`HiveReauthRequired`, later calls raise
        it again without contacting Cognito until new tokens are stored.

        Raises:
            HiveReauthRequired: Hive wants an SMS code, no longer trusts the
                saved device, or rejected the username or password.
            HiveConnectionError: Cognito could not be reached.
            HiveApiError: Cognito asked for a challenge this library cannot answer.
        """
        self._raise_if_reauth_required()
        try:
            await self._login()
        except HiveReauthRequired as err:
            self._reauth_error = err
            raise

    async def _login(self) -> None:
        """Run the Cognito login and store the tokens; see :meth:`login`."""
        result = await self.auth.login()
        if result.get("ChallengeName") == HiveAuth.DEVICE_VERIFIER_CHALLENGE:
            result = await self.auth.device_login()
        if result.get("ChallengeName") == HiveAuth.SMS_MFA_CHALLENGE:
            raise HiveReauthRequired("Hive asked for an SMS code")
        if not result.get("AuthenticationResult"):
            raise HiveApiError(f"Login did not complete: {result.get('ChallengeName')}")
        self.update_tokens(result)

    def _refresh_due(self) -> bool:
        """Return True once the tokens have used most of their lifetime."""
        expiry = (
            self.tokens.token_created + self.tokens.token_expiry * REFRESH_THRESHOLD
        )
        return datetime.now(UTC) >= expiry

    async def hive_refresh_tokens(self) -> None:
        """Refresh the tokens once most of their lifetime has passed.

        Logs in again with the saved device credentials when Cognito rejects
        the refresh token.

        Raises:
            HiveReauthRequired: The user has to log in again.
            HiveConnectionError: Cognito could not be reached.
            HiveApiError: Cognito refused the refresh for another reason.
        """
        self._raise_if_reauth_required()
        if self.config.file or not self._refresh_due():
            return
        async with self._refresh_lock:
            # Another caller may have refreshed while this one waited.
            if not self._refresh_due():
                return
            try:
                result = await self.auth.refresh_token(
                    self.tokens.token_data.get("refreshToken")
                )
            except HiveReauthRequired as err:
                _LOGGER.debug("Token refresh rejected (%s), logging in again", err)
                await self.login()
                return
            self.update_tokens(result)

    async def _api_call[T](self, call: Callable[[], Awaitable[T]]) -> T:
        """Call the API with fresh tokens, logging in again if Hive rejects them.

        Callers whose tokens are rejected at the same time log in once: the
        login is serialised through the refresh lock and skipped when another
        caller has already replaced the tokens.
        """
        await self.hive_refresh_tokens()
        issued = self.tokens.token_created
        try:
            return await call()
        except HiveReauthRequired as err:
            async with self._refresh_lock:
                if self.tokens.token_created == issued:
                    _LOGGER.debug("Hive rejected the token (%s), logging in again", err)
                    await self.login()
            return await call()

    async def set_state(
        self, product_type: str, product_id: str, **state: JsonValue
    ) -> None:
        """Change a product's state on Hive. Nothing is sent in file mode.

        Raises:
            HiveReauthRequired: The user has to log in again.
            HiveConnectionError: Hive could not be reached.
            HiveApiError: Hive refused the change.
        """
        if self.config.file:
            return
        await self._api_call(
            lambda: self.api.set_state(product_type, product_id, **state)
        )

    # --- Polling -----------------------------------------------------------

    def update_interval(self, new_interval: float | timedelta) -> None:
        """Set the polling interval.

        Args:
            new_interval: Seconds, or a timedelta. Values below
                ``MIN_SCAN_INTERVAL`` are raised to it.
        """
        if not isinstance(new_interval, timedelta):
            new_interval = timedelta(seconds=new_interval)
        self.config.scan_interval = max(new_interval, MIN_SCAN_INTERVAL)

    def _poll_is_due(self) -> bool:
        """Return True once the scan interval has passed since the last poll."""
        return datetime.now(UTC) >= self.config.last_update + self.config.scan_interval

    async def update_data(self) -> bool:
        """Poll Hive if the scan interval has passed since the last poll.

        Home Assistant calls this from every entity's update, so the poll runs
        at most once per interval across all entities and never concurrently.

        Returns:
            True if a poll ran and stored new data.

        Raises:
            HiveReauthRequired: The user has to log in again.
        """
        if not self._poll_is_due() or self.update_lock.locked():
            return False
        async with self.update_lock:
            return await self.get_devices()

    async def get_devices(self) -> bool:
        """Poll Hive now and store the latest products and devices.

        A failed poll keeps the last known state, is logged, and is retried
        after ``POLL_RETRY_DELAY`` rather than a full scan interval.

        Returns:
            True if new data was stored.

        Raises:
            HiveReauthRequired: The user has to log in again.
        """
        try:
            await self._poll()
        except HiveReauthRequired:
            self.config.last_update = datetime.now(UTC)
            raise
        except HiveError as err:
            _LOGGER.warning(
                "Could not poll Hive, keeping the last known state: %s", err
            )
            self._schedule_poll_retry()
            return False
        return True

    async def _poll(self) -> None:
        """Fetch and store the latest products and devices.

        Raises:
            HiveReauthRequired: The user has to log in again.
            HiveConnectionError: Hive could not be reached.
            HiveApiError: Hive refused the request.
        """
        if self.config.file:
            nodes = await asyncio.to_thread(self.open_file, "data.json")
        else:
            nodes = await self._api_call(self.api.get_all)
        self._store_nodes(nodes)
        self.config.last_update = datetime.now(UTC)

    def _store_nodes(self, nodes: dict[str, Any]) -> None:
        """Store the user, products and devices from a poll.

        Args:
            nodes: The parsed JSON body of the all-nodes response.
        """
        if "user" in nodes:
            self.data.user = nodes["user"]
        products = self._by_id(nodes.get("products", []))
        devices = self._by_id(nodes.get("devices", []))
        _LOGGER.debug(
            "Hive returned %d products and %d devices", len(products), len(devices)
        )
        if products:
            self._keep_missing_state(products)
            self.data.products = products
        if devices:
            self.data.devices = devices

    @staticmethod
    def _by_id(items: list[Any]) -> dict[str, Any]:
        """Key Hive nodes by ID, dropping anything that is not a node with an ID."""
        return {
            item["id"]: item
            for item in items
            if isinstance(item, dict) and "id" in item
        }

    def _schedule_poll_retry(self) -> None:
        """Allow the next poll after a short delay instead of a full interval."""
        self.config.last_update = (
            datetime.now(UTC) - self.config.scan_interval + POLL_RETRY_DELAY
        )

    def _keep_missing_state(self, new_products: dict[str, Any]) -> None:
        """Carry over required state keys that a poll response has dropped.

        Hive sometimes answers a poll with products whose ``state`` block is
        missing keys it normally has. Keep the last known value for the keys
        in ``REQUIRED_STATE_KEYS``, for up to ``MAX_STALE_STATE_POLLS``
        consecutive polls.

        Args:
            new_products: Products from the latest response, keyed by ID.
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
                    "polls - no longer keeping the last known values.",
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
                "Hive API returned incomplete state - keeping the last known "
                "values: %s",
                "; ".join(sorted(incomplete)),
            )

    # --- Session start and device discovery --------------------------------

    def open_file(self, file: str) -> dict[str, Any]:
        """Load a JSON fixture file from the package data directory.

        Args:
            file: Filename relative to the ``data/`` directory.

        Returns:
            Parsed JSON content of the file.
        """
        result: dict[str, Any] = json.loads(
            (_DATA_DIR / file).read_text(encoding="utf-8")
        )
        return result

    def _configure_file_mode(self, username: str | None) -> None:
        """Switch to the bundled fixture data for the magic testing username."""
        if username == "use@file.com":
            self.config.file = True

    def _apply_device_data(self, device_data: Sequence[Any]) -> None:
        """Load saved device credentials into the session.

        Args:
            device_data: ``(device_group_key, device_key, device_password)``.
                Any further values, as older versions saved, are ignored.

        Raises:
            HiveReauthRequired: Fewer than three values were given.
        """
        if len(device_data) < _DEVICE_DATA_FIELDS:
            raise HiveReauthRequired(
                "device_data must hold device_group_key, device_key and device_password"
            )
        self.auth.device_group_key = device_data[0]
        self.auth.device_key = device_data[1]
        self.auth.device_password = device_data[2]

    async def start_session(
        self, config: dict[str, Any] | None = None
    ) -> dict[str, list[Device]]:
        """Load the config, poll Hive once and build the device list.

        Args:
            config: ``username``, ``password``, ``tokens``, ``device_data`` and
                ``options.scan_interval`` from the Home Assistant config entry.

        Returns:
            Devices by Home Assistant entity type.

        Raises:
            HiveReauthRequired: The config has no tokens, or the user has to
                log in again.
            HiveConnectionError: Hive could not be reached or returned no
                products or devices.
            HiveApiError: Hive refused the request.
        """
        config = config or {}
        _LOGGER.debug("start_session - Config: %s", sanitize_payload(config))
        self._configure_file_mode(config.get("username", self.config.username))
        scan_interval = (config.get("options") or {}).get("scan_interval")
        if scan_interval is not None:
            self.update_interval(scan_interval)
        if not self.config.file:
            self._apply_config(config)
        await self._poll()
        if not self.data.devices or not self.data.products:
            # The library already treats gaps in a poll as transient, so an
            # empty answer is retried rather than failing the setup for good.
            raise HiveConnectionError(
                "Hive returned no products or devices for this account"
            )
        return self.create_devices()

    def _apply_config(self, config: dict[str, Any]) -> None:
        """Load the credentials, tokens and device data from a config entry.

        Raises:
            HiveReauthRequired: The config has no tokens.
        """
        if "tokens" not in config:
            raise HiveReauthRequired("The config has no tokens")
        self.update_tokens(config["tokens"], update_expiry_time=False)
        if "username" in config:
            self.auth.username = config["username"]
        if "password" in config:
            self.auth.password = config["password"]
        if "device_data" in config:
            self._apply_device_data(config["device_data"])

    def create_devices(self) -> dict[str, list[Device]]:
        """Build the device list from the stored products and devices.

        Returns:
            ``parent`` (the hub), ``climate`` and ``water_heater`` lists.
        """
        self.device_list = {"parent": [], "climate": [], "water_heater": []}
        self.hub_id = None

        hub = next(
            (
                (device_id, device)
                for device_id, device in self.data.devices.items()
                if device.get("type") == "hub"
            ),
            None,
        )
        if hub is None:
            _LOGGER.warning("No hub found among the Hive devices")
        else:
            # Products point at the hub only when it made it into the list.
            hub_device = self._add_device("parent", hub[1])
            if hub_device is not None:
                self.hub_id = hub_device.device_id

        temperature_unit = self.data.user.get("temperatureUnit")
        for product_id, product in self.data.products.items():
            if "error" in product:
                _LOGGER.warning(
                    "Skipping product %s due to error: %s", product_id, product["error"]
                )
                continue
            product_type = product.get("type", "Unknown")
            entity_type = PRODUCT_ENTITY_TYPES.get(product_type)
            if entity_type is None:
                _LOGGER.debug(
                    "Skipping unsupported product %s (type: %s)",
                    product_id,
                    product_type,
                )
                continue
            if product.get("isGroup", False) and entity_type != "climate":
                _LOGGER.debug(
                    "Skipping group product %s (type: %s)", product_id, product_type
                )
                continue
            self._add_device(
                entity_type,
                product,
                temperature_unit=temperature_unit if entity_type == "climate" else None,
            )

        _LOGGER.info(
            "Device discovery completed: %d parent, %d climate, %d water_heater",
            len(self.device_list["parent"]),
            len(self.device_list["climate"]),
            len(self.device_list["water_heater"]),
        )
        return self.device_list

    def _add_device(
        self,
        entity_type: str,
        data: dict[str, Any],
        temperature_unit: str | None = None,
    ) -> Device | None:
        """Create a Device for a product or the hub and add it to the device list.

        Args:
            entity_type: ``parent``, ``climate`` or ``water_heater``.
            data: Raw product or device data from the Hive API.
            temperature_unit: The account's temperature unit, for climate entities.

        Returns:
            The created device, or None when the data is incomplete.
        """
        try:
            device_data = self._device_for_product(data)
            # The entity is named after the product (the heating zone, the hot
            # water); the device after the hardware that carries it.
            device_name = _display_name(device_data["state"]["name"])
            name = _display_name((data.get("state") or {}).get("name") or device_name)
            device = Device(
                hive_id=data.get("id", ""),
                hive_name=name,
                hive_type=data.get("type", ""),
                ha_type=entity_type,
                device_id=device_data["id"],
                device_name=device_name,
                device_data=device_data.get("props", data.get("props", {})),
                parent_device=self.hub_id,
                ha_name=name,
                temperature_unit=temperature_unit,
            )
        except (AttributeError, IndexError, KeyError, TypeError) as error:
            _LOGGER.warning(
                "Skipping %s %s: %r",
                data.get("type", "unknown"),
                data.get("id", "?"),
                error,
            )
            return None
        self.device_list[entity_type].append(device)
        return device

    def _device_for_product(self, product: dict[str, Any]) -> dict[str, Any]:
        """Return the Hive device that carries a product's hardware details.

        Heating and hot water products belong to the thermostat in their zone;
        TRV controls belong to their first TRV. Anything else, including the
        hub itself, is its own device.

        Raises:
            IndexError: A TRV control has no TRVs.
            KeyError: The product's device is missing from the data.
            AttributeError, TypeError: The product or a device is malformed.
        """
        product_type = product["type"]
        if product_type in ("heating", "hotwater"):
            product_zone = product.get("props", {}).get("zone")
            return next(
                (
                    candidate
                    for candidate in self.data.devices.values()
                    if candidate["type"] in THERMOSTAT_DEVICE_TYPES
                    and product_zone
                    and candidate.get("props", {}).get("zone") == product_zone
                ),
                product,
            )
        if product_type == "trvcontrol":
            return self.data.devices[product["props"]["trvs"][0]]
        return self.data.devices[product["id"]]

    # --- Device bookkeeping ------------------------------------------------

    def device_recovered(self, device_id: str) -> None:
        """Forget that a device was offline."""
        self._offline_devices.discard(device_id)

    def report_device_offline(self, device_id: str) -> None:
        """Log once that a device is offline, until it recovers."""
        if device_id in self._offline_devices:
            return
        self._offline_devices.add(device_id)
        _LOGGER.warning("%s is offline", self._device_name(device_id))

    def _device_name(self, device_id: str) -> str:
        """Return the product or device name for an ID, or the ID itself."""
        for nodes in (self.data.products, self.data.devices):
            name = ((nodes.get(device_id) or {}).get("state") or {}).get("name")
            if name:
                return str(name)
        return device_id
