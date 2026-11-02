"""HTTP calls to the Hive REST API.

Every aiohttp error is translated here into one of the library's exceptions,
so the rest of the package only ever sees :class:`~hiveheating.exceptions.HiveError`
subclasses.
"""

from __future__ import annotations

import json
import logging
import time
from http import HTTPStatus
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from hiveheating.exceptions import HiveApiError, HiveConnectionError, HiveReauthRequired

_LOGGER = logging.getLogger(__name__)

# Per-request budget. The total has to sit above Hive's slow days, which can
# reach several seconds for the all-nodes response, and below the scan
# interval; the connect limit makes an unreachable Hive fail fast.
_TIMEOUT = ClientTimeout(total=20, connect=10)
_BASE_URL = "https://beekeeper.hivehome.com/1.0"
_HEADERS = {
    "content-type": "application/json",
    "Accept": "*/*",
    "User-Agent": "Hive/12.04.0 iOS/18.3.1 Apple",
}


async def _fetch(
    websession: ClientSession,
    method: str,
    url: str,
    *,
    token: str | None = None,
    data: str | None = None,
) -> str:
    """Make one HTTP request and return the body of a 2xx response.

    Args:
        websession: The aiohttp session to use.
        method: HTTP method.
        url: URL to call.
        token: API token to send. When one is sent, an HTTP 401 means Hive
            rejected it.
        data: Request body.

    Raises:
        HiveReauthRequired: Hive rejected the token (HTTP 401 on a request
            that sent one).
        HiveConnectionError: Network error, timeout, HTTP 5xx, or HTTP 403
            ``ForbiddenException``: Hive's API gateway blocks every request
            from a connection for a minute or two after a burst of about 25.
        HiveApiError: Any other non-2xx status, including any other HTTP
            403: Hive's API gateway answers a bad token with 401, and a 403
            means it refused the request itself (for example an unknown path).
    """
    headers = {**_HEADERS, "Authorization": token} if token else None
    start = time.monotonic()
    try:
        async with websession.request(
            method, url, headers=headers, data=data, timeout=_TIMEOUT
        ) as resp:
            body = await resp.text()
            status = resp.status
            error_type = resp.headers.get("x-amzn-ErrorType")
    except TimeoutError as err:
        raise HiveConnectionError(f"Timed out calling {url}") from err
    except (ClientError, OSError) as err:
        raise HiveConnectionError(f"Could not reach {url}: {err}") from err
    _LOGGER.debug(
        "%s %s answered HTTP %s in %.2fs",
        method.upper(),
        url,
        status,
        time.monotonic() - start,
    )
    if HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
        return body
    # API Gateway names the kind of refusal, e.g. UnauthorizedException or
    # ForbiddenException, sometimes followed by ":<documentation URL>".
    error_name = error_type.split(":")[0] if error_type else None
    kind = f" ({error_name})" if error_name else ""
    detail = f"HTTP {status}{kind} calling {url}: {body[:200]}"
    if token and status == HTTPStatus.UNAUTHORIZED:
        raise HiveReauthRequired(f"Hive rejected the token: {detail}")
    if status == HTTPStatus.FORBIDDEN and error_name == "ForbiddenException":
        raise HiveConnectionError(
            f"Hive is temporarily blocking requests, try again in a few minutes: "
            f"{detail}"
        )
    if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        raise HiveConnectionError(f"Hive server error: {detail}")
    raise HiveApiError(f"Hive refused the request: {detail}")


async def request(
    websession: ClientSession,
    method: str,
    path: str,
    *,
    token: str | None,
    data: str | None = None,
) -> dict[str, Any]:
    """Make an authenticated Hive API request and return its JSON body.

    Args:
        websession: The aiohttp session to use.
        method: HTTP method.
        path: Path under the API's base URL, e.g. ``/nodes/all``.
        token: The session's API token.
        data: Request body.

    Returns:
        The decoded body, or ``{}`` when the response has none.

    Raises:
        HiveReauthRequired: There is no token, or Hive rejected it.
        HiveConnectionError: Hive could not be reached, failed, or sent an
            unreadable body.
        HiveApiError: Hive refused the request, or the body is not a JSON
            object.
    """
    if not token:
        raise HiveReauthRequired("The session has no API token")
    url = f"{_BASE_URL}{path}"
    body = await _fetch(websession, method, url, token=token, data=data)
    if not body.strip():
        return {}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as err:
        raise HiveConnectionError(
            f"Hive returned an unreadable body calling {url}"
        ) from err
    if not isinstance(parsed, dict):
        raise HiveApiError(
            f"Hive returned a JSON {type(parsed).__name__} calling {url}"
        )
    return parsed
