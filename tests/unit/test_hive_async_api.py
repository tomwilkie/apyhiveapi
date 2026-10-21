"""Unit tests for HiveApiAsync."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aiohttp import web_exceptions
from apyhiveapi.api.hive_async_api import HiveApiAsync
from apyhiveapi.helper.hive_exceptions import (
    FileInUse,
    HiveApiError,
    HiveAuthError,
    HiveConnectionError,
    NoApiToken,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_response(status=200, json_data=None):
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value="")
    resp.json = AsyncMock(return_value=json_data or {"data": "test"})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_mock_websession(status=200, json_data=None):
    resp = _make_mock_response(status=status, json_data=json_data)
    websession = MagicMock()
    websession.request.return_value = resp
    websession.closed = False
    websession.close = AsyncMock()
    return websession


def _make_api(status=200, json_data=None, token="test-token", file_mode=False):
    websession = _make_mock_websession(status=status, json_data=json_data)
    session = MagicMock()
    session.tokens = MagicMock()
    session.tokens.token_data = {"token": token}
    session.config = MagicMock()
    session.config.file = file_mode
    return HiveApiAsync(hive_session=session, websession=websession)


def _make_api_no_token():
    """Return an API instance whose session raises KeyError on token lookup."""
    websession = _make_mock_websession(status=200)
    session = MagicMock()
    session.tokens = MagicMock()
    # Raise KeyError when "token" key is accessed
    session.tokens.token_data = {}
    session.config = MagicMock()
    session.config.file = False
    return HiveApiAsync(hive_session=session, websession=websession)


# ---------------------------------------------------------------------------
# Tests: HiveApiAsync.request
# ---------------------------------------------------------------------------


class TestHiveApiAsyncRequest:
    async def test_successful_200_returns_response(self):
        api = _make_api(status=200, json_data={"ok": True})
        resp = await api.request("get", "https://beekeeper.hivehome.com/1.0/nodes/all")
        assert resp.status == 200

    async def test_201_also_succeeds(self):
        api = _make_api(status=201)
        resp = await api.request("post", "https://beekeeper.hivehome.com/1.0/nodes/x/y")
        assert resp.status == 201

    async def test_non_sso_without_token_raises_no_api_token(self):
        api = _make_api_no_token()
        with pytest.raises(NoApiToken):
            await api.request("get", "https://beekeeper.hivehome.com/1.0/nodes/all")

    async def test_401_raises_hive_auth_error(self):
        api = _make_api(status=401)
        with pytest.raises(HiveAuthError):
            await api.request("get", "https://beekeeper.hivehome.com/1.0/nodes/all")

    async def test_403_raises_hive_auth_error(self):
        api = _make_api(status=403)
        with pytest.raises(HiveAuthError):
            await api.request("get", "https://beekeeper.hivehome.com/1.0/nodes/all")

    async def test_500_raises_hive_api_error(self):
        api = _make_api(status=500)
        with pytest.raises(HiveApiError):
            await api.request("get", "https://beekeeper.hivehome.com/1.0/nodes/all")

    async def test_404_raises_hive_api_error(self):
        api = _make_api(status=404)
        with pytest.raises(HiveApiError):
            await api.request("get", "https://beekeeper.hivehome.com/1.0/nodes/all")


# ---------------------------------------------------------------------------
# Tests: HiveApiAsync.get_all
# ---------------------------------------------------------------------------


class TestGetAll:
    async def test_successful_get_all_returns_parsed_json(self):
        payload = {"products": [], "devices": []}
        api = _make_api(status=200, json_data=payload)
        result = await api.get_all()
        assert result["original"] == 200
        assert result["parsed"] == payload

    async def test_timeout_error_propagates(self):
        api = _make_api(status=200)
        api.websession.request.side_effect = asyncio.TimeoutError
        with pytest.raises(asyncio.TimeoutError):
            await api.get_all()

    async def test_os_error_calls_error_method(self):
        api = _make_api(status=200)
        api.websession.request.side_effect = OSError("network down")
        with pytest.raises(web_exceptions.HTTPError):
            await api.get_all()

    async def test_runtime_error_calls_error_method(self):
        api = _make_api(status=200)
        api.websession.request.side_effect = RuntimeError("boom")
        with pytest.raises(web_exceptions.HTTPError):
            await api.get_all()


# ---------------------------------------------------------------------------
# Tests: HiveApiAsync.get_devices / get_products / get_actions
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests: HiveApiAsync.set_state
# ---------------------------------------------------------------------------


class TestSetState:
    async def test_file_in_use_returns_file_response(self):
        api = _make_api(status=200, file_mode=True)
        result = await api.set_state("heating", "node-1", mode="MANUAL")
        assert result == {"original": "file"}

    async def test_successful_set_state(self):
        payload = {"id": "node-1", "mode": "MANUAL"}
        api = _make_api(status=200, json_data=payload)
        result = await api.set_state("heating", "node-1", mode="MANUAL")
        assert result["original"] == 200
        assert result["parsed"] == payload

    async def test_os_error_calls_error_method(self):
        api = _make_api(status=200)
        api.websession.request.side_effect = OSError("fail")
        with pytest.raises(web_exceptions.HTTPError):
            await api.set_state("heating", "node-1", mode="MANUAL")

    async def test_runtime_error_calls_error_method(self):
        api = _make_api(status=200)
        api.websession.request.side_effect = RuntimeError("fail")
        with pytest.raises(web_exceptions.HTTPError):
            await api.set_state("heating", "node-1", mode="MANUAL")


# ---------------------------------------------------------------------------
# Tests: HiveApiAsync.set_action
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests: HiveApiAsync holiday mode
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests: HiveApiAsync.motion_sensor
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests: HiveApiAsync.error
# ---------------------------------------------------------------------------


class TestError:
    async def test_error_raises_http_error(self):
        api = _make_api()
        with pytest.raises(web_exceptions.HTTPError):
            await api.error()


# ---------------------------------------------------------------------------
# Tests: HiveApiAsync.is_file_being_used
# ---------------------------------------------------------------------------


class TestIsFileBeingUsed:
    async def test_file_mode_raises_file_in_use(self):
        api = _make_api(file_mode=True)
        with pytest.raises(FileInUse):
            await api.is_file_being_used()

    async def test_not_file_mode_does_not_raise(self):
        api = _make_api(file_mode=False)
        await api.is_file_being_used()  # Should not raise


# ---------------------------------------------------------------------------
# Tests: HiveApiAsync.__init__
# ---------------------------------------------------------------------------


class TestInit:
    def test_no_websession_created_at_init(self):
        """No ClientSession is created in the sync constructor."""
        session = MagicMock()
        session.tokens = MagicMock()
        session.tokens.token_data = {"token": "tok"}
        session.config = MagicMock()
        api = HiveApiAsync(hive_session=session)
        assert api.websession is None

    def test_websession_created_lazily_and_cached(self):
        """The first _get_websession() call creates and caches a ClientSession."""
        session = MagicMock()
        api = HiveApiAsync(hive_session=session)
        with patch("apyhiveapi.api.hive_async_api.ClientSession") as mock_session_cls:
            first = api._get_websession()
            second = api._get_websession()
        mock_session_cls.assert_called_once()
        assert first is mock_session_cls.return_value
        assert second is first
        assert api.websession is first

    def test_custom_websession_is_used(self):
        session = MagicMock()
        ws = MagicMock()
        api = HiveApiAsync(hive_session=session, websession=ws)
        assert api.websession is ws

    def test_base_url_is_set(self):
        api = _make_api()
        assert api.base_url == "https://beekeeper.hivehome.com/1.0"

    def test_default_timeout(self):
        api = _make_api()
        assert api.timeout == 5


# ---------------------------------------------------------------------------
# Migrated from test_hive_async_api_extended.py
# ---------------------------------------------------------------------------


class TestRequestNonAuthErrorBranch:
    """Cover lines 100-108: url/status not None branch leading to HiveApiError."""

    async def test_404_logs_and_raises_hive_api_error(self):
        """A 404 falls through to the url/status branch and raises HiveApiError."""
        api = _make_api(status=404)
        with pytest.raises(HiveApiError):
            await api.request("get", "https://beekeeper.hivehome.com/1.0/nodes/all")

    async def test_503_logs_and_raises_hive_api_error(self):
        """A 503 falls through to the url/status branch and raises HiveApiError."""
        api = _make_api(status=503)
        with pytest.raises(HiveApiError):
            await api.request("get", "https://beekeeper.hivehome.com/1.0/nodes/all")

    async def test_422_logs_and_raises_hive_api_error(self):
        """A 422 also falls through (not 401/403) and raises HiveApiError."""
        api = _make_api(status=422)
        with pytest.raises(HiveApiError):
            await api.request("get", "https://beekeeper.hivehome.com/1.0/devices")


class TestSetStateJsonEncoding:
    """set_state must produce valid JSON even when kwarg values contain special characters."""

    async def test_set_state_escapes_quotes_in_value(self):
        """A value containing double-quotes must produce valid, parseable JSON."""
        session = MagicMock()
        session.tokens.token_data = {"token": "tok"}
        session.config.file = False
        api = HiveApiAsync(hive_session=session)
        api.urls = {"nodes": "https://beekeeper.hivehome.com/1.0/nodes/{}/{}"}

        captured = {}

        async def fake_request(_method, _url, **kwargs):
            captured["data"] = kwargs.get("data")
            resp = MagicMock()
            resp.status = 200
            resp.json = AsyncMock(return_value={})
            return resp

        with patch.object(api, "request", side_effect=fake_request):
            with patch.object(api, "is_file_being_used", new=AsyncMock()):
                await api.set_state("heating", "node-1", mode='MANUAL"injected')

        parsed = json.loads(captured["data"])
        assert parsed["mode"] == 'MANUAL"injected'


# ---------------------------------------------------------------------------
# Tests: HiveApiAsync.get_login_info
# ---------------------------------------------------------------------------

_SSO_PAGE = (
    "<html><head><script>"
    'window.HiveSSOPoolId="eu-west-1_abc",'
    'window.HiveSSOPublicCognitoClientId="client123"'
    "</script></head></html>"
)
_SSO_LOGIN_DATA = {
    "UPID": "eu-west-1_abc",
    "CLIID": "client123",
    "REGION": "eu-west-1_abc",
}


def _make_page_websession(html: str, status: int = 200):
    """Return a mock websession whose GET yields *html*."""
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=html)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    websession = MagicMock()
    websession.get.return_value = resp
    return websession


class TestGetLoginInfo:
    async def test_successful_parse_returns_login_data(self):
        """Parses HiveSSOPoolId and HiveSSOPublicCognitoClientId from the SSO page."""
        api = HiveApiAsync(websession=_make_page_websession(_SSO_PAGE))

        result = await api.get_login_info()

        assert result == _SSO_LOGIN_DATA
        api.websession.get.assert_called_once()
        assert api.websession.get.call_args.args[0] == "https://sso.hivehome.com/"

    async def test_single_quoted_values_are_parsed(self):
        """Since 2026-10-20 the SSO page uses single quotes and spaces around =."""
        html = (
            "<script>\n"
            "        window.HiveSSOPoolId = 'eu-west-1_abc'\n"
            "        window.HiveSSOCognitoClientId = 'private123'\n"
            "        window.HiveSSOPublicCognitoClientId = 'client123'\n"
            "        window.HiveSSOState = '%%STATE%%'\n"
            "    </script>"
        )
        api = HiveApiAsync(websession=_make_page_websession(html))

        assert await api.get_login_info() == _SSO_LOGIN_DATA

    async def test_only_the_first_script_block_is_parsed(self):
        """Assignments in later script blocks are ignored, as before the port."""
        html = (
            "<script>window.Unrelated = 'x'</script>"
            "<script>window.HiveSSOPoolId = 'p' "
            "window.HiveSSOPublicCognitoClientId = 'c'</script>"
        )
        api = HiveApiAsync(websession=_make_page_websession(html))

        assert await api.get_login_info() is None

    async def test_mismatched_quotes_are_not_parsed(self):
        """A value must be closed by the same quote character it was opened with."""
        html = "<script>window.HiveSSOPoolId = 'eu-west-1_abc\"</script>"
        api = HiveApiAsync(websession=_make_page_websession(html))

        assert await api.get_login_info() is None

    async def test_missing_keys_return_none(self):
        """A script block without the Cognito keys yields None."""
        html = "<script>window.SomeOtherKey=value</script>"
        api = HiveApiAsync(websession=_make_page_websession(html))

        assert await api.get_login_info() is None

    async def test_page_without_script_returns_none(self):
        api = HiveApiAsync(websession=_make_page_websession("<html></html>"))

        assert await api.get_login_info() is None

    async def test_client_error_raises_connection_error(self):
        websession = _make_page_websession(_SSO_PAGE)
        websession.get.side_effect = aiohttp.ClientError("net error")
        api = HiveApiAsync(websession=websession)

        with pytest.raises(HiveConnectionError):
            await api.get_login_info()

    async def test_timeout_raises_connection_error(self):
        """A timeout on the SSO page is transient, so it must not return None."""
        websession = _make_page_websession(_SSO_PAGE)
        websession.get.side_effect = TimeoutError()
        api = HiveApiAsync(websession=websession)

        with pytest.raises(HiveConnectionError):
            await api.get_login_info()

    async def test_uses_a_temporary_session_when_none_is_configured(self):
        """Auth has no shared session, so a short-lived one is opened and closed."""
        temp_session = _make_page_websession(_SSO_PAGE)
        temp_session.__aenter__ = AsyncMock(return_value=temp_session)
        temp_session.__aexit__ = AsyncMock(return_value=False)
        api = HiveApiAsync()

        with patch(
            "apyhiveapi.api.hive_async_api.ClientSession", return_value=temp_session
        ) as session_cls:
            result = await api.get_login_info()

        assert result == _SSO_LOGIN_DATA
        session_cls.assert_called_once_with()
        temp_session.__aexit__.assert_awaited_once()
        assert api.websession is None
