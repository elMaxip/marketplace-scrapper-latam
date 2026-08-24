"""Mandar sobre la ejecución sin detener el scraper, y respetar los intervalos.

Four behaviours, all of them about the same misconception the monitor used to
have: that the only ways to affect a running loop are "pause everything" and
"edit the file".

* **Stop this platform.**  One ``(item, marketplace)`` search ends where it
  stands and nothing else is touched -- not the same product on another
  platform, not the queue, not the browser.
* **Stop this search.**  The product is done for this pass, *including the
  platforms it has not started yet*.  A stop that only ended the page currently
  open would leave the rest of the product running exactly as before, which is
  not what the button says.
* **Run this one next.**  A product jumps the queue without the search under way
  being interrupted.  The promise is the next search, not this one.
* **"Iniciar" respects the intervals.**  Starting the scraper is not an
  instruction to override every timer the user set.  ``schedule`` starts each
  job's clock when the job is built, so a rebuilt schedule -- which happens on
  every save and every start -- used to hand all of them a fresh interval; the
  monitor now remembers when each pair actually last ran and seeds the jobs from
  that.

Nothing here opens a browser: searching is replaced by a recorder, which is
exactly what is under test -- what runs, in what order, and what does not.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest
import schedule

from ai_marketplace_monitor import control
from ai_marketplace_monitor.monitor import MarketplaceMonitor
from ai_marketplace_monitor.utils import CacheType, cache

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
search_interval = "30m"
# Sequential, said rather than assumed. Most of these tests are about controls
# that work the same either way, but one of them is about the order of the
# single queue -- and "the order" is not a thing a parallel pass has, since
# each platform runs its own queue at the same time as the other.
parallel_marketplaces = false
parallel_listing_updates = false

[item.ps5]
search_phrases = "playstation 5"

[item.bici]
search_phrases = "bicicleta"
"""


class Recorder:
    """Stands in for searching.  Records the pair it was asked for."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []
        self._lock = threading.Lock()

    def __call__(self, marketplace_config, marketplace, item_config) -> None:
        with self._lock:
            self.calls.append((item_config.name, marketplace_config.name))

    @property
    def items(self) -> List[str]:
        """Just the products, in the order they were searched."""
        return [item for item, _marketplace in self.calls]


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    control.reset_for_tests()
    schedule.clear()
    _forget_runs()
    yield
    schedule.clear()
    control.reset_for_tests()
    _forget_runs()


def _forget_runs() -> None:
    """Drop the persisted run times.  They outlive a process, hence a test."""
    for key in list(cache.iterkeys()):
        if isinstance(key, tuple) and key and key[0] == CacheType.SEARCH_RUNS.value:
            cache.delete(key)


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
    instance._schedule_dirty = False
    instance.logger = logging.getLogger("test-run-controls")
    instance.keyboard_monitor = None
    instance.context = None
    instance.lanes = {}
    instance.refresher = None
    instance.active_marketplaces = {}
    instance._review_due = float("inf")  # no review gets in the way of a pass
    instance.load_config_file()
    return instance


def stub(monitor: MarketplaceMonitor, monkeypatch, recorder: Recorder) -> None:
    monkeypatch.setattr(monitor, "search_item", recorder)
    monkeypatch.setattr(monitor, "_ensure_browser", lambda: None)
    monkeypatch.setattr(monitor, "_apply_pending_sessions", lambda: None)
    monkeypatch.setattr(monitor, "_refresh_slice", lambda *args, **kwargs: True)
    monkeypatch.setattr(monitor, "wait_while_paused", lambda: None)
    monkeypatch.setattr(monitor, "handle_pause", lambda: None)


# --------------------------------------------------------------------------- #
# Ending a search without ending the pass
# --------------------------------------------------------------------------- #


def test_stopping_one_platform_leaves_the_rest_of_the_product(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()

    control.request_search_stop("ps5", "facebook")
    assert monitor._run_jobs() is True

    assert ("ps5", "facebook") not in recorder.calls
    # Everything else ran, including the same product elsewhere.
    assert ("ps5", "mercadolibre") in recorder.calls
    assert ("bici", "facebook") in recorder.calls


def test_stopping_a_search_reaches_the_platforms_it_never_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()

    control.request_search_stop("ps5")
    assert monitor._run_jobs() is True

    assert "ps5" not in recorder.items
    # And the scraper carried straight on: this is not a pause.
    assert sorted(recorder.calls) == [("bici", "facebook"), ("bici", "mercadolibre")]


def test_a_stop_lasts_one_pass_and_no_longer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anything longer-lived is what switching the search off is for.

    A stop that survived its pass would silently skip the same search next time
    round, with nothing on screen to say why.
    """
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()

    control.request_search_stop("ps5")
    monitor._run_jobs()
    assert control.search_stops() == []

    recorder.calls.clear()
    monitor._run_jobs()
    assert "ps5" in recorder.items


def test_a_stop_in_a_targeted_pass_is_spent_by_that_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pass that saving a search starts is narrowed to the searches it
    touched -- and it is the only thing that will ever spend their stops.

    This is the whole of the "deteniendose..." that never went away.  Only a
    pass over the *whole* queue cleared the register, so a stop made during a
    narrowed pass (which is what saving a new search starts) was cleared by
    nobody: the interface went on reporting the search as stopping, accurately,
    while "proxima ejecucion" counted down beside it perfectly normally.
    """
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()

    pairs = {("ps5", "facebook"), ("ps5", "mercadolibre")}
    control.request_search_stop("ps5")
    assert monitor._run_jobs(only=pairs) is True

    assert "ps5" not in recorder.items, "the stop still has to stop the search"
    assert control.search_stops() == [], "and then stop being pending"


def test_a_targeted_pass_leaves_another_search_s_stop_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It spends the stops of the searches it held, and no others: a pass that
    never ran a search cannot know whether its stop has been honoured yet."""
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()

    control.request_search_stop("bici")
    monitor._run_jobs(only={("ps5", "facebook")})

    assert control.stop_requested("bici", "facebook") is not None


# --------------------------------------------------------------------------- #
# Choosing what runs next
# --------------------------------------------------------------------------- #


def test_the_chosen_search_goes_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()

    # "bici" comes second in the file, so this is a real promotion.
    control.set_next_search("bici")
    assert monitor._run_jobs() is True

    assert recorder.items[0] == "bici"
    # Nothing was skipped: it went first, it did not go instead.
    assert sorted(recorder.calls) == [
        ("bici", "facebook"),
        ("bici", "mercadolibre"),
        ("ps5", "facebook"),
        ("ps5", "mercadolibre"),
    ]


def test_the_promotion_is_spent_by_the_pass_that_honoured_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Run this one next" is a promise about the next search, not a new order.

    Left standing, it would put the same product first on every pass from then
    on -- which nobody asked for and nothing on screen would explain.
    """
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()

    control.set_next_search("bici")
    monitor._run_jobs()
    assert control.next_search() is None

    recorder.calls.clear()
    monitor._run_jobs()
    assert recorder.items[0] == "ps5"


def test_promoting_does_not_reorder_anything_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The queue alternates between platforms; a promotion must not undo that.

    Interleaving is what stopped the second platform from being starved, so a
    promotion that flattened it would trade one bug for the other.
    """
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()

    control.set_next_search("bici")
    monitor._run_jobs()

    platforms = [marketplace for _item, marketplace in recorder.calls]
    assert platforms[0] != platforms[1]


# --------------------------------------------------------------------------- #
# "Iniciar" respects the intervals
# --------------------------------------------------------------------------- #


def test_a_search_that_has_never_run_is_due_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no interval to wait out when nothing has happened yet.

    A brand new search sitting idle for its first half hour looks broken, and
    the interval is a gap *between* runs -- it cannot precede the first one.
    """
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()

    assert monitor._run_jobs(due_only=True) is True
    assert sorted(set(recorder.items)) == ["bici", "ps5"]


def test_a_search_that_just_ran_is_not_due_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This is what "Iniciar" not meaning "buscar todo ahora" rests on.

    ``schedule`` starts a job's clock when the job is built, so every rebuild --
    every save, every start -- handed all of them a fresh interval and made the
    configured intervals decorative.
    """
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)

    # Ran a minute ago, under a thirty-minute interval.
    for marketplace in ("facebook", "mercadolibre"):
        monitor._remember_run("ps5", marketplace, time.time() - 60)
    monitor.schedule_jobs()

    assert monitor._run_jobs(due_only=True) is True
    assert "ps5" not in recorder.items
    # The one that has never run still goes, which is the other half.
    assert "bici" in recorder.items


def test_a_search_whose_interval_has_elapsed_is_due(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)

    for marketplace in ("facebook", "mercadolibre"):
        monitor._remember_run("ps5", marketplace, time.time() - 60 * 60)
    monitor.schedule_jobs()

    assert monitor._run_jobs(due_only=True) is True
    assert "ps5" in recorder.items


def test_searching_records_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without this the memory is empty and every start is a full pass again."""
    monitor = build(tmp_path)
    recorder = Recorder()
    stub(monitor, monkeypatch, recorder)
    monitor.schedule_jobs()
    monitor._run_jobs()

    assert monitor._remembered_run("ps5", "facebook") is not None
    assert monitor._remembered_run("bici", "mercadolibre") is not None


def test_rebuilding_the_schedule_does_not_postpone_untouched_searches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Editing one search used to reset the timer of all the others.

    Every save rebuilds the schedule, and a rebuilt job starts its clock from
    scratch -- so a product two minutes from its turn quietly went back to
    thirty, once per save.
    """
    monitor = build(tmp_path)
    stub(monitor, monkeypatch, Recorder())
    for marketplace in ("facebook", "mercadolibre"):
        monitor._remember_run("ps5", marketplace, time.time() - 29 * 60)
    monitor.schedule_jobs()

    schedule.clear()
    monitor.schedule_jobs()

    due = [
        job
        for job in schedule.get_jobs()
        if getattr(job, "amm_pair", ("", ""))[0] == "ps5" and job.next_run is not None
    ]
    assert due, "the ps5 jobs should still be scheduled"
    # A minute left, not thirty: the rebuild kept the elapsed time.
    assert all((job.next_run - job.last_run).total_seconds() <= 30 * 60 for job in due)
    assert all(job.next_run.timestamp() - time.time() < 5 * 60 for job in due)


def test_a_stopped_search_is_not_recorded_as_a_failure() -> None:
    """The outcome the user reads back is the one thing this button produces.

    `control.search` classifies whatever came out of the block, and its
    catch-all is "failed" — so a control that worked exactly as designed was
    reported to the user as a fault. Caught on a live run, not by a test, which
    is why there is one now.
    """
    with pytest.raises(control.SearchStopped):
        with control.search("ps5", "mercadolibre"):
            raise control.SearchStopped(item="ps5", marketplace="mercadolibre")

    entry = next(row for row in control.searches() if row["marketplace"] == "mercadolibre")
    assert entry["last_outcome"] == "stopped"


def test_a_stopped_job_is_not_recorded_as_a_failure_either() -> None:
    """`running` is the other context manager over the same block."""
    with pytest.raises(control.SearchStopped):
        with control.running(item="ps5", marketplace="facebook"):
            raise control.SearchStopped(item="ps5", marketplace="facebook")

    assert control.state()["last"]["outcome"] == "stopped"
