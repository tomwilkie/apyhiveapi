"""Unit tests for the exception hierarchy."""

import pytest

import apyhiveapi
from apyhiveapi.exceptions import (
    HiveApiError,
    HiveConnectionError,
    HiveError,
    HiveInvalid2FACode,
    HiveInvalidPassword,
    HiveInvalidUsername,
    HiveReauthRequired,
)

LEAF_EXCEPTIONS = (
    HiveConnectionError,
    HiveApiError,
    HiveReauthRequired,
    HiveInvalidUsername,
    HiveInvalidPassword,
    HiveInvalid2FACode,
)


class TestHierarchy:
    @pytest.mark.parametrize(
        ("exc_type", "parent"),
        [
            (HiveConnectionError, HiveError),
            (HiveApiError, HiveError),
            (HiveReauthRequired, HiveError),
            (HiveInvalidUsername, HiveReauthRequired),
            (HiveInvalidPassword, HiveReauthRequired),
            (HiveInvalid2FACode, HiveReauthRequired),
        ],
    )
    def test_direct_parent(self, exc_type, parent):
        # The fork's except ordering relies on the three categories under
        # HiveError being disjoint (retry later, give up, log in again) and
        # on the credential errors being reauth errors.
        assert exc_type.__bases__ == (parent,)


class TestPublicExports:
    def test_exactly_the_hive_errors_are_exported(self):
        exported = {
            name: getattr(apyhiveapi, name)
            for name in apyhiveapi.__all__
            if isinstance(getattr(apyhiveapi, name), type)
            and issubclass(getattr(apyhiveapi, name), BaseException)
        }
        assert exported == {cls.__name__: cls for cls in (HiveError, *LEAF_EXCEPTIONS)}
