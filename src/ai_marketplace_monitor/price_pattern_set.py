"""A list of excluded price patterns, written once and named.

The same three or four rules -- a run of nines, the keyboard walk, the zero that
means the listing is really an advert -- are wanted by every search a person
has, and they used to be retyped into each one.  Retyped filters drift: one
search excludes ``9*`` and its neighbour excludes ``99999``, and the group with
the placeholder still in it is the one whose average nobody can trust.

Deliberately the same shape as ``[region.*]`` rather than a mechanism of its
own, because the problem is the same one and it is already solved here: a
section the user names, referred to by that name from the searches, and
*resolved into the real values before anything runs*
(:meth:`Config.expand_price_patterns`).  Nothing downstream learns a new
concept -- :meth:`Marketplace.junk_price` still reads one flat list of pattern
strings, the web UI's "what the scraper is actually running" view shows that
resolved list, and a set that is renamed or deleted is caught by the loader with
the name in the message rather than by a filter that quietly stops matching.

The syntax of the patterns themselves is
:mod:`ai_marketplace_monitor.price_patterns`, which stays free of config: it
compiles strings into a predicate and knows nothing about where they came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from .price_patterns import validate_patterns
from .utils import BaseConfig, hilight


@dataclass
class PricePatternsConfig(BaseConfig):
    """One ``[price_patterns.<name>]`` section."""

    #: The patterns themselves, in the syntax of ``price_patterns.py``.
    patterns: List[str] = field(default_factory=list)
    #: A line for the user's own benefit.  Never read by anything that matches.
    description: str = ""

    def handle_patterns(self: "PricePatternsConfig") -> None:
        """Accept one pattern or a list, and refuse anything unparseable here.

        At load time rather than at match time, for the same reason
        ``handle_excluded_price_patterns`` does it: a pattern that cannot be
        compiled would otherwise fail in the middle of a search, and a filter
        that quietly matches nothing looks exactly like a filter that is working
        and finding nothing to exclude.  Worse here than there, because a bad
        entry in a saved set is silently wrong in every search that uses it.
        """
        if isinstance(self.patterns, str):
            self.patterns = [self.patterns]
        if not isinstance(self.patterns, list) or not all(
            isinstance(pattern, str) for pattern in self.patterns
        ):
            raise ValueError(
                f"Price patterns {hilight(self.name)} patterns must be a string or a "
                "list of strings."
            )
        self.patterns = [pattern.strip() for pattern in self.patterns if pattern.strip()]
        if not self.patterns:
            raise ValueError(
                f"Price patterns {hilight(self.name)} holds no pattern. Delete the "
                "section or give it something to exclude."
            )
        problems = validate_patterns(self.patterns)
        if problems:
            raise ValueError(f"Price patterns {hilight(self.name)}: {' '.join(problems)}")

    def handle_description(self: "PricePatternsConfig") -> None:
        if self.description is None:
            self.description = ""
        if not isinstance(self.description, str):
            raise ValueError(
                f"Price patterns {hilight(self.name)} description must be a string."
            )
