"""Unit tests for pure SRP/HKDF crypto helpers — no mocking needed."""

import pytest

from hiveheating.srp import pad_hex


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
