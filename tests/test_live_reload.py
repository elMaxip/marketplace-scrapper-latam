"""Configuration saved while the scraper is running, and what it does about it.

The behaviour these pin, in the order a user meets it:

* saving a search does not wait for a restart, or even for the current search
  to end, unless the current search is the one that changed;
* the search that *was* changed keeps going where it can: an edit is taken
  into the search already under way, because the results it is producing are
  still wanted, only under different filters.  Deleting it or switching it off
  is the exception -- neither leaves anything worth finishing;
* what a running search *cannot* absorb is named rather than glossed over: the
  city and the price band went into the URL it is paging through, so they are
  reported as waiting for its next run instead of counted as applied;
* a change to something else does not throw away work in progress;
* a file caught halfway through being written never becomes an outage: the
  loop keeps running what it already has;
* and every one of those is *said*, so the interface can tell the user their
  change is in use rather than leaving them to compare hashes.

``MarketplaceMonitor.__init__`` starts Playwright, which none of this needs, so
the instance is built without it: everything under test reads ``self.config``
and writes to :mod:`ai_marketplace_monitor.control`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator, List

import pytest

from ai_marketplace_monitor import control
from ai_marketplace_monitor.control import SearchStopped, SearchSuperseded
from ai_marketplace_monitor.live_config import DISABLED, MODIFIED, REMOVED
from ai_marketplace_monitor.monitor import MarketplaceMonitor
from ai_marketplace_monitor.utils import calculate_file_hash

# Two products, and therefore four searches: the platforms are built into the
# monitor, so a search that says nothing about them runs on all of them. The
# pairs below come in twos for that reason, not because the file asks for it.
TWO_SEARCHES = """
# Sequential on purpose.  The pass these tests instrument is the one that runs
# on this thread and reports through `_run_job`; with the platforms searching
# in parallel -- which is the default -- half of the work goes to a lane and
# through `search_item` instead, and none of that is what is being tested here.
[monitor]
parallel_marketplaces = false
parallel_listing_updates = false

[marketplace.facebook]
username = "user@example.com"
password = "secret"
search_city = "houston"

[user.me]
pushbullet_token = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

[item.ps5]
search_phrases = "playstation 5"

[item.bici]
search_phrases = "bicicleta"
"""


@pytest.fixture(autouse=True)
def clean_control() -> Iterator[None]:
    control.reset_for_tests()
    yield
    control.reset_for_tests()


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(TWO_SEARCHES, encoding="utf-8")
    return path


@pytest.fixture
def monitor(config_file: Path) -> MarketplaceMonitor:
    """A monitor with a configuration loaded and no browser behind it."""
    instance = MarketplaceMonitor.__new__(MarketplaceMonitor)
    instance.config_files = [config_file]
    instance.config = None
    instance.config_hash = None
    instance._loaded_snapshot = {}
    instance._fingerprints = {}
    instance._searching = None
    instance._probe_at = 0.0
    instance._probe_signature = None
    instance._reported_bad_version = None
    instance._announced_pending = None
    instance._schedule_dirty = False
    instance.logger = logging.getLogger("test-live-reload")
    instance.keyboard_monitor = None
    instance.load_config_file()
    return instance


def save(path: Path, content: str) -> None:
    """What the web UI's save does, from the monitor's side: the file changes.

    Named rather than inlined because that is the whole event under test here,
    and every one of these tests is a sentence about when it happens.
    """
    path.write_text(content, encoding="utf-8")


def probe(monitor: MarketplaceMonitor) -> object:
    """Ask the monitor to look now, without waiting out its throttle."""
    return monitor._probe_config(force=True)


# --------------------------------------------------------------------------- #
# Noticing
# --------------------------------------------------------------------------- #


def test_an_untouched_file_is_not_a_change(monitor: MarketplaceMonitor) -> None:
    assert probe(monitor) is None


def test_a_rewrite_with_the_same_content_is_not_a_change(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    """The web UI's "search now" button touches the file on purpose.

    It has to stay distinguishable from a real edit, or pressing it would
    restart the schedule every time.
    """
    save(config_file, TWO_SEARCHES)
    assert probe(monitor) is None


def test_a_broken_file_is_a_change_that_cannot_be_read(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    save(config_file, "[item.ps5]\nsearch_phrases = ")
    result = probe(monitor)
    assert result is not None
    assert result.changed
    assert not result.readable
    assert result.error
    # And the loop keeps the configuration it already had.
    assert monitor.config is not None
    assert set(monitor.config.items) == {
        ("facebook", "ps5"),
        ("facebook", "bici"),
        ("mercadolibre", "ps5"),
        ("mercadolibre", "bici"),
    }


def test_a_changed_secret_is_a_change_the_snapshot_cannot_itemise(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    save(config_file, TWO_SEARCHES.replace("x" * 33, "y" * 33))
    result = probe(monitor)
    assert result is not None and result.readable
    assert result.change.general
    assert result.change.affects("ps5", "facebook") is None


# --------------------------------------------------------------------------- #
# The guard: what happens to the search that is running
# --------------------------------------------------------------------------- #


def test_deleting_the_running_search_stops_it(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    monitor._searching = ("ps5", "facebook")
    save(config_file, TWO_SEARCHES.replace('[item.ps5]\nsearch_phrases = "playstation 5"\n', ""))
    with pytest.raises(SearchSuperseded) as raised:
        monitor._config_guard()
    assert raised.value.item == "ps5"
    assert raised.value.reason == REMOVED
    # The new configuration is in force by the time the exception is raised,
    # so the loop that catches it can schedule from it immediately.
    assert set(monitor.config.items) == {("facebook", "bici"), ("mercadolibre", "bici")}


def test_switching_the_running_search_off_stops_it(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    monitor._searching = ("ps5", "facebook")
    save(config_file, TWO_SEARCHES.replace(
        '[item.ps5]\nsearch_phrases = "playstation 5"',
        '[item.ps5]\nenabled = false\nsearch_phrases = "playstation 5"',
    ))
    with pytest.raises(SearchSuperseded) as raised:
        monitor._config_guard()
    assert raised.value.reason == DISABLED


def test_editing_the_running_search_lets_it_carry_on(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    """Lowering a maximum price is not an instruction to abandon a page.

    The user edited the search because they want its results under different
    settings, not because they want it to stop.  Dropping it would throw away
    the page load and the AI calls already spent to gain a restart.
    """
    monitor._searching = ("ps5", "facebook")
    save(config_file, TWO_SEARCHES.replace('search_phrases = "playstation 5"',
                                           'search_phrases = "playstation 5"\nkeywords = "slim"'))
    monitor._config_guard()  # does not raise
    applied = control.config_applied()
    assert applied is not None
    assert applied["interrupted"] is None
    assert applied["live"]["applied"] == ["keywords"]


def test_a_filter_edit_reaches_the_object_the_running_search_is_holding(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    """The point of applying live, and the only proof of it.

    ``Marketplace.search`` is a generator holding *this* dataclass instance and
    consulting it once per listing.  Rebinding ``self.config`` would leave it
    reading the old one to the end, so the new values have to land on the object
    itself -- and this is the test that would fail if that ever became a
    replacement instead of a mutation.
    """
    monitor._searching = ("ps5", "facebook")
    running = monitor.config.items[("facebook", "ps5")]
    assert running.keywords is None
    save(config_file, TWO_SEARCHES.replace('search_phrases = "playstation 5"',
                                           'search_phrases = "playstation 5"\nkeywords = "slim"'))
    monitor._config_guard()
    assert running.keywords == ["slim"]


def test_what_the_running_search_cannot_absorb_is_named_not_claimed(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    """A price band already spent on a URL cannot be applied to that URL.

    So it is reported as waiting for the search's next run.  Saying otherwise
    would put a tick next to a setting that is not in force, which is the one
    thing the whole sync display exists to prevent.
    """
    monitor._searching = ("ps5", "facebook")
    save(config_file, TWO_SEARCHES.replace('search_phrases = "playstation 5"',
                                           'search_phrases = "playstation 5"\nmax_price = 500'))
    monitor._config_guard()
    live = control.config_applied()["live"]
    assert live["deferred"] == ["max_price"]
    assert live["applied"] == []


def test_the_old_behaviour_is_still_available(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    """``apply_changes_while_running = false`` restores dropping the search.

    Defensible, and the reason it is a setting rather than a decision: results
    produced under filters the user has replaced are of no use to some people,
    and a restart is cheap when a search is short.
    """
    monitor._searching = ("ps5", "facebook")
    monitor.config.monitor.apply_changes_while_running = False
    save(config_file, TWO_SEARCHES.replace('search_phrases = "playstation 5"',
                                           'search_phrases = "playstation 5"\nkeywords = "slim"'))
    with pytest.raises(SearchSuperseded) as raised:
        monitor._config_guard()
    assert raised.value.reason == MODIFIED


def test_deleting_the_running_search_may_be_allowed_to_finish(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    """``on_delete_running = "finish"``: the search ends normally, then is gone.

    The default is the other way round -- deleting something usually means now
    -- but a search two minutes from the end is worth more than the tidiness.
    """
    monitor._searching = ("ps5", "facebook")
    monitor.config.monitor.on_delete_running = "finish"
    save(config_file, TWO_SEARCHES.replace('[item.ps5]\nsearch_phrases = "playstation 5"\n', ""))
    monitor._config_guard()  # does not raise
    assert ("facebook", "ps5") not in monitor.config.items


def test_deleting_a_different_search_leaves_the_running_one_alone(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    monitor._searching = ("ps5", "facebook")
    save(config_file, TWO_SEARCHES.replace('[item.bici]\nsearch_phrases = "bicicleta"\n', ""))
    monitor._config_guard()  # does not raise
    # And it *is* adopted, right there in the middle of the search that was not
    # touched.  Sitting on it until the current search ended is what made a
    # search created from the web UI take minutes to appear in "búsquedas que
    # el scraper está usando": nothing about the running search stops the loop
    # from knowing the deletion happened.
    assert set(monitor.config.items) == {
        ("facebook", "ps5"),
        ("mercadolibre", "ps5"),
    }
    # The `schedule` registry is not touched from here -- a lane's checkpoint
    # may be the caller -- so the monitor thread is told to rebuild it.
    assert monitor._schedule_dirty is True


def test_adding_a_search_leaves_the_running_one_alone(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    monitor._searching = ("ps5", "facebook")
    save(config_file, TWO_SEARCHES + '\n[item.tele]\nsearch_phrases = "televisor"\n')
    monitor._config_guard()
    # Present immediately, which is what the interface promises: creating a
    # search while another one runs shows it straight away, still respecting
    # its own schedule for when it actually runs.
    assert ("facebook", "tele") in monitor.config.items


def test_a_broken_file_never_stops_a_running_search(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    """A save is not atomic from the reader's side.

    Catching the file halfway through being written must not look like a
    deleted search, or every save would abandon whatever was running.
    """
    monitor._searching = ("ps5", "facebook")
    save(config_file, "[item.ps5")
    monitor._config_guard()


def test_the_guard_does_nothing_between_searches(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    """The listing refresher stops at the same checkpoints.

    It is not running a search, so there is no search to supersede -- and the
    loop is about to reload on its own anyway.
    """
    monitor._searching = None
    save(config_file, TWO_SEARCHES.replace('[item.ps5]\nsearch_phrases = "playstation 5"\n', ""))
    monitor._config_guard()


# --------------------------------------------------------------------------- #
# Saying so
# --------------------------------------------------------------------------- #


def test_adopting_announces_what_landed(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    save(config_file, TWO_SEARCHES + '\n[item.tele]\nsearch_phrases = "televisor"\n')
    result = probe(monitor)
    monitor._adopt_config(result)

    applied = control.config_applied()
    assert applied is not None
    assert applied["version"] == calculate_file_hash([config_file])
    assert applied["change"]["added"] == [
        {"item": "tele", "marketplace": "facebook"},
        {"item": "tele", "marketplace": "mercadolibre"},
    ]
    assert applied["interrupted"] is None
    # And the versions now agree, which is the other half of the answer.
    assert control.loaded_version() == applied["version"]


def test_a_dropped_search_is_named_in_the_announcement(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    monitor._searching = ("ps5", "facebook")
    save(config_file, TWO_SEARCHES.replace('[item.ps5]\nsearch_phrases = "playstation 5"\n', ""))
    with pytest.raises(SearchSuperseded):
        monitor._config_guard()

    applied = control.config_applied()
    assert applied is not None
    assert applied["interrupted"] == {
        "item": "ps5",
        "marketplace": "facebook",
        "reason": REMOVED,
    }
    assert applied["change"]["removed"] == [
        {"item": "ps5", "marketplace": "facebook"},
        {"item": "ps5", "marketplace": "mercadolibre"},
    ]


def test_nothing_is_announced_before_a_change_happens(monitor: MarketplaceMonitor) -> None:
    """The first load is not a change, and announcing it would put a notice on
    the screen of every user who has just started the monitor."""
    assert control.config_applied() is None


def test_the_notice_moves_on_once_per_change(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    """The interface polls; it needs to tell a new notice from the same one
    read again, without having to trust timestamps it did not issue."""
    save(config_file, TWO_SEARCHES + '\n[item.tele]\nsearch_phrases = "televisor"\n')
    monitor._adopt_config(probe(monitor))
    first = control.config_applied()["seq"]

    save(config_file, TWO_SEARCHES + '\n[item.tele]\nsearch_phrases = "televisor 55"\n')
    monitor._adopt_config(probe(monitor))
    assert control.config_applied()["seq"] > first


def test_a_change_is_taken_up_once_not_at_every_checkpoint(
    monitor: MarketplaceMonitor, config_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """There used to be a "saved, will apply later" notice here.

    There is nothing left for it to say: the change is adopted at the first
    checkpoint that sees it.  What still matters is that it is adopted *once* --
    the guard runs between every pair of listings, and a monitor announcing the
    same change forty times is a monitor nobody reads the log of.
    """
    monitor._searching = ("ps5", "facebook")
    save(config_file, TWO_SEARCHES.replace('[item.bici]\nsearch_phrases = "bicicleta"\n', ""))
    with caplog.at_level(logging.INFO, logger="test-live-reload"):
        for _ in range(5):
            monitor._probe_at = 0.0  # the throttle is not what is under test
            monitor._config_guard()
    landed = [record for record in caplog.records
              if getattr(record, "aimm", {}).get("kind") == "config_applied"]
    assert len(landed) == 1


# --------------------------------------------------------------------------- #
# Ending one search without stopping the scraper
# --------------------------------------------------------------------------- #


def test_stopping_one_platform_ends_that_search_only(
    monitor: MarketplaceMonitor,
) -> None:
    monitor._searching = ("ps5", "facebook")
    control.request_search_stop("ps5", "facebook")
    with pytest.raises(SearchStopped) as raised:
        monitor._config_guard()
    assert raised.value.scope == "platform"
    # Spent by the search it stopped: the same product on the other platform is
    # not touched, which is the whole promise of the per-platform button.
    assert control.stop_requested("ps5", "mercadolibre") is None
    assert control.stop_requested("ps5", "facebook") is None


def test_stopping_a_search_reaches_the_platforms_it_has_not_started(
    monitor: MarketplaceMonitor,
) -> None:
    """"Stop this search" is pressed on a product, and a product runs on
    several platforms.  A stop that only ended the page currently open would
    leave the rest of the product to run exactly as before."""
    monitor._searching = ("ps5", "facebook")
    control.request_search_stop("ps5")
    with pytest.raises(SearchStopped) as raised:
        monitor._config_guard()
    assert raised.value.scope == "search"
    # Still standing, because Mercado Libre has not had its turn yet.
    assert control.stop_requested("ps5", "mercadolibre") is not None
    assert monitor._skip_stopped("ps5", "mercadolibre") is True
    # And nobody else is affected.
    assert monitor._skip_stopped("bici", "facebook") is False


def test_a_stop_beats_a_configuration_change(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    """Two reasons to end the same search, and only one of them is the user
    watching the screen at that moment."""
    monitor._searching = ("ps5", "facebook")
    control.request_search_stop("ps5", "facebook")
    save(config_file, TWO_SEARCHES.replace("playstation 5", "playstation 5 pro"))
    with pytest.raises(SearchStopped):
        monitor._config_guard()


def test_a_broken_file_is_complained_about_once(
    monitor: MarketplaceMonitor, config_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    save(config_file, "[item.ps5\n")
    with caplog.at_level(logging.WARNING, logger="test-live-reload"):
        for _ in range(5):
            probe(monitor)
    complaints = [record for record in caplog.records
                  if getattr(record, "aimm", {}).get("kind") == "config_unreadable"]
    assert len(complaints) == 1


def test_fixing_a_broken_file_is_noticed(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    save(config_file, "[item.ps5\n")
    assert not probe(monitor).readable
    save(config_file, TWO_SEARCHES + '\n[item.tele]\nsearch_phrases = "televisor"\n')
    result = probe(monitor)
    assert result.readable
    assert result.change.added == (("tele", "facebook"), ("tele", "mercadolibre"))


# --------------------------------------------------------------------------- #
# The queue across a reload
# --------------------------------------------------------------------------- #


class FakeJob:
    """Enough of a ``schedule.Job`` for :meth:`_job_key`."""

    def __init__(self, item: str, marketplace: str, slot: int = 0) -> None:
        self.amm_pair = (item, marketplace)
        self.amm_slot = slot


def test_a_search_untouched_by_a_reload_keeps_its_place(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    """Half a pass done, one search edited: the other eight must not run again.

    This is the whole reason the key carries a fingerprint rather than a name.
    """
    job = FakeJob("bici", "facebook")
    before = monitor._job_key(job)
    save(config_file, TWO_SEARCHES.replace("playstation 5", "playstation 5 pro"))
    monitor._adopt_config(probe(monitor))
    assert monitor._job_key(job) == before


def test_an_edited_search_comes_back_as_a_different_job(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    job = FakeJob("ps5", "facebook")
    before = monitor._job_key(job)
    save(config_file, TWO_SEARCHES.replace("playstation 5", "playstation 5 pro"))
    monitor._adopt_config(probe(monitor))
    # A new key means "not yet run in this pass", which is how the edit takes
    # effect now instead of at the search's next scheduled slot.
    assert monitor._job_key(job) != before


def test_a_platform_edit_makes_every_search_on_it_a_different_job(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    jobs: List[FakeJob] = [FakeJob("ps5", "facebook"), FakeJob("bici", "facebook")]
    before = [monitor._job_key(job) for job in jobs]
    save(config_file, TWO_SEARCHES.replace('search_city = "houston"', 'search_city = "dallas"'))
    monitor._adopt_config(probe(monitor))
    assert [monitor._job_key(job) for job in jobs] != before


def test_two_slots_of_one_search_are_two_jobs(monitor: MarketplaceMonitor) -> None:
    """An item on both an interval and a fixed time has two jobs.

    They must not collapse into one key, or running the first would mark the
    second as already done.
    """
    assert monitor._job_key(FakeJob("ps5", "facebook", 0)) != monitor._job_key(
        FakeJob("ps5", "facebook", 1)
    )


def test_the_loaded_configuration_is_republished_on_adoption(
    monitor: MarketplaceMonitor, config_file: Path
) -> None:
    """A deleted search must stop being reported as one the scraper runs.

    Its history goes with it: leaving "last run 3 minutes ago" on screen for a
    search that no longer exists is the interface claiming the scraper is still
    doing something it has just been told to stop.
    """
    control.set_next_runs(
        {"ps5": "2026-01-01T00:00:00+00:00", "bici": "2026-01-01T01:00:00+00:00"}
    )
    for item in ("ps5", "bici"):
        with control.search(item, "facebook"):
            pass
    assert {entry["item"] for entry in control.searches()} == {"ps5", "bici"}

    save(config_file, TWO_SEARCHES.replace('[item.ps5]\nsearch_phrases = "playstation 5"\n', ""))
    monitor._adopt_config(probe(monitor))

    assert [entry["item"] for entry in control.searches()] == ["bici"]
    # The one that survived keeps everything it had.
    assert control.searches()[0]["next_run"] == "2026-01-01T01:00:00+00:00"
    loaded = control.loaded_config()
    assert {entry["item"] for entry in loaded["searches"]} == {"bici"}


# --------------------------------------------------------------------------- #
# A pass that has changes saved under it
# --------------------------------------------------------------------------- #
#
# The behaviour the user described, end to end: the pass keeps its place, the
# search that was changed is dropped where it stands, the next one starts, and
# nothing that was already searched is searched twice for it.


class PassJob:
    """A stand-in for one scheduled search.

    Runs the real checkpoint -- ``control.raise_if_cancelled`` -- with the
    monitor's guard installed, so a configuration saved "during" this search
    reaches it exactly as it would through Playwright.
    """

    def __init__(self, monitor: MarketplaceMonitor, item: str, marketplace: str = "facebook",
                 slot: int = 0) -> None:
        self.monitor = monitor
        self.amm_pair = (item, marketplace)
        self.amm_slot = slot
        self.tags = {item}
        self.should_run = False
        #: Called once, at this search's first checkpoint.  The test's way of
        #: saying "the user pressed save while this was running".
        self.while_running = None

    def run(self) -> None:
        self.monitor._searching = self.amm_pair
        try:
            if self.while_running is not None:
                action, self.while_running = self.while_running, None
                action()
            control.raise_if_cancelled()
        finally:
            self.monitor._searching = None


class PassSchedule:
    """Enough of the ``schedule`` module for :meth:`_run_jobs`."""

    def __init__(self) -> None:
        self.jobs: List[PassJob] = []

    def get_jobs(self) -> List[PassJob]:
        return list(self.jobs)

    def clear(self) -> None:
        self.jobs = []


@pytest.fixture
def pass_monitor(
    monitor: MarketplaceMonitor, monkeypatch: pytest.MonkeyPatch
) -> MarketplaceMonitor:
    """A monitor whose pass loop is real and whose browser and clock are not."""
    from ai_marketplace_monitor import monitor as monitor_module

    fake = PassSchedule()
    monkeypatch.setattr(monitor_module, "schedule", fake)
    control.set_checkpoint_guard(monitor._config_guard)

    monitor.schedule = fake  # type: ignore[attr-defined]
    monitor.ran = []  # type: ignore[attr-defined]
    monitor.wait_while_paused = lambda: None  # type: ignore[method-assign]
    monitor.handle_pause = lambda: None  # type: ignore[method-assign]
    monitor._ensure_browser = lambda: None  # type: ignore[method-assign]
    monitor._apply_pending_sessions = lambda: None  # type: ignore[method-assign]
    monitor._publish_schedule = lambda: None  # type: ignore[method-assign]
    monitor._refresh_slice = lambda *args, **kwargs: True  # type: ignore[method-assign]

    def rebuild() -> None:
        """What ``schedule_jobs`` would do, from the configuration in hand."""
        assert monitor.config is not None
        fake.jobs = [
            PassJob(monitor, item, marketplace)
            for (marketplace, item), item_config in sorted(monitor.config.items.items())
            if item_config.enabled is not False
        ]

    monitor._rebuild_schedule = rebuild  # type: ignore[method-assign]

    real_run_job = monitor._run_job

    def run_job(job: PassJob):  # type: ignore[no-untyped-def]
        monitor.ran.append(job.amm_pair[0])  # type: ignore[attr-defined]
        return real_run_job(job)

    monitor._run_job = run_job  # type: ignore[method-assign]
    rebuild()
    return monitor


def test_a_pass_runs_every_search_once(pass_monitor: MarketplaceMonitor) -> None:
    assert pass_monitor._run_jobs() is True
    # Each product once per platform.
    assert sorted(pass_monitor.ran) == ["bici", "bici", "ps5", "ps5"]


def test_deleting_the_running_search_moves_on_to_the_next(
    pass_monitor: MarketplaceMonitor, config_file: Path
) -> None:
    """The case the user asked about, exactly.

    The search being scraped is deleted from the web UI.  It stops where it is
    -- not when it happens to finish -- and the pass carries on with the next
    one instead of starting over or stopping.
    """
    first = pass_monitor.schedule.jobs[0]
    assert first.amm_pair[0] == "bici"
    first.while_running = lambda: save(
        config_file, TWO_SEARCHES.replace('[item.bici]\nsearch_phrases = "bicicleta"\n', "")
    )

    assert pass_monitor._run_jobs() is True
    # bici stopped where it was and never came back -- not on this platform and
    # not on the other, because the product itself is gone.
    assert pass_monitor.ran == ["bici", "ps5", "ps5"]
    # It really was dropped rather than allowed to finish.
    assert control.config_applied()["interrupted"]["item"] == "bici"
    assert set(pass_monitor.config.items) == {("facebook", "ps5"), ("mercadolibre", "ps5")}


def test_deleting_a_search_that_has_not_run_yet_means_it_never_runs(
    pass_monitor: MarketplaceMonitor, config_file: Path
) -> None:
    first = pass_monitor.schedule.jobs[0]
    first.while_running = lambda: save(
        config_file, TWO_SEARCHES.replace('[item.ps5]\nsearch_phrases = "playstation 5"\n', "")
    )

    assert pass_monitor._run_jobs() is True
    # ps5 is deleted while bici's first platform is being searched, so bici
    # still runs on both and ps5 on neither.
    assert pass_monitor.ran == ["bici", "bici"]


def test_a_search_already_done_is_not_searched_again_for_someone_else_s_edit(
    pass_monitor: MarketplaceMonitor, config_file: Path
) -> None:
    """The old behaviour was to clear the schedule and start the pass over.

    Adding one product would then re-scrape every other one, which is a burst
    of traffic the marketplace notices and results nobody asked to refresh.
    """
    first = pass_monitor.schedule.jobs[0]
    first.while_running = lambda: save(
        config_file, TWO_SEARCHES + '\n[item.tele]\nsearch_phrases = "televisor"\n'
    )

    assert pass_monitor._run_jobs() is True
    # The new product is picked up and searched on both platforms; the two that
    # were already running or done are not repeated for it.
    assert pass_monitor.ran == ["bici", "bici", "ps5", "ps5", "tele", "tele"]


def test_editing_a_search_that_already_ran_makes_it_run_again(
    pass_monitor: MarketplaceMonitor, config_file: Path
) -> None:
    """Its results came from the old settings, so they do not answer the new ones."""
    first = pass_monitor.schedule.jobs[0]
    first.while_running = lambda: save(
        config_file, TWO_SEARCHES.replace("bicicleta", "bicicleta rodado 29")
    )

    assert pass_monitor._run_jobs() is True
    # Dropped as soon as the edit landed, then run again under the new phrase —
    # so bici appears three times: the abandoned Facebook run, its repeat, and
    # Mercado Libre, which had not started yet.
    assert pass_monitor.ran == ["bici", "bici", "bici", "ps5", "ps5"]


def test_a_targeted_pass_leaves_the_untouched_searches_alone(
    pass_monitor: MarketplaceMonitor,
) -> None:
    """What a save while the monitor is idle triggers: only what changed."""
    assert pass_monitor._run_jobs(only={("ps5", "facebook")}) is True
    assert pass_monitor.ran == ["ps5"]


def test_a_file_broken_during_a_pass_stops_the_pass_cleanly(
    pass_monitor: MarketplaceMonitor, config_file: Path
) -> None:
    """Half a save is not a configuration.

    The pass ends without a search being abandoned and without an exception;
    the loader is where a broken file gets reported and waited on.
    """
    first = pass_monitor.schedule.jobs[0]
    first.while_running = lambda: save(config_file, "[item.ps5")

    assert pass_monitor._run_jobs() is True
    # And what it was running is still what it is running.
    assert set(pass_monitor.config.items) == {
        ("facebook", "ps5"),
        ("facebook", "bici"),
        ("mercadolibre", "ps5"),
        ("mercadolibre", "bici"),
    }


def test_deleting_every_search_leaves_the_pass_with_nothing_to_do(
    pass_monitor: MarketplaceMonitor, config_file: Path
) -> None:
    """Zero searches is a state the user is entitled to, not a fault.

    The pass has to end quietly on it -- the loop above handles an empty
    configuration by waiting for one to appear, and it can only do that if the
    pass hands control back rather than spinning or stopping the monitor.
    """
    first = pass_monitor.schedule.jobs[0]
    first.while_running = lambda: save(
        config_file,
        TWO_SEARCHES.replace('[item.bici]\nsearch_phrases = "bicicleta"\n', "").replace(
            '[item.ps5]\nsearch_phrases = "playstation 5"\n', ""
        ),
    )

    assert pass_monitor._run_jobs() is True
    # Everything is gone by the time the first search reaches its checkpoint,
    # so it is dropped and there is nothing left to run.
    assert pass_monitor.ran == ["bici"]
    assert pass_monitor.schedule.jobs == []
    assert pass_monitor.config.items == {}
