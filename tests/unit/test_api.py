"""Unit tests for apyhiveapi.api: _fetch, get_login_info and HiveApi."""

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from apyhiveapi.api import (
    _ALL_NODES_URL,
    _BASE_URL,
    _HEADERS,
    _SSO_URL,
    _TIMEOUT,
    HiveApi,
    _fetch,
    get_login_info,
)
from apyhiveapi.exceptions import HiveApiError, HiveConnectionError, HiveReauthRequired

_TOKEN = "test-token"
_URL = "https://beekeeper.hivehome.com/1.0/nodes/heating/node-1"
_AUTH_HEADERS = {**_HEADERS, "Authorization": _TOKEN}
_SSO_PAGE = (
    "<html><head><script>"
    'window.HiveSSOPoolId="eu-west-1_abc",'
    'window.HiveSSOPublicCognitoClientId="client123"'
    "</script></head></html>"
)
_LOGIN_INFO = {
    "UPID": "eu-west-1_abc",
    "CLIID": "client123",
    "REGION": "eu-west-1_abc",
}


def _make_response(status=200, body=""):
    """Return a mock aiohttp response usable as an async context manager."""
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=body)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_websession(status=200, body=""):
    """Return a mock ClientSession whose request() yields one response."""
    websession = MagicMock()
    websession.request.return_value = _make_response(status, body)
    return websession


def _make_api(status=200, body="", token=_TOKEN):
    """Return a HiveApi over a mock Hive session and a mock websession."""
    session = MagicMock()
    session.tokens.token_data = {"token": token}
    return HiveApi(session, websession=_make_websession(status, body))


def _api_messages(caplog):
    """Return the messages the apyhiveapi.api logger emitted."""
    return [r.getMessage() for r in caplog.records if r.name == "apyhiveapi.api"]


# Tests for the module constants


class TestConstants:
    def test_timeout_budget(self):
        # Above Hive's slow responses, below the scan interval, and an
        # unreachable Hive fails at the connect limit.
        assert aiohttp.ClientTimeout(total=20, connect=10) == _TIMEOUT

    def test_urls_point_at_hive_sso_and_beekeeper(self):
        assert _SSO_URL == "https://sso.hivehome.com/"
        assert _BASE_URL == "https://beekeeper.hivehome.com/1.0"
        assert _ALL_NODES_URL == (
            "https://beekeeper.hivehome.com/1.0/nodes/all?products=true&devices=true"
        )

    def test_headers_carry_no_token(self):
        assert _HEADERS["content-type"] == "application/json"
        assert _HEADERS["Accept"] == "*/*"
        assert _HEADERS["User-Agent"].startswith("Hive/")
        assert "Authorization" not in _HEADERS


# Tests for _fetch


class TestFetch:
    async def test_returns_the_body_of_a_200(self):
        websession = _make_websession(200, '{"ok": true}')

        assert await _fetch(websession, "get", _URL) == '{"ok": true}'

    async def test_passes_method_url_token_data_and_timeout(self):
        websession = _make_websession()

        await _fetch(websession, "post", _URL, token="tok", data='{"a": 1}')

        websession.request.assert_called_once_with(
            "post",
            _URL,
            headers={**_HEADERS, "Authorization": "tok"},
            data='{"a": 1}',
            timeout=aiohttp.ClientTimeout(total=20, connect=10),
        )

    async def test_headers_and_data_default_to_none(self):
        websession = _make_websession()

        await _fetch(websession, "get", _URL)

        websession.request.assert_called_once_with(
            "get", _URL, headers=None, data=None, timeout=_TIMEOUT
        )

    async def test_reads_the_body_inside_the_response_context(self):
        websession = _make_websession(200, "body")
        resp = websession.request.return_value

        await _fetch(websession, "get", _URL)

        resp.__aenter__.assert_awaited_once()
        resp.text.assert_awaited_once_with()
        resp.__aexit__.assert_awaited_once()

    @pytest.mark.parametrize("status", [200, 201, 204, 299])
    async def test_any_2xx_returns_the_body(self, status):
        websession = _make_websession(status, "body")

        assert await _fetch(websession, "get", _URL) == "body"

    @pytest.mark.parametrize("status", [401, 403])
    async def test_401_and_403_on_an_authenticated_request_raise_reauth(self, status):
        websession = _make_websession(status, '{"error": "unauthorised"}')

        with pytest.raises(HiveReauthRequired) as exc_info:
            await _fetch(websession, "get", _URL, token="test-token")

        assert str(exc_info.value) == (
            f"Hive rejected the token: HTTP {status} calling {_URL}: "
            '{"error": "unauthorised"}'
        )

    @pytest.mark.parametrize("status", [401, 403])
    async def test_401_and_403_without_a_token_are_plain_api_errors(self, status):
        websession = _make_websession(status, "blocked")

        with pytest.raises(HiveApiError) as exc_info:
            await _fetch(websession, "get", _URL)

        assert str(exc_info.value) == (
            f"Hive refused the request: HTTP {status} calling {_URL}: blocked"
        )

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    async def test_5xx_raises_connection_error(self, status):
        websession = _make_websession(status, "<html>Bad gateway</html>")

        with pytest.raises(HiveConnectionError) as exc_info:
            await _fetch(websession, "get", _URL)

        assert str(exc_info.value) == (
            f"Hive server error: HTTP {status} calling {_URL}: <html>Bad gateway</html>"
        )

    @pytest.mark.parametrize("status", [101, 300, 304, 400, 404, 422, 429])
    async def test_any_other_status_raises_api_error(self, status):
        websession = _make_websession(status, "nope")

        with pytest.raises(HiveApiError) as exc_info:
            await _fetch(websession, "get", _URL)

        assert str(exc_info.value) == (
            f"Hive refused the request: HTTP {status} calling {_URL}: nope"
        )

    async def test_error_detail_keeps_only_the_first_200_body_characters(self):
        websession = _make_websession(404, "x" * 200 + "TRUNCATED")

        with pytest.raises(HiveApiError) as exc_info:
            await _fetch(websession, "get", _URL)

        assert str(exc_info.value) == (
            f"Hive refused the request: HTTP 404 calling {_URL}: {'x' * 200}"
        )

    async def test_timeout_raises_connection_error(self):
        websession = _make_websession()
        err = TimeoutError()
        websession.request.side_effect = err

        with pytest.raises(HiveConnectionError) as exc_info:
            await _fetch(websession, "get", _URL)

        assert str(exc_info.value) == f"Timed out calling {_URL}"
        assert exc_info.value.__cause__ is err

    async def test_server_timeout_counts_as_a_timeout(self):
        # aiohttp.ServerTimeoutError is a ClientError as well as a TimeoutError;
        # the timeout translation wins.
        websession = _make_websession()
        err = aiohttp.ServerTimeoutError("Connection timeout to host")
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

    async def test_connection_refused_names_the_host(self):
        websession = _make_websession()
        websession.request.side_effect = aiohttp.ClientConnectorError(
            MagicMock(host="beekeeper.hivehome.com", port=443, ssl=True),
            OSError(61, "Connection refused"),
        )

        with pytest.raises(HiveConnectionError) as exc_info:
            await _fetch(websession, "get", _URL)

        assert str(exc_info.value) == (
            f"Could not reach {_URL}: Cannot connect to host "
            "beekeeper.hivehome.com:443 ssl:default [Connection refused]"
        )

    async def test_error_while_opening_the_response_is_translated(self):
        websession = _make_websession()
        err = aiohttp.ServerDisconnectedError()
        websession.request.return_value.__aenter__.side_effect = err

        with pytest.raises(HiveConnectionError) as exc_info:
            await _fetch(websession, "get", _URL)

        assert str(exc_info.value) == f"Could not reach {_URL}: Server disconnected"
        assert exc_info.value.__cause__ is err

    async def test_error_while_reading_the_body_is_translated(self):
        websession = _make_websession()
        resp = websession.request.return_value
        err = aiohttp.ClientPayloadError("Response payload is not completed")
        resp.text.side_effect = err

        with pytest.raises(HiveConnectionError) as exc_info:
            await _fetch(websession, "get", _URL)

        assert exc_info.value.__cause__ is err
        resp.__aexit__.assert_awaited_once()

    async def test_timeout_while_reading_the_body_is_translated(self):
        websession = _make_websession()
        websession.request.return_value.text.side_effect = TimeoutError()

        with pytest.raises(HiveConnectionError) as exc_info:
            await _fetch(websession, "get", _URL)

        assert str(exc_info.value) == f"Timed out calling {_URL}"

    async def test_unexpected_errors_propagate_untranslated(self):
        websession = _make_websession()
        websession.request.side_effect = RuntimeError("Session is closed")

        with pytest.raises(RuntimeError, match="Session is closed"):
            await _fetch(websession, "get", _URL)


class TestFetchLogging:
    async def test_logs_method_url_status_and_duration(self, caplog):
        caplog.set_level(logging.DEBUG, logger="apyhiveapi.api")
        websession = _make_websession(200, "body")

        with patch("apyhiveapi.api.time") as fake_time:
            fake_time.monotonic.side_effect = [10.0, 10.25]
            await _fetch(websession, "post", _URL)

        assert _api_messages(caplog) == [f"POST {_URL} answered HTTP 200 in 0.25s"]
        assert caplog.records[0].levelno == logging.DEBUG

    async def test_logs_rejected_requests_too(self, caplog):
        caplog.set_level(logging.DEBUG, logger="apyhiveapi.api")
        websession = _make_websession(503, "")

        with pytest.raises(HiveConnectionError):
            await _fetch(websession, "get", _URL)

        messages = _api_messages(caplog)
        assert len(messages) == 1
        assert messages[0].startswith(f"GET {_URL} answered HTTP 503 in ")

    async def test_logs_nothing_when_the_request_fails(self, caplog):
        caplog.set_level(logging.DEBUG, logger="apyhiveapi.api")
        websession = _make_websession()
        websession.request.side_effect = OSError("network down")

        with pytest.raises(HiveConnectionError):
            await _fetch(websession, "get", _URL)

        assert _api_messages(caplog) == []


# Tests for get_login_info


class TestGetLoginInfo:
    async def test_parses_double_quoted_assignments(self):
        websession = _make_websession(body=_SSO_PAGE)

        assert await get_login_info(websession) == _LOGIN_INFO

    async def test_fetches_the_sso_page_with_the_given_session(self):
        websession = _make_websession(body=_SSO_PAGE)

        with patch("apyhiveapi.api.ClientSession") as session_cls:
            await get_login_info(websession)

        websession.request.assert_called_once_with(
            "get",
            "https://sso.hivehome.com/",
            headers=None,
            data=None,
            timeout=_TIMEOUT,
        )
        session_cls.assert_not_called()
        websession.close.assert_not_called()

    async def test_parses_single_quoted_assignments_with_spaces(self):
        html = (
            "<script>\n"
            "        window.HiveSSOPoolId = 'eu-west-1_abc'\n"
            "        window.HiveSSOCognitoClientId = 'private123'\n"
            "        window.HiveSSOPublicCognitoClientId = 'client123'\n"
            "        window.HiveSSOState = '%%STATE%%'\n"
            "    </script>"
        )
        websession = _make_websession(body=html)

        assert await get_login_info(websession) == _LOGIN_INFO

    async def test_region_is_the_pool_id(self):
        result = await get_login_info(_make_websession(body=_SSO_PAGE))

        assert result["REGION"] == "eu-west-1_abc"
        assert result["REGION"] == result["UPID"]

    async def test_script_tag_attributes_and_case_are_accepted(self):
        html = (
            '<SCRIPT type="text/javascript">'
            'window.HiveSSOPoolId = "eu-west-1_abc"; '
            'window.HiveSSOPublicCognitoClientId = "client123";'
            "</SCRIPT>"
        )
        websession = _make_websession(body=html)

        assert await get_login_info(websession) == _LOGIN_INFO

    async def test_a_value_may_contain_the_other_quote_character(self):
        html = (
            "<script>"
            'window.HiveSSOPoolId = "it\'s"; '
            "window.HiveSSOPublicCognitoClientId = 'say \"hi\"'"
            "</script>"
        )
        websession = _make_websession(body=html)

        assert await get_login_info(websession) == {
            "UPID": "it's",
            "CLIID": 'say "hi"',
            "REGION": "it's",
        }

    async def test_only_the_first_script_block_is_parsed(self):
        html = (
            "<script>window.Unrelated = 'x'</script>"
            "<script>window.HiveSSOPoolId = 'p' "
            "window.HiveSSOPublicCognitoClientId = 'c'</script>"
        )
        websession = _make_websession(body=html)

        with pytest.raises(HiveApiError) as exc_info:
            await get_login_info(websession)

        assert str(exc_info.value) == (
            "The Hive login page did not include 'HiveSSOPoolId'"
        )

    async def test_mismatched_quotes_are_not_parsed(self):
        html = "<script>window.HiveSSOPoolId = 'eu-west-1_abc\"</script>"
        websession = _make_websession(body=html)

        with pytest.raises(HiveApiError, match="'HiveSSOPoolId'"):
            await get_login_info(websession)

    async def test_missing_client_id_is_named(self):
        html = "<script>window.HiveSSOPoolId = 'eu-west-1_abc'</script>"
        websession = _make_websession(body=html)

        with pytest.raises(HiveApiError) as exc_info:
            await get_login_info(websession)

        assert str(exc_info.value) == (
            "The Hive login page did not include 'HiveSSOPublicCognitoClientId'"
        )

    async def test_missing_keys_raise_api_error_caused_by_key_error(self):
        html = "<script>window.SomeOtherKey = 'value'</script>"
        websession = _make_websession(body=html)

        with pytest.raises(HiveApiError) as exc_info:
            await get_login_info(websession)

        assert str(exc_info.value) == (
            "The Hive login page did not include 'HiveSSOPoolId'"
        )
        assert isinstance(exc_info.value.__cause__, KeyError)
        assert exc_info.value.__cause__.args == ("HiveSSOPoolId",)

    @pytest.mark.parametrize(
        "html", ["", "<html><body>Down for maintenance</body></html>"]
    )
    async def test_page_without_a_script_raises_api_error(self, html):
        websession = _make_websession(body=html)

        with pytest.raises(HiveApiError, match="did not include 'HiveSSOPoolId'"):
            await get_login_info(websession)

    @pytest.mark.parametrize(
        ("status", "exc"),
        [
            # The login page gets no token, so a 401/403 is not a token problem.
            (401, HiveApiError),
            (403, HiveApiError),
            (404, HiveApiError),
            (500, HiveConnectionError),
            (503, HiveConnectionError),
        ],
    )
    async def test_http_errors_propagate(self, status, exc):
        websession = _make_websession(status, "blocked")

        with pytest.raises(exc) as exc_info:
            await get_login_info(websession)

        assert f"HTTP {status} calling https://sso.hivehome.com/: blocked" in str(
            exc_info.value
        )

    @pytest.mark.parametrize(
        "err",
        [TimeoutError(), aiohttp.ClientError("net error"), OSError("network down")],
        ids=["timeout", "client_error", "os_error"],
    )
    async def test_network_errors_propagate_as_connection_errors(self, err):
        websession = _make_websession()
        websession.request.side_effect = err

        with pytest.raises(HiveConnectionError) as exc_info:
            await get_login_info(websession)

        assert exc_info.value.__cause__ is err

    async def test_opens_and_closes_a_temporary_session_when_none_is_given(self):
        temp = _make_websession(body=_SSO_PAGE)
        temp.__aenter__ = AsyncMock(return_value=temp)
        temp.__aexit__ = AsyncMock(return_value=False)

        with patch("apyhiveapi.api.ClientSession", return_value=temp) as session_cls:
            result = await get_login_info()

        assert result == _LOGIN_INFO
        session_cls.assert_called_once_with()
        temp.__aenter__.assert_awaited_once()
        temp.request.assert_called_once_with(
            "get", _SSO_URL, headers=None, data=None, timeout=_TIMEOUT
        )
        temp.__aexit__.assert_awaited_once()

    async def test_the_temporary_session_is_closed_when_the_fetch_fails(self):
        temp = _make_websession(503, "down")
        temp.__aenter__ = AsyncMock(return_value=temp)
        temp.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("apyhiveapi.api.ClientSession", return_value=temp),
            pytest.raises(HiveConnectionError),
        ):
            await get_login_info()

        temp.__aexit__.assert_awaited_once()
        assert temp.__aexit__.await_args.args[0] is HiveConnectionError


# Tests for HiveApi construction and its websession


class TestHiveApiWebsession:
    def test_stores_the_session_and_the_websession(self):
        session = MagicMock()
        websession = MagicMock()

        api = HiveApi(session, websession)

        assert api.session is session
        assert api.websession is websession

    def test_the_constructor_creates_no_client_session(self):
        with patch("apyhiveapi.api.ClientSession") as session_cls:
            api = HiveApi(MagicMock())

        session_cls.assert_not_called()
        assert api.websession is None

    def test_get_websession_returns_the_given_session(self):
        websession = MagicMock()
        api = HiveApi(MagicMock(), websession)

        with patch("apyhiveapi.api.ClientSession") as session_cls:
            assert api._get_websession() is websession

        session_cls.assert_not_called()

    def test_get_websession_creates_one_lazily_and_caches_it(self):
        api = HiveApi(MagicMock())

        with patch("apyhiveapi.api.ClientSession") as session_cls:
            first = api._get_websession()
            second = api._get_websession()

        session_cls.assert_called_once_with()
        assert first is session_cls.return_value
        assert second is first
        assert api.websession is first

    async def test_requests_share_the_lazily_created_session(self):
        websession = _make_websession(200, '{"ok": true}')
        session = MagicMock()
        session.tokens.token_data = {"token": _TOKEN}
        api = HiveApi(session)

        with patch(
            "apyhiveapi.api.ClientSession", return_value=websession
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

    async def test_sends_the_token_with_the_default_headers(self):
        api = _make_api()

        await api.request("get", _URL)

        api.websession.request.assert_called_once_with(
            "get", _URL, headers=_AUTH_HEADERS, data=None, timeout=_TIMEOUT
        )
        headers = api.websession.request.call_args.kwargs["headers"]
        assert headers["Authorization"] == "test-token"
        assert headers["content-type"] == "application/json"

    async def test_does_not_mutate_the_module_headers(self):
        api = _make_api()

        await api.request("get", _URL)

        assert "Authorization" not in _HEADERS

    async def test_sends_the_data_as_given(self):
        api = _make_api()

        await api.request("post", _URL, data='{"mode": "MANUAL"}')

        assert api.websession.request.call_args.args == ("post", _URL)
        assert api.websession.request.call_args.kwargs["data"] == '{"mode": "MANUAL"}'

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
        [
            ("[]", "list"),
            ('[{"id": "node-1"}]', "list"),
            ('"text"', "str"),
            ("42", "int"),
            ("true", "bool"),
            ("null", "NoneType"),
        ],
    )
    async def test_json_that_is_not_an_object_raises_api_error(self, body, type_name):
        api = _make_api(200, body)

        with pytest.raises(HiveApiError) as exc_info:
            await api.request("get", _URL)

        assert str(exc_info.value) == (
            f"Hive returned a JSON {type_name} calling {_URL}"
        )
        assert exc_info.value.__cause__ is None

    @pytest.mark.parametrize(
        ("status", "exc"),
        [
            (401, HiveReauthRequired),
            (403, HiveReauthRequired),
            (404, HiveApiError),
            (500, HiveConnectionError),
            (503, HiveConnectionError),
        ],
    )
    async def test_http_errors_are_raised_before_the_body_is_parsed(self, status, exc):
        api = _make_api(status, "<html>not json</html>")

        with pytest.raises(exc) as exc_info:
            await api.request("get", _URL)

        assert f"HTTP {status} calling {_URL}: <html>not json</html>" in str(
            exc_info.value
        )

    async def test_network_errors_raise_connection_error(self):
        api = _make_api()
        err = aiohttp.ClientConnectorError(
            MagicMock(host="beekeeper.hivehome.com", port=443, ssl=True),
            OSError(61, "Connection refused"),
        )
        api.websession.request.side_effect = err

        with pytest.raises(HiveConnectionError) as exc_info:
            await api.request("get", _URL)

        assert exc_info.value.__cause__ is err

    async def test_timeout_raises_connection_error(self):
        api = _make_api()
        api.websession.request.side_effect = TimeoutError()

        with pytest.raises(HiveConnectionError, match="Timed out calling"):
            await api.request("get", _URL)


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

    async def test_an_empty_body_returns_an_empty_dict(self):
        api = _make_api(200, "")

        assert await api.get_all() == {}

    async def test_without_a_token_raises_reauth_before_any_call(self):
        api = _make_api()
        api.session.tokens.token_data = {}

        with pytest.raises(HiveReauthRequired, match="no API token"):
            await api.get_all()

        api.websession.request.assert_not_called()

    @pytest.mark.parametrize(
        ("status", "body", "exc"),
        [
            (401, "", HiveReauthRequired),
            (403, "", HiveReauthRequired),
            (404, "", HiveApiError),
            (503, "", HiveConnectionError),
            (200, "<html>", HiveConnectionError),
            (200, "[]", HiveApiError),
        ],
        ids=["401", "403", "404", "503", "unreadable_body", "json_list"],
    )
    async def test_errors_propagate(self, status, body, exc):
        api = _make_api(status, body)

        with pytest.raises(exc):
            await api.get_all()

    async def test_timeout_raises_connection_error(self):
        api = _make_api()
        api.websession.request.side_effect = TimeoutError()

        with pytest.raises(HiveConnectionError, match="Timed out calling"):
            await api.get_all()


# Tests for HiveApi.set_state


class TestSetState:
    async def test_posts_the_state_as_json_to_the_product_url(self):
        api = _make_api(200, "")

        result = await api.set_state("heating", "node-1", mode="MANUAL", target=21.5)

        assert result is None
        api.websession.request.assert_called_once_with(
            "post",
            "https://beekeeper.hivehome.com/1.0/nodes/heating/node-1",
            headers=_AUTH_HEADERS,
            data='{"mode": "MANUAL", "target": 21.5}',
            timeout=_TIMEOUT,
        )

    async def test_builds_the_url_from_the_product_type_and_id(self):
        api = _make_api()

        await api.set_state("hotwater", "hw-9", mode="ON")

        assert api.websession.request.call_args.args == (
            "post",
            f"{_BASE_URL}/nodes/hotwater/hw-9",
        )

    async def test_without_state_posts_an_empty_object(self):
        api = _make_api()

        await api.set_state("trvcontrol", "trv-1")

        assert api.websession.request.call_args.kwargs["data"] == "{}"

    async def test_every_json_value_type_survives_the_round_trip(self):
        api = _make_api()
        state = {
            "mode": "BOOST",
            "boost": 30,
            "target": 22.0,
            "scheduleLock": False,
            "note": None,
        }

        await api.set_state("heating", "node-1", **state)

        data = api.websession.request.call_args.kwargs["data"]
        assert data == json.dumps(state)
        assert json.loads(data) == state

    async def test_quotes_in_values_are_escaped(self):
        api = _make_api()

        await api.set_state("heating", "node-1", mode='MANUAL"injected')

        data = api.websession.request.call_args.kwargs["data"]
        assert json.loads(data) == {"mode": 'MANUAL"injected'}

    async def test_the_response_body_is_discarded(self):
        api = _make_api(200, '{"id": "node-1", "mode": "MANUAL"}')

        assert await api.set_state("heating", "node-1", mode="MANUAL") is None

    async def test_logs_the_requested_change(self, caplog):
        caplog.set_level(logging.DEBUG, logger="apyhiveapi.api")
        api = _make_api()

        await api.set_state("heating", "node-1", mode="MANUAL")

        assert "Setting heating node-1: {'mode': 'MANUAL'}" in _api_messages(caplog)

    async def test_without_a_token_raises_reauth_before_any_call(self):
        api = _make_api()
        api.session.tokens.token_data = {}

        with pytest.raises(HiveReauthRequired, match="no API token"):
            await api.set_state("heating", "node-1", mode="MANUAL")

        api.websession.request.assert_not_called()

    @pytest.mark.parametrize(
        ("status", "body", "exc"),
        [
            (401, "", HiveReauthRequired),
            (403, "", HiveReauthRequired),
            (404, "", HiveApiError),
            (500, "", HiveConnectionError),
            (200, "<html>", HiveConnectionError),
            (200, "[]", HiveApiError),
        ],
        ids=["401", "403", "404", "500", "unreadable_body", "json_list"],
    )
    async def test_errors_propagate(self, status, body, exc):
        api = _make_api(status, body)

        with pytest.raises(exc):
            await api.set_state("heating", "node-1", mode="MANUAL")

    async def test_network_errors_raise_connection_error(self):
        api = _make_api()
        err = aiohttp.ServerDisconnectedError()
        api.websession.request.side_effect = err

        with pytest.raises(HiveConnectionError) as exc_info:
            await api.set_state("heating", "node-1", mode="MANUAL")

        assert str(exc_info.value) == (
            f"Could not reach {_BASE_URL}/nodes/heating/node-1: Server disconnected"
        )
        assert exc_info.value.__cause__ is err
