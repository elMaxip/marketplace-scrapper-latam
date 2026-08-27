"""The monitor with nothing to do, and what it tells the web UI it has loaded.

A monitor with no searches configured used to log an error and cycle every
sixty seconds, complaining about a state the user may well have chosen.  It now
counts what it actually has to run, and reports that count -- along with the
version of the configuration it counted -- rather than leaving the interface to
work it out from the file.

``MarketplaceMonitor.__init__`` starts Playwright, which these tests have no use
for, so the instance is built without it: everything under test reads
``self.config`` and writes to :mod:`ai_marketplace_monitor.control`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from ai_marketplace_monitor import control
from ai_marketplace_monitor.config import Config
from ai_marketplace_monitor.monitor import MarketplaceMonitor
from ai_marketplace_monitor.utils import calculate_file_hash

TWO_SEARCHES = """
[marketplace.facebook]
username = "user@example.com"
password = "secret"
search_city = "houston"

[marketplace.mercadolibre]
market_type = "mercadolibre"

[item.ps5]
search_phrases = "playstation 5"

[item.bici]
search_phrases = "bicicleta"

[user.me]
pushbullet_token = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
"""

NO_SEARCHES = """
[marketplace.facebook]
username = "user@example.com"
password = "secret"
search_city = "houston"

[user.me]
pushbullet_token = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
"""


@pytest.fixture(autouse=True)
def clean_control() -> Iterator[None]:
    control.reset_for_tests()
    yield
    control.reset_for_tests()


def monitor_for(tmp_path: Path, content: str) -> MarketplaceMonitor:
    """A monitor holding a loaded configuration, and no browser."""
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    monitor = MarketplaceMonitor.__new__(MarketplaceMonitor)
    monitor.config_files = [path]
    monitor.config = Config([path])
    monitor.config_hash = calculate_file_hash([path])
    monitor.logger = None
    monitor.context = None
    return monitor


# --------------------------------------------------------------------------- #
# Counting what there is to run
# --------------------------------------------------------------------------- #


def test_no_searches_counts_zero(tmp_path: Path) -> None:
    assert monitor_for(tmp_path, NO_SEARCHES)._configured_searches() == 0


def test_each_item_counts_once_per_platform(tmp_path: Path) -> None:
    """Two products on two platforms is four searches, not two.

    The pair is the unit of work: the same product is asked for differently on
    each platform and runs on its own schedule slot there.
    """
    assert monitor_for(tmp_path, TWO_SEARCHES)._configured_searches() == 4


def test_a_disabled_item_is_not_something_to_run(tmp_path: Path) -> None:
    monitor = monitor_for(tmp_path, TWO_SEARCHES.replace(
        '[item.bici]\nsearch_phrases = "bicicleta"',
        '[item.bici]\nsearch_phrases = "bicicleta"\nenabled = false',
    ))
    assert monitor._configured_searches() == 2


def test_a_disabled_platform_takes_its_searches_with_it(tmp_path: Path) -> None:
    monitor = monitor_for(tmp_path, TWO_SEARCHES.replace(
        "[marketplace.mercadolibre]\nmarket_type = \"mercadolibre\"",
        "[marketplace.mercadolibre]\nmarket_type = \"mercadolibre\"\nenabled = false",
    ))
    assert monitor._configured_searches() == 2


def test_switching_every_search_off_leaves_nothing_to_run(tmp_path: Path) -> None:
    """Not the same file as "no searches", but the same amount of work."""
    monitor = monitor_for(
        tmp_path,
        TWO_SEARCHES.replace("search_phrases", "enabled = false\nsearch_phrases"),
    )
    assert monitor._configured_searches() == 0


# --------------------------------------------------------------------------- #
# Reporting what was loaded
# --------------------------------------------------------------------------- #


def test_publishing_records_the_version_and_the_searches(tmp_path: Path) -> None:
    monitor = monitor_for(tmp_path, TWO_SEARCHES)
    monitor._publish_config()

    loaded = control.loaded_config()
    assert loaded is not None
    assert loaded["version"] == monitor.config_hash
    assert loaded["items"] == ["bici", "ps5"]
    assert len(loaded["searches"]) == 4


def test_publishing_an_empty_config_is_not_a_failure(tmp_path: Path) -> None:
    monitor = monitor_for(tmp_path, NO_SEARCHES)
    monitor._publish_config()

    loaded = control.loaded_config()
    assert loaded is not None
    assert loaded["items"] == []
    assert loaded["searches"] == []
    # The platforms are built in, so stored listings can still be re-checked
    # on all of them even with nothing configured to search.  The shops are in
    # this list too: a search has to opt in to *searching* one, but a listing
    # already stored from one is re-checked like any other.
    assert control.updates()["enabled"] is True
    assert control.updates()["marketplaces"] == ["facebook", "mercadolibre", "lider", "sodimac"]


def test_a_reload_forgets_a_search_that_is_gone(tmp_path: Path) -> None:
    """Otherwise the interface keeps reporting a deleted search as one we run."""
    monitor = monitor_for(tmp_path, TWO_SEARCHES)
    monitor._publish_config()
    with control.search("bici", "facebook"):
        pass
    assert any(entry["item"] == "bici" for entry in control.searches())

    # The user deletes it, and the monitor reloads.
    monitor = monitor_for(tmp_path, TWO_SEARCHES.replace(
        '[item.bici]\nsearch_phrases = "bicicleta"\n', ""
    ))
    monitor._publish_config()

    assert all(entry["item"] != "bici" for entry in control.searches())


def test_a_search_still_running_keeps_its_place(tmp_path: Path) -> None:
    """Forgetting one mid-run would hide work that is genuinely still going."""
    monitor = monitor_for(tmp_path, TWO_SEARCHES)
    monitor._publish_config()
    with control.search("ps5", "facebook"):
        monitor._publish_config()
        running = [entry for entry in control.searches() if entry.get("running")]
        assert [entry["item"] for entry in running] == ["ps5"]
