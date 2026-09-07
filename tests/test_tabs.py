"""When a tab is closed, and the one case where it must not be.

The complaint: tabs stayed open with nothing using them.  A review tab still
showing the last listing it read, hours after the round ended; a Mercado Libre
tab parked on the account page, opened to ask whether the session was still good
and never given back.  On a server with a gigabyte to spare that is not
cosmetic.

The rule these tests hold to is the one in :mod:`ai_marketplace_monitor.tabs`: a
tab belongs to a task, and the task ending -- however it ends -- is what closes
it.  Two things that rule must not become:

* *close everything but the first tab.*  Lanes exist because several flows drive
  a browser each, and a sweep that ran while another task held a tab would close
  the page it is reading.  Every sweep names what to keep.
* *close the last tab.*  A persistent context with no pages has closed itself,
  taking the profile lock and the signed-in session with it.  The last one is
  parked on ``about:blank`` instead.

The fakes are pages and contexts that behave the way Playwright's do about the
one thing that matters here: closing a page removes it from ``context.pages``.
"""

from __future__ import annotations

from typing import List

import pytest

from ai_marketplace_monitor.marketplace import Marketplace
from ai_marketplace_monitor.tabs import BLANK, close_idle_pages, close_page, park


class FakePage:
    def __init__(self: "FakePage", context: "FakeContext", url: str = BLANK) -> None:
        self.context = context
        self.url = url
        self.closed = False
        self.refuses_to_close = False

    def goto(self: "FakePage", url: str) -> None:
        if self.closed:
            raise RuntimeError("Target page, context or browser has been closed")
        self.url = url

    def close(self: "FakePage") -> None:
        if self.refuses_to_close:
            raise RuntimeError("could not close")
        self.closed = True
        self.context._pages = [page for page in self.context._pages if page is not self]


class FakeContext:
    def __init__(self: "FakeContext", urls: List[str] | None = None) -> None:
        self._pages: List[FakePage] = []
        self.closed = False
        for url in urls or []:
            self.open(url)

    def open(self: "FakeContext", url: str = BLANK) -> FakePage:
        page = FakePage(self, url)
        self._pages.append(page)
        return page

    @property
    def pages(self: "FakeContext") -> List[FakePage]:
        if self.closed:
            raise RuntimeError("Target page, context or browser has been closed")
        return list(self._pages)

    def new_page(self: "FakeContext") -> FakePage:
        return self.open()


# --------------------------------------------------------------------------- #
# One tab
# --------------------------------------------------------------------------- #


def test_a_tab_with_company_is_closed() -> None:
    context = FakeContext(["https://facebook.com/marketplace", "https://mercadolibre.cl"])
    first, second = context.pages
    assert close_page(first, context) is True
    assert first.closed
    assert context.pages == [second]


def test_the_last_tab_is_parked_not_closed() -> None:
    """Closing it would close the browser, and with it the profile lock."""
    context = FakeContext(["https://facebook.com/marketplace/item/1"])
    only = context.pages[0]
    assert close_page(only, context) is False
    assert not only.closed
    assert only.url == BLANK
    assert context.pages == [only]


def test_parking_a_tab_that_is_already_blank_costs_nothing() -> None:
    context = FakeContext([BLANK])
    page = context.pages[0]
    assert park(page) is True
    assert page.url == BLANK


def test_a_tab_that_will_not_close_is_not_a_failure() -> None:
    """A search must not end because a tab refused to go away."""
    context = FakeContext(["https://a", "https://b"])
    stubborn = context.pages[0]
    stubborn.refuses_to_close = True
    assert close_page(stubborn, context) is False
    assert len(context.pages) == 2


def test_a_dead_browser_is_left_alone() -> None:
    """`pages` raises on a closed context; that is not an error to propagate."""
    context = FakeContext(["https://a", "https://b"])
    page = context.pages[0]
    context.closed = True
    assert close_page(page, context) is False


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #


def test_the_sweep_keeps_what_a_task_is_using() -> None:
    """The whole reason this takes a `keep` list rather than closing all but one."""
    context = FakeContext(["https://in-use", "https://abandoned", "https://also-abandoned"])
    in_use = context.pages[0]
    assert close_idle_pages(context, keep=[in_use]) == 2
    assert context.pages == [in_use]
    assert in_use.url == "https://in-use"


def test_the_sweep_catches_a_tab_no_marketplace_owns() -> None:
    """A pop-up the site opened for itself, or a tab a task left behind when it
    raised.  Nothing has a name for these, which is why the sweep exists."""
    context = FakeContext(["https://search", "https://popup.example"])
    search = context.pages[0]
    close_idle_pages(context, keep=[search])
    assert [page.url for page in context.pages] == ["https://search"]


def test_the_sweep_parks_the_last_one_when_nothing_is_kept() -> None:
    context = FakeContext(["https://a", "https://b", "https://c"])
    assert close_idle_pages(context) == 2
    remaining = context.pages
    assert len(remaining) == 1
    assert remaining[0].url == BLANK


def test_the_sweep_never_empties_a_browser() -> None:
    """The invariant, said as an invariant: whatever it is handed, a page is
    left.  A context with no pages is a browser that has closed itself."""
    for count in range(1, 6):
        context = FakeContext([f"https://{index}" for index in range(count)])
        close_idle_pages(context)
        assert len(context.pages) == 1


def test_the_sweep_on_a_dead_browser_does_nothing() -> None:
    context = FakeContext(["https://a"])
    context.closed = True
    assert close_idle_pages(context) == 0


def test_the_sweep_on_no_browser_does_nothing() -> None:
    assert close_idle_pages(None) == 0


# --------------------------------------------------------------------------- #
# What a marketplace does with its own tab
# --------------------------------------------------------------------------- #


def _marketplace(context: FakeContext) -> Marketplace:
    return Marketplace("facebook", context)  # type: ignore[arg-type]


def test_a_marketplace_gives_its_tab_back() -> None:
    context = FakeContext()
    market = _marketplace(context)
    page = market.create_page()
    page.url = "https://facebook.com/marketplace/search"
    context.open("https://other")

    market.release_page()
    assert market.page is None
    assert page.closed


def test_releasing_twice_is_harmless() -> None:
    """It is called from `finally` blocks that can nest."""
    context = FakeContext()
    market = _marketplace(context)
    market.create_page()
    market.release_page()
    market.release_page()
    assert market.page is None


def test_the_released_tab_is_claimed_again_rather_than_added_to() -> None:
    """The cost of the policy, pinned: one navigation, not one tab per pass.

    A marketplace whose tab was parked takes the same tab back on its next
    search -- which is what stops "close it when idle" from turning into a
    browser that grows a window every half hour.
    """
    context = FakeContext()
    market = _marketplace(context)
    first = market.create_page()
    first.url = "https://facebook.com/marketplace/search"
    market.release_page()
    assert first.url == BLANK  # parked, being the only one
    second = market.create_page()
    assert second is first
    assert len(context.pages) == 1


def test_two_marketplaces_share_one_browser_without_taking_each_others_tab() -> None:
    """The shared-browser mode the user runs: one Chromium, several platforms.

    Releasing one platform's tab must leave the other platform's alone -- it may
    be halfway through a search.
    """
    context = FakeContext()
    facebook = _marketplace(context)
    mercado = Marketplace("mercadolibre", context)  # type: ignore[arg-type]
    facebook_page = facebook.create_page()
    facebook_page.url = "https://facebook.com/marketplace"
    mercado_page = mercado.create_page()
    mercado_page.url = "https://mercadolibre.cl/x"
    assert mercado_page is not facebook_page

    facebook.release_page()

    assert facebook_page.closed
    assert not mercado_page.closed
    assert mercado.page is mercado_page
    assert mercado_page.url == "https://mercadolibre.cl/x"


def test_a_marketplace_with_no_browser_releases_nothing() -> None:
    market = Marketplace("facebook", None)  # type: ignore[arg-type]
    market.release_page()
    assert market.page is None


def test_release_survives_a_browser_that_died_under_it() -> None:
    """A container restarted mid-search, a window closed by hand.  The tidy-up
    must not be the thing that raises."""
    context = FakeContext()
    market = _marketplace(context)
    market.create_page()
    context.closed = True
    market.release_page()
    assert market.page is None


@pytest.mark.parametrize("url", ["about:blank", ""])
def test_a_stray_blank_is_still_swept(url: str) -> None:
    """`create_page` already claimed one blank; a second is nobody's."""
    context = FakeContext(["https://in-use", url])
    in_use = context.pages[0]
    assert close_idle_pages(context, keep=[in_use]) == 1
