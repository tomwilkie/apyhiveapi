"""Unit tests for HiveAuth: SRP login, SMS 2FA, token refresh and trusted devices."""

import base64
import hashlib
import hmac
import logging
import re
import threading
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, call, patch

import botocore.exceptions
import pytest

from apyhiveapi.auth import HiveAuth
from apyhiveapi.exceptions import (
    HiveApiError,
    HiveConnectionError,
    HiveInvalid2FACode,
    HiveInvalidPassword,
    HiveInvalidUsername,
    HiveReauthRequired,
)
from apyhiveapi.srp import (
    G_HEX,
    N_HEX,
    calculate_u,
    compute_hkdf,
    hash_sha256,
    hex_hash,
    hex_to_long,
    long_to_hex,
    pad_hex,
)

USERNAME = "user@test.com"
PASSWORD = "pass"
CLIENT_ID = "client-id"
POOL_ID = "eu-west-1_pool"
LOGIN_INFO = {"UPID": POOL_ID, "CLIID": CLIENT_ID, "REGION": POOL_ID}
DEVICE_CREDENTIALS = {
    "device_group_key": "grp-key",
    "device_key": "dev-key",
    "device_password": "dev-pass",
}
FROZEN_NOW = datetime(2024, 1, 5, 3, 4, 5, tzinfo=UTC)
FROZEN_TIMESTAMP = "Fri Jan 5 03:04:05 UTC 2024"
# Cognito's timestamp format: no leading zero on the day number.
TIMESTAMP_RE = re.compile(
    r"[A-Z][a-z]{2} [A-Z][a-z]{2} [1-9]\d? \d{2}:\d{2}:\d{2} UTC \d{4}"
)

# Cognito's answers to the SRP handshakes: the values the client has to sign.
PASSWORD_CHALLENGE = {
    "ChallengeName": "PASSWORD_VERIFIER",
    "ChallengeParameters": {
        "USER_ID_FOR_SRP": "uuid-user",
        "SALT": "aabbccdd",
        "SRP_B": "ccddee",
        "SECRET_BLOCK": "YWJj",
    },
}
DEVICE_CHALLENGE = {
    "ChallengeName": "DEVICE_PASSWORD_VERIFIER",
    "ChallengeParameters": {
        "USERNAME": "uuid-user",
        "SALT": "11223344",
        "SRP_B": "55667788",
        "SECRET_BLOCK": "dGVzdA==",
    },
}
SMS_CHALLENGE = {"ChallengeName": "SMS_MFA", "Session": "session-1"}
AUTH_RESULT = {"AuthenticationResult": {"AccessToken": "access-token"}}
NEW_DEVICE_RESULT = {
    "AuthenticationResult": {
        "AccessToken": "access-token",
        "NewDeviceMetadata": {"DeviceGroupKey": "new-grp", "DeviceKey": "new-dev"},
    }
}
REMEMBERED_DEVICE = {
    "Device": {
        "DeviceKey": "dev-key",
        "DeviceAttributes": [
            {"Name": "device_name", "Value": "my-host"},
            {"Name": "dev:device_remembered_status", "Value": "remembered"},
        ],
    }
}


def make_auth(
    username=USERNAME,
    password=PASSWORD,
    device_group_key=None,
    device_key=None,
    device_password=None,
    access_token=None,
):
    """Return a HiveAuth with a MagicMock Cognito client, so async_init is skipped."""
    auth = HiveAuth(
        username,
        password,
        device_group_key=device_group_key,
        device_key=device_key,
        device_password=device_password,
    )
    auth.client = MagicMock()
    auth._client_id = CLIENT_ID
    auth._pool_id = POOL_ID
    auth.access_token = access_token
    return auth


def client_error(code, message="Cognito refused", operation="InitiateAuth"):
    """Return the botocore error Cognito raises for an error code."""
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": message}}, operation
    )


def expected_signature(key, identity, secret_block, timestamp):
    """Sign an SRP challenge the way Cognito checks it."""
    message = identity.encode() + base64.standard_b64decode(secret_block)
    message += timestamp.encode()
    return base64.standard_b64encode(
        hmac.new(key, message, hashlib.sha256).digest()
    ).decode()


def cognito_server_key(auth, identity, secret, salt, server_secret):
    """Run Cognito's side of the SRP handshake against the client's public A.

    Cognito only holds the password verifier, so this is an independent check
    of the client maths: both sides must end up with the same HKDF key.

    Returns:
        Cognito's ``SRP_B`` and the key it would use to check the signature.
    """
    x_value = hex_to_long(
        hex_hash(pad_hex(salt) + hash_sha256(f"{identity}:{secret}".encode()))
    )
    verifier = pow(auth.g_value, x_value, auth.big_n)
    srp_b = (auth.k * verifier + pow(auth.g_value, server_secret, auth.big_n)) % (
        auth.big_n
    )
    u_value = calculate_u(auth.large_a_value, srp_b)
    shared = pow(
        auth.large_a_value * pow(verifier, u_value, auth.big_n),
        server_secret,
        auth.big_n,
    )
    return srp_b, compute_hkdf(
        bytearray.fromhex(pad_hex(shared)),
        bytearray.fromhex(pad_hex(long_to_hex(u_value))),
    )


# Tests for the constructor


class TestInit:
    def test_defaults(self):
        websession = object()
        auth = HiveAuth(USERNAME, PASSWORD, websession=websession)
        assert auth.username == USERNAME
        assert auth.password == PASSWORD
        assert auth.user_id == USERNAME
        assert auth.device_group_key is None
        assert auth.device_key is None
        assert auth.device_password is None
        assert auth.access_token is None
        assert auth.token_created is None
        assert auth.client is None
        assert auth._pool_id is None
        assert auth._client_id is None
        assert auth._websession is websession
        assert auth.use_file is False
        assert auth.file_response == {"AuthenticationResult": {"AccessToken": "file"}}

    def test_keeps_the_saved_device_credentials(self):
        auth = HiveAuth(USERNAME, PASSWORD, **DEVICE_CREDENTIALS)
        assert auth.device_group_key == "grp-key"
        assert auth.device_key == "dev-key"
        assert auth.device_password == "dev-pass"

    @pytest.mark.parametrize(
        ("username", "use_file"),
        [("use@file.com", True), ("user@test.com", False), ("USE@FILE.COM", False)],
    )
    def test_file_mode_is_only_for_the_file_username(self, username, use_file):
        assert HiveAuth(username, "").use_file is use_file

    def test_srp_group_constants(self):
        auth = HiveAuth(USERNAME, PASSWORD)
        assert auth.big_n == hex_to_long(N_HEX)
        assert auth.g_value == 2
        assert auth.k == hex_to_long(hex_hash(pad_hex(N_HEX) + pad_hex(G_HEX)))

    def test_srp_ephemeral_is_a_pair_of_ints_below_n(self):
        auth = HiveAuth(USERNAME, PASSWORD)
        assert isinstance(auth.small_a_value, int)
        assert isinstance(auth.large_a_value, int)
        assert 0 < auth.small_a_value < auth.big_n
        assert 0 < auth.large_a_value < auth.big_n
        assert auth.large_a_value == pow(auth.g_value, auth.small_a_value, auth.big_n)

    def test_each_instance_gets_its_own_ephemeral(self):
        first = HiveAuth(USERNAME, PASSWORD)
        second = HiveAuth(USERNAME, PASSWORD)
        assert first.small_a_value != second.small_a_value
        assert first.large_a_value != second.large_a_value


# Tests for async_init and _ensure_client


class TestAsyncInit:
    async def test_reads_the_pool_details_and_creates_the_client(self):
        websession = object()
        auth = HiveAuth(USERNAME, PASSWORD, websession=websession)
        boto_client = MagicMock()
        info = {"UPID": "eu-west-1_abc", "CLIID": CLIENT_ID, "REGION": "eu-west-1_abc"}
        with (
            patch(
                "apyhiveapi.auth.get_login_info", new=AsyncMock(return_value=info)
            ) as get_login_info,
            patch(
                "apyhiveapi.auth.boto3.client", return_value=boto_client
            ) as boto3_client,
        ):
            await auth.async_init()
        get_login_info.assert_awaited_once_with(websession)
        boto3_client.assert_called_once_with(
            "cognito-idp",
            "eu-west-1",
            aws_access_key_id="ACCESS_KEY",
            aws_secret_access_key="SECRET_KEY",
            aws_session_token="SESSION_TOKEN",
        )
        assert auth._pool_id == "eu-west-1_abc"
        assert auth._client_id == CLIENT_ID
        assert auth.client is boto_client

    @pytest.mark.parametrize(
        ("pool_id", "region"),
        [("eu-west-1_abc", "eu-west-1"), ("ap-southeast-2_XyzPool", "ap-southeast-2")],
    )
    async def test_region_is_the_pool_id_before_the_underscore(self, pool_id, region):
        auth = HiveAuth(USERNAME, PASSWORD)
        info = {"UPID": pool_id, "CLIID": CLIENT_ID, "REGION": pool_id}
        with (
            patch("apyhiveapi.auth.get_login_info", new=AsyncMock(return_value=info)),
            patch("apyhiveapi.auth.boto3.client") as boto3_client,
        ):
            await auth.async_init()
        assert boto3_client.call_args.args == ("cognito-idp", region)

    async def test_without_a_websession_get_login_info_makes_its_own(self):
        auth = HiveAuth(USERNAME, PASSWORD)
        with (
            patch(
                "apyhiveapi.auth.get_login_info",
                new=AsyncMock(return_value=LOGIN_INFO),
            ) as get_login_info,
            patch("apyhiveapi.auth.boto3.client"),
        ):
            await auth.async_init()
        get_login_info.assert_awaited_once_with(None)

    @pytest.mark.parametrize("error", [HiveConnectionError, HiveApiError])
    async def test_login_page_errors_propagate(self, error):
        auth = HiveAuth(USERNAME, PASSWORD)
        with (
            patch(
                "apyhiveapi.auth.get_login_info",
                new=AsyncMock(side_effect=error("login page")),
            ),
            patch("apyhiveapi.auth.boto3.client") as boto3_client,
            pytest.raises(error, match="login page"),
        ):
            await auth.async_init()
        boto3_client.assert_not_called()
        assert auth.client is None
        assert auth._pool_id is None


class TestEnsureClient:
    async def test_initialises_once(self):
        auth = HiveAuth(USERNAME, PASSWORD)
        with (
            patch(
                "apyhiveapi.auth.get_login_info",
                new=AsyncMock(return_value=LOGIN_INFO),
            ) as get_login_info,
            patch("apyhiveapi.auth.boto3.client") as boto3_client,
        ):
            await auth._ensure_client()
            first_client = auth.client
            await auth._ensure_client()
        get_login_info.assert_awaited_once()
        boto3_client.assert_called_once()
        assert auth.client is first_client

    async def test_does_nothing_once_the_client_is_set(self):
        auth = make_auth()
        with patch.object(auth, "async_init", new=AsyncMock()) as async_init:
            await auth._ensure_client()
        async_init.assert_not_awaited()

    @pytest.mark.parametrize(
        ("method", "args", "cognito_method"),
        [
            ("login", (), "initiate_auth"),
            ("device_login", (), "respond_to_auth_challenge"),
            ("sms_2fa", ("123456", SMS_CHALLENGE), "respond_to_auth_challenge"),
            ("refresh_token", ("refresh-token",), "initiate_auth"),
            ("confirm_device", ("my-device",), "confirm_device"),
            ("update_device_status", (), "update_device_status"),
            ("is_device_registered", (), "get_device"),
            ("forget_device", ("access-token", "dev-key"), "forget_device"),
        ],
    )
    async def test_every_cognito_call_creates_the_client_first(
        self, method, args, cognito_method
    ):
        auth = HiveAuth(USERNAME, PASSWORD, **DEVICE_CREDENTIALS)
        auth.access_token = "access-token"
        client = MagicMock()
        client.initiate_auth.return_value = AUTH_RESULT
        client.respond_to_auth_challenge.side_effect = [DEVICE_CHALLENGE, AUTH_RESULT]
        client.get_device.return_value = REMEMBERED_DEVICE

        async def fake_init():
            auth.client = client
            auth._client_id = CLIENT_ID
            auth._pool_id = POOL_ID

        with patch.object(
            auth, "async_init", new=AsyncMock(side_effect=fake_init)
        ) as async_init:
            await getattr(auth, method)(*args)
        async_init.assert_awaited_once()
        assert getattr(client, cognito_method).called


# Tests for _cognito


class TestCognito:
    async def test_returns_the_result_and_passes_the_kwargs_through(self):
        auth = make_auth()
        auth.client.get_device.return_value = {"Device": {"DeviceKey": "dev-key"}}
        result = await auth._cognito("get_device", AccessToken="tok", DeviceKey="key")
        assert result == {"Device": {"DeviceKey": "dev-key"}}
        auth.client.get_device.assert_called_once_with(
            AccessToken="tok", DeviceKey="key"
        )

    async def test_a_code_map_is_not_sent_to_cognito(self):
        auth = make_auth()
        await auth._cognito("get_device", {"X": HiveApiError}, DeviceKey="key")
        auth.client.get_device.assert_called_once_with(DeviceKey="key")

    async def test_runs_the_call_off_the_event_loop_thread(self):
        auth = make_auth()

        def record_thread(**_kwargs):
            return {"thread": threading.get_ident()}

        auth.client.get_device.side_effect = record_thread
        result = await auth._cognito("get_device", DeviceKey="key")
        assert result["thread"] != threading.get_ident()

    async def test_a_mapped_code_becomes_the_named_exception(self):
        auth = make_auth()
        err = client_error("UserNotFoundException", "User does not exist.")
        auth.client.initiate_auth.side_effect = err
        with pytest.raises(HiveInvalidUsername) as exc_info:
            await auth._cognito(
                "initiate_auth",
                {"UserNotFoundException": HiveInvalidUsername},
                AuthFlow="USER_SRP_AUTH",
            )
        assert exc_info.value.__cause__ is err
        assert str(exc_info.value) == (
            "Cognito initiate_auth failed with UserNotFoundException: "
            "User does not exist."
        )

    @pytest.mark.parametrize(
        "code", ["InternalErrorException", "TooManyRequestsException"]
    )
    async def test_transient_codes_become_connection_errors(self, code):
        auth = make_auth()
        err = client_error(code, "Try again later")
        auth.client.initiate_auth.side_effect = err
        # A transient code is a connection error even when the code map names it.
        with pytest.raises(HiveConnectionError) as exc_info:
            await auth._cognito("initiate_auth", {code: HiveInvalidUsername})
        assert exc_info.value.__cause__ is err
        assert str(exc_info.value) == (
            f"Cognito initiate_auth failed with {code}: Try again later"
        )

    @pytest.mark.parametrize(
        "code_map", [None, {}, {"CodeMismatchException": HiveInvalid2FACode}]
    )
    async def test_an_unmapped_code_becomes_an_api_error(self, code_map):
        auth = make_auth()
        err = client_error(
            "LimitExceededException", "Attempt limit exceeded", "RespondToAuthChallenge"
        )
        auth.client.respond_to_auth_challenge.side_effect = err
        with pytest.raises(HiveApiError) as exc_info:
            await auth._cognito("respond_to_auth_challenge", code_map)
        assert exc_info.type is HiveApiError
        assert exc_info.value.__cause__ is err
        assert str(exc_info.value) == (
            "Cognito respond_to_auth_challenge failed with LimitExceededException: "
            "Attempt limit exceeded"
        )

    @pytest.mark.parametrize("response", [None, {}, {"Error": None}, {"Error": {}}])
    async def test_a_client_error_without_details_is_still_translated(self, response):
        auth = make_auth()
        err = botocore.exceptions.ClientError({}, "GetDevice")
        err.response = response
        auth.client.get_device.side_effect = err
        with pytest.raises(HiveApiError) as exc_info:
            await auth._cognito("get_device")
        assert exc_info.value.__cause__ is err
        assert str(exc_info.value) == "Cognito get_device failed with : "

    @pytest.mark.parametrize(
        ("error_type", "kwargs"),
        [
            (botocore.exceptions.EndpointConnectionError, {"endpoint_url": "x"}),
            (botocore.exceptions.ConnectTimeoutError, {"endpoint_url": "x"}),
            (botocore.exceptions.ReadTimeoutError, {"endpoint_url": "x"}),
            (botocore.exceptions.ConnectionError, {"error": "boom"}),
            (botocore.exceptions.HTTPClientError, {"error": "boom"}),
        ],
    )
    async def test_network_errors_become_connection_errors(self, error_type, kwargs):
        auth = make_auth()
        err = error_type(**kwargs)
        auth.client.initiate_auth.side_effect = err
        with pytest.raises(HiveConnectionError) as exc_info:
            await auth._cognito("initiate_auth", AuthFlow="USER_SRP_AUTH")
        assert exc_info.value.__cause__ is err
        assert (
            str(exc_info.value) == f"Could not reach Cognito for initiate_auth: {err}"
        )


# Tests for login


class TestLogin:
    async def test_file_mode_answers_from_the_fixture_without_cognito(self):
        auth = HiveAuth("use@file.com", "")
        with patch("apyhiveapi.auth.get_login_info", new=AsyncMock()) as get_login_info:
            result = await auth.login()
        assert result is auth.file_response
        assert result == {"AuthenticationResult": {"AccessToken": "file"}}
        get_login_info.assert_not_awaited()
        assert auth.client is None
        assert auth.access_token is None

    async def test_starts_an_srp_handshake_without_the_device_key(self):
        auth = make_auth(device_key="dev-key")
        auth.client.initiate_auth.return_value = AUTH_RESULT
        await auth.login()
        auth.client.initiate_auth.assert_called_once_with(
            AuthFlow="USER_SRP_AUTH",
            AuthParameters={
                "USERNAME": USERNAME,
                "SRP_A": long_to_hex(auth.large_a_value),
            },
            ClientId=CLIENT_ID,
        )

    async def test_uses_a_fresh_srp_ephemeral_for_every_attempt(self):
        auth = make_auth()
        auth.client.initiate_auth.return_value = AUTH_RESULT
        initial_a = auth.large_a_value
        await auth.login()
        second_a = auth.large_a_value
        await auth.login()
        third_a = auth.large_a_value
        assert len({initial_a, second_a, third_a}) == 3
        assert auth.large_a_value == pow(auth.g_value, auth.small_a_value, auth.big_n)
        sent = [
            kwargs["AuthParameters"]["SRP_A"]
            for _name, _args, kwargs in auth.client.initiate_auth.mock_calls
        ]
        assert sent == [long_to_hex(second_a), long_to_hex(third_a)]

    async def test_stores_a_direct_authentication_result(self, caplog):
        auth = make_auth()
        auth.client.initiate_auth.return_value = NEW_DEVICE_RESULT
        before = datetime.now(UTC)
        with caplog.at_level(logging.DEBUG, logger="apyhiveapi.auth"):
            result = await auth.login()
        assert result is NEW_DEVICE_RESULT
        assert auth.access_token == "access-token"
        assert auth.token_created.tzinfo is UTC
        assert before <= auth.token_created <= datetime.now(UTC)
        assert auth.device_group_key == "new-grp"
        assert auth.device_key == "new-dev"
        assert "Cognito issued new device keys" in caplog.text
        auth.client.respond_to_auth_challenge.assert_not_called()

    async def test_a_result_without_device_metadata_keeps_the_saved_keys(self, caplog):
        auth = make_auth(**DEVICE_CREDENTIALS)
        auth.client.initiate_auth.return_value = AUTH_RESULT
        with caplog.at_level(logging.DEBUG, logger="apyhiveapi.auth"):
            await auth.login()
        assert auth.access_token == "access-token"
        assert auth.device_group_key == "grp-key"
        assert auth.device_key == "dev-key"
        assert "new device keys" not in caplog.text

    async def test_answers_the_password_verifier_challenge(self):
        auth = make_auth()
        auth.client.initiate_auth.return_value = PASSWORD_CHALLENGE
        auth.client.respond_to_auth_challenge.return_value = AUTH_RESULT
        result = await auth.login()
        assert result is AUTH_RESULT
        assert auth.user_id == "uuid-user"
        assert auth.access_token == "access-token"
        auth.client.respond_to_auth_challenge.assert_called_once()
        kwargs = auth.client.respond_to_auth_challenge.call_args.kwargs
        assert kwargs["ClientId"] == CLIENT_ID
        assert kwargs["ChallengeName"] == "PASSWORD_VERIFIER"
        answer = kwargs["ChallengeResponses"]
        assert set(answer) == {
            "TIMESTAMP",
            "USERNAME",
            "PASSWORD_CLAIM_SECRET_BLOCK",
            "PASSWORD_CLAIM_SIGNATURE",
        }
        assert answer["USERNAME"] == "uuid-user"
        assert answer["PASSWORD_CLAIM_SECRET_BLOCK"] == "YWJj"
        assert TIMESTAMP_RE.fullmatch(answer["TIMESTAMP"])
        # Signed with the key for the pool name plus Cognito's user ID and the
        # account password, over identity + secret block + timestamp.
        key = auth._srp_key("pooluuid-user", PASSWORD, "ccddee", "aabbccdd")
        assert answer["PASSWORD_CLAIM_SIGNATURE"] == expected_signature(
            key, "pooluuid-user", "YWJj", answer["TIMESTAMP"]
        )

    async def test_the_verifier_answer_names_the_saved_device(self):
        auth = make_auth(device_key="dev-key")
        auth.client.initiate_auth.return_value = PASSWORD_CHALLENGE
        auth.client.respond_to_auth_challenge.return_value = AUTH_RESULT
        await auth.login()
        handshake = auth.client.initiate_auth.call_args.kwargs["AuthParameters"]
        assert "DEVICE_KEY" not in handshake
        answer = auth.client.respond_to_auth_challenge.call_args.kwargs
        assert answer["ChallengeResponses"]["DEVICE_KEY"] == "dev-key"

    async def test_the_verifier_can_answer_with_another_challenge(self):
        auth = make_auth()
        auth.client.initiate_auth.return_value = PASSWORD_CHALLENGE
        auth.client.respond_to_auth_challenge.return_value = SMS_CHALLENGE
        result = await auth.login()
        assert result is SMS_CHALLENGE
        assert auth.access_token is None
        assert auth.token_created is None

    @pytest.mark.parametrize(
        ("response", "name"),
        [
            (
                {"ChallengeName": "NEW_PASSWORD_REQUIRED", "ChallengeParameters": {}},
                "NEW_PASSWORD_REQUIRED",
            ),
            ({"ChallengeName": "CUSTOM_CHALLENGE"}, "CUSTOM_CHALLENGE"),
            ({}, "None"),
        ],
    )
    async def test_other_challenges_are_refused(self, response, name):
        auth = make_auth()
        auth.client.initiate_auth.return_value = response
        with pytest.raises(
            HiveApiError, match=f"Unsupported Cognito challenge: {name}"
        ):
            await auth.login()
        auth.client.respond_to_auth_challenge.assert_not_called()
        assert auth.access_token is None

    @pytest.mark.parametrize(
        ("code", "exc_type"),
        [
            ("UserNotFoundException", HiveInvalidUsername),
            ("NotAuthorizedException", HiveApiError),
            ("InternalErrorException", HiveConnectionError),
        ],
    )
    async def test_handshake_refusals(self, code, exc_type):
        auth = make_auth()
        err = client_error(code)
        auth.client.initiate_auth.side_effect = err
        with pytest.raises(exc_type) as exc_info:
            await auth.login()
        assert exc_info.type is exc_type
        assert exc_info.value.__cause__ is err
        auth.client.respond_to_auth_challenge.assert_not_called()

    @pytest.mark.parametrize(
        ("code", "exc_type"),
        [
            ("UserNotFoundException", HiveInvalidUsername),
            ("NotAuthorizedException", HiveInvalidPassword),
            ("ResourceNotFoundException", HiveReauthRequired),
            ("InvalidParameterException", HiveApiError),
            ("TooManyRequestsException", HiveConnectionError),
        ],
    )
    async def test_verifier_refusals(self, code, exc_type):
        auth = make_auth()
        auth.client.initiate_auth.return_value = PASSWORD_CHALLENGE
        err = client_error(code, operation="RespondToAuthChallenge")
        auth.client.respond_to_auth_challenge.side_effect = err
        with pytest.raises(exc_type) as exc_info:
            await auth.login()
        assert exc_info.type is exc_type
        assert exc_info.value.__cause__ is err
        assert auth.access_token is None

    async def test_a_network_error_is_a_connection_error(self):
        auth = make_auth()
        err = botocore.exceptions.EndpointConnectionError(endpoint_url="x")
        auth.client.initiate_auth.side_effect = err
        with pytest.raises(HiveConnectionError) as exc_info:
            await auth.login()
        assert exc_info.value.__cause__ is err


# Tests for device_login


class TestDeviceLogin:
    @pytest.mark.parametrize(
        "missing", ["device_group_key", "device_key", "device_password"]
    )
    async def test_missing_credentials_need_a_fresh_login(self, missing):
        auth = make_auth(**DEVICE_CREDENTIALS)
        setattr(auth, missing, None)
        with pytest.raises(HiveReauthRequired, match="no saved device credentials"):
            await auth.device_login()
        auth.client.respond_to_auth_challenge.assert_not_called()

    async def test_credentials_are_checked_before_the_client_is_created(self):
        auth = HiveAuth(USERNAME, PASSWORD)
        with (
            patch.object(auth, "async_init", new=AsyncMock()) as async_init,
            pytest.raises(HiveReauthRequired),
        ):
            await auth.device_login()
        async_init.assert_not_awaited()

    async def test_completes_the_device_srp_handshake(self):
        auth = make_auth(**DEVICE_CREDENTIALS)
        auth.client.respond_to_auth_challenge.side_effect = [
            DEVICE_CHALLENGE,
            AUTH_RESULT,
        ]
        result = await auth.device_login()
        assert result is AUTH_RESULT
        assert auth.access_token == "access-token"
        assert auth.token_created is not None
        first, second = auth.client.respond_to_auth_challenge.call_args_list
        assert first == call(
            ClientId=CLIENT_ID,
            ChallengeName="DEVICE_SRP_AUTH",
            ChallengeResponses={
                "USERNAME": USERNAME,
                "SRP_A": long_to_hex(auth.large_a_value),
                "DEVICE_KEY": "dev-key",
            },
        )
        assert second.kwargs["ClientId"] == CLIENT_ID
        assert second.kwargs["ChallengeName"] == "DEVICE_PASSWORD_VERIFIER"
        answer = second.kwargs["ChallengeResponses"]
        assert set(answer) == {
            "TIMESTAMP",
            "USERNAME",
            "PASSWORD_CLAIM_SECRET_BLOCK",
            "PASSWORD_CLAIM_SIGNATURE",
            "DEVICE_KEY",
        }
        # The username is the one Cognito sent, not the account's email address.
        assert answer["USERNAME"] == "uuid-user"
        assert answer["DEVICE_KEY"] == "dev-key"
        assert answer["PASSWORD_CLAIM_SECRET_BLOCK"] == "dGVzdA=="
        assert TIMESTAMP_RE.fullmatch(answer["TIMESTAMP"])
        # Signed with the key for group key plus device key and the device password.
        key = auth._srp_key("grp-keydev-key", "dev-pass", "55667788", "11223344")
        assert answer["PASSWORD_CLAIM_SIGNATURE"] == expected_signature(
            key, "grp-keydev-key", "dGVzdA==", answer["TIMESTAMP"]
        )

    async def test_uses_a_fresh_srp_ephemeral(self):
        auth = make_auth(**DEVICE_CREDENTIALS)
        auth.client.respond_to_auth_challenge.side_effect = [
            DEVICE_CHALLENGE,
            AUTH_RESULT,
        ]
        initial_a = auth.large_a_value
        await auth.device_login()
        assert auth.large_a_value != initial_a
        first = auth.client.respond_to_auth_challenge.call_args_list[0]
        srp_a = first.kwargs["ChallengeResponses"]["SRP_A"]
        assert srp_a == long_to_hex(auth.large_a_value)

    async def test_stores_new_device_keys(self):
        auth = make_auth(**DEVICE_CREDENTIALS)
        auth.client.respond_to_auth_challenge.side_effect = [
            DEVICE_CHALLENGE,
            NEW_DEVICE_RESULT,
        ]
        await auth.device_login()
        assert auth.device_group_key == "new-grp"
        assert auth.device_key == "new-dev"
        assert auth.device_password == "dev-pass"

    async def test_a_result_without_tokens_is_returned_as_is(self):
        auth = make_auth(**DEVICE_CREDENTIALS)
        auth.client.respond_to_auth_challenge.side_effect = [
            DEVICE_CHALLENGE,
            SMS_CHALLENGE,
        ]
        result = await auth.device_login()
        assert result is SMS_CHALLENGE
        assert auth.access_token is None

    @pytest.mark.parametrize(
        "code", ["NotAuthorizedException", "ResourceNotFoundException"]
    )
    async def test_a_rejected_device_on_the_handshake(self, code):
        auth = make_auth(**DEVICE_CREDENTIALS)
        err = client_error(code, operation="RespondToAuthChallenge")
        auth.client.respond_to_auth_challenge.side_effect = err
        with pytest.raises(HiveReauthRequired) as exc_info:
            await auth.device_login()
        assert exc_info.type is HiveReauthRequired
        assert exc_info.value.__cause__ is err
        assert auth.client.respond_to_auth_challenge.call_count == 1

    @pytest.mark.parametrize(
        "code", ["NotAuthorizedException", "ResourceNotFoundException"]
    )
    async def test_a_rejected_device_on_the_verifier(self, code):
        auth = make_auth(**DEVICE_CREDENTIALS)
        err = client_error(code, operation="RespondToAuthChallenge")
        auth.client.respond_to_auth_challenge.side_effect = [DEVICE_CHALLENGE, err]
        with pytest.raises(HiveReauthRequired) as exc_info:
            await auth.device_login()
        assert exc_info.type is HiveReauthRequired
        assert exc_info.value.__cause__ is err
        assert auth.client.respond_to_auth_challenge.call_count == 2
        assert auth.access_token is None

    @pytest.mark.parametrize(
        ("code", "exc_type"),
        [
            ("InvalidParameterException", HiveApiError),
            ("TooManyRequestsException", HiveConnectionError),
        ],
    )
    async def test_other_refusals(self, code, exc_type):
        auth = make_auth(**DEVICE_CREDENTIALS)
        err = client_error(code, operation="RespondToAuthChallenge")
        auth.client.respond_to_auth_challenge.side_effect = err
        with pytest.raises(exc_type) as exc_info:
            await auth.device_login()
        assert exc_info.type is exc_type
        assert exc_info.value.__cause__ is err


# Tests for sms_2fa


class TestSms2fa:
    @pytest.mark.parametrize("code", ["123456", 123456])
    async def test_answers_the_sms_challenge(self, code):
        auth = make_auth()
        auth.user_id = "uuid-user"
        auth.client.respond_to_auth_challenge.return_value = NEW_DEVICE_RESULT
        result = await auth.sms_2fa(code, SMS_CHALLENGE)
        assert result is NEW_DEVICE_RESULT
        auth.client.respond_to_auth_challenge.assert_called_once_with(
            ClientId=CLIENT_ID,
            ChallengeName="SMS_MFA",
            Session="session-1",
            ChallengeResponses={"SMS_MFA_CODE": "123456", "USERNAME": "uuid-user"},
        )
        assert auth.access_token == "access-token"
        assert auth.token_created is not None
        assert auth.device_group_key == "new-grp"
        assert auth.device_key == "new-dev"

    async def test_a_result_without_tokens_is_returned_as_is(self):
        auth = make_auth()
        retry = {"ChallengeName": "SMS_MFA", "Session": "session-2"}
        auth.client.respond_to_auth_challenge.return_value = retry
        result = await auth.sms_2fa("000000", SMS_CHALLENGE)
        assert result is retry
        assert auth.access_token is None
        assert auth.token_created is None

    @pytest.mark.parametrize(
        "challenge", [{}, {"ChallengeName": "SMS_MFA"}, {"Session": ""}]
    )
    async def test_a_challenge_without_a_session_is_rejected(self, challenge):
        auth = make_auth()
        with pytest.raises(ValueError, match="no Session"):
            await auth.sms_2fa("123456", challenge)
        auth.client.respond_to_auth_challenge.assert_not_called()

    @pytest.mark.parametrize(
        "code",
        ["CodeMismatchException", "ExpiredCodeException", "NotAuthorizedException"],
    )
    async def test_a_rejected_code(self, code):
        auth = make_auth()
        err = client_error(code, operation="RespondToAuthChallenge")
        auth.client.respond_to_auth_challenge.side_effect = err
        with pytest.raises(HiveInvalid2FACode) as exc_info:
            await auth.sms_2fa("000000", SMS_CHALLENGE)
        assert exc_info.value.__cause__ is err
        assert auth.access_token is None

    @pytest.mark.parametrize(
        ("code", "exc_type"),
        [
            ("LimitExceededException", HiveApiError),
            ("TooManyRequestsException", HiveConnectionError),
        ],
    )
    async def test_other_refusals(self, code, exc_type):
        auth = make_auth()
        err = client_error(code, operation="RespondToAuthChallenge")
        auth.client.respond_to_auth_challenge.side_effect = err
        with pytest.raises(exc_type) as exc_info:
            await auth.sms_2fa("123456", SMS_CHALLENGE)
        assert exc_info.type is exc_type
        assert exc_info.value.__cause__ is err


# Tests for refresh_token


class TestRefreshToken:
    @pytest.mark.parametrize("token", [None, ""])
    async def test_without_a_refresh_token_the_user_must_log_in_again(self, token):
        auth = HiveAuth(USERNAME, PASSWORD)
        with (
            patch.object(auth, "async_init", new=AsyncMock()) as async_init,
            pytest.raises(HiveReauthRequired, match="There is no refresh token"),
        ):
            await auth.refresh_token(token)
        async_init.assert_not_awaited()
        assert auth.client is None

    async def test_exchanges_the_refresh_token(self):
        auth = make_auth()
        auth.client.initiate_auth.return_value = AUTH_RESULT
        result = await auth.refresh_token("refresh-token")
        assert result is AUTH_RESULT
        auth.client.initiate_auth.assert_called_once_with(
            ClientId=CLIENT_ID,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": "refresh-token"},
        )
        assert auth.access_token == "access-token"
        assert auth.token_created is not None

    async def test_names_the_saved_device(self):
        auth = make_auth(device_key="dev-key")
        auth.client.initiate_auth.return_value = AUTH_RESULT
        await auth.refresh_token("refresh-token")
        auth.client.initiate_auth.assert_called_once_with(
            ClientId=CLIENT_ID,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": "refresh-token", "DEVICE_KEY": "dev-key"},
        )

    async def test_a_result_without_tokens_is_returned_as_is(self):
        auth = make_auth()
        auth.client.initiate_auth.return_value = {"ChallengeName": "SMS_MFA"}
        result = await auth.refresh_token("refresh-token")
        assert result == {"ChallengeName": "SMS_MFA"}
        assert auth.access_token is None

    @pytest.mark.parametrize(
        ("code", "exc_type"),
        [
            ("NotAuthorizedException", HiveReauthRequired),
            ("UserNotFoundException", HiveInvalidUsername),
            ("InvalidParameterException", HiveApiError),
            ("TooManyRequestsException", HiveConnectionError),
        ],
    )
    async def test_refusals(self, code, exc_type):
        auth = make_auth()
        err = client_error(code, "Invalid Refresh Token")
        auth.client.initiate_auth.side_effect = err
        with pytest.raises(exc_type) as exc_info:
            await auth.refresh_token("refresh-token")
        assert exc_info.type is exc_type
        assert exc_info.value.__cause__ is err
        assert auth.access_token is None


# Tests for the trusted-device methods


class TestDeviceVerifier:
    def test_returns_a_verifier_for_a_new_device_password(self):
        auth = HiveAuth(USERNAME, PASSWORD, **DEVICE_CREDENTIALS)
        config, password = auth._device_verifier("grp-key", "dev-key")
        assert set(config) == {"PasswordVerifier", "Salt"}
        assert password != "dev-pass"
        assert len(base64.standard_b64decode(password)) == 40
        # Cognito will check the verifier against the salt and this password:
        # it must be g to the power of the hash of the salt and of
        # "<group key><device key>:<password>".
        salt = base64.standard_b64decode(config["Salt"]).hex()
        x_value = hex_to_long(
            hex_hash(salt + hash_sha256(f"grp-keydev-key:{password}".encode()))
        )
        verifier = pad_hex(pow(auth.g_value, x_value, auth.big_n))
        decoded = base64.standard_b64decode(config["PasswordVerifier"])
        assert decoded == bytes.fromhex(verifier)

    def test_does_not_touch_the_saved_credentials(self):
        auth = HiveAuth(USERNAME, PASSWORD, **DEVICE_CREDENTIALS)
        auth._device_verifier("grp-key", "dev-key")
        assert auth.device_password == "dev-pass"

    def test_every_call_makes_a_new_password(self):
        auth = HiveAuth(USERNAME, PASSWORD)
        first, first_password = auth._device_verifier("grp-key", "dev-key")
        second, second_password = auth._device_verifier("grp-key", "dev-key")
        assert first_password != second_password
        assert first["PasswordVerifier"] != second["PasswordVerifier"]
        assert first["Salt"] != second["Salt"]


class TestConfirmDevice:
    async def test_confirms_the_device_as_the_hostname_by_default(self):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        auth.client.confirm_device.return_value = {"UserConfirmationNecessary": False}
        with patch("apyhiveapi.auth.socket.gethostname", return_value="my-host"):
            result = await auth.confirm_device()
        assert result == {"UserConfirmationNecessary": False}
        auth.client.confirm_device.assert_called_once()
        kwargs = auth.client.confirm_device.call_args.kwargs
        assert set(kwargs) == {
            "AccessToken",
            "DeviceKey",
            "DeviceName",
            "DeviceSecretVerifierConfig",
        }
        assert kwargs["AccessToken"] == "access-token"
        assert kwargs["DeviceKey"] == "dev-key"
        assert kwargs["DeviceName"] == "my-host"

    async def test_confirms_the_device_under_the_given_name(self):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        with patch("apyhiveapi.auth.socket.gethostname") as gethostname:
            await auth.confirm_device("Home Assistant")
        gethostname.assert_not_called()
        kwargs = auth.client.confirm_device.call_args.kwargs
        assert kwargs["DeviceName"] == "Home Assistant"

    async def test_sends_a_verifier_for_a_new_device_password(self):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        await auth.confirm_device("Home Assistant")
        config = auth.client.confirm_device.call_args.kwargs[
            "DeviceSecretVerifierConfig"
        ]
        assert set(config) == {"PasswordVerifier", "Salt"}
        password = auth.device_password
        assert password != "dev-pass"
        salt = base64.standard_b64decode(config["Salt"]).hex()
        x_value = hex_to_long(
            hex_hash(salt + hash_sha256(f"grp-keydev-key:{password}".encode()))
        )
        verifier = pad_hex(pow(auth.g_value, x_value, auth.big_n))
        decoded = base64.standard_b64decode(config["PasswordVerifier"])
        assert decoded == bytes.fromhex(verifier)

    async def test_refusals_are_api_errors(self):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        err = client_error("NotAuthorizedException", operation="ConfirmDevice")
        auth.client.confirm_device.side_effect = err
        with pytest.raises(HiveApiError) as exc_info:
            await auth.confirm_device("Home Assistant")
        assert exc_info.type is HiveApiError
        assert exc_info.value.__cause__ is err

    async def test_a_refusal_keeps_the_saved_device_password(self):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        auth.client.confirm_device.side_effect = client_error(
            "NotAuthorizedException", operation="ConfirmDevice"
        )
        with pytest.raises(HiveApiError):
            await auth.confirm_device("Home Assistant")
        assert auth.device_password == "dev-pass"

    @pytest.mark.parametrize(
        "missing", [{"device_group_key": None}, {"device_key": ""}]
    )
    async def test_without_device_keys_raises_before_any_call(self, missing):
        auth = make_auth(**{**DEVICE_CREDENTIALS, **missing}, access_token="tok")
        with pytest.raises(HiveApiError, match="no device keys"):
            await auth.confirm_device("Home Assistant")
        auth.client.confirm_device.assert_not_called()
        assert auth.device_password == "dev-pass"


class TestUpdateDeviceStatus:
    async def test_marks_the_device_remembered(self):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        auth.client.update_device_status.return_value = {"ResponseMetadata": {}}
        result = await auth.update_device_status()
        assert result == {"ResponseMetadata": {}}
        auth.client.update_device_status.assert_called_once_with(
            AccessToken="access-token",
            DeviceKey="dev-key",
            DeviceRememberedStatus="remembered",
        )

    async def test_refusals_are_api_errors(self):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        err = client_error("ResourceNotFoundException", operation="UpdateDeviceStatus")
        auth.client.update_device_status.side_effect = err
        with pytest.raises(HiveApiError) as exc_info:
            await auth.update_device_status()
        assert exc_info.type is HiveApiError
        assert exc_info.value.__cause__ is err


class TestDeviceRegistration:
    async def test_confirms_then_remembers_the_device(self):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        with patch("apyhiveapi.auth.socket.gethostname", return_value="my-host"):
            assert await auth.device_registration() is None
        names = [name for name, _args, _kwargs in auth.client.method_calls]
        assert names == ["confirm_device", "update_device_status"]
        assert auth.client.confirm_device.call_args.kwargs["DeviceName"] == "my-host"
        auth.client.update_device_status.assert_called_once_with(
            AccessToken="access-token",
            DeviceKey="dev-key",
            DeviceRememberedStatus="remembered",
        )

    async def test_passes_the_device_name_on(self):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        await auth.device_registration("Home Assistant")
        kwargs = auth.client.confirm_device.call_args.kwargs
        assert kwargs["DeviceName"] == "Home Assistant"

    async def test_stops_when_the_confirmation_fails(self):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        err = client_error("NotAuthorizedException", operation="ConfirmDevice")
        auth.client.confirm_device.side_effect = err
        with pytest.raises(HiveApiError):
            await auth.device_registration("Home Assistant")
        auth.client.update_device_status.assert_not_called()


class TestGetDeviceData:
    async def test_returns_the_credentials_to_save(self):
        auth = HiveAuth(USERNAME, PASSWORD, **DEVICE_CREDENTIALS)
        auth.token_created = FROZEN_NOW
        result = auth.get_device_data()
        assert result == ("grp-key", "dev-key", "dev-pass", FROZEN_NOW)

    async def test_before_any_login(self):
        auth = HiveAuth(USERNAME, PASSWORD)
        assert auth.get_device_data() == (None, None, None, None)

    async def test_reflects_the_keys_issued_at_login(self):
        auth = make_auth()
        auth.client.initiate_auth.return_value = NEW_DEVICE_RESULT
        await auth.login()
        (
            group_key,
            device_key,
            device_password,
            token_created,
        ) = auth.get_device_data()
        assert (group_key, device_key, device_password) == ("new-grp", "new-dev", None)
        assert token_created is auth.token_created

    async def test_reflects_the_password_made_at_registration(self):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        await auth.device_registration("Home Assistant")
        group_key, device_key, device_password, _created = auth.get_device_data()
        assert (group_key, device_key) == ("grp-key", "dev-key")
        assert device_password == auth.device_password
        assert device_password != "dev-pass"


class TestIsDeviceRegistered:
    @pytest.mark.parametrize(
        ("access_token", "device_key"),
        [(None, "dev-key"), ("", "dev-key"), ("access-token", None), (None, None)],
    )
    async def test_false_without_a_token_or_key(self, access_token, device_key):
        auth = make_auth(device_key=device_key, access_token=access_token)
        assert await auth.is_device_registered() is False
        auth.client.get_device.assert_not_called()

    async def test_does_not_create_the_client_without_a_token_or_key(self):
        auth = HiveAuth(USERNAME, PASSWORD)
        with patch.object(auth, "async_init", new=AsyncMock()) as async_init:
            assert await auth.is_device_registered() is False
        async_init.assert_not_awaited()

    async def test_true_when_cognito_remembers_the_device(self):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        auth.client.get_device.return_value = REMEMBERED_DEVICE
        assert await auth.is_device_registered() is True
        auth.client.get_device.assert_called_once_with(
            AccessToken="access-token", DeviceKey="dev-key"
        )

    @pytest.mark.parametrize(
        "result",
        [
            {
                "Device": {
                    "DeviceAttributes": [
                        {
                            "Name": "dev:device_remembered_status",
                            "Value": "not_remembered",
                        }
                    ]
                }
            },
            {"Device": {"DeviceAttributes": [{"Name": "device_name", "Value": "x"}]}},
            {"Device": {"DeviceAttributes": [{"Value": "remembered"}]}},
            {"Device": {"DeviceAttributes": []}},
            {"Device": {}},
            {},
        ],
    )
    async def test_false_unless_the_device_is_remembered(self, result):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        auth.client.get_device.return_value = result
        assert await auth.is_device_registered() is False

    @pytest.mark.parametrize(
        "code",
        [
            "ResourceNotFoundException",
            "NotAuthorizedException",
            "InvalidParameterException",
        ],
    )
    async def test_false_with_a_debug_log_when_cognito_refuses(self, code, caplog):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        auth.client.get_device.side_effect = client_error(
            code, "Device does not exist.", "GetDevice"
        )
        with caplog.at_level(logging.DEBUG, logger="apyhiveapi.auth"):
            assert await auth.is_device_registered() is False
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.DEBUG
        assert caplog.text.count("Device dev-key is not registered") == 1
        assert f"Cognito get_device failed with {code}: Device does not exist." in (
            caplog.text
        )

    @pytest.mark.parametrize(
        "err",
        [
            botocore.exceptions.EndpointConnectionError(endpoint_url="x"),
            client_error("TooManyRequestsException", operation="GetDevice"),
        ],
    )
    async def test_connection_errors_propagate(self, err):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        auth.client.get_device.side_effect = err
        with pytest.raises(HiveConnectionError) as exc_info:
            await auth.is_device_registered()
        assert exc_info.value.__cause__ is err

    async def test_explicit_arguments_override_the_saved_ones(self):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        auth.client.get_device.return_value = REMEMBERED_DEVICE
        assert await auth.is_device_registered("other-token", "other-key") is True
        auth.client.get_device.assert_called_once_with(
            AccessToken="other-token", DeviceKey="other-key"
        )

    async def test_explicit_arguments_work_without_saved_ones(self):
        auth = make_auth()
        auth.client.get_device.return_value = REMEMBERED_DEVICE
        result = await auth.is_device_registered(
            access_token="other-token", device_key="other-key"
        )
        assert result is True
        auth.client.get_device.assert_called_once_with(
            AccessToken="other-token", DeviceKey="other-key"
        )


class TestForgetDevice:
    async def test_forgets_the_given_device(self):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        auth.client.forget_device.return_value = {}
        result = await auth.forget_device("other-token", "other-key")
        assert result == {}
        auth.client.forget_device.assert_called_once_with(
            AccessToken="other-token", DeviceKey="other-key"
        )

    async def test_refusals_are_api_errors(self):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        err = client_error("ResourceNotFoundException", operation="ForgetDevice")
        auth.client.forget_device.side_effect = err
        with pytest.raises(HiveApiError) as exc_info:
            await auth.forget_device("access-token", "dev-key")
        assert exc_info.type is HiveApiError
        assert exc_info.value.__cause__ is err


# Tests for the SRP helpers


class TestToInt:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (42, 42),
            (0, 0),
            (b"\xff", 255),
            (b"\x00", 0),
            (b"\xcc\xdd\xee", 0xCCDDEE),
            ("ff", 255),
            ("CCDDEE", 0xCCDDEE),
            ("0", 0),
        ],
    )
    def test_accepts_an_int_bytes_or_a_hex_string(self, value, expected):
        assert HiveAuth._to_int(value) == expected


class TestCognitoTimestamp:
    def test_has_the_format_cognito_expects(self):
        assert TIMESTAMP_RE.fullmatch(HiveAuth._cognito_timestamp())

    @pytest.mark.parametrize(
        ("now", "expected"),
        [
            (FROZEN_NOW, FROZEN_TIMESTAMP),
            (
                datetime(2024, 1, 15, 3, 4, 5, tzinfo=UTC),
                "Mon Jan 15 03:04:05 UTC 2024",
            ),
            (
                datetime(2026, 12, 31, 23, 59, 0, tzinfo=UTC),
                "Thu Dec 31 23:59:00 UTC 2026",
            ),
        ],
    )
    def test_strips_the_leading_zero_from_the_day(self, now, expected):
        with patch("apyhiveapi.auth.datetime") as mock_datetime:
            mock_datetime.now.return_value = now
            assert HiveAuth._cognito_timestamp() == expected
        mock_datetime.now.assert_called_once_with(UTC)


class TestPoolName:
    def test_is_the_pool_id_without_its_region(self):
        auth = make_auth()
        assert auth._pool_name() == "pool"

    @pytest.mark.parametrize("pool_id", [None, "", "nounderscore"])
    def test_rejects_an_unexpected_pool_id(self, pool_id):
        auth = HiveAuth(USERNAME, PASSWORD)
        auth._pool_id = pool_id
        with pytest.raises(HiveApiError, match="Unexpected Cognito pool ID"):
            auth._pool_name()


class TestSrpEphemeral:
    def test_small_a_is_random_and_below_n(self):
        auth = HiveAuth(USERNAME, PASSWORD)
        values = {auth.generate_random_small_a() for _ in range(5)}
        assert len(values) == 5
        assert all(0 <= value < auth.big_n for value in values)

    def test_calculate_a_is_g_to_the_power_of_a(self):
        auth = HiveAuth(USERNAME, PASSWORD)
        auth.small_a_value = 12345
        assert auth.calculate_a() == pow(2, 12345, auth.big_n)

    @pytest.mark.parametrize("multiple", [0, 1, 2])
    def test_calculate_a_rejects_a_multiple_of_n(self, multiple):
        auth = HiveAuth(USERNAME, PASSWORD)
        with (
            patch(
                "apyhiveapi.auth.pow", create=True, return_value=multiple * auth.big_n
            ),
            pytest.raises(ValueError, match="Safety check for A failed"),
        ):
            auth.calculate_a()

    def test_new_srp_ephemeral_replaces_both_values(self):
        auth = HiveAuth(USERNAME, PASSWORD)
        initial = (auth.small_a_value, auth.large_a_value)
        auth._new_srp_ephemeral()
        assert auth.small_a_value != initial[0]
        assert auth.large_a_value != initial[1]
        assert auth.large_a_value == pow(auth.g_value, auth.small_a_value, auth.big_n)

    def test_auth_params(self):
        auth = HiveAuth(USERNAME, PASSWORD)
        srp_a = long_to_hex(auth.large_a_value)
        assert auth._auth_params() == {"USERNAME": USERNAME, "SRP_A": srp_a}
        assert auth._auth_params("dev-key") == {
            "USERNAME": USERNAME,
            "SRP_A": srp_a,
            "DEVICE_KEY": "dev-key",
        }


class TestSrpKey:
    def test_agrees_with_cognito_s_side_of_the_handshake(self):
        auth = HiveAuth(USERNAME, PASSWORD)
        srp_b, server_key = cognito_server_key(
            auth, "pooluuid-user", PASSWORD, "aabbccdd", server_secret=0x1234_5678_9ABC
        )
        key = auth._srp_key("pooluuid-user", PASSWORD, long_to_hex(srp_b), "aabbccdd")
        assert key == server_key

    def test_agrees_for_a_device_login(self):
        auth = HiveAuth(USERNAME, PASSWORD, **DEVICE_CREDENTIALS)
        srp_b, server_key = cognito_server_key(
            auth, "grp-keydev-key", "dev-pass", "11223344", server_secret=987654321
        )
        key = auth._srp_key(
            "grp-keydev-key", "dev-pass", long_to_hex(srp_b), "11223344"
        )
        assert key == server_key

    def test_is_16_bytes_and_deterministic(self):
        auth = HiveAuth(USERNAME, PASSWORD)
        key = auth._srp_key("pooluuid-user", PASSWORD, "ccddee", "aabbccdd")
        assert isinstance(key, bytes)
        assert len(key) == 16
        assert auth._srp_key("pooluuid-user", PASSWORD, "ccddee", "aabbccdd") == key

    @pytest.mark.parametrize(
        ("identity", "secret", "srp_b", "salt"),
        [
            ("pooluuid-other", PASSWORD, "ccddee", "aabbccdd"),
            ("pooluuid-user", "other", "ccddee", "aabbccdd"),
            ("pooluuid-user", PASSWORD, "ccddef", "aabbccdd"),
            ("pooluuid-user", PASSWORD, "ccddee", "aabbccde"),
        ],
    )
    def test_changes_with_every_input(self, identity, secret, srp_b, salt):
        auth = HiveAuth(USERNAME, PASSWORD)
        key = auth._srp_key("pooluuid-user", PASSWORD, "ccddee", "aabbccdd")
        assert auth._srp_key(identity, secret, srp_b, salt) != key

    def test_changes_with_the_ephemeral(self):
        auth = HiveAuth(USERNAME, PASSWORD)
        key = auth._srp_key("pooluuid-user", PASSWORD, "ccddee", "aabbccdd")
        auth._new_srp_ephemeral()
        assert auth._srp_key("pooluuid-user", PASSWORD, "ccddee", "aabbccdd") != key

    @pytest.mark.parametrize("srp_b", ["ccddee", b"\xcc\xdd\xee", 0xCCDDEE])
    def test_accepts_srp_b_as_hex_bytes_or_int(self, srp_b):
        auth = HiveAuth(USERNAME, PASSWORD)
        key = auth._srp_key("pooluuid-user", PASSWORD, "ccddee", "aabbccdd")
        assert auth._srp_key("pooluuid-user", PASSWORD, srp_b, "aabbccdd") == key

    def test_rejects_a_zero_u_value(self):
        auth = HiveAuth(USERNAME, PASSWORD)
        with (
            patch("apyhiveapi.auth.calculate_u", return_value=0),
            pytest.raises(ValueError, match="U cannot be zero"),
        ):
            auth._srp_key("pooluuid-user", PASSWORD, "ccddee", "aabbccdd")

    def test_known_answer(self):
        # Regression vector pinned from the implementation, whose maths the
        # server-side tests above verify independently.
        auth = HiveAuth(USERNAME, PASSWORD)
        auth.small_a_value = 12345
        auth.large_a_value = auth.calculate_a()
        key = auth._srp_key("pooluser", "pass", "ccddee", "aabbccdd")
        assert key.hex() == "f61d3eda114bbcdcd35c229bad30c781"


class TestAnswerSrpChallenge:
    async def test_signs_the_challenge(self):
        auth = HiveAuth(USERNAME, PASSWORD)
        with patch("apyhiveapi.auth.datetime") as mock_datetime:
            mock_datetime.now.return_value = FROZEN_NOW
            answer = await auth._answer_srp_challenge(
                PASSWORD_CHALLENGE["ChallengeParameters"],
                identity="pooluuid-user",
                secret=PASSWORD,
                username="uuid-user",
            )
        key = auth._srp_key("pooluuid-user", PASSWORD, "ccddee", "aabbccdd")
        assert answer == {
            "TIMESTAMP": FROZEN_TIMESTAMP,
            "USERNAME": "uuid-user",
            "PASSWORD_CLAIM_SECRET_BLOCK": "YWJj",
            "PASSWORD_CLAIM_SIGNATURE": expected_signature(
                key, "pooluuid-user", "YWJj", FROZEN_TIMESTAMP
            ),
        }

    async def test_names_the_saved_device(self):
        auth = HiveAuth(USERNAME, PASSWORD, device_key="dev-key")
        answer = await auth._answer_srp_challenge(
            PASSWORD_CHALLENGE["ChallengeParameters"],
            identity="pooluuid-user",
            secret=PASSWORD,
            username="uuid-user",
        )
        assert answer["DEVICE_KEY"] == "dev-key"

    async def test_an_integer_salt_is_padded_to_hex(self):
        auth = HiveAuth(USERNAME, PASSWORD)
        params = dict(PASSWORD_CHALLENGE["ChallengeParameters"])
        params["SALT"] = 0xAABBCCDD
        with patch("apyhiveapi.auth.datetime") as mock_datetime:
            mock_datetime.now.return_value = FROZEN_NOW
            from_int = await auth._answer_srp_challenge(
                params, identity="pooluuid-user", secret=PASSWORD, username="uuid-user"
            )
            from_str = await auth._answer_srp_challenge(
                PASSWORD_CHALLENGE["ChallengeParameters"],
                identity="pooluuid-user",
                secret=PASSWORD,
                username="uuid-user",
            )
        assert from_int == from_str

    async def test_derives_the_key_off_the_event_loop_thread(self):
        auth = HiveAuth(USERNAME, PASSWORD)
        threads = []

        def fake_key(*_args):
            threads.append(threading.get_ident())
            return b"\x00" * 16

        auth._srp_key = MagicMock(side_effect=fake_key)
        answer = await auth._answer_srp_challenge(
            PASSWORD_CHALLENGE["ChallengeParameters"],
            identity="pooluuid-user",
            secret=PASSWORD,
            username="uuid-user",
        )
        auth._srp_key.assert_called_once_with(
            "pooluuid-user", PASSWORD, "ccddee", "aabbccdd"
        )
        assert len(threads) == 1
        assert threads[0] != threading.get_ident()
        assert answer["PASSWORD_CLAIM_SIGNATURE"] == expected_signature(
            b"\x00" * 16, "pooluuid-user", "YWJj", answer["TIMESTAMP"]
        )


class TestAnswerPasswordChallenge:
    async def test_learns_the_user_id_and_signs_as_the_pool_user(self):
        auth = make_auth()
        with patch("apyhiveapi.auth.datetime") as mock_datetime:
            mock_datetime.now.return_value = FROZEN_NOW
            answer = await auth._answer_password_challenge(
                PASSWORD_CHALLENGE["ChallengeParameters"]
            )
        assert auth.user_id == "uuid-user"
        assert answer["USERNAME"] == "uuid-user"
        key = auth._srp_key("pooluuid-user", PASSWORD, "ccddee", "aabbccdd")
        assert answer["PASSWORD_CLAIM_SIGNATURE"] == expected_signature(
            key, "pooluuid-user", "YWJj", FROZEN_TIMESTAMP
        )

    async def test_needs_the_pool_id(self):
        auth = make_auth()
        auth._pool_id = None
        with pytest.raises(HiveApiError, match="Unexpected Cognito pool ID"):
            await auth._answer_password_challenge(
                PASSWORD_CHALLENGE["ChallengeParameters"]
            )


class TestAnswerDeviceChallenge:
    async def test_signs_as_the_device(self):
        auth = make_auth(**DEVICE_CREDENTIALS)
        with patch("apyhiveapi.auth.datetime") as mock_datetime:
            mock_datetime.now.return_value = FROZEN_NOW
            answer = await auth._answer_device_challenge(
                DEVICE_CHALLENGE["ChallengeParameters"]
            )
        assert answer["USERNAME"] == "uuid-user"
        assert answer["DEVICE_KEY"] == "dev-key"
        assert auth.user_id == USERNAME
        key = auth._srp_key("grp-keydev-key", "dev-pass", "55667788", "11223344")
        assert answer["PASSWORD_CLAIM_SIGNATURE"] == expected_signature(
            key, "grp-keydev-key", "dGVzdA==", FROZEN_TIMESTAMP
        )

    async def test_needs_the_saved_credentials(self):
        auth = make_auth(device_group_key="grp-key", device_key="dev-key")
        with pytest.raises(HiveReauthRequired, match="no saved device credentials"):
            await auth._answer_device_challenge(DEVICE_CHALLENGE["ChallengeParameters"])
