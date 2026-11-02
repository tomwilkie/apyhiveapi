"""Unit tests for HiveAuth: SRP login, SMS 2FA, token refresh and trusted devices."""

import base64
import hashlib
import hmac
import re
import threading
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, call, patch

import botocore.exceptions
import pytest

from hiveheating.api import _TIMEOUT
from hiveheating.auth import (
    _COGNITO_CONFIG,
    _SSO_URL,
    HiveAuth,
)
from hiveheating.exceptions import (
    HiveApiError,
    HiveConnectionError,
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
    hash_sha256,
    hex_hash,
    hex_to_long,
    long_to_hex,
    pad_hex,
)

USERNAME = "user@test.com"
PASSWORD = "pass"
CLIENT_ID = "client-id"
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
    """Return a HiveAuth with a MagicMock Cognito client and pool name "pool"."""
    auth = HiveAuth(
        username,
        password,
        device_group_key=device_group_key,
        device_key=device_key,
        device_password=device_password,
    )
    auth.client = MagicMock()
    auth._client_id = CLIENT_ID
    auth._pool_name = "pool"
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
    verifier = pow(G, x_value, BIG_N)
    srp_b = (K * verifier + pow(G, server_secret, BIG_N)) % (BIG_N)
    u_value = calculate_u(auth.large_a_value, srp_b)
    shared = pow(
        auth.large_a_value * pow(verifier, u_value, BIG_N),
        server_secret,
        BIG_N,
    )
    return srp_b, compute_hkdf(
        bytearray.fromhex(pad_hex(shared)),
        bytearray.fromhex(pad_hex(long_to_hex(u_value))),
    )


_SSO_PAGE = (
    "<html><head><script>"
    'window.HiveSSOPoolId="eu-west-1_abc",'
    'window.HiveSSOPublicCognitoClientId="client123"'
    "</script></head></html>"
)


def _make_websession(status=200, body=""):
    """Return a mock ClientSession whose request() yields one response."""
    resp = MagicMock()
    resp.status = status
    resp.headers = {}
    resp.text = AsyncMock(return_value=body)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    websession = MagicMock()
    websession.request.return_value = resp
    return websession


# Tests for the constructor


class TestInit:
    @pytest.mark.parametrize(
        ("username", "use_file"),
        [("use@file.com", True), ("user@test.com", False), ("USE@FILE.COM", False)],
    )
    def test_file_mode_is_only_for_the_file_username(self, username, use_file):
        assert HiveAuth(username, "").use_file is use_file


# Tests for _ensure_client


def _no_config(missing_key):
    return f"The Hive login page did not include '{missing_key}'"


class TestEnsureClient:
    @pytest.mark.parametrize(
        ("html", "client_id", "pool_name"),
        [
            pytest.param(_SSO_PAGE, "client123", "abc", id="double-quoted"),
            pytest.param(
                "<script>\n"
                "        window.HiveSSOPoolId = 'eu-west-1_abc'\n"
                "        window.HiveSSOCognitoClientId = 'private123'\n"
                "        window.HiveSSOPublicCognitoClientId = 'client123'\n"
                "        window.HiveSSOState = '%%STATE%%'\n"
                "    </script>",
                "client123",
                "abc",
                id="single-quoted-with-spaces",
            ),
            pytest.param(
                '<SCRIPT type="text/javascript">'
                'window.HiveSSOPoolId = "eu-west-1_abc"; '
                'window.HiveSSOPublicCognitoClientId = "client123";'
                "</SCRIPT>",
                "client123",
                "abc",
                id="script-tag-attributes-and-uppercase",
            ),
            pytest.param(
                "<script>"
                'window.HiveSSOPoolId = "eu-west-1_it\'s"; '
                "window.HiveSSOPublicCognitoClientId = 'say \"hi\"'"
                "</script>",
                'say "hi"',
                "it's",
                id="value-contains-other-quote",
            ),
        ],
    )
    async def test_reads_the_pool_details_from_the_first_script(
        self, html, client_id, pool_name
    ):
        auth = HiveAuth(USERNAME, PASSWORD, websession=_make_websession(body=html))

        with patch("hiveheating.auth.boto3.client"):
            await auth._ensure_client()

        assert auth._client_id == client_id
        assert auth._pool_name == pool_name

    async def test_creates_the_client_once_in_the_pool_region(self):
        websession = _make_websession(body=_SSO_PAGE)
        auth = HiveAuth(USERNAME, PASSWORD, websession=websession)
        boto_client = MagicMock()

        with (
            patch("hiveheating.auth.ClientSession") as session_cls,
            patch(
                "hiveheating.auth.boto3.client", return_value=boto_client
            ) as boto3_client,
        ):
            await auth._ensure_client()
            await auth._ensure_client()

        websession.request.assert_called_once_with(
            "get", _SSO_URL, headers=None, data=None, timeout=_TIMEOUT
        )
        session_cls.assert_not_called()
        websession.close.assert_not_called()
        boto3_client.assert_called_once_with(
            "cognito-idp",
            "eu-west-1",
            aws_access_key_id="ACCESS_KEY",
            aws_secret_access_key="SECRET_KEY",
            aws_session_token="SESSION_TOKEN",
            config=_COGNITO_CONFIG,
        )
        assert auth.client is boto_client

    @pytest.mark.parametrize(
        ("html", "missing_key"),
        [
            pytest.param(
                "<script>window.Unrelated = 'x'</script>"
                "<script>window.HiveSSOPoolId = 'p' "
                "window.HiveSSOPublicCognitoClientId = 'c'</script>",
                "HiveSSOPoolId",
                id="config-in-second-script",
            ),
            pytest.param(
                "<script>window.HiveSSOPoolId = 'eu-west-1_abc\"</script>",
                "HiveSSOPoolId",
                id="mismatched-quotes",
            ),
            pytest.param(
                "<script>window.HiveSSOPoolId = 'eu-west-1_abc'</script>",
                "HiveSSOPublicCognitoClientId",
                id="pool-id-only",
            ),
            pytest.param(
                "<script>window.SomeOtherKey = 'value'</script>",
                "HiveSSOPoolId",
                id="unrelated-key",
            ),
            pytest.param("", "HiveSSOPoolId", id="empty-page"),
            pytest.param(
                "<html><body>Down for maintenance</body></html>",
                "HiveSSOPoolId",
                id="no-script",
            ),
        ],
    )
    async def test_page_without_the_config_raises_api_error(self, html, missing_key):
        auth = HiveAuth(USERNAME, PASSWORD, websession=_make_websession(body=html))

        with (
            patch("hiveheating.auth.boto3.client") as boto3_client,
            pytest.raises(HiveApiError) as exc_info,
        ):
            await auth._ensure_client()

        assert str(exc_info.value) == _no_config(missing_key)
        assert isinstance(exc_info.value.__cause__, KeyError)
        assert exc_info.value.__cause__.args == (missing_key,)
        boto3_client.assert_not_called()
        assert auth.client is None

    @pytest.mark.parametrize("pool_id", ["nounderscore", "eu-west-1_"])
    async def test_rejects_an_unexpected_pool_id(self, pool_id):
        html = (
            f"<script>window.HiveSSOPoolId = '{pool_id}' "
            "window.HiveSSOPublicCognitoClientId = 'c'</script>"
        )
        auth = HiveAuth(USERNAME, PASSWORD, websession=_make_websession(body=html))

        with (
            patch("hiveheating.auth.boto3.client") as boto3_client,
            pytest.raises(HiveApiError, match="Unexpected Cognito pool ID"),
        ):
            await auth._ensure_client()

        boto3_client.assert_not_called()
        assert auth.client is None

    async def test_opens_and_closes_a_temporary_session_when_none_is_given(self):
        temp = _make_websession(body=_SSO_PAGE)
        temp.__aenter__ = AsyncMock(return_value=temp)
        temp.__aexit__ = AsyncMock(return_value=False)
        auth = HiveAuth(USERNAME, PASSWORD)

        with (
            patch("hiveheating.auth.ClientSession", return_value=temp) as session_cls,
            patch("hiveheating.auth.boto3.client"),
        ):
            await auth._ensure_client()

        assert auth._client_id == "client123"
        session_cls.assert_called_once_with()
        temp.__aenter__.assert_awaited_once()
        temp.request.assert_called_once_with(
            "get", _SSO_URL, headers=None, data=None, timeout=_TIMEOUT
        )
        temp.__aexit__.assert_awaited_once()

    @pytest.mark.parametrize(
        ("method", "args", "cognito_method"),
        [
            pytest.param("login", (), "initiate_auth", id="login"),
            pytest.param(
                "device_login", (), "respond_to_auth_challenge", id="device-login"
            ),
            pytest.param(
                "sms_2fa",
                ("123456", SMS_CHALLENGE),
                "respond_to_auth_challenge",
                id="sms-2fa",
            ),
            pytest.param(
                "refresh_token", ("refresh-token",), "initiate_auth", id="refresh-token"
            ),
            pytest.param(
                "device_registration",
                ("my-device",),
                "confirm_device",
                id="device-registration",
            ),
            pytest.param(
                "is_device_registered", (), "get_device", id="is-device-registered"
            ),
            pytest.param(
                "forget_device",
                ("access-token", "dev-key"),
                "forget_device",
                id="forget-device",
            ),
        ],
    )
    async def test_every_cognito_call_creates_the_client_first(
        self, method, args, cognito_method
    ):
        auth = HiveAuth(
            USERNAME,
            PASSWORD,
            **DEVICE_CREDENTIALS,
            websession=_make_websession(body=_SSO_PAGE),
        )
        auth.access_token = "access-token"
        client = MagicMock()
        client.initiate_auth.return_value = PASSWORD_CHALLENGE
        client.respond_to_auth_challenge.side_effect = [DEVICE_CHALLENGE, AUTH_RESULT]
        client.get_device.return_value = REMEMBERED_DEVICE

        with patch("hiveheating.auth.boto3.client", return_value=client):
            await getattr(auth, method)(*args)

        assert auth.client is client
        assert getattr(client, cognito_method).called


# Tests for _cognito


class TestCognito:
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
        "code_map",
        [
            pytest.param(None, id="no-code-map"),
            pytest.param({}, id="empty-code-map"),
            pytest.param(
                {"CodeMismatchException": HiveInvalid2FACode}, id="other-code-mapped"
            ),
        ],
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

    @pytest.mark.parametrize(
        "response",
        [
            pytest.param(None, id="no-response"),
            pytest.param({}, id="empty-response"),
            pytest.param({"Error": None}, id="null-error"),
            pytest.param({"Error": {}}, id="empty-error"),
        ],
    )
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
            pytest.param(
                botocore.exceptions.EndpointConnectionError,
                {"endpoint_url": "x"},
                id="endpoint-connection-error",
            ),
            pytest.param(
                botocore.exceptions.ConnectTimeoutError,
                {"endpoint_url": "x"},
                id="connect-timeout",
            ),
            pytest.param(
                botocore.exceptions.ReadTimeoutError,
                {"endpoint_url": "x"},
                id="read-timeout",
            ),
            pytest.param(
                botocore.exceptions.ConnectionError,
                {"error": "boom"},
                id="connection-error",
            ),
            pytest.param(
                botocore.exceptions.HTTPClientError,
                {"error": "boom"},
                id="http-client-error",
            ),
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
        with patch("hiveheating.auth._fetch", new=AsyncMock()) as fetch:
            result = await auth.login()
        assert result is auth.file_response
        assert result == {"AuthenticationResult": {"AccessToken": "file"}}
        fetch.assert_not_awaited()
        assert auth.client is None
        assert auth.access_token is None

    async def test_uses_a_fresh_srp_ephemeral_for_every_attempt(self):
        auth = make_auth()
        auth.client.initiate_auth.return_value = PASSWORD_CHALLENGE
        auth.client.respond_to_auth_challenge.return_value = AUTH_RESULT
        await auth.login()
        second_a = auth.large_a_value
        await auth.login()
        third_a = auth.large_a_value
        assert second_a != third_a
        assert auth.large_a_value == pow(G, auth.small_a_value, BIG_N)
        sent = [
            kwargs["AuthParameters"]["SRP_A"]
            for _name, _args, kwargs in auth.client.initiate_auth.mock_calls
        ]
        assert sent == [long_to_hex(second_a), long_to_hex(third_a)]

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

    async def test_a_saved_device_is_named_in_the_verifier_answer_not_the_handshake(
        self,
    ):
        auth = make_auth(**DEVICE_CREDENTIALS)
        auth.client.initiate_auth.return_value = PASSWORD_CHALLENGE
        auth.client.respond_to_auth_challenge.return_value = AUTH_RESULT
        await auth.login()
        auth.client.initiate_auth.assert_called_once_with(
            AuthFlow="USER_SRP_AUTH",
            AuthParameters={
                "USERNAME": USERNAME,
                "SRP_A": long_to_hex(auth.large_a_value),
            },
            ClientId=CLIENT_ID,
        )
        answer = auth.client.respond_to_auth_challenge.call_args.kwargs
        assert answer["ChallengeResponses"]["DEVICE_KEY"] == "dev-key"
        assert auth.access_token == "access-token"
        assert auth.device_group_key == "grp-key"
        assert auth.device_key == "dev-key"

    async def test_the_verifier_can_answer_with_another_challenge(self):
        auth = make_auth()
        auth.client.initiate_auth.return_value = PASSWORD_CHALLENGE
        auth.client.respond_to_auth_challenge.return_value = SMS_CHALLENGE
        result = await auth.login()
        assert result is SMS_CHALLENGE
        assert auth.access_token is None

    @pytest.mark.parametrize(
        ("response", "name"),
        [
            pytest.param(
                {"ChallengeName": "NEW_PASSWORD_REQUIRED", "ChallengeParameters": {}},
                "NEW_PASSWORD_REQUIRED",
                id="new-password-required",
            ),
            pytest.param(
                {"ChallengeName": "CUSTOM_CHALLENGE"},
                "CUSTOM_CHALLENGE",
                id="custom-challenge",
            ),
            pytest.param({}, "None", id="no-challenge"),
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


# Tests for device_login


class TestDeviceLogin:
    @pytest.mark.parametrize(
        "missing", ["device_group_key", "device_key", "device_password"]
    )
    async def test_missing_credentials_need_a_fresh_login(self, missing):
        auth = HiveAuth(USERNAME, PASSWORD, **DEVICE_CREDENTIALS)
        setattr(auth, missing, None)
        with (
            patch("hiveheating.auth._fetch", new=AsyncMock()) as fetch,
            pytest.raises(HiveReauthRequired, match="no saved device credentials"),
        ):
            await auth.device_login()
        fetch.assert_not_awaited()
        assert auth.client is None

    async def test_completes_the_device_srp_handshake(self):
        auth = make_auth(**DEVICE_CREDENTIALS)
        auth.client.respond_to_auth_challenge.side_effect = [
            DEVICE_CHALLENGE,
            NEW_DEVICE_RESULT,
        ]
        result = await auth.device_login()
        assert result is NEW_DEVICE_RESULT
        assert auth.access_token == "access-token"
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
        assert auth.user_id == USERNAME
        assert answer["DEVICE_KEY"] == "dev-key"
        assert answer["PASSWORD_CLAIM_SECRET_BLOCK"] == "dGVzdA=="
        assert TIMESTAMP_RE.fullmatch(answer["TIMESTAMP"])
        # Signed with the key for group key plus device key and the device password.
        key = auth._srp_key("grp-keydev-key", "dev-pass", "55667788", "11223344")
        assert answer["PASSWORD_CLAIM_SIGNATURE"] == expected_signature(
            key, "grp-keydev-key", "dGVzdA==", answer["TIMESTAMP"]
        )
        # Cognito's new device keys replace the saved ones; the password stays.
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
        ("code", "answered", "call_count"),
        [
            pytest.param("NotAuthorizedException", [], 1, id="handshake-NotAuthorized"),
            pytest.param(
                "ResourceNotFoundException", [], 1, id="handshake-ResourceNotFound"
            ),
            pytest.param(
                "NotAuthorizedException",
                [DEVICE_CHALLENGE],
                2,
                id="verifier-NotAuthorized",
            ),
            pytest.param(
                "ResourceNotFoundException",
                [DEVICE_CHALLENGE],
                2,
                id="verifier-ResourceNotFound",
            ),
        ],
    )
    async def test_a_rejected_device(self, code, answered, call_count):
        auth = make_auth(**DEVICE_CREDENTIALS)
        err = client_error(code, operation="RespondToAuthChallenge")
        auth.client.respond_to_auth_challenge.side_effect = [*answered, err]
        with pytest.raises(HiveReauthRequired) as exc_info:
            await auth.device_login()
        assert exc_info.type is HiveReauthRequired
        assert exc_info.value.__cause__ is err
        assert auth.client.respond_to_auth_challenge.call_count == call_count
        assert auth.access_token is None


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
        assert auth.device_group_key == "new-grp"
        assert auth.device_key == "new-dev"

    async def test_a_result_without_tokens_is_returned_as_is(self):
        auth = make_auth()
        retry = {"ChallengeName": "SMS_MFA", "Session": "session-2"}
        auth.client.respond_to_auth_challenge.return_value = retry
        result = await auth.sms_2fa("000000", SMS_CHALLENGE)
        assert result is retry
        assert auth.access_token is None

    @pytest.mark.parametrize(
        "challenge",
        [
            pytest.param({}, id="empty-challenge"),
            pytest.param({"ChallengeName": "SMS_MFA"}, id="no-session"),
            pytest.param({"Session": ""}, id="empty-session"),
        ],
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


# Tests for refresh_token


class TestRefreshToken:
    @pytest.mark.parametrize("token", [None, ""])
    async def test_without_a_refresh_token_the_user_must_log_in_again(self, token):
        auth = HiveAuth(USERNAME, PASSWORD)
        with (
            patch("hiveheating.auth._fetch", new=AsyncMock()) as fetch,
            pytest.raises(HiveReauthRequired, match="There is no refresh token"),
        ):
            await auth.refresh_token(token)
        fetch.assert_not_awaited()
        assert auth.client is None

    @pytest.mark.parametrize(
        ("device_key", "expected_params"),
        [
            pytest.param(None, {"REFRESH_TOKEN": "refresh-token"}, id="no-device"),
            pytest.param(
                "dev-key",
                {"REFRESH_TOKEN": "refresh-token", "DEVICE_KEY": "dev-key"},
                id="saved-device",
            ),
        ],
    )
    async def test_exchanges_the_refresh_token(self, device_key, expected_params):
        auth = make_auth(device_key=device_key)
        auth.client.initiate_auth.return_value = AUTH_RESULT
        result = await auth.refresh_token("refresh-token")
        assert result is AUTH_RESULT
        auth.client.initiate_auth.assert_called_once_with(
            ClientId=CLIENT_ID,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters=expected_params,
        )
        assert auth.access_token == "access-token"

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
        ],
    )
    async def test_refused_refresh_codes_map_to_hive_errors(self, code, exc_type):
        auth = make_auth()
        err = client_error(code, "Invalid Refresh Token")
        auth.client.initiate_auth.side_effect = err
        with pytest.raises(exc_type) as exc_info:
            await auth.refresh_token("refresh-token")
        assert exc_info.type is exc_type
        assert exc_info.value.__cause__ is err
        assert auth.access_token is None


# Tests for the trusted-device methods


class TestDeviceRegistration:
    @pytest.mark.parametrize(
        ("device_name", "expected_name"),
        [
            pytest.param(None, "my-host", id="hostname-by-default"),
            pytest.param("Home Assistant", "Home Assistant", id="given-name"),
        ],
    )
    async def test_confirms_then_remembers_the_device(self, device_name, expected_name):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        with patch("hiveheating.auth.socket.gethostname", return_value="my-host"):
            assert await auth.device_registration(device_name) is None
        names = [name for name, _args, _kwargs in auth.client.method_calls]
        assert names == ["confirm_device", "update_device_status"]
        kwargs = auth.client.confirm_device.call_args.kwargs
        assert set(kwargs) == {
            "AccessToken",
            "DeviceKey",
            "DeviceName",
            "DeviceSecretVerifierConfig",
        }
        assert kwargs["AccessToken"] == "access-token"
        assert kwargs["DeviceKey"] == "dev-key"
        assert kwargs["DeviceName"] == expected_name
        auth.client.update_device_status.assert_called_once_with(
            AccessToken="access-token",
            DeviceKey="dev-key",
            DeviceRememberedStatus="remembered",
        )

    async def test_the_verifier_proves_a_new_device_password(self):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        await auth.device_registration("Home Assistant")
        password = auth.device_password
        assert password != "dev-pass"
        assert len(base64.standard_b64decode(password)) == 40
        # Cognito will check the verifier against the salt and this password:
        # it must be g to the power of the hash of the salt and of
        # "<group key><device key>:<password>".
        config = auth.client.confirm_device.call_args.kwargs[
            "DeviceSecretVerifierConfig"
        ]
        assert set(config) == {"PasswordVerifier", "Salt"}
        salt = base64.standard_b64decode(config["Salt"]).hex()
        x_value = hex_to_long(
            hex_hash(salt + hash_sha256(f"grp-keydev-key:{password}".encode()))
        )
        verifier = pad_hex(pow(G, x_value, BIG_N))
        decoded = base64.standard_b64decode(config["PasswordVerifier"])
        assert decoded == bytes.fromhex(verifier)

    async def test_a_refused_confirmation_keeps_the_saved_password_and_stops(self):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        err = client_error("NotAuthorizedException", operation="ConfirmDevice")
        auth.client.confirm_device.side_effect = err
        with pytest.raises(HiveApiError) as exc_info:
            await auth.device_registration("Home Assistant")
        assert exc_info.type is HiveApiError
        assert exc_info.value.__cause__ is err
        assert auth.device_password == "dev-pass"
        auth.client.update_device_status.assert_not_called()

    @pytest.mark.parametrize(
        "missing",
        [
            pytest.param({"device_group_key": None}, id="no-device-group-key"),
            pytest.param({"device_key": ""}, id="empty-device-key"),
        ],
    )
    async def test_without_device_keys_raises_before_any_call(self, missing):
        auth = make_auth(**{**DEVICE_CREDENTIALS, **missing}, access_token="tok")
        with pytest.raises(HiveApiError, match="no device keys"):
            await auth.device_registration("Home Assistant")
        assert auth.client.method_calls == []
        assert auth.device_password == "dev-pass"


class TestGetDeviceData:
    @pytest.mark.parametrize(
        ("credentials", "expected"),
        [
            pytest.param(
                DEVICE_CREDENTIALS, ("grp-key", "dev-key", "dev-pass"), id="saved"
            ),
            pytest.param({}, (None, None, None), id="before-any-login"),
        ],
    )
    def test_returns_the_credentials_to_save(self, credentials, expected):
        auth = HiveAuth(USERNAME, PASSWORD, **credentials)
        assert auth.get_device_data() == expected


class TestIsDeviceRegistered:
    @pytest.mark.parametrize(
        ("access_token", "device_key"),
        [(None, "dev-key"), ("", "dev-key"), ("access-token", None), (None, None)],
    )
    async def test_false_without_a_token_or_key(self, access_token, device_key):
        auth = HiveAuth(USERNAME, PASSWORD, device_key=device_key)
        auth.access_token = access_token
        with patch("hiveheating.auth._fetch", new=AsyncMock()) as fetch:
            assert await auth.is_device_registered() is False
        fetch.assert_not_awaited()
        assert auth.client is None

    async def test_unreadable_login_page_raises_rather_than_answering_false(self):
        auth = HiveAuth(
            USERNAME,
            PASSWORD,
            **DEVICE_CREDENTIALS,
            websession=_make_websession(body="Down for maintenance"),
        )
        auth.access_token = "access-token"
        with pytest.raises(HiveApiError, match="did not include"):
            await auth.is_device_registered()

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
            pytest.param(
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
                id="not-remembered",
            ),
            pytest.param(
                {
                    "Device": {
                        "DeviceAttributes": [{"Name": "device_name", "Value": "x"}]
                    }
                },
                id="no-remembered-status-attribute",
            ),
            pytest.param(
                {"Device": {"DeviceAttributes": [{"Value": "remembered"}]}},
                id="attribute-without-a-name",
            ),
            pytest.param({"Device": {"DeviceAttributes": []}}, id="no-attributes"),
            pytest.param({"Device": {}}, id="no-attribute-list"),
            pytest.param({}, id="no-device"),
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
    async def test_false_when_cognito_refuses(self, code):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        auth.client.get_device.side_effect = client_error(
            code, "Device does not exist.", "GetDevice"
        )
        assert await auth.is_device_registered() is False

    async def test_connection_errors_propagate(self):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        err = botocore.exceptions.EndpointConnectionError(endpoint_url="x")
        auth.client.get_device.side_effect = err
        with pytest.raises(HiveConnectionError) as exc_info:
            await auth.is_device_registered()
        assert exc_info.value.__cause__ is err


class TestForgetDevice:
    async def test_forgets_the_given_device(self):
        auth = make_auth(**DEVICE_CREDENTIALS, access_token="access-token")
        auth.client.forget_device.return_value = {}
        result = await auth.forget_device("other-token", "other-key")
        assert result == {}
        auth.client.forget_device.assert_called_once_with(
            AccessToken="other-token", DeviceKey="other-key"
        )


# Tests for the SRP helpers


class TestCognitoTimestamp:
    @pytest.mark.parametrize(
        ("now", "expected"),
        [
            pytest.param(FROZEN_NOW, FROZEN_TIMESTAMP, id="single-digit-day"),
            pytest.param(
                datetime(2024, 1, 15, 3, 4, 5, tzinfo=UTC),
                "Mon Jan 15 03:04:05 UTC 2024",
                id="two-digit-day",
            ),
            pytest.param(
                datetime(2026, 12, 31, 23, 59, 0, tzinfo=UTC),
                "Thu Dec 31 23:59:00 UTC 2026",
                id="end-of-year",
            ),
        ],
    )
    def test_strips_the_leading_zero_from_the_day(self, now, expected):
        with patch("hiveheating.auth.datetime") as mock_datetime:
            mock_datetime.now.return_value = now
            assert HiveAuth._cognito_timestamp() == expected
        mock_datetime.now.assert_called_once_with(UTC)


class TestSrpEphemeral:
    @pytest.mark.parametrize("multiple", [0, 1, 2])
    def test_calculate_a_rejects_a_multiple_of_n(self, multiple):
        with (
            patch("hiveheating.srp.pow", create=True, return_value=multiple * BIG_N),
            pytest.raises(ValueError, match="Safety check for A failed"),
        ):
            calculate_a(12345)


class TestSrpKey:
    def test_agrees_with_cognito_s_side_of_the_handshake(self):
        auth = HiveAuth(USERNAME, PASSWORD)
        auth._new_srp_ephemeral()
        srp_b, server_key = cognito_server_key(
            auth, "pooluuid-user", PASSWORD, "aabbccdd", server_secret=0x1234_5678_9ABC
        )
        key = auth._srp_key("pooluuid-user", PASSWORD, long_to_hex(srp_b), "aabbccdd")
        assert key == server_key

    def test_rejects_a_zero_u_value(self):
        auth = HiveAuth(USERNAME, PASSWORD)
        auth._new_srp_ephemeral()
        with (
            patch("hiveheating.auth.calculate_u", return_value=0),
            pytest.raises(ValueError, match="U cannot be zero"),
        ):
            auth._srp_key("pooluuid-user", PASSWORD, "ccddee", "aabbccdd")

    def test_known_answer(self):
        # Regression vector pinned from the implementation, whose maths the
        # server-side tests above verify independently.
        auth = HiveAuth(USERNAME, PASSWORD)
        auth.small_a_value = 12345
        auth.large_a_value = calculate_a(12345)
        key = auth._srp_key("pooluser", "pass", "ccddee", "aabbccdd")
        assert key.hex() == "f61d3eda114bbcdcd35c229bad30c781"


class TestAnswerSrpChallenge:
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
