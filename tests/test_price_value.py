"""Tests for reading a number out of a scraped price string.

The case that drove this: Facebook renders CLP with a space as the thousands
separator, so a listing costs "450\u00a0000".  The parser that
:meth:`User._is_discounted` used to carry stripped plain spaces only, which left
the non-breaking one in place, so ``float()`` raised on every Chilean price and
both sides of the comparison collapsed to the same "very expensive" fallback --
no price drop was ever detected.
"""

from __future__ import annotations

import pytest

from ai_marketplace_monitor.user import User
from ai_marketplace_monitor.utils import price_value

NBSP = "\u00a0"
NARROW_NBSP = "\u202f"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Space-grouped thousands, in every space Facebook uses.
        ("450 000", 450000.0),
        (f"450{NBSP}000", 450000.0),
        (f"$1{NARROW_NBSP}500{NARROW_NBSP}000", 1500000.0),
        # Dot- and comma-grouped thousands.
        ("$100.000", 100000.0),
        ("$1.500.000", 1500000.0),
        ("1,500,000", 1500000.0),
        # Decimals: one or two trailing digits mean a decimal point.
        ("$100.50", 100.5),
        ("$1,234.56", 1234.56),
        ("$1.234,56", 1234.56),
        # Currency on either side.
        ("CLP 150 000", 150000.0),
        ("150.000 CLP", 150000.0),
        ("US$ 80", 80.0),
        # A discounted listing keeps the current price first.
        (f"180{NBSP}000 | $200{NBSP}000", 180000.0),
        # Text bleeding into the amount is ignored, the number is not.
        (f"atis500{NBSP}000", 500000.0),
        # Free is zero, not "no price".
        ("Gratis", 0.0),
        ("Free", 0.0),
        ("$0", 0.0),
        # Nothing to read.
        ("", None),
        (None, None),
        ("**unspecified**", None),
        ("Consultar", None),
    ],
)
def test_price_value(raw: str | None, expected: float | None) -> None:
    assert price_value(raw) == expected


def test_clp_price_keeps_its_magnitude() -> None:
    """The regression that mattered: the non-breaking space is not a separator
    between two numbers, and 450 thousand must not read as 450."""
    assert price_value(f"450{NBSP}000") == 450000.0


@pytest.mark.parametrize(
    ("old_price", "new_price", "discounted"),
    [
        # The Chilean case: both prices readable, so the drop is seen.
        (f"500{NBSP}000", f"450{NBSP}000", True),
        (f"450{NBSP}000", f"500{NBSP}000", False),
        (f"450{NBSP}000", f"450{NBSP}000", False),
        # A struck-through original on the new price does not hide the drop.
        (f"200{NBSP}000", f"180{NBSP}000 | $200{NBSP}000", True),
        # US formatting still works.
        ("$1,200.00", "$999.99", True),
        ("$999.99", "$1,200.00", False),
        # An unreadable price is treated as very expensive, as before: an old
        # one that cannot be read must not hide a real drop...
        ("**unspecified**", f"450{NBSP}000", True),
        # ...and a new one that cannot be read is not a drop.
        (f"450{NBSP}000", "**unspecified**", False),
        (None, None, False),
        # Beyond the old 999999999 ceiling, which compared as equal.
        ("2 000 000 000", "1 000 000 000", True),
    ],
)
def test_is_discounted(
    user: User, old_price: str | None, new_price: str | None, discounted: bool
) -> None:
    assert user._is_discounted(old_price, new_price) is discounted
