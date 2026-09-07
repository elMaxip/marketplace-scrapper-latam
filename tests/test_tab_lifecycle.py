"""The monitor's end of the tab policy: every task gives its tab back.

:mod:`tests.test_tabs` covers the mechanics -- what closing and sweeping do to a
browser.  This is about the *boundaries*: the places the monitor calls them
from, and the promise that it calls them however the task ended.

Three ways a task ends, and all three have to reach the same tidy-up:

* it finishes;
* it raises -- a site changed shape, a page timed out;
* it is cancelled -- the user pressed "detener", the configuration moved under
  it, the monitor was paused.

The third is the one that used to leak most visibly, because a cancellation
unwinds through several frames and none of them owned the tab.

No browser is opened here.  The monitor is built with ``__new__`` and given only
the attributes the boundary touches, which is exactly how much of it these paths
actually use -- and the sharpest way to say that the tidy-up must not depend on
the rest of the object being there.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterator, List

import pytest

from ai_marketplace_monitor import control
from ai_marketplace_monitor.control import CancelledScrape
from ai_marketplace_monitor.marketplace import Marketplace
from ai_marketplace_monitor.monitor import MarketplaceMonitor
from ai_marketplace_monitor.tabs import BLANK

from .test_tabs import FakeContext


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    control.reset_for_tests()
    yield
    control.reset_for_tests()


def build_monitor(context: FakeContext, marketplaces: Dict[str, Marketplace]) -> Any:
    monitor = MarketplaceMonitor.__new__(MarketplaceMonitor)
    monitor.context = context
    monitor.active_marketplaces = marketplaces
    monitor.logger = logging.getLogger("test-tabs")
    monitor.lanes = {}
    return monitor


def marketplace_on(context: FakeContext, name: str, url: str) -> Marketplace:
    """A marketplace holding one tab, on that tab's browser."""
    market = Marketplace(name, context)  # type: ignore[arg-type]
    page = market.create_page()
    page.url = url
    return market


# --------------------------------------------------------------------------- #
# The helper itself
# --------------------------------------------------------------------------- #


def test_the_named_marketplaces_give_their_tabs_back() -> None:
    context = FakeContext()
    facebook = marketplace_on(context, "facebook", "https://facebook.com/marketplace")
    mercado = marketplace_on(context, "mercadolibre", "https://mercadolibre.cl/x")
    monitor = build_monitor(context, {"facebook": facebook, "mercadolibre": mercado})

    monitor._release_tabs(context, [facebook, mercado], "a search pass")

    assert facebook.page is None and mercado.page is None
    # One tab left, blank: a browser with none has closed itself.
    assert [page.url for page in context.pages] == [BLANK]


def test_a_tab_nobody_owns_goes_too() -> None:
    """The sweep.  A pop-up the site opened, a tab a crash left behind."""
    context = FakeContext()
    facebook = marketplace_on(context, "facebook", "https://facebook.com/marketplace")
    context.open("https://facebook.com/some-popup")
    monitor = build_monitor(context, {"facebook": facebook})

    monitor._release_tabs(context, [facebook], "a search pass")

    assert len(context.pages) == 1


def test_the_browser_itself_is_never_closed() -> None:
    """Closing the browser is the idle release's decision, made on how long the
    monitor has had nothing to do.  A round ending is not that."""
    context = FakeContext()
    facebook = marketplace_on(context, "facebook", "https://facebook.com/x")
    monitor = build_monitor(context, {"facebook": facebook})

    monitor._release_tabs(context, [facebook], "a review round")

    assert not context.closed
    assert monitor.context is context


def test_a_marketplace_that_raises_does_not_stop_the_others() -> None:
    """One platform's tidy-up failing must not leave the next one's tab open."""
    context = FakeContext()
    broken = marketplace_on(context, "broken", "https://a")
    healthy = marketplace_on(context, "facebook", "https://b")

    def explode() -> None:
        raise RuntimeError("driver is unhappy")

    broken.release_page = explode  # type: ignore[method-assign]
    monitor = build_monitor(context, {"broken": broken, "facebook": healthy})

    monitor._release_tabs(context, [broken, healthy], "a search pass")

    assert healthy.page is None
    assert len(context.pages) == 1


def test_the_helper_works_on_a_monitor_that_has_no_browser_yet() -> None:
    monitor = MarketplaceMonitor.__new__(MarketplaceMonitor)
    monitor.logger = None
    monitor._release_search_tabs("a search pass")  # must not raise


# --------------------------------------------------------------------------- #
# The boundaries: a search pass
# --------------------------------------------------------------------------- #


def test_a_search_pass_gives_its_tabs_back(monkeypatch: pytest.MonkeyPatch) -> None:
    context = FakeContext()
    facebook = marketplace_on(context, "facebook", "https://facebook.com/marketplace/search")
    monitor = build_monitor(context, {"facebook": facebook})
    monkeypatch.setattr(monitor, "_marketplaces_run_in_parallel", lambda: False)
    monkeypatch.setattr(monitor, "_run_jobs_sequentially", lambda *a, **k: True)

    assert monitor._run_jobs() is True
    assert facebook.page is None
    assert [page.url for page in context.pages] == [BLANK]


@pytest.mark.parametrize(
    "error", [CancelledScrape("stopped"), RuntimeError("the site changed shape")]
)
def test_a_pass_that_ends_badly_still_gives_its_tabs_back(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    """The promise that makes this a policy rather than a habit.

    A cancellation unwinds through several frames on its way out of a search and
    none of them owns the tab, which is how "detener búsqueda" used to leave a
    results page loaded for the rest of the afternoon.
    """
    context = FakeContext()
    facebook = marketplace_on(context, "facebook", "https://facebook.com/marketplace/search")
    monitor = build_monitor(context, {"facebook": facebook})
    monkeypatch.setattr(monitor, "_marketplaces_run_in_parallel", lambda: False)

    def boom(*args: Any, **kwargs: Any) -> bool:
        raise error

    monkeypatch.setattr(monitor, "_run_jobs_sequentially", boom)

    with pytest.raises(type(error)):
        monitor._run_jobs()

    assert facebook.page is None
    assert [page.url for page in context.pages] == [BLANK]


# --------------------------------------------------------------------------- #
# The boundaries: a lane's pass
# --------------------------------------------------------------------------- #


class FakeLane:
    def __init__(self: "FakeLane", name: str) -> None:
        self.name = name
        self.marketplaces: Dict[str, Marketplace] = {}


def test_a_lane_gives_its_tabs_back_when_its_pass_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lane is a second browser.  Nothing else can tidy it up: its Playwright
    objects belong to its thread, so the pass itself has to."""
    context = FakeContext()
    lane = FakeLane("mercadolibre")
    market = marketplace_on(context, "mercadolibre", "https://mercadolibre.cl/search")
    lane.marketplaces["mercadolibre"] = market
    monitor = build_monitor(FakeContext(), {})
    monitor.lanes = {"mercadolibre": lane}

    work = monitor._lane_pass("mercadolibre", [])
    assert work(context) is True

    assert market.page is None
    assert [page.url for page in context.pages] == [BLANK]


def test_a_lane_whose_pass_raises_still_gives_its_tabs_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lane's browser is the one nothing else can reach, so a pass that ends
    by raising is the case where a tab would be abandoned for good."""
    context = FakeContext()
    lane = FakeLane("mercadolibre")
    market = marketplace_on(context, "mercadolibre", "https://mercadolibre.cl/search")
    lane.marketplaces["mercadolibre"] = market
    monitor = build_monitor(FakeContext(), {})
    monitor.lanes = {"mercadolibre": lane}

    def explode() -> None:
        raise RuntimeError("the lane's browser fell over")

    # Raised from the first thing the queue loop asks, which is as close to
    # "anywhere inside the pass" as this can be said without a real search.
    monkeypatch.setattr(control, "next_search_now", explode)
    work = monitor._lane_pass("mercadolibre", [("marketplace-config", "item-config")])

    with pytest.raises(RuntimeError):
        work(context)

    assert market.page is None
    assert [page.url for page in context.pages] == [BLANK]


# --------------------------------------------------------------------------- #
# The boundaries: a review round
# --------------------------------------------------------------------------- #


def test_a_shared_tab_review_round_gives_the_tab_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The complaint, in the mode the user runs: one browser for everything.

    The review borrows the search's marketplace, so what is left loaded at the
    end of a round is the last listing page it read -- and the next search may
    be half an hour away.
    """
    context = FakeContext()
    facebook = marketplace_on(context, "facebook", "https://facebook.com/marketplace/item/999")
    monitor = build_monitor(context, {"facebook": facebook})
    monitor.review_schedule = type("Schedule", (), {"batch": 5})()
    # Enough of a config for `_refresh_slice` to get past its own guards:
    # what is under test is the tidy-up at the end, not the decision to run.
    monitor.config = type("Config", (), {"monitor": type("M", (), {})()})()

    monkeypatch.setattr(monitor, "_review_due_now", lambda: True)
    monkeypatch.setattr(monitor, "_review_marketplaces", lambda: ("facebook",))
    monkeypatch.setattr(monitor, "_plan_next_review", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "_log_review", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "_announce_price_drops", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "_announce_top_listings_after_review", lambda *a, **k: None)
    monkeypatch.setattr(monitor, "_announce_low_stock", lambda *a, **k: None)

    class Refresher:
        def run_slice(self, names: Any, limit: int) -> Any:
            return None

    monkeypatch.setattr(monitor, "_get_refresher", lambda: Refresher())

    assert monitor._refresh_slice() is True

    assert facebook.page is None
    assert [page.url for page in context.pages] == [BLANK]


def test_a_review_round_that_fails_gives_the_tab_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = FakeContext()
    facebook = marketplace_on(context, "facebook", "https://facebook.com/marketplace/item/999")
    monitor = build_monitor(context, {"facebook": facebook})
    monitor.review_schedule = type("Schedule", (), {"batch": 5})()
    # Enough of a config for `_refresh_slice` to get past its own guards:
    # what is under test is the tidy-up at the end, not the decision to run.
    monitor.config = type("Config", (), {"monitor": type("M", (), {})()})()

    monkeypatch.setattr(monitor, "_review_due_now", lambda: True)
    monkeypatch.setattr(monitor, "_review_marketplaces", lambda: ("facebook",))
    monkeypatch.setattr(monitor, "_plan_next_review", lambda *a, **k: None)

    class Refresher:
        def run_slice(self, names: Any, limit: int) -> Any:
            raise RuntimeError("the listing page timed out")

    monkeypatch.setattr(monitor, "_get_refresher", lambda: Refresher())

    assert monitor._refresh_slice() is True

    assert facebook.page is None
    assert [page.url for page in context.pages] == [BLANK]


# --------------------------------------------------------------------------- #
# The session probe
# --------------------------------------------------------------------------- #


def test_a_session_probe_gives_its_tab_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Mercado Libre tab titled "Resumen".

    The probe asks the account page whether the session is still good.  It is a
    task of about two seconds, and the tab it leaves behind used to sit in the
    window the searches run in until something happened to navigate it.
    """
    context = FakeContext()
    market = marketplace_on(context, "mercadolibre", BLANK)
    asked: List[str] = []

    def is_signed_in() -> bool:
        assert market.page is not None
        market.page.url = "https://myaccount.mercadolibre.cl/"
        asked.append("yes")
        return True

    market.is_signed_in = is_signed_in  # type: ignore[attr-defined]
    monitor = build_monitor(context, {"mercadolibre": market})
    monitor.__dict__["_browser_of"] = {}
    monkeypatch.setattr(monitor, "_report_signed_in", lambda *a, **k: None)

    monitor._report_session_health("mercadolibre")

    assert asked == ["yes"]
    assert market.page is None
    assert [page.url for page in context.pages] == [BLANK]


def test_a_session_probe_that_raises_gives_its_tab_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = FakeContext()
    market = marketplace_on(context, "mercadolibre", "https://myaccount.mercadolibre.cl/")

    def health() -> Any:
        raise RuntimeError("the site refused us")

    market.session_health = health  # type: ignore[attr-defined]
    monitor = build_monitor(context, {"mercadolibre": market})
    monitor.__dict__["_browser_of"] = {}

    monitor._report_session_health("mercadolibre")

    assert market.page is None
    assert [page.url for page in context.pages] == [BLANK]
