"""Pressing "Iniciar" reads the configuration file.

The bug these pin was reported as intermittent and was not: a stopped monitor
holds the configuration it was stopped with, and nothing between the button and
the schedule being rebuilt used to consult the file again.  So a search created
while the monitor was stopped was invisible to it -- ``_configured_searches``
counted the searches in the *old* object, found none, and the wait that followed
watched for a change that had already happened.  ``doze`` starts its file
watcher when it is called, so the edit made a minute earlier is not an edit it
can ever see; the monitor then sat there for an hour, and stopping and starting
again sometimes appeared to help only because whatever the user did next
happened to touch the file at the right moment.

The whole fix is :meth:`MarketplaceMonitor._refresh_config` and the two places
that now call it.  These tests are written against the file rather than against
a mock so that they fail if either call site is removed.

``MarketplaceMonitor.__init__`` starts Playwright, which none of this needs, so
the instances are built with ``__new__`` -- the same shape ``test_monitor_idle``
uses.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Iterator

import pytest

from ai_marketplace_monitor import control, pause
from ai_marketplace_monitor.config import Config
from ai_marketplace_monitor.monitor import MarketplaceMonitor
from ai_marketplace_monitor.utils import calculate_file_hash

NO_SEARCHES = """
[marketplace.facebook]
username = "user@example.com"
password = "secret"
"""

ONE_SEARCH = (
    NO_SEARCHES
    + """
[item.ps5]
search_phrases = "playstation 5"

[item.ps5.facebook]
search_city = "santiago"
"""
)

TWO_SEARCHES = (
    ONE_SEARCH
    + """
[item.bici]
search_phrases = "bicicleta"

[item.bici.facebook]
search_city = "santiago"
"""
)


@pytest.fixture(autouse=True)
def clean_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A control register and a pause switch of this test's own.

    The pause switch is a file under the real ``~/.ai-marketplace-monitor``, so
    it is redirected: a test that stopped the switch and crashed would otherwise
    leave the developer's own monitor paused.
    """
    monkeypatch.setattr(pause, "STATE_FILE", tmp_path / "paused.json")
    pause.reset_for_tests()
    control.reset_for_tests()
    yield
    pause.set_paused(False)
    pause.reset_for_tests()
    control.reset_for_tests()


def monitor_for(path: Path) -> MarketplaceMonitor:
    """A monitor holding a loaded configuration, and no browser."""
    monitor = MarketplaceMonitor.__new__(MarketplaceMonitor)
    monitor.config_files = [path]
    monitor.config = Config([path])
    monitor.config_hash = calculate_file_hash([path])
    monitor._loaded_snapshot = monitor.config.describe()
    monitor.logger = None
    monitor.context = None
    monitor.lanes = {}
    monitor._lock = threading.RLock()
    monitor._thread_state = threading.local()
    monitor._probe_at = 0.0
    monitor._probe_signature = None
    monitor._reported_bad_version = None
    monitor._announced_pending = None
    monitor._schedule_dirty = False
    monitor._review_due = 0.0
    monitor._review_thread = None
    monitor._review_stop = threading.Event()
    return monitor


def products(monitor: MarketplaceMonitor) -> int:
    """Distinct products configured, rather than (product, platform) pairs.

    ``_configured_searches`` counts the pairs -- one search that runs on
    Facebook and on Mercado Libre is two of them -- which is the right number
    for the loop and the wrong one to write a test against: it changes when a
    platform's default does, and this is a test about reading the file.
    """
    assert monitor.config is not None
    return len({item for _marketplace, item in monitor.config.items})


def write(path: Path, content: str) -> None:
    """Save the file the way the web UI does, and make the change detectable.

    ``_probe_config`` short-circuits on ``(mtime, size)``, so a rewrite of the
    same length inside one filesystem tick would look like no change at all --
    a property of the test's speed, not of the monitor.  Every content here
    differs in length, and the timestamp is pushed forward to be sure.
    """
    path.write_text(content, encoding="utf-8")
    stat = path.stat()
    import os

    os.utime(path, (stat.st_atime + 10, stat.st_mtime + 10))


# --------------------------------------------------------------------------- #
# The reported scenario
# --------------------------------------------------------------------------- #


def test_a_search_added_while_stopped_is_there_when_started(tmp_path: Path) -> None:
    """Stop with nothing configured, add a search, start: it is loaded.

    The exact sequence from the report, and the one that used to leave the
    monitor waiting an hour on a configuration that no longer existed.
    """
    path = tmp_path / "config.toml"
    path.write_text(NO_SEARCHES, encoding="utf-8")
    monitor = monitor_for(path)
    assert products(monitor) == 0

    # "Detener", then the search is created while the monitor is stopped.
    pause.set_paused(True, force=True)
    write(path, ONE_SEARCH)
    # Still invisible: nothing has read the file.
    assert products(monitor) == 0

    # "Iniciar".  The switch goes back before the monitor notices, exactly as
    # it does when the web UI clears it.
    pause.set_paused(False)
    monitor._refresh_config()

    assert products(monitor) == 1
    assert ("facebook", "ps5") in monitor.config.items


def test_the_wait_for_searches_reads_the_file_before_deciding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_wait_for_searches`` must not decide from the loaded snapshot.

    The wait it used to enter is the hour-long one, and this is what made it
    unreachable: the file already said there was a search, and the monitor's
    copy of it did not.  ``doze`` is replaced with a failure, because reaching
    it at all is the bug.
    """
    path = tmp_path / "config.toml"
    path.write_text(NO_SEARCHES, encoding="utf-8")
    monitor = monitor_for(path)
    monitor.keyboard_monitor = None

    write(path, ONE_SEARCH)

    import ai_marketplace_monitor.monitor as monitor_module

    def never(*args: object, **kwargs: object) -> object:
        raise AssertionError(
            "waited for a file change that had already happened; the configuration "
            "was not re-read before the decision"
        )

    monkeypatch.setattr(monitor_module, "doze", never)
    monitor._wait_for_searches()

    assert products(monitor) == 1


def test_repeated_stop_and_start_keeps_up(tmp_path: Path) -> None:
    """Several rounds of stop, edit, start -- each one on one press.

    "Press Iniciar twice" was one of the workarounds; this is what it looked
    like from the file's side.
    """
    path = tmp_path / "config.toml"
    path.write_text(NO_SEARCHES, encoding="utf-8")
    monitor = monitor_for(path)

    for content, expected in (
        (ONE_SEARCH, 1),
        (TWO_SEARCHES, 2),
        (ONE_SEARCH, 1),
        (NO_SEARCHES, 0),
        (TWO_SEARCHES, 2),
    ):
        pause.set_paused(True, force=True)
        write(path, content)
        pause.set_paused(False)
        monitor._refresh_config()
        assert products(monitor) == expected, content


def test_an_unreadable_file_leaves_the_loaded_one_alone(tmp_path: Path) -> None:
    """A half-written file is not a reason to start with nothing.

    ``_refresh_config`` returns False and keeps what it had, which is the same
    answer ``_probe_config`` gives a checkpoint mid-search: the file may simply
    be part-way through being saved.
    """
    path = tmp_path / "config.toml"
    path.write_text(ONE_SEARCH, encoding="utf-8")
    monitor = monitor_for(path)
    assert products(monitor) == 1

    write(path, "[item.broken\nthis is not toml at all")
    assert monitor._refresh_config() is False
    assert products(monitor) == 1


def test_a_change_that_is_adopted_is_announced(tmp_path: Path) -> None:
    """The interface learns the change landed, not just that it was saved.

    Saving and the scraper using it are two events (`config_sync.applied`), and
    a start that reloads silently would leave the second one unreported -- the
    interface would show "pendiente" over a monitor that had already taken the
    change up.
    """
    path = tmp_path / "config.toml"
    path.write_text(NO_SEARCHES, encoding="utf-8")
    monitor = monitor_for(path)

    write(path, ONE_SEARCH)
    assert monitor._refresh_config() is True

    applied = control.config_applied()
    assert applied is not None
    assert applied["version"] == monitor.config_hash
    assert {"item": "ps5", "marketplace": "facebook"} in applied["change"]["added"]


def test_nothing_is_announced_when_nothing_changed(tmp_path: Path) -> None:
    """Starting a monitor whose file has not moved is not a configuration event."""
    path = tmp_path / "config.toml"
    path.write_text(ONE_SEARCH, encoding="utf-8")
    monitor = monitor_for(path)

    assert monitor._refresh_config() is False
    assert control.config_applied() is None


# --------------------------------------------------------------------------- #
# The other thing a stop leaves behind
# --------------------------------------------------------------------------- #


def test_a_search_switched_off_while_stopped_loses_its_slot(tmp_path: Path) -> None:
    """The schedule is rebuilt on start, not added to.

    ``schedule_jobs`` registers jobs with the ``schedule`` package, which keeps
    them for the life of the process.  So the entry a search had before the stop
    outlived the configuration that created it: the loop found a non-empty
    registry, took the "idle" branch, and the interface reported "próxima
    búsqueda: consola" for a search that had been switched off.

    Reloading alone does not fix this -- the configuration was right and the
    registry was stale -- which is why it is a second fix rather than a second
    symptom of the first.
    """
    import schedule

    path = tmp_path / "config.toml"
    path.write_text(ONE_SEARCH, encoding="utf-8")
    monitor = monitor_for(path)
    monitor.keyboard_monitor = None
    monitor.context = None
    monitor.active_marketplaces = {}
    monitor.ai_agents = []

    schedule.clear()
    try:
        monitor._rebuild_schedule()
        assert schedule.get_jobs(), "a configured search should have a slot"

        pause.set_paused(True, force=True)
        # The whole search, not one of its platforms: it runs on every
        # marketplace that has not opted out, so switching off the Facebook
        # block alone would leave the Mercado Libre job standing and the test
        # would be measuring the wrong thing.
        off = ONE_SEARCH.replace("[item.ps5]", "[item.ps5]\nenabled = false")
        write(path, off)
        pause.set_paused(False)
        monitor._refresh_config()
        monitor._rebuild_schedule()

        assert not schedule.get_jobs(), (
            "the slot from before the stop outlived the search that owned it"
        )
    finally:
        schedule.clear()

