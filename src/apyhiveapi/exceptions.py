"""Exceptions raised by apyhiveapi.

Each class names what the caller should do about it: retry later
(:class:`HiveConnectionError`), give up on this request
(:class:`HiveApiError`, :class:`HiveDeviceOffline`) or have the user log in
again (:class:`HiveReauthRequired` and its subclasses). Errors from aiohttp
and botocore never escape the library: they are translated where they occur
and chained as ``__cause__``.
"""


class HiveError(Exception):
    """Base class for every error this library raises."""


class HiveConnectionError(HiveError):
    """Hive or Cognito could not be reached, or answered with a server error.

    Covers network failures, timeouts, HTTP 5xx responses, Cognito throttling
    and unreadable bodies. The failure is transient: retry later.
    """


class HiveApiError(HiveError):
    """Hive or Cognito refused the request or answered unexpectedly.

    Retrying will not help: the request itself, the account or the response
    is the problem.
    """


class HiveDeviceOffline(HiveError):
    """The device is offline or no longer on the account, so it cannot be controlled."""


class HiveReauthRequired(HiveError):
    """The user has to log in again.

    Raised when Hive rejects the tokens, the refresh token or the saved device
    credentials, when there are no tokens to use, or when Cognito wants an
    SMS code.
    """


class HiveInvalidUsername(HiveReauthRequired):
    """The username is not known to Hive."""


class HiveInvalidPassword(HiveReauthRequired):
    """The password was rejected."""


class HiveInvalid2FACode(HiveReauthRequired):
    """The SMS two-factor code was rejected or has expired."""
