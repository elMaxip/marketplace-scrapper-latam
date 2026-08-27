"""Zero searches is a state the whole system has to support.

A fresh install has none, and the user is allowed to delete the last one.  Both
used to be impossible: the loader insisted on an ``[item]`` section, so the web
UI's delete of the last search came back rejected, and the first-run template
shipped a made-up GoPro search to satisfy that rule.

These tests pin the three halves of the fix -- the loader accepting a file with
no searches, the write path accepting the delete that produces one, and the
template no longer inventing a search nobody asked for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_marketplace_monitor.config import Config
from ai_marketplace_monitor.webui.config_api import ConfigFileService

NO_ITEMS = """
[marketplace.facebook]
username = "user@example.com"
password = "secret"
search_city = "houston"

[user.me]
pushbullet_token = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
"""

ONE_ITEM = (
    NO_ITEMS
    + """
[item.ps5]
search_phrases = "playstation 5"
"""
)


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(ONE_ITEM, encoding="utf-8")
    return path


def test_config_loads_with_no_searches(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(NO_ITEMS, encoding="utf-8")
    config = Config([path])
    assert config.items == {}
    assert config.item == {}
    # The platform is still there: nothing about having no searches makes the
    # rest of the configuration invalid.
    assert "facebook" in config.marketplace


def test_deleting_the_last_search_is_accepted(config_file: Path) -> None:
    """The delete the web UI performs, end to end.

    Removing the last ``[item.*]`` block leaves a file with no searches, which
    is exactly what the loader used to refuse -- making the last search
    undeletable from the interface with no explanation the user could act on.
    """
    service = ConfigFileService([config_file])
    content, mtime = service.read("primary")
    without = content.split("[item.ps5]")[0]

    _mtime, ok, error = service.write("primary", without, base_mtime=mtime)

    assert ok, error
    on_disk = config_file.read_text(encoding="utf-8")
    assert "[item.ps5]" not in on_disk
    # The secret was round-tripped rather than written back as its mask.
    assert "user@example.com" in on_disk


def test_a_search_can_be_added_again_afterwards(config_file: Path) -> None:
    service = ConfigFileService([config_file])
    content, mtime = service.read("primary")
    _mtime, ok, _error = service.write(
        "primary", content.split("[item.ps5]")[0], base_mtime=mtime
    )
    assert ok

    content, mtime = service.read("primary")
    _mtime, ok, error = service.write(
        "primary",
        content + '\n[item.bici]\nsearch_phrases = "bicicleta"\n',
        base_mtime=mtime,
    )

    assert ok, error
    assert "[item.bici]" in config_file.read_text(encoding="utf-8")


def test_no_marketplace_section_is_needed(tmp_path: Path) -> None:
    """The platforms are built in, so nothing has to declare them.

    This used to be refused outright, which meant a configuration could be
    complete in every way the user could see and still be rejected for a
    section whose only content was the name of a platform the monitor already
    knows how to search.
    """
    path = tmp_path / "config.toml"
    path.write_text('[user.me]\npushbullet_token = "x"\n', encoding="utf-8")
    config = Config([path])
    assert sorted(config.marketplace) == ["facebook", "lider", "mercadolibre", "sodimac"]


def test_a_completely_empty_file_loads(tmp_path: Path) -> None:
    """What a fresh install writes: comments and nothing else."""
    path = tmp_path / "config.toml"
    path.write_text("# nothing yet\n", encoding="utf-8")
    config = Config([path])
    assert config.items == {}
    assert config.user == {}
    assert sorted(config.marketplace) == ["facebook", "lider", "mercadolibre", "sodimac"]


def test_an_unknown_section_is_still_refused(tmp_path: Path) -> None:
    """Only the required-section rule was dropped, not the rest of the checks."""
    path = tmp_path / "config.toml"
    path.write_text("[nonsense.thing]\nx = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="nonsense"):
        Config([path])


def test_the_first_run_template_configures_no_searches() -> None:
    """A fresh install starts empty rather than with somebody else's product."""
    from ai_marketplace_monitor.cli import _DEFAULT_CONFIG_TEMPLATE

    assert "[item." not in _DEFAULT_CONFIG_TEMPLATE
    assert "gopro" not in _DEFAULT_CONFIG_TEMPLATE.lower()


def test_the_first_run_template_creates_nothing_at_all(tmp_path: Path) -> None:
    """No search, no user, and no platform settings either.

    The template used to carry a ``[marketplace.facebook]`` with a Houston city
    in it and a ``[user.me]`` nobody asked for.  The city made the web UI
    announce "there are per-platform search settings from the previous version"
    on a brand new install -- a migration notice about a file the installer had
    written thirty seconds earlier -- and the user was a recipient with no
    channel that somebody then had to work out how to delete.
    """
    import logging

    from ai_marketplace_monitor.cli import _DEFAULT_CONFIG_TEMPLATE, _seed_default_config

    assert "[user." not in _DEFAULT_CONFIG_TEMPLATE
    assert "search_city" not in _DEFAULT_CONFIG_TEMPLATE

    path = tmp_path / "config.toml"
    _seed_default_config(path, logging.getLogger("test-seed"))
    config = Config([path])
    assert config.items == {}
    assert config.user == {}
    # And nothing in it reads as leftovers from an older version.
    assert all(
        marketplace.search_city is None for marketplace in config.marketplace.values()
    )


def test_the_facebook_language_is_a_search_setting_now(tmp_path: Path) -> None:
    """Two searches, two languages, from the same monitor."""
    path = tmp_path / "config.toml"
    path.write_text(
        """
[item.ps5]
search_phrases = "playstation 5"

[item.ps5.facebook]
search_city = "santiago"
language = "es_LA"

[item.tv]
search_phrases = "televisor"

[item.tv.facebook]
search_city = "houston"
language = "en_US"
""",
        encoding="utf-8",
    )
    config = Config([path])
    assert config.items[("facebook", "ps5")].language == "es_LA"
    assert config.items[("facebook", "tv")].language == "en_US"
