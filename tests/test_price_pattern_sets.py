"""Saved price patterns: named once, referred to by name, resolved before use.

The point of the design is that nothing downstream knows they exist.  A search
carries ``excluded_price_pattern_sets = ["basura"]``, the loader turns that into
the flat ``excluded_price_patterns`` list every filter already read, and
``junk_price`` is untouched.  So most of what is worth pinning is about the
resolution: the order, the deduplication, and what a missing name does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_marketplace_monitor.config import Config
from ai_marketplace_monitor.price_pattern_set import PricePatternsConfig

BASE = """
[price_patterns.basura]
description = "Rellenos típicos"
patterns = ["9*", "0", "123456"]

[price_patterns.regalos]
patterns = ["gratis", "1"]

[marketplace.facebook]
username = "user@example.com"
password = "secret"

[item.ps5]
search_phrases = "playstation 5"
"""


def load(tmp_path: Path, extra: str) -> Config:
    path = tmp_path / "config.toml"
    path.write_text(BASE + extra, encoding="utf-8")
    return Config([path])


def patterns_of(config: Config, item: str = "ps5") -> list:
    return config.items[("facebook", item)].excluded_price_patterns or []


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


def test_a_named_set_becomes_the_patterns_the_filter_reads(tmp_path: Path) -> None:
    config = load(
        tmp_path,
        """
[item.ps5.facebook]
search_city = "santiago"
excluded_price_pattern_sets = ["basura"]
""",
    )
    assert patterns_of(config) == ["9*", "0", "123456"]


def test_several_sets_are_used_by_one_search(tmp_path: Path) -> None:
    config = load(
        tmp_path,
        """
[item.ps5.facebook]
search_city = "santiago"
excluded_price_pattern_sets = ["basura", "regalos"]
""",
    )
    assert patterns_of(config) == ["9*", "0", "123456", "gratis", "1"]


def test_a_search_keeps_its_own_patterns_alongside(tmp_path: Path) -> None:
    """Saved sets add to the search's own list; they do not replace it.

    The old field is untouched, which is the compatibility requirement and also
    the useful case: two searches share the general noise and one of them
    additionally excludes a number that only its own market prints.
    """
    config = load(
        tmp_path,
        """
[item.ps5.facebook]
search_city = "santiago"
excluded_price_pattern_sets = ["regalos"]
excluded_price_patterns = ["777"]
""",
    )
    assert patterns_of(config) == ["gratis", "1", "777"]


def test_the_same_pattern_from_both_sides_appears_once(tmp_path: Path) -> None:
    """The inconsistency the sets exist to remove, removed.

    A search that names a set and also copies one of its patterns is exactly the
    duplication this feature is for; leaving both in would put the same rule
    twice into every readback and every log line.
    """
    config = load(
        tmp_path,
        """
[item.ps5.facebook]
search_city = "santiago"
excluded_price_pattern_sets = ["basura"]
excluded_price_patterns = ["9*", "777"]
""",
    )
    assert patterns_of(config) == ["9*", "0", "123456", "777"]


def test_two_sets_sharing_a_pattern_contribute_it_once(tmp_path: Path) -> None:
    config = load(
        tmp_path,
        """
[price_patterns.otra]
patterns = ["0", "555"]

[item.ps5.facebook]
search_city = "santiago"
excluded_price_pattern_sets = ["basura", "otra"]
""",
    )
    assert patterns_of(config) == ["9*", "0", "123456", "555"]


def test_a_disabled_set_contributes_nothing(tmp_path: Path) -> None:
    """`enabled = false` is how a set is switched off without deleting it.

    Deleting it would mean editing every search that names it; switching it off
    leaves those references valid and stops the patterns applying.
    """
    config = load(
        tmp_path,
        """
[price_patterns.apagada]
enabled = false
patterns = ["555"]

[item.ps5.facebook]
search_city = "santiago"
excluded_price_pattern_sets = ["apagada", "regalos"]
""",
    )
    assert patterns_of(config) == ["gratis", "1"]


def test_a_search_naming_no_set_is_untouched(tmp_path: Path) -> None:
    """Saved sets existing does not change a search that ignores them."""
    config = load(
        tmp_path,
        """
[item.ps5.facebook]
search_city = "santiago"
excluded_price_patterns = ["9*"]
""",
    )
    assert patterns_of(config) == ["9*"]


def test_the_resolved_list_is_what_the_web_ui_is_shown(tmp_path: Path) -> None:
    """`describe()` publishes what is really excluded, not the reference.

    "What the scraper is actually running" is the whole job of that view, and a
    row reading ``["basura"]`` would put the user one indirection away from the
    answer they came for.
    """
    config = load(
        tmp_path,
        """
[item.ps5.facebook]
search_city = "santiago"
excluded_price_pattern_sets = ["regalos"]
""",
    )
    described = config.describe()
    search = next(
        row for row in described["searches"] if row["marketplace"] == "facebook"
    )
    assert search["options"]["excluded_price_patterns"] == ["gratis", "1"]
    assert described["price_patterns"] == ["basura", "regalos"]


# --------------------------------------------------------------------------- #
# Broken references
# --------------------------------------------------------------------------- #


def test_a_missing_set_is_refused_by_name(tmp_path: Path) -> None:
    """Named, and with the search named too.

    The alternative is worse than a hard error here: an unknown set that was
    quietly ignored would leave a search running perfectly and silently not
    excluding anything, which shows up weeks later as a group whose maximum
    price is 999999.
    """
    with pytest.raises(ValueError, match="nope"):
        load(
            tmp_path,
            """
[item.ps5.facebook]
search_city = "santiago"
excluded_price_pattern_sets = ["nope"]
""",
        )


def test_a_platform_level_reference_resolves_too(tmp_path: Path) -> None:
    """`[marketplace.*]` may name a set, the way it may name a region."""
    path = tmp_path / "config.toml"
    path.write_text(
        """
[price_patterns.basura]
patterns = ["9*"]

[marketplace.facebook]
username = "u"
password = "p"
excluded_price_pattern_sets = ["basura"]
""",
        encoding="utf-8",
    )
    config = Config([path])
    assert config.marketplace["facebook"].excluded_price_patterns == ["9*"]


# --------------------------------------------------------------------------- #
# The section itself
# --------------------------------------------------------------------------- #


def test_one_pattern_may_be_written_without_a_list(tmp_path: Path) -> None:
    assert PricePatternsConfig(name="x", patterns="9*").patterns == ["9*"]


def test_an_unparseable_pattern_is_refused_when_the_file_loads(tmp_path: Path) -> None:
    """At load time, not at match time.

    A bad entry in a *shared* list is worse than one in a search's own: it fails
    silently in every search that uses it, and a filter that quietly matches
    nothing looks exactly like a filter that is working.
    """
    with pytest.raises(ValueError, match="99"):
        PricePatternsConfig(name="mala", patterns=["99*"])


def test_an_empty_set_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="holds no pattern"):
        PricePatternsConfig(name="vacia", patterns=[])


def test_blank_entries_are_dropped(tmp_path: Path) -> None:
    assert PricePatternsConfig(name="x", patterns=["9*", "  ", ""]).patterns == ["9*"]
