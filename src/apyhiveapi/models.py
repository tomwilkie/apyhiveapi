"""Data classes for the Hive session."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta


def _epoch() -> datetime:
    return datetime.min.replace(tzinfo=UTC)


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
    username: str | None = None
