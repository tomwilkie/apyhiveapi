"""Unit tests for the helpers module."""

import pytest

from apyhiveapi.helpers import sanitize_payload


class TestSanitizePayload:
    """Tests for sanitize_payload."""

    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "token",
            "secret",
            "code",
            "session",
            "device_data",
            "authenticationresult",
            "responsemetadata",
            "newdevicemetadata",
            "accessToken",
            "AuthenticationResult",
        ],
    )
    def test_every_sensitive_key_part_is_masked(self, key):
        """A key containing a sensitive part, in any case, has its value masked."""
        assert sanitize_payload({key: "hidden"})[key] == "***"

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("", id="empty"),
            pytest.param("abc", id="short"),
            pytest.param("12345678", id="eight-chars"),
            pytest.param("123456789", id="nine-chars"),
            pytest.param("x" * 1000, id="token-length"),
        ],
    )
    def test_every_string_becomes_stars_whatever_its_length(self, value):
        """No part of a sensitive string is kept, so its head and tail are hidden."""
        assert sanitize_payload({"token": value})["token"] == "***"

    def test_non_sensitive_key_passes_through(self):
        """Keys with no sensitive substrings are left unchanged."""
        result = sanitize_payload({"username": "user@test.com"})
        assert result["username"] == "user@test.com"

    def test_nested_dict_is_recursed_for_sensitive_key(self):
        """Sensitive key inside a nested dict is masked."""
        result = sanitize_payload({"outer": {"token": "abc"}})
        assert result["outer"]["token"] == "***"

    def test_list_items_are_masked_when_parent_key_is_sensitive(self):
        """List values under a sensitive key have each element masked."""
        result = sanitize_payload({"tokens": ["abc", "def"]})
        assert result["tokens"] == ["***", "***"]

    def test_original_payload_is_not_mutated(self):
        """sanitize_payload works on a deep copy, so the original must not change."""
        original = {"token": "supersecretvalue"}
        sanitize_payload(original)
        assert original["token"] == "supersecretvalue"

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(42, id="int"),
            pytest.param(None, id="none"),
            pytest.param(True, id="bool"),
        ],
    )
    def test_scalar_under_sensitive_key_passes_through(self, value):
        """Non-string, non-container values under sensitive keys are returned as-is."""
        assert sanitize_payload({"token": value})["token"] == value

    def test_dict_under_sensitive_key_is_recursively_masked(self):
        """A dict value under a sensitive key has its own values masked."""
        result = sanitize_payload({"token": {"inner_key": "secret_value"}})
        assert isinstance(result["token"], dict)
        assert "inner_key" in result["token"]
        assert result["token"]["inner_key"] == "***"

    def test_list_containing_dicts_is_walked_recursively(self):
        """A list of dicts under a non-sensitive key is recursively processed."""
        result = sanitize_payload(
            {
                "items": [
                    {"token": "abc", "name": "device1"},
                    {"token": "xyz", "name": "device2"},
                ]
            }
        )
        assert result["items"][0]["name"] == "device1"
        assert result["items"][0]["token"] == "***"
        assert result["items"][1]["name"] == "device2"
        assert result["items"][1]["token"] == "***"
