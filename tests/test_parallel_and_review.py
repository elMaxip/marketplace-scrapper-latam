"""The three things that changed about *when* work happens.

1. The queue alternates between platforms instead of emptying one first, which
   is why Mercado Libre used to be scheduled, reported as configured, and never
   actually searched.
2. Platforms can search at the same time, each on a browser of its own.
3. Reviewing stored listings has a schedule the user sets and the interface can
   show, instead of happening "whenever there is a gap".

The lane tests use a fake browser rather than a real one: what is being checked
is that work reaches the lane's thread, that failures come back to the caller,
and that a lane can be closed while it is idle -- none of which is about
Playwright, and all of which would otherwise need a Chromium per test.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import pytest
import schedule

from ai_marketplace_monitor import control
from ai_marketplace_monitor.lanes import BrowserLane
from ai_marketplace_monitor.monitor import MarketplaceMonitor
from ai_marketplace_monitor.review import (
    DEFAULT_REVIEW_BATCH,
    DEFAULT_REVIEW_INTERVAL,
    ReviewSchedule,
    _next_time_of_day,
)


# --------------------------------------------------------------------------- #
# The queue alternates between platforms
# --------------------------------------------------------------------------- #


def _job(item: str, marketplace: str) -> schedule.Job:
    job = schedule.Job(interval=1)
    job.amm_pair = (item, marketplace)
    job.amm_slot = 0
    return job


def _pairs(jobs):
    return [job.amm_pair for job in jobs]


def test_one_platform_is_left_in_the_order_it_came():
    jobs = [_job("a", "facebook"), _job("b", "facebook")]
    assert _pairs(MarketplaceMonitor._interleave(jobs)) == [
        ("a", "facebook"),
        ("b", "facebook"),
    ]


def test_two_platforms_alternate():
    # As built, the queue is every Facebook search and then every Mercado Libre
    # one, because that is the order the configuration is read in.  Working
    # through it as built is why the second platform was never reached.
    jobs = [
        _job("a", "facebook"),
        _job("b", "facebook"),
        _job("a", "mercadolibre"),
        _job("b", "mercadolibre"),
    ]
    assert _pairs(MarketplaceMonitor._interleave(jobs)) == [
        ("a", "facebook"),
        ("a", "mercadolibre"),
        ("b", "facebook"),
        ("b", "mercadolibre"),
    ]


def test_the_shorter_platform_running_out_does_not_stop_the_other():
    jobs = [
        _job("a", "facebook"),
        _job("b", "facebook"),
        _job("c", "facebook"),
        _job("a", "mercadolibre"),
    ]
    assert _pairs(MarketplaceMonitor._interleave(jobs)) == [
        ("a", "facebook"),
        ("a", "mercadolibre"),
        ("b", "facebook"),
        ("c", "facebook"),
    ]


def test_order_within_a_platform_is_untouched():
    jobs = [_job(name, "facebook") for name in "abcdef"] + [
        _job(name, "mercadolibre") for name in "abcdef"
    ]
    dealt = _pairs(MarketplaceMonitor._interleave(jobs))
    for marketplace in ("facebook", "mercadolibre"):
        assert [item for item, name in dealt if name == marketplace] == list("abcdef")


def test_a_job_carries_the_configurations_a_lane_needs():
    # A lane is handed configuration objects rather than Jobs, so the schedule
    # registry stays single-threaded.  This is where they come from.
    marketplace_config = object()
    marketplace = object()
    item_config = object()
    job = schedule.every(1).seconds
    job.do(lambda *args: None, marketplace_config, marketplace, item_config)
    try:
        assert MarketplaceMonitor._searches_of([job]) == [(marketplace_config, item_config)]
    finally:
        schedule.clear()


def test_a_job_with_no_arguments_is_skipped_rather_than_crashing():
    job = schedule.every(1).seconds
    job.do(lambda: None)
    try:
        assert MarketplaceMonitor._searches_of([job]) == []
    finally:
        schedule.clear()


# --------------------------------------------------------------------------- #
# Lanes
# --------------------------------------------------------------------------- #


class FakeContext:
    """Stands in for a BrowserContext.  Only its identity is looked at."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def lane_factory():
    made = []

    def make(launch=None, name="test"):
        context = FakeContext()
        lane = BrowserLane(name, launch=launch or (lambda playwright, lane_name: context))
        lane.context_for_test = context
        made.append(lane)
        return lane

    yield make
    for lane in made:
        lane.close(timeout=5)


def test_work_runs_on_the_lane_s_own_thread(lane_factory, monkeypatch):
    monkeypatch.setattr("ai_marketplace_monitor.lanes.sync_playwright", _fake_playwright)
    lane = lane_factory()
    threads = []
    lane.run(lambda context: threads.append(threading.current_thread().name))
    assert threads and threads[0] != threading.current_thread().name
    assert threads[0].endswith("test")


def test_the_context_handed_to_the_work_is_the_lane_s(lane_factory, monkeypatch):
    monkeypatch.setattr("ai_marketplace_monitor.lanes.sync_playwright", _fake_playwright)
    lane = lane_factory()
    assert lane.run(lambda context: context) is lane.context_for_test


def test_two_lanes_run_at_the_same_time(lane_factory, monkeypatch):
    monkeypatch.setattr("ai_marketplace_monitor.lanes.sync_playwright", _fake_playwright)
    # Both tasks wait on the same barrier, so the test can only finish if they
    # are genuinely running at once.  A queue that ran them in turn would hang.
    barrier = threading.Barrier(2, timeout=10)
    first, second = lane_factory(name="one"), lane_factory(name="two")
    tasks = [lane.submit(lambda context: barrier.wait()) for lane in (first, second)]
    for task in tasks:
        task.wait(timeout=10)


def test_a_failure_comes_back_to_whoever_waited(lane_factory, monkeypatch):
    monkeypatch.setattr("ai_marketplace_monitor.lanes.sync_playwright", _fake_playwright)
    lane = lane_factory()

    def boom(context):
        raise ValueError("no")

    with pytest.raises(ValueError, match="no"):
        lane.run(boom)
    # And the lane is still usable: one failed task does not take it down.
    assert lane.run(lambda context: "fine") == "fine"


def test_a_browser_that_will_not_open_is_reported_not_swallowed(lane_factory, monkeypatch):
    monkeypatch.setattr("ai_marketplace_monitor.lanes.sync_playwright", _fake_playwright)

    def refuse(playwright, lane_name):
        raise RuntimeError("no browser")

    lane = lane_factory(launch=refuse)
    with pytest.raises(RuntimeError, match="no browser"):
        lane.start()


def test_closing_a_lane_lets_go_of_its_browser(lane_factory, monkeypatch):
    monkeypatch.setattr("ai_marketplace_monitor.lanes.sync_playwright", _fake_playwright)
    lane = lane_factory()
    lane.run(lambda context: None)
    lane.close(timeout=5)
    assert lane.context_for_test.closed
    assert not lane.alive


def test_closing_a_lane_that_never_started_is_harmless(lane_factory, monkeypatch):
    monkeypatch.setattr("ai_marketplace_monitor.lanes.sync_playwright", _fake_playwright)
    lane_factory().close(timeout=5)


class _FakePlaywright:
    def stop(self) -> None:
        pass


class _FakeStarter:
    def start(self) -> "_FakePlaywright":
        return _FakePlaywright()


def _fake_playwright() -> "_FakeStarter":
    return _FakeStarter()


# --------------------------------------------------------------------------- #
# Two lanes, one listing: the claim keeps them apart
# --------------------------------------------------------------------------- #


def test_only_one_holder_of_a_listing_at_a_time():
    control.reset_for_tests()
    with control.claim("facebook", "1") as mine:
        assert mine
        with control.claim("facebook", "1") as theirs:
            # The second flow is told no rather than made to wait: both would be
            # doing the same fetch, and the loser has other listings to get on
            # with.
            assert not theirs
    with control.claim("facebook", "1") as after:
        assert after


def test_a_claim_is_per_listing_not_per_marketplace():
    control.reset_for_tests()
    with control.claim("facebook", "1") as first, control.claim("facebook", "2") as second:
        assert first and second


# --------------------------------------------------------------------------- #
# Several jobs under way at once
# --------------------------------------------------------------------------- #


def test_lanes_are_reported_separately():
    control.reset_for_tests()
    with control.running(item="a", marketplace="facebook", lane="main"):
        with control.running(item="b", marketplace="mercadolibre", lane="mercadolibre"):
            state = control.state()
            assert state["running"]
            assert {lane["lane"] for lane in state["lanes"]} == {"main", "mercadolibre"}
            # The one-line summary still names the main lane, so every caller
            # that has only ever had room for one reads as it always did.
            assert state["current"]["item"] == "a"
        assert [lane["lane"] for lane in control.state()["lanes"]] == ["main"]
    assert not control.state()["running"]


def test_a_lane_ending_leaves_the_other_running():
    control.reset_for_tests()
    with control.running(item="a", marketplace="facebook", lane="main"):
        with control.running(item="b", marketplace="mercadolibre", lane="mercadolibre"):
            pass
        assert control.is_running()
        assert [lane["lane"] for lane in control.state()["lanes"]] == ["main"]


def test_a_failed_lane_does_not_leave_the_job_marked_as_running():
    control.reset_for_tests()
    with pytest.raises(ValueError):
        with control.running(item="a", marketplace="facebook", lane="mercadolibre"):
            raise ValueError("no")
    assert not control.is_running()
    assert control.state()["last"]["outcome"] == "failed"


def test_a_run_is_refused_while_any_lane_is_busy():
    control.reset_for_tests()
    with control.running(item="a", marketplace="mercadolibre", lane="mercadolibre"):
        # Refused even though the *main* lane is free: a second full pass on top
        # of one already going is the concurrent hammering this avoids.
        assert control.request_run()["accepted"] is False


# --------------------------------------------------------------------------- #
# When a review happens
# --------------------------------------------------------------------------- #


def test_nothing_configured_keeps_the_old_rhythm():
    schedule_ = ReviewSchedule()
    assert schedule_.mode == "default"
    assert schedule_.batch == DEFAULT_REVIEW_BATCH
    now = time.time()
    assert schedule_.next_after(now) == pytest.approx(now + DEFAULT_REVIEW_INTERVAL, abs=1)


def test_a_fixed_interval_is_measured_from_the_end_of_the_last_round():
    schedule_ = ReviewSchedule(interval=1800)
    assert schedule_.mode == "fixed"
    now = time.time()
    assert schedule_.next_after(now) == pytest.approx(now + 1800, abs=1)


def test_a_random_interval_lands_inside_its_range():
    schedule_ = ReviewSchedule(interval=1800, max_interval=5400)
    assert schedule_.mode == "random"
    now = time.time()
    for _ in range(50):
        assert now + 1800 <= schedule_.next_after(now) <= now + 5400


def test_a_range_whose_ends_are_equal_is_a_fixed_interval():
    assert ReviewSchedule(interval=1800, max_interval=1800).mode == "fixed"


def test_fixed_times_pick_the_next_one_today():
    schedule_ = ReviewSchedule(start_at=["09:00", "15:00", "21:00"])
    assert schedule_.mode == "times"
    noon = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    when = datetime.fromtimestamp(schedule_.next_after(noon.timestamp()))
    assert (when.hour, when.minute) == (15, 0)
    assert when.date() == noon.date()


def test_fixed_times_roll_over_to_tomorrow_after_the_last_one():
    schedule_ = ReviewSchedule(start_at=["09:00"])
    late = datetime.now().replace(hour=23, minute=0, second=0, microsecond=0)
    when = datetime.fromtimestamp(schedule_.next_after(late.timestamp()))
    assert when.date() == (late + timedelta(days=1)).date()
    assert when.hour == 9


def test_an_interval_and_a_fixed_time_are_not_alternatives():
    # Whichever comes first wins, exactly as the search schedule treats them.
    schedule_ = ReviewSchedule(interval=6 * 3600, start_at=["09:00"])
    eight = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
    when = datetime.fromtimestamp(schedule_.next_after(eight.timestamp()))
    assert when.hour == 9


def test_the_hourly_and_minutely_forms_are_understood():
    after = datetime.now().replace(hour=10, minute=30, second=0, microsecond=0)
    hourly = _next_time_of_day("*:45", after)
    assert hourly is not None and (hourly.hour, hourly.minute) == (10, 45)

    minutely = _next_time_of_day("*:*:15", after)
    assert minutely is not None and (minutely.minute, minutely.second) == (30, 15)


def test_an_unparseable_time_does_not_stop_reviews_for_good():
    schedule_ = ReviewSchedule(start_at=["not a time"])
    now = time.time()
    # No usable time in the whole schedule, so it falls back rather than
    # returning nothing and leaving the review never due again.
    assert schedule_.next_after(now) > now


def test_the_schedule_describes_itself_in_numbers_not_prose():
    described = ReviewSchedule(
        interval=1800, max_interval=5400, start_at=["09:00"], batch=50
    ).describe()
    assert described == {
        "mode": "random",
        "interval": 1800,
        "max_interval": 5400,
        "start_at": ["09:00"],
        "batch": 50,
        "default": False,
    }


def test_a_batch_of_zero_is_not_accepted_silently():
    # A round that re-checks nothing is not a round; the floor is one.
    assert ReviewSchedule(batch=0).batch == DEFAULT_REVIEW_BATCH
    assert ReviewSchedule(batch=-5).batch == 1


def test_the_next_round_is_published_where_the_interface_reads_it():
    control.reset_for_tests()
    control.set_updates_next_run("2026-01-01T00:00:00+00:00")
    assert control.updates()["next_run"] == "2026-01-01T00:00:00+00:00"


def test_a_round_that_checked_nothing_still_counts_as_having_happened():
    control.reset_for_tests()
    assert control.updates()["last_run"] is None
    with control.updating(["facebook"], lane=control.UPDATES_LANE):
        assert control.updates()["running"]
        assert control.updates()["lane"] == control.UPDATES_LANE
    snapshot = control.updates()
    assert not snapshot["running"]
    assert snapshot["last_run"] is not None


def test_the_configured_schedule_reaches_the_interface():
    control.reset_for_tests()
    described = ReviewSchedule(interval=7200, batch=25).describe()
    control.set_updates_config(
        enabled=True,
        parallel=True,
        interval=6 * 3600,
        marketplaces=["facebook"],
        batch=25,
        schedule=described,
        lane=control.UPDATES_LANE,
    )
    snapshot = control.updates()
    assert snapshot["schedule"] == described
    assert snapshot["batch"] == 25
    assert snapshot["parallel"] is True
