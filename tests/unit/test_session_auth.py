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
        assert isinstance(hive.api, HiveApi)
        assert hive.api.session is hive
        assert hive.tokens == SessionTokens()
        assert hive.config.username == "user@example.com"
        assert hive.config.file is False
        assert hive.data == SessionData()
        assert isinstance(hive.heating, Climate)
        assert isinstance(hive.hotwater, WaterHeater)

    def test_has_separate_update_and_refresh_locks(self):
        hive = Hive(username="user@example.com", password="pw")
        assert isinstance(hive.update_lock, asyncio.Lock)
        assert isinstance(hive._refresh_lock, asyncio.Lock)
        assert hive._refresh_lock is not hive.update_lock
        assert hive._offline_devices == set()

    def test_passes_the_websession_to_auth_and_api(self):
        websession = MagicMock()
        hive = Hive(websession, username="user@example.com", password="pw")
        assert hive.api.websession is websession
        assert hive.auth._websession is websession

    def test_without_a_websession_none_is_created_yet(self):
        hive = Hive(username="user@example.com", password="pw")
        assert hive.api.websession is None
        assert hive.auth._websession is None

    def test_missing_credentials_default_to_empty_strings(self):
        hive = Hive()
        assert hive.auth.username == ""
        assert hive.auth.password == ""
        assert hive.config.username is None


class TestUpdateTokens:
    def test_returns_none(self):
        hive = _make_hive()
        assert hive.update_tokens(AUTH_RESULT) is None

    def test_cognito_result_maps_the_token_names(self):
        hive = _make_hive()
        hive.update_tokens(AUTH_RESULT)
        assert hive.tokens.token_data == NEW_TOKENS

    def test_cognito_result_sets_token_expiry(self):
        hive = _make_hive()
        hive.update_tokens({"AuthenticationResult": {"ExpiresIn": 1800}})
        assert hive.tokens.token_expiry == timedelta(seconds=1800)

    def test_sets_token_created_to_now_by_default(self):
        hive = _make_hive()
        hive.tokens.token_created = _LAST_POLL
        hive.update_tokens(AUTH_RESULT)
        assert hive.tokens.token_created.tzinfo is UTC
        assert _is_about(hive.tokens.token_created, datetime.now(UTC))

    def test_update_expiry_time_false_leaves_token_created(self):
        hive = _make_hive()
        hive.tokens.token_created = _LAST_POLL
        hive.update_tokens(AUTH_RESULT, update_expiry_time=False)
        assert hive.tokens.token_created == _LAST_POLL
        assert hive.tokens.token_data == NEW_TOKENS

    def test_cognito_result_missing_keys_are_not_written(self):
        hive = _make_hive()
        hive.update_tokens({"AuthenticationResult": {"IdToken": "new-id"}})
        assert hive.tokens.token_data == {**OLD_TOKENS, "token": "new-id"}
        assert hive.tokens.token_expiry == timedelta(seconds=3600)

    def test_empty_authentication_result_stores_nothing(self):
        hive = _make_hive()
        hive.update_tokens({"AuthenticationResult": None})
        assert hive.tokens.token_data == OLD_TOKENS

    def test_flat_dict_is_stored_as_is(self):
        hive = _make_hive()
        hive.tokens.token_created = _LAST_POLL
        hive.update_tokens(dict(NEW_TOKENS))
        assert hive.tokens.token_data == NEW_TOKENS
        assert _is_about(hive.tokens.token_created, datetime.now(UTC))

    def test_flat_dict_missing_keys_are_not_written(self):
        hive = _make_hive()
        hive.update_tokens({"token": "t-only"})
        assert hive.tokens.token_data == {**OLD_TOKENS, "token": "t-only"}

    def test_flat_dict_expires_in_sets_token_expiry(self):
        hive = _make_hive()
        hive.update_tokens({"token": "t", "ExpiresIn": 1800})
        assert hive.tokens.token_expiry == timedelta(seconds=1800)

    def test_without_expires_in_token_expiry_is_kept(self):
        hive = _make_hive()
        hive.tokens.token_expiry = timedelta(seconds=900)
        hive.update_tokens({"token": "t"})
        assert hive.tokens.token_expiry == timedelta(seconds=900)

    def test_unknown_shape_changes_nothing(self):
        hive = _make_hive()
        hive.tokens.token_created = _LAST_POLL
        hive.tokens.token_expiry = timedelta(seconds=900)
        hive.update_tokens({"ChallengeName": "SMS_MFA", "Session": "abc"})
        assert hive.tokens.token_data == OLD_TOKENS
        assert hive.tokens.token_created == _LAST_POLL
        assert hive.tokens.token_expiry == timedelta(seconds=900)

    def test_debug_log_masks_the_tokens(self, caplog):
        hive = _make_hive()
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            hive.update_tokens(AUTH_RESULT)
        assert "update_tokens" in caplog.text
        for token in NEW_TOKENS.values():
            assert token not in caplog.text


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

    async def test_sms_challenge_raises_reauth_required(self):
        hive = _make_hive()
        hive.auth.login.return_value = SMS_CHALLENGE
        with pytest.raises(HiveReauthRequired, match="SMS"):
            await hive.login()
        hive.auth.device_login.assert_not_awaited()
        assert hive.tokens.token_data == OLD_TOKENS

    async def test_sms_challenge_after_device_login_raises_reauth_required(self):
        hive = _make_hive()
        hive.auth.login.return_value = DEVICE_CHALLENGE
        hive.auth.device_login.return_value = SMS_CHALLENGE
        with pytest.raises(HiveReauthRequired):
            await hive.login()
        assert hive.tokens.token_data == OLD_TOKENS

    async def test_unanswerable_challenge_raises_api_error(self):
        hive = _make_hive()
        hive.auth.login.return_value = {"ChallengeName": "NEW_PASSWORD_REQUIRED"}
        with pytest.raises(HiveApiError, match="NEW_PASSWORD_REQUIRED"):
            await hive.login()
        assert hive.tokens.token_data == OLD_TOKENS

    async def test_result_without_tokens_raises_api_error(self):
        hive = _make_hive()
        hive.auth.login.return_value = {}
        with pytest.raises(HiveApiError):
            await hive.login()
        assert hive.tokens.token_data == OLD_TOKENS

    @pytest.mark.parametrize(
        "error",
        [
            HiveInvalidUsername("unknown user"),
            HiveInvalidPassword("wrong password"),
            HiveReauthRequired("device no longer trusted"),
            HiveConnectionError("Cognito timed out"),
            HiveApiError("Cognito refused"),
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


class TestRefreshDue:
    def test_due_for_tokens_created_long_ago(self):
        hive = _make_hive()
        _expire_tokens(hive)
        assert hive._refresh_due() is True

    def test_not_due_for_fresh_tokens(self):
        hive = _make_hive()
        assert hive._refresh_due() is False

    def test_not_due_for_tokens_created_in_the_future(self):
        hive = _make_hive()
        hive.tokens.token_created = datetime.now(UTC) + timedelta(hours=1)
        assert hive._refresh_due() is False

    def test_due_once_the_threshold_share_of_the_lifetime_has_passed(self):
        hive = _make_hive()
        hive.tokens.token_expiry = timedelta(seconds=1000)
        threshold = timedelta(seconds=1000 * REFRESH_THRESHOLD)
        hive.tokens.token_created = datetime.now(UTC) - threshold + _TOLERANCE
        assert hive._refresh_due() is False
        hive.tokens.token_created = datetime.now(UTC) - threshold - _TOLERANCE
        assert hive._refresh_due() is True

    def test_threshold_is_ninety_percent(self):
        assert REFRESH_THRESHOLD == 0.90

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

    async def test_missing_refresh_token_is_passed_as_none(self):
        hive = _make_hive()
        hive.tokens.token_data = {"token": "id-tok", "accessToken": "acc-tok"}
        _expire_tokens(hive)
        hive.auth.refresh_token.return_value = AUTH_RESULT
        await hive.hive_refresh_tokens()
        hive.auth.refresh_token.assert_awaited_once_with(None)

    async def test_rejected_refresh_logs_in_again(self, caplog):
        hive = _make_hive()
        _expire_tokens(hive)
        hive.auth.refresh_token.side_effect = HiveReauthRequired("refresh expired")
        hive.login = AsyncMock()
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            assert await hive.hive_refresh_tokens() is None
        hive.login.assert_awaited_once_with()
        assert hive.tokens.token_data == OLD_TOKENS
        assert "Token refresh rejected (refresh expired)" in caplog.text

    async def test_rejected_refresh_then_login_stores_the_new_tokens(self):
        hive = _make_hive()
        _expire_tokens(hive)
        hive.auth.refresh_token.side_effect = HiveReauthRequired("refresh expired")
        hive.auth.login.return_value = AUTH_RESULT
        await hive.hive_refresh_tokens()
        hive.auth.login.assert_awaited_once_with()
        assert hive.tokens.token_data == NEW_TOKENS

    async def test_rejected_refresh_and_login_needing_sms_raises_reauth(self):
        hive = _make_hive()
        _expire_tokens(hive)
        hive.auth.refresh_token.side_effect = HiveReauthRequired("refresh expired")
        hive.auth.login.return_value = SMS_CHALLENGE
        with pytest.raises(HiveReauthRequired, match="SMS"):
            await hive.hive_refresh_tokens()

    async def test_rejected_refresh_and_failed_login_propagates(self):
        hive = _make_hive()
        _expire_tokens(hive)
        hive.auth.refresh_token.side_effect = HiveReauthRequired("refresh expired")
        hive.login = AsyncMock(side_effect=HiveInvalidPassword("wrong password"))
        with pytest.raises(HiveInvalidPassword):
            await hive.hive_refresh_tokens()

    @pytest.mark.parametrize(
        "error",
        [HiveConnectionError("Cognito timed out"), HiveApiError("Cognito refused")],
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

    async def test_recheck_under_the_lock_skips_the_refresh(self):
        hive = _make_hive()
        _expire_tokens(hive)
        await hive._refresh_lock.acquire()

        async def _refresh_elsewhere_then_release():
            await asyncio.sleep(0)
            hive.update_tokens(AUTH_RESULT)
            hive._refresh_lock.release()

        task = asyncio.create_task(_refresh_elsewhere_then_release())
        await hive.hive_refresh_tokens()
        await task
        hive.auth.refresh_token.assert_not_awaited()
        assert hive.tokens.token_data == NEW_TOKENS


class TestApiCall:
    async def test_refreshes_the_tokens_before_the_call(self):
        hive = _make_hive()
        order = []

        async def _refresh():
            order.append("refresh")

        async def _call():
            order.append("call")
            return "result"

        hive.hive_refresh_tokens = _refresh
        assert await hive._api_call(_call) == "result"
        assert order == ["refresh", "call"]

    async def test_due_tokens_are_refreshed_through_cognito(self):
        hive = _make_hive()
        _expire_tokens(hive)
        hive.auth.refresh_token.return_value = AUTH_RESULT
        assert await hive._api_call(AsyncMock(return_value={"ok": True})) == {
            "ok": True
        }
        hive.auth.refresh_token.assert_awaited_once_with("rt")

    async def test_rejected_token_logs_in_and_retries_once(self, caplog):
        hive = _make_hive()
        hive.login = AsyncMock()
        api_call = AsyncMock(side_effect=[HiveReauthRequired("HTTP 401"), "second"])
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            assert await hive._api_call(api_call) == "second"
        hive.login.assert_awaited_once_with()
        assert api_call.await_count == 2
        assert "Hive rejected the token (HTTP 401)" in caplog.text

    async def test_login_through_cognito_then_retry(self):
        hive = _make_hive()
        hive.auth.login.return_value = AUTH_RESULT
        api_call = AsyncMock(side_effect=[HiveReauthRequired("HTTP 401"), "second"])
        assert await hive._api_call(api_call) == "second"
        hive.auth.login.assert_awaited_once_with()
        assert hive.tokens.token_data == NEW_TOKENS

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
        [HiveConnectionError("Cognito timed out"), HiveReauthRequired("expired")],
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
            HiveConnectionError("Hive timed out"),
            HiveApiError("Hive refused"),
            HiveDeviceOffline("offline"),
        ],
    )
    async def test_other_hive_errors_propagate_without_a_login(self, error):
        hive = _make_hive()
        hive.login = AsyncMock()
        api_call = AsyncMock(side_effect=error)
        with pytest.raises(type(error)) as excinfo:
            await hive._api_call(api_call)
        assert excinfo.value is error
        hive.login.assert_not_awaited()
        api_call.assert_awaited_once_with()

    async def test_unexpected_errors_propagate_without_a_login(self):
        hive = _make_hive()
        hive.login = AsyncMock()
        api_call = AsyncMock(side_effect=ValueError("boom"))
        with pytest.raises(ValueError, match="boom"):
            await hive._api_call(api_call)
        hive.login.assert_not_awaited()

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

    async def test_a_rejection_after_someone_else_logged_in_just_retries(self):
        hive = _make_hive()
        hive.login = AsyncMock()

        async def _rejected_then_ok():
            if hive.tokens.token_data.get("token") != NEW_TOKENS["token"]:
                # Another caller replaces the tokens while this call is failing.
                hive.update_tokens(AUTH_RESULT)
                raise HiveReauthRequired("HTTP 401")
            return "ok"

        assert await hive._api_call(_rejected_then_ok) == "ok"
        hive.login.assert_not_awaited()


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

    async def test_refreshes_due_tokens_first(self):
        hive = _make_hive()
        _expire_tokens(hive)
        hive.auth.refresh_token.return_value = AUTH_RESULT
        await hive.set_state("hotwater", "p2", mode="ON")
        hive.auth.refresh_token.assert_awaited_once_with("rt")
        hive.api.set_state.assert_awaited_once_with("hotwater", "p2", mode="ON")

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

    async def test_login_needing_sms_raises_reauth(self):
        hive = _make_hive()
        hive.api.set_state.side_effect = HiveReauthRequired("HTTP 401")
        hive.auth.login.return_value = SMS_CHALLENGE
        with pytest.raises(HiveReauthRequired, match="SMS"):
            await hive.set_state("heating", "p1", target=21.0)
        hive.api.set_state.assert_awaited_once()

    @pytest.mark.parametrize(
        "error", [HiveConnectionError("Hive timed out"), HiveApiError("Hive refused")]
    )
    async def test_api_errors_propagate(self, error):
        hive = _make_hive()
        hive.api.set_state.side_effect = error
        with pytest.raises(type(error)) as excinfo:
            await hive.set_state("heating", "p1", target=21.0)
        assert excinfo.value is error
        hive.auth.login.assert_not_awaited()


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

    async def test_file_mode_reads_data_json(self):
        hive = _make_hive()
        hive.config.file = True
        with patch.object(hive, "open_file", return_value=_nodes()) as open_file:
            await hive._poll()
        open_file.assert_called_once_with("data.json")
        assert hive.data.user == {"id": "u1", "temperatureUnit": "C"}
        hive.api.get_all.assert_not_awaited()

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

    async def test_api_mode_refreshes_due_tokens_first(self):
        hive = _make_hive()
        _expire_tokens(hive)
        hive.auth.refresh_token.return_value = AUTH_RESULT
        await hive._poll()
        hive.auth.refresh_token.assert_awaited_once_with("rt")
        hive.api.get_all.assert_awaited_once_with()
        assert hive.tokens.token_data == NEW_TOKENS

    async def test_api_mode_logs_in_again_when_the_token_is_rejected(self):
        hive = _make_hive()
        hive.api.get_all.side_effect = [HiveReauthRequired("HTTP 401"), _nodes()]
        hive.auth.login.return_value = AUTH_RESULT
        await hive._poll()
        hive.auth.login.assert_awaited_once_with()
        assert hive.api.get_all.await_count == 2
        assert hive.tokens.token_data == NEW_TOKENS
        assert hive.data.user == {"id": "u1", "temperatureUnit": "C"}

    async def test_failed_poll_leaves_the_data_and_last_update_alone(self):
        hive = _make_hive()
        hive.data.user = {"id": "previous"}
        hive.config.last_update = _LAST_POLL
        hive.api.get_all.side_effect = HiveConnectionError("Hive timed out")
        with pytest.raises(HiveConnectionError):
            await hive._poll()
        assert hive.data.user == {"id": "previous"}
        assert hive.config.last_update == _LAST_POLL


class TestGetDevices:
    async def test_returns_true_after_a_successful_poll(self, caplog):
        hive = _make_hive()
        hive._poll = AsyncMock()
        assert await hive.get_devices() is True
        hive._poll.assert_awaited_once_with()
        assert _warnings(caplog) == []

    async def test_polls_the_api_and_stores_the_nodes(self):
        hive = _make_hive()
        hive.config.last_update = _LAST_POLL
        assert await hive.get_devices() is True
        hive.api.get_all.assert_awaited_once_with()
        assert hive.data.user == {"id": "u1", "temperatureUnit": "C"}
        assert _is_about(hive.config.last_update, datetime.now(UTC))

    @pytest.mark.parametrize(
        "error",
        [
            HiveConnectionError("Cannot connect to Hive"),
            HiveApiError("Hive refused the request"),
            HiveDeviceOffline("offline"),
            HiveError("something else"),
        ],
    )
    async def test_hive_errors_keep_the_last_state_and_schedule_a_retry(
        self, caplog, error
    ):
        hive = _make_hive()
        hive.data.user = {"id": "previous"}
        hive.config.last_update = _LAST_POLL
        hive._poll = AsyncMock(side_effect=error)
        assert await hive.get_devices() is False
        assert hive.data.user == {"id": "previous"}
        assert _warnings(caplog) == [
            f"Could not poll Hive, keeping the last known state: {error}"
        ]
        expected = datetime.now(UTC) - hive.config.scan_interval + POLL_RETRY_DELAY
        assert _is_about(hive.config.last_update, expected)

    async def test_retry_is_sooner_than_a_full_scan_interval(self):
        hive = _make_hive()
        hive._poll = AsyncMock(side_effect=HiveConnectionError("Cannot connect"))
        await hive.get_devices()
        next_poll = hive.config.last_update + hive.config.scan_interval
        assert hive.config.scan_interval > POLL_RETRY_DELAY
        assert _is_about(next_poll, datetime.now(UTC) + timedelta(seconds=30))

    async def test_failed_api_poll_returns_false(self, caplog):
        hive = _make_hive()
        hive.api.get_all.side_effect = HiveApiError("Hive refused")
        assert await hive.get_devices() is False
        assert _warnings(caplog) == [
            "Could not poll Hive, keeping the last known state: Hive refused"
        ]

    @pytest.mark.parametrize(
        "error",
        [
            HiveReauthRequired("token rejected"),
            HiveInvalidUsername("unknown user"),
            HiveInvalidPassword("wrong password"),
            HiveInvalid2FACode("bad code"),
        ],
    )
    async def test_reauth_required_sets_last_update_and_propagates(self, caplog, error):
        hive = _make_hive()
        hive.data.user = {"id": "previous"}
        hive.config.last_update = _LAST_POLL
        hive._poll = AsyncMock(side_effect=error)
        with pytest.raises(HiveReauthRequired) as excinfo:
            await hive.get_devices()
        assert excinfo.value is error
        assert hive.data.user == {"id": "previous"}
        assert _is_about(hive.config.last_update, datetime.now(UTC))
        assert _warnings(caplog) == []

    async def test_unexpected_errors_propagate_untouched(self, caplog):
        hive = _make_hive()
        hive.config.last_update = _LAST_POLL
        hive._poll = AsyncMock(side_effect=KeyError("id"))
        with pytest.raises(KeyError):
            await hive.get_devices()
        assert hive.config.last_update == _LAST_POLL
        assert _warnings(caplog) == []
