"""Data classes for the Hive session and its devices."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_SCAN_INTERVAL = timedelta(seconds=120)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _epoch() -> datetime:
    return datetime.min.replace(tzinfo=UTC)


@dataclass
class Device:
    """A Home Assistant entity backed by a Hive product or device."""

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

    file: bool = False
    last_update: datetime = field(default_factory=_utcnow)
    scan_interval: timedelta = DEFAULT_SCAN_INTERVAL
    username: str | None = None
