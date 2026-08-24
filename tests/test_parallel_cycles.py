"""Two platforms searching at once, each with a cycle of its own.

The report this comes from: with parallel searching on, two platforms were
searching a PS5.  Facebook was stopped by hand and moved on to the next product
by itself, correctly, on the same browser.  Mercado Libre was then stopped too
-- and did *not* move on.  It sat there until Facebook had finished everything
it had, and when it finally did start its next search it appeared to be driving
the browser Facebook had been using.

It was never reproduced, so nothing here is a test of that transcript.  What it
is a test of is the two things in the code that would produce exactly it:

1. **The pass was a barrier.**  Work was handed out, and then the monitor
   thread joined every lane before it touched the schedule again.  A lane that
   emptied its queue in two minutes was left holding an open browser for the
   fifty the slowest participant took, with its next search not even chosen and
   its "próxima ejecución" still showing a slot that had gone by.  Two searches
   ran at once and their *cycles* were locked together, which is the one thing
   parallel searching was for.

2. **A platform's browser was decided per pass.**  The rule was "the first
   platform in this pass runs on the monitor's own browser, the rest get
   lanes", and a pass was built from whatever was due at that instant.  So a
   platform that had a lane at 14:00, because something else was due
   alongside it, ran on the monitor's browser at 14:20 when it was the only
   one due -- and from outside, that is a search inheriting the browser
   another platform had been using.

Nothing here opens a browser.  Searching is a function that records what it was
asked for, on which thread, and how long it waited.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
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

[monitor]
parallel_marketplaces = true

[item.ps5]
search_phrases = "playstation 5"

[item.switch]
search_phrases = "nintendo switch"
"""


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    control.reset_for_tests()
    schedule.clear()
    yield
    schedule.clear()
    control.reset_for_tests()


class FakeContext:
    """Stands in for a BrowserContext.  Identity is what is looked at."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False

    @property
    def browser(self):
        return None

    @property
    def pages(self):
        if self.closed:
            raise RuntimeError("target closed")
        return []

    def close(self) -> None:
        self.closed = True


class _FakePlaywright:
    def stop(self) -> None:
        pass


class _FakeStarter:
    def start(self) -> "_FakePlaywright":
        return _FakePlaywright()


class Recorder:
    """What ran, on which thread, on which browser, and when."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []
        self.threads: List[str] = []
        self.at: List[float] = []
        self.contexts: List[str] = []
        self._lock = threading.Lock()
        self.on_call = None

    def __call__(self, marketplace_config, marketplace, item_config) -> None:
        with self._lock:
            self.calls.append((item_config.name, marketplace_config.name))
            self.threads.append(threading.current_thread().name)
            self.at.append(time.monotonic())
            self.contexts.append(getattr(marketplace, "context_name", "main"))
        # The real `search_item` registers the pair here, and it is what the
        # interface reads; a stub that skipped it would leave these tests
        # asserting about an empty state.
        with control.search(item_config.name, marketplace_config.name):
            if self.on_call is not None:
                self.on_call(marketplace_config.name, item_config.name)

    def when(self, item: str, marketplace: str) -> float:
        return self.at[self.calls.index((item, marketplace))]

    @property
    def pairs(self) -> List[Tuple[str, str]]:
        return list(self.calls)


def _future(stamp: str | None) -> bool:
    """Whether this published slot is still to come, offsets and all."""
    return stamp is not None and datetime.fromisoformat(stamp) > datetime.now(timezone.utc)


class FakeMarketplace:
    def __init__(self, context_name: str) -> None:
        self.context_name = context_name


def build(tmp_path: Path, config_text: str = TWO_PLATFORMS) -> MarketplaceMonitor:
    path = tmp_path / "config.toml"
    path.write_text(config_text, encoding="utf-8")
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
    instance.logger = logging.getLogger("test-parallel-cycles")
    instance.keyboard_monitor = None
    instance.context = None
    instance.lanes = {}
    instance.refresher = None
    instance.active_marketplaces = {}
    instance._browsers_idle = False
    instance._review_due = float("inf")
    instance.load_config_file()
    return instance


def stub(monitor: MarketplaceMonitor, monkeypatch, recorder: Recorder) -> None:
    """Everything a pass does with a browser, replaced; the queueing kept."""
    monkeypatch.setattr("ai_marketplace_monitor.lanes.sync_playwright", lambda: _FakeStarter())
    monkeypatch.setattr(monitor, "search_item", recorder)
    monkeypatch.setattr(monitor, "_ensure_browser", lambda: None)
    monkeypatch.setattr(monitor, "_apply_pending_sessions", lambda: None)
    monkeypatch.setattr(monitor, "_refresh_slice", lambda *a, **k: True)
    monkeypatch.setattr(monitor, "wait_while_paused", lambda: None)
    monkeypatch.setattr(monitor, "handle_pause", lambda: None)
    monkeypatch.setattr("ai_marketplace_monitor.monitor.is_paused", lambda: False)
    monkeypatch.setattr(monitor, "_select_translator", lambda language=None: None)
    monkeypatch.setattr(
        "ai_marketplace_monitor.marketplace.Marketplace.configure",
        lambda self, config, translator=None: None,
    )
    contexts: dict = {}

    def launch(playwright=None, lane=None):
        name = lane or "main"
        contexts.setdefault(name, FakeContext(name))
        return contexts[name]

    monkeypatch.setattr(monitor, "_launch_context", launch)
    monkeypatch.setattr(
        monitor,
        "_lane_marketplace",
        lambda lane, ctx, marketplace_config: FakeMarketplace(ctx.name),
    )
    monitor._test_contexts = contexts


# --------------------------------------------------------------------------- #
# Identity: a platform's browser is its own
# --------------------------------------------------------------------------- #


def test_each_platform_is_bound_to_one_browser(tmp_path, monkeypatch):
    monitor = build(tmp_path)
    stub(monitor, monkeypatch, Recorder())
    monitor.schedule_jobs()
    monitor._bind_platforms(["facebook", "mercadolibre"])

    # One of them keeps the monitor's own browser -- the profile an interactive
    # login signed in -- and the other gets a lane named after itself.
    assert sorted(monitor._browser_of) == ["facebook", "mercadolibre"]
    assert sorted(monitor._browser_of.values()) == ["", "mercadolibre"]
    assert monitor._main_platform() == "facebook"


def test_the_binding_does_not_change_when_the_other_platform_is_absent(
    tmp_path, monkeypatch
):
    """The bug: a lane-owned platform running alone used to become "first"."""
    monitor = build(tmp_path)
    stub(monitor, monkeypatch, Recorder())
    monitor.schedule_jobs()
    monitor._bind_platforms(["facebook", "mercadolibre"])

    # A later pass in which only Mercado Libre is due must not promote it onto
    # the monitor's own browser.
    monitor._bind_platforms(["mercadolibre", "facebook"])
    assert monitor._browser_of["mercadolibre"] == "mercadolibre"
    assert monitor._browser_of["facebook"] == ""


def test_a_lane_owned_platform_alone_still_runs_on_its_own_lane(tmp_path, monkeypatch):
    """The symptom that was reported, as a test.

    Mercado Libre is searched with nothing else due.  Before, that made it the
    "first" platform of the pass and it ran on this thread, against the browser
    Facebook normally drives.
    """
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()
    monitor._bind_platforms(["facebook", "mercadolibre"])

    pairs = {("ps5", "mercadolibre"), ("switch", "mercadolibre")}
    assert monitor._run_jobs(only=pairs) is True

    assert sorted(recorder.pairs) == [("ps5", "mercadolibre"), ("switch", "mercadolibre")]
    assert set(recorder.contexts) == {"mercadolibre"}
    assert threading.current_thread().name not in recorder.threads
    monitor._close_lanes()


def test_a_forgotten_platform_stops_being_bound(tmp_path, monkeypatch):
    monitor = build(tmp_path)
    stub(monitor, monkeypatch, Recorder())
    monitor._bind_platforms(["facebook", "mercadolibre"])
    monitor._bind_platforms(["mercadolibre"])

    # Facebook is gone from the configuration, so its binding is.  Mercado
    # Libre keeps the browser it has been using: nothing about the platform
    # that was deleted is a reason to move the one that was not, and moving it
    # is precisely the instability these tests exist for.
    assert monitor._browser_of == {"mercadolibre": "mercadolibre"}
    # A platform added later takes the browser that was freed up.
    monitor._bind_platforms(["mercadolibre", "facebook"])
    assert monitor._browser_of["facebook"] == ""


# --------------------------------------------------------------------------- #
# Two searches, two platforms, at the same time
# --------------------------------------------------------------------------- #


def test_two_platforms_search_at_the_same_time(tmp_path, monkeypatch):
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()

    assert monitor._run_jobs() is True
    assert sorted(recorder.pairs) == [
        ("ps5", "facebook"),
        ("ps5", "mercadolibre"),
        ("switch", "facebook"),
        ("switch", "mercadolibre"),
    ]
    assert len(set(recorder.threads)) == 2
    monitor._close_lanes()


def test_a_lane_does_not_wait_for_the_other_platform_s_whole_queue(
    tmp_path, monkeypatch
):
    """The barrier, as a measurement.

    Facebook is made slow and Mercado Libre fast.  Mercado Libre's *second*
    search has to start while Facebook is still working through its first --
    which is exactly what did not happen before, because its next search was
    not even chosen until every lane had been joined.
    """
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)

    def pace(marketplace: str, _item: str) -> None:
        if marketplace == "facebook":
            time.sleep(0.6)

    recorder.on_call = pace
    monitor.schedule_jobs()
    assert monitor._run_jobs() is True

    ml_second = max(
        recorder.when("ps5", "mercadolibre"), recorder.when("switch", "mercadolibre")
    )
    fb_last = max(recorder.when("ps5", "facebook"), recorder.when("switch", "facebook"))
    assert ml_second < fb_last
    monitor._close_lanes()


def test_stopping_one_platform_leaves_the_other_running(tmp_path, monkeypatch):
    """Cancelling a platform is not cancelling the pass."""
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)

    # Asked for from the web UI before its turn came round, which is what
    # "Detener búsqueda de esta plataforma" does to a search that is queued.
    control.request_search_stop("ps5", "mercadolibre")
    monitor.schedule_jobs()
    assert monitor._run_jobs() is True

    # Everything except the search that was stopped, including the other
    # search on the platform it was stopped on.
    assert ("ps5", "mercadolibre") not in recorder.pairs
    assert ("switch", "mercadolibre") in recorder.pairs
    assert ("ps5", "facebook") in recorder.pairs
    assert ("switch", "facebook") in recorder.pairs
    monitor._close_lanes()


def test_a_search_does_not_inherit_the_previous_one_s_marketplace(tmp_path, monkeypatch):
    """Every search on a lane sees that lane's browser and no other."""
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()
    monitor._run_jobs()

    by_platform: dict = {}
    for (_item, platform), context in zip(recorder.pairs, recorder.contexts):
        by_platform.setdefault(platform, set()).add(context)
    assert all(len(seen) == 1 for seen in by_platform.values()), by_platform
    assert by_platform["mercadolibre"] == {"mercadolibre"}
    monitor._close_lanes()


def test_a_lane_s_searches_advance_the_schedule_as_they_finish(tmp_path, monkeypatch):
    """And the interface is told, which is the "próxima ejecución" complaint.

    `_mark_ran` publishes on the way out.  Before, the job really did still
    hold the slot that had gone by when the state was last published: the
    publish in `_run_job`'s `finally` runs before the caller advances the job,
    and nothing published again until the next search ended.
    """
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()
    monitor._run_jobs()

    for job in schedule.get_jobs():
        assert job.last_run is not None, job.amm_pair
    published = {
        (entry["item"], entry["marketplace"]): entry
        for entry in control.searches()
    }
    assert len(published) == 4
    # Every pair has a slot, it is in the future, and the monitor does not
    # think it is overdue: a slot left in the past is what the interface
    # renders as "en cualquier momento" under a label that says "próxima".
    for pair, entry in published.items():
        assert entry["next_run"] is not None, pair
        assert _future(entry["next_run"]), (pair, entry["next_run"])
        assert entry["due_now"] is False, pair
    monitor._close_lanes()


def test_cancelling_a_platform_republishes_its_next_run(tmp_path, monkeypatch):
    """The "próxima ejecución" complaint, reduced to its cause.

    A job that is waiting its turn holds a slot that has already gone by --
    that is what "due" means to the scheduler.  Cancelling the search advances
    it, and the interface has to be told, or it keeps rendering the slot that
    passed.
    """
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()
    # Register the pair the way a search does, without running one.
    with control.search("ps5", "mercadolibre"):
        pass
    # A job waiting its turn behind another search: its slot came and went.
    for job in schedule.get_jobs():
        if job.amm_pair == ("ps5", "mercadolibre"):
            job.next_run = datetime.now() - timedelta(minutes=8)
    monitor._publish_schedule()

    before = next(
        entry
        for entry in control.searches()
        if (entry["item"], entry["marketplace"]) == ("ps5", "mercadolibre")
    )
    assert before["due_now"] is True
    assert not _future(before["next_run"])

    monitor._mark_ran({("ps5", "mercadolibre")})

    after = next(
        entry
        for entry in control.searches()
        if (entry["item"], entry["marketplace"]) == ("ps5", "mercadolibre")
    )
    assert _future(after["next_run"])
    assert after["due_now"] is False
    monitor._close_lanes()


def test_a_slot_in_another_time_zone_is_not_read_as_overdue():
    """Offsets are parsed, not compared as text.

    The scheduler publishes local time with its own offset and `control`
    stamps in UTC.  Compared as strings, a Chilean "13:37-04:00" is always
    "less than" the UTC "17:37+00:00" of the same instant, so every waiting
    search read as overdue; four hours the other way it would read as not yet
    due when it already was.
    """
    soon = datetime.now(timezone(timedelta(hours=-4))) + timedelta(hours=1)
    past = datetime.now(timezone(timedelta(hours=+9))) - timedelta(hours=1)
    assert control.has_passed(soon.isoformat(timespec="seconds")) is False
    assert control.has_passed(past.isoformat(timespec="seconds")) is True
    assert control.has_passed(None) is False
    assert control.has_passed("not a time") is False


def test_a_new_search_after_a_cancelled_one_starts_at_once(tmp_path, monkeypatch):
    """Stopping a platform's search hands its lane the next one immediately."""
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)

    def pace(marketplace: str, _item: str) -> None:
        if marketplace == "facebook":
            time.sleep(0.4)

    recorder.on_call = pace
    control.request_search_stop("ps5", "mercadolibre")
    monitor.schedule_jobs()
    started = time.monotonic()
    monitor._run_jobs()

    # The next Mercado Libre search runs on its own lane, and does not wait for
    # Facebook to work through both of its.
    assert ("switch", "mercadolibre") in recorder.pairs
    assert recorder.when("switch", "mercadolibre") - started < 0.4
    monitor._close_lanes()


def test_a_lane_that_fails_does_not_take_the_other_down(tmp_path, monkeypatch):
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)

    def sometimes_fails(marketplace_config, marketplace, item_config):
        if marketplace_config.name == "mercadolibre" and item_config.name == "ps5":
            raise RuntimeError("that platform is having a bad day")
        recorder(marketplace_config, marketplace, item_config)

    monkeypatch.setattr(monitor, "search_item", sometimes_fails)
    monitor.schedule_jobs()

    assert monitor._run_jobs() is True
    assert sorted(recorder.pairs) == [
        ("ps5", "facebook"),
        ("switch", "facebook"),
        ("switch", "mercadolibre"),
    ]
    monitor._close_lanes()


def test_two_lanes_are_never_given_the_same_search(tmp_path, monkeypatch):
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()
    monitor._run_jobs()

    assert len(recorder.pairs) == len(set(recorder.pairs))
    monitor._close_lanes()


# --------------------------------------------------------------------------- #
# The controls, in a pass that is not a single queue
# --------------------------------------------------------------------------- #
#
# Both of these were broken for as long as parallel searching existed, and
# harmlessly so while it was off by default.  It is on by default now, so they
# are the difference between two buttons working and two buttons not.
#
# The cause was the same for both: `_run_jobs_in_parallel` ran its own share
# through `_run_jobs_sequentially` narrowed to one platform, and a pass narrowed
# to one platform is exactly the case that declines to honour a promotion or
# spend a stop -- correctly, because it cannot see the other platforms.  So the
# whole pass has to claim them, and it does.


def test_a_promoted_search_goes_first_on_every_platform(tmp_path, monkeypatch):
    """"Search this one next" is a promise about the next search, not about
    the next search *on one platform*."""
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()

    # "switch" is second in the file, so this is a real promotion.
    control.set_next_search("switch")
    assert monitor._run_jobs() is True

    first_on = {}
    for item, platform in recorder.pairs:
        first_on.setdefault(platform, item)
    assert first_on == {"facebook": "switch", "mercadolibre": "switch"}
    monitor._close_lanes()


def test_the_promotion_is_spent_by_the_pass_that_honoured_it(tmp_path, monkeypatch):
    """Claimed once for the whole pass, not once per platform.

    Two claims would leave the promotion standing after the pass that already
    honoured it, and the same product would jump the queue for ever.
    """
    monitor = build(tmp_path)
    stub(monitor, monkeypatch, Recorder())
    monitor.schedule_jobs()

    control.set_next_search("switch")
    monitor._run_jobs()

    assert control.next_search() is None
    monitor._close_lanes()


def test_a_stop_is_spent_by_the_pass_it_was_made_for(tmp_path, monkeypatch):
    """Otherwise it silently skips the same search on every later pass.

    Switching a search off for one round is what the button means; a stop that
    outlived its pass would be switching it off for good, with nothing on the
    screen saying so.
    """
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()

    control.request_search_stop("ps5")
    assert monitor._run_jobs() is True
    assert ("ps5", "facebook") not in recorder.pairs
    assert ("ps5", "mercadolibre") not in recorder.pairs
    assert control.search_stops() == []

    # And the next pass searches it again, which is the whole point.
    recorder.calls.clear()
    schedule.clear()
    monitor.schedule_jobs()
    assert monitor._run_jobs() is True
    assert ("ps5", "facebook") in recorder.pairs
    assert ("ps5", "mercadolibre") in recorder.pairs
    monitor._close_lanes()


def test_a_targeted_pass_does_not_spend_the_stops_it_cannot_see(tmp_path, monkeypatch):
    """A pass over two edited searches is not the pass a stop belonged to."""
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()

    control.request_search_stop("switch")
    monitor._run_jobs(only={("ps5", "facebook"), ("ps5", "mercadolibre")})

    assert control.search_stops() != []
    monitor._close_lanes()


def test_a_targeted_pass_does_spend_the_stops_it_held(tmp_path, monkeypatch):
    """The other half, and the one that was missing.

    Saving a search starts a pass narrowed to it, and with parallel searching on
    -- the default -- that pass is *this* method.  Neither half cleared its
    stops, so the request stayed in the register with nothing left to honour it:
    the interface reported the search as "deteniendose..." indefinitely while
    its next run counted down beside it, and the next full pass skipped it once
    for a button pressed an hour earlier.
    """
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()

    pairs = {("ps5", "facebook"), ("ps5", "mercadolibre")}
    control.request_search_stop("ps5")
    monitor._run_jobs(only=pairs)

    assert ("ps5", "facebook") not in recorder.pairs, "the stop still has to stop it"
    assert control.search_stops() == [], "and then stop being pending"
    monitor._close_lanes()


# --------------------------------------------------------------------------- #
# No browser is opened for work that is not going to happen
# --------------------------------------------------------------------------- #


def test_no_lane_is_opened_for_a_platform_on_cooldown(tmp_path, monkeypatch):
    """A window at about:blank for a pass that never ran.

    The lane used to be started first and find out afterwards that every one of
    its searches was going to be skipped, which left a browser open showing
    nothing -- the "extra browser" that gets reported.
    """
    monitor = build(tmp_path)
    stub(monitor, monkeypatch, Recorder())
    monitor.schedule_jobs()
    monitor._bind_platforms(["facebook", "mercadolibre"])

    control.block_marketplace("mercadolibre", reason="asked us to sign in")
    assert monitor._run_jobs() is True

    assert "mercadolibre" not in monitor.lanes
    assert "mercadolibre" not in monitor._test_contexts
    monitor._close_lanes()


def test_no_lane_is_opened_when_every_one_of_its_searches_is_stopped(
    tmp_path, monkeypatch
):
    monitor = build(tmp_path)
    stub(monitor, monkeypatch, Recorder())
    monitor.schedule_jobs()
    monitor._bind_platforms(["facebook", "mercadolibre"])

    control.request_search_stop("ps5", "mercadolibre")
    control.request_search_stop("switch", "mercadolibre")
    assert monitor._run_jobs() is True

    assert "mercadolibre" not in monitor._test_contexts
    monitor._close_lanes()


def test_a_lane_is_still_opened_when_one_of_its_searches_survives(tmp_path, monkeypatch):
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()
    monitor._bind_platforms(["facebook", "mercadolibre"])

    control.request_search_stop("ps5", "mercadolibre")
    assert monitor._run_jobs() is True

    assert ("switch", "mercadolibre") in recorder.pairs
    monitor._close_lanes()
