"""Data classes for the Hive session and its devices."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_SCAN_INTERVAL = timedelta(seconds=120)

_SENTINEL = object()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _epoch() -> datetime:
    return datetime.min.replace(tzinfo=UTC)


@dataclass
class Device:
    """A Home Assistant entity backed by a Hive product or device.

    Attributes are also readable dict-style (``device["status"]``), which is
    how the Home Assistant integration consumes them.
    """

    hive_id: str
    hive_name: str
    hive_type: str
    ha_type: str
    device_id: str
    device_name: str
    device_data: dict[str, Any]
    parent_device: str | None = None
    ha_name: str = ""
    temperature_unit: str | None = None
    status: dict[str, Any] | None = None
    min_temp: float | None = None
    max_temp: float | None = None

    def __getitem__(self, key: str) -> Any:  # noqa: ANN401 - attribute values vary
        """Support dict-style read access to the attributes."""
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key) from None

    def __setitem__(self, key: str, value: object) -> None:
        """Support dict-style write access to the attributes."""
        setattr(self, key, value)

    def __contains__(self, key: str) -> bool:
        """Return True if the key names a non-None attribute."""
        val = getattr(self, key, _SENTINEL)
        return val is not _SENTINEL and val is not None

    def get(self, key: str, default: Any = None) -> Any:  # noqa: ANN401 - see __getitem__
        """Return the value for key, or default if missing or None."""
        try:
            val = self[key]
        except KeyError:
            return default
        return val if val is not None else default


@dataclass
class SessionData:
    """The latest products, devices and account details from the Hive API."""

    products: dict[str, Any] = field(default_factory=dict)
    devices: dict[str, Any] = field(default_factory=dict)
    user: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionTokens:
    """The session's API tokens and when they were issued."""

    token_data: dict[str, str] = field(default_factory=dict)
    token_created: datetime = field(default_factory=_epoch)
    token_expiry: timedelta = field(default_factory=lambda: timedelta(seconds=3600))


@dataclass
class SessionConfig:
    """Session configuration state."""

    error_list: dict[str, datetime] = field(default_factory=dict)
    file: bool = False
    home_id: str | None = None
    last_update: datetime = field(default_factory=_utcnow)
    scan_interval: timedelta = DEFAULT_SCAN_INTERVAL
    user_id: str | None = None
    username: str | None = None
