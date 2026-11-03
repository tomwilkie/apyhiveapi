"""Shared pytest fixtures."""

from unittest.mock import MagicMock

import pytest

from hiveheating import Hive


@pytest.fixture
async def file_session():
    """Hive session reading the bundled data.json fixture."""
    hive = Hive(MagicMock())
    hive.new_session({"username": "use@file.com"})
    return hive
