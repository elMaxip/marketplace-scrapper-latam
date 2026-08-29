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

import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from ai_marketplace_monitor import control, retailer, session
from ai_marketplace_monitor.lider import LiderMarketplace, LiderMarketplaceConfig
from ai_marketplace_monitor.marketplace import ItemConfig, ListingStatus
from ai_marketplace_monitor.sodimac import (
    SodimacItemConfig,
    SodimacMarketplace,
    SodimacMarketplaceConfig,
)

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


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No real sleeping here.

    A search paces itself between results pages and between product pages, and
    walking a catalogue in a test would spend that time for real -- fifty
    seconds of this file waiting. The pacing has `test_pacing.py` to itself;
    these tests are about what the search decides, not how long it waits.

    The delay is replaced rather than switched off, because `HUMAN_PACING=False`
    means "do not *vary* the wait" and still waits the nominal amount -- which
    was four seconds a test, and is exactly the trap this comment exists to stop
    the next person falling into.
    """
    from ai_marketplace_monitor import retailer as retailer_module

    monkeypatch.setattr(retailer_module, "human_delay", lambda seconds: 0.0)
    monkeypatch.setattr(retailer_module, "human_scroll", lambda *a, **k: False)
    yield


@pytest.fixture(autouse=True)
def temp_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the session files at a temporary directory.

    `_hit_wall` now rewrites the stored session to drop the device identity the
    shop has just refused, and most of the tests below drive `_hit_wall` under
    the real marketplace name.  Without this they edit the user's own
    `~/.ai-marketplace-monitor/sessions/lider.json` -- which is exactly what
    happened once, and got away with it only because the cookies those tests
    removed were ones that genuinely had to go.  A `challenge_cookies` list with
    a login name in it would have destroyed a session the user pasted by hand
    and cannot get back without pasting it again.
    """
    monkeypatch.setattr(session, "SESSION_DIR", tmp_path / "sessions")
    yield


@pytest.fixture(autouse=True)
def no_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep these tests out of the real caches, in both directions.

    A search reads and writes two of them, and the store behind both is one
    diskcache under the user's own home -- shared by the whole session and by
    the running monitor:

    * the observation log, asked by `is_known` before a product page is opened
      and appended to for every entry examined;
    * the listing-details cache, which `get_listing_details` consults *before*
      it navigates, so a product page recorded by an earlier run is served from
      disk and the navigation these tests are counting never happens.

    Both bit: the tests wrote invented Lider ids into the user's real store, and
    then failed on the next run against the entries they had left there.
    """
    from ai_marketplace_monitor import retailer as retailer_module

    monkeypatch.setattr(retailer_module, "is_known", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(retailer_module, "record_observation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        retailer_module.Listing, "from_cache", classmethod(lambda *_a, **_k: None)
    )
    monkeypatch.setattr(retailer_module.Listing, "to_cache", lambda *_a, **_k: None)
    yield


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


def _walled_search(market: LiderMarketplace, phrase: str = "bicicleta") -> None:
    """Run a search that the shop refuses, to the point where it gives up.

    Through `search()` rather than `open_payload()`, which is the contract now:
    detecting a wall and reacting to it were deliberately separated, because
    what to do about a refusal -- open a fresh browser and retry, or wait --
    depends on whether this search has already had its retry, and only the
    search knows that.  These tests are about the second half.
    """
    list(market.search(ItemConfig(name=phrase, search_phrases=[phrase], marketplace="lider")))


def test_a_refusal_starts_a_cooldown() -> None:
    market = _lider(FakePage(url="https://www.lider.cl/blocked?ref=abc"))
    _walled_search(market)
    assert control.marketplace_blocked("lider") is True


def test_a_page_that_did_not_load_starts_no_cooldown() -> None:
    market = _lider(FakePage(body=DULL_HTML))
    _walled_search(market)
    assert control.marketplace_blocked("lider") is False


def test_consecutive_refusals_back_off_further() -> None:
    # A site still refusing after fifteen minutes is not going to be talked
    # round by asking again in another fifteen.  Cooldowns apart, so these are
    # two separate refusals rather than one still in force.
    market = _lider(FakePage(url="https://www.lider.cl/blocked"))
    _walled_search(market, "a")
    first = control.marketplace_block("lider") or {}
    control.clear_marketplace_block("lider")
    control.block_marketplace("lider", reason="x", seconds=0)
    _walled_search(market, "b")
    second = control.marketplace_block("lider") or {}
    assert second["seconds"] > first["seconds"]


def test_a_walled_pass_is_one_strike_not_forty_eight() -> None:
    # The search keeps opening product pages after the wall goes up -- on
    # purpose, the card is still returned for each -- and every one of them is
    # refused too.  Counted separately, one walled pass took Lider from fifteen
    # minutes to the four-hour ceiling with 48 strikes on the board.
    market = _lider(FakePage(url="https://www.lider.cl/blocked"))
    for index in range(20):
        market.open_payload(f"https://www.lider.cl/ip/x/{index}", is_the_page_we_came_for=False)
    market._hit_wall("sent us to its bot check")
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
    _walled_search(market, "a")
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


# --------------------------------------------------------------------------- #
# A wall that arrives with a payload
# --------------------------------------------------------------------------- #
#
# The blind spot: `open_payload` only asks `blocked_reason` when there was no
# payload at all.  Both shops are Next.js applications and so is the wall each
# of them serves, so a refusal can carry a perfectly good `__NEXT_DATA__` with
# no products in it -- which was reported as "0 result(s)", indistinguishable
# from a shop that sells none of it, and left the cooldown switched off so the
# next cycle went straight back through the wall.


class PayloadPage(FakePage):
    """A page that answers with a payload the parser can read and find nothing in."""

    #: No browser behind it, so the session save `open_payload` does on a served
    #: page has nothing to write and says so instead of reaching for a context.
    context = None

    def evaluate(self, script: str) -> Any:
        return "{}"


def test_a_walled_results_page_with_a_payload_is_a_refusal() -> None:
    page = PayloadPage(url="https://www.lider.cl/blocked?ref=abc", body=WALL_HTML)
    market = _lider(page)
    item = ItemConfig(name="tv", search_phrases=["tv"], marketplace="lider")

    assert list(market.search(item)) == []
    assert control.marketplace_blocked("lider") is True


def test_an_empty_catalogue_is_not_a_refusal() -> None:
    """The other half, and the reason the check is `blocked_reason` and not
    "found nothing": a shop that genuinely sells none of it must not be put in
    a cooldown for saying so."""
    page = PayloadPage(url="https://www.lider.cl/search?query=x", body=DULL_HTML)
    market = _lider(page)
    item = ItemConfig(name="tv", search_phrases=["tv"], marketplace="lider")

    assert list(market.search(item)) == []
    assert control.marketplace_blocked("lider") is False


# --------------------------------------------------------------------------- #
# A product page is not the page the search came for
# --------------------------------------------------------------------------- #
#
# The shape of a bad afternoon on both shops is not "the shop refuses us".  It
# is: the results grid is served, forty-odd cards come back, and the first
# product page opened for a description is the wall.  Treating that as a refusal
# put the whole platform on a fifteen-minute cooldown and skipped the *next*
# search — the one thing that was still working — which is how Lider ended up
# alternating between "found everything" and "0 new listings" for ever.


SEARCH_URL = "https://www.lider.cl/search?query=tv"

def grid_of(*listing_ids: str) -> Dict[str, Any]:
    """A results grid, as `lider.parse_search` reads it.

    The ids are per test on purpose: the observation store is a real diskcache
    shared by the whole session, so a listing one test recorded is `is_known`
    to the next one and would be skipped before the wall was ever reached.
    """
    return {
        "props": {
            "pageProps": {
                "initialData": {
                    "searchResult": {
                        "itemStacks": [
                            {
                                "items": [
                                    {
                                        "usItemId": listing_id,
                                        "name": "Televisor SMART TV 50",
                                        "canonicalUrl": f"/ip/tv/televisor/{listing_id}",
                                        "priceInfo": {"linePrice": "$299.990"},
                                    }
                                    for listing_id in listing_ids
                                ]
                            }
                        ]
                    }
                }
            }
        }
    }


class GridServedProductWalled(FakePage):
    """The shop serves its results grid and walls every product page."""

    context = None

    #: Whether product pages are walled.  A subclass flips it to serve them.
    walls_products = True

    def __init__(self, *listing_ids: str) -> None:
        super().__init__(url=SEARCH_URL, body=DULL_HTML)
        self.listing_ids = listing_ids
        self.products_opened = 0

    def goto(self, url: str, timeout: int = 0) -> None:
        self.visited.append(url)
        self.url = url
        if "/ip/" in url and self.walls_products:
            self.products_opened += 1
            self.body = WALL_HTML
        elif "/ip/" in url:
            self.products_opened += 1
            self.body = DULL_HTML

    def evaluate(self, script: str) -> Any:
        import json

        if "/ip/" not in self.url:
            return json.dumps(grid_of(*self.listing_ids))
        if self.walls_products:
            return None
        return json.dumps(
            {"props": {"pageProps": {"initialData": {"data": {"product": {
                "usItemId": self.url.rsplit("/", 1)[-1],
                "name": "Televisor SMART TV 50",
                "availabilityStatus": "IN_STOCK",
            }}}}}}
        )


class GridAndProductsServed(GridServedProductWalled):
    """Nothing is walled: the shop is having a good afternoon."""

    walls_products = False


def _search(market: LiderMarketplace, **options: Any) -> List[Any]:
    """Run one search.

    ``keywords`` by default, because a search with nothing that reads the
    description does not open product pages at all any more (see
    `_description_decides`) -- and these tests are about what happens when it
    does.
    """
    options.setdefault("keywords", ["televisor"])
    item = ItemConfig(name="tv", search_phrases=["tv"], marketplace="lider", **options)
    return list(market.search(item))


def test_a_walled_product_page_does_not_cool_down_the_shop() -> None:
    market = _lider(GridServedProductWalled("wall-1"))
    assert len(_search(market)) == 1
    assert control.marketplace_blocked("lider") is False


def test_the_listing_survives_as_its_search_card() -> None:
    """§10: the batch that was already collected must not be thrown away."""
    market = _lider(GridServedProductWalled("wall-2"))
    (listing,) = _search(market)
    assert listing.title == "Televisor SMART TV 50"
    assert listing.price == "$299.990"
    assert listing.description == ""


def test_a_walled_search_page_still_cools_the_shop_down() -> None:
    """The other half: refused the page the search came for, there is nothing to
    salvage and nothing to gain by asking again soon."""
    page = PayloadPage(url="https://www.lider.cl/blocked?ref=abc", body=WALL_HTML)
    market = _lider(page)
    assert _search(market) == []
    assert control.marketplace_blocked("lider") is True


def test_the_shop_stops_being_asked_for_product_pages_after_the_first_wall() -> None:
    """Forty product loads through a wall is the traffic that keeps a shop
    refusing, and every one of them returns the card anyway."""
    page = GridServedProductWalled("wall-3a", "wall-3b", "wall-3c")
    market = _lider(page)
    assert len(_search(market)) == 3
    # The first one finds the wall; the other two are taken from the grid
    # without a navigation.
    assert page.products_opened == 1


def test_a_served_page_forgives_the_product_wall() -> None:
    """Cleared by any page that comes back, so the next search's own grid
    clears it and nothing has to remember to reset it."""
    market = _lider(GridAndProductsServed("ok-1"))
    market._products_walled = "served a bot check"
    _search(market)
    assert market._products_walled is None


def test_the_review_gets_to_try_a_walled_shop_again() -> None:
    """A marketplace object lives as long as its lane and the review never
    loads a results page, so nothing there would ever lift the flag: one walled
    product page would end re-checking on that shop for the life of the
    process."""
    market = _lider(GridServedProductWalled("review-1"))
    market._products_walled = "served a bot check"
    assert market.recheck_listing(
        "https://www.lider.cl/ip/tv/televisor/review-1",
        ItemConfig(name="tv", search_phrases=["tv"], marketplace="lider"),
    ) == (ListingStatus.UNKNOWN, None)

    market.forget_product_wall()
    assert market._products_walled is None


# --------------------------------------------------------------------------- #
# Not opening a page that decides nothing
# --------------------------------------------------------------------------- #
#
# The product page is opened for one thing: the description, which on a shop is
# a marketing blurb rather than a seller writing about their own object.  One
# per catalogue entry is nonetheless the bulk of a search's traffic and, on
# Lider, the exact requests the bot check refuses.


def test_a_search_that_reads_no_description_opens_no_product_pages() -> None:
    page = GridAndProductsServed("quiet-1", "quiet-2")
    market = _lider(page)
    listings = _search(market, keywords=None)
    assert len(listings) == 2
    assert page.products_opened == 0


def test_the_listings_are_still_complete_without_the_page() -> None:
    """Everything the grid publishes is on the card; only the description is
    not, and the review reads the page later anyway."""
    page = GridAndProductsServed("quiet-3")
    (listing,) = _search(_lider(page), keywords=None)
    assert listing.title == "Televisor SMART TV 50"
    assert listing.price == "$299.990"
    assert listing.post_url.endswith("/quiet-3")


def test_keywords_open_the_page() -> None:
    """The entry's fate depends on the description, so it has to be read."""
    page = GridAndProductsServed("kw-1")
    _search(_lider(page), keywords=["televisor"])
    assert page.products_opened == 1


def test_an_ai_opens_the_page() -> None:
    """The description is most of what a rating has to go on."""
    page = GridAndProductsServed("ai-1")
    _search(_lider(page), keywords=None, ai=["openai"])
    assert page.products_opened == 1


def test_in_stock_only_opens_the_page() -> None:
    """`availability` is not on either shop's grid."""
    page = GridAndProductsServed("stock-1")
    market = _lider(page)
    market.config = LiderMarketplaceConfig(name="lider", in_stock_only=True)
    _search(market, keywords=None)
    assert page.products_opened == 1


# --------------------------------------------------------------------------- #
# A refusal throws away the device identity that earned it
# --------------------------------------------------------------------------- #


class JarPage(PayloadPage):
    """A walled page whose context remembers what was cleared out of it."""

    def __init__(self, url: str, body: str) -> None:
        super().__init__(url=url, body=body)
        self.cleared: List[str] = []
        self.context = self  # its own context, which is all `_hit_wall` needs

    def clear_cookies(self, name: str) -> None:
        self.cleared.append(name)

    def storage_state(self) -> Dict[str, Any]:
        return {"cookies": [], "origins": []}


def test_a_refusal_clears_the_burned_device_id_from_the_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live half.  Without it the cooldown expires and the very next request
    arrives wearing the same flagged id."""
    from ai_marketplace_monitor import marketplace as marketplace_module

    monkeypatch.setattr(marketplace_module, "drop_cookies", lambda *_a, **_k: 0)
    page = JarPage(url="https://www.lider.cl/blocked?ref=abc", body=WALL_HTML)
    market = _lider(page)
    market.context = None
    _search(market)

    assert control.marketplace_blocked("lider") is True
    assert "_pxvid" in page.cleared
    assert "_px3" in page.cleared


def test_a_refusal_clears_it_from_the_stored_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half that decides what the next profile starts from."""
    from ai_marketplace_monitor import marketplace as marketplace_module

    asked: List[Any] = []
    monkeypatch.setattr(
        marketplace_module,
        "drop_cookies",
        lambda name, names: asked.append((name, tuple(names))) or 2,
    )
    page = JarPage(url="https://www.lider.cl/blocked?ref=abc", body=WALL_HTML)
    market = _lider(page)
    market.context = None
    _search(market)

    assert asked == [("lider", LiderMarketplace.challenge_cookies)]


def test_a_served_page_keeps_the_clearance(monkeypatch: pytest.MonkeyPatch) -> None:
    """While these cookies work they are worth more than any timer: a clearance
    that survives a discarded profile is the whole reason `save_session` exists.
    Dropping them on a good page would start every profile challenged again."""
    from ai_marketplace_monitor import marketplace as marketplace_module

    dropped: List[Any] = []
    monkeypatch.setattr(
        marketplace_module, "drop_cookies", lambda *a, **k: dropped.append(a) or 0
    )
    market = _lider(GridAndProductsServed("clear-1"))
    _search(market)
    assert dropped == []


def test_a_quiet_pass_is_not_reported_as_a_walled_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two reasons a listing arrives without its description, and only one is
    news.  Counted together they read as the alarming one: a log line saying
    "the product pages were walled" appeared on a pass where Lider had walled
    nothing and the monitor had simply, correctly, opened no pages."""
    market = _lider(GridAndProductsServed("quiet-log-1"))
    market.logger = logging.getLogger("test-retailer-wall")
    with caplog.at_level(logging.DEBUG):
        _search(market, keywords=None)
    assert "refused their product pages" not in caplog.text
    assert "nothing in this search needs their descriptions" in caplog.text


def test_a_walled_pass_says_so_as_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    page = GridServedProductWalled("walled-log-1", "walled-log-2")
    market = _lider(page)
    market.logger = logging.getLogger("test-retailer-wall")
    with caplog.at_level(logging.DEBUG):
        _search(market)
    assert "refused their product pages" in caplog.text


# --------------------------------------------------------------------------- #
# Recovering inside the search that hit the wall
# --------------------------------------------------------------------------- #
#
# Waiting fifteen minutes was the wrong first answer.  The evidence: a profile
# carrying an identity the wall has decided against is refused in about a
# second, and a profile with no history is served -- five probes out of five.
# So a refusal is answered with a new profile, in the same search, and the
# cooldown becomes the last resort rather than the first reaction.


class WalledUntilRenewed(FakePage):
    """Refuses everything until the browser is replaced, then serves."""

    context = None

    def __init__(self, listing_id: str = "renew-1") -> None:
        super().__init__(url="https://www.lider.cl/blocked", body=WALL_HTML)
        self.listing_id = listing_id
        self.serving = False
        self.renewals = 0

    def renew(self) -> "WalledUntilRenewed":
        self.renewals += 1
        self.serving = True
        self.url = SEARCH_URL
        self.body = DULL_HTML
        return self

    # The renewer hands back a *context*, and the marketplace then takes a page
    # on it.  One object stands in for both here, which is all `create_page`
    # needs: something with `pages` and `new_page`.

    @property
    def pages(self) -> List["WalledUntilRenewed"]:
        return [self]

    def new_page(self) -> "WalledUntilRenewed":
        return self

    def clear_cookies(self, name: str) -> None:
        return None

    def goto(self, url: str, timeout: int = 0) -> None:
        self.visited.append(url)
        self.url = url if self.serving else "https://www.lider.cl/blocked"

    def evaluate(self, script: str) -> Any:
        import json

        return json.dumps(grid_of(self.listing_id)) if self.serving else None


def _with_renewal(market: LiderMarketplace, page: WalledUntilRenewed) -> None:
    """Give the marketplace a renewer, the way a lane or the monitor does."""
    market.renew_browser = page.renew


def test_a_wall_is_answered_with_a_fresh_browser_not_a_wait() -> None:
    page = WalledUntilRenewed()
    market = _lider(page)
    _with_renewal(market, page)

    listings = _search(market)
    assert page.renewals == 1
    assert len(listings) == 1
    # The whole point: the search carried on and nothing was put to sleep.
    assert control.marketplace_blocked("lider") is False


def test_the_burned_identity_goes_before_the_new_profile_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise the fresh profile is reseeded from `sessions/` with the very
    identity that was just refused, and the exercise is pointless."""
    from ai_marketplace_monitor import marketplace as marketplace_module

    order: List[str] = []
    monkeypatch.setattr(
        marketplace_module, "drop_cookies", lambda *a, **k: order.append("dropped") or 1
    )
    page = WalledUntilRenewed()
    market = _lider(page)
    market.renew_browser = lambda: (order.append("renewed"), page.renew())[1]

    _search(market)
    assert order == ["dropped", "renewed"]


def test_only_one_fresh_browser_per_search() -> None:
    """Without the limit a shop that refuses everything becomes a loop of
    opening and closing browsers, which is the surest way to stay refused."""
    page = WalledUntilRenewed()
    page.renew = lambda: page  # renewed, and still walled
    market = _lider(page)
    market.renew_browser = page.renew

    _search(market)
    assert control.marketplace_blocked("lider") is True


def test_a_shop_that_refuses_the_fresh_browser_too_falls_back_to_waiting() -> None:
    renewals: List[int] = []

    def renew_but_stay_walled() -> Any:
        renewals.append(1)
        return page

    page = WalledUntilRenewed()
    market = _lider(page)
    market.renew_browser = renew_but_stay_walled

    _search(market)
    assert len(renewals) == 1
    assert control.marketplace_blocked("lider") is True


def test_without_a_renewer_it_behaves_exactly_as_before() -> None:
    """A marketplace nobody offered a renewer to -- the `--check` path, a test,
    anything holding one outside a lane -- must not break."""
    market = _lider(FakePage(url="https://www.lider.cl/blocked"))
    assert market.renew_browser is None
    _walled_search(market)
    assert control.marketplace_blocked("lider") is True


# --------------------------------------------------------------------------- #
# Walking a catalogue to its end
# --------------------------------------------------------------------------- #
#
# `max_pages` used to default to one, so nine tenths of a shop simply did not
# exist for the monitor.  Unset now means "until the shop runs out", and the
# interesting part is every way a shop can fail to say it has run out.


class Catalogue(FakePage):
    """A shop with a known number of pages, `pages_worth` entries each."""

    context = None
    #: Set to a number to publish `paginationV2.maxPage`, or None to stay quiet
    #: about it -- both are real, and they take different exits from the loop.
    announces_total: int | None = None
    #: True for a shop that accepts the page parameter and ignores it, which is
    #: Sodimac's `/lista` route to the letter.
    ignores_page_number = False

    def __init__(self, pages: int, per_page: int = 2) -> None:
        super().__init__(url=SEARCH_URL, body=DULL_HTML)
        self.pages_available = pages
        self.per_page = per_page
        self.requested: List[int] = []

    def goto(self, url: str, timeout: int = 0) -> None:
        self.visited.append(url)
        self.url = url
        number = 1
        if "&page=" in url:
            number = int(url.rsplit("&page=", 1)[1])
        self.requested.append(number)

    def evaluate(self, script: str) -> Any:
        import json

        number = 1 if self.ignores_page_number else self.requested[-1]
        if number > self.pages_available:
            return json.dumps(grid_of())
        first = (number - 1) * self.per_page
        ids = [f"p{number}-{index}" for index in range(first, first + self.per_page)]
        payload = grid_of(*ids)
        if self.announces_total is not None:
            payload["props"]["pageProps"]["initialData"]["searchResult"]["paginationV2"] = {
                "maxPage": self.announces_total
            }
        return json.dumps(payload)


def test_it_walks_past_the_first_page_now() -> None:
    """The regression that started this: one page of a shop that has several."""
    page = Catalogue(pages=4)
    assert len(_search(_lider(page), keywords=None)) == 8
    assert page.requested[:4] == [1, 2, 3, 4]


def test_an_empty_page_ends_it() -> None:
    page = Catalogue(pages=2)
    _search(_lider(page), keywords=None)
    # Asked for the third, got nothing, stopped.
    assert page.requested == [1, 2, 3]


def test_the_shops_own_page_count_is_believed() -> None:
    """Saves the request that would otherwise find the end by hitting it."""
    page = Catalogue(pages=9)
    page.announces_total = 2
    _search(_lider(page), keywords=None)
    assert page.requested == [1, 2]


def test_a_shop_that_ignores_the_page_number_does_not_loop() -> None:
    """Sodimac's `/lista` route accepts `?currentpage` and answers with page one
    for ever.  Without this stop the loop would only end at the ceiling."""
    page = Catalogue(pages=99)
    page.ignores_page_number = True
    _search(_lider(page), keywords=None)
    assert page.requested == [1, 2]


class SodimacCatalogue(FakePage):
    """Sodimac's category route, with the two behaviours that broke its paging.

    Both verified against the live site: ``?Ntt=<phrase>`` redirects and the
    redirect keeps **none** of the query string, and the route it lands on
    answers to ``?page`` and to nothing else -- ``currentpage`` included.
    Together they meant every parameter the monitor sent was either dropped in
    transit or ignored on arrival, so page two was page one and the duplicate
    guard called it the end of the catalogue.
    """

    context = None
    LISTA = "https://www.sodimac.cl/sodimac-cl/lista/cat14080023/Taladros"

    def __init__(self, pages: int, per_page: int = 2) -> None:
        super().__init__(url=self.LISTA, body=DULL_HTML)
        self.pages_available = pages
        self.per_page = per_page
        self.requested: List[int] = []

    def goto(self, url: str, timeout: int = 0) -> None:
        self.visited.append(url)
        number = 1
        if "/search?" in url:
            # The redirect.  Note what it does to `&currentpage=2`: nothing
            # keeps it, so the category route never even sees the request.
            self.url = self.LISTA
        else:
            self.url = url
            if "?page=" in url:
                number = int(url.rsplit("?page=", 1)[1])
        self.requested.append(number)

    def evaluate(self, script: str) -> Any:
        import json

        number = self.requested[-1]
        first = (number - 1) * self.per_page
        ids = (
            []
            if number > self.pages_available
            else [f"cat{number}-{index}" for index in range(first, first + self.per_page)]
        )
        return json.dumps(self._payload(ids, number))

    def _payload(self, ids: List[str], number: int) -> Dict[str, Any]:
        return {
            "props": {
                "pageProps": {
                    # The only thing that knows where the redirect put us.
                    "canonicalUrl": "/sodimac-cl/lista/cat14080023/Taladros",
                    "pagination": {
                        "count": self.pages_available * self.per_page,
                        "perPage": self.per_page,
                        # Padded with sponsored cards, as the live one is, so a
                        # divisor that reached for this would show up here.
                        "totalPerPage": self.per_page + 1,
                        "currentPage": number,
                    },
                    "results": [
                        {
                            "productId": listing_id,
                            "skuId": listing_id,
                            "displayName": "Taladro percutor 20V",
                            "url": f"https://www.sodimac.cl/sodimac-cl/articulo/{listing_id}/T",
                            "prices": [
                                {
                                    "type": "internetPrice",
                                    "symbol": "$",
                                    "price": ["99.990"],
                                    "crossed": False,
                                }
                            ],
                        }
                        for listing_id in ids
                    ],
                }
            }
        }


def _sodimac_search(market: SodimacMarketplace, **options: Any) -> List[Any]:
    """One Sodimac search, reading the grid alone -- see `_search`."""
    options.setdefault("keywords", None)
    item = SodimacItemConfig(
        name="taladros", search_phrases=["taladro"], marketplace="sodimac", **options
    )
    return list(market.search(item))


def test_sodimac_walks_the_category_route_it_was_redirected_to() -> None:
    """The regression this whole hook exists for: one page of twelve.

    Asking for page two at the address the search *started* from cannot work on
    this route, however the parameter is spelled.  It has to be asked for at the
    address page one came back from.
    """
    page = SodimacCatalogue(pages=4)
    found = _sodimac_search(_sodimac(page))
    assert page.requested == [1, 2, 3, 4]
    assert len(found) == 8
    # And the later pages were asked for by the category's own address, not by
    # the `?Ntt=` one, which would have been redirected back to page one.
    assert page.visited[1] == f"{SodimacCatalogue.LISTA}?page=2"


def test_the_ceiling_is_the_last_resort() -> None:
    """For the next site to invent a way of never saying "no more": every page
    genuinely new, and no total published."""
    page = Catalogue(pages=10_000)
    _search(_lider(page), keywords=None)
    assert len(page.requested) == retailer.MAX_PAGES_CEILING


def test_a_configured_page_count_is_still_honoured() -> None:
    """An existing config asking for two pages must keep getting two."""
    page = Catalogue(pages=50)
    market = _lider(page)
    market.config = LiderMarketplaceConfig(name="lider", max_pages=2)
    _search(market, keywords=None)
    assert page.requested == [1, 2]


def test_lider_reads_its_own_page_count() -> None:
    from ai_marketplace_monitor.lider import total_pages

    assert total_pages({"props": {"pageProps": {"initialData": {"searchResult": {
        "paginationV2": {"maxPage": 7}}}}}}) == 7


def test_a_missing_page_count_is_unknown_not_one() -> None:
    """Guessing 1 would stop every search after its first page, which is the
    exact bug this change removes."""
    from ai_marketplace_monitor.lider import total_pages

    assert total_pages({}) is None
    assert total_pages({"props": {"pageProps": {"initialData": {"searchResult": {}}}}}) is None
