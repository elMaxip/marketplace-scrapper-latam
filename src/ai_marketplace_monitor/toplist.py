"""The cheapest listing a search has, and noticing when that number goes down.

Every other notification the monitor sends is about *one listing*: it is new, it
got cheaper, the seller edited it.  This one is not.  "The cheapest PS5 anyone
is offering is now 359.990" is a fact about a whole search -- it can become true
because a new listing appeared, because an old one was reduced, or because the
listing that held the record was sold and the next one down inherited it -- and
no amount of looking at a single listing can tell you it happened.

So the question is asked at the two moments where the picture is complete: after
a search finishes, and after a round of re-checks finishes.  Both are already
places the monitor stops and takes stock.

Three rules, and the second is the one that matters:

* **Only listings that passed.**  The floor is taken from listings the filters
  kept (``matched``), which by the time this runs also excludes junk prices --
  see :mod:`ai_marketplace_monitor.price_patterns`.  Without that a placeholder
  "0" is the cheapest listing of every search, forever.
* **Only announce a *lower* number.**  The stored record is the price, not the
  listing.  A different listing at the same price is not news, and announcing it
  would mean a message every time two sellers tie and the sort order wobbles.
* **A search that never had a top gets one message.**  The first time there is
  anything to say, it is said; after that only improvements are.

The question is asked about a *scope*, which is usually one search and is not
always: trackers can be gathered into a group (``[track.<name>] group = ...``),
and the cheapest of five pages watched by hand is exactly the fact that group
exists to produce.  ``scope_of`` folds those names together before the floor is
taken -- the winner is still one listing on one page, so nothing about a
tracker's own identity is lost; what the group changes is who it competed
against.  A name with no scope is its own scope, so a lone tracker and an
ordinary search behave exactly as they did.

The record lives in its own cache namespace keyed by search name, next to the
notification cache rather than inside the observation store: it is not a fact
about any listing, and writing it into one would bump that listing's revision
and make every dashboard re-sync a row that did not change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from diskcache import Cache  # type: ignore

from .listing import Listing
from .observations import iter_observations
from .utils import CacheType, cache, price_value

logger = logging.getLogger(__name__)

TOP_TAG = CacheType.TOP_LISTING.value


def _resolve(local_cache: Cache | None) -> Cache:
    return cache if local_cache is None else local_cache


def top_key(item_name: str) -> Tuple[str, str]:
    return (TOP_TAG, item_name)


@dataclass(frozen=True)
class TopListing:
    """The cheapest valid listing of one search, with the listing to show."""

    marketplace: str
    id: str
    price: str
    value: float
    #: The stored snapshot, so a notification can be built without a second
    #: pass over the cache.  Absent only for a record read back from an older
    #: store, which is why every caller treats it as optional.
    snapshot: Dict[str, Any]
    #: The AI's verdict on this listing, as the observation store holds it, or
    #: an empty dict.  Carried so the top-1 message can show the same stars the
    #: dashboard does rather than arriving as the one notification with no
    #: rating on it -- the listing has been evaluated, this flow simply is not
    #: the one that evaluated it.
    rating: Dict[str, Any] = field(default_factory=dict)

    def as_listing(self: "TopListing") -> Optional[Listing]:
        """The snapshot as a :class:`Listing`, or None when it cannot be.

        A stored snapshot is whatever ``Listing`` looked like when it was
        written, so a field added since then is missing and one removed since is
        extra.  Both are ordinary, and neither is worth an exception on the
        notification path: a top-1 that cannot be rendered is simply not
        announced this round.
        """
        if not self.snapshot:
            return None
        try:
            return Listing(**self.snapshot)
        except KeyboardInterrupt:
            raise
        except TypeError:
            logger.debug("Stored snapshot for %s/%s is not a Listing", self.marketplace, self.id)
            return None


def _item_names_of(record: Dict[str, Any], snapshot: Dict[str, Any]) -> Tuple[str, ...]:
    """Every search this stored listing turned up under.

    ``items`` accumulates across sightings because one listing can match several
    searches; the snapshot's own name is the fallback for a record written
    before ``items`` existed.
    """
    items = record.get("items")
    if isinstance(items, list) and items:
        return tuple(str(name) for name in items)
    name = snapshot.get("name")
    return (str(name),) if name else ()


def current_tops(
    item_names: Sequence[str],
    marketplaces: Sequence[str] | None = None,
    local_cache: Cache | None = None,
    scope_of: Mapping[str, str] | None = None,
) -> Dict[str, TopListing]:
    """The cheapest valid listing of each named scope, in one pass.

    One pass and not one per search, which is the whole reason this exists
    alongside :func:`current_top`: the review lane asks about every configured
    search at the end of every round, and the store is walked record by record
    off disk.  Twenty searches used to mean twenty full walks of the same
    thousands of records to answer one question.

    Ties are broken by listing id rather than left to the store's iteration
    order.  Not aesthetics: two listings at the same price would otherwise take
    turns being "the top one" between rounds, and while that never produces a
    notification -- the price has not gone down -- it does make the stored
    record churn for no reason.

    ``scope_of`` maps a name in ``item_names`` to the bucket it competes in, for
    the trackers a user has grouped.  The keys of the result are those buckets,
    which is what the caller stores the record under and what the message names.
    """
    wanted_items = set(item_names)
    if not wanted_items:
        return {}
    scopes = dict(scope_of or {})
    wanted_markets = set(marketplaces) if marketplaces else None
    best: Dict[str, Tuple[float, str, Dict[str, Any]]] = {}

    for record in iter_observations(local_cache):
        if record.get("deleted") or not record.get("matched", True):
            continue
        if wanted_markets is not None:
            if str(record.get("marketplace") or "") not in wanted_markets:
                continue
        snapshot = record.get("listing")
        if not isinstance(snapshot, dict):
            continue
        names = [name for name in _item_names_of(record, snapshot) if name in wanted_items]
        if not names:
            continue
        value = price_value(snapshot.get("price"))
        if value is None:
            continue
        listing_id = str(record.get("id") or "")
        # `set` and not the list: two trackers of the same group can be on one
        # record only in theory, but folding them would otherwise let one
        # listing be compared with itself.
        for name in {scopes.get(name, name) for name in names}:
            held = best.get(name)
            if held is None or (value, listing_id) < (held[0], held[1]):
                best[name] = (value, listing_id, record)

    tops: Dict[str, TopListing] = {}
    for name, (value, _id, record) in best.items():
        snapshot = record.get("listing") or {}
        rating = record.get("rating")
        tops[name] = TopListing(
            marketplace=str(record.get("marketplace") or ""),
            id=str(record.get("id") or ""),
            price=str(snapshot.get("price") or ""),
            value=value,
            snapshot=dict(snapshot),
            rating=dict(rating) if isinstance(rating, dict) else {},
        )
    return tops


def current_top(
    item_name: str,
    marketplaces: Sequence[str] | None = None,
    local_cache: Cache | None = None,
) -> Optional[TopListing]:
    """The cheapest valid listing one search has right now, or None."""
    return current_tops([item_name], marketplaces, local_cache).get(item_name)


def stored_top(item_name: str, local_cache: Cache | None = None) -> Optional[Dict[str, Any]]:
    """What was last announced for this search, or None when nothing was."""
    try:
        record = _resolve(local_cache).get(top_key(item_name))
    except KeyboardInterrupt:
        raise
    except Exception:
        return None
    return record if isinstance(record, dict) else None


def remember_top(
    item_name: str, top: TopListing, local_cache: Cache | None = None
) -> None:
    """Record what was announced, so the same number is not announced twice.

    Best effort, like the observation writes: failing to remember a top-1 costs
    at worst one repeated message, and taking down a scrape over it would cost
    considerably more.
    """
    try:
        _resolve(local_cache).set(
            top_key(item_name),
            {
                "marketplace": top.marketplace,
                "id": top.id,
                "price": top.price,
                "value": float(top.value),
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            tag=TOP_TAG,
        )
    except KeyboardInterrupt:
        raise
    except Exception:
        logger.debug("Could not remember the top listing for %r", item_name, exc_info=True)


def _previous_value(record: Optional[Dict[str, Any]]) -> Optional[float]:
    """The price last announced, as a number, or None when unusable.

    An unreadable stored value is treated as "nothing was announced", which
    errs towards one extra message rather than towards permanent silence: a
    corrupt record must not be able to switch the feature off for good.
    """
    if not isinstance(record, dict):
        return None
    value = record.get("value")
    if isinstance(value, (int, float)):
        return float(value)
    return price_value(record.get("price"))


def new_tops(
    item_names: Sequence[str],
    marketplaces: Sequence[str] | None = None,
    local_cache: Cache | None = None,
    scope_of: Mapping[str, str] | None = None,
) -> Dict[str, TopListing]:
    """The searches whose cheapest listing is worth announcing, in one pass.

    A search is left out when nothing changed, when its cheapest got *more*
    expensive (the record holder sold and the next one up inherited the title --
    true, and not news anybody asked for), and when it has no priced listings at
    all.

    Writes nothing: the caller announces first and calls :func:`remember_top`
    after, so a notification that could not be delivered is tried again next
    round rather than being marked as sent.
    """
    tops = current_tops(item_names, marketplaces, local_cache, scope_of)
    fresh: Dict[str, TopListing] = {}
    for name, top in tops.items():
        previous = _previous_value(stored_top(name, local_cache))
        if previous is not None and top.value >= previous:
            continue
        fresh[name] = top
    return fresh


def new_top(
    item_name: str,
    marketplaces: Sequence[str] | None = None,
    local_cache: Cache | None = None,
) -> Optional[TopListing]:
    """The cheapest listing of one search, but only when it is worth announcing."""
    return new_tops([item_name], marketplaces, local_cache).get(item_name)


def forget_top(item_name: str, local_cache: Cache | None = None) -> None:
    """Drop the record for one search.  For a search that was deleted."""
    try:
        _resolve(local_cache).delete(top_key(item_name))
    except KeyboardInterrupt:
        raise
    except Exception:
        logger.debug("Could not forget the top listing for %r", item_name, exc_info=True)
