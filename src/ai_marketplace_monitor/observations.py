"""Persistent sighting log for every listing the monitor examines.

:meth:`Listing.to_cache` keeps only the *latest* snapshot of a listing, keyed by
URL and overwritten in place, so the moment a listing was first seen, how often
it reappeared, and every price it ever carried are all lost.  The analytics
dashboard needs exactly those, so each sighting is additionally recorded here as
an *observation*: first/last seen, a sighting count, a timestamped diff log of
the fields that changed between sightings, and the AI rating / notification
outcome once those are known.

Observations live in their own cache namespace keyed by ``(marketplace, id)``,
matching the shape ``USER_NOTIFIED`` already uses.  Every write bumps a global
monotonic revision and stamps the record with it; that is what lets the web UI
pull only what changed since its last sync instead of re-reading the whole cache
(see :mod:`ai_marketplace_monitor.webui.listings_api`).

Writes are best effort.  Analytics must never take down a scrape, so
:func:`record_observation` and friends swallow their own failures.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from diskcache import Cache  # type: ignore

from .listing import Listing
from .utils import CacheType, cache

logger = logging.getLogger(__name__)

OBSERVATION_TAG = CacheType.LISTING_OBSERVATION.value
META_TAG = CacheType.LISTING_OBSERVATION_META.value

_REVISION_KEY = (META_TAG, "revision")
_EPOCH_KEY = (META_TAG, "epoch")

#: Snapshot fields whose changes are worth a history entry.
TRACKED_FIELDS: Tuple[str, ...] = (
    "title",
    "price",
    "description",
    "location",
    "seller",
    "condition",
    "image",
)

#: Keep the timeline bounded; a listing that churns forever must not grow a
#: record without limit.  Oldest entries are dropped first.
MAX_HISTORY = 200

#: Same idea for the price series, which the dashboard uses for "lowest price
#: ever seen".  It only grows when the price actually changes, so it is far
#: slower moving than the generic history.
MAX_PRICE_POINTS = 200

ObservationKey = Tuple[str, str, str]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def observation_key(marketplace: str, listing_id: str) -> ObservationKey:
    """Cache key for one listing's observation record."""
    return (OBSERVATION_TAG, marketplace, listing_id)


def _diff_value(field: str, value: Any) -> Any:
    """Normalize a field before comparing two sightings.

    Facebook image URLs carry an expiring signature in the query string, so a
    raw comparison would report an image change on every single scrape.  Compare
    the path only; the full URL is still what gets stored.
    """
    if field == "image" and isinstance(value, str):
        return value.split("?")[0]
    return value


# --------------------------------------------------------------------------- #
# Revision bookkeeping
# --------------------------------------------------------------------------- #


def _resolve(local_cache: Cache | None) -> Cache:
    return cache if local_cache is None else local_cache


def current_revision(local_cache: Cache | None = None) -> int:
    """Highest revision handed out so far (0 when nothing was ever recorded)."""
    try:
        return int(_resolve(local_cache).get(_REVISION_KEY) or 0)
    except KeyboardInterrupt:
        raise
        # A corrupt/absent counter reads as "nothing recorded yet".
    except Exception:
        return 0


def store_epoch(local_cache: Cache | None = None) -> str:
    """Identity of the current observation store.

    A client that has synced a store caches this value alongside its cursor.  A
    ``--clear-cache all`` wipes the counter and mints a fresh epoch, and the
    mismatch tells the client its local copy is stale and must be rebuilt rather
    than resumed.
    """
    local = _resolve(local_cache)
    try:
        epoch = local.get(_EPOCH_KEY)
        if isinstance(epoch, str) and epoch:
            return epoch
        epoch = uuid.uuid4().hex
        # ``add`` is a no-op when another writer got there first, so re-read
        # instead of trusting what we just generated.
        local.add(_EPOCH_KEY, epoch, tag=META_TAG)
        stored = local.get(_EPOCH_KEY)
        return stored if isinstance(stored, str) and stored else epoch
    except KeyboardInterrupt:
        raise
    except Exception:
        logger.debug("Could not read or create the observation store epoch", exc_info=True)
        return ""


def _next_revision(local: Cache) -> int:
    """Allocate the next revision.  Caller must hold a transaction."""
    revision = int(local.get(_REVISION_KEY) or 0) + 1
    # Tagged with OBSERVATION_TAG so that ``--clear-cache listing-observations``
    # resets the counter together with the records it numbers.  Clients notice
    # the counter going backwards and resync from scratch.
    local.set(_REVISION_KEY, revision, tag=OBSERVATION_TAG)
    return revision


# --------------------------------------------------------------------------- #
# In-memory revision index
# --------------------------------------------------------------------------- #


class _Index:
    """``(marketplace, id) -> rev``, so "what changed since N" avoids a scan.

    diskcache cannot query by value, so answering a sync request from the cache
    alone means reading every observation.  The monitor and the web UI share a
    process (the UI runs on a thread), so the writer can keep this index hot and
    the reader only rebuilds it when it finds the stored revision ahead of the
    one the index reflects -- which, with an in-process writer, is just the first
    request after startup.
    """

    __slots__ = ("revision", "by_key")

    def __init__(self, revision: int, by_key: Dict[Tuple[str, str], int]) -> None:
        self.revision = revision
        self.by_key = by_key


_index_lock = threading.Lock()
_indexes: Dict[str, _Index] = {}


def _index_id(local: Cache) -> str:
    return str(getattr(local, "directory", "")) or str(id(local))


def _rebuild_index(local: Cache) -> _Index:
    """Scan the observation namespace once to recover the revision map."""
    by_key: Dict[Tuple[str, str], int] = {}
    for key in local.iterkeys():
        if not isinstance(key, tuple) or len(key) != 3 or key[0] != OBSERVATION_TAG:
            continue
        try:
            record = local.get(key)
        except KeyboardInterrupt:
            raise
        except Exception:
            logger.debug("Skipping unreadable observation %r", key, exc_info=True)
            continue
        if isinstance(record, dict):
            by_key[(key[1], key[2])] = int(record.get("rev") or 0)
    return _Index(current_revision(local), by_key)


def _get_index(local: Cache) -> _Index:
    """Return an index guaranteed to reflect at least the stored revision."""
    ident = _index_id(local)
    with _index_lock:
        index = _indexes.get(ident)
        if index is None or index.revision < current_revision(local):
            index = _rebuild_index(local)
            _indexes[ident] = index
        elif index.revision > current_revision(local):
            # The store was cleared out from under us.
            index = _rebuild_index(local)
            _indexes[ident] = index
        return index


def _touch_index(local: Cache, marketplace: str, listing_id: str, revision: int) -> None:
    ident = _index_id(local)
    with _index_lock:
        index = _indexes.get(ident)
        if index is None:
            return
        index.by_key[(marketplace, listing_id)] = revision
        index.revision = max(index.revision, revision)


def reset_index_cache() -> None:
    """Drop every cached index.  For tests, which build throwaway caches."""
    with _index_lock:
        _indexes.clear()


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def _new_record(listing: Listing, now: str) -> Dict[str, Any]:
    return {
        "marketplace": listing.marketplace,
        "id": listing.id,
        "first_seen": now,
        "last_seen": now,
        "seen_count": 0,
        "matched": True,
        "items": [],
        "listing": {},
        "history": [],
        "price_points": [],
        "rating": None,
        "notified": {},
        "rev": 0,
    }


def _update(
    listing: Listing,
    mutate: Callable[[Dict[str, Any]], bool],
    local_cache: Cache | None,
) -> Optional[Dict[str, Any]]:
    """Read-modify-write one observation under a transaction.

    ``mutate`` returns whether anything actually changed; when it returns False
    no revision is burned and no write happens, which keeps repeat AI
    evaluations (they are themselves cached) from forcing clients to resync.
    """
    if not listing.marketplace or not listing.id:
        return None
    local = _resolve(local_cache)
    key = observation_key(listing.marketplace, listing.id)
    try:
        with local.transact():
            record = local.get(key)
            if isinstance(record, dict) and record.get("deleted"):
                # Deleted from the dashboard on purpose. Seeing it again is the
                # normal case -- the scraper has no memory of what the user threw
                # away -- so the tombstone has to outrank the sighting, or the
                # delete button would be undone by the very next search.
                return None
            if not isinstance(record, dict):
                record = _new_record(listing, _now())
            if not mutate(record):
                return record
            record["rev"] = _next_revision(local)
            local.set(key, record, tag=OBSERVATION_TAG)
        _touch_index(local, listing.marketplace, listing.id, int(record["rev"]))
        return record
    except KeyboardInterrupt:
        raise
    except Exception:
        # Analytics bookkeeping must never break a scrape.
        logger.debug("Failed to record observation for %r", key, exc_info=True)
        return None


def record_observation(
    listing: Listing,
    matched: bool = True,
    item_name: str | None = None,
    local_cache: Cache | None = None,
) -> Optional[Dict[str, Any]]:
    """Record that ``listing`` was seen right now.

    ``matched`` is the monitor's keep/discard verdict for this sighting, kept so
    the dashboard can show the whole market rather than only the hits.
    ``item_name`` is the search item it turned up under, accumulated across
    sightings because one listing can match several searches.
    """
    snapshot = asdict(listing)
    now = _now()

    def mutate(record: Dict[str, Any]) -> bool:
        previous = record.get("listing") or {}
        first_sighting = not previous

        if not first_sighting:
            changes = {
                field: {"from": previous.get(field), "to": snapshot.get(field)}
                for field in TRACKED_FIELDS
                if _diff_value(field, previous.get(field))
                != _diff_value(field, snapshot.get(field))
            }
            if changes:
                history: List[Dict[str, Any]] = list(record.get("history") or [])
                history.append({"ts": now, "changes": changes})
                record["history"] = history[-MAX_HISTORY:]

        price = snapshot.get("price")
        points: List[Dict[str, Any]] = list(record.get("price_points") or [])
        if price and (not points or points[-1].get("price") != price):
            points.append({"ts": now, "price": price})
            record["price_points"] = points[-MAX_PRICE_POINTS:]

        record["listing"] = snapshot
        record["last_seen"] = now
        record["seen_count"] = int(record.get("seen_count") or 0) + 1
        record["matched"] = bool(matched)
        if item_name:
            items = list(record.get("items") or [])
            if item_name not in items:
                items.append(item_name)
                record["items"] = sorted(items)
        return True

    return _update(listing, mutate, local_cache)


def record_rating(
    listing: Listing,
    score: int,
    comment: str = "",
    conclusion: str = "",
    ai_name: str | None = None,
    local_cache: Cache | None = None,
) -> Optional[Dict[str, Any]]:
    """Attach the latest AI verdict.  A repeat of the same verdict is ignored."""
    rating = {
        "score": int(score),
        "comment": comment or "",
        "conclusion": conclusion or "",
        "name": ai_name or "",
    }

    def mutate(record: Dict[str, Any]) -> bool:
        existing = record.get("rating")
        if isinstance(existing, dict) and all(
            existing.get(field) == value for field, value in rating.items()
        ):
            return False
        rating["ts"] = _now()
        record["rating"] = rating
        return True

    return _update(listing, mutate, local_cache)


def record_notification(
    listing: Listing,
    user: str,
    local_cache: Cache | None = None,
) -> Optional[Dict[str, Any]]:
    """Note that ``user`` was notified about this listing, with a timestamp."""

    def mutate(record: Dict[str, Any]) -> bool:
        notified: Dict[str, Any] = dict(record.get("notified") or {})
        if user in notified:
            return False
        notified[user] = _now()
        record["notified"] = notified
        return True

    return _update(listing, mutate, local_cache)


# --------------------------------------------------------------------------- #
# Deleting
# --------------------------------------------------------------------------- #


def is_deleted(record: Optional[Dict[str, Any]]) -> bool:
    """Whether a record is a tombstone rather than a real observation."""
    return isinstance(record, dict) and bool(record.get("deleted"))


def delete_observations(
    keys: List[Tuple[str, str]],
    local_cache: Cache | None = None,
) -> Tuple[int, int]:
    """Remove listings from the dashboard, permanently.

    The record is replaced by a tombstone rather than erased.  Two things need
    it: the sync is "what changed since revision N?", so a deletion has to carry
    a revision of its own or clients would keep the row until their next full
    rebuild; and :func:`record_observation` consults it, so a listing the user
    threw away does not walk back in on the next search.

    Returns ``(deleted, revision)`` -- how many records were actually removed,
    and where the store's revision counter ended up.  Keys that were already
    gone (or already tombstoned) are not counted and burn no revision.
    """
    local = _resolve(local_cache)
    deleted = 0
    now = _now()
    for marketplace, listing_id in keys:
        if not marketplace or not listing_id:
            continue
        key = observation_key(marketplace, listing_id)
        try:
            with local.transact():
                record = local.get(key)
                if not isinstance(record, dict) or record.get("deleted"):
                    continue
                tombstone: Dict[str, Any] = {
                    "marketplace": marketplace,
                    "id": listing_id,
                    "deleted": True,
                    "deleted_at": now,
                    "rev": _next_revision(local),
                }
                local.set(key, tombstone, tag=OBSERVATION_TAG)
            _touch_index(local, marketplace, listing_id, int(tombstone["rev"]))
            deleted += 1
        except KeyboardInterrupt:
            raise
        except Exception:
            logger.debug("Failed to delete observation %r", key, exc_info=True)
            continue
    return deleted, current_revision(local)


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def get_observation(
    marketplace: str,
    listing_id: str,
    local_cache: Cache | None = None,
) -> Optional[Dict[str, Any]]:
    """One observation record, or None when the listing was never seen."""
    try:
        record = _resolve(local_cache).get(observation_key(marketplace, listing_id))
    except KeyboardInterrupt:
        raise
    except Exception:
        return None
    return record if isinstance(record, dict) else None


def iter_observations(local_cache: Cache | None = None) -> Iterator[Dict[str, Any]]:
    """Every observation record, in no particular order."""
    local = _resolve(local_cache)
    for key in local.iterkeys():
        if not isinstance(key, tuple) or len(key) != 3 or key[0] != OBSERVATION_TAG:
            continue
        try:
            record = local.get(key)
        except KeyboardInterrupt:
            raise
        except Exception:
            logger.debug("Skipping unreadable observation %r", key, exc_info=True)
            continue
        if isinstance(record, dict):
            yield record


def observations_since(
    since: int = 0,
    limit: int = 500,
    local_cache: Cache | None = None,
) -> Tuple[List[Dict[str, Any]], int, bool]:
    """Records with ``rev > since``, oldest revision first.

    Returns ``(records, cursor, more)``.  ``cursor`` is the revision the caller
    should send next; it advances to the last returned record, or to the store's
    current revision when nothing was pending.  ``more`` says whether the page
    was truncated, so a client can keep pulling without waiting for a poll.
    """
    local = _resolve(local_cache)
    index = _get_index(local)
    pending = sorted(
        ((rev, key) for key, rev in index.by_key.items() if rev > since),
        key=lambda entry: entry[0],
    )
    truncated = len(pending) > limit
    records: List[Dict[str, Any]] = []
    cursor = since
    for rev, (marketplace, listing_id) in pending[:limit]:
        record = get_observation(marketplace, listing_id, local_cache=local)
        if record is None:
            # Evicted between indexing and reading; the index self-heals on its
            # next rebuild, and skipping keeps the cursor moving.
            cursor = max(cursor, rev)
            continue
        records.append(record)
        cursor = max(cursor, int(record.get("rev") or rev))
    if not pending:
        cursor = max(cursor, current_revision(local))
    return records, cursor, truncated
