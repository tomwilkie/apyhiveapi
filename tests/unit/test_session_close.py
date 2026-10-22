"""Tests for Hive.close() covering both branches of the websession guard."""

from unittest.mock import AsyncMock, MagicMock

from apyhiveapi.hive import Hive


class TestHiveClose:
    """Branch coverage for Hive.close() (line 79)."""

    async def test_close_calls_websession_close_when_not_already_closed(self):
        """close() calls websession.close() when websession is open (closed=False)."""
        session = object.__new__(Hive)
        session.api = MagicMock()
        session.api.websession.closed = False
        session.api.websession.close = AsyncMock()
        session._owns_websession = True

        await session.close()

        session.api.websession.close.assert_called_once()

    async def test_close_with_no_websession_does_not_raise(self):
        """close() is a no-op when the lazy websession was never created."""
        session = object.__new__(Hive)
        session.api = MagicMock()
        session.api.websession = None
        session._owns_websession = True

        await session.close()

    async def test_close_skips_websession_close_when_already_closed(self):
        """close() skips websession.close() when it is already closed."""
        session = object.__new__(Hive)
        session.api = MagicMock()
        session.api.websession.closed = True
        session.api.websession.close = AsyncMock()
        session._owns_websession = True

        await session.close()

        session.api.websession.close.assert_not_called()


class TestHiveCloseOwnership:
    """close() must not close a caller-provided websession."""

    async def test_close_does_not_close_caller_provided_websession(self):
        """When _owns_websession=False, close() must not close the websession."""
        session = object.__new__(Hive)
        session.api = MagicMock()
        session.api.websession.closed = False
        session.api.websession.close = AsyncMock()
        session._owns_websession = False

        await session.close()

        session.api.websession.close.assert_not_called()

    async def test_close_closes_owned_websession(self):
        """When _owns_websession=True, close() closes the websession as normal."""
        session = object.__new__(Hive)
        session.api = MagicMock()
        session.api.websession.closed = False
        session.api.websession.close = AsyncMock()
        session._owns_websession = True

        await session.close()

        session.api.websession.close.assert_called_once()
