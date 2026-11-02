"""AWS Cognito authentication for Hive: SRP login, SMS 2FA and trusted devices.

The Cognito pool details come from Hive's SSO login page, fetched through
:func:`hiveheating.api._fetch`, which translates its aiohttp errors.
Cognito's refusals and connection failures are translated in
:meth:`HiveAuth._cognito`, so callers only ever see
:class:`~hiveheating.exceptions.HiveError` subclasses. A botocore
``ParamValidationError`` means a bug in the call itself and propagates.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import hmac
import logging
import os
import re
import socket
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import boto3
import botocore.exceptions
from aiohttp import ClientSession
from botocore.config import Config

from hiveheating.api import _fetch
from hiveheating.exceptions import (
    HiveApiError,
    HiveConnectionError,
    HiveError,
    HiveInvalid2FACode,
    HiveInvalidPassword,
    HiveInvalidUsername,
    HiveReauthRequired,
)
from hiveheating.srp import (
    BIG_N,
    G,
    K,
    calculate_a,
    calculate_u,
    compute_hkdf,
    generate_small_a,
    get_random,
    hash_sha256,
    hex_hash,
    hex_to_long,
    long_to_hex,
    pad_hex,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOGGER = logging.getLogger(__name__)

# The ChallengeName of a login that needs the user's SMS code.
SMS_REQUIRED = "SMS_MFA"

# The username that switches to the bundled fixture instead of Hive.
FILE_USERNAME = "use@file.com"

# Cognito error codes and the exception each one becomes.
type CodeMap = Mapping[str, type[HiveError]]

# Cognito error codes that mean "try again later", whatever the call.
_TRANSIENT_CODES = frozenset({"InternalErrorException", "TooManyRequestsException"})

# botocore's defaults are 60 s timeouts and up to five attempts, so a login
# against an unreachable Cognito could hang for minutes. Bound it like the
# Hive HTTP calls and keep a single retry for throttling and server errors.
_COGNITO_CONFIG = Config(
    connect_timeout=10,
    read_timeout=20,
    retries={"max_attempts": 2, "mode": "standard"},
)

_SSO_URL = "https://sso.hivehome.com/"

# The SSO bootstrap page embeds its Cognito config in its first <script>
# block as `window.<key> = <value>` assignments. The value quoting changed
# from double to single quotes on 2026-10-20, so accept either (and require
# the closing quote to match).
_FIRST_SCRIPT = re.compile(r"<script[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)
_SSO_ASSIGNMENT = re.compile(r"""window\.(\w+)\s*=\s*(["'])(.*?)\2""")

# Cognito error codes that mean the saved device is no longer trusted.
_DEVICE_REJECTED: CodeMap = {
    "NotAuthorizedException": HiveReauthRequired,
    "ResourceNotFoundException": HiveReauthRequired,
}


class HiveAuth:
    """Log in to a Hive account through AWS Cognito and manage trusted devices."""

    PASSWORD_VERIFIER_CHALLENGE = "PASSWORD_VERIFIER"  # noqa: S105 - challenge name
    DEVICE_VERIFIER_CHALLENGE = "DEVICE_SRP_AUTH"
    DEVICE_PASSWORD_CHALLENGE = "DEVICE_PASSWORD_VERIFIER"  # noqa: S105 - challenge name

    def __init__(  # noqa: PLR0913 - one argument per saved credential
        self,
        username: str,
        password: str,
        device_group_key: str | None = None,
        device_key: str | None = None,
        device_password: str | None = None,
        *,
        websession: ClientSession | None = None,
    ) -> None:
        """Set up the auth state.

        Args:
            username: Hive account email address.
            password: Hive account password.
            device_group_key: Saved trusted-device group key, if any.
            device_key: Saved trusted-device key, if any.
            device_password: Saved trusted-device password, if any.
            websession: aiohttp session for reading the Hive login page. A
                short-lived one is created when none is given.
        """
        self.username = username
        self.password = password
        self.device_group_key = device_group_key
        self.device_key = device_key
        self.device_password = device_password
        self.access_token: str | None = None
        # Cognito's own ID for the user, learnt from the PASSWORD_VERIFIER challenge.
        self.user_id = username
        # The SRP client ephemeral (a, A), made afresh for every handshake.
        self.small_a_value = 0
        self.large_a_value = 0
        self.use_file = self.username == FILE_USERNAME
        self.file_response: dict[str, Any] = {
            "AuthenticationResult": {"AccessToken": "file"}
        }
        self._websession = websession
        # Set by _ensure_client once the SSO login page has been read.
        self._pool_name: str | None = None
        self._client_id: str | None = None
        self.client: Any = None

    async def _ensure_client(self) -> None:
        """Create the Cognito client on first use.

        The pool details come from Hive's SSO login page, read with the given
        aiohttp session or, when there is none, a short-lived one.

        Raises:
            HiveConnectionError: The login page could not be fetched.
            HiveApiError: The page did not include the Cognito details.
        """
        if self.client is not None:
            return
        if self._websession is None:
            async with ClientSession() as own_session:
                page = await _fetch(own_session, "get", _SSO_URL)
        else:
            page = await _fetch(self._websession, "get", _SSO_URL)
        script = _FIRST_SCRIPT.search(page)
        values = {
            key: value
            for key, _quote, value in _SSO_ASSIGNMENT.findall(
                script.group(1) if script else ""
            )
        }
        try:
            pool_id = values["HiveSSOPoolId"]
            client_id = values["HiveSSOPublicCognitoClientId"]
        except KeyError as err:
            raise HiveApiError(f"The Hive login page did not include {err}") from err
        region, _, pool_name = pool_id.partition("_")
        if not pool_name:
            raise HiveApiError(f"Unexpected Cognito pool ID: {pool_id!r}")
        # Cognito USER_SRP_AUTH does not use IAM credentials, but boto3 requires
        # non-None values, so these placeholders are deliberate.
        self.client = await asyncio.get_running_loop().run_in_executor(
            None,
            functools.partial(
                boto3.client,
                "cognito-idp",
                region,
                aws_access_key_id="ACCESS_KEY",
                aws_secret_access_key="SECRET_KEY",  # noqa: S106 - placeholder, see above
                aws_session_token="SESSION_TOKEN",  # noqa: S106 - placeholder, see above
                config=_COGNITO_CONFIG,
            ),
        )
        self._pool_name = pool_name
        self._client_id = client_id

    async def _cognito(
        self, method: str, code_map: CodeMap | None = None, **kwargs: object
    ) -> dict[str, Any]:
        """Call a Cognito client method in the executor and translate its errors.

        Args:
            method: Name of the ``cognito-idp`` client method to call.
            code_map: Cognito error codes and the exception each one becomes.
            **kwargs: Parameters for the Cognito call.

        Raises:
            HiveConnectionError: Cognito could not be reached, timed out or
                asked for a retry.
            HiveApiError: Cognito refused the call with a code not in *code_map*.
            HiveError: The exception *code_map* names for Cognito's error code.
        """
        call = functools.partial(getattr(self.client, method), **kwargs)
        try:
            result: dict[str, Any] = await asyncio.get_running_loop().run_in_executor(
                None, call
            )
        except botocore.exceptions.ClientError as err:
            error = (err.response or {}).get("Error") or {}
            code = error.get("Code", "")
            message = f"Cognito {method} failed with {code}: {error.get('Message', '')}"
            if code in _TRANSIENT_CODES:
                raise HiveConnectionError(message) from err
            exc_type = (code_map or {}).get(code, HiveApiError)
            raise exc_type(message) from err
        except (
            botocore.exceptions.ConnectionError,
            botocore.exceptions.HTTPClientError,
        ) as err:
            raise HiveConnectionError(
                f"Could not reach Cognito for {method}: {err}"
            ) from err
        return result

    # --- Login flows -------------------------------------------------------

    async def login(self) -> dict[str, Any]:
        """Log in with the username and password.

        Returns:
            Cognito's response: an ``AuthenticationResult`` with the tokens, or
            a challenge for the caller to complete (``SMS_MFA`` through
            :meth:`sms_2fa`, ``DEVICE_SRP_AUTH`` through :meth:`device_login`).

        Raises:
            HiveInvalidUsername: Hive does not know the username.
            HiveInvalidPassword: Hive rejected the password.
            HiveReauthRequired: Hive rejected the saved device credentials.
            HiveApiError: Cognito asked for a challenge this library cannot answer.
            HiveConnectionError: Cognito or the login page could not be reached.
        """
        if self.use_file:
            return self.file_response
        await self._ensure_client()
        self._new_srp_ephemeral()
        response = await self._cognito(
            "initiate_auth",
            {"UserNotFoundException": HiveInvalidUsername},
            AuthFlow="USER_SRP_AUTH",
            AuthParameters=self._auth_params(),
            ClientId=self._client_id,
        )
        challenge = response.get("ChallengeName")
        if challenge != self.PASSWORD_VERIFIER_CHALLENGE:
            raise HiveApiError(f"Unsupported Cognito challenge: {challenge}")
        parameters = response["ChallengeParameters"]
        self.user_id = parameters["USER_ID_FOR_SRP"]
        answer = await self._answer_srp_challenge(
            parameters,
            identity=f"{self._pool_name}{self.user_id}",
            secret=self.password,
            username=self.user_id,
        )
        result = await self._cognito(
            "respond_to_auth_challenge",
            {
                "NotAuthorizedException": HiveInvalidPassword,
                "UserNotFoundException": HiveInvalidUsername,
                "ResourceNotFoundException": HiveReauthRequired,
            },
            ClientId=self._client_id,
            ChallengeName=self.PASSWORD_VERIFIER_CHALLENGE,
            ChallengeResponses=answer,
        )
        if "AuthenticationResult" in result:
            self._store_auth_result(result)
        return result

    async def device_login(self) -> dict[str, Any]:
        """Answer the ``DEVICE_SRP_AUTH`` challenge with the saved device credentials.

        Returns:
            Cognito's response, normally an ``AuthenticationResult`` with the tokens.

        Raises:
            HiveReauthRequired: No device credentials are saved, or Cognito no
                longer trusts the device.
            HiveConnectionError: Cognito could not be reached.
            HiveApiError: Cognito refused the call for another reason.
        """
        group_key = self.device_group_key
        device_key = self.device_key
        device_password = self.device_password
        if not (group_key and device_key and device_password):
            raise HiveReauthRequired("There are no saved device credentials")
        await self._ensure_client()
        self._new_srp_ephemeral()
        challenge = await self._cognito(
            "respond_to_auth_challenge",
            _DEVICE_REJECTED,
            ClientId=self._client_id,
            ChallengeName=self.DEVICE_VERIFIER_CHALLENGE,
            ChallengeResponses=self._auth_params(device_key),
        )
        parameters = challenge["ChallengeParameters"]
        answer = await self._answer_srp_challenge(
            parameters,
            identity=f"{group_key}{device_key}",
            secret=device_password,
            username=parameters["USERNAME"],
        )
        result = await self._cognito(
            "respond_to_auth_challenge",
            _DEVICE_REJECTED,
            ClientId=self._client_id,
            ChallengeName=self.DEVICE_PASSWORD_CHALLENGE,
            ChallengeResponses=answer,
        )
        if "AuthenticationResult" in result:
            self._store_auth_result(result)
        return result

    async def sms_2fa(
        self, entered_code: str | int, challenge: dict[str, Any]
    ) -> dict[str, Any]:
        """Answer the ``SMS_MFA`` challenge from :meth:`login` with the user's code.

        Raises:
            ValueError: *challenge* is not the SMS challenge :meth:`login` returned.
            HiveInvalid2FACode: Cognito rejected the code.
            HiveConnectionError: Cognito could not be reached.
            HiveApiError: Cognito refused the call for another reason.
        """
        session = challenge.get("Session")
        if not session:
            raise ValueError("The challenge has no Session: pass the result of login()")
        await self._ensure_client()
        result = await self._cognito(
            "respond_to_auth_challenge",
            {
                "CodeMismatchException": HiveInvalid2FACode,
                "ExpiredCodeException": HiveInvalid2FACode,
                "NotAuthorizedException": HiveInvalid2FACode,
            },
            ClientId=self._client_id,
            ChallengeName=SMS_REQUIRED,
            Session=session,
            ChallengeResponses={
                "SMS_MFA_CODE": str(entered_code),
                "USERNAME": self.user_id,
            },
        )
        if "AuthenticationResult" in result:
            self._store_auth_result(result)
        return result

    async def refresh_token(self, token: str | None) -> dict[str, Any]:
        """Exchange the refresh token for new ID and access tokens.

        Raises:
            HiveReauthRequired: There is no refresh token, or Cognito rejected it.
            HiveConnectionError: Cognito could not be reached.
            HiveApiError: Cognito refused the call for another reason.
        """
        if not token:
            raise HiveReauthRequired("There is no refresh token")
        await self._ensure_client()
        params = {"REFRESH_TOKEN": token}
        if self.device_key is not None:
            params["DEVICE_KEY"] = self.device_key
        result = await self._cognito(
            "initiate_auth",
            {
                "NotAuthorizedException": HiveReauthRequired,
                "UserNotFoundException": HiveInvalidUsername,
            },
            ClientId=self._client_id,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters=params,
        )
        if "AuthenticationResult" in result:
            self._store_auth_result(result)
        return result

    def _store_auth_result(self, result: dict[str, Any]) -> None:
        """Keep the access token and new device keys from an AuthenticationResult."""
        auth_result = result["AuthenticationResult"]
        self.access_token = auth_result["AccessToken"]
        if "NewDeviceMetadata" in auth_result:
            self.device_group_key = auth_result["NewDeviceMetadata"]["DeviceGroupKey"]
            self.device_key = auth_result["NewDeviceMetadata"]["DeviceKey"]
            _LOGGER.debug("Cognito issued new device keys")

    # --- Trusted devices ---------------------------------------------------

    async def device_registration(self, device_name: str | None = None) -> None:
        """Register this machine as a trusted device so later logins skip the SMS.

        Confirms the device with Cognito as *device_name*, the hostname by
        default, then marks it as remembered. Once Cognito accepts the
        confirmation, ``device_password`` holds the new password the device
        will log in with; a failed confirmation leaves the saved credentials
        alone.

        Raises:
            HiveApiError: The login issued no device keys, or Cognito refused.
            HiveConnectionError: Cognito could not be reached.
        """
        group_key = self.device_group_key
        device_key = self.device_key
        if not (group_key and device_key):
            raise HiveApiError("Cognito issued no device keys to register")
        await self._ensure_client()
        # The verifier proves a new device password without sending it. Source:
        # amazon-cognito-identity-js AuthenticationHelper.generateHashDevice.
        device_password = base64.standard_b64encode(os.urandom(40)).decode("utf-8")
        combined_hash = hash_sha256(
            f"{group_key}{device_key}:{device_password}".encode()
        )
        salt = pad_hex(get_random(16))
        x_value = hex_to_long(hex_hash(salt + combined_hash))
        verifier = pad_hex(pow(G, x_value, BIG_N))
        await self._cognito(
            "confirm_device",
            AccessToken=self.access_token,
            DeviceKey=device_key,
            DeviceName=device_name or socket.gethostname(),
            DeviceSecretVerifierConfig={
                "PasswordVerifier": base64.standard_b64encode(
                    bytearray.fromhex(verifier)
                ).decode("utf-8"),
                "Salt": base64.standard_b64encode(bytearray.fromhex(salt)).decode(
                    "utf-8"
                ),
            },
        )
        self.device_password = device_password
        await self._cognito(
            "update_device_status",
            AccessToken=self.access_token,
            DeviceKey=device_key,
            DeviceRememberedStatus="remembered",
        )

    def get_device_data(self) -> tuple[str | None, str | None, str | None]:
        """Return the credentials to save for device logins.

        Returns:
            ``(device_group_key, device_key, device_password)``, to pass as
            ``device_data`` in the ``new_session`` config.
        """
        return (self.device_group_key, self.device_key, self.device_password)

    async def is_device_registered(self) -> bool:
        """Return whether Cognito knows this device and remembers it.

        Raises:
            HiveConnectionError: Cognito or the login page could not be reached.
            HiveApiError: The login page did not include the Cognito details.
        """
        token = self.access_token
        key = self.device_key
        if not token or not key:
            return False
        await self._ensure_client()
        try:
            result = await self._cognito("get_device", AccessToken=token, DeviceKey=key)
        except HiveApiError as err:
            _LOGGER.debug("This device is not registered: %s", err)
            return False
        attributes = result.get("Device", {}).get("DeviceAttributes", [])
        return any(
            attr.get("Name") == "dev:device_remembered_status"
            and attr.get("Value") == "remembered"
            for attr in attributes
        )

    async def forget_device(self, access_token: str, device_key: str) -> dict[str, Any]:
        """Remove the device from the account's trusted devices."""
        await self._ensure_client()
        return await self._cognito(
            "forget_device", AccessToken=access_token, DeviceKey=device_key
        )

    # --- SRP ---------------------------------------------------------------

    @staticmethod
    def _cognito_timestamp() -> str:
        """Return the current time in the format Cognito's SRP challenge expects."""
        # The regex strips the leading zero from the day number, as Cognito requires.
        return re.sub(
            r" 0(\d) ",
            r" \1 ",
            datetime.now(UTC).strftime("%a %b %d %H:%M:%S UTC %Y"),
        )

    def _new_srp_ephemeral(self) -> None:
        """Generate a fresh SRP client ephemeral (a, A) pair.

        SRP ephemerals must not be reused across handshakes, so this is
        called at the start of every authentication attempt.
        """
        self.small_a_value = generate_small_a()
        self.large_a_value = calculate_a(self.small_a_value)

    def _auth_params(self, device_key: str | None = None) -> dict[str, str]:
        """Return the parameters that start an SRP handshake."""
        params = {"USERNAME": self.username, "SRP_A": long_to_hex(self.large_a_value)}
        if device_key is not None:
            params["DEVICE_KEY"] = device_key
        return params

    def _srp_key(
        self, identity: str, secret: str, server_b_value: str, salt: str
    ) -> bytes:
        """Derive the SRP session key for *identity* and its *secret*.

        *identity* is the pool name plus user ID for a password login, or the
        device group key plus device key for a device login.

        Args:
            identity: Who is authenticating, as Cognito expects it.
            secret: The password or device password.
            server_b_value: Cognito's ``SRP_B`` value, a hex string.
            salt: Cognito's ``SALT`` value.

        Returns:
            The HKDF key that signs the challenge response.
        """
        server_b = hex_to_long(server_b_value)
        u_value = calculate_u(self.large_a_value, server_b)
        if u_value == 0:
            raise ValueError("U cannot be zero")
        identity_hash = hash_sha256(f"{identity}:{secret}".encode())
        x_value = hex_to_long(hex_hash(pad_hex(salt) + identity_hash))
        base = (server_b - K * pow(G, x_value, BIG_N)) % BIG_N
        s_value = pow(base, self.small_a_value + u_value * x_value, BIG_N)
        return compute_hkdf(
            bytearray.fromhex(pad_hex(s_value)),
            bytearray.fromhex(pad_hex(long_to_hex(u_value))),
        )

    async def _answer_srp_challenge(
        self,
        challenge_parameters: dict[str, Any],
        *,
        identity: str,
        secret: str,
        username: str,
    ) -> dict[str, Any]:
        """Build the signed response to a password or device SRP challenge."""
        salt = challenge_parameters["SALT"]
        secret_block = challenge_parameters["SECRET_BLOCK"]
        timestamp = self._cognito_timestamp()
        key = await asyncio.get_running_loop().run_in_executor(
            None, self._srp_key, identity, secret, challenge_parameters["SRP_B"], salt
        )
        message = (
            identity.encode()
            + base64.standard_b64decode(secret_block)
            + timestamp.encode()
        )
        signature = hmac.new(key, message, hashlib.sha256).digest()
        response = {
            "TIMESTAMP": timestamp,
            "USERNAME": username,
            "PASSWORD_CLAIM_SECRET_BLOCK": secret_block,
            "PASSWORD_CLAIM_SIGNATURE": base64.standard_b64encode(signature).decode(),
        }
        if self.device_key is not None:
            response["DEVICE_KEY"] = self.device_key
        return response
