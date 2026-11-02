"""Unit tests for closing the aiohttp session a Hive owns."""

# pylint: disable=protected-access

from unittest.mock import AsyncMock, MagicMock, patch

from hiveheating.hive import Hive


def _mock_client_session(*, closed=False):
    """Return a stand-in for an aiohttp ClientSession."""
    websession = MagicMock()
    websession.closed = closed
    websession.close = AsyncMock()
    return websession


def _hive():
    """Return a Hive with a token whose Hive API requests are mocked."""
    hive = Hive()
    hive.tokens.token_data = {"token": "id-token"}
    return hive


class TestOwnedWebsession:
    """A Hive created without a websession owns the one its requests create."""

    async def test_no_websession_is_created_up_front_or_by_close(self):
        hive = Hive()
        assert hive.websession is None
        await hive.close()
        assert hive.websession is None

    async def test_requests_share_the_lazily_created_websession(self):
        hive = _hive()
        websession = _mock_client_session()
        with (
            patch("hiveheating.hive.ClientSession", return_value=websession) as factory,
            patch("hiveheating.hive.request", new=AsyncMock()) as request,
        ):
            await hive._request("get", "/nodes/all")
            await hive._request("get", "/nodes/all")
        factory.assert_called_once_with()
        assert [c.args[0] for c in request.await_args_list] == [websession, websession]
        assert hive.websession is websession

    async def test_close_closes_the_lazily_created_websession(self):
        hive = _hive()
        websession = _mock_client_session()
        with (
            patch("hiveheating.hive.ClientSession", return_value=websession),
            patch("hiveheating.hive.request", new=AsyncMock()),
        ):
            await hive._request("get", "/nodes/all")
            await hive.close()
        websession.close.assert_awaited_once_with()

    async def test_close_skips_an_already_closed_websession(self):
        hive = _hive()
        websession = _mock_client_session(closed=True)
        with (
            patch("hiveheating.hive.ClientSession", return_value=websession),
            patch("hiveheating.hive.request", new=AsyncMock()),
        ):
            await hive._request("get", "/nodes/all")
            await hive.close()
        websession.close.assert_not_awaited()

    async def test_async_with_closes_the_websession_on_exit(self):
        hive = _hive()
        websession = _mock_client_session()
        with (
            patch("hiveheating.hive.ClientSession", return_value=websession),
            patch("hiveheating.hive.request", new=AsyncMock()),
        ):
            async with hive as entered:
                assert entered is hive
                await hive._request("get", "/nodes/all")
                websession.close.assert_not_awaited()
        websession.close.assert_awaited_once_with()


class TestGivenWebsession:
    """A websession passed in, as Home Assistant does, is never closed."""

    async def test_close_leaves_the_given_websession_open(self):
        given = _mock_client_session()
        await Hive(websession=given).close()
        given.close.assert_not_awaited()
