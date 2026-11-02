"""Shared pytest fixtures."""

import pytest

from hiveheating import Hive


@pytest.fixture
async def file_session():
    """Hive session reading the bundled data.json fixture."""
    async with Hive(username="use@file.com", password="") as hive:
        hive.new_session({})
        yield hive
