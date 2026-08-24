"""A whole pass over the configured searches, sequential and in parallel.

The bug these exist for: with two platforms configured, the queue was built one
platform at a time and worked through in that order, so every Facebook search
had to finish before the first Mercado Libre one started.  A Facebook pass over
a handful of products is the better part of an hour, and a forced pause or a
restart sends the pass back to the top of the queue -- so in practice the second
platform was scheduled, was reported as configured, and never ran at all.

Nothing here opens a browser.  A search is replaced by a function that records
which pair it was asked for and on which thread, which is exactly what these
tests are about: what runs, in what order, and how much of it at once.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest
import schedule

from ai_marketplace_monitor import control
from ai_marketplace_monitor.monitor import MarketplaceMonitor

TWO_PLATFORMS = """
[marketplace.facebook]
username = "user@example.com"
password = "secret"
search_city = "houston"

[marketplace.mercadolibre]
market_type = "mercadolibre"

[user.me]
pushbullet_token = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

[item.ps5]
search_phrases = "playstation 5"

[item.bici]
search_phrases = "bicicleta"
"""

# Both platforms exist -- they are built into the monitor -- but this search
# runs on only one of them, which is what the web UI writes when the other is
# unticked. "One platform" is a property of the pass, not of the file.
ONE_PLATFORM = """
[marketplace.facebook]
username = "user@example.com"
password = "secret"
search_city = "houston"

[user.me]
pushbullet_token = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

[item.ps5]
search_phrases = "playstation 5"

[item.ps5.mercadolibre]
enabled = false
"""

NO_SEARCHES = """
[marketplace.facebook]
username = "user@example.com"
password = "secret"
search_city = "houston"

[user.me]
pushbullet_token = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
"""


class Recorder:
    """Stands in for searching.  Records the pair and the thread it ran on."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []
        self.threads: List[str] = []
        self._lock = threading.Lock()

    def __call__(self, marketplace_config, marketplace, item_config) -> None:
        with self._lock:
            self.calls.append((item_config.name, marketplace_config.name))
            self.threads.append(threading.current_thread().name)

    @property
    def pairs(self) -> List[Tuple[str, str]]:
        return list(self.calls)


@pytest.fixture(autouse=True)
def clean(tmp_path: Path) -> Iterator[None]:
    control.reset_for_tests()
    schedule.clear()
    yield
    schedule.clear()
    control.reset_for_tests()


def build(config_text: str, tmp_path: Path, parallel: bool = False) -> MarketplaceMonitor:
    """A monitor with a configuration loaded and no browser behind it."""
    path = tmp_path / "config.toml"
    # Written either way, never left to the default: half of these tests are
    # about the single queue and half about the lanes, and a test that does not
    # say which one it means starts passing or failing when the default moves.
    path.write_text(
        config_text
        + f"\n[monitor]\nparallel_marketplaces = {str(parallel).lower()}\n"
        + "parallel_listing_updates = false\n",
        encoding="utf-8",
    )
    instance = MarketplaceMonitor.__new__(MarketplaceMonitor)
    instance.config_files = [path]
    instance.config = None
    instance.config_hash = None
    instance._loaded_snapshot = {}
    instance._fingerprints = {}
    instance._probe_at = 0.0
    instance._probe_signature = None
    instance._reported_bad_version = None
    instance._announced_pending = None
    instance.logger = logging.getLogger("test-search-passes")
    instance.keyboard_monitor = None
    instance.context = None
    instance.lanes = {}
    instance.refresher = None
    instance.active_marketplaces = {}
    instance._review_due = float("inf")  # no review gets in the way of a pass
    instance.load_config_file()
    return instance


def stub_pass(monitor: MarketplaceMonitor, monkeypatch, recorder: Recorder) -> None:
    """Replace everything a pass does with a browser, keeping the queueing."""
    monkeypatch.setattr(monitor, "search_item", recorder)
    monkeypatch.setattr(monitor, "_ensure_browser", lambda: None)
    monkeypatch.setattr(monitor, "_apply_pending_sessions", lambda: None)
    monkeypatch.setattr(monitor, "_refresh_slice", lambda *args, **kwargs: True)
    monkeypatch.setattr(monitor, "wait_while_paused", lambda: None)
    monkeypatch.setattr(monitor, "handle_pause", lambda: None)
    # The pause switch is a file in the user's home directory, and a developer
    # who left their own monitor paused would otherwise see every one of these
    # fail for a reason that has nothing to do with them.
    monkeypatch.setattr("ai_marketplace_monitor.monitor.is_paused", lambda: False)
    monkeypatch.setattr(monitor, "_select_translator", lambda language=None: None)
    # `schedule_jobs` binds the marketplace objects; without a browser they are
    # never driven, and the recorder ignores them.
    monkeypatch.setattr(
        "ai_marketplace_monitor.marketplace.Marketplace.configure",
        lambda self, config, translator=None: None,
    )


# --------------------------------------------------------------------------- #
# Sequential
# --------------------------------------------------------------------------- #


def test_both_platforms_are_searched_in_one_pass(tmp_path, monkeypatch):
    monitor = build(TWO_PLATFORMS, tmp_path)
    recorder = Recorder()
    stub_pass(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()

    assert monitor._run_jobs() is True
    assert sorted(recorder.pairs) == [
        ("bici", "facebook"),
        ("bici", "mercadolibre"),
        ("ps5", "facebook"),
        ("ps5", "mercadolibre"),
    ]


def test_the_pass_alternates_between_platforms(tmp_path, monkeypatch):
    monitor = build(TWO_PLATFORMS, tmp_path)
    recorder = Recorder()
    stub_pass(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()
    monitor._run_jobs()

    platforms = [marketplace for _item, marketplace in recorder.pairs]
    # Never two of the same platform in a row while both still have work: that
    # is the difference between "Mercado Libre runs second" and "Mercado Libre
    # runs after every Facebook search, which in practice means never".
    assert platforms[0] != platforms[1]
    assert set(platforms[:2]) == {"facebook", "mercadolibre"}


def test_a_pass_cut_short_still_reached_the_second_platform(tmp_path, monkeypatch):
    """The scenario the user actually hit: a stop after only two searches.

    Ordered as it used to be, two searches in means two Facebook searches and
    nothing else.  Alternating, it means one of each.
    """
    monitor = build(TWO_PLATFORMS, tmp_path)
    recorder = Recorder()
    stub_pass(monitor, monkeypatch, recorder)

    def stop_after_two(marketplace_config, marketplace, item_config):
        recorder(marketplace_config, marketplace, item_config)
        if len(recorder.calls) == 2:
            control.request_cancel()
            control.raise_if_cancelled()

    monkeypatch.setattr(monitor, "search_item", stop_after_two)
    monkeypatch.setattr(monitor, "_abandon_scrape", lambda: control.clear_cancel())
    monitor.schedule_jobs()

    assert monitor._run_jobs() is False
    assert {marketplace for _item, marketplace in recorder.pairs} == {
        "facebook",
        "mercadolibre",
    }


def test_one_platform_in_use_is_not_treated_as_a_parallel_pass(tmp_path, monkeypatch):
    monitor = build(ONE_PLATFORM, tmp_path, parallel=True)
    recorder = Recorder()
    stub_pass(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()

    assert monitor._run_jobs() is True
    assert recorder.pairs == [("ps5", "facebook")]
    # No second browser was opened for a platform nothing is searching on.
    assert monitor.lanes == {}


def test_no_searches_configured_is_a_pass_with_nothing_to_do(tmp_path, monkeypatch):
    monitor = build(NO_SEARCHES, tmp_path)
    recorder = Recorder()
    stub_pass(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()

    assert monitor._run_jobs() is True
    assert recorder.pairs == []
    assert monitor._configured_searches() == 0


# --------------------------------------------------------------------------- #
# In parallel
# --------------------------------------------------------------------------- #


class FakeContext:
    def close(self) -> None:
        pass


class _FakePlaywright:
    def stop(self) -> None:
        pass


class _FakeStarter:
    def start(self) -> "_FakePlaywright":
        return _FakePlaywright()


@pytest.fixture
def fake_browser(monkeypatch):
    """Lanes that open instantly and hold nothing."""
    monkeypatch.setattr("ai_marketplace_monitor.lanes.sync_playwright", lambda: _FakeStarter())
    return FakeContext()


def stub_parallel(monitor: MarketplaceMonitor, monkeypatch, context: FakeContext) -> None:
    monkeypatch.setattr(monitor, "_launch_context", lambda playwright=None, lane=None: context)
    monkeypatch.setattr(
        monitor,
        "_lane_marketplace",
        lambda lane, ctx, marketplace_config: object(),
    )


def test_each_platform_runs_on_a_thread_of_its_own(tmp_path, monkeypatch, fake_browser):
    monitor = build(TWO_PLATFORMS, tmp_path, parallel=True)
    recorder = Recorder()
    stub_pass(monitor, monkeypatch, recorder)
    stub_parallel(monitor, monkeypatch, fake_browser)
    monitor.schedule_jobs()

    assert monitor._run_jobs() is True
    assert sorted(recorder.pairs) == [
        ("bici", "facebook"),
        ("bici", "mercadolibre"),
        ("ps5", "facebook"),
        ("ps5", "mercadolibre"),
    ]
    # Facebook stayed on the monitor's own thread -- so the profile holding the
    # signed-in session is not copied -- and Mercado Libre got a lane.
    assert "mercadolibre" in monitor.lanes
    assert "facebook" not in monitor.lanes
    assert len(set(recorder.threads)) == 2
    monitor._close_lanes()


def test_a_lane_that_will_not_open_falls_back_to_searching_in_turn(
    tmp_path, monkeypatch, fake_browser
):
    monitor = build(TWO_PLATFORMS, tmp_path, parallel=True)
    recorder = Recorder()
    stub_pass(monitor, monkeypatch, recorder)
    stub_parallel(monitor, monkeypatch, fake_browser)

    def refuse(playwright=None, lane=None):
        if lane == "mercadolibre":
            raise RuntimeError("no second browser here")
        return fake_browser

    monkeypatch.setattr(monitor, "_launch_context", refuse)
    monitor.schedule_jobs()

    # A platform that cannot have a browser of its own is still searched, on
    # this thread, once the parallel part is done.  Skipping it would be a
    # setting quietly disabling a platform.
    assert monitor._run_jobs() is True
    assert sorted(recorder.pairs) == [
        ("bici", "facebook"),
        ("bici", "mercadolibre"),
        ("ps5", "facebook"),
        ("ps5", "mercadolibre"),
    ]
    monitor._close_lanes()


def test_one_platform_failing_does_not_stop_the_other(tmp_path, monkeypatch, fake_browser):
    monitor = build(TWO_PLATFORMS, tmp_path, parallel=True)
    recorder = Recorder()
    stub_pass(monitor, monkeypatch, recorder)
    stub_parallel(monitor, monkeypatch, fake_browser)

    def sometimes_fails(marketplace_config, marketplace, item_config):
        if marketplace_config.name == "mercadolibre" and item_config.name == "ps5":
            raise RuntimeError("that platform is having a bad day")
        recorder(marketplace_config, marketplace, item_config)

    monkeypatch.setattr(monitor, "search_item", sometimes_fails)
    monitor.schedule_jobs()

    assert monitor._run_jobs() is True
    # Everything except the one that failed, including the *other* search on the
    # platform that failed: a lane keeps going through its own queue.
    assert sorted(recorder.pairs) == [
        ("bici", "facebook"),
        ("bici", "mercadolibre"),
        ("ps5", "facebook"),
    ]
    monitor._close_lanes()


def test_a_lane_s_searches_are_marked_as_having_run(tmp_path, monkeypatch, fake_browser):
    """A lane is handed configurations, not Jobs, so the Job has to be told.

    Without this the job standing for a lane's search never advances, comes
    round as due again immediately, and the platform is searched in a loop.
    """
    monitor = build(TWO_PLATFORMS, tmp_path, parallel=True)
    recorder = Recorder()
    stub_pass(monitor, monkeypatch, recorder)
    stub_parallel(monitor, monkeypatch, fake_browser)
    monitor.schedule_jobs()
    monitor._run_jobs()

    for job in schedule.get_jobs():
        assert job.last_run is not None, job.amm_pair
    assert not any(job.should_run for job in schedule.get_jobs())
    monitor._close_lanes()


def test_stopping_closes_every_lane_and_starting_again_works(
    tmp_path, monkeypatch, fake_browser
):
    monitor = build(TWO_PLATFORMS, tmp_path, parallel=True)
    recorder = Recorder()
    stub_pass(monitor, monkeypatch, recorder)
    stub_parallel(monitor, monkeypatch, fake_browser)
    monitor.schedule_jobs()
    monitor._run_jobs()
    assert monitor.lanes

    monitor._close_lanes()
    assert monitor.lanes == {}

    # And a second pass builds them again rather than reusing a closed thread.
    recorder.calls.clear()
    monitor._mark_ran(set())
    schedule.clear()
    monitor.schedule_jobs()
    assert monitor._run_jobs() is True
    assert len(recorder.pairs) == 4
    monitor._close_lanes()
