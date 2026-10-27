"""Unit tests for pure SRP/HKDF crypto helpers — no mocking needed."""

import pytest

from apyhiveapi.srp import hash_sha256, hex_hash, hex_to_long, long_to_hex, pad_hex

HELLO_SHA256 = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def test_hash_sha256_and_hex_hash_known_answer():
    """hash_sha256 hashes bytes; hex_hash hashes the bytes a hex string encodes."""
    assert hash_sha256(b"hello") == HELLO_SHA256
    assert hex_hash("68656c6c6f") == HELLO_SHA256


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, "01"), (255, "00ff"), (0x1A, "1a"), ("abc", "0abc"), ("ff", "00ff")],
    ids=[
        "int-odd",
        "int-high-nibble",
        "int-even-low-nibble",
        "str-odd",
        "str-high-nibble",
    ],
)
def test_pad_hex(value, expected):
    """pad_hex gives an even length and a 00 prefix when the high nibble is set."""
    assert pad_hex(value) == expected


@pytest.mark.parametrize("value", [1, 255, 256, 65535, 2**32])
def test_long_to_hex_and_back_is_identity(value):
    """hex_to_long(long_to_hex(n)) == n for positive integers."""
    assert hex_to_long(long_to_hex(value)) == value
