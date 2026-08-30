"""What a currency is for here, and what happens when there is no rate.

The currency on a search city does exactly one thing: it lets a bound be written
once in one currency (``max_price = "500 USD"``) and sent to a city that prices
in another.  That is a real use and the field stays -- but the list of codes the
monitor *accepts* used to be the list the converter has *rates* for, and those
are two different lists.  CLP was missing from both, so a Chilean city could not
name its own currency at all; ARS was in the enum and has never been in the
converter, so an Argentine one that did crashed the search the first time a
price needed converting.

These pin the split and the behaviour that falls out of it: an unconvertible
pair sends the number as written, with a warning, which is what happens when no
currency is named at all.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

import pytest

from ai_marketplace_monitor.config import Config
from ai_marketplace_monitor.facebook import FacebookMarketplace
from ai_marketplace_monitor.utils import Currency, convert_price, convertible_currencies


class Recorder(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def facebook() -> FacebookMarketplace:
    """A marketplace with a logger and nothing else; only ``_price_in`` is used."""
    marketplace = FacebookMarketplace.__new__(FacebookMarketplace)
    logger = logging.getLogger("test-currency")
    logger.handlers = []
    logger.setLevel(logging.DEBUG)
    logger.addHandler(Recorder())
    logger.propagate = False
    marketplace.logger = logger
    return marketplace


def recorded(marketplace: FacebookMarketplace) -> List[str]:
    return marketplace.logger.handlers[0].messages  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# The two lists
# --------------------------------------------------------------------------- #


def test_the_region_currencies_are_accepted() -> None:
    """The ones this monitor is actually pointed at.

    Mercado Libre alone is searched on seven Latin American sites, and none of
    those currencies could be named before.
    """
    for code in ("CLP", "ARS", "COP", "PEN", "UYU", "BRL", "MXN"):
        assert Currency(code).value == code


def test_none_of_them_is_convertible() -> None:
    """Which is the fact the old design had wrong, not a limitation to fix.

    The ECB publishes no reference rate for any of them; the enum claiming
    otherwise is what turned "name your city's currency" into a crash.
    """
    available = convertible_currencies()
    for code in ("CLP", "ARS", "COP", "PEN", "UYU"):
        assert code not in available
    assert "USD" in available and "EUR" in available


def test_convert_price_declines_rather_than_raising() -> None:
    assert convert_price(100, "USD", "CLP") is None
    assert convert_price(100, "CLP", "USD") is None
    assert convert_price(100, "USD", "EUR") is not None


def test_the_same_currency_needs_no_rate() -> None:
    """A CLP bound on a CLP city converts to itself and asks nobody."""
    assert convert_price(450000, "CLP", "CLP") == 450000
    assert convert_price(450000, "", "CLP") == 450000


# --------------------------------------------------------------------------- #
# What the search URL ends up carrying
# --------------------------------------------------------------------------- #


def test_a_plain_number_passes_through(facebook: FacebookMarketplace) -> None:
    assert facebook._price_in("600000", "CLP") == "600000"
    assert facebook._price_in("600000", "") == "600000"


def test_a_convertible_bound_is_converted(facebook: FacebookMarketplace) -> None:
    converted = facebook._price_in("500 USD", "EUR")
    assert converted.isdigit()
    assert converted != "500"


def test_an_unconvertible_bound_is_sent_as_written_with_a_warning(
    facebook: FacebookMarketplace,
) -> None:
    """The crash, replaced by a filter that is slightly wrong and says so.

    Slightly wrong is recoverable; a search that cannot assemble its own address
    is not, and that is what the unconditional conversion produced.
    """
    assert facebook._price_in("500 USD", "CLP") == "500"
    assert any("CLP" in message for message in recorded(facebook))


def test_no_city_currency_means_no_conversion(facebook: FacebookMarketplace) -> None:
    """The behaviour "sin especificar" has, unchanged and still the safe one."""
    assert facebook._price_in("500 USD", "") == "500"
    assert facebook._price_in("500 USD", None) == "500"
    assert recorded(facebook) == []


# --------------------------------------------------------------------------- #
# Through the config
# --------------------------------------------------------------------------- #


def test_a_chilean_city_can_name_its_currency(tmp_path: Path) -> None:
    """The whole reason CLP was added: this file used to be rejected."""
    path = tmp_path / "config.toml"
    path.write_text(
        """
[item.ps5]
search_phrases = "playstation 5"

[item.ps5.facebook]
search_city = "santiago"
currency = "CLP"
max_price = "600000"
""",
        encoding="utf-8",
    )
    config = Config([path])
    assert config.items[("facebook", "ps5")].currency == ["CLP"]


def test_an_unknown_code_is_still_refused(tmp_path: Path) -> None:
    """Only the list grew; the check did not go away."""
    path = tmp_path / "config.toml"
    path.write_text(
        """
[item.ps5]
search_phrases = "playstation 5"

[item.ps5.facebook]
search_city = "santiago"
currency = "GALLEONS"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="GALLEONS"):
        Config([path])
