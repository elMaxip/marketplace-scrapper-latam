"""What changed between two configurations, and who it touches.

The monitor reloads its configuration while it is running, so "the file
changed" is never a good enough answer: a new notification token and a deleted
search both change the file, and only one of them is a reason to throw away the
search that is under way.  This module turns two snapshots of
:meth:`ai_marketplace_monitor.config.Config.describe` into the specific
question the loop actually has to answer -- *does this change affect what I am
doing right now?* -- and into the summary the web UI shows the user.

Nothing here reads a file or touches the loop.  It is a comparison of two
plain-data pictures, which is what makes it testable and what keeps the
decision out of the middle of the scraping code.

The snapshots come from ``describe()``, so they are the configuration *as
resolved*: every default applied and every inherited option folded in.  A
change that resolves to the same settings -- a comment added, a value moved
from ``[marketplace.facebook]`` to the item that already overrode it -- is
therefore not a change here, and rightly so: the search would run identically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Set, Tuple

#: One configured search: the product and the platform it runs on.
Pair = Tuple[str, str]

#: Option keys that carry runtime state rather than user intent.  ``describe``
#: renders every field of the item configuration, and this one counts searches
#: performed -- comparing it would report every search as edited after its
#: first run.
VOLATILE_OPTIONS = frozenset({"searched_count"})

#: Why a search under way can no longer be trusted, most decisive first.  The
#: order matters: a search that was both switched off and edited is reported as
#: switched off, because that is the fact that decides what happens to it.
REMOVED = "removed"
DISABLED = "disabled"
MODIFIED = "modified"
MARKETPLACE = "marketplace"


def _definition(entry: Dict[str, Any]) -> Any:
    """The part of a search that decides what it will actually fetch."""
    options = entry.get("options") or {}
    return (
        entry.get("market_type"),
        tuple(entry.get("search_phrases") or []),
        tuple(sorted((key, repr(value)) for key, value in options.items()
                     if key not in VOLATILE_OPTIONS)),
    )


def _searches_of(snapshot: Dict[str, Any]) -> Dict[Pair, Dict[str, Any]]:
    return {
        (str(entry.get("item")), str(entry.get("marketplace"))): entry
        for entry in (snapshot or {}).get("searches") or []
    }


def fingerprints(snapshot: Dict[str, Any]) -> Dict[Pair, str]:
    """A short stable string per search, changing exactly when its work does.

    The monitor uses it to tell "the same search, still queued" from "the same
    name, different search" after a reload -- which is how a pass that has
    already run half the searches can adopt an edit without starting over and
    without skipping the search that was edited.

    The platform's own settings are folded in, because they decide what the
    search fetches just as much as the phrases do: change the city under
    ``[marketplace.facebook]`` and every search on it is a different search,
    whatever its own section still says.
    """
    marketplaces = (snapshot or {}).get("marketplaces") or {}
    return {
        pair: repr((_definition(entry), marketplaces.get(pair[1])))
        for pair, entry in _searches_of(snapshot).items()
    }


@dataclass(frozen=True)
class ConfigChange:
    """The difference between the configuration loaded and the one on disk.

    Every field is a fact about searches or sections, never about files: the
    interface says "se eliminó la búsqueda X", and this is where that sentence
    gets its X.
    """

    #: Searches that did not exist before.
    added: Tuple[Pair, ...] = ()
    #: Searches that are gone from the file entirely.
    removed: Tuple[Pair, ...] = ()
    #: Searches that were switched back on.
    enabled: Tuple[Pair, ...] = ()
    #: Searches switched off, on the item or on the platform under it.
    disabled: Tuple[Pair, ...] = ()
    #: Searches whose phrases or filters changed.
    modified: Tuple[Pair, ...] = ()
    #: ``[marketplace.*]`` sections whose settings changed, added or removed.
    #: Every search on one of these runs differently now.
    marketplaces: Tuple[str, ...] = ()
    #: The ``[monitor]`` section: when and how often searches run.
    schedule: bool = False
    #: Anything else the snapshot cannot itemise -- users, AI services,
    #: notification targets, secrets.  Never a reason to stop a search.
    general: bool = False

    def __bool__(self: "ConfigChange") -> bool:
        return bool(
            self.added
            or self.removed
            or self.enabled
            or self.disabled
            or self.modified
            or self.marketplaces
            or self.schedule
            or self.general
        )

    def affects(self: "ConfigChange", item: str, marketplace: str) -> str | None:
        """Why a search of this pair should not be allowed to finish, or None.

        None is the common answer and the important one: most changes have
        nothing to do with the search running at that moment, and abandoning it
        would cost a page load and a set of results for nothing.
        """
        pair = (item, marketplace)
        if pair in self.removed:
            return REMOVED
        if pair in self.disabled:
            return DISABLED
        if pair in self.modified:
            return MODIFIED
        if marketplace in self.marketplaces:
            return MARKETPLACE
        return None

    def to_run(self: "ConfigChange", available: Iterable[Pair]) -> Set[Pair]:
        """Which of the searches that now exist deserve to run straight away.

        The ones the user just added, switched on, edited, or whose platform
        settings they changed.  Not the rest: a change to one search is no
        reason to re-search everything, and doing so is a burst of traffic the
        marketplace notices.
        """
        exists = set(available)
        pairs = {pair for pair in exists if self.affects(*pair) is not None}
        pairs |= set(self.added) | set(self.enabled)
        return pairs & exists

    def to_dict(self: "ConfigChange") -> Dict[str, Any]:
        """The shape the web UI reads.  Pairs become objects, not tuples."""

        def pairs(values: Tuple[Pair, ...]) -> List[Dict[str, str]]:
            return [{"item": item, "marketplace": marketplace} for item, marketplace in values]

        return {
            "added": pairs(self.added),
            "removed": pairs(self.removed),
            "enabled": pairs(self.enabled),
            "disabled": pairs(self.disabled),
            "modified": pairs(self.modified),
            "marketplaces": list(self.marketplaces),
            "schedule": self.schedule,
            "general": self.general,
        }

    def summary(self: "ConfigChange") -> str:
        """One English line for the log.  The interface writes its own."""
        parts: List[str] = []
        for label, values in (
            ("added", self.added),
            ("removed", self.removed),
            ("re-enabled", self.enabled),
            ("switched off", self.disabled),
            ("edited", self.modified),
        ):
            if values:
                names = ", ".join(sorted({item for item, _marketplace in values}))
                parts.append(f"{label}: {names}")
        if self.marketplaces:
            parts.append(f"""platform settings: {", ".join(self.marketplaces)}""")
        if self.schedule:
            parts.append("schedule")
        if self.general and not parts:
            parts.append("settings outside the searches")
        return "; ".join(parts) or "no visible difference"


def diff_config(before: Dict[str, Any], after: Dict[str, Any]) -> ConfigChange:
    """Compare two ``describe()`` snapshots.

    ``before`` is what the loop is running, ``after`` what is on disk.  An
    empty ``before`` -- nothing loaded yet -- makes every search an addition,
    which is the honest reading of "the monitor has just picked all of this up".

    A falsy result means the two resolve identically: a comment, a reordering,
    a value moved to where it was already being inherited from.  It can also
    mean a difference the snapshot does not render, a changed secret being the
    one that matters -- so a caller that knows the files differ (because it
    compared their hashes) should read a falsy result as
    :attr:`ConfigChange.general` rather than as nothing at all.  Only that
    caller can tell the two apart, which is why this does not guess.
    """
    old = _searches_of(before)
    new = _searches_of(after)

    added: List[Pair] = sorted(set(new) - set(old))
    removed: List[Pair] = sorted(set(old) - set(new))
    enabled: List[Pair] = []
    disabled: List[Pair] = []
    modified: List[Pair] = []

    for pair in sorted(set(old) & set(new)):
        was, is_now = old[pair], new[pair]
        if bool(was.get("enabled")) != bool(is_now.get("enabled")):
            # Reported as the switch it is, even when the settings moved too:
            # a search that is off does not run, and that is the whole story.
            (enabled if is_now.get("enabled") else disabled).append(pair)
        elif _definition(was) != _definition(is_now):
            modified.append(pair)

    old_marketplaces = (before or {}).get("marketplaces") or {}
    new_marketplaces = (after or {}).get("marketplaces") or {}
    marketplaces = sorted(
        name
        for name in set(old_marketplaces) | set(new_marketplaces)
        if old_marketplaces.get(name) != new_marketplaces.get(name)
    )

    schedule = (before or {}).get("monitor") != (after or {}).get("monitor")
    general = any(
        (before or {}).get(key) != (after or {}).get(key)
        for key in ("users", "ai", "notifications", "regions")
    )

    return ConfigChange(
        added=tuple(added),
        removed=tuple(removed),
        enabled=tuple(enabled),
        disabled=tuple(disabled),
        modified=tuple(modified),
        marketplaces=tuple(marketplaces),
        schedule=schedule,
        general=general,
    )
