"""Tests for pulling prices out of the marketplace's raw price text.

Facebook renders CLP with a space as the thousands separator ("100 000").  A
pattern that only understands comma grouping reads that as two separate prices,
or -- worse, with a currency prefix -- keeps only the leading group and silently
records $100.000 as $100.
"""

from __future__ import annotations

import pytest

from ai_marketplace_monitor.utils import extract_price

NBSP = " "
NARROW_NBSP = " "


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Space-grouped thousands stay one price.
        ("100 000", "100 000"),
        ("$100 000", "$100 000"),
        (f"100{NBSP}000", f"100{NBSP}000"),
        (f"$1{NARROW_NBSP}500{NARROW_NBSP}000", f"$1{NARROW_NBSP}500{NARROW_NBSP}000"),
        # Dot- and comma-grouped thousands.
        ("$100.000", "$100.000"),
        ("100.000", "100.000"),
        ("$1.500.000", "$1.500.000"),
        ("$1,234.56", "$1,234.56"),
        # Decimals survive.
        ("$100.50", "$100.50"),
        # Currency codes.
        ("CLP 150 000", "CLP 150 000"),
        ("150.000 CLP", "150.000 CLP"),
        # Bare numbers.
        ("$0", "$0"),
        ("110", "110"),
    ],
)
def test_single_price_is_not_split(raw: str, expected: str) -> None:
    assert extract_price(raw) == expected


def test_two_concatenated_prices_are_separated() -> None:
    """Facebook runs the current and struck-through original together."""
    assert extract_price("$80.000$90.000") == "$80.000 | $90.000"


def test_only_the_first_two_prices_are_kept() -> None:
    assert extract_price("$10.000$20.000$30.000") == "$10.000 | $20.000"


@pytest.mark.parametrize("raw", ["", "**unspecified**", "Gratis", "Free"])
def test_non_prices_pass_through(raw: str) -> None:
    assert extract_price(raw) == raw


def test_space_grouped_price_keeps_its_magnitude() -> None:
    """The regression that mattered: $100 000 must not become $100."""
    assert "000" in extract_price("$100 000")
