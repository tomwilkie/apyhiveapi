"""Unit tests for the exception hierarchy."""

import pytest

import apyhiveapi
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

LEAF_EXCEPTIONS = (
    HiveConnectionError,
    HiveApiError,
    HiveDeviceOffline,
    HiveReauthRequired,
    HiveInvalidUsername,
    HiveInvalidPassword,
    HiveInvalid2FACode,
)


class TestHierarchy:
    @pytest.mark.parametrize("exc_type", LEAF_EXCEPTIONS)
    def test_every_exception_is_a_hive_error(self, exc_type):
        assert issubclass(exc_type, HiveError)
        with pytest.raises(HiveError):
            raise exc_type("test")

    def test_connection_and_api_errors_are_distinct(self):
        # A caller can tell "retry later" from "give up".
        assert not issubclass(HiveConnectionError, HiveApiError)
        assert not issubclass(HiveApiError, HiveConnectionError)

    def test_device_offline_is_neither_transient_nor_reauth(self):
        assert not issubclass(HiveDeviceOffline, HiveConnectionError)
        assert not issubclass(HiveDeviceOffline, HiveReauthRequired)

    @pytest.mark.parametrize(
        "exc_type", [HiveInvalidUsername, HiveInvalidPassword, HiveInvalid2FACode]
    )
    def test_credential_errors_require_reauthentication(self, exc_type):
        assert issubclass(exc_type, HiveReauthRequired)

    def test_reauth_required_is_not_an_api_error(self):
        assert not issubclass(HiveReauthRequired, HiveApiError)


class TestPublicExports:
    @pytest.mark.parametrize("exc_type", [HiveError, *LEAF_EXCEPTIONS])
    def test_exported_from_the_package(self, exc_type):
        assert getattr(apyhiveapi, exc_type.__name__) is exc_type
        assert exc_type.__name__ in apyhiveapi.__all__

    def test_no_other_exceptions_are_exported(self):
        exported = {
            name
            for name in apyhiveapi.__all__
            if isinstance(getattr(apyhiveapi, name), type)
            and issubclass(getattr(apyhiveapi, name), BaseException)
        }
        assert exported == {HiveError.__name__} | {
            exc_type.__name__ for exc_type in LEAF_EXCEPTIONS
        }
