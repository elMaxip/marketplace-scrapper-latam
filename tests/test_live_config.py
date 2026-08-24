"""Comparing two configurations, and deciding who a change touches.

The question the monitor asks a hundred times an hour is not "did the file
change" -- it is "does this change mean the search I am running right now is
the wrong one to be running".  Everything here pins that answer, because
getting it wrong is expensive in both directions: too eager and every saved
notification token throws away a search in progress; too shy and a deleted
search keeps scraping for the minutes it takes to finish.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from ai_marketplace_monitor.config import Config
from ai_marketplace_monitor.live_config import (
    DISABLED,
    MARKETPLACE,
    MODIFIED,
    REMOVED,
    diff_config,
    fingerprints,
)

# Every search here runs on both built-in platforms, because that is what a
# search with nothing to say about platforms does: they are not something the
# file has to declare, so an item is searched on all of them unless it opts out.
# Hence the pairs below coming in twos.
BASE = """
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


def describe(tmp_path: Path, content: str, name: str = "config.toml") -> Dict[str, Any]:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return Config([path]).describe()


@pytest.fixture
def before(tmp_path: Path) -> Dict[str, Any]:
    return describe(tmp_path, BASE, "before.toml")


def test_identical_configurations_differ_in_nothing(tmp_path: Path, before: Dict[str, Any]) -> None:
    change = diff_config(before, describe(tmp_path, BASE, "after.toml"))
    assert not change
    assert change.affects("ps5", "facebook") is None


def test_a_comment_is_not_a_change(tmp_path: Path, before: Dict[str, Any]) -> None:
    """What matters is the configuration as resolved, not the bytes on disk.

    The file hash moves for a comment; nothing the scraper does moves with it,
    and a search under way must not be thrown out over one.
    """
    after = describe(tmp_path, BASE + "\n# me lo apunto para después\n", "after.toml")
    assert not diff_config(before, after)


def test_deleted_search_is_removed_and_affects_only_itself(
    tmp_path: Path, before: Dict[str, Any]
) -> None:
    after = describe(tmp_path, BASE.replace('[item.ps5]\nsearch_phrases = "playstation 5"\n', ""),
                     "after.toml")
    change = diff_config(before, after)
    assert change.removed == (("ps5", "facebook"), ("ps5", "mercadolibre"))
    assert change.affects("ps5", "facebook") == REMOVED
    assert change.affects("ps5", "mercadolibre") == REMOVED
    # The other search is untouched: a delete is not a reason to stop it.
    assert change.affects("bici", "facebook") is None


def test_switched_off_search_is_disabled_not_removed(
    tmp_path: Path, before: Dict[str, Any]
) -> None:
    after = describe(tmp_path, BASE.replace(
        '[item.ps5]\nsearch_phrases = "playstation 5"',
        '[item.ps5]\nenabled = false\nsearch_phrases = "playstation 5"',
    ), "after.toml")
    change = diff_config(before, after)
    assert change.removed == ()
    assert change.disabled == (("ps5", "facebook"), ("ps5", "mercadolibre"))
    assert change.affects("ps5", "facebook") == DISABLED


def test_edited_phrases_are_a_modification(tmp_path: Path, before: Dict[str, Any]) -> None:
    after = describe(tmp_path, BASE.replace("playstation 5", "playstation 5 pro"), "after.toml")
    change = diff_config(before, after)
    assert change.modified == (("ps5", "facebook"), ("ps5", "mercadolibre"))
    assert change.affects("ps5", "facebook") == MODIFIED
    assert change.affects("bici", "facebook") is None


def test_new_search_is_added_and_stops_nothing(tmp_path: Path, before: Dict[str, Any]) -> None:
    after = describe(tmp_path, BASE + '\n[item.tele]\nsearch_phrases = "televisor"\n', "after.toml")
    change = diff_config(before, after)
    assert change.added == (("tele", "facebook"), ("tele", "mercadolibre"))
    # Adding one search is no reason to abandon another that is running.
    assert change.affects("ps5", "facebook") is None
    assert change.affects("bici", "facebook") is None


def test_platform_settings_reach_every_search_on_that_platform(
    tmp_path: Path, before: Dict[str, Any]
) -> None:
    after = describe(tmp_path, BASE.replace('search_city = "houston"',
                                            'search_city = "dallas"'), "after.toml")
    change = diff_config(before, after)
    assert change.marketplaces == ("facebook",)
    assert change.affects("ps5", "facebook") == MARKETPLACE
    assert change.affects("bici", "facebook") == MARKETPLACE


def test_a_changed_secret_stops_nothing(tmp_path: Path, before: Dict[str, Any]) -> None:
    """The snapshot does not render secrets, so it cannot see this one move.

    That is the right answer to the question this module asks: whatever the
    token now is, the search under way is fetching exactly what it was asked
    to.  The caller that compared the hashes knows the file moved and supplies
    the "something changed" reading -- see the monitor's probe.
    """
    after = describe(tmp_path, BASE.replace("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                                            "yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"), "after.toml")
    change = diff_config(before, after)
    assert change.affects("ps5", "facebook") is None
    assert change.to_run({("ps5", "facebook"), ("bici", "facebook")}) == set()


def test_schedule_change_is_reported_apart_from_the_searches(
    tmp_path: Path, before: Dict[str, Any]
) -> None:
    after = describe(tmp_path, BASE + "\n[monitor]\nsearch_interval = 45\n", "after.toml")
    change = diff_config(before, after)
    assert change.schedule
    # When a search runs is not what a search does: nothing is abandoned.
    assert change.affects("ps5", "facebook") is None


def test_to_run_picks_the_touched_searches_only(tmp_path: Path, before: Dict[str, Any]) -> None:
    after = describe(
        tmp_path,
        BASE.replace("playstation 5", "playstation 5 pro") + '\n[item.tele]\nsearch_phrases = "tv"\n',
        "after.toml",
    )
    change = diff_config(before, after)
    available = {("ps5", "facebook"), ("bici", "facebook"), ("tele", "facebook")}
    assert change.to_run(available) == {("ps5", "facebook"), ("tele", "facebook")}


def test_to_run_never_names_a_search_that_no_longer_exists(
    tmp_path: Path, before: Dict[str, Any]
) -> None:
    after = describe(tmp_path, BASE.replace('[item.ps5]\nsearch_phrases = "playstation 5"\n', ""),
                     "after.toml")
    change = diff_config(before, after)
    assert change.to_run({("bici", "facebook")}) == set()


def test_first_load_reads_as_every_search_arriving(tmp_path: Path) -> None:
    change = diff_config({}, describe(tmp_path, BASE, "after.toml"))
    assert set(change.added) == {
        ("ps5", "facebook"),
        ("ps5", "mercadolibre"),
        ("bici", "facebook"),
        ("bici", "mercadolibre"),
    }


def test_fingerprint_moves_only_when_the_work_does(tmp_path: Path) -> None:
    same = fingerprints(describe(tmp_path, BASE, "a.toml"))
    unchanged = fingerprints(describe(tmp_path, BASE + "\n# nota\n", "b.toml"))
    edited = fingerprints(describe(tmp_path, BASE.replace("bicicleta", "bicicleta rodado 29"),
                                   "c.toml"))
    assert same[("ps5", "facebook")] == unchanged[("ps5", "facebook")]
    assert same[("bici", "facebook")] != edited[("bici", "facebook")]
    # The edit was to one search: the other keeps its place in the queue.
    assert same[("ps5", "facebook")] == edited[("ps5", "facebook")]


def test_fingerprint_follows_the_platform_settings_too(tmp_path: Path) -> None:
    """A city change is a different search, whatever the item section says.

    The fingerprint is what decides whether a search already run in this pass
    has to be run again, so a platform-level edit has to move it -- otherwise
    the setting the user just changed would not take until the next slot.
    """
    houston = fingerprints(describe(tmp_path, BASE, "a.toml"))
    dallas = fingerprints(
        describe(tmp_path, BASE.replace('search_city = "houston"', 'search_city = "dallas"'),
                 "b.toml")
    )
    assert houston[("ps5", "facebook")] != dallas[("ps5", "facebook")]


def test_a_search_that_ran_does_not_read_as_edited(tmp_path: Path) -> None:
    """``searched_count`` is runtime state and must stay out of the comparison.

    It lives on the item configuration like any option and is incremented by
    every search.  Compared naively, the first search of a run would make every
    search on the platform look like something the user had just edited.
    """
    path = tmp_path / "config.toml"
    path.write_text(BASE, encoding="utf-8")
    config = Config([path])
    before = config.describe()
    config.items[("facebook", "ps5")].searched_count += 3
    assert not diff_config(before, config.describe())
