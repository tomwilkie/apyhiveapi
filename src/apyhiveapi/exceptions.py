"""Exceptions raised by apyhiveapi."""


class FileInUse(Exception):
    """The session is in file mode, so the API must not be called."""


class NoApiToken(Exception):
    """No API token is available for the request."""


class HiveError(Exception):
    """Common base class for all Hive-specific exceptions."""


class HiveApiError(HiveError):
    """The Hive API returned an error."""


class HiveAuthError(HiveApiError):
    """Auth error (401/403): the token may be expired or invalid."""


class HiveRefreshTokenExpired(HiveApiError):
    """The refresh token has expired."""


class HiveFailedToRefreshTokens(HiveApiError):
    """The tokens could not be refreshed."""


class HiveConnectionError(HiveApiError):
    """Hive could not be reached (timeout or network error).

    The failure is expected to be transient, so callers can retry.
    """


class HiveConfigurationError(HiveError):
    """Base class for configuration-related errors."""


class HiveUnknownConfiguration(HiveConfigurationError):
    """The session configuration is incomplete or unknown."""


class HiveInvalidDeviceAuthentication(HiveConfigurationError):
    """The saved device credentials were rejected."""


class HiveAuthCredentialError(HiveError):
    """Base class for authentication credential errors."""


class HiveInvalidUsername(HiveAuthCredentialError):
    """The username is not known to Hive."""


class HiveInvalidPassword(HiveAuthCredentialError):
    """The password was rejected."""


class HiveInvalid2FACode(HiveAuthCredentialError):
    """The SMS two-factor code was rejected."""


class HiveReauthRequired(HiveError):
    """The user must log in again."""
