"""Opening a browser, letting go of it, and noticing when it has gone.

Three complaints, one subject.

*It stayed open.*  A search every half hour held a Chromium -- two, with
parallel platforms -- and a visible window for twenty-nine minutes out of every
thirty.  It is now released while there is nothing to search, and opened again
on the same persistent profile, which costs a browser start and no sign-in.

*Closing it by hand broke the monitor.*  ``self.context`` went on pointing at a
browser that no longer existed, and the interface went on saying that listings
were being reviewed while there was no browser at all.  The object stays
perfectly valid Python after the process behind it dies, which is why it has to
be *asked*.

*And then the next run could not recover.*  A lane kept its dead context and
failed every later search in the same way, which read as the searches failing
rather than as the browser being gone.

The fakes here are contexts that can be "closed" and then answer like a closed
one: ``pages`` raises, which is what Playwright does.
"""

from __future__ import annotations

import logging
import threading
from typing import Iterator

import pytest

from ai_marketplace_monitor import control
from ai_marketplace_monitor.lanes import BrowserLane, context_is_alive
from ai_marketplace_monitor.monitor import MarketplaceMonitor


class FakeContext:
    def __init__(self, name: str = "main") -> None:
        self.name = name
        self.closed = False

    @property
    def browser(self):
        return None

    @property
    def pages(self):
        if self.closed:
            raise RuntimeError("Target page, context or browser has been closed")
        return []

    def close(self) -> None:
        self.closed = True


class DeadBrowser:
    def is_connected(self) -> bool:
        return False


class ContextWithDeadBrowser(FakeContext):
    @property
    def browser(self):
        return DeadBrowser()


class _FakePlaywright:
    def stop(self) -> None:
        pass


class _FakeStarter:
    def start(self) -> "_FakePlaywright":
        return _FakePlaywright()


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    control.reset_for_tests()
    yield
    control.reset_for_tests()


@pytest.fixture
def fake_playwright(monkeypatch):
    monkeypatch.setattr("ai_marketplace_monitor.lanes.sync_playwright", lambda: _FakeStarter())


# --------------------------------------------------------------------------- #
# Is this browser still there?
# --------------------------------------------------------------------------- #


def test_a_live_context_is_alive():
    assert context_is_alive(FakeContext()) is True


def test_a_closed_context_is_not():
    context = FakeContext()
    context.close()
    assert context_is_alive(context) is False


def test_a_context_whose_browser_has_disconnected_is_not():
    assert context_is_alive(ContextWithDeadBrowser()) is False


def test_nothing_at_all_is_not_a_browser():
    assert context_is_alive(None) is False


def test_a_context_that_cannot_be_asked_counts_as_gone():
    """A browser we cannot ask about is one we must not use."""

    class Unreachable:
        @property
        def browser(self):
            raise OSError("the driver is not answering")

    assert context_is_alive(Unreachable()) is False


# --------------------------------------------------------------------------- #
# The monitor's own browser
# --------------------------------------------------------------------------- #


def _monitor(monkeypatch) -> MarketplaceMonitor:
    instance = MarketplaceMonitor.__new__(MarketplaceMonitor)
    instance.logger = logging.getLogger("test-browser-lifecycle")
    instance.context = None
    instance.lanes = {}
    instance.active_marketplaces = {}
    instance.config = None
    instance._browsers_idle = False
    instance.opened = []

    def launch(playwright=None, lane=None):
        context = FakeContext(lane or "main")
        instance.opened.append(context)
        return context

    monkeypatch.setattr(instance, "_launch_context", launch)
    monkeypatch.setattr(instance, "_seed_imported_sessions", lambda: None)
    return instance


def test_the_browser_is_opened_on_first_ask(monkeypatch):
    monitor = _monitor(monkeypatch)
    context = monitor._ensure_browser()
    assert context is monitor.context
    assert len(monitor.opened) == 1


def test_the_same_browser_is_reused_while_it_lives(monkeypatch):
    monitor = _monitor(monkeypatch)
    first = monitor._ensure_browser()
    assert monitor._ensure_browser() is first
    assert len(monitor.opened) == 1


def test_a_browser_closed_by_hand_is_replaced(monkeypatch):
    """The state the monitor must never be left in: it believes it is
    searching, and there is no browser."""
    monitor = _monitor(monkeypatch)
    first = monitor._ensure_browser()
    first.close()

    second = monitor._ensure_browser()
    assert second is not first
    assert context_is_alive(second)
    assert len(monitor.opened) == 2


def test_the_marketplaces_are_rebound_to_the_new_browser(monkeypatch):
    class FakeMarketplace:
        def __init__(self) -> None:
            self.context = None
            self.stopped = False

        def set_context(self, context) -> None:
            self.context = context

        def stop(self) -> None:
            self.stopped = True

    monitor = _monitor(monkeypatch)
    marketplace = FakeMarketplace()
    first = monitor._ensure_browser()
    monitor.active_marketplaces = {"facebook": marketplace}
    first.close()

    second = monitor._ensure_browser()
    assert marketplace.stopped is True
    assert marketplace.context is second


# --------------------------------------------------------------------------- #
# Letting go while there is nothing to do
# --------------------------------------------------------------------------- #


def test_a_short_gap_keeps_the_browser(monkeypatch):
    monitor = _monitor(monkeypatch)
    monitor._ensure_browser()
    monitor._release_idle_browsers(30)
    assert monitor.context is not None
    assert monitor._browsers_idle is False


def test_a_long_gap_gives_the_browser_back(monkeypatch, fake_playwright):
    monitor = _monitor(monkeypatch)
    monitor.notifier = _NoNotifications()
    context = monitor._ensure_browser()
    monitor._release_idle_browsers(3600)

    assert monitor.context is None
    assert context.closed is True
    # Said explicitly: "there is no browser" has two meanings, and only this
    # one may be answered by opening another.
    assert monitor._browsers_idle is True


def test_a_released_browser_comes_back_for_the_next_search(monkeypatch, fake_playwright):
    monitor = _monitor(monkeypatch)
    monitor.notifier = _NoNotifications()
    monitor._ensure_browser()
    monitor._release_idle_browsers(3600)

    context = monitor._ensure_browser()
    assert context_is_alive(context)
    assert monitor._browsers_idle is False
    assert len(monitor.opened) == 2


def test_the_lanes_are_released_too_but_not_the_review_lane(
    monkeypatch, fake_playwright
):
    monitor = _monitor(monkeypatch)
    monitor.notifier = _NoNotifications()
    monitor._ensure_browser()
    search_lane = monitor._lane("mercadolibre")
    search_lane.start()
    review_lane = monitor._lane(control.UPDATES_LANE)
    review_lane.start()

    monitor._release_idle_browsers(3600)

    # A review with a browser of its own is *using* it right now: the gap
    # between searches is when it gets the most done.
    assert "mercadolibre" not in monitor.lanes
    assert control.UPDATES_LANE in monitor.lanes
    review_lane.close(timeout=5)


def test_nothing_is_released_while_a_search_is_running(monkeypatch):
    monitor = _monitor(monkeypatch)
    monitor.notifier = _NoNotifications()
    monitor._ensure_browser()
    with control.running(item="ps5", marketplace="facebook"):
        monitor._release_idle_browsers(3600)
        assert monitor.context is not None


def test_nothing_is_released_while_a_notification_is_in_flight(monkeypatch):
    monitor = _monitor(monkeypatch)
    monitor.notifier = _OneNotificationPending()
    monitor._ensure_browser()
    monitor._release_idle_browsers(3600)
    assert monitor.context is not None


class _NoNotifications:
    pending = 0


class _OneNotificationPending:
    pending = 1


# --------------------------------------------------------------------------- #
# A lane's browser
# --------------------------------------------------------------------------- #


def test_a_lane_opens_its_browser_on_its_own_thread(fake_playwright):
    made: list = []

    def launch(playwright, name):
        made.append(threading.current_thread().name)
        return FakeContext(name)

    lane = BrowserLane("mercadolibre", launch=launch)
    try:
        lane.run(lambda context: None)
        assert made and made[0] == "amm-lane-mercadolibre"
    finally:
        lane.close(timeout=5)


def test_a_lane_replaces_a_browser_that_has_gone(fake_playwright):
    """A lane outlives any one search; its window can be closed between two."""
    contexts: list = []

    def launch(playwright, name):
        context = FakeContext(name)
        contexts.append(context)
        return context

    lane = BrowserLane("mercadolibre", launch=launch, logger=logging.getLogger("lane"))
    try:
        first = lane.run(lambda context: context)
        first.close()
        second = lane.run(lambda context: context)
        assert second is not first
        assert context_is_alive(second)
        assert len(contexts) == 2
    finally:
        lane.close(timeout=5)


def test_a_lane_drops_the_marketplaces_bound_to_a_dead_browser(fake_playwright):
    class FakeMarketplace:
        def __init__(self) -> None:
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

    def launch(playwright, name):
        return FakeContext(name)

    lane = BrowserLane("mercadolibre", launch=launch)
    try:
        context = lane.run(lambda ctx: ctx)
        marketplace = FakeMarketplace()
        lane.marketplaces["mercadolibre"] = marketplace
        context.close()

        lane.run(lambda ctx: ctx)
        # Otherwise the next search drives a tab that no longer exists.
        assert marketplace.stopped is True
        assert lane.marketplaces == {}
    finally:
        lane.close(timeout=5)


def test_closing_a_lane_lets_go_of_its_browser(fake_playwright):
    context = FakeContext("mercadolibre")
    lane = BrowserLane("mercadolibre", launch=lambda playwright, name: context)
    lane.run(lambda ctx: None)
    lane.close(timeout=5)
    assert context.closed is True


def test_a_lane_that_cannot_open_a_browser_says_so(fake_playwright):
    def refuse(playwright, name):
        raise RuntimeError("no browser here")

    lane = BrowserLane("mercadolibre", launch=refuse)
    try:
        with pytest.raises(RuntimeError, match="no browser here"):
            lane.run(lambda ctx: None)
    finally:
        lane.close(timeout=5)


# --------------------------------------------------------------------------- #
# No browser is opened for work that is not there
# --------------------------------------------------------------------------- #
#
# The reported symptom: "sometimes it opens an extra browser on about:blank".
# It was not a race at launch -- the profile's first tab is always present by
# the time `launch_persistent_context` returns -- it was a browser opened before
# anyone had checked whether there was anything for it to do.  The review lane
# is the one that showed it most, because on a fresh install the store is empty
# and it therefore had nothing to do for its whole life.


class _Cfg:
    pass


def _review_monitor(monkeypatch, *, parallel=True, stale=True) -> MarketplaceMonitor:
    instance = _monitor(monkeypatch)
    config = _Cfg()
    config.monitor = _Cfg()
    config.monitor.parallel_listing_updates = parallel
    config.monitor.listing_recheck_interval = 3600
    facebook = _Cfg()
    facebook.enabled = True
    config.marketplace = {"facebook": facebook}
    instance.config = config
    instance._review_stop = threading.Event()
    monkeypatch.setattr(
        "ai_marketplace_monitor.monitor.stale_records",
        lambda *a, **k: ([{"marketplace": "facebook", "id": "1"}] if stale else []),
    )
    return instance


def test_the_review_lane_is_not_started_with_nothing_to_review(monkeypatch):
    monitor = _review_monitor(monkeypatch, stale=False)
    monitor._start_review_lane()
    assert monitor.lanes == {}
    assert monitor.opened == []


def test_the_review_lane_starts_once_there_is_something_to_review(
    monkeypatch, fake_playwright
):
    monitor = _review_monitor(monkeypatch, stale=True)
    monkeypatch.setattr(monitor, "_review_lane_loop", lambda context: True)
    monitor._start_review_lane()
    try:
        assert control.UPDATES_LANE in monitor.lanes
    finally:
        monitor._stop_review_lane()


def test_the_review_lane_is_not_started_when_the_setting_is_off(monkeypatch):
    monitor = _review_monitor(monkeypatch, parallel=False, stale=True)
    monitor._start_review_lane()
    assert monitor.lanes == {}


def test_an_unreadable_store_does_not_refuse_the_review(monkeypatch):
    """"We could not look" must not read as "there is nothing there"."""
    monitor = _review_monitor(monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("the cache is locked")

    monkeypatch.setattr("ai_marketplace_monitor.monitor.stale_records", boom)
    assert monitor._listings_to_review() is True


def test_a_platform_that_just_refused_us_is_not_worth_a_browser(monkeypatch):
    monitor = _review_monitor(monkeypatch, stale=True)
    control.block_marketplace("facebook", reason="sign-in wall")
    assert monitor._listings_to_review() is False


# --------------------------------------------------------------------------- #
# Stray blank tabs
# --------------------------------------------------------------------------- #


class TabContext(FakeContext):
    """A context whose tabs can be listed, opened and closed."""

    def __init__(self, urls=()) -> None:
        super().__init__("main")
        self._pages = [TabPage(url, self) for url in urls]

    @property
    def pages(self):
        if self.closed:
            raise RuntimeError("closed")
        return list(self._pages)

    def new_page(self):
        page = TabPage("about:blank", self)
        self._pages.append(page)
        return page


class TabPage:
    def __init__(self, url: str, context=None) -> None:
        self.url = url
        self.closed = False
        self._context = context

    def close(self) -> None:
        # Playwright drops a closed page from `context.pages`; a fake that did
        # not would let a bug that closes nothing look like a pass.
        self.closed = True
        if self._context is not None and self in self._context._pages:
            self._context._pages.remove(self)


def _marketplace_on(context):
    from ai_marketplace_monitor.marketplace import Marketplace

    instance = Marketplace.__new__(Marketplace)
    instance.context = context
    instance.page = None
    return instance


def test_the_blank_tab_the_profile_opens_with_is_the_one_used():
    context = TabContext(["about:blank"])
    page = _marketplace_on(context).create_page()
    assert page is context.pages[0]
    assert len(context.pages) == 1


def test_a_leftover_blank_tab_beside_a_real_one_is_closed():
    """Two marketplaces on one browser, and the order went wrong once."""
    context = TabContext(["https://www.facebook.com/marketplace/", "about:blank", ""])
    marketplace = _marketplace_on(context)
    marketplace.page = context.pages[0]
    marketplace.create_page()

    assert [page.url for page in context.pages] == [
        "https://www.facebook.com/marketplace/"
    ]


def test_the_last_tab_is_never_closed():
    """Closing every tab of a persistent context takes the browser with it."""
    context = TabContext(["about:blank"])
    marketplace = _marketplace_on(context)
    marketplace.create_page()
    assert len(context.pages) == 1
    assert context.pages[0].closed is False


def test_another_marketplace_s_page_is_left_alone():
    context = TabContext(["https://www.facebook.com/marketplace/", "about:blank"])
    marketplace = _marketplace_on(context)
    page = marketplace.create_page()
    # It took the blank one and left the other platform's page where it was.
    assert page.url == "about:blank"
    assert [p.url for p in context.pages] == [
        "https://www.facebook.com/marketplace/",
        "about:blank",
    ]
