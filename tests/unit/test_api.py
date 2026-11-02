"""Unit tests for hiveheating.api: _fetch and HiveApi."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from hiveheating.api import _HEADERS, _TIMEOUT, HiveApi, _fetch
from hiveheating.exceptions import HiveApiError, HiveConnectionError, HiveReauthRequired

_TOKEN = "test-token"
_URL = "https://beekeeper.hivehome.com/1.0/nodes/heating/node-1"
_AUTH_HEADERS = {**_HEADERS, "Authorization": _TOKEN}


def _make_response(status=200, body="", headers=None):
    """Return a mock aiohttp response usable as an async context manager."""
    resp = MagicMock()
    resp.status = status
    resp.headers = headers or {}
    resp.text = AsyncMock(return_value=body)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_websession(status=200, body="", headers=None):
    """Return a mock ClientSession whose request() yields one response."""
    websession = MagicMock()
    websession.request.return_value = _make_response(status, body, headers)
    return websession


def _make_api(status=200, body="", token=_TOKEN):
    """Return a HiveApi over a mock Hive session and a mock websession."""
    session = MagicMock()
    session.tokens.token_data = {"token": token}
    return HiveApi(session, websession=_make_websession(status, body))


# Tests for _fetch


class TestFetch:
    @pytest.mark.parametrize(
        ("token", "data", "headers"),
        [
            pytest.param(
                "tok",
                '{"a": 1}',
                {**_HEADERS, "Authorization": "tok"},
                id="with-token-and-data",
            ),
            pytest.param(None, None, None, id="defaults"),
        ],
    )
    async def test_passes_method_url_headers_data_and_timeout(
        self, token, data, headers
    ):
        websession = _make_websession()

        await _fetch(websession, "post", _URL, token=token, data=data)

        # The timeout sits above Hive's slow responses, below the scan
        # interval, and an unreachable Hive fails at the connect limit.
        websession.request.assert_called_once_with(
            "post",
            _URL,
            headers=headers,
            data=data,
            timeout=aiohttp.ClientTimeout(total=20, connect=10),
        )

    @pytest.mark.parametrize("status", [200, 201, 204, 299])
    async def test_any_2xx_returns_the_body(self, status):
        websession = _make_websession(status, "body")

        assert await _fetch(websession, "get", _URL) == "body"

    async def test_401_on_an_authenticated_request_raises_reauth(self):
        websession = _make_websession(
            401,
            '{"message":"Unauthorized"}',
            {"x-amzn-ErrorType": "UnauthorizedException"},
        )

        with pytest.raises(HiveReauthRequired) as exc_info:
            await _fetch(websession, "get", _URL, token="test-token")

        assert str(exc_info.value) == (
            f"Hive rejected the token: HTTP 401 (UnauthorizedException) calling "
            f'{_URL}: {{"message":"Unauthorized"}}'
        )

    @pytest.mark.parametrize(
        ("status", "error_type", "token"),
        [
            # Hive answers a bad token with 401; a 403 is the gateway refusing
            # the request itself, which logging in again would not fix.
            pytest.param(
                403,
                "MissingAuthenticationTokenException",
                _TOKEN,
                id="403-missing-auth-token-with-token",
            ),
            pytest.param(403, None, _TOKEN, id="403-no-error-type-with-token"),
            # Without a token a 401 is not a token problem either.
            pytest.param(401, None, None, id="401-without-token"),
            pytest.param(403, None, None, id="403-without-token"),
            pytest.param(400, None, _TOKEN, id="400"),
            pytest.param(404, None, _TOKEN, id="404"),
            pytest.param(422, None, _TOKEN, id="422"),
            pytest.param(429, None, _TOKEN, id="429"),
            # A gateway block needs HTTP 403 as well as the error type.
            pytest.param(
                401,
                "ForbiddenException",
                None,
                id="401-forbidden-exception-without-token",
            ),
            pytest.param(
                429, "ForbiddenException", _TOKEN, id="429-forbidden-exception"
            ),
        ],
    )
    async def test_refused_requests_raise_api_error(self, status, error_type, token):
        headers = {"x-amzn-ErrorType": error_type} if error_type else None
        websession = _make_websession(status, "blocked", headers)

        with pytest.raises(HiveApiError) as exc_info:
            await _fetch(websession, "get", _URL, token=token)

        assert not isinstance(exc_info.value, HiveReauthRequired)
        assert not isinstance(exc_info.value, HiveConnectionError)
        kind = f" ({error_type})" if error_type else ""
        assert str(exc_info.value) == (
            f"Hive refused the request: HTTP {status}{kind} calling {_URL}: blocked"
        )

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    async def test_5xx_raises_connection_error(self, status):
        websession = _make_websession(status, "<html>Bad gateway</html>")

        with pytest.raises(HiveConnectionError) as exc_info:
            await _fetch(websession, "get", _URL)

        assert str(exc_info.value) == (
            f"Hive server error: HTTP {status} calling {_URL}: <html>Bad gateway</html>"
        )

    async def test_error_detail_keeps_only_the_first_200_body_characters(self):
        websession = _make_websession(404, "x" * 200 + "TRUNCATED")

        with pytest.raises(HiveApiError) as exc_info:
            await _fetch(websession, "get", _URL)

        assert str(exc_info.value) == (
            f"Hive refused the request: HTTP 404 calling {_URL}: {'x' * 200}"
        )

    @pytest.mark.parametrize(
        "err",
        [
            pytest.param(TimeoutError(), id="asyncio-timeout"),
            # aiohttp.ServerTimeoutError is a ClientError as well as a
            # TimeoutError; the timeout translation wins.
            pytest.param(
                aiohttp.ServerTimeoutError("Connection timeout to host"),
                id="server-timeout",
            ),
        ],
    )
    async def test_timeout_raises_connection_error(self, err):
        websession = _make_websession()
        websession.request.side_effect = err

        with pytest.raises(HiveConnectionError) as exc_info:
            await _fetch(websession, "get", _URL)

        assert str(exc_info.value) == f"Timed out calling {_URL}"
        assert exc_info.value.__cause__ is err

    @pytest.mark.parametrize(
        "err",
        [
            aiohttp.ClientError("boom"),
            aiohttp.ServerDisconnectedError(),
            aiohttp.ClientPayloadError("Response payload is not completed"),
            OSError("network down"),
            ConnectionResetError(54, "Connection reset by peer"),
        ],
        ids=[
            "client_error",
            "server_disconnected",
            "payload_error",
            "os_error",
            "connection_reset",
        ],
    )
    async def test_client_and_os_errors_raise_connection_error(self, err):
        websession = _make_websession()
        websession.request.side_effect = err

        with pytest.raises(HiveConnectionError) as exc_info:
            await _fetch(websession, "get", _URL)

        assert str(exc_info.value) == f"Could not reach {_URL}: {err}"
        assert exc_info.value.__cause__ is err

    @pytest.mark.parametrize(
        ("err", "message"),
        [
            pytest.param(
                aiohttp.ClientPayloadError("Response payload is not completed"),
                "Could not reach",
                id="payload-error",
            ),
            pytest.param(TimeoutError(), "Timed out calling", id="timeout"),
        ],
    )
    async def test_error_while_reading_the_body_is_translated(self, err, message):
        websession = _make_websession()
        resp = websession.request.return_value
        resp.text.side_effect = err

        with pytest.raises(HiveConnectionError, match=message) as exc_info:
            await _fetch(websession, "get", _URL)

        assert exc_info.value.__cause__ is err
        resp.__aexit__.assert_awaited_once()

    async def test_unexpected_errors_propagate_untranslated(self):
        websession = _make_websession()
        websession.request.side_effect = RuntimeError("Session is closed")

        with pytest.raises(RuntimeError, match="Session is closed"):
            await _fetch(websession, "get", _URL)


class TestFetchGatewayErrorType:
    """Hive's API gateway blocking a burst of requests."""

    @pytest.mark.parametrize(
        ("token", "error_type"),
        [
            pytest.param(_TOKEN, "ForbiddenException", id="with-token"),
            pytest.param(None, "ForbiddenException", id="without-token"),
            pytest.param(
                _TOKEN,
                "ForbiddenException:http://internal.amazon.com/",
                id="doc-suffix",
            ),
        ],
    )
    async def test_a_gateway_block_is_a_connection_error(self, token, error_type):
        # Hive's gateway answers a burst of requests with 403
        # ForbiddenException for a minute or two: retry later, don't log in.
        websession = _make_websession(
            403, '{"message":"Forbidden"}', {"x-amzn-ErrorType": error_type}
        )

        with pytest.raises(HiveConnectionError) as exc_info:
            await _fetch(websession, "post", _URL, token=token)

        assert str(exc_info.value) == (
            "Hive is temporarily blocking requests, try again in a few minutes: "
            f'HTTP 403 (ForbiddenException) calling {_URL}: {{"message":"Forbidden"}}'
        )


# Tests for HiveApi construction and its websession


class TestHiveApiWebsession:
    async def test_requests_share_the_lazily_created_session(self):
        websession = _make_websession(200, '{"ok": true}')
        session = MagicMock()
        session.tokens.token_data = {"token": _TOKEN}
        api = HiveApi(session)

        with patch(
            "hiveheating.api.ClientSession", return_value=websession
        ) as session_cls:
            first = await api.request("get", _URL)
            second = await api.request("get", _URL)

        assert first == {"ok": True}
        assert second == {"ok": True}
        session_cls.assert_called_once_with()
        assert websession.request.call_count == 2
        assert api.websession is websession


# Tests for HiveApi.request


class TestRequest:
    async def test_returns_the_decoded_json_object(self):
        api = _make_api(200, '{"products": [], "devices": []}')

        assert await api.request("get", _URL) == {"products": [], "devices": []}

    @pytest.mark.parametrize(
        ("status", "body"),
        [(200, ""), (204, ""), (200, " \n ")],
        ids=["200", "204", "whitespace"],
    )
    async def test_an_empty_body_returns_an_empty_dict(self, status, body):
        api = _make_api(status, body)

        assert await api.request("post", _URL) == {}

    @pytest.mark.parametrize(
        "token_data",
        [{}, {"token": ""}, {"token": None}],
        ids=["missing", "empty", "none"],
    )
    async def test_without_a_token_raises_reauth_before_any_call(self, token_data):
        api = _make_api()
        api.session.tokens.token_data = token_data

        with pytest.raises(HiveReauthRequired) as exc_info:
            await api.request("get", _URL)

        assert str(exc_info.value) == "The session has no API token"
        api.websession.request.assert_not_called()

    @pytest.mark.parametrize(
        "body",
        ["<html>Bad gateway</html>", "not json", '{"truncated": '],
        ids=["html", "text", "truncated_json"],
    )
    async def test_a_body_that_is_not_json_raises_connection_error(self, body):
        api = _make_api(200, body)

        with pytest.raises(HiveConnectionError) as exc_info:
            await api.request("get", _URL)

        assert str(exc_info.value) == (
            f"Hive returned an unreadable body calling {_URL}"
        )
        assert isinstance(exc_info.value.__cause__, json.JSONDecodeError)

    @pytest.mark.parametrize(
        ("body", "type_name"),
        [('[{"id": "node-1"}]', "list"), ('"text"', "str"), ("null", "NoneType")],
    )
    async def test_json_that_is_not_an_object_raises_api_error(self, body, type_name):
        api = _make_api(200, body)

        with pytest.raises(HiveApiError) as exc_info:
            await api.request("get", _URL)

        assert str(exc_info.value) == (
            f"Hive returned a JSON {type_name} calling {_URL}"
        )
        assert exc_info.value.__cause__ is None

    async def test_http_errors_are_raised_before_the_body_is_parsed(self):
        api = _make_api(503, "<html>not json</html>")

        with pytest.raises(HiveConnectionError) as exc_info:
            await api.request("get", _URL)

        assert f"HTTP 503 calling {_URL}: <html>not json</html>" in str(exc_info.value)


# Tests for HiveApi.get_all


class TestGetAll:
    async def test_gets_every_product_and_device(self):
        body = '{"products": [{"id": "p1"}], "devices": [{"id": "d1"}], "user": {}}'
        api = _make_api(200, body)

        result = await api.get_all()

        assert result == {
            "products": [{"id": "p1"}],
            "devices": [{"id": "d1"}],
            "user": {},
        }
        api.websession.request.assert_called_once_with(
            "get",
            "https://beekeeper.hivehome.com/1.0/nodes/all?products=true&devices=true",
            headers=_AUTH_HEADERS,
            data=None,
            timeout=_TIMEOUT,
        )


# Tests for HiveApi.set_state


class TestSetState:
    async def test_posts_the_state_as_json_to_the_product_url(self):
        api = _make_api(200, '{"id": "node-1"}')

        await api.set_state("heating", "node-1", mode="MANUAL", target=21.5)

        api.websession.request.assert_called_once_with(
            "post",
            "https://beekeeper.hivehome.com/1.0/nodes/heating/node-1",
            headers=_AUTH_HEADERS,
            data='{"mode": "MANUAL", "target": 21.5}',
            timeout=_TIMEOUT,
        )
