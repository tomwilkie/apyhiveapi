"""AWS Cognito authentication for Hive: SRP login, SMS 2FA and device registration."""

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
from typing import Any

import boto3
import botocore

from apyhiveapi.api import HiveApi
from apyhiveapi.exceptions import (
    HiveApiError,
    HiveConnectionError,
    HiveFailedToRefreshTokens,
    HiveInvalid2FACode,
    HiveInvalidDeviceAuthentication,
    HiveInvalidPassword,
    HiveInvalidUsername,
    HiveRefreshTokenExpired,
    HiveUnknownConfiguration,
)
from apyhiveapi.srp import (
    G_HEX,
    N_HEX,
    calculate_u,
    compute_hkdf,
    get_random,
    hash_sha256,
    hex_hash,
    hex_to_long,
    long_to_hex,
    pad_hex,
)

_LOGGER = logging.getLogger(__name__)


class HiveAuth:
    """Log in to a Hive account through AWS Cognito and manage trusted devices."""

    PASSWORD_VERIFIER_CHALLENGE = "PASSWORD_VERIFIER"  # noqa: S105 - challenge name
    SMS_MFA_CHALLENGE = "SMS_MFA"
    DEVICE_VERIFIER_CHALLENGE = "DEVICE_SRP_AUTH"
    DEVICE_PASSWORD_CHALLENGE = "DEVICE_PASSWORD_VERIFIER"  # noqa: S105 - challenge name

    def __init__(
        self,
        username: str,
        password: str,
        device_group_key: str | None = None,
        device_key: str | None = None,
        device_password: str | None = None,
    ) -> None:
        """Set up the auth state.

        Args:
            username: Hive account email address.
            password: Hive account password.
            device_group_key: Saved trusted-device group key, if any.
            device_key: Saved trusted-device key, if any.
            device_password: Saved trusted-device password, if any.
        """
        self.loop: asyncio.AbstractEventLoop | None = None
        self.username = username
        self.password = password
        self.device_group_key = device_group_key
        self.device_key = device_key
        self.device_password = device_password
        self.access_token: str | None = None
        self.token_created: datetime | None = None
        self.api = HiveApi()
        self.user_id = "user_id"
        self.big_n: int = hex_to_long(N_HEX)
        self.g_value: int = hex_to_long(G_HEX)
        self.k: int = hex_to_long(hex_hash(pad_hex(N_HEX) + pad_hex(G_HEX)))
        self.small_a_value: int = self.generate_random_small_a()
        self.large_a_value: int = self.calculate_a()
        self.use_file = self.username == "use@file.com"
        self.file_response: dict[str, Any] = {
            "AuthenticationResult": {"AccessToken": "file"}
        }
        # Set by async_init once the SSO login page has been read.
        self.data: dict[str, str] | None = None
        self._pool_id: str | None = None
        self._client_id: str | None = None
        self._region: str | None = None
        self.client: Any = None

    async def async_init(self) -> None:
        """Read the Cognito pool details and create the boto3 client."""
        self.loop = asyncio.get_running_loop()
        self.data = await self.api.get_login_info()
        if not self.data:
            message = "SSO login page returned no data"
            raise HiveUnknownConfiguration(message)
        self._pool_id = self.data.get("UPID")
        self._client_id = self.data.get("CLIID")
        region_raw = self.data.get("REGION")
        if not self._pool_id or not region_raw:
            message = "SSO login page did not return required pool/region data"
            raise HiveUnknownConfiguration(message)
        self._region = region_raw.split("_")[0]
        # Cognito USER_SRP_AUTH does not use IAM credentials, but boto3 requires
        # non-None values, so these placeholders are deliberate.
        self.client = await self.loop.run_in_executor(
            None,
            functools.partial(
                boto3.client,
                "cognito-idp",
                self._region,
                aws_access_key_id="ACCESS_KEY",
                aws_secret_access_key="SECRET_KEY",  # noqa: S106 - placeholder, see above
                aws_session_token="SESSION_TOKEN",  # noqa: S106 - placeholder, see above
            ),
        )

    @property
    def _loop(self) -> asyncio.AbstractEventLoop:
        """Return the running event loop, remembering it on first use."""
        if self.loop is None:
            self.loop = asyncio.get_running_loop()
        return self.loop

    def _pool_name(self) -> str:
        """Return the Cognito user-pool name (the pool ID without its region)."""
        if not self._pool_id or "_" not in self._pool_id:
            message = f"Invalid pool ID format: {self._pool_id!r}"
            raise HiveUnknownConfiguration(message)
        return self._pool_id.split("_")[1]

    @staticmethod
    def _to_int(value: int | bytes | str) -> int:
        """Accept an int, bytes or hex string and return an int."""
        if isinstance(value, int):
            return value
        if isinstance(value, bytes):
            return int(value.hex(), 16)
        return int(str(value), 16)

    @staticmethod
    def _cognito_timestamp() -> str:
        """Return the current time in the format Cognito's SRP challenge expects."""
        # The regex strips the leading zero from the day number, as Cognito requires.
        return re.sub(
            r" 0(\d) ",
            r" \1 ",
            datetime.now(UTC).strftime("%a %b %d %H:%M:%S UTC %Y"),
        )

    # --- SRP ---------------------------------------------------------------

    def generate_random_small_a(self) -> int:
        """Generate the random SRP client secret ``a``."""
        random_long_int = get_random(128)
        return random_long_int % self.big_n

    def calculate_a(self) -> int:
        """Calculate the client's public SRP value ``A`` from ``a``."""
        big_a = pow(self.g_value, self.small_a_value, self.big_n)
        if (big_a % self.big_n) == 0:
            message = "Safety check for A failed"
            raise ValueError(message)
        return big_a

    def _new_srp_ephemeral(self) -> None:
        """Generate a fresh SRP client ephemeral (a, A) pair.

        SRP ephemerals must not be reused across handshakes, so this is
        called at the start of every authentication attempt.
        """
        self.small_a_value = self.generate_random_small_a()
        self.large_a_value = self.calculate_a()

    def _store_auth_result(self, result: dict[str, Any]) -> None:
        """Store tokens and any new device keys from an AuthenticationResult."""
        auth_result = result["AuthenticationResult"]
        self.access_token = auth_result["AccessToken"]
        self.token_created = datetime.now(UTC)
        if "NewDeviceMetadata" in auth_result:
            self.device_group_key = auth_result["NewDeviceMetadata"]["DeviceGroupKey"]
            self.device_key = auth_result["NewDeviceMetadata"]["DeviceKey"]
            _LOGGER.debug("Device keys stored successfully.")

    def get_password_authentication_key(
        self,
        username: str,
        password: str,
        server_b_value: int | bytes | str,
        salt: str,
    ) -> bytes:
        """Calculate the final HKDF from the computed S and U values and the key.

        Args:
            username: Username.
            password: Password.
            server_b_value: Server B value.
            salt: Generated salt.

        Returns:
            bytes: Computed HKDF value.
        """
        server_b_value = self._to_int(server_b_value)
        u_value = calculate_u(self.large_a_value, server_b_value)
        if u_value == 0:
            message = "U cannot be zero."
            raise ValueError(message)
        username_password = f"{self._pool_name()}{username}:{password}"
        username_password_hash = hash_sha256(username_password.encode("utf-8"))

        x_value = hex_to_long(hex_hash(pad_hex(salt) + username_password_hash))
        g_mod_pow_xn = pow(self.g_value, x_value, self.big_n)
        int_value2 = (server_b_value - self.k * g_mod_pow_xn) % self.big_n
        exp = self.small_a_value + u_value * x_value
        s_value = pow(int_value2, exp, self.big_n)
        return compute_hkdf(
            bytearray.fromhex(pad_hex(s_value)),
            bytearray.fromhex(pad_hex(long_to_hex(u_value))),
        )

    async def get_auth_params(self, *, is_device_login: bool = False) -> dict[str, Any]:
        """Return the parameters that start an SRP handshake."""
        _LOGGER.debug("get_auth_params - Getting auth params")
        auth_params: dict[str, Any] = {
            "USERNAME": self.username,
            "SRP_A": long_to_hex(self.large_a_value),
        }
        if is_device_login:
            auth_params["DEVICE_KEY"] = self.device_key
        return auth_params

    async def process_challenge(
        self, challenge_parameters: dict[str, Any]
    ) -> dict[str, Any]:
        """Answer the PASSWORD_VERIFIER challenge parameters."""
        self.user_id = challenge_parameters["USER_ID_FOR_SRP"]
        salt_hex = (
            challenge_parameters["SALT"]
            if isinstance(challenge_parameters["SALT"], str)
            else pad_hex(challenge_parameters["SALT"])
        )
        srp_b_hex = challenge_parameters["SRP_B"]
        secret_block_b64 = challenge_parameters["SECRET_BLOCK"]
        timestamp = self._cognito_timestamp()
        hkdf = await self._loop.run_in_executor(
            None,
            self.get_password_authentication_key,
            self.user_id,
            self.password,
            srp_b_hex,
            salt_hex,
        )
        secret_block_bytes = base64.standard_b64decode(secret_block_b64)
        msg = (
            bytearray(self._pool_name(), "utf-8")
            + bytearray(self.user_id, "utf-8")
            + bytearray(secret_block_bytes)
            + bytearray(timestamp, "utf-8")
        )
        hmac_obj = hmac.new(hkdf, msg, digestmod=hashlib.sha256)
        signature_string = base64.standard_b64encode(hmac_obj.digest())
        response = {
            "TIMESTAMP": timestamp,
            "USERNAME": self.user_id,
            "PASSWORD_CLAIM_SECRET_BLOCK": secret_block_b64,
            "PASSWORD_CLAIM_SIGNATURE": signature_string.decode("utf-8"),
        }
        if self.device_key is not None:
            response["DEVICE_KEY"] = self.device_key

        return response

    # --- Login flows -------------------------------------------------------

    async def login(self) -> dict[str, Any]:
        """Log in to a Hive account; handles the initial SRP auth only."""
        if self.use_file:
            _LOGGER.debug("login - Using file-based authentication.")
            return self.file_response

        if self.client is None:
            await self.async_init()

        self._new_srp_ephemeral()
        response = await self._initiate_srp_auth()

        if "AuthenticationResult" in response:
            _LOGGER.debug("login - Authenticated directly without a challenge.")
            self._store_auth_result(response)
            return response

        challenge_name = response.get("ChallengeName")
        if challenge_name == self.PASSWORD_VERIFIER_CHALLENGE:
            return await self._answer_password_verifier(response["ChallengeParameters"])

        _LOGGER.error("Unsupported Cognito challenge: %s", challenge_name)
        message = f"The {challenge_name} challenge is not supported"
        raise NotImplementedError(message)

    async def _initiate_srp_auth(self) -> dict[str, Any]:
        """Start the SRP handshake with Cognito and return its response."""
        auth_params = await self.get_auth_params()
        _LOGGER.debug("login - Initiating SRP auth with Cognito.")
        try:
            return await self._loop.run_in_executor(
                None,
                functools.partial(
                    self.client.initiate_auth,
                    AuthFlow="USER_SRP_AUTH",
                    AuthParameters=auth_params,
                    ClientId=self._client_id,
                ),
            )
        except botocore.exceptions.ClientError as err:
            code = (err.response or {}).get("Error", {}).get("Code", "")
            if code == "UserNotFoundException":
                _LOGGER.warning("Cognito auth failed: user not found.")
                raise HiveInvalidUsername from err
            _LOGGER.warning("Cognito auth failed: %s", code)
            raise HiveApiError from err
        except botocore.exceptions.EndpointConnectionError as err:
            _LOGGER.warning("Cognito auth failed: cannot reach endpoint.")
            raise HiveApiError from err

    async def _answer_password_verifier(
        self, challenge_parameters: dict[str, Any]
    ) -> dict[str, Any]:
        """Answer the PASSWORD_VERIFIER challenge and store the resulting tokens."""
        _LOGGER.debug("login - Processing PASSWORD_VERIFIER challenge.")
        challenge_response = await self.process_challenge(challenge_parameters)
        try:
            result = await self._loop.run_in_executor(
                None,
                functools.partial(
                    self.client.respond_to_auth_challenge,
                    ClientId=self._client_id,
                    ChallengeName=self.PASSWORD_VERIFIER_CHALLENGE,
                    ChallengeResponses=challenge_response,
                ),
            )
        except botocore.exceptions.ClientError as err:
            code = (err.response or {}).get("Error", {}).get("Code", "")
            if code == "NotAuthorizedException":
                _LOGGER.warning("Cognito auth challenge failed: not authorised.")
                raise HiveInvalidPassword from err
            if code == "ResourceNotFoundException":
                _LOGGER.warning(
                    "Cognito auth challenge failed: device resource not found."
                )
                raise HiveInvalidDeviceAuthentication from err
            _LOGGER.warning("Cognito auth challenge failed: %s", code)
            raise HiveApiError from err
        except botocore.exceptions.EndpointConnectionError as err:
            _LOGGER.warning("Cognito auth challenge failed: cannot reach endpoint.")
            raise HiveApiError from err

        _LOGGER.debug("login - SRP auth challenge completed successfully.")
        if "AuthenticationResult" in result:
            self._store_auth_result(result)
        return result

    async def device_login(self) -> dict[str, Any]:
        """Log in with the saved trusted-device credentials (DEVICE_SRP_AUTH).

        Returns:
            dict: Authentication result with tokens.
        """
        _LOGGER.debug("device_login - Starting device SRP authentication.")

        if self.client is None:
            await self.async_init()

        self._new_srp_ephemeral()
        auth_params = await self.get_auth_params(is_device_login=True)

        _LOGGER.debug("device_login - Processing DEVICE_SRP_AUTH challenge.")
        try:
            initial_result = await self._loop.run_in_executor(
                None,
                functools.partial(
                    self.client.respond_to_auth_challenge,
                    ClientId=self._client_id,
                    ChallengeName=self.DEVICE_VERIFIER_CHALLENGE,
                    ChallengeResponses=auth_params,
                ),
            )

            device_challenge_response = await self.process_device_challenge(
                initial_result["ChallengeParameters"]
            )
            result = await self._loop.run_in_executor(
                None,
                functools.partial(
                    self.client.respond_to_auth_challenge,
                    ClientId=self._client_id,
                    ChallengeName=self.DEVICE_PASSWORD_CHALLENGE,
                    ChallengeResponses=device_challenge_response,
                ),
            )
        except botocore.exceptions.ClientError as err:
            error_code = (err.response or {}).get("Error", {}).get("Code", "")
            if error_code in ("ResourceNotFoundException", "NotAuthorizedException"):
                _LOGGER.warning(
                    "Device login failed: device not registered"
                    " or not remembered (%s).",
                    error_code,
                )
                raise HiveInvalidDeviceAuthentication from err
            raise
        except botocore.exceptions.EndpointConnectionError as err:
            _LOGGER.warning("Device login failed: cannot reach endpoint.")
            raise HiveApiError from err

        _LOGGER.debug("device_login - Device authentication completed successfully.")
        return result

    async def sms_2fa(
        self, entered_code: str | int, challenge_parameters: dict[str, Any]
    ) -> dict[str, Any]:
        """Answer the SMS two-factor challenge with the code the user received."""
        session = challenge_parameters.get("Session")
        code = str(entered_code)
        _LOGGER.debug("sms_2fa - Submitting SMS 2FA code to Cognito.")
        try:
            result = await self._loop.run_in_executor(
                None,
                functools.partial(
                    self.client.respond_to_auth_challenge,
                    ClientId=self._client_id,
                    ChallengeName=self.SMS_MFA_CHALLENGE,
                    Session=session,
                    ChallengeResponses={
                        "SMS_MFA_CODE": code,
                        "USERNAME": self.user_id,
                    },
                ),
            )
            if result and "AuthenticationResult" in result:
                self._store_auth_result(result)
        except botocore.exceptions.ClientError as err:
            error_code = (err.response or {}).get("Error", {}).get("Code", "")
            if error_code in ("NotAuthorizedException", "CodeMismatchException"):
                _LOGGER.warning("2FA code rejected by Cognito.")
                raise HiveInvalid2FACode from err
            _LOGGER.warning("2FA failed: %s", error_code)
            raise HiveApiError from err
        except botocore.exceptions.EndpointConnectionError as err:
            _LOGGER.warning("2FA failed: cannot reach Cognito endpoint.")
            raise HiveApiError from err

        _LOGGER.debug("sms_2fa - 2FA authentication completed successfully.")
        return result

    async def refresh_token(self, token: str | None) -> dict[str, Any]:
        """Exchange the refresh token for new ID and access tokens."""
        if self.client is None:
            await self.async_init()
        _LOGGER.debug("refresh_token - Requesting token refresh from Cognito.")
        auth_params: dict[str, Any] = {"REFRESH_TOKEN": token}
        if self.device_key is not None:
            auth_params["DEVICE_KEY"] = self.device_key

        try:
            result = await self._loop.run_in_executor(
                None,
                functools.partial(
                    self.client.initiate_auth,
                    ClientId=self._client_id,
                    AuthFlow="REFRESH_TOKEN_AUTH",
                    AuthParameters=auth_params,
                ),
            )
        except botocore.exceptions.ClientError as err:
            error = (err.response or {}).get("Error", {})
            error_code = error.get("Code")
            error_message = error.get("Message", "")
            if (
                error_code == "NotAuthorizedException"
                and "Invalid Refresh Token" in str(error_message)
            ):
                _LOGGER.warning("Refresh token is invalid or expired.")
                raise HiveRefreshTokenExpired from err

            _LOGGER.warning(
                "refresh_token - Token refresh failed: %s - %s",
                error_code,
                error_message,
            )
            raise HiveFailedToRefreshTokens from err
        except botocore.exceptions.EndpointConnectionError as err:
            _LOGGER.warning(
                "refresh_token - Token refresh failed: cannot reach Cognito endpoint."
            )
            raise HiveConnectionError from err

        _LOGGER.debug("refresh_token - Cognito token refresh completed successfully.")
        return result

    # --- Trusted devices ---------------------------------------------------

    def generate_hash_device(
        self, device_group_key: str | None, device_key: str | None
    ) -> dict[str, str]:
        """Generate the password verifier that registers this device with Cognito."""
        # Source: amazon-cognito-identity-js AuthenticationHelper.generateHashDevice.
        device_password = base64.standard_b64encode(os.urandom(40)).decode("utf-8")
        combined_string = f"{device_group_key}{device_key}:{device_password}"
        combined_string_hash = hash_sha256(combined_string.encode("utf-8"))
        salt = pad_hex(get_random(16))

        x_value = hex_to_long(hex_hash(salt + combined_string_hash))
        g_value = hex_to_long(G_HEX)
        big_n = hex_to_long(N_HEX)
        verifier_device_not_padded = pow(g_value, x_value, big_n)
        verifier = pad_hex(verifier_device_not_padded)

        device_secret_verifier_config = {
            "PasswordVerifier": base64.standard_b64encode(
                bytearray.fromhex(verifier)
            ).decode("utf-8"),
            "Salt": base64.standard_b64encode(bytearray.fromhex(salt)).decode("utf-8"),
        }
        self.device_password = device_password
        return device_secret_verifier_config

    def get_device_authentication_key(  # pylint: disable=too-many-positional-arguments
        self,
        device_group_key: str | None,
        device_key: str | None,
        device_password: str | None,
        server_b_value: int,
        salt: str,
    ) -> bytes:
        """Calculate the HKDF that answers a device SRP challenge."""
        u_value = calculate_u(self.large_a_value, server_b_value)
        if u_value == 0:
            message = "U cannot be zero."
            raise ValueError(message)
        username_password = f"{device_group_key}{device_key}:{device_password}"
        username_password_hash = hash_sha256(username_password.encode("utf-8"))

        x_value = hex_to_long(hex_hash(pad_hex(salt) + username_password_hash))
        g_mod_pow_xn = pow(self.g_value, x_value, self.big_n)
        int_value2 = (server_b_value - self.k * g_mod_pow_xn) % self.big_n
        exp = self.small_a_value + u_value * x_value
        s_value = pow(int_value2, exp, self.big_n)
        return compute_hkdf(
            bytearray.fromhex(pad_hex(s_value)),
            bytearray.fromhex(pad_hex(long_to_hex(u_value))),
        )

    async def process_device_challenge(
        self, challenge_parameters: dict[str, Any]
    ) -> dict[str, Any]:
        """Answer the DEVICE_PASSWORD_VERIFIER challenge parameters."""
        if self.device_group_key is None or self.device_key is None:
            message = "Device login attempted without saved device credentials"
            raise HiveInvalidDeviceAuthentication(message)
        username = challenge_parameters["USERNAME"]
        salt_hex = (
            challenge_parameters["SALT"]
            if isinstance(challenge_parameters["SALT"], str)
            else pad_hex(challenge_parameters["SALT"])
        )
        srp_b_hex = challenge_parameters["SRP_B"]
        secret_block_b64 = challenge_parameters["SECRET_BLOCK"]
        timestamp = self._cognito_timestamp()
        hkdf = self.get_device_authentication_key(
            self.device_group_key,
            self.device_key,
            self.device_password,
            hex_to_long(srp_b_hex),
            salt_hex,
        )
        secret_block_bytes = base64.standard_b64decode(secret_block_b64)
        msg = (
            bytearray(self.device_group_key, "utf-8")
            + bytearray(self.device_key, "utf-8")
            + bytearray(secret_block_bytes)
            + bytearray(timestamp, "utf-8")
        )
        hmac_obj = hmac.new(hkdf, msg, digestmod=hashlib.sha256)
        signature_string = base64.standard_b64encode(hmac_obj.digest())
        return {
            "TIMESTAMP": timestamp,
            "USERNAME": username,
            "PASSWORD_CLAIM_SECRET_BLOCK": secret_block_b64,
            "PASSWORD_CLAIM_SIGNATURE": signature_string.decode("utf-8"),
            "DEVICE_KEY": self.device_key,
        }

    async def device_registration(self, device_name: str | None = None) -> None:
        """Register this machine as a trusted device so later logins skip the SMS."""
        _LOGGER.debug("device_registration - Registering device with Hive.")
        await self.confirm_device(device_name)
        await self.update_device_status()

    async def confirm_device(self, device_name: str | None = None) -> dict[str, Any]:
        """Confirm the device with Cognito under *device_name*."""
        if self.client is None:
            await self.async_init()

        if device_name is None:
            device_name = socket.gethostname()

        try:
            device_secret_verifier_config = self.generate_hash_device(
                self.device_group_key, self.device_key
            )
            return await self._loop.run_in_executor(
                None,
                functools.partial(
                    self.client.confirm_device,
                    AccessToken=self.access_token,
                    DeviceKey=self.device_key,
                    DeviceName=device_name,
                    DeviceSecretVerifierConfig=device_secret_verifier_config,
                ),
            )
        except botocore.exceptions.ClientError as err:
            code = (err.response or {}).get("Error", {}).get("Code", "")
            if code == "CodeMismatchException":
                raise HiveInvalid2FACode from err
            raise HiveApiError from err
        except botocore.exceptions.EndpointConnectionError as err:
            raise HiveApiError from err

    async def update_device_status(self) -> dict[str, Any]:
        """Mark the device as remembered so Cognito trusts it."""
        if self.client is None:
            await self.async_init()
        try:
            return await self._loop.run_in_executor(
                None,
                functools.partial(
                    self.client.update_device_status,
                    AccessToken=self.access_token,
                    DeviceKey=self.device_key,
                    DeviceRememberedStatus="remembered",
                ),
            )
        except botocore.exceptions.EndpointConnectionError as err:
            raise HiveApiError from err

    async def get_device_data(
        self,
    ) -> tuple[str | None, str | None, str | None, datetime | None]:
        """Return the credentials to save for device logins.

        Returns:
            tuple: ``(device_group_key, device_key, device_password, token_created)``.
                ``token_created`` marks when the current tokens were issued. Pass
                all four values as ``device_data`` in the ``start_session`` config
                so the session can compute token expiry from the real issue time.
        """
        return (
            self.device_group_key,
            self.device_key,
            self.device_password,
            self.token_created,
        )

    async def is_device_registered(
        self, access_token: str | None = None, device_key: str | None = None
    ) -> bool:
        """Check if the current device is registered with Cognito.

        Args:
            access_token: Access token. Defaults to ``self.access_token``.
            device_key: Device key. Defaults to ``self.device_key``.

        Returns:
            bool: True if device is registered and remembered, False otherwise.

        Raises:
            HiveApiError: If unable to reach Cognito endpoint.
        """
        if self.client is None:
            await self.async_init()

        token = access_token or self.access_token
        key = device_key or self.device_key

        if not token or not key:
            _LOGGER.debug(
                "is_device_registered - Missing access token or device key, "
                "device not registered"
            )
            return False

        _LOGGER.debug(
            "is_device_registered - Checking device registration status for device: %s",
            key,
        )

        try:
            result = await self._loop.run_in_executor(
                None,
                functools.partial(
                    self.client.get_device,
                    AccessToken=token,
                    DeviceKey=key,
                ),
            )
        except botocore.exceptions.ClientError as err:
            self._log_device_lookup_error(err, key)
            return False
        except botocore.exceptions.EndpointConnectionError as err:
            _LOGGER.warning(
                "is_device_registered - Cannot reach Cognito endpoint: %s", err
            )
            raise HiveApiError from err

        if result and "Device" in result:
            for attr in result["Device"].get("DeviceAttributes", []):
                if (
                    attr.get("Name") == "dev:device_remembered_status"
                    and attr.get("Value") == "remembered"
                ):
                    _LOGGER.debug(
                        "is_device_registered - Device %s is registered and remembered",
                        key,
                    )
                    return True

            _LOGGER.debug(
                "is_device_registered - Device %s is registered but not remembered",
                key,
            )

        return False

    @staticmethod
    def _log_device_lookup_error(
        err: botocore.exceptions.ClientError, key: str
    ) -> None:
        """Log why Cognito refused to describe a device."""
        error = (err.response or {}).get("Error", {})
        error_code = error.get("Code")
        error_message = error.get("Message", "")

        if error_code == "ResourceNotFoundException":
            _LOGGER.debug("is_device_registered - Device %s not found in Cognito", key)
        elif error_code == "NotAuthorizedException":
            _LOGGER.warning(
                "is_device_registered - Not authorized to check device status: %s",
                error_message,
            )
        else:
            _LOGGER.error(
                "is_device_registered - Error checking device status: %s - %s",
                error_code,
                error_message,
                exc_info=err,
            )

    async def forget_device(self, access_token: str, device_key: str) -> dict[str, Any]:
        """Remove the device from the account's trusted devices."""
        if self.client is None:
            await self.async_init()
        try:
            return await self._loop.run_in_executor(
                None,
                functools.partial(
                    self.client.forget_device,
                    AccessToken=access_token,
                    DeviceKey=device_key,
                ),
            )
        except (
            botocore.exceptions.ClientError,
            botocore.exceptions.EndpointConnectionError,
        ) as err:
            raise HiveApiError from err
