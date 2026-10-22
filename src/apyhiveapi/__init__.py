"""Async Python library for the Hive smart home API."""

from apyhiveapi.auth import HiveAuth as Auth
from apyhiveapi.const import SMS_REQUIRED
from apyhiveapi.exceptions import (
    HiveApiError,
    HiveAuthCredentialError,
    HiveAuthError,
    HiveConfigurationError,
    HiveConnectionError,
    HiveError,
    HiveFailedToRefreshTokens,
    HiveInvalid2FACode,
    HiveInvalidDeviceAuthentication,
    HiveInvalidPassword,
    HiveInvalidUsername,
    HiveReauthRequired,
    HiveRefreshTokenExpired,
    HiveUnknownConfiguration,
)
from apyhiveapi.hive import Hive
from apyhiveapi.models import Device

__all__ = [
    "SMS_REQUIRED",
    "Auth",
    "Device",
    "Hive",
    "HiveApiError",
    "HiveAuthCredentialError",
    "HiveAuthError",
    "HiveConfigurationError",
    "HiveConnectionError",
    "HiveError",
    "HiveFailedToRefreshTokens",
    "HiveInvalid2FACode",
    "HiveInvalidDeviceAuthentication",
    "HiveInvalidPassword",
    "HiveInvalidUsername",
    "HiveReauthRequired",
    "HiveRefreshTokenExpired",
    "HiveUnknownConfiguration",
]
