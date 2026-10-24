"""Unit tests for the Device, SessionData, SessionTokens and SessionConfig models."""

from datetime import UTC, datetime, timedelta

from apyhiveapi.models import Device, SessionConfig, SessionData, SessionTokens


def _make_device(**kwargs):
    """Create a Device with sensible defaults for testing."""
    defaults = {
        "hive_id": "h1",
        "hive_name": "Test",
        "hive_type": "heating",
        "ha_type": "climate",
        "device_id": "d1",
        "device_name": "Test",
        "device_data": {"online": True},
    }
    defaults.update(kwargs)
    return Device(**defaults)


class TestDevice:
    """Tests for Device dataclass."""

    def test_optional_fields_default_to_none(self):
        d = _make_device()
        assert d.parent_device is None
        assert d.temperature_unit is None
        assert d.status is None
        assert d.min_temp is None
        assert d.max_temp is None
        assert d.ha_name == ""


class TestSessionData:
    """Tests for SessionData dataclass."""

    def test_defaults_are_independent_empty_dicts(self):
        first = SessionData()
        second = SessionData()
        first.products["p1"] = {}
        assert second.products == {}
        assert second.devices == {}
        assert second.user == {}


class TestSessionTokens:
    """Tests for SessionTokens dataclass."""

    def test_default_token_data_is_empty_dict(self):
        """Test token_data defaults to empty dict."""
        t = SessionTokens()
        assert t.token_data == {}

    def test_default_token_created_is_the_epoch(self):
        """Test token_created defaults to the earliest aware UTC datetime."""
        t = SessionTokens()
        assert t.token_created == datetime.min.replace(tzinfo=UTC)
        assert t.token_created.tzinfo is UTC

    def test_default_token_expiry_is_one_hour(self):
        """Test token_expiry defaults to 3600 seconds."""
        t = SessionTokens()
        assert t.token_expiry == timedelta(seconds=3600)


class TestSessionConfig:
    """Tests for SessionConfig dataclass."""

    def test_default_file_is_false(self):
        """Test file defaults to False."""
        c = SessionConfig()
        assert c.file is False

    def test_default_scan_interval_is_120s(self):
        """Test scan_interval defaults to 120 seconds."""
        c = SessionConfig()
        assert c.scan_interval == timedelta(seconds=120)

    def test_default_last_update_is_aware(self):
        """Test last_update defaults to an aware UTC datetime."""
        c = SessionConfig()
        assert c.last_update.tzinfo is UTC

    def test_username_stored(self):
        """Test username can be set and retrieved."""
        c = SessionConfig(username="user@example.com")
        assert c.username == "user@example.com"
