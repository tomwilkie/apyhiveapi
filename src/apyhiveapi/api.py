"""HTTP client for the Hive API and the SSO login page.

Every aiohttp error is translated here into one of the library's exceptions,
so the rest of the package only ever sees :class:`~apyhiveapi.exceptions.HiveError`
subclasses.
"""

from __future__ import annotations

import json
import logging
import re
import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from apyhiveapi.exceptions import HiveApiError, HiveConnectionError, HiveReauthRequired

if TYPE_CHECKING:
    from apyhiveapi.hive import Hive

_LOGGER = logging.getLogger(__name__)

# Per-request budget. The total has to sit above Hive's slow days, which can
# reach several seconds for the all-nodes response, and below the scan
# interval; the connect limit makes an unreachable Hive fail fast.
_TIMEOUT = ClientTimeout(total=20, connect=10)
_SSO_URL = "https://sso.hivehome.com/"
_BASE_URL = "https://beekeeper.hivehome.com/1.0"
_ALL_NODES_URL = f"{_BASE_URL}/nodes/all?products=true&devices=true"
_HEADERS = {
    "content-type": "application/json",
    "Accept": "*/*",
    "User-Agent": "Hive/12.04.0 iOS/18.3.1 Apple",
}

# The SSO bootstrap page embeds its Cognito config in its first <script>
# block as `window.<key> = <value>` assignments. The value quoting changed
# from double to single quotes on 2026-10-20, so accept either (and require
# the closing quote to match).
_FIRST_SCRIPT = re.compile(r"<script[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)
_SSO_ASSIGNMENT = re.compile(r"""window\.(\w+)\s*=\s*(["'])(.*?)\2""")
_SSO_KEYS = {
    "UPID": "HiveSSOPoolId",
    "CLIID": "HiveSSOPublicCognitoClientId",
    "REGION": "HiveSSOPoolId",
}

type JsonValue = str | int | float | bool | None


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


async def get_login_info(websession: ClientSession | None = None) -> dict[str, str]:
    """Fetch the Cognito pool details embedded in the Hive SSO login page.

    Args:
        websession: aiohttp session to use. A short-lived one is created when
            none is given.

    Returns:
        ``UPID``, ``CLIID`` and ``REGION``.

    Raises:
        HiveConnectionError: The page could not be fetched.
        HiveApiError: The page does not contain the Cognito details.
    """
    if websession is None:
        async with ClientSession() as own_session:
            page = await _fetch(own_session, "get", _SSO_URL)
    else:
        page = await _fetch(websession, "get", _SSO_URL)
    script = _FIRST_SCRIPT.search(page)
    values = {
        key: value
        for key, _quote, value in _SSO_ASSIGNMENT.findall(
            script.group(1) if script else ""
        )
    }
    try:
        return {name: values[source] for name, source in _SSO_KEYS.items()}
    except KeyError as err:
        raise HiveApiError(f"The Hive login page did not include {err}") from err


class HiveApi:
    """Authenticated calls to the Hive REST API."""

    def __init__(self, session: Hive, websession: ClientSession | None = None) -> None:
        """Set up the client.

        Args:
            session: The Hive session whose tokens authenticate the calls.
            websession: The aiohttp session to use. One is created on first
                use when none is given.
        """
        self.session = session
        self.websession = websession

    def _get_websession(self) -> ClientSession:
        """Return the shared ClientSession, creating it on first use.

        Created lazily so the session is constructed inside a running
        event loop rather than in the synchronous constructor.
        """
        if self.websession is None:
            self.websession = ClientSession()
        return self.websession

    async def request(
        self, method: str, url: str, data: str | None = None
    ) -> dict[str, Any]:
        """Make an authenticated request and return its JSON body.

        Returns:
            The decoded body, or ``{}`` when the response has none.

        Raises:
            HiveReauthRequired: The session has no token, or Hive rejected it.
            HiveConnectionError: Hive could not be reached, failed, or sent an
                unreadable body.
            HiveApiError: Hive refused the request, or the body is not a JSON
                object.
        """
        token = self.session.tokens.token_data.get("token")
        if not token:
            raise HiveReauthRequired("The session has no API token")
        body = await _fetch(self._get_websession(), method, url, token=token, data=data)
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

    async def get_all(self) -> dict[str, Any]:
        """Fetch every product and device on the account."""
        return await self.request("get", _ALL_NODES_URL)

    async def set_state(
        self, product_type: str, product_id: str, **state: JsonValue
    ) -> None:
        """Change a product's state."""
        _LOGGER.debug("Setting %s %s: %s", product_type, product_id, state)
        url = f"{_BASE_URL}/nodes/{product_type}/{product_id}"
        await self.request("post", url, data=json.dumps(state))
