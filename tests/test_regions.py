"""Una región guardada, y la búsqueda que la usa.

A region is a named set of Facebook cities, saved once and picked by name
afterwards.  ``Config.expand_regions`` replaces a search's ``search_region``
with those cities before the search runs, and everything downstream reads
``search_city`` -- so a region that expands into nothing produces a search that
is rejected with "No search_city or search_region is specified" while plainly
specifying a region.

That is exactly what used to happen.  The expansion zipped four parallel
columns together, and ``currency`` is optional: a region saved with cities, a
label and a radius but no currency zipped against an empty list and produced no
cities at all.  Every search using it then failed to load, and the message
pointed at the one thing the user *had* filled in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_marketplace_monitor.config import DEFAULT_REGION_RADIUS, Config

USING_A_REGION = """
[region.vina]
full_name = "Viña del Mar"
search_city = ["106647439372422"]
city_name = ["viña del mar"]
radius = [40]

[item.ps5]
search_phrases = "playstation 5"

[item.ps5.facebook]
search_region = ["vina"]
"""


def load(tmp_path: Path, content: str) -> Config:
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    return Config([path])


def test_a_region_without_a_currency_still_expands(tmp_path: Path) -> None:
    config = load(tmp_path, USING_A_REGION)
    item = config.items[("facebook", "ps5")]
    assert item.search_city == ["106647439372422"]
    assert item.city_name == ["viña del mar"]
    assert item.radius == [40]
    # Nothing was invented in the column that was left empty.
    assert item.currency == []


def test_a_region_of_bare_cities_expands(tmp_path: Path) -> None:
    """The minimum a region can be: a name and a list of cities."""
    config = load(
        tmp_path,
        """
[region.centro]
search_city = ["106647439372422", "112233445566778"]

[item.ps5]
search_phrases = "playstation 5"

[item.ps5.facebook]
search_region = ["centro"]
""",
    )
    item = config.items[("facebook", "ps5")]
    assert item.search_city == ["106647439372422", "112233445566778"]
    # Filled in rather than left short: the four columns are read in parallel.
    assert item.radius == [DEFAULT_REGION_RADIUS, DEFAULT_REGION_RADIUS]
    assert len(item.city_name) == 2


def test_a_region_with_a_currency_keeps_it(tmp_path: Path) -> None:
    config = load(
        tmp_path,
        """
[region.usa]
search_city = ["houston", "dallas"]
city_name = ["Houston", "Dallas"]
radius = [100, 100]
currency = ["USD", "USD"]

[item.ps5]
search_phrases = "playstation 5"

[item.ps5.facebook]
search_region = ["usa"]
""",
    )
    item = config.items[("facebook", "ps5")]
    assert item.search_city == ["houston", "dallas"]
    assert item.currency == ["USD", "USD"]


def test_two_regions_are_added_together_without_repeats(tmp_path: Path) -> None:
    config = load(
        tmp_path,
        """
[region.a]
search_city = ["uno", "dos"]

[region.b]
search_city = ["dos", "tres"]

[item.ps5]
search_phrases = "playstation 5"

[item.ps5.facebook]
search_region = ["a", "b"]
""",
    )
    item = config.items[("facebook", "ps5")]
    assert item.search_city == ["uno", "dos", "tres"]
    assert len(item.radius) == 3
    assert len(item.city_name) == 3


def test_a_region_created_alongside_the_search_works_immediately(tmp_path: Path) -> None:
    """Both halves saved in one write, which is what the web UI does when the
    user defines a region while creating the search that uses it."""
    config = load(
        tmp_path,
        """
[region.brandnew]
search_city = ["106647439372422"]

[item.tele]
search_phrases = "televisor"

[item.tele.facebook]
search_region = ["brandnew"]
""",
    )
    assert config.items[("facebook", "tele")].search_city == ["106647439372422"]


def test_naming_a_region_that_does_not_exist_says_so(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="fantasma"):
        load(
            tmp_path,
            """
[item.ps5]
search_phrases = "playstation 5"

[item.ps5.facebook]
search_region = ["fantasma"]
""",
        )


def test_a_region_with_no_cities_is_named_in_the_complaint(tmp_path: Path) -> None:
    """The old message blamed the search for specifying no region, which was
    the one thing it had done."""
    with pytest.raises(ValueError, match="vacia"):
        load(
            tmp_path,
            """
[region.vacia]
full_name = "sin ciudades"

[item.ps5]
search_phrases = "playstation 5"

[item.ps5.facebook]
search_region = ["vacia"]
""",
        )
