"""Unit tests for closing the aiohttp session a Hive owns."""

# pylint: disable=protected-access

from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientSession

from apyhiveapi.hive import Hive


def _mock_client_session(*, closed=False):
    """Return a stand-in for an aiohttp ClientSession."""
    websession = MagicMock()
    websession.closed = closed
    websession.close = AsyncMock()
    return websession


class TestOwnedWebsession:
    """A Hive created without a websession owns the one the API creates."""

    def test_no_websession_is_created_up_front(self):
        assert Hive().api.websession is None

    async def test_close_without_a_websession_does_nothing(self):
        hive = Hive()
        await hive.close()
        assert hive.api.websession is None

    async def test_close_closes_the_lazily_created_websession(self):
        hive = Hive()
        websession = _mock_client_session()
        with patch("apyhiveapi.api.ClientSession", return_value=websession) as factory:
            assert hive.api._get_websession() is websession
            await hive.close()
        factory.assert_called_once_with()
        websession.close.assert_awaited_once_with()

    async def test_close_skips_an_already_closed_websession(self):
        hive = Hive()
        websession = _mock_client_session(closed=True)
        with patch("apyhiveapi.api.ClientSession", return_value=websession):
            hive.api._get_websession()
            await hive.close()
        websession.close.assert_not_awaited()

    async def test_async_with_closes_the_websession_on_exit(self):
        websession = _mock_client_session()
        with patch("apyhiveapi.api.ClientSession", return_value=websession):
            async with Hive() as hive:
                hive.api._get_websession()
                websession.close.assert_not_awaited()
        websession.close.assert_awaited_once_with()

    async def test_aenter_returns_the_session_itself(self):
        hive = Hive(username="use@file.com", password="")
        async with hive as entered:
            assert entered is hive

    async def test_a_real_websession_is_closed_only_once(self):
        hive = Hive()
        websession = hive.api._get_websession()
        assert isinstance(websession, ClientSession)
        assert not websession.closed
        await hive.close()
        assert websession.closed
        await hive.close()
        assert websession.closed


class TestGivenWebsession:
    """A websession passed in, as Home Assistant does, is never closed."""

    def test_the_given_websession_is_used(self):
        given = _mock_client_session()
        hive = Hive(websession=given)
        assert hive.api.websession is given
        assert hive.api._get_websession() is given

    async def test_close_leaves_the_given_websession_open(self):
        given = _mock_client_session()
        await Hive(websession=given).close()
        given.close.assert_not_awaited()

    async def test_async_with_leaves_the_given_websession_open(self):
        given = _mock_client_session()
        async with Hive(websession=given) as hive:
            assert hive.api.websession is given
        given.close.assert_not_awaited()
