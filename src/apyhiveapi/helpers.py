"""Small helpers shared across the package."""

from __future__ import annotations

import copy
from typing import Any

# Payload keys whose values are masked in logs. A key is masked when its
# lower-cased name contains any of these, so "token" also covers keys such
# as "accessToken" and "tokens".
_SENSITIVE_KEY_PARTS = (
    "password",
    "token",
    "secret",
    "code",
    "session",
    "device_data",
    "authenticationresult",
    "responsemetadata",
    "newdevicemetadata",
)

_MAX_UNMASKED_LENGTH = 8


def _mask(value: Any) -> Any:  # noqa: ANN401 - arbitrary JSON values
    """Mask a sensitive value, recursing into containers."""
    if isinstance(value, str):
        if len(value) <= _MAX_UNMASKED_LENGTH:
            return "***"
        return f"{value[:4]}...{value[-4:]}"
    if isinstance(value, dict):
        return {key: _mask(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask(item) for item in value]
    return value


def _walk(node: Any) -> Any:  # noqa: ANN401 - arbitrary JSON values
    """Mask the sensitive keys found anywhere inside *node*."""
    if isinstance(node, dict):
        return {
            key: _mask(value)
            if any(part in key.lower() for part in _SENSITIVE_KEY_PARTS)
            else _walk(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_walk(item) for item in node]
    return node


def sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of payload with sensitive values masked for logs."""
    result: dict[str, Any] = _walk(copy.deepcopy(payload))
    return result
