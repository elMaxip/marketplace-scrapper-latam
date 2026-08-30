"""Word rules, their three scopes, and when each one can be answered.

Two things are being pinned, and only the first is about matching.

The rules themselves: ``keywords``/``antikeywords`` keep their old meaning
(title *and* description, in one haystack), and four new keys narrow the same
two rules to one half of the listing.  A search written before the scoped keys
existed must behave exactly as it did, which is why the old two are tested
alongside the new four rather than assumed.

And *when*: a shop's results grid carries titles and no descriptions, so a rule
that only reads the title can settle a listing before its page is opened, and
one that reads the description cannot.  Getting that wrong is not a matter of
tidiness -- opening a product page per catalogue entry is the bulk of a search's
traffic and is what Lider's bot check refuses -- so
:func:`keyword_filters.needs_description` and the three-state
:func:`keyword_filters.decision` are tested directly, with no browser anywhere.
"""

from __future__ import annotations

from typing import List

import pytest

from ai_marketplace_monitor import keyword_filters
from ai_marketplace_monitor.keyword_filters import Decision
from ai_marketplace_monitor.marketplace import ItemConfig

TITLE = "PlayStation 5 Slim digital sellada"
DESCRIPTION = "Consola nueva en caja, incluye control. No incluye funda."


def config(**rules: List[str] | None) -> ItemConfig:
    return ItemConfig(name="ps5", search_phrases=["ps5"], **rules)


def decide(item: ItemConfig, *, description_available: bool = True) -> Decision:
    return keyword_filters.decision(
        item,
        TITLE,
        DESCRIPTION if description_available else "",
        description_available=description_available,
    )


# --------------------------------------------------------------------------- #
# The old two keys, unchanged
# --------------------------------------------------------------------------- #


def test_the_general_rules_still_read_both_halves() -> None:
    """`keywords` matching in the description alone is still a match."""
    assert decide(config(keywords=["incluye control"])) is Decision.ACCEPT
    assert decide(config(keywords=["Slim"])) is Decision.ACCEPT
    assert decide(config(antikeywords=["funda"])) is Decision.REJECT
    assert decide(config(antikeywords=["Slim"])) is Decision.REJECT


def test_a_general_requirement_is_undecided_without_a_description() -> None:
    """The behaviour that already existed, now with a name.

    A card whose title does not carry the required word is not a card that
    fails the rule: the description has not been read yet.  This is the bug the
    ``description_available`` flag was added for in the first place, and it must
    survive the rewrite.
    """
    assert decide(config(keywords=["incluye control"]), description_available=False) is (
        Decision.UNDECIDED
    )


def test_a_general_exclusion_still_fires_on_the_title_alone() -> None:
    """A banned word in the title is settled without opening anything."""
    assert decide(config(antikeywords=["Slim"]), description_available=False) is (
        Decision.REJECT
    )


# --------------------------------------------------------------------------- #
# The scoped rules
# --------------------------------------------------------------------------- #


def test_a_title_rule_ignores_the_description() -> None:
    assert decide(config(antikeywords_title=["funda"])) is Decision.ACCEPT
    assert decide(config(keywords_title=["incluye control"])) is Decision.REJECT
    assert decide(config(keywords_title=["Slim"])) is Decision.ACCEPT
    assert decide(config(antikeywords_title=["Slim"])) is Decision.REJECT


def test_a_description_rule_ignores_the_title() -> None:
    assert decide(config(antikeywords_description=["Slim"])) is Decision.ACCEPT
    assert decide(config(keywords_description=["Slim"])) is Decision.REJECT
    assert decide(config(keywords_description=["incluye control"])) is Decision.ACCEPT
    assert decide(config(antikeywords_description=["funda"])) is Decision.REJECT


def test_several_rules_together() -> None:
    """Every rule has to pass; the first failure decides."""
    assert (
        decide(
            config(
                keywords_title=["PlayStation"],
                antikeywords_title=["cochera"],
                keywords_description=["caja"],
                antikeywords_description=["reparar"],
            )
        )
        is Decision.ACCEPT
    )
    assert (
        decide(
            config(
                keywords_title=["PlayStation"],
                antikeywords_description=["funda"],
            )
        )
        is Decision.REJECT
    )


# --------------------------------------------------------------------------- #
# When a rule can be answered -- the part the scrapers act on
# --------------------------------------------------------------------------- #


def test_a_title_rule_is_settled_from_the_card() -> None:
    """The requirement in one line: no page load for a rule about the title."""
    assert keyword_filters.needs_description(config(antikeywords_title=["funda"])) is False
    assert keyword_filters.needs_description(config(keywords_title=["Slim"])) is False

    assert decide(config(antikeywords_title=["PlayStation"]), description_available=False) is (
        Decision.REJECT
    )
    assert decide(config(keywords_title=["PlayStation"]), description_available=False) is (
        Decision.ACCEPT
    )
    assert decide(config(keywords_title=["Xbox"]), description_available=False) is (
        Decision.REJECT
    )


@pytest.mark.parametrize(
    "rules",
    [
        {"keywords": ["caja"]},
        {"antikeywords": ["caja"]},
        {"keywords_description": ["caja"]},
        {"antikeywords_description": ["caja"]},
    ],
)
def test_a_description_rule_needs_the_page(rules: dict) -> None:
    assert keyword_filters.needs_description(config(**rules)) is True


def test_no_word_rules_at_all_needs_no_page() -> None:
    assert keyword_filters.needs_description(config()) is False


def test_a_description_rule_is_undecided_rather_than_failed() -> None:
    """Never fail a rule that has not been asked, and never call it met either.

    Both mistakes are silent.  Treating it as failed empties the search of
    everything the shop sells; treating it as met lets every listing through and
    the filter looks like it is working.
    """
    assert decide(config(keywords_description=["caja"]), description_available=False) is (
        Decision.UNDECIDED
    )
    assert decide(config(antikeywords_description=["funda"]), description_available=False) is (
        Decision.UNDECIDED
    )


def test_a_title_exclusion_wins_over_an_undecided_description_rule() -> None:
    """Priority order: settled beats unsettled, and rejection is settled.

    The listing is going away either way, so the page is not worth opening --
    which is the whole saving the scoped rules were added for.
    """
    item = config(antikeywords_title=["PlayStation"], keywords_description=["caja"])
    assert decide(item, description_available=False) is Decision.REJECT


def test_the_rule_that_fired_is_named() -> None:
    """The caller logs *which* list threw the listing away.

    With three exclusion lists in one search, "excluded keywords" leaves the
    user to guess which one and where it matched.
    """
    hit = keyword_filters.excluded_by(
        config(antikeywords_description=["funda"]), TITLE, DESCRIPTION
    )
    assert hit is not None
    assert hit[0] == "antikeywords_description"
    assert hit[1] == ["funda"]

    miss = keyword_filters.missing_required(config(keywords_title=["Xbox"]), TITLE, DESCRIPTION)
    assert miss is not None
    assert miss[0] == "keywords_title"


# --------------------------------------------------------------------------- #
# Inheriting from the platform
# --------------------------------------------------------------------------- #


def test_the_item_wins_over_the_platform_default() -> None:
    """The precedence every other option in this codebase uses."""

    class Fallback:
        antikeywords_title = ["PlayStation"]

    assert (
        keyword_filters.excluded_by(
            config(antikeywords_title=["Xbox"]), TITLE, DESCRIPTION, fallback=Fallback()
        )
        is None
    )
    assert (
        keyword_filters.excluded_by(config(), TITLE, DESCRIPTION, fallback=Fallback())
        is not None
    )


# --------------------------------------------------------------------------- #
# The loader
# --------------------------------------------------------------------------- #


def test_a_single_word_is_accepted_as_a_list() -> None:
    """Same shape the two older keys accept, so the file reads the same."""
    item = ItemConfig(
        name="ps5",
        search_phrases=["ps5"],
        keywords_title="sellada",
        antikeywords_description="reparar",
    )
    assert item.keywords_title == ["sellada"]
    assert item.antikeywords_description == ["reparar"]


def test_a_number_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="keywords_title"):
        ItemConfig(name="ps5", search_phrases=["ps5"], keywords_title=[5])
