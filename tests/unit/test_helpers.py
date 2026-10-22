"""Unit tests for the helpers module."""

from apyhiveapi.helpers import sanitize_payload


class TestSanitizePayload:
    """Tests for sanitize_payload."""

    def test_masks_password_key(self):
        """Keys containing 'password' are masked in the output."""
        _pw = "s3cr3t-v@lue-for-test"  # pragma: allowlist secret
        result = sanitize_payload({"password": _pw})
        assert result["password"] != _pw

    def test_short_value_becomes_stars(self):
        """Values of 8 characters or fewer are replaced with '***'."""
        result = sanitize_payload({"token": "abc"})
        assert result["token"] == "***"

    def test_long_value_shows_head_and_tail(self):
        """Values over 8 characters are replaced with first4...last4."""
        result = sanitize_payload({"token": "abcdefghijklmnop"})
        assert result["token"].startswith("abcd")
        assert result["token"].endswith("mnop")
        assert "..." in result["token"]

    def test_exactly_8_chars_becomes_stars(self):
        """Boundary: an 8-character value becomes '***'."""
        result = sanitize_payload({"token": "12345678"})
        assert result["token"] == "***"

    def test_nine_chars_shows_head_and_tail(self):
        """Boundary: a 9-character value is truncated."""
        result = sanitize_payload({"token": "123456789"})
        assert result["token"].startswith("1234")
        assert result["token"].endswith("6789")

    def test_non_sensitive_key_passes_through(self):
        """Keys with no sensitive substrings are left unchanged."""
        result = sanitize_payload({"username": "user@test.com"})
        assert result["username"] == "user@test.com"

    def test_nested_dict_is_recursed_for_sensitive_key(self):
        """Sensitive key inside a nested dict is masked."""
        result = sanitize_payload({"outer": {"token": "abc"}})
        assert result["outer"]["token"] == "***"

    def test_nested_dict_non_sensitive_passes_through(self):
        """Non-sensitive key inside a nested dict is left unchanged."""
        result = sanitize_payload({"outer": {"name": "living room"}})
        assert result["outer"]["name"] == "living room"

    def test_list_items_are_masked_when_parent_key_is_sensitive(self):
        """List values under a sensitive key have each element masked."""
        result = sanitize_payload({"tokens": ["abc", "def"]})
        assert result["tokens"] == ["***", "***"]

    def test_original_payload_is_not_mutated(self):
        """sanitize_payload works on a deep copy, so the original must not change."""
        original = {"token": "supersecretvalue"}
        sanitize_payload(original)
        assert original["token"] == "supersecretvalue"

    def test_secret_key_is_masked(self):
        """Keys containing 'secret' are masked in the output."""
        _val = "s3cr3t-v@lue-for-test"  # pragma: allowlist secret
        result = sanitize_payload({"secret": _val})
        assert result["secret"] != _val

    def test_code_key_is_masked(self):
        """Keys containing 'code' are masked; short values become '***'."""
        result = sanitize_payload({"code": "123456"})
        assert result["code"] == "***"

    def test_non_string_value_under_sensitive_key_passes_through(self):
        """Non-string, non-container values under sensitive keys are returned as-is."""
        _non_string_int = 42
        result = sanitize_payload({"token": _non_string_int})
        assert result["token"] == _non_string_int

    def test_dict_under_sensitive_key_is_recursively_masked(self):
        """A dict value under a sensitive key has its own values masked."""
        result = sanitize_payload({"token": {"inner_key": "secret_value"}})
        assert isinstance(result["token"], dict)
        assert "inner_key" in result["token"]
        assert result["token"]["inner_key"] != "secret_value"

    def test_list_value_under_sensitive_key_masks_each_element(self):
        """A list under a sensitive key has each element masked individually."""
        payload = {"token": ["short", "averylongtoken123"]}
        result = sanitize_payload(payload)
        assert result["token"] == ["***", "aver...n123"]

    def test_none_under_sensitive_key_passes_through(self):
        """None under a sensitive key is returned unchanged."""
        assert sanitize_payload({"token": None})["token"] is None

    def test_bool_under_sensitive_key_passes_through(self):
        """A bool under a sensitive key is returned unchanged."""
        assert sanitize_payload({"token": True})["token"] is True

    def test_list_under_non_sensitive_key_is_walked(self):
        """A list value under a non-sensitive key is returned element by element."""
        result = sanitize_payload({"devices": ["device-a", "device-b"]})
        assert result == {"devices": ["device-a", "device-b"]}

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
        assert result["items"][0]["token"] != "abc"
        assert result["items"][1]["name"] == "device2"
        assert result["items"][1]["token"] != "xyz"
