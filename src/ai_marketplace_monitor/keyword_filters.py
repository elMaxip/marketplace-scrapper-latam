"""Which words a listing must have, which it must not, and *where*.

There were two rules and they both read the same haystack -- the title and the
description glued together.  That is the right default and a poor only option:
"no busco fundas" is a rule about the title, because every listing of a console
mentions a case somewhere in its description, and "tiene que decir sellado" is a
rule about the description, because a title has room for four words.  A user who
wanted either of those had to accept the other's false matches.

So there are now three scopes for each rule, and the third is the interesting
one:

======================  =========================================
``antikeywords``        anywhere: the title or the description
``antikeywords_title``  the title only
``antikeywords_description``  the description only
``keywords``            anywhere
``keywords_title``      the title only
``keywords_description``  the description only
======================  =========================================

The old two keys keep their exact meaning, so a search written before this
behaves identically -- which is the whole compatibility requirement, and why
they were not redefined as "title plus description of the new kind".

**When a rule can be answered is as much of the design as what it says.**  A
shop's results grid carries titles and no descriptions, and opening a product
page per catalogue entry is the bulk of a search's traffic and, on Lider, the
exact requests its bot check refuses.  So the filters run twice: once against
the card, where only the title is known, and again against the page if one was
opened.  :func:`decision` is the whole of that logic and returns three states
rather than a boolean, because "not excluded" and "not excluded *yet*" are
different facts:

``REJECT``    a rule that could be answered says no.  Nothing will change that:
              a title does not gain words when the page loads.
``ACCEPT``    every rule was answered, and all of them said yes.
``UNDECIDED`` a rule needs the description and there is none yet.

The priority order that produces is deliberate and is the requirement in three
lines: never open a page a title rule has already settled; never throw a listing
away on a rule that has not been answered; and never count a rule as satisfied
before there is enough to satisfy it.

:func:`needs_description` is the other half -- asked *before* opening anything,
it says whether the page could change any outcome.  A search whose only word
rule is ``antikeywords_title`` reads a whole catalogue from the grid.

Nothing here touches a listing object, a page or the config loader: it takes
strings and lists of strings.  That is what lets every combination be tested
against the titles and descriptions that actually turn up rather than through a
browser.
"""

from __future__ import annotations

from enum import Enum
from logging import Logger
from typing import Any, List, Sequence, Tuple

from .utils import is_substring

#: The six keys, as ``(config attribute, scope)``.
#:
#: One list rather than six branches, so a scope cannot be handled in the
#: exclusion pass and forgotten in the requirement pass -- which is precisely
#: the shape of bug that makes a filter quietly stop filtering.
EXCLUDE_KEYS: Tuple[Tuple[str, str], ...] = (
    ("antikeywords", "any"),
    ("antikeywords_title", "title"),
    ("antikeywords_description", "description"),
)

REQUIRE_KEYS: Tuple[Tuple[str, str], ...] = (
    ("keywords", "any"),
    ("keywords_title", "title"),
    ("keywords_description", "description"),
)

#: Every key this module reads, for callers that need the whole set.
ALL_KEYS: Tuple[str, ...] = tuple(
    key for key, _scope in (*EXCLUDE_KEYS, *REQUIRE_KEYS)
)

#: The ones whose answer can depend on the description.
#:
#: ``keywords``/``antikeywords`` are in here because they read both halves: an
#: exclusion may still fire on the title alone, but a *requirement* spanning
#: both cannot be settled until the description exists.
DESCRIPTION_KEYS: Tuple[str, ...] = (
    "antikeywords",
    "antikeywords_description",
    "keywords",
    "keywords_description",
)


class Decision(Enum):
    """What the word rules have to say, given what is known so far."""

    ACCEPT = "accept"
    REJECT = "reject"
    UNDECIDED = "undecided"


def _rule(config: Any, fallback: Any, key: str) -> List[str] | None:
    """One rule, from the item's own settings or the platform's defaults.

    The item first, the platform second: the precedence every other option in
    this codebase uses.  ``fallback`` may be ``None`` -- Facebook reads only the
    item for these, and passing nothing is how it says so.
    """
    value = getattr(config, key, None)
    if value:
        return list(value)
    if fallback is not None:
        inherited = getattr(fallback, key, None)
        if inherited:
            return list(inherited)
    return None


def _haystack(scope: str, title: str, description: str) -> str:
    """The text one scope looks at.

    Two spaces between the halves for ``any``, which is how the existing calls
    joined them: it stops a phrase from matching across the seam, where the last
    word of the title and the first of the description are not adjacent in
    anything a human wrote.
    """
    if scope == "title":
        return title
    if scope == "description":
        return description
    return f"{title}  {description}"


def _can_exclude(scope: str, description_available: bool) -> bool:
    """Whether an exclusion with this scope could still fire on what is known.

    ``any`` is asked even with no description, and that asymmetry is not a
    shortcut: a banned word found in the title is *settled* -- the page will not
    make the word go away -- while not finding one is inconclusive and is
    reported as such by :func:`unanswered`.  This is also the behaviour that
    existed before the scoped keys, where the exclusion was checked against
    ``title + description`` whatever had been read; changing it would quietly
    start opening pages that used to be skipped.
    """
    return description_available or scope in ("title", "any")


def _can_require(scope: str, description_available: bool) -> bool:
    """Whether a requirement with this scope can be decided yet.

    Only the title one, and the asymmetry with :func:`_can_exclude` is the same
    fact read the other way round: for a requirement it is the *match* that is
    inconclusive without a description, not the miss.  A card that does not yet
    show the required word has not failed the rule, and rejecting it there is
    the bug ``description_available`` was introduced to fix.
    """
    return description_available or scope == "title"


def needs_description(config: Any, fallback: Any = None) -> bool:
    """Whether any word rule here can only be answered by the product page.

    Asked before a page is opened.  A search with nothing but title rules --
    or with no word rules at all -- reads a shop's whole catalogue from its
    results grid, which on Lider is the difference between one request that is
    served and forty-eight that are refused.
    """
    return any(_rule(config, fallback, key) for key in DESCRIPTION_KEYS)


def excluded_by(
    config: Any,
    title: str,
    description: str,
    fallback: Any = None,
    description_available: bool = True,
    logger: Logger | None = None,
) -> Tuple[str, Sequence[str]] | None:
    """The first exclusion rule this listing trips, or ``None``.

    The rule is returned rather than a boolean so the caller can log *which*
    words threw the listing away; "excluded by keywords" in a search with three
    lists is a log line that has to be guessed at.
    """
    for key, scope in EXCLUDE_KEYS:
        if not _can_exclude(scope, description_available):
            continue
        words = _rule(config, fallback, key)
        if not words:
            continue
        if is_substring(words, _haystack(scope, title, description), logger=logger):
            return key, words
    return None


def missing_required(
    config: Any,
    title: str,
    description: str,
    fallback: Any = None,
    description_available: bool = True,
    logger: Logger | None = None,
) -> Tuple[str, Sequence[str]] | None:
    """The first requirement this listing fails to meet, or ``None``.

    A rule that cannot be answered yet is not a rule this listing fails: it is
    skipped, and :func:`decision` reports the listing as undecided so the caller
    knows to come back once the description is in hand.  Treating it as failed
    is the mistake that would empty a search of everything a shop sells; treating
    it as met is the mistake that would let everything through.
    """
    for key, scope in REQUIRE_KEYS:
        if not _can_require(scope, description_available):
            continue
        words = _rule(config, fallback, key)
        if not words:
            continue
        if not is_substring(words, _haystack(scope, title, description), logger=logger):
            return key, words
    return None


def unanswered(
    config: Any, fallback: Any = None, description_available: bool = True
) -> bool:
    """Whether any rule was skipped for want of a description."""
    if description_available:
        return False
    return any(
        _rule(config, fallback, key)
        for key, scope in (*EXCLUDE_KEYS, *REQUIRE_KEYS)
        if scope != "title"
    )


def decision(
    config: Any,
    title: str,
    description: str,
    fallback: Any = None,
    description_available: bool = True,
    logger: Logger | None = None,
) -> Decision:
    """``REJECT``, ``ACCEPT`` or ``UNDECIDED`` for this listing's word rules.

    Exclusions are asked first: a title that carries a banned word is settled
    whatever else is missing, and settling it is what saves the page load.
    """
    if excluded_by(config, title, description, fallback, description_available, logger):
        return Decision.REJECT
    if missing_required(
        config, title, description, fallback, description_available, logger
    ):
        return Decision.REJECT
    if unanswered(config, fallback, description_available):
        return Decision.UNDECIDED
    return Decision.ACCEPT
