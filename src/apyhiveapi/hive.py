"""The Hive session: login, token refresh and the Hive API calls."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self

from apyhiveapi.api import HiveApi
from apyhiveapi.auth import HiveAuth
from apyhiveapi.exceptions import (
    HiveApiError,
    HiveConnectionError,
    HiveReauthRequired,
)
from apyhiveapi.helpers import sanitize_payload
from apyhiveapi.models import SessionConfig, SessionTokens
from apyhiveapi.nodes import Nodes, parse_nodes

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from aiohttp import ClientSession

    from apyhiveapi.api import JsonValue
    from apyhiveapi.nodes import HeatingProduct, HotWaterProduct, Product

_LOGGER = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent / "data"

# The modes a product can be set to. Hive's MANUAL is "on" for hot water.
type Mode = Literal["SCHEDULE", "MANUAL", "OFF"]

# Tokens are refreshed once this share of their lifetime has passed.
REFRESH_THRESHOLD = 0.90

# device_data holds device_group_key, device_key and device_password. Entries
# saved by older versions add the token issue time, which is ignored.
_DEVICE_DATA_FIELDS = 3


class Hive:
    """A Hive account session.

    Create one per account and call :meth:`new_session` with the saved
    tokens and device credentials. Then :meth:`get_nodes` fetches Hive's
    ``GET /nodes/all``, and the ``set_*`` commands each send one
    ``POST /nodes/{type}/{id}``. The session keeps no device state and has no
    timer: the caller decides when to poll.

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
        self._refresh_lock = asyncio.Lock()
        # The login failure that needs the user, kept so later calls fail fast.
        self._reauth_error: HiveReauthRequired | None = None

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

    # --- Commands ----------------------------------------------------------

    async def set_mode(self, product: Product, mode: Mode) -> None:
        """Set a product's mode: ``{"mode": mode}``.

        Ending a boost is setting the mode it returns to, which Hive reports
        as the product's ``previous_mode``.

        Raises:
            HiveReauthRequired: The user has to log in again.
            HiveConnectionError: Hive could not be reached.
            HiveApiError: Hive refused the change.
        """
        await self._set_state(product, mode=mode)

    async def set_target_temperature(
        self, product: HeatingProduct, target: float
    ) -> None:
        """Set a heating or TRV target temperature: ``{"target": target}``.

        Raises:
            HiveReauthRequired: The user has to log in again.
            HiveConnectionError: Hive could not be reached.
            HiveApiError: Hive refused the change.
        """
        await self._set_state(product, target=target)

    async def set_heating_boost(
        self, product: HeatingProduct, minutes: int, target: float
    ) -> None:
        """Boost heating to *target* for *minutes*.

        Sends ``{"mode": "BOOST", "boost": minutes, "target": target}``.

        Raises:
            HiveReauthRequired: The user has to log in again.
            HiveConnectionError: Hive could not be reached.
            HiveApiError: Hive refused the change.
        """
        await self._set_state(product, mode="BOOST", boost=minutes, target=target)

    async def set_hot_water_boost(self, product: HotWaterProduct, minutes: int) -> None:
        """Boost the hot water for *minutes*: ``{"mode": "BOOST", "boost": minutes}``.

        Raises:
            HiveReauthRequired: The user has to log in again.
            HiveConnectionError: Hive could not be reached.
            HiveApiError: Hive refused the change.
        """
        await self._set_state(product, mode="BOOST", boost=minutes)

    async def _set_state(self, product: Product, **state: JsonValue) -> None:
        """Post a product's new state to Hive. Nothing is sent in file mode."""
        if self.config.file:
            return
        await self._api_call(
            lambda: self.api.set_state(product.type, product.id, **state)
        )

    # --- Nodes -------------------------------------------------------------

    async def get_nodes(self) -> Nodes:
        """Fetch every product and device: Hive's ``GET /nodes/all``.

        In file mode the bundled fixture is read instead.

        Returns:
            The snapshot, with only the node types this library supports.

        Raises:
            HiveReauthRequired: The user has to log in again.
            HiveConnectionError: Hive could not be reached, or returned no
                products or no devices.
            HiveApiError: Hive refused the request.
        """
        if self.config.file:
            response = await asyncio.to_thread(self.open_file, "data.json")
        else:
            response = await self._api_call(self.api.get_all)
        products, devices = response.get("products"), response.get("devices")
        if not products or not devices:
            # Every account has at least its hub, so an empty answer is a
            # fault on Hive's side, not an account without devices.
            raise HiveConnectionError("Hive returned no products or no devices")
        nodes = parse_nodes(response)
        _LOGGER.debug(
            "Hive returned %d products and %d devices",
            len(nodes.products),
            len(nodes.devices),
        )
        return nodes

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

    # --- New session -------------------------------------------------------

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

    def new_session(self, config: dict[str, Any] | None = None) -> None:
        """Load the credentials, tokens and device data from a config entry.

        Nothing is sent to Hive or Cognito; the first :meth:`get_nodes` is.

        Args:
            config: ``username``, ``password``, ``tokens`` and ``device_data``
                from the Home Assistant config entry.

        Raises:
            HiveReauthRequired: The config has no tokens, or its device data
                is incomplete.
        """
        config = config or {}
        _LOGGER.debug("new_session - Config: %s", sanitize_payload(config))
        self._configure_file_mode(config.get("username", self.config.username))
        if self.config.file:
            return
        if "tokens" not in config:
            raise HiveReauthRequired("The config has no tokens")
        self.update_tokens(config["tokens"], update_expiry_time=False)
        if "username" in config:
            self.auth.username = config["username"]
        if "password" in config:
            self.auth.password = config["password"]
        if "device_data" in config:
            self._apply_device_data(config["device_data"])
