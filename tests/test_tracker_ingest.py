"""Un seguimiento es una publicación guardada, no una búsqueda.

A tracker used to be scheduled exactly like a search: a repeating job on the
``tracked`` platform.  It could not work, and the way it failed was silent.  A
search only ever looks for listings nobody has recorded yet
(:func:`~ai_marketplace_monitor.observations.is_known`), and a tracker's one
listing is recorded the first time it is read -- so every run after that opened
a browser, found the page already known, closed it, and reported "0 new
listings".  Meanwhile the price on that page moved and only the review would
ever notice.

So the schedule no longer holds trackers at all.  A tracker is read **once**,
when it is added, on a browser of its own that is closed straight afterwards --
and from then on it is an ordinary stored listing that the review re-reads,
which is what it was always meant to be.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any, Iterator, List, Tuple

import pytest
import schedule
from diskcache import Cache  # type: ignore

from ai_marketplace_monitor import control
from ai_marketplace_monitor import monitor as monitor_module
from ai_marketplace_monitor import observations as obs
from ai_marketplace_monitor.listing import Listing
from ai_marketplace_monitor.monitor import MarketplaceMonitor
from ai_marketplace_monitor.tracking import PLATFORM as TRACKED, tracked_id

CONFIG = """
[marketplace.facebook]
search_city = "santiago"

[user.ana]
pushbullet_token = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

[item.ps5]
search_phrases = "playstation 5"

[track.sabanas]
url = "https://t.cl/p/sabanas"
notify = "ana"
"""

TRACKERS_ONLY = """
[user.ana]
pushbullet_token = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

[track.sabanas]
url = "https://t.cl/p/sabanas"
"""


@pytest.fixture(autouse=True)
def clean(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Cache]:
    """A store of this test's own, so nothing here touches the real one."""
    store = Cache(str(tmp_path / "store"))
    monkeypatch.setattr(obs, "cache", store)
    # The pause switch is persisted in the user's own home directory, and
    # whether *they* left the monitor paused is not a fact about this test.
    monkeypatch.setattr(monitor_module, "is_paused", lambda: False)
    obs.reset_index_cache()
    control.reset_for_tests()
    schedule.clear()
    yield store
    schedule.clear()
    control.reset_for_tests()
    obs.reset_index_cache()
    store.close()


class FakeLane:
    """A lane without a browser: it only has to hold marketplace objects.

    ``renew_context`` is here because every real lane has one and the
    marketplaces built on a lane are handed it -- a shop that is refused asks
    for a fresh browser through exactly this.  A tracker read never gets that
    far, but leaving it off made building the marketplace raise, and the read
    was reported as a tracker that could not be read.
    """

    def __init__(self) -> None:
        self.marketplaces: dict = {}

    def renew_context(self) -> None:
        raise AssertionError("a tracker read should never need a new browser")


def build(
    tmp_path: pathlib.Path, config_text: str = CONFIG, name: str = "config.toml"
) -> MarketplaceMonitor:
    path = tmp_path / name
    path.write_text(config_text, encoding="utf-8")
    monitor = MarketplaceMonitor.__new__(MarketplaceMonitor)
    monitor.config_files = [path]
    monitor.config = None
    monitor.config_hash = None
    monitor._loaded_snapshot = {}
    monitor._fingerprints = {}
    monitor._probe_at = 0.0
    monitor._probe_signature = None
    monitor._reported_bad_version = None
    monitor._announced_pending = None
    monitor._schedule_dirty = False
    monitor.logger = logging.getLogger("test-tracker-ingest")
    monitor.keyboard_monitor = None
    monitor.context = None
    monitor.lanes = {}
    monitor.refresher = None
    monitor.active_marketplaces = {}
    monitor.ai_agents = []
    monitor.load_config_file()
    return monitor


def already_read(store: Cache, url: str = "https://t.cl/p/sabanas") -> None:
    """Put the tracked page in the store, as its first read would."""
    obs.record_observation(
        Listing(
            marketplace=TRACKED,
            name="sabanas",
            id=tracked_id(url),
            title="Juego de sabanas",
            image="",
            price="$27 490",
            post_url=url,
            location="",
            seller="",
            condition="",
            description="",
        ),
        matched=True,
        item_name="sabanas",
        local_cache=store,
    )


# --------------------------------------------------------------------------- #
# It is not scheduled
# --------------------------------------------------------------------------- #


def test_a_tracker_gets_no_repeating_job(tmp_path: pathlib.Path) -> None:
    monitor = build(tmp_path)
    monitor.schedule_jobs()

    pairs = {job.amm_pair for job in schedule.get_jobs()}
    assert ("ps5", "facebook") in pairs
    assert not [pair for pair in pairs if pair[1] == TRACKED]


def test_the_tracked_platform_is_still_built_for_the_review(
    tmp_path: pathlib.Path,
) -> None:
    """No job, but the review still has to have something to drive."""
    monitor = build(tmp_path)
    monitor.schedule_jobs()

    assert TRACKED in monitor.active_marketplaces


def test_a_monitor_with_only_trackers_schedules_nothing(tmp_path: pathlib.Path) -> None:
    # And that is not "nothing to do": the loop reviews and ingests in exactly
    # this state, which is the normal one for somebody who only follows pages.
    monitor = build(tmp_path, TRACKERS_ONLY)
    monitor.schedule_jobs()

    assert schedule.get_jobs() == []
    assert monitor._configured_searches() == 1


# --------------------------------------------------------------------------- #
# It is read once, when it is added
# --------------------------------------------------------------------------- #


def test_a_new_tracker_is_waiting_to_be_read(tmp_path: pathlib.Path) -> None:
    monitor = build(tmp_path)
    pending = monitor._trackers_to_ingest()

    assert [item_config.name for _marketplace, item_config in pending] == ["sabanas"]


def test_a_tracker_already_in_the_store_is_not_read_again(
    tmp_path: pathlib.Path, clean: Cache
) -> None:
    """The whole point: after the first read it belongs to the review."""
    monitor = build(tmp_path)
    already_read(clean)

    assert monitor._trackers_to_ingest() == []


def test_a_switched_off_tracker_is_left_alone(tmp_path: pathlib.Path) -> None:
    monitor = build(
        tmp_path, CONFIG.replace('notify = "ana"', 'notify = "ana"\nenabled = false')
    )
    assert monitor._trackers_to_ingest() == []


def test_reading_them_uses_the_ordinary_search_path(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``search_item`` and nothing new: the first read notifies, rates and
    records exactly as a listing found by a search does -- which is what makes
    a tracked page an ordinary stored listing from then on."""
    monitor = build(tmp_path)
    read: List[Tuple[str, str]] = []
    monkeypatch.setattr(
        monitor,
        "search_item",
        lambda marketplace_config, marketplace, item_config: read.append(
            (item_config.name, marketplace_config.name)
        ),
    )

    assert monitor._read_trackers(FakeLane(), None, monitor._trackers_to_ingest()) is True
    assert read == [("sabanas", TRACKED)]


def test_one_read_while_the_browser_was_opening_is_not_repeated(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, clean: Cache
) -> None:
    monitor = build(tmp_path)
    pending = monitor._trackers_to_ingest()
    already_read(clean)

    read: List[str] = []
    monkeypatch.setattr(
        monitor,
        "search_item",
        lambda marketplace_config, marketplace, item_config: read.append(item_config.name),
    )
    monitor._read_trackers(FakeLane(), None, pending)

    assert read == []


def test_nothing_is_read_while_the_monitor_is_paused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monitor = build(tmp_path)
    started: List[Any] = []
    monkeypatch.setattr(monitor, "_ingest_pass", lambda pending: started.append(pending))
    monkeypatch.setattr(monitor_module, "is_paused", lambda: True)

    monitor._ingest_trackers()

    assert started == []
    assert monitor._ingesting is False


def test_a_browser_is_asked_for_only_when_there_is_something_to_read(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, clean: Cache
) -> None:
    """Opening a window to discover there is nothing to do is the cost the
    pre-check exists to avoid -- and was the visible symptom of the old
    behaviour: an empty browser opening and closing on a timer."""
    monitor = build(tmp_path)
    started: List[Any] = []
    monkeypatch.setattr(monitor, "_ingest_pass", lambda pending: started.append(pending))
    already_read(clean)

    monitor._ingest_trackers()

    assert started == []
