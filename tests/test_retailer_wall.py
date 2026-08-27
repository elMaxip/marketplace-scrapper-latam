"""What happens when a shop stops serving us pages.

Lider is behind PerimeterX and refuses this scraper roughly half the time, and
until now nothing said so: the payload came back empty, the log said "did not
serve its results", and the next cycle asked again at full rate through the
wall.  Two things were indistinguishable and needed not to be -- "the shop is
refusing us", which is a fact about the whole pass, and "that page did not
load", which is a fact about one page.

The parser is deliberately not exercised here.  When Lider *is* served it parses
forty-eight results without complaint; every test below is about the other half
of the afternoon.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List

import pytest

from ai_marketplace_monitor import control
from ai_marketplace_monitor.lider import LiderMarketplace, LiderMarketplaceConfig
from ai_marketplace_monitor.marketplace import ItemConfig, ListingStatus
from ai_marketplace_monitor.sodimac import SodimacMarketplace, SodimacMarketplaceConfig

#: What the interstitial looks like: a wall-ish title and no product markup at
#: all, which is both halves of `extract.looks_blocked`.
WALL_HTML = "<html><head><title>Robot or human?</title></head><body></body></html>"

#: A page that failed for a duller reason.  No payload either -- and it must not
#: be mistaken for a refusal, because the answer to it is to try again rather
#: than to stop.
DULL_HTML = "<html><head><title>Lider.cl</title></head><body>vacío</body></html>"


@pytest.fixture(autouse=True)
def clean_control() -> Iterator[None]:
    control.reset_for_tests()
    yield
    control.reset_for_tests()


class FakePage:
    """Just enough of a Playwright page for the wall check."""

    def __init__(self, url: str = "https://www.lider.cl/search?query=x", body: str = "") -> None:
        self.url = url
        self.body = body
        self.visited: List[str] = []

    def content(self) -> str:
        return self.body

    def goto(self, url: str, timeout: int = 0) -> None:
        self.visited.append(url)

    def wait_for_load_state(self, state: str) -> None:
        return None

    def evaluate(self, script: str) -> Any:
        # No `__NEXT_DATA__` on any of these pages, which is what a wall, an
        # error page and a redirect all look like from here.
        return None


def _lider(page: FakePage) -> LiderMarketplace:
    market = LiderMarketplace("lider", None)
    market.config = LiderMarketplaceConfig(name="lider")
    market.page = page
    return market


def _sodimac(page: FakePage) -> SodimacMarketplace:
    market = SodimacMarketplace("sodimac", None)
    market.config = SodimacMarketplaceConfig(name="sodimac")
    market.page = page
    return market


# --------------------------------------------------------------------------- #
# Telling a refusal from a page that did not load
# --------------------------------------------------------------------------- #


def test_the_block_page_is_recognised() -> None:
    market = _lider(FakePage(url="https://www.lider.cl/blocked?ref=abc"))
    assert market.blocked_reason() == "sent us to its bot check"


def test_a_bot_check_is_recognised_wherever_it_lives() -> None:
    # The path is today's deployment, not a contract.  A scraper that knew only
    # `/blocked` would go back to hammering the site the day it moved.
    market = _lider(FakePage(url="https://www.lider.cl/challenge/xyz", body=WALL_HTML))
    assert market.blocked_reason() == "served a bot check"


def test_a_page_that_merely_failed_is_not_a_refusal() -> None:
    market = _lider(FakePage(body=DULL_HTML))
    assert market.blocked_reason() is None


def test_a_shop_with_no_wall_of_its_own_says_nothing() -> None:
    # The base answer, and the one that keeps the old behaviour exactly.
    assert _sodimac(FakePage(body=WALL_HTML)).blocked_reason() is None


# --------------------------------------------------------------------------- #
# What the monitor does about it
# --------------------------------------------------------------------------- #


def test_a_refusal_starts_a_cooldown() -> None:
    market = _lider(FakePage(url="https://www.lider.cl/blocked?ref=abc"))
    assert market.open_payload("https://www.lider.cl/search?query=bicicleta") is None
    assert control.marketplace_blocked("lider") is True


def test_a_page_that_did_not_load_starts_no_cooldown() -> None:
    market = _lider(FakePage(body=DULL_HTML))
    assert market.open_payload("https://www.lider.cl/search?query=bicicleta") is None
    assert control.marketplace_blocked("lider") is False


def test_consecutive_refusals_back_off_further() -> None:
    # A site still refusing after fifteen minutes is not going to be talked
    # round by asking again in another fifteen.  Cooldowns apart, so these are
    # two separate refusals rather than one still in force.
    market = _lider(FakePage(url="https://www.lider.cl/blocked"))
    market.open_payload("https://www.lider.cl/search?query=a")
    first = control.marketplace_block("lider") or {}
    control.clear_marketplace_block("lider")
    control.block_marketplace("lider", reason="x", seconds=0)
    market.open_payload("https://www.lider.cl/search?query=b")
    second = control.marketplace_block("lider") or {}
    assert second["seconds"] > first["seconds"]


def test_a_walled_pass_is_one_strike_not_forty_eight() -> None:
    # The search keeps opening product pages after the wall goes up -- on
    # purpose, the card is still returned for each -- and every one of them is
    # refused too.  Counted separately, one walled pass took Lider from fifteen
    # minutes to the four-hour ceiling with 48 strikes on the board.
    market = _lider(FakePage(url="https://www.lider.cl/blocked"))
    for index in range(20):
        market.open_payload(f"https://www.lider.cl/ip/x/{index}")
    block = control.marketplace_block("lider") or {}
    assert block["strikes"] == 1
    assert len(control.take_new_blocks()) == 1


def test_a_search_inside_a_cooldown_asks_for_nothing() -> None:
    # The whole point: a refused shop was being hit at full rate every cycle,
    # which is the surest way to stay refused.
    page = FakePage(url="https://www.lider.cl/blocked")
    market = _lider(page)
    control.block_marketplace("lider", reason="served a bot check")
    item = ItemConfig(name="bici", search_phrases=["bicicleta"], marketplace="lider")
    assert list(market.search(item)) == []
    assert page.visited == []


def test_a_recheck_inside_a_cooldown_is_undecided_not_gone() -> None:
    # Being refused is evidence about us, not about the product.  Only evidence
    # deletes -- see `ListingStatus`.
    market = _lider(FakePage())
    control.block_marketplace("lider", reason="served a bot check")
    item = ItemConfig(name="bici", search_phrases=["bicicleta"], marketplace="lider")
    status, listing = market.recheck_listing("https://www.lider.cl/ip/x/1", item)
    assert status is ListingStatus.UNKNOWN
    assert listing is None


def test_a_page_that_comes_back_clears_the_cooldown(monkeypatch) -> None:
    # Worth more than any timer: the site has evidently forgiven us.
    payload: Dict[str, Any] = {"props": {"pageProps": {}}}

    class Served(FakePage):
        def evaluate(self, script: str) -> Any:
            return '{"props": {"pageProps": {}}}'

    market = _lider(Served())
    monkeypatch.setattr(market, "save_session", lambda: True)
    control.block_marketplace("lider", reason="served a bot check")
    assert market.open_payload("https://www.lider.cl/search?query=a") == payload
    assert control.marketplace_blocked("lider") is False


# --------------------------------------------------------------------------- #
# Telling somebody
# --------------------------------------------------------------------------- #
#
# It is the one failure that looks like nothing at all: the monitor is healthy,
# the searches run, and one platform quietly finds nothing for hours.  On a
# server nobody is watching the interface, which is exactly where it happens.


def test_a_refusal_is_queued_to_be_told_once() -> None:
    market = _lider(FakePage(url="https://www.lider.cl/blocked"))
    market.open_payload("https://www.lider.cl/search?query=a")
    told = control.take_new_blocks()
    assert [entry["marketplace"] for entry in told] == ["lider"]
    assert told[0]["reason"] == "sent us to its bot check"
    # Claimed, so a cooldown read on every poll is not a message on every poll.
    assert control.take_new_blocks() == []


def test_putting_a_marketplace_on_cooldown_by_hand_tells_nobody() -> None:
    # `block_marketplace` is also a plain state setter -- seeding a cooldown to
    # see what the loop does with it is not a site refusing anybody.
    control.block_marketplace("lider", reason="testing")
    assert control.take_new_blocks() == []
    assert control.marketplace_blocked("lider") is True
