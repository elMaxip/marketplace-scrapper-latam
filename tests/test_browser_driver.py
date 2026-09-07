"""The driver process, and the hang that came of keeping a dead one.

A ``Playwright`` object is a connection to a node process.  That process can
die -- a container short of memory, a crash, a Docker Desktop hiccup -- and
nothing about the Python object changes when it does.  What changes is how the
next call behaves, and the two halves are why this needed fixing rather than
noticing:

* a call on something that already exists (``context.close()``,
  ``context.pages``) raises **at once**: "Connection closed while reading from
  the driver";
* ``launch_persistent_context`` **blocks for ever**.  Its ``timeout`` is
  enforced by the driver, so a missing driver is a missing timeout -- and a
  synchronous Playwright call runs on a thread nothing else may touch, so there
  is nobody to interrupt it.

Measured, not inferred.  Killing the driver under a live Playwright reproduces
both exactly:

    context.close(): Exception: BrowserContext.close: Connection closed while
                     reading from the driver (0.00s)
    relaunch:        HUNG (still blocked after 40s)

In the container that showed up as a monitor whose log ended on "Attempting to
launch chromium browser..." and never wrote another line -- searching, from the
outside, simply stopped happening.

There is no way to ask a driver whether it is alive that does not risk the same
hang, so the rule is positional rather than diagnostic: **a driver is never kept
across a browser that has been closed or lost.**  Stopping a dead one returns
immediately, and a fresh one costs about half a second, once per idle gap.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator, List

import pytest

from ai_marketplace_monitor import control
from ai_marketplace_monitor.browser_engine import restart_driver
from ai_marketplace_monitor.lanes import BrowserLane
from ai_marketplace_monitor.monitor import MarketplaceMonitor

from .test_tabs import FakeContext


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    control.reset_for_tests()
    yield
    control.reset_for_tests()


class FakeDriver:
    """A driver connection that records whether it was stopped."""

    def __init__(self: "FakeDriver", name: str = "driver", dead: bool = False) -> None:
        self.name = name
        self.dead = dead
        self.stopped = False

    def stop(self: "FakeDriver") -> None:
        if self.dead:
            # What a driver whose process is gone does.  The message is the one
            # the container actually printed.
            raise Exception("Connection closed while reading from the driver")
        self.stopped = True


# --------------------------------------------------------------------------- #
# The helper
# --------------------------------------------------------------------------- #


def test_the_old_driver_is_stopped_and_a_new_one_returned() -> None:
    old = FakeDriver("old")
    new = FakeDriver("new")
    assert restart_driver(old, lambda: new) is new
    assert old.stopped


def test_a_driver_that_cannot_be_stopped_is_still_replaced() -> None:
    """The case this exists for: the process is already gone, so `stop()`
    raises -- and that must not stop us getting a working one."""
    dead = FakeDriver("dead", dead=True)
    new = FakeDriver("new")
    assert restart_driver(dead, lambda: new) is new


def test_no_driver_yet_just_starts_one() -> None:
    new = FakeDriver("new")
    assert restart_driver(None, lambda: new) is new


def test_the_replacement_is_made_the_callers_own_way() -> None:
    """`start` rather than `sync_playwright` directly: a lane and the monitor
    each know how they made their driver, and a replacement made some other way
    is not a replacement."""
    made: List[str] = []

    def start() -> FakeDriver:
        made.append("called")
        return FakeDriver("new")

    restart_driver(FakeDriver("old"), start)
    assert made == ["called"]


# --------------------------------------------------------------------------- #
# The monitor's own driver
# --------------------------------------------------------------------------- #


def build_monitor(context: Any, driver: Any) -> Any:
    monitor = MarketplaceMonitor.__new__(MarketplaceMonitor)
    monitor.context = context
    monitor.playwright = driver
    monitor.active_marketplaces = {}
    monitor.logger = logging.getLogger("test-driver")
    monitor.lanes = {}
    return monitor


def test_closing_the_browser_lets_go_of_the_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fix, stated once: the driver does not outlive the browser."""
    context = FakeContext(["https://a"])
    driver = FakeDriver()
    monitor = build_monitor(context, driver)

    monitor._close_browser()

    assert monitor.context is None
    assert monitor.playwright is None
    assert driver.stopped


def test_a_dead_driver_does_not_stop_the_browser_being_let_go() -> None:
    """`stop()` on a gone process raises; the close must still complete, or a
    browser that crashed would leave the monitor unable to tidy up at all."""
    context = FakeContext(["https://a"])
    dead = FakeDriver(dead=True)
    monitor = build_monitor(context, dead)

    monitor._close_browser()

    assert monitor.playwright is None


def test_the_next_launch_starts_a_fresh_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half.  Dropping it is only useful if something makes another."""
    import ai_marketplace_monitor.monitor as monitor_module

    fresh = FakeDriver("fresh")
    monkeypatch.setattr(
        monitor_module, "sync_playwright", lambda: type("Starter", (), {"start": lambda _self: fresh})()
    )
    monitor = build_monitor(FakeContext(["https://a"]), FakeDriver("old"))
    monitor._close_browser()

    assert monitor._ensure_driver() is fresh
    assert monitor.playwright is fresh


def test_a_driver_still_in_hand_is_not_replaced_for_nothing() -> None:
    """Half a second is cheap once per idle gap and not cheap per launch."""
    driver = FakeDriver()
    monitor = build_monitor(FakeContext(), driver)
    assert monitor._ensure_driver() is driver
    assert monitor._ensure_driver() is driver
    assert not driver.stopped


def test_renewing_the_browser_renews_the_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """This path runs because a shop refused us, and one of the ways a shop
    refuses is the browser dying under us -- exactly when the driver has gone
    too."""
    import ai_marketplace_monitor.monitor as monitor_module

    # Stubbed, and not for speed: the real one deletes the browser profile
    # directory, and a test that reaches the user's own home has no business
    # doing so.
    monkeypatch.setattr(monitor_module, "reset_profile", lambda lane=None: True)
    old = FakeDriver("old")
    monitor = build_monitor(FakeContext(["https://a"]), old)
    launched: List[str] = []

    def launch(*_args: Any, **_kwargs: Any) -> FakeContext:
        launched.append("launched")
        return FakeContext()

    monkeypatch.setattr(monitor, "_launch_context", launch)
    monkeypatch.setattr(monitor, "_report_browser", lambda: None)

    monitor._renew_main_browser()

    assert old.stopped
    assert monitor.playwright is None  # the next launch starts a fresh one
    assert launched == ["launched"]


# --------------------------------------------------------------------------- #
# A lane's driver
# --------------------------------------------------------------------------- #


def make_lane(driver: Any, context: Any) -> BrowserLane:
    lane = BrowserLane("test", launch=lambda playwright, name: context)
    lane._playwright = driver
    lane.logger = logging.getLogger("test-driver")
    return lane


class DeadContext:
    """A context whose browser is gone: `pages` raises, as Playwright's does."""

    @property
    def browser(self) -> None:
        return None

    @property
    def pages(self) -> List[Any]:
        raise RuntimeError("Target page, context or browser has been closed")

    def close(self) -> None:
        raise RuntimeError("Target page, context or browser has been closed")


def test_a_lane_replaces_its_driver_when_its_browser_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lane is a second browser on a thread nothing else may touch.  A lane
    that hangs here answers no further task, and its platform's searches look
    like searches that simply stopped happening."""
    import ai_marketplace_monitor.lanes as lanes_module

    fresh = FakeDriver("fresh")
    monkeypatch.setattr(
        lanes_module, "sync_playwright", lambda: type("Starter", (), {"start": lambda _self: fresh})()
    )
    old = FakeDriver("old")
    replacement = FakeContext()
    lane = make_lane(old, replacement)
    lane._context = DeadContext()

    assert lane._live_context(old) is replacement
    assert old.stopped
    assert lane._playwright is fresh


def test_the_lane_remembers_the_driver_it_now_has(monkeypatch: pytest.MonkeyPatch) -> None:
    """`renew_context` reads `self._playwright`.  A driver swapped without
    updating it would leave the shop-refusal recovery opening browsers on the
    connection that was just thrown away."""
    import ai_marketplace_monitor.lanes as lanes_module

    fresh = FakeDriver("fresh")
    monkeypatch.setattr(
        lanes_module, "sync_playwright", lambda: type("Starter", (), {"start": lambda _self: fresh})()
    )
    lane = make_lane(FakeDriver("old"), FakeContext())
    lane._context = DeadContext()
    lane._live_context(lane._playwright)

    assert lane._playwright is fresh


def test_a_lane_stops_the_driver_it_has_rather_than_the_one_it_started_with() -> None:
    """Otherwise every replacement leaks a node process for the life of the
    container: `_run` holds the first driver in a local, and that is what used
    to be stopped."""
    started_with = FakeDriver("started-with")
    current = FakeDriver("current")
    lane = make_lane(current, FakeContext())

    lane._teardown(started_with)

    assert current.stopped
    assert not started_with.stopped
