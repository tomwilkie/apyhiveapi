"""Tests for the Hive session: login, tokens, API calls and poll errors."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from apyhiveapi.api import HiveApi
from apyhiveapi.auth import HiveAuth
from apyhiveapi.exceptions import (
    HiveApiError,
    HiveConnectionError,
    HiveDeviceOffline,
    HiveError,
    HiveInvalid2FACode,
    HiveInvalidPassword,
    HiveInvalidUsername,
    HiveReauthRequired,
)
from apyhiveapi.heating import Climate
from apyhiveapi.hive import POLL_RETRY_DELAY, REFRESH_THRESHOLD, Hive
from apyhiveapi.hotwater import WaterHeater
from apyhiveapi.models import SessionData, SessionTokens

AUTH_RESULT = {
    "AuthenticationResult": {
        "IdToken": "id-token-0001",
        "AccessToken": "access-token-0001",
        "RefreshToken": "refresh-token-0001",
        "ExpiresIn": 3600,
    }
}
NEW_TOKENS = {
    "token": "id-token-0001",
    "refreshToken": "refresh-token-0001",
    "accessToken": "access-token-0001",
}
OLD_TOKENS = {"token": "id-tok", "refreshToken": "rt", "accessToken": "acc-tok"}
SMS_CHALLENGE = {"ChallengeName": HiveAuth.SMS_MFA_CHALLENGE}
DEVICE_CHALLENGE = {"ChallengeName": HiveAuth.DEVICE_VERIFIER_CHALLENGE}
_LOGGER_NAME = "apyhiveapi.hive"
_LAST_POLL = datetime(2024, 1, 1, tzinfo=UTC)
_TOLERANCE = timedelta(seconds=5)


def _nodes():
    """Return a minimal all-nodes response."""
    return {
        "user": {"id": "u1", "temperatureUnit": "C"},
        "products": [{"id": "p1", "type": "heating", "state": {"name": "Hall"}}],
        "devices": [{"id": "d1", "type": "hub", "state": {"name": "Hub"}}],
    }


def _make_hive():
    """Return a Hive with fresh tokens whose Cognito and API calls are mocked."""
    hive = Hive(username="user@example.com", password="pw")
    hive.auth.login = AsyncMock()
    hive.auth.device_login = AsyncMock()
    hive.auth.refresh_token = AsyncMock()
    hive.api.get_all = AsyncMock(return_value=_nodes())
    hive.api.set_state = AsyncMock()
    hive.tokens.token_data = dict(OLD_TOKENS)
    hive.tokens.token_created = datetime.now(UTC)
    return hive


def _expire_tokens(hive):
    """Make the token refresh due."""
    hive.tokens.token_created = datetime.now(UTC) - timedelta(hours=2)
    hive.tokens.token_expiry = timedelta(hours=1)


def _is_about(moment, expected):
    return abs(moment - expected) < _TOLERANCE


def _warnings(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _LOGGER_NAME and record.levelno == logging.WARNING
    ]


class TestInit:
    def test_builds_the_auth_api_and_session_state(self):
        hive = Hive(username="user@example.com", password="pw")
        assert isinstance(hive.auth, HiveAuth)
        assert hive.auth.username == "user@example.com"
        assert hive.auth.password == "pw"
        assert hive.auth._websession is None
        assert isinstance(hive.api, HiveApi)
        assert hive.api.session is hive
        assert hive.api.websession is None
        assert hive.tokens == SessionTokens()
        assert hive.config.username == "user@example.com"
        assert hive.config.file is False
        assert hive.data == SessionData()
        assert isinstance(hive.heating, Climate)
        assert hive.heating.session is hive
        assert isinstance(hive.hotwater, WaterHeater)
        assert hive.hotwater.session is hive
        # update_lock is held while _api_call takes _refresh_lock.
        assert isinstance(hive.update_lock, asyncio.Lock)
        assert isinstance(hive._refresh_lock, asyncio.Lock)
        assert hive._refresh_lock is not hive.update_lock
        assert hive._offline_devices == set()

    def test_passes_the_websession_to_auth_and_api(self):
        websession = MagicMock()
        hive = Hive(websession, username="user@example.com", password="pw")
        assert hive.api.websession is websession
        assert hive.auth._websession is websession

    def test_missing_credentials_default_to_empty_strings(self):
        hive = Hive()
        assert hive.auth.username == ""
        assert hive.auth.password == ""
        assert hive.config.username is None


class TestUpdateTokens:
    def test_cognito_result_stores_tokens_expiry_and_issue_time(self):
        hive = _make_hive()
        hive.tokens.token_created = _LAST_POLL
        hive.tokens.token_expiry = timedelta(seconds=900)
        hive.update_tokens(AUTH_RESULT)
        assert hive.tokens.token_data == NEW_TOKENS
        assert hive.tokens.token_expiry == timedelta(seconds=3600)
        assert hive.tokens.token_created.tzinfo is UTC
        assert _is_about(hive.tokens.token_created, datetime.now(UTC))

    def test_update_expiry_time_false_leaves_token_created(self):
        hive = _make_hive()
        hive.tokens.token_created = _LAST_POLL
        hive.update_tokens(AUTH_RESULT, update_expiry_time=False)
        assert hive.tokens.token_created == _LAST_POLL
        assert hive.tokens.token_data == NEW_TOKENS

    @pytest.mark.parametrize(
        ("tokens", "expected_token"),
        [
            pytest.param(
                {"AuthenticationResult": {"IdToken": "new-id"}},
                "new-id",
                id="cognito-result",
            ),
            pytest.param({"token": "t-only"}, "t-only", id="flat-dict"),
        ],
    )
    def test_missing_keys_are_not_written(self, tokens, expected_token):
        hive = _make_hive()
        hive.update_tokens(tokens)
        assert hive.tokens.token_data == {**OLD_TOKENS, "token": expected_token}
        assert hive.tokens.token_expiry == timedelta(seconds=3600)

    @pytest.mark.parametrize(
        "tokens",
        [
            pytest.param(
                {"AuthenticationResult": None}, id="null-authentication-result"
            ),
            pytest.param(
                {"ChallengeName": "SMS_MFA", "Session": "abc"}, id="sms-challenge"
            ),
        ],
    )
    def test_input_without_tokens_changes_nothing(self, tokens):
        hive = _make_hive()
        hive.tokens.token_created = _LAST_POLL
        hive.tokens.token_expiry = timedelta(seconds=900)
        hive.update_tokens(tokens)
        assert hive.tokens.token_data == OLD_TOKENS
        assert hive.tokens.token_created == _LAST_POLL
        assert hive.tokens.token_expiry == timedelta(seconds=900)

    def test_flat_dict_is_stored_as_is(self):
        hive = _make_hive()
        hive.tokens.token_created = _LAST_POLL
        hive.tokens.token_expiry = timedelta(seconds=900)
        hive.update_tokens(dict(NEW_TOKENS))
        assert hive.tokens.token_data == NEW_TOKENS
        assert _is_about(hive.tokens.token_created, datetime.now(UTC))
        assert hive.tokens.token_expiry == timedelta(seconds=900)

    def test_debug_log_shows_no_part_of_a_long_token(self, caplog):
        hive = _make_hive()
        tokens = {
            "IdToken": "IDHEADxxxxxxxxxxxxxxxxIDTAIL",
            "AccessToken": "ACHEADxxxxxxxxxxxxxxxxACTAIL",
            "RefreshToken": "RFHEADxxxxxxxxxxxxxxxxRFTAIL",
        }
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            hive.update_tokens({"AuthenticationResult": tokens})
        assert "update_tokens" in caplog.text
        for fragment in ("IDHEAD", "IDTAIL", "ACHEAD", "ACTAIL", "RFHEAD", "RFTAIL"):
            assert fragment not in caplog.text


class TestLogin:
    async def test_stores_the_tokens(self):
        hive = _make_hive()
        hive.auth.login.return_value = AUTH_RESULT
        hive.tokens.token_created = _LAST_POLL
        assert await hive.login() is None
        hive.auth.login.assert_awaited_once_with()
        hive.auth.device_login.assert_not_awaited()
        assert hive.tokens.token_data == NEW_TOKENS
        assert _is_about(hive.tokens.token_created, datetime.now(UTC))

    async def test_device_challenge_is_answered_with_device_login(self):
        hive = _make_hive()
        hive.auth.login.return_value = DEVICE_CHALLENGE
        hive.auth.device_login.return_value = AUTH_RESULT
        await hive.login()
        hive.auth.device_login.assert_awaited_once_with()
        assert hive.tokens.token_data == NEW_TOKENS

    @pytest.mark.parametrize(
        ("login_result", "device_login_result"),
        [
            pytest.param(SMS_CHALLENGE, None, id="direct"),
            pytest.param(DEVICE_CHALLENGE, SMS_CHALLENGE, id="after-device-challenge"),
        ],
    )
    async def test_sms_challenge_raises_reauth_required(
        self, login_result, device_login_result
    ):
        hive = _make_hive()
        hive.auth.login.return_value = login_result
        hive.auth.device_login.return_value = device_login_result
        with pytest.raises(HiveReauthRequired, match="SMS"):
            await hive.login()
        if device_login_result is None:
            hive.auth.device_login.assert_not_awaited()
        else:
            hive.auth.device_login.assert_awaited_once_with()
        assert hive.tokens.token_data == OLD_TOKENS

    @pytest.mark.parametrize(
        "result",
        [
            pytest.param(
                {"ChallengeName": "NEW_PASSWORD_REQUIRED"}, id="unknown-challenge"
            ),
            pytest.param({}, id="no-tokens"),
        ],
    )
    async def test_unanswerable_login_raises_api_error(self, result):
        hive = _make_hive()
        hive.auth.login.return_value = result
        with pytest.raises(HiveApiError) as excinfo:
            await hive.login()
        assert str(excinfo.value) == (
            f"Login did not complete: {result.get('ChallengeName')}"
        )
        assert hive.tokens.token_data == OLD_TOKENS

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(HiveInvalidUsername("unknown user"), id="invalid-username"),
            pytest.param(HiveInvalidPassword("wrong password"), id="invalid-password"),
            pytest.param(
                HiveReauthRequired("device no longer trusted"), id="reauth-required"
            ),
            pytest.param(
                HiveConnectionError("Cognito timed out"), id="connection-error"
            ),
            pytest.param(HiveApiError("Cognito refused"), id="api-error"),
        ],
    )
    async def test_login_errors_propagate_unchanged(self, error):
        hive = _make_hive()
        hive.auth.login.side_effect = error
        with pytest.raises(type(error)) as excinfo:
            await hive.login()
        assert excinfo.value is error
        assert hive.tokens.token_data == OLD_TOKENS

    async def test_device_login_errors_propagate_unchanged(self):
        hive = _make_hive()
        hive.auth.login.return_value = DEVICE_CHALLENGE
        error = HiveConnectionError("Cognito timed out")
        hive.auth.device_login.side_effect = error
        with pytest.raises(HiveConnectionError) as excinfo:
            await hive.login()
        assert excinfo.value is error


class TestReauthLatch:
    @pytest.mark.parametrize(
        "login",
        [
            pytest.param({"return_value": SMS_CHALLENGE}, id="sms-challenge"),
            pytest.param(
                {"side_effect": HiveInvalidPassword("wrong password")},
                id="rejected-password",
            ),
        ],
    )
    async def test_a_failed_login_is_not_tried_again(self, login):
        hive = _make_hive()
        hive.auth.login.configure_mock(**login)
        with pytest.raises(HiveReauthRequired) as first:
            await hive.login()

        with pytest.raises(HiveReauthRequired) as second:
            await hive.login()

        assert second.value is first.value
        hive.auth.login.assert_awaited_once_with()

    async def test_api_calls_fail_without_contacting_cognito_or_hive(self):
        hive = _make_hive()
        hive.auth.login.return_value = SMS_CHALLENGE
        api_call = AsyncMock(side_effect=HiveReauthRequired("HTTP 401"))
        with pytest.raises(HiveReauthRequired):
            await hive._api_call(api_call)
        _expire_tokens(hive)

        with pytest.raises(HiveReauthRequired, match="SMS"):
            await hive._api_call(api_call)

        api_call.assert_awaited_once_with()
        hive.auth.login.assert_awaited_once_with()
        hive.auth.refresh_token.assert_not_awaited()

    async def test_new_tokens_allow_logging_in_again(self):
        hive = _make_hive()
        hive.auth.login.return_value = SMS_CHALLENGE
        with pytest.raises(HiveReauthRequired):
            await hive.login()
        hive.update_tokens(AUTH_RESULT)
        hive.auth.login.return_value = AUTH_RESULT

        await hive.login()

        assert hive.auth.login.await_count == 2

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(
                HiveConnectionError("Cognito timed out"), id="connection-error"
            ),
            pytest.param(HiveApiError("Cognito refused"), id="api-error"),
        ],
    )
    async def test_other_login_failures_are_tried_again(self, error):
        hive = _make_hive()
        hive.auth.login.side_effect = [error, AUTH_RESULT]
        with pytest.raises(type(error)):
            await hive.login()

        await hive.login()

        assert hive.tokens.token_data == NEW_TOKENS


class TestRefreshDue:
    def test_due_once_the_threshold_share_of_the_lifetime_has_passed(self):
        assert REFRESH_THRESHOLD == 0.90
        hive = _make_hive()
        hive.tokens.token_expiry = timedelta(seconds=1000)
        threshold = timedelta(seconds=1000 * REFRESH_THRESHOLD)
        hive.tokens.token_created = datetime.now(UTC) - threshold + _TOLERANCE
        assert hive._refresh_due() is False
        hive.tokens.token_created = datetime.now(UTC) - threshold - _TOLERANCE
        assert hive._refresh_due() is True

    def test_new_session_is_due(self):
        hive = Hive(username="user@example.com", password="pw")
        assert hive._refresh_due() is True


class TestHiveRefreshTokens:
    async def test_file_mode_never_refreshes(self):
        hive = _make_hive()
        hive.config.file = True
        _expire_tokens(hive)
        assert await hive.hive_refresh_tokens() is None
        hive.auth.refresh_token.assert_not_awaited()

    async def test_fresh_tokens_are_left_alone(self):
        hive = _make_hive()
        assert await hive.hive_refresh_tokens() is None
        hive.auth.refresh_token.assert_not_awaited()
        assert hive.tokens.token_data == OLD_TOKENS

    async def test_due_tokens_are_refreshed(self):
        hive = _make_hive()
        _expire_tokens(hive)
        hive.auth.refresh_token.return_value = AUTH_RESULT
        assert await hive.hive_refresh_tokens() is None
        hive.auth.refresh_token.assert_awaited_once_with("rt")
        assert hive.tokens.token_data == NEW_TOKENS
        assert _is_about(hive.tokens.token_created, datetime.now(UTC))
        assert hive._refresh_due() is False

    async def test_rejected_refresh_logs_in_again(self, caplog):
        hive = _make_hive()
        _expire_tokens(hive)
        hive.auth.refresh_token.side_effect = HiveReauthRequired("refresh expired")
        hive.auth.login.return_value = AUTH_RESULT
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            assert await hive.hive_refresh_tokens() is None
        hive.auth.login.assert_awaited_once_with()
        assert hive.tokens.token_data == NEW_TOKENS
        assert "Token refresh rejected (refresh expired)" in caplog.text

    @pytest.mark.parametrize(
        ("login", "exc"),
        [
            pytest.param(
                {"return_value": SMS_CHALLENGE}, HiveReauthRequired, id="sms-challenge"
            ),
            pytest.param(
                {"side_effect": HiveInvalidPassword("wrong password")},
                HiveInvalidPassword,
                id="rejected-password",
            ),
        ],
    )
    async def test_rejected_refresh_and_failed_login_propagates(self, login, exc):
        hive = _make_hive()
        _expire_tokens(hive)
        hive.auth.refresh_token.side_effect = HiveReauthRequired("refresh expired")
        hive.auth.login.configure_mock(**login)
        with pytest.raises(exc):
            await hive.hive_refresh_tokens()
        hive.auth.login.assert_awaited_once_with()

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(
                HiveConnectionError("Cognito timed out"), id="connection-error"
            ),
            pytest.param(HiveApiError("Cognito refused"), id="api-error"),
        ],
    )
    async def test_connection_and_api_errors_propagate(self, error):
        hive = _make_hive()
        _expire_tokens(hive)
        hive.auth.refresh_token.side_effect = error
        with pytest.raises(type(error)) as excinfo:
            await hive.hive_refresh_tokens()
        assert excinfo.value is error
        hive.auth.login.assert_not_awaited()
        assert hive.tokens.token_data == OLD_TOKENS
        assert not hive._refresh_lock.locked()

    async def test_concurrent_callers_refresh_once(self):
        hive = _make_hive()
        _expire_tokens(hive)

        async def _refresh(_token):
            # Yield so the second caller runs while the refresh is in flight.
            await asyncio.sleep(0)
            hive.update_tokens(AUTH_RESULT)
            return AUTH_RESULT

        hive.auth.refresh_token = AsyncMock(side_effect=_refresh)
        await asyncio.gather(hive.hive_refresh_tokens(), hive.hive_refresh_tokens())
        hive.auth.refresh_token.assert_awaited_once_with("rt")
        assert hive.tokens.token_data == NEW_TOKENS


class TestApiCall:
    async def test_refreshes_the_tokens_before_the_call(self):
        hive = _make_hive()
        _expire_tokens(hive)
        hive.auth.refresh_token.return_value = AUTH_RESULT

        async def _call():
            # The refreshed tokens are in place by the time the call runs.
            assert hive.tokens.token_data == NEW_TOKENS
            return "result"

        assert await hive._api_call(_call) == "result"
        hive.auth.refresh_token.assert_awaited_once_with("rt")

    async def test_rejected_token_logs_in_and_retries_once(self, caplog):
        hive = _make_hive()
        hive.auth.login.return_value = AUTH_RESULT
        api_call = AsyncMock(side_effect=[HiveReauthRequired("HTTP 401"), "second"])
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            assert await hive._api_call(api_call) == "second"
        hive.auth.login.assert_awaited_once_with()
        assert api_call.await_count == 2
        assert hive.tokens.token_data == NEW_TOKENS
        assert "Hive rejected the token (HTTP 401)" in caplog.text

    async def test_second_rejection_propagates(self):
        hive = _make_hive()
        hive.login = AsyncMock()
        second = HiveReauthRequired("still rejected")
        api_call = AsyncMock(side_effect=[HiveReauthRequired("HTTP 401"), second])
        with pytest.raises(HiveReauthRequired) as excinfo:
            await hive._api_call(api_call)
        assert excinfo.value is second
        hive.login.assert_awaited_once_with()
        assert api_call.await_count == 2

    async def test_failed_login_propagates(self):
        hive = _make_hive()
        hive.login = AsyncMock(side_effect=HiveInvalidPassword("wrong password"))
        api_call = AsyncMock(side_effect=HiveReauthRequired("HTTP 401"))
        with pytest.raises(HiveInvalidPassword):
            await hive._api_call(api_call)
        api_call.assert_awaited_once_with()

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(
                HiveConnectionError("Cognito timed out"), id="connection-error"
            ),
            pytest.param(HiveReauthRequired("expired"), id="reauth-required"),
        ],
    )
    async def test_refresh_errors_propagate_before_the_call(self, error):
        hive = _make_hive()
        hive.hive_refresh_tokens = AsyncMock(side_effect=error)
        api_call = AsyncMock()
        with pytest.raises(type(error)) as excinfo:
            await hive._api_call(api_call)
        assert excinfo.value is error
        api_call.assert_not_awaited()

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(HiveConnectionError("Hive timed out"), id="connection-error"),
            pytest.param(HiveApiError("Hive refused"), id="api-error"),
            pytest.param(HiveDeviceOffline("offline"), id="device-offline"),
            pytest.param(ValueError("boom"), id="programming-error"),
        ],
    )
    async def test_other_errors_propagate_without_a_login(self, error):
        hive = _make_hive()
        hive.login = AsyncMock()
        api_call = AsyncMock(side_effect=error)
        with pytest.raises(type(error)) as excinfo:
            await hive._api_call(api_call)
        assert excinfo.value is error
        hive.login.assert_not_awaited()
        api_call.assert_awaited_once_with()

    async def test_concurrent_rejections_log_in_once(self):
        hive = _make_hive()

        async def _login():
            # A real login yields to the loop and then replaces the tokens.
            await asyncio.sleep(0)
            hive.update_tokens(AUTH_RESULT)

        hive.login = AsyncMock(side_effect=_login)
        first = AsyncMock(side_effect=[HiveReauthRequired("HTTP 401"), "a"])
        second = AsyncMock(side_effect=[HiveReauthRequired("HTTP 401"), "b"])
        results = await asyncio.gather(hive._api_call(first), hive._api_call(second))
        assert results == ["a", "b"]
        hive.login.assert_awaited_once_with()
        assert first.await_count == second.await_count == 2


class TestSetState:
    async def test_file_mode_sends_nothing(self):
        hive = _make_hive()
        hive.config.file = True
        _expire_tokens(hive)
        assert await hive.set_state("heating", "p1", target=20.5) is None
        hive.api.set_state.assert_not_awaited()
        hive.auth.refresh_token.assert_not_awaited()

    async def test_sends_the_state_to_the_api(self):
        hive = _make_hive()
        result = await hive.set_state("heating", "p1", target=20.5, mode="MANUAL")
        assert result is None
        hive.api.set_state.assert_awaited_once_with(
            "heating", "p1", target=20.5, mode="MANUAL"
        )
        hive.auth.refresh_token.assert_not_awaited()

    async def test_rejected_token_logs_in_and_retries(self):
        hive = _make_hive()
        hive.api.set_state.side_effect = [HiveReauthRequired("HTTP 401"), None]
        hive.auth.login.return_value = AUTH_RESULT
        await hive.set_state("heating", "p1", target=21.0)
        hive.auth.login.assert_awaited_once_with()
        assert hive.api.set_state.await_args_list == [
            call("heating", "p1", target=21.0),
            call("heating", "p1", target=21.0),
        ]
        assert hive.tokens.token_data == NEW_TOKENS


class TestPoll:
    async def test_file_mode_loads_the_bundled_fixture(self):
        hive = Hive(username="use@file.com", password="")
        hive.config.file = True
        hive.api.get_all = AsyncMock()
        hive.config.last_update = _LAST_POLL
        await hive._poll()
        fixture = hive.open_file("data.json")
        assert hive.data.user == fixture["user"]
        assert hive.data.products == {p["id"]: p for p in fixture["products"]}
        assert hive.data.devices == {d["id"]: d for d in fixture["devices"]}
        hive.api.get_all.assert_not_awaited()
        assert _is_about(hive.config.last_update, datetime.now(UTC))

    async def test_file_mode_reads_off_the_event_loop(self):
        hive = _make_hive()
        hive.config.file = True
        with patch(
            "apyhiveapi.hive.asyncio.to_thread", new=AsyncMock(return_value=_nodes())
        ) as to_thread:
            await hive._poll()
        to_thread.assert_awaited_once_with(hive.open_file, "data.json")

    async def test_api_mode_fetches_and_stores_all_nodes(self):
        hive = _make_hive()
        hive.config.last_update = _LAST_POLL
        with patch.object(hive, "open_file") as open_file:
            await hive._poll()
        open_file.assert_not_called()
        hive.api.get_all.assert_awaited_once_with()
        nodes = _nodes()
        assert hive.data.user == nodes["user"]
        assert hive.data.products == {"p1": nodes["products"][0]}
        assert hive.data.devices == {"d1": nodes["devices"][0]}
        assert _is_about(hive.config.last_update, datetime.now(UTC))

    async def test_api_mode_logs_in_again_when_the_token_is_rejected(self):
        hive = _make_hive()
        hive.api.get_all.side_effect = [HiveReauthRequired("HTTP 401"), _nodes()]
        hive.auth.login.return_value = AUTH_RESULT
        await hive._poll()
        hive.auth.login.assert_awaited_once_with()
        assert hive.api.get_all.await_count == 2
        assert hive.tokens.token_data == NEW_TOKENS
        assert hive.data.user == {"id": "u1", "temperatureUnit": "C"}


class TestGetDevices:
    async def test_returns_true_after_a_successful_poll(self, caplog):
        hive = _make_hive()
        hive.config.last_update = _LAST_POLL
        assert await hive.get_devices() is True
        hive.api.get_all.assert_awaited_once_with()
        assert hive.data.user == {"id": "u1", "temperatureUnit": "C"}
        assert _is_about(hive.config.last_update, datetime.now(UTC))
        assert _warnings(caplog) == []

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(
                HiveConnectionError("Cannot connect to Hive"), id="connection-error"
            ),
            pytest.param(HiveApiError("Hive refused the request"), id="api-error"),
            pytest.param(HiveDeviceOffline("offline"), id="device-offline"),
            pytest.param(HiveError("something else"), id="other-hive-error"),
        ],
    )
    async def test_hive_errors_keep_the_last_state_and_schedule_a_retry(
        self, caplog, error
    ):
        hive = _make_hive()
        hive.data.user = {"id": "previous"}
        hive.config.last_update = _LAST_POLL
        hive.api.get_all.side_effect = error
        assert await hive.get_devices() is False
        assert hive.data.user == {"id": "previous"}
        assert _warnings(caplog) == [
            f"Could not poll Hive, keeping the last known state: {error}"
        ]
        expected = datetime.now(UTC) - hive.config.scan_interval + POLL_RETRY_DELAY
        assert _is_about(hive.config.last_update, expected)

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(
                HiveReauthRequired("device not trusted"), id="reauth-required"
            ),
            pytest.param(HiveInvalidUsername("unknown user"), id="invalid-username"),
            pytest.param(HiveInvalidPassword("wrong password"), id="invalid-password"),
            pytest.param(HiveInvalid2FACode("bad code"), id="invalid-2fa-code"),
        ],
    )
    async def test_reauth_required_sets_last_update_and_propagates(self, caplog, error):
        hive = _make_hive()
        hive.data.user = {"id": "previous"}
        hive.config.last_update = _LAST_POLL
        # Hive rejects the token and the login that follows fails.
        hive.api.get_all.side_effect = HiveReauthRequired("HTTP 401")
        hive.auth.login.side_effect = error
        with pytest.raises(HiveReauthRequired) as excinfo:
            await hive.get_devices()
        assert excinfo.value is error
        assert hive.data.user == {"id": "previous"}
        assert _is_about(hive.config.last_update, datetime.now(UTC))
        assert _warnings(caplog) == []

    async def test_unexpected_errors_propagate_untouched(self, caplog):
        hive = _make_hive()
        hive.config.last_update = _LAST_POLL
        hive.api.get_all.side_effect = KeyError("id")
        with pytest.raises(KeyError):
            await hive.get_devices()
        assert hive.config.last_update == _LAST_POLL
        assert _warnings(caplog) == []
