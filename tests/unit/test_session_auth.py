"""Tests for the token lifecycle on Hive: update_tokens, login and refresh."""

# pylint: disable=protected-access

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from botocore.exceptions import ReadTimeoutError

from apyhiveapi.exceptions import (
    HiveApiError,
    HiveFailedToRefreshTokens,
    HiveInvalidDeviceAuthentication,
    HiveInvalidPassword,
    HiveInvalidUsername,
    HiveReauthRequired,
    HiveRefreshTokenExpired,
    HiveUnknownConfiguration,
)
from apyhiveapi.hive import Hive

AUTH_RESULT = {
    "AuthenticationResult": {
        "IdToken": "id-tok",
        "AccessToken": "acc-tok",
        "RefreshToken": "ref-tok",
        "ExpiresIn": 3600,
    }
}
_EPOCH = datetime.min.replace(tzinfo=UTC)
_RETRY_ATTEMPTS = 3


def _make_hive():
    """Return a Hive whose Cognito auth object is mocked."""
    hive = Hive(username="user@example.com", password="pw")
    hive.auth = MagicMock()
    hive.auth.DEVICE_VERIFIER_CHALLENGE = "DEVICE_SRP_AUTH"
    hive.auth.SMS_MFA_CHALLENGE = "SMS_MFA"
    hive.auth.login = AsyncMock()
    hive.auth.device_login = AsyncMock()
    hive.auth.refresh_token = AsyncMock()
    hive.tokens.token_data = {"refreshToken": "rt", "token": "", "accessToken": ""}
    return hive


def _expire_tokens(hive):
    hive.tokens.token_created = datetime.now(UTC) - timedelta(hours=2)
    hive.tokens.token_expiry = timedelta(hours=1)


class TestUpdateTokens:
    """Tests for Hive.update_tokens."""

    async def test_authentication_result_sets_all_tokens(self):
        hive = _make_hive()
        await hive.update_tokens(AUTH_RESULT)
        assert hive.tokens.token_data == {
            "token": "id-tok",
            "accessToken": "acc-tok",
            "refreshToken": "ref-tok",
        }

    async def test_expires_in_updates_token_expiry(self):
        hive = _make_hive()
        await hive.update_tokens(AUTH_RESULT)
        assert hive.tokens.token_expiry == timedelta(seconds=3600)

    async def test_update_expiry_time_default_sets_token_created(self):
        hive = _make_hive()
        await hive.update_tokens(AUTH_RESULT)
        assert hive.tokens.token_created > _EPOCH
        assert hive.tokens.token_created.tzinfo is UTC

    async def test_update_expiry_time_false_leaves_token_created(self):
        hive = _make_hive()
        await hive.update_tokens(AUTH_RESULT, update_expiry_time=False)
        assert hive.tokens.token_created == _EPOCH

    async def test_auth_result_missing_keys_keeps_existing_values(self):
        hive = _make_hive()
        await hive.update_tokens({"AuthenticationResult": {"IdToken": "new-id"}})
        assert hive.tokens.token_data["token"] == "new-id"
        assert hive.tokens.token_data["refreshToken"] == "rt"
        assert hive.tokens.token_data["accessToken"] == ""

    async def test_flat_token_dict_sets_all_keys(self):
        hive = _make_hive()
        await hive.update_tokens(
            {"token": "t", "refreshToken": "r", "accessToken": "a"}
        )
        assert hive.tokens.token_data == {
            "token": "t",
            "refreshToken": "r",
            "accessToken": "a",
        }
        assert hive.tokens.token_created > _EPOCH

    async def test_flat_token_dict_missing_keys_does_not_crash(self):
        hive = _make_hive()
        await hive.update_tokens({"token": "t-only"})
        assert hive.tokens.token_data["token"] == "t-only"
        assert hive.tokens.token_data["accessToken"] == ""

    async def test_flat_dict_with_expires_in_sets_token_expiry(self):
        hive = _make_hive()
        await hive.update_tokens({"token": "t", "ExpiresIn": 1800})
        assert hive.tokens.token_expiry == timedelta(seconds=1800)

    async def test_flat_dict_without_expires_in_keeps_token_expiry(self):
        hive = _make_hive()
        await hive.update_tokens({"token": "t"})
        assert hive.tokens.token_expiry == timedelta(seconds=3600)

    async def test_unknown_shape_changes_nothing(self):
        hive = _make_hive()
        await hive.update_tokens({"some_other_key": "value"})
        assert hive.tokens.token_data["token"] == ""
        assert hive.tokens.token_created == _EPOCH

    async def test_returns_the_token_store(self):
        hive = _make_hive()
        assert await hive.update_tokens(AUTH_RESULT) is hive.tokens


class TestLogin:
    """Tests for Hive.login."""

    async def test_auth_result_updates_tokens_and_returns(self):
        hive = _make_hive()
        hive.auth.login.return_value = AUTH_RESULT
        result = await hive.login()
        assert result is AUTH_RESULT
        assert hive.tokens.token_data["token"] == "id-tok"

    async def test_sms_mfa_challenge_returned_directly(self):
        hive = _make_hive()
        hive.auth.login.return_value = {"ChallengeName": "SMS_MFA"}
        result = await hive.login()
        assert result["ChallengeName"] == "SMS_MFA"
        assert hive.tokens.token_data["token"] == ""

    async def test_unknown_challenge_raises(self):
        hive = _make_hive()
        hive.auth.login.return_value = {"ChallengeName": "TOTALLY_UNKNOWN"}
        with pytest.raises(HiveUnknownConfiguration):
            await hive.login()

    async def test_device_srp_challenge_routes_to_device_login(self):
        hive = _make_hive()
        hive.auth.login.return_value = {"ChallengeName": "DEVICE_SRP_AUTH"}
        hive.auth.device_login.return_value = AUTH_RESULT
        assert await hive.login() is AUTH_RESULT
        hive.auth.device_login.assert_called_once()
        assert hive.tokens.token_data["token"] == "id-tok"

    @pytest.mark.parametrize(
        "error", [HiveInvalidUsername(), HiveInvalidPassword(), HiveApiError()]
    )
    async def test_auth_errors_propagate(self, error):
        hive = _make_hive()
        hive.auth.login.side_effect = error
        with pytest.raises(type(error)):
            await hive.login()


class TestHandleDeviceLoginChallenge:
    """Tests for Hive._handle_device_login_challenge."""

    async def test_sms_mfa_response_raises_reauth(self):
        hive = _make_hive()
        hive.auth.device_login.return_value = {"ChallengeName": "SMS_MFA"}
        with pytest.raises(HiveReauthRequired):
            await hive._handle_device_login_challenge()

    async def test_result_without_auth_result_is_returned_unchanged(self):
        hive = _make_hive()
        hive.auth.device_login.return_value = {"ok": True}
        assert await hive._handle_device_login_challenge() == {"ok": True}
        assert hive.tokens.token_data["token"] == ""


class _NeedsArgsError(Exception):
    """Exception type that cannot be constructed without arguments."""

    def __init__(self, first, second):
        super().__init__(f"{first}/{second}")


class TestRetryWithBackoff:
    """Tests for Hive._retry_with_backoff."""

    async def test_original_exception_instance_propagates(self):
        hive = _make_hive()
        original = _NeedsArgsError("a", "b")

        async def _always_fail():
            raise original

        with pytest.raises(_NeedsArgsError) as excinfo:
            await hive._retry_with_backoff(_always_fail, delays=(0,))
        assert excinfo.value is original

    async def test_empty_delays_raises_runtime_error(self):
        hive = _make_hive()

        async def _never_called():
            raise AssertionError("should not run")

        with pytest.raises(RuntimeError, match="exhausted"):
            await hive._retry_with_backoff(_never_called, delays=())

    async def test_reraise_as_translates_exception_type(self):
        hive = _make_hive()

        async def _always_fail():
            raise ValueError("boom")

        with pytest.raises(HiveReauthRequired) as excinfo:
            await hive._retry_with_backoff(
                _always_fail, delays=(0,), reraise_as=HiveReauthRequired
            )
        assert isinstance(excinfo.value.__cause__, ValueError)

    async def test_pass_through_errors_are_not_retried(self):
        hive = _make_hive()
        calls = []

        async def _fail():
            calls.append(1)
            raise HiveReauthRequired

        with pytest.raises(HiveReauthRequired):
            await hive._retry_with_backoff(
                _fail, delays=(0, 0), pass_through=(HiveReauthRequired,)
            )
        assert len(calls) == 1

    async def test_non_zero_delay_sleeps_before_the_attempt(self):
        hive = _make_hive()

        async def factory():
            return "ok"

        with patch("apyhiveapi.hive.asyncio.sleep", new_callable=AsyncMock) as sleep:
            assert await hive._retry_with_backoff(factory, delays=(5,)) == "ok"
        sleep.assert_called_once_with(5)

    async def test_zero_delay_does_not_sleep(self):
        hive = _make_hive()

        async def factory():
            return "done"

        with patch("apyhiveapi.hive.asyncio.sleep", new_callable=AsyncMock) as sleep:
            assert await hive._retry_with_backoff(factory, delays=(0,)) == "done"
        sleep.assert_not_called()


class TestRetryLogin:
    """Tests for Hive._retry_login."""

    async def test_successful_login_completes(self):
        hive = _make_hive()
        hive.auth.login.return_value = AUTH_RESULT
        await hive._retry_login()
        assert hive.tokens.token_data["token"] == "id-tok"

    async def test_sms_challenge_raises_reauth_without_retry(self):
        hive = _make_hive()
        hive.auth.login.return_value = {"ChallengeName": "SMS_MFA"}
        with pytest.raises(HiveReauthRequired):
            await hive._retry_login()
        assert hive.auth.login.call_count == 1

    @pytest.mark.parametrize(
        "error",
        [
            HiveInvalidUsername(),
            HiveInvalidPassword(),
            HiveInvalidDeviceAuthentication(),
        ],
    )
    async def test_credential_errors_become_reauth_without_retry(self, error):
        hive = _make_hive()
        hive.auth.login.side_effect = error
        with pytest.raises(HiveReauthRequired):
            await hive._retry_login()
        assert hive.auth.login.call_count == 1

    @pytest.mark.parametrize(
        "error",
        [
            HiveApiError(),
            ReadTimeoutError(endpoint_url="https://cognito-idp"),
            aiohttp.ClientConnectionError("Cannot connect"),
            KeyError("ChallengeName"),
            HiveUnknownConfiguration(),
        ],
    )
    async def test_other_failures_are_retried_then_reauth(self, error):
        hive = _make_hive()
        hive.auth.login.side_effect = error
        with (
            patch("asyncio.sleep", AsyncMock()),
            pytest.raises(HiveReauthRequired),
        ):
            await hive._retry_login()
        assert hive.auth.login.call_count == _RETRY_ATTEMPTS

    async def test_transient_failure_recovers_on_retry(self):
        hive = _make_hive()
        hive.auth.login.side_effect = [
            aiohttp.ClientConnectionError("Cannot connect"),
            AUTH_RESULT,
        ]
        with patch("asyncio.sleep", AsyncMock()):
            await hive._retry_login()
        assert hive.auth.login.call_count == 2


class TestHiveRefreshTokens:
    """Tests for Hive.hive_refresh_tokens."""

    async def test_file_mode_skips_refresh_entirely(self):
        hive = _make_hive()
        hive.config.file = True
        _expire_tokens(hive)
        assert await hive.hive_refresh_tokens() is None
        hive.auth.refresh_token.assert_not_called()

    async def test_fresh_token_returns_none_without_refreshing(self):
        hive = _make_hive()
        hive.tokens.token_created = datetime.now(UTC)
        assert await hive.hive_refresh_tokens() is None
        hive.auth.refresh_token.assert_not_called()

    async def test_expired_token_is_refreshed(self):
        hive = _make_hive()
        _expire_tokens(hive)
        hive.auth.refresh_token.return_value = AUTH_RESULT
        assert await hive.hive_refresh_tokens() is AUTH_RESULT
        hive.auth.refresh_token.assert_called_once_with("rt")
        assert hive.tokens.token_data["token"] == "id-tok"

    async def test_force_refresh_refreshes_a_fresh_token(self):
        hive = _make_hive()
        hive.tokens.token_created = datetime.now(UTC)
        hive.auth.refresh_token.return_value = AUTH_RESULT
        await hive.hive_refresh_tokens(force_refresh=True)
        hive.auth.refresh_token.assert_called_once()

    async def test_recheck_inside_lock_skips_refresh(self):
        """Another caller refreshed while we waited for the lock."""
        hive = _make_hive()
        _expire_tokens(hive)
        await hive._refresh_lock.acquire()

        async def _refresh_then_release():
            await asyncio.sleep(0)
            hive.tokens.token_created = datetime.now(UTC)
            hive._refresh_lock.release()

        release_task = asyncio.create_task(_refresh_then_release())
        result = await hive.hive_refresh_tokens()
        await release_task
        assert result is None
        hive.auth.refresh_token.assert_not_called()

    @pytest.mark.parametrize(
        "error", [HiveRefreshTokenExpired(), HiveFailedToRefreshTokens()]
    )
    async def test_rejected_refresh_falls_back_to_login(self, error):
        hive = _make_hive()
        _expire_tokens(hive)
        hive.auth.refresh_token.side_effect = error
        hive._retry_login = AsyncMock()
        assert await hive.hive_refresh_tokens() is None
        hive._retry_login.assert_called_once()

    async def test_rejected_refresh_when_forced_raises_reauth(self):
        hive = _make_hive()
        _expire_tokens(hive)
        hive.auth.refresh_token.side_effect = HiveRefreshTokenExpired()
        with pytest.raises(HiveReauthRequired):
            await hive.hive_refresh_tokens(force_refresh=True)

    async def test_api_error_during_refresh_propagates(self):
        hive = _make_hive()
        _expire_tokens(hive)
        hive.auth.refresh_token.side_effect = HiveApiError()
        with pytest.raises(HiveApiError):
            await hive.hive_refresh_tokens()

    async def test_result_without_auth_result_leaves_tokens_unchanged(self):
        hive = _make_hive()
        _expire_tokens(hive)
        hive.auth.refresh_token.return_value = {"SomeOtherKey": "value"}
        assert await hive.hive_refresh_tokens() == {"SomeOtherKey": "value"}
        assert hive.tokens.token_data["token"] == ""

    async def test_missing_refresh_token_is_passed_as_none(self):
        hive = _make_hive()
        hive.tokens.token_data = {"token": "id", "accessToken": "acc"}
        _expire_tokens(hive)
        hive.auth.refresh_token.return_value = None
        assert await hive.hive_refresh_tokens() is None
        hive.auth.refresh_token.assert_called_once_with(None)
