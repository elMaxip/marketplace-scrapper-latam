"""A search's own schedule, and what it means to not have one.

The global ``[monitor]`` schedule stays the answer for every search that does
not name one of the three keys itself -- nothing is copied down, so turning a
search's schedule off is deleting three lines and not restoring three values.

What is tested here is only :meth:`MarketplaceMonitor._schedule_for`, because
that is the whole decision: everything downstream of it -- the jobs, the tags,
the reload -- already worked one item at a time.
"""

from __future__ import annotations

from datetime import time
from types import SimpleNamespace
from typing import Iterator, List

import pytest
import schedule

from ai_marketplace_monitor.monitor import MarketplaceMonitor


@pytest.fixture(autouse=True)
def empty_scheduler() -> Iterator[None]:
    """``schedule.every()`` registers into a module-level scheduler."""
    schedule.clear()
    yield
    schedule.clear()


def monitor_with(**monitor_keys: object) -> MarketplaceMonitor:
    """A monitor that has a configuration and nothing else."""
    self = MarketplaceMonitor.__new__(MarketplaceMonitor)
    self.config = SimpleNamespace(  # type: ignore[assignment]
        monitor=SimpleNamespace(
            search_interval=monitor_keys.get("search_interval"),
            max_search_interval=monitor_keys.get("max_search_interval"),
            start_at=monitor_keys.get("start_at"),
        )
    )
    self.logger = None  # type: ignore[assignment]
    return self


def section(**keys: object) -> SimpleNamespace:
    return SimpleNamespace(
        name=str(keys.get("name", "ps5")),
        search_interval=keys.get("search_interval"),
        max_search_interval=keys.get("max_search_interval"),
        start_at=keys.get("start_at"),
    )


def jobs_of(
    monitor: MarketplaceMonitor, item: SimpleNamespace, marketplace: SimpleNamespace | None = None
) -> List[schedule.Job]:
    return monitor._schedule_for(item, marketplace or section(name="facebook"))  # type: ignore[arg-type]


def intervals(jobs: List[schedule.Job]) -> List[tuple]:
    """The (low, high) seconds of each interval job.  A fixed interval is the
    range whose two ends are equal, which is how ``schedule`` stores it."""
    return [(job.interval, job.latest, job.unit) for job in jobs if job.at_time is None]


def times(jobs: List[schedule.Job]) -> List[tuple]:
    return [(job.at_time, job.unit) for job in jobs if job.at_time is not None]


def test_a_search_without_a_schedule_uses_the_global_one() -> None:
    monitor = monitor_with(search_interval=3600)
    jobs = jobs_of(monitor, section())
    assert intervals(jobs) == [(3600, 3600, "seconds")]


def test_its_own_fixed_interval_wins_over_the_global_one() -> None:
    monitor = monitor_with(search_interval=3600)
    jobs = jobs_of(monitor, section(search_interval=1800))
    assert intervals(jobs) == [(1800, 1800, "seconds")]


def test_its_own_random_range_wins_over_the_global_one() -> None:
    monitor = monitor_with(search_interval=3600)
    jobs = jobs_of(monitor, section(search_interval=1200, max_search_interval=3600))
    assert intervals(jobs) == [(1200, 3600, "seconds")]


def test_its_own_fixed_times_win_over_the_global_interval() -> None:
    """C in the brief: times of its own, and the global interval ignored."""
    monitor = monitor_with(search_interval=3600)
    jobs = jobs_of(monitor, section(start_at=["09:30", "18:00"]))
    assert intervals(jobs) == []
    assert times(jobs) == [(time(9, 30), "days"), (time(18, 0), "days")]


def test_its_own_times_and_its_own_interval_are_both_scheduled() -> None:
    """Additive, exactly like the global pair: the third checkbox is not an
    alternative to the first two."""
    monitor = monitor_with(search_interval=3600)
    jobs = jobs_of(monitor, section(search_interval=900, start_at=["09:30"]))
    assert intervals(jobs) == [(900, 900, "seconds")]
    assert times(jobs) == [(time(9, 30), "days")]


def test_turning_every_option_off_goes_back_to_the_global_one() -> None:
    """The three keys gone is the only thing that says "no own schedule", and
    it has to be enough: the interface erases them rather than writing the
    global values into the item."""
    monitor = monitor_with(search_interval=1800, max_search_interval=3600)
    jobs = jobs_of(monitor, section(search_interval=None, max_search_interval=None, start_at=None))
    assert intervals(jobs) == [(1800, 3600, "seconds")]


def test_one_search_does_not_move_another() -> None:
    monitor = monitor_with(search_interval=3600)
    mine = jobs_of(monitor, section(name="ps5", search_interval=300))
    yours = jobs_of(monitor, section(name="bici"))
    assert intervals(mine) == [(300, 300, "seconds")]
    assert intervals(yours) == [(3600, 3600, "seconds")]


def test_without_any_schedule_anywhere_the_defaults_apply() -> None:
    monitor = monitor_with()
    jobs = jobs_of(monitor, section())
    assert intervals(jobs) == [
        (
            MarketplaceMonitor.DEFAULT_SEARCH_INTERVAL,
            MarketplaceMonitor.DEFAULT_MAX_SEARCH_INTERVAL,
            "seconds",
        )
    ]


def test_an_old_file_still_reads_fixed_times_as_replacing_the_interval() -> None:
    """No ``[monitor]`` schedule identifies a file written before the key
    existed, and back then ``start_at`` replaced the interval.  Reading it
    additively would start searching every 15 minutes under a config that has
    asked for 09:00 and nothing else for a year."""
    monitor = monitor_with()
    jobs = jobs_of(monitor, section(search_interval=900, start_at=["09:00"]))
    assert intervals(jobs) == []
    assert times(jobs) == [(time(9, 0), "days")]


def test_the_platform_section_is_still_the_last_fallback() -> None:
    """Only when nothing else says anything: an item that has a schedule of its
    own no longer inherits its platform's."""
    monitor = monitor_with()
    jobs = jobs_of(monitor, section(), section(name="facebook", search_interval=120))
    assert intervals(jobs) == [(120, 120, "seconds")]
