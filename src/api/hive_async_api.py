"""Hive API Module."""

import json
import logging
import re
import time

from aiohttp import (
    ClientError,
    ClientResponse,
    ClientSession,
    ClientTimeout,
    web_exceptions,
)

from ..helper.const import HTTP_FORBIDDEN, HTTP_UNAUTHORIZED
from ..helper.hive_exceptions import (
    FileInUse,
    HiveApiError,
    HiveAuthError,
    HiveConnectionError,
    NoApiToken,
)

_LOGGER = logging.getLogger(__name__)

_REQUEST_ERRORS = (ClientError, OSError, RuntimeError, json.JSONDecodeError)

# The SSO bootstrap page embeds its Cognito config in its first <script>
# block as `window.<key> = <value>` assignments. The value quoting changed
# from double to single quotes on 2026-10-20, so accept either (and require
# the closing quote to match).
_FIRST_SCRIPT = re.compile(r"<script[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)
_SSO_ASSIGNMENT = re.compile(r"""window\.(\w+)\s*=\s*(["'])(.*?)\2""")


class HiveApiAsync:
    """Hive API Code."""

    def __init__(self, hive_session=None, websession: ClientSession | None = None):
        """Hive API initialisation."""
        self.base_url = "https://beekeeper.hivehome.com/1.0"
        self.urls = {
            "properties": "https://sso.hivehome.com/",
            "all": f"{self.base_url}/nodes/all?products=true&devices=true",
            "nodes": f"{self.base_url}/nodes/{{0}}/{{1}}",
        }
        self.timeout = 5
        self.json_return = {
            "original": "No response to Hive API request",
            "parsed": "No response to Hive API request",
        }
        self.session = hive_session
        self.websession = websession

    def _get_websession(self) -> ClientSession:
        """Return the shared ClientSession, creating it on first use.

        Created lazily so the session is constructed inside a running
        event loop rather than in the synchronous constructor.
        """
        if self.websession is None:
            self.websession = ClientSession()
        return self.websession

    async def get_login_info(self) -> dict | None:
        """Fetch the Cognito pool details embedded in the Hive SSO login page.

        Returns:
            dict | None: ``UPID``, ``CLIID`` and ``REGION``, or None when the
            page does not contain them.

        Raises:
            HiveConnectionError: The page could not be fetched.
        """
        url = self.urls["properties"]
        _LOGGER.debug("get_login_info - Fetching login info from: %s", url)
        try:
            if self.websession is not None:
                page = await self._fetch_page(self.websession, url)
            else:
                # Auth objects have no shared session, so use a short-lived one.
                async with ClientSession() as websession:
                    page = await self._fetch_page(websession, url)
        except (ClientError, OSError) as err:
            _LOGGER.error("Failed to get login info: %s", err)
            raise HiveConnectionError("Could not reach the Hive login page") from err

        script = _FIRST_SCRIPT.search(page)
        script_text = script.group(1) if script else ""
        sso_values = {
            key: value for key, _quote, value in _SSO_ASSIGNMENT.findall(script_text)
        }
        try:
            login_data = {
                "UPID": sso_values["HiveSSOPoolId"],
                "CLIID": sso_values["HiveSSOPublicCognitoClientId"],
                "REGION": sso_values["HiveSSOPoolId"],
            }
        except KeyError as err:
            _LOGGER.error("Failed to get login info: missing %s", err)
            return None
        _LOGGER.debug("get_login_info - Login info extracted successfully")
        return login_data

    async def _fetch_page(self, websession: ClientSession, url: str) -> str:
        """Return the body of *url* as text."""
        timeout = ClientTimeout(total=self.timeout)
        async with websession.get(url, timeout=timeout) as resp:
            _LOGGER.debug(
                "get_login_info - Login info response status: %s", resp.status
            )
            return await resp.text()

    async def request(self, method: str, url: str, **kwargs) -> ClientResponse:
        """Make a request."""
        _LOGGER.debug("API %s request to %s", method.upper(), url)
        data = kwargs.get("data")

        headers = {
            "content-type": "application/json",
            "Accept": "*/*",
            "User-Agent": "Hive/12.04.0 iOS/18.3.1 Apple",
        }
        try:
            headers["Authorization"] = self.session.tokens.token_data["token"]
        except KeyError as exc:
            raise NoApiToken from exc

        auth_token = headers["Authorization"]
        _LOGGER.debug(
            "Using token (len=%d, tail=…%s)", len(auth_token), auth_token[-4:]
        )

        timeout = ClientTimeout(total=self.timeout)
        req_start = time.monotonic()
        async with self._get_websession().request(
            method, url, headers=headers, data=data, timeout=timeout
        ) as resp:
            resp_body = await resp.text()
            req_duration = time.monotonic() - req_start
            _LOGGER.debug(
                "API %s %s completed in %.2fs — HTTP %s",
                method.upper(),
                url,
                req_duration,
                resp.status,
            )
            if str(resp.status).startswith("20"):
                return resp

        if resp.status in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
            _LOGGER.error(
                "Hive token rejected calling %s - HTTP %s — response: %s",
                url,
                resp.status,
                resp_body[:200],
            )
            raise HiveAuthError(
                f"Token expired or forbidden calling {url} — HTTP {resp.status}"
            )
        _LOGGER.error(
            "Something has gone wrong calling %s - HTTP status is - %s — response: %s",
            url,
            resp.status,
            resp_body[:200],
        )
        raise HiveApiError

    async def _call_endpoint(self, method: str, url: str, data=None) -> dict:
        """Call an endpoint and return {"original": status, "parsed": json}."""
        json_return: dict = {}
        try:
            resp = await self.request(method, url, data=data)
            json_return["original"] = resp.status
            json_return["parsed"] = await resp.json(content_type=None)
        except TimeoutError:
            _LOGGER.warning("Hive API request timed out calling %s", url)
            raise
        except _REQUEST_ERRORS:
            await self.error()

        return json_return

    async def _call_unless_file_mode(self, method: str, url: str, data=None) -> dict:
        """Call a state-changing endpoint, or return a stub response in file mode."""
        try:
            await self.is_file_being_used()
        except FileInUse:
            return {"original": "file"}
        return await self._call_endpoint(method, url, data=data)

    async def get_all(self):
        """Build and query all endpoint."""
        return await self._call_endpoint("get", self.urls["all"])

    async def set_state(self, n_type, n_id, **kwargs):
        """Set the state of a Device."""
        _LOGGER.debug("set_state - Setting state for %s/%s: %s", n_type, n_id, kwargs)
        jsc = json.dumps(kwargs)
        url = self.urls["nodes"].format(n_type, n_id)
        return await self._call_unless_file_mode("post", url, data=jsc)

    async def error(self):
        """An error has occurred interacting with the Hive API."""
        _LOGGER.error("HTTP error occurred during Hive API interaction.")
        raise web_exceptions.HTTPError

    async def is_file_being_used(self):
        """Check if running in file mode."""
        if self.session.config.file:
            raise FileInUse()
