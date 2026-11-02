"""Unit tests for starting a session from a config entry."""

# pylint: disable=protected-access

import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from hiveheating.exceptions import HiveReauthRequired
from hiveheating.hive import Hive, _load_fixture

_LOGGER_NAME = "hiveheating.hive"
_EPOCH = datetime.min.replace(tzinfo=UTC)
_TOKENS = {
    "token": "id-token",
    "accessToken": "access-token",
    "refreshToken": "refresh-token",
}


def _make_hive():
    """Return a live-mode Hive whose network calls are mocked."""
    hive = Hive(username="user@example.com", password="pw")
    hive.hive_refresh_tokens = AsyncMock()
    hive.api.get_all = AsyncMock()
    return hive


class TestLoadFixture:
    """Tests for _load_fixture."""

    def test_returns_the_parsed_fixture(self):
        nodes = _load_fixture()
        assert set(nodes) == {"user", "products", "devices"}
        assert isinstance(nodes["products"], list)
        assert isinstance(nodes["devices"], list)
        assert nodes["user"]["temperatureUnit"] == "C"


class TestConfigureFileMode:
    """Tests for Hive._configure_file_mode."""

    @pytest.mark.parametrize(
        ("username", "expected"),
        [("use@file.com", True), ("real@user.com", False), (None, False), ("", False)],
    )
    def test_only_the_magic_username_enables_file_mode(self, username, expected):
        hive = Hive()
        hive._configure_file_mode(username)
        assert hive.config.file is expected


class TestApplyDeviceData:
    """Tests for Hive._apply_device_data."""

    @pytest.mark.parametrize(
        "device_data",
        [
            pytest.param([], id="empty"),
            pytest.param(["group"], id="group-only"),
            pytest.param(["group", "key"], id="no-password"),
        ],
    )
    def test_fewer_than_three_values_raises_without_applying(self, device_data):
        hive = _make_hive()
        with pytest.raises(HiveReauthRequired):
            hive._apply_device_data(device_data)
        assert hive.auth.device_group_key is None
        assert hive.auth.device_key is None
        assert hive.auth.device_password is None

    def test_three_values_set_the_device_credentials(self):
        hive = _make_hive()
        hive._apply_device_data(["group", "key", "password"])
        assert hive.auth.device_group_key == "group"
        assert hive.auth.device_key == "key"
        assert hive.auth.device_password == "password"  # pragma: allowlist secret

    def test_a_saved_token_issue_time_is_ignored(self):
        hive = _make_hive()
        hive._apply_device_data(
            ["group", "key", "password", "2026-01-01T12:00:00+00:00"]
        )
        assert hive.auth.device_key == "key"
        assert hive.tokens.token_created == _EPOCH


class TestNewSession:
    """Tests for Hive.new_session."""

    @pytest.mark.parametrize(
        ("ctor_username", "config"),
        [
            pytest.param("use@file.com", {}, id="constructor-empty-config"),
            pytest.param("use@file.com", None, id="constructor-no-config"),
            pytest.param(None, {"username": "use@file.com"}, id="config-username"),
            pytest.param(
                "use@file.com",
                {"username": "use@file.com", "device_data": []},
                id="config-username-empty-device-data",
            ),
        ],
    )
    def test_file_mode_needs_no_tokens(self, ctor_username, config):
        hive = Hive(username=ctor_username, password="")
        hive.new_session(config)
        assert hive.config.file is True
        assert hive.tokens.token_data == {}
        assert hive.auth.device_key is None

    @pytest.mark.parametrize(
        "config",
        [
            pytest.param(None, id="none"),
            pytest.param({}, id="empty"),
            pytest.param(
                {
                    "username": "real@user.com",
                    "password": "secret",  # pragma: allowlist secret
                    "device_data": ["group", "key", "password"],
                },
                id="credentials-only",
            ),
        ],
    )
    def test_live_mode_without_tokens_raises_before_anything_is_applied(self, config):
        hive = _make_hive()
        with pytest.raises(HiveReauthRequired, match="no tokens"):
            hive.new_session(config)
        assert hive.auth.username == "user@example.com"
        assert hive.auth.password == "pw"  # pragma: allowlist secret
        assert hive.auth.device_key is None
        assert hive.tokens.token_data == {}

    def test_tokens_only_config_stores_tokens_and_keeps_credentials(self):
        hive = _make_hive()
        hive.new_session({"tokens": _TOKENS})
        assert hive.tokens.token_data == _TOKENS
        assert hive.tokens.token_created == _EPOCH
        assert hive.auth.username == "user@example.com"
        assert hive.auth.password == "pw"
        assert hive.auth.device_key is None

    def test_full_config_applies_credentials_and_device_data_without_a_request(
        self,
    ):
        hive = _make_hive()
        hive.new_session(
            {
                "tokens": _TOKENS,
                "username": "real@user.com",
                "password": "secret",  # pragma: allowlist secret
                "device_data": ["group", "key", "password"],
            }
        )
        assert hive.auth.username == "real@user.com"
        assert hive.auth.password == "secret"  # pragma: allowlist secret
        assert hive.auth.device_group_key == "group"
        assert hive.auth.device_key == "key"
        assert hive.auth.device_password == "password"  # pragma: allowlist secret
        hive.hive_refresh_tokens.assert_not_awaited()
        hive.api.get_all.assert_not_awaited()

    def test_debug_log_shows_no_part_of_the_credentials(self, caplog):
        hive = _make_hive()
        config = {
            "username": "user@example.com",
            "password": "PWHEADxxxxPWTAIL",  # pragma: allowlist secret
            "device_data": ["GKHEADxxxxGKTAIL", "DKHEADxxxxDKTAIL", "DPHEADxxxxDPTAIL"],
            "tokens": {
                "token": "IDHEADxxxxxxxxxxIDTAIL",
                "refreshToken": "RFHEADxxxxxxxxxxRFTAIL",
                "accessToken": "ACHEADxxxxxxxxxxACTAIL",
            },
        }
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            hive.new_session(config)
        assert "new_session - Config" in caplog.text
        assert "user@example.com" in caplog.text
        for value in ("PW", "GK", "DK", "DP", "ID", "RF", "AC"):
            assert f"{value}HEAD" not in caplog.text
            assert f"{value}TAIL" not in caplog.text
