"""The Hive session: login, token refresh, polling and device discovery."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from aiohttp.web import HTTPException

from apyhiveapi.api import HiveApi
from apyhiveapi.auth import HiveAuth
from apyhiveapi.const import (
    EXPECTED_DEVICE_DATA_LENGTH,
    PRODUCT_ENTITY_TYPES,
    THERMOSTAT_DEVICE_TYPES,
)
from apyhiveapi.exceptions import (
    HiveApiError,
    HiveAuthError,
    HiveConnectionError,
    HiveFailedToRefreshTokens,
    HiveInvalidDeviceAuthentication,
    HiveInvalidPassword,
    HiveInvalidUsername,
    HiveReauthRequired,
    HiveRefreshTokenExpired,
    HiveUnknownConfiguration,
)
from apyhiveapi.heating import Climate
from apyhiveapi.helpers import sanitize_payload
from apyhiveapi.hotwater import WaterHeater
from apyhiveapi.models import Device, SessionConfig, SessionData, SessionTokens

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from aiohttp import ClientSession

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

# A poll slower than this many seconds makes entities serve cached state
# until the next poll completes.
SLOW_POLL_THRESHOLD = 3

# How long after a failed poll the next attempt may run.
POLL_RETRY_DELAY = timedelta(seconds=30)

_RETRY_DELAYS: tuple[float, ...] = (0, 5, 10)


class Hive:
    """A Hive account session.

    Create one per account, call :meth:`start_session` with the saved
    tokens and device credentials, then poll with :meth:`update_data` and
    read entity state through :attr:`heating` and :attr:`hotwater`.
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
        self.auth = HiveAuth(username or "", password or "")
        self._owns_websession = websession is None
        self.api = HiveApi(self, websession)
        self.tokens = SessionTokens()
        self.config = SessionConfig(username=username)
        self.data = SessionData()
        self.entity_cache: dict[str, Device] = {}
        self.device_list: dict[str, list[Device]] = {}
        self.hub_id: str | None = None
        self.update_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._refresh_threshold = REFRESH_THRESHOLD
        self._slow_poll_threshold = SLOW_POLL_THRESHOLD
        self._last_poll_slow = False
        self._stale_state_polls: dict[str, int] = {}
        self._update_task: asyncio.Task[Any] | None = None
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

    async def _retry_with_backoff[T](
        self,
        coro_factory: Callable[[], Awaitable[T]],
        *,
        delays: Sequence[float] = _RETRY_DELAYS,
        reraise_as: type[Exception] | None = None,
        pass_through: tuple[type[Exception], ...] = (),
    ) -> T:
        """Retry an async operation with sequential delays.

        Args:
            coro_factory: Zero-argument callable returning a coroutine to attempt.
            delays: Seconds to wait before each attempt; the first (0) is immediate.
            reraise_as: Exception type to raise once all attempts are exhausted.
                Defaults to the type of the last caught exception.
            pass_through: Exception types that bypass retrying and propagate
                immediately to the caller.

        Returns:
            The result of the first successful ``coro_factory()`` call.

        Raises:
            Exception: ``reraise_as``, or the last error, when all attempts fail.
        """
        last_err: Exception | None = None
        for delay in delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                return await coro_factory()
            except pass_through:
                raise
            except Exception as err:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                last_err = err
        if reraise_as is not None:
            raise reraise_as() from last_err
        if last_err is not None:
            raise last_err
        message = "Retry attempts exhausted without capturing an error"
        raise RuntimeError(message)

    async def update_tokens(
        self, tokens: dict[str, Any], *, update_expiry_time: bool = True
    ) -> SessionTokens:
        """Store tokens from a Cognito response or a saved config entry.

        Args:
            tokens: Either a Cognito response with an ``AuthenticationResult``
                or a flat ``{"token": ..., "refreshToken": ..., "accessToken": ...}``.
            update_expiry_time: Whether the tokens count as freshly issued.

        Returns:
            SessionTokens: The updated token store.
        """
        _LOGGER.debug("update_tokens - Input tokens: %s", sanitize_payload(tokens))
        data: dict[str, Any] = {}
        mapping: dict[str, str] = {}
        if "AuthenticationResult" in tokens:
            data = tokens.get("AuthenticationResult") or {}
            mapping = {
                "IdToken": "token",
                "RefreshToken": "refreshToken",
                "AccessToken": "accessToken",
            }
        elif "token" in tokens:
            data = tokens
            mapping = {
                "token": "token",
                "refreshToken": "refreshToken",
                "accessToken": "accessToken",
            }
        for source, target in mapping.items():
            if source in data:
                self.tokens.token_data[target] = data[source]
        if mapping and update_expiry_time:
            self.tokens.token_created = datetime.now(UTC)
        if "ExpiresIn" in data:
            self.tokens.token_expiry = timedelta(seconds=data["ExpiresIn"])

        id_token = self.tokens.token_data.get("token", "")
        access_token = self.tokens.token_data.get("accessToken", "")
        refresh_token = self.tokens.token_data.get("refreshToken", "")
        _LOGGER.debug(
            "update_tokens - Final session tokens: IdToken: len=%d tail=...%s | "
            "AccessToken: len=%d tail=...%s | "
            "RefreshToken: %s | "
            "ExpiresIn: %s | token_created: %s | token_expiry: %s",
            len(id_token),
            id_token[-4:],
            len(access_token),
            access_token[-4:],
            (
                f"present (len={len(refresh_token)} tail=...{refresh_token[-4:]})"
                if refresh_token
                else "not present"
            ),
            data.get("ExpiresIn", "N/A"),
            self.tokens.token_created,
            self.tokens.token_expiry,
        )
        return self.tokens

    async def login(self) -> dict[str, Any]:
        """Log in and store the tokens.

        A plain login returns tokens directly. A ``DEVICE_SRP_AUTH`` challenge
        is answered with the saved device credentials. An ``SMS_MFA``
        challenge is returned to the caller, who must complete it with
        :meth:`HiveAuth.sms_2fa`.

        Raises:
            HiveUnknownConfiguration: Cognito asked for an unsupported challenge.

        Returns:
            dict: The authentication result.
        """
        _LOGGER.debug("login - Attempting login to Hive account.")
        try:
            result = await self.auth.login()
        except HiveInvalidUsername:
            _LOGGER.warning("Login failed: invalid username.")
            raise
        except HiveInvalidPassword:
            _LOGGER.warning("Login failed: invalid password.")
            raise
        except HiveApiError:
            _LOGGER.warning("Login failed: API error or no internet connection.")
            raise

        if result and "AuthenticationResult" in result:
            _LOGGER.debug(
                "login - Login successful - AuthenticationResult keys: %s",
                list(result["AuthenticationResult"]),
            )
            await self.update_tokens(result)
            return result

        challenge_name = result.get("ChallengeName")
        _LOGGER.debug("login - Challenge detected: %s", challenge_name)

        if challenge_name == self.auth.DEVICE_VERIFIER_CHALLENGE:
            _LOGGER.debug("login - Routing to device login flow")
            return await self._handle_device_login_challenge()
        if challenge_name == self.auth.SMS_MFA_CHALLENGE:
            _LOGGER.debug("login - Routing to SMS 2FA flow (requires user input)")
            return result
        _LOGGER.error("login - Unsupported challenge: %s", challenge_name)
        raise HiveUnknownConfiguration

    async def _handle_device_login_challenge(self) -> dict[str, Any]:
        """Complete a login with the saved device credentials.

        Raises:
            HiveReauthRequired: Cognito no longer remembers the device.
            HiveInvalidDeviceAuthentication: The device is not registered.

        Returns:
            dict: Authentication result with tokens.
        """
        _LOGGER.debug("_handle_device_login_challenge - Processing device login")
        result = await self.auth.device_login()

        if result and result.get("ChallengeName") == self.auth.SMS_MFA_CHALLENGE:
            _LOGGER.error(
                "_handle_device_login_challenge - Device login failed: SMS MFA "
                "challenge detected. Device is not remembered by Cognito. "
                "User must reauthenticate."
            )
            raise HiveReauthRequired

        if result and "AuthenticationResult" in result:
            _LOGGER.debug(
                "_handle_device_login_challenge - Device login successful"
                " - AuthenticationResult keys: %s",
                list(result["AuthenticationResult"]),
            )
            await self.update_tokens(result)

        return result

    async def _retry_login(self) -> None:
        """Log in again with retries and backoff after a failed token refresh.

        Raises:
            HiveReauthRequired: User interaction is required (SMS 2FA), the
                credentials or saved device credentials were rejected, or
                every attempt failed.
        """

        async def _attempt() -> dict[str, Any]:
            try:
                result = await self.login()
            except Exception as err:
                _LOGGER.warning(
                    "_retry_login - Login attempt failed: %s", type(err).__name__
                )
                raise
            if result and result.get("ChallengeName") == self.auth.SMS_MFA_CHALLENGE:
                _LOGGER.error(
                    "_retry_login - Login requires SMS 2FA. User must reauthenticate."
                )
                raise HiveReauthRequired
            return result

        try:
            await self._retry_with_backoff(
                _attempt,
                reraise_as=HiveReauthRequired,
                pass_through=(
                    HiveReauthRequired,
                    HiveInvalidUsername,
                    HiveInvalidPassword,
                    HiveInvalidDeviceAuthentication,
                ),
            )
        except (
            HiveInvalidUsername,
            HiveInvalidPassword,
            HiveInvalidDeviceAuthentication,
        ) as exc:
            _LOGGER.warning(
                "_retry_login - Login failed with invalid credentials,"
                " reauthentication required."
            )
            raise HiveReauthRequired from exc

    def _refresh_due(self) -> bool:
        """Return True once the tokens have used most of their lifetime."""
        expiry_time = self.tokens.token_created + (
            self.tokens.token_expiry * self._refresh_threshold
        )
        return datetime.now(UTC) >= expiry_time

    async def hive_refresh_tokens(
        self, *, force_refresh: bool = False
    ) -> dict[str, Any] | None:
        """Refresh the tokens if they are near expiry, or always when forced.

        Falls back to a full login when the refresh token is rejected.

        Returns:
            dict | None: Cognito's refresh response, or None when nothing was
            refreshed.
        """
        if self.config.file:
            return None

        _LOGGER.debug(
            "hive_refresh_tokens - Session token created: %s | expiry: %s | now: %s",
            self.tokens.token_created,
            self.tokens.token_created + self.tokens.token_expiry,
            datetime.now(UTC),
        )
        if not self._refresh_due() and not force_refresh:
            return None

        async with self._refresh_lock:
            # Re-check after acquiring the lock: another caller may have refreshed.
            if not self._refresh_due() and not force_refresh:
                return None
            result: dict[str, Any] | None = None
            try:
                result = await self.auth.refresh_token(
                    self.tokens.token_data.get("refreshToken")
                )
                if result and "AuthenticationResult" in result:
                    await self.update_tokens(result)
                    _LOGGER.debug(
                        "hive_refresh_tokens - Session Token refresh"
                        " successful. New expiry: %s",
                        self.tokens.token_created + self.tokens.token_expiry,
                    )
            except (HiveRefreshTokenExpired, HiveFailedToRefreshTokens) as exc:
                _LOGGER.warning(
                    "Session Token refresh failed (%s), falling back to login.",
                    type(exc).__name__,
                )
                if force_refresh:
                    _LOGGER.warning(
                        "Token refresh failed during retry attempt, giving up."
                    )
                    raise HiveReauthRequired from exc
                await self._retry_login()
            except HiveApiError:
                _LOGGER.warning("API error during token refresh.")
                raise

        return result

    # --- Polling -----------------------------------------------------------

    @staticmethod
    def _entity_cache_key(device: Device) -> str:
        """Build a stable cache key for an entity instance."""
        return f"{device.ha_type}|{device.hive_id}|{device.hive_type}"

    def get_cached_device(self, device: Device) -> Device | None:
        """Return the cached state for an entity, if any."""
        return self.entity_cache.get(self._entity_cache_key(device))

    def set_cached_device(self, device: Device) -> Device:
        """Store the entity's state in the cache and return it."""
        self.entity_cache[self._entity_cache_key(device)] = device
        return device

    def should_use_cached_data(self) -> bool:
        """Return True while entities should serve cached state.

        That is the case when the last poll was slow or another task is
        currently polling.
        """
        if self._last_poll_slow:
            return True
        if self.update_lock.locked():
            current_task = asyncio.current_task()
            return self._update_task is None or current_task is not self._update_task
        return False

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

    async def update_interval(self, new_interval: float | timedelta) -> bool:
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
        """Fetch the latest device state from the Hive API."""
        return await self.get_devices()

    def _poll_is_due(self) -> bool:
        """Return True once the scan interval has passed since the last poll."""
        return datetime.now(UTC) >= self.config.last_update + self.config.scan_interval

    async def update_data(self, _device: Device) -> bool:
        """Poll Hive if the scan interval has passed since the last poll.

        Home Assistant calls this from every entity's update, so the poll is
        rate limited and runs at most once per interval across all entities.

        Args:
            _device: The entity requesting the update (unused).

        Returns:
            bool: True if a poll ran and succeeded.
        """
        if not self._poll_is_due():
            return False
        current_task = asyncio.current_task()
        if self.update_lock.locked() and (
            self._update_task is None or current_task is not self._update_task
        ):
            _LOGGER.debug("update_data - Update poll already in progress")
            return False
        async with self.update_lock:
            # Re-check after acquiring the lock: another caller may have polled.
            if not self._poll_is_due():
                return False
            self._update_task = current_task
            try:
                _LOGGER.debug("Polling Hive API for device updates.")
                updated = await self._poll_devices()
                if updated:
                    _LOGGER.debug("update_data - Device update completed successfully.")
                else:
                    _LOGGER.debug(
                        "update_data - Device update failed,"
                        " will retry after scan interval."
                    )
            finally:
                if self._update_task is current_task:
                    self._update_task = None
        return updated

    async def _fetch_all_nodes(self) -> dict[str, Any] | None:
        """Refresh tokens, then fetch every product and device from the API.

        Logs in again and retries when the API rejects the token, and records
        whether the call was slow.

        Raises:
            HTTPException: The API answered with a non-2xx status.
            HiveApiError: The API answered without a parsed body.

        Returns:
            dict | None: The ``{"original": ..., "parsed": ...}`` response.
        """
        _LOGGER.debug("get_devices - Refreshing tokens before fetching devices.")
        await self.hive_refresh_tokens()
        _LOGGER.debug("get_devices - Fetching all devices from Hive API.")
        api_call_start = time.monotonic()
        try:
            response = await self.api.get_all()
        except HiveAuthError:
            self._last_poll_slow = False
            _LOGGER.warning(
                "Auth error (401/403) after token refresh, "
                "falling back to full device re-login."
            )
            await self._retry_login()
            response = await self._retry_with_backoff(
                self.api.get_all, reraise_as=HiveReauthRequired
            )
        else:
            api_call_duration = time.monotonic() - api_call_start
            self._last_poll_slow = api_call_duration > self._slow_poll_threshold
            if self._last_poll_slow:
                _LOGGER.debug(
                    "get_devices - Hive API response took %.1fs"
                    " - marking poll as slow.",
                    api_call_duration,
                )
        if response is None:
            return None
        if not str(response["original"]).startswith("2"):
            raise HTTPException
        if response["parsed"] is None:
            raise HiveApiError
        return response

    def _store_nodes(self, nodes: dict[str, Any]) -> None:
        """Store the user, home, products and devices from a poll.

        Args:
            nodes: The parsed JSON body of the all-nodes response.
        """
        if "user" in nodes:
            self.data.user = nodes["user"]
            self.config.user_id = nodes["user"].get("id")
        homes = nodes.get("homes")
        if isinstance(homes, dict) and homes.get("homes"):
            self.config.home_id = homes["homes"][0]["id"]
        products = {product["id"]: product for product in nodes.get("products", [])}
        devices = {device["id"]: device for device in nodes.get("devices", [])}

        _LOGGER.debug(
            "get_devices - API returned %d products, %d devices.",
            len(products),
            len(devices),
        )
        if products:
            self._keep_missing_state(products)
            self.data.products = products
        if devices:
            self.data.devices = devices

    async def _fetch_nodes(self) -> dict[str, Any] | None:
        """Return the all-nodes response from the fixture file or the API."""
        if self.config.file:
            _LOGGER.debug("get_devices - Loading device data from file.")
            return self.open_file("data.json")
        return await self._fetch_all_nodes()

    async def get_devices(self) -> bool:
        """Fetch and store the latest state of every product and device.

        Raises:
            HiveReauthRequired: The user must log in again.

        Returns:
            bool: True if the data was updated.
        """
        try:
            response = await self._fetch_nodes()
            if response is not None:
                self._store_nodes(response["parsed"])
        except HiveReauthRequired:
            _LOGGER.warning("Reauthentication required, propagating to caller.")
            self.config.last_update = datetime.now(UTC)
            raise
        except TimeoutError:
            _LOGGER.warning("Hive API request timed out - keeping cached device data.")
            self._last_poll_slow = True
            self._schedule_poll_retry()
            return False
        except (
            OSError,
            RuntimeError,
            HiveApiError,
            ConnectionError,
            HTTPException,
            KeyError,
        ):
            _LOGGER.exception("Failed to fetch devices")
            self._schedule_poll_retry()
            return False
        if response is None:
            return False
        self.config.last_update = datetime.now(UTC)
        return True

    def _schedule_poll_retry(self) -> None:
        """Allow the next poll after a short delay instead of a full interval."""
        self.config.last_update = (
            datetime.now(UTC) - self.config.scan_interval + POLL_RETRY_DELAY
        )

    # --- Session start and device discovery --------------------------------

    def open_file(self, file: str) -> dict[str, Any]:
        """Load a JSON fixture file from the package data directory.

        Args:
            file: Filename relative to the ``data/`` directory.

        Returns:
            dict: Parsed JSON content of the file.
        """
        result: dict[str, Any] = json.loads(
            (_DATA_DIR / file).read_text(encoding="utf-8")
        )
        return result

    def _configure_file_mode(self, username: str | None) -> None:
        """Switch to the bundled fixture data for the magic testing username."""
        if username == "use@file.com":
            self.config.file = True

    @staticmethod
    def _parse_token_created(value: object) -> datetime | None:
        """Return the saved token-issue time as an aware UTC datetime.

        Home Assistant stores ``device_data`` as JSON, so after a restart the
        datetime returned by ``get_device_data()`` comes back as an ISO string.
        Naive values, as older versions saved, are taken as local time.

        Args:
            value: A datetime, an ISO 8601 string, or a falsy value.

        Returns:
            datetime | None: The parsed time, or None if it is missing or invalid.
        """
        if isinstance(value, datetime):
            return value.astimezone(UTC)
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value).astimezone(UTC)
            except ValueError:
                _LOGGER.warning("start_session - Ignoring invalid token_created value")
        return None

    def _apply_device_data(self, device_data: Sequence[Any]) -> None:
        """Load saved device credentials (and token issue time) into the session.

        Args:
            device_data: ``(device_group_key, device_key, device_password)``,
                optionally followed by ``token_created``.

        Raises:
            HiveUnknownConfiguration: Fewer than three values were given.
        """
        if len(device_data) < EXPECTED_DEVICE_DATA_LENGTH:
            message = (
                "device_data must contain device_group_key, "
                "device_key and device_password"
            )
            raise HiveUnknownConfiguration(message)
        self.auth.device_group_key = device_data[0]
        self.auth.device_key = device_data[1]
        self.auth.device_password = device_data[2]
        if len(device_data) > EXPECTED_DEVICE_DATA_LENGTH:
            token_created = self._parse_token_created(device_data[3])
            if token_created is not None:
                self.tokens.token_created = token_created

    async def start_session(
        self, config: dict[str, Any] | None = None
    ) -> dict[str, list[Device]]:
        """Load the config, poll Hive once and build the device list.

        Args:
            config: ``username``, ``password``, ``tokens``, ``device_data`` and
                ``options.scan_interval`` from the Home Assistant config entry.

        Raises:
            HiveUnknownConfiguration: No tokens were given, or Hive returned
                no devices.
            HiveConnectionError: Hive could not be reached.
            HiveReauthRequired: The user must log in again.

        Returns:
            dict: Devices by Home Assistant entity type.
        """
        if config is None:
            config = {}
        _LOGGER.debug("start_session - Starting Hive session.")
        _LOGGER.debug("start_session - Config: %s", sanitize_payload(config))
        self._configure_file_mode(config.get("username", self.config.username))

        scan_interval = (config.get("options") or {}).get("scan_interval")
        if scan_interval is not None:
            await self.update_interval(scan_interval)

        if config and not self.config.file:
            await self._apply_config(config)

        fetched = await self.get_devices()

        if not self.data.devices or not self.data.products:
            if not fetched:
                # The request failed rather than returning an empty account,
                # so let the caller retry instead of treating it as bad config.
                message = "Could not fetch devices from Hive"
                raise HiveConnectionError(message)
            _LOGGER.error("No devices or products returned from Hive API.")
            raise HiveUnknownConfiguration

        return self.create_devices()

    async def _apply_config(self, config: dict[str, Any]) -> None:
        """Load the credentials, tokens and device data from a config entry.

        Raises:
            HiveUnknownConfiguration: The config has no tokens.
        """
        if "tokens" in config:
            _LOGGER.debug("start_session - Updating tokens from config")
            await self.update_tokens(config["tokens"], update_expiry_time=False)
        if "username" in config:
            self.auth.username = config["username"]
        if "password" in config:
            self.auth.password = config["password"]
        if "device_data" in config:
            self._apply_device_data(config["device_data"])
        if "tokens" not in config:
            raise HiveUnknownConfiguration

    def create_devices(self) -> dict[str, list[Device]]:
        """Build the device list from the stored products and devices.

        Returns:
            dict: ``parent`` (the hub), ``climate`` and ``water_heater`` lists.
        """
        _LOGGER.info("create_devices - Starting device discovery process")
        self.device_list = {"parent": [], "climate": [], "water_heater": []}

        hub = next(
            (
                (device_id, device)
                for device_id, device in self.data.devices.items()
                if device.get("type") == "hub"
            ),
            None,
        )
        if hub is None:
            _LOGGER.warning("create_devices - No hub device found in device list")
        else:
            self.hub_id, hub_data = hub
            _LOGGER.debug(
                "create_devices - Found hub device: %s (ID: %s)",
                hub_data.get("state", {}).get("name", self.hub_id),
                self.hub_id,
            )
            self._add_device("parent", hub_data)

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
                    "create_devices - Skipping unsupported product %s (type: %s)",
                    product_id,
                    product_type,
                )
                continue
            if product.get("isGroup", False) and entity_type != "climate":
                _LOGGER.debug(
                    "create_devices - Skipping group product %s (type: %s)",
                    product_id,
                    product_type,
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
            Device: The created device, or None when the data is incomplete.
        """
        try:
            device_data = self._device_for_product(data)
            state_name = device_data["state"]["name"]
            name = "Heating" if state_name == "Receiver" else state_name
            device = Device(
                hive_id=data.get("id", ""),
                hive_name=name,
                hive_type=data.get("type", ""),
                ha_type=entity_type,
                device_id=device_data["id"],
                device_name=name,
                device_data=device_data.get("props", data.get("props", {})),
                parent_device=self.hub_id,
                ha_name=name,
                temperature_unit=temperature_unit,
            )
        except (KeyError, TypeError) as error:
            _LOGGER.warning(
                "create_devices - Skipping %s %s: %r",
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
            KeyError: The product's device is missing from the data.
        """
        product_type = product["type"]
        if product_type in ("heating", "hotwater"):
            device = product
            product_zone = product.get("props", {}).get("zone")
            for candidate in self.data.devices.values():
                if candidate["type"] in THERMOSTAT_DEVICE_TYPES:
                    device_zone = candidate.get("props", {}).get("zone")
                    if product_zone and device_zone and product_zone == device_zone:
                        device = candidate
            return device
        if product_type == "trvcontrol":
            trvs = product["props"]["trvs"]
            if not trvs:
                _LOGGER.error(
                    "get_device_data - No TRVs found for product %s",
                    product.get("id", "Unknown"),
                )
                raise KeyError(product_type)
            return self.data.devices[trvs[0]]
        return self.data.devices[product["id"]]

    # --- Device bookkeeping ------------------------------------------------

    def device_recovered(self, device_id: str) -> None:
        """Forget that a device was offline."""
        self.config.error_list.pop(device_id, None)

    def report_device_offline(self, device_id: str) -> None:
        """Log once that a device is offline, until it recovers."""
        if device_id in self.config.error_list:
            return
        _LOGGER.warning(
            "Device offline could not update entity - %s", self._device_name(device_id)
        )
        self.config.error_list[device_id] = datetime.now(UTC)

    def _device_name(self, device_id: str) -> str:
        """Return the product or device name for an ID, or the ID itself."""
        for nodes in (self.data.products, self.data.devices):
            name = nodes.get(device_id, {}).get("state", {}).get("name")
            if name:
                return name
        _LOGGER.warning(
            "get_device_name - No product or device name found for ID: %s", device_id
        )
        return device_id
