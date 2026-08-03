"""Wire format for the dashboard's incremental listing sync.

The dashboard keeps its own copy of every observed listing in IndexedDB and
refreshes it by asking "what changed since revision N?".  This module turns
:mod:`ai_marketplace_monitor.observations` records into that response.

Records are flattened on the way out -- the ``Listing`` snapshot is spread into
the top level rather than nested -- because the client indexes IndexedDB on
those fields directly and a flat row keeps the store schema and the wire format
the same shape.

Derived values (numeric price, currency, city/comuna) are deliberately *not*
computed here.  They come from heuristics that will get tuned as real data comes
in, and deriving them client-side means a tweak re-derives from the local copy
instead of forcing a full re-sync.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from diskcache import Cache  # type: ignore

from ..observations import current_revision, observations_since, store_epoch

#: Cap on records per response.  Big enough that a first sync of a few thousand
#: listings finishes in a handful of round trips, small enough that no single
#: response has to hold tens of thousands of descriptions in memory.
DEFAULT_LIMIT = 500
MAX_LIMIT = 2000

#: Fields lifted out of the stored ``Listing`` snapshot.
_SNAPSHOT_FIELDS = (
    "title",
    "price",
    "description",
    "location",
    "seller",
    "condition",
    "image",
    "post_url",
    "name",
)


def _fallback_url(marketplace: str, listing_id: str) -> str:
    """Reconstruct a listing URL when the snapshot has none."""
    if marketplace == "facebook":
        return f"https://www.facebook.com/marketplace/item/{listing_id}/"
    return ""


def serialize_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten one observation into the shape the client stores."""
    snapshot = record.get("listing")
    if not isinstance(snapshot, dict):
        snapshot = {}
    marketplace = str(record.get("marketplace") or "")
    listing_id = str(record.get("id") or "")

    row: Dict[str, Any] = {
        "key": f"{marketplace}:{listing_id}",
        "marketplace": marketplace,
        "id": listing_id,
        "rev": int(record.get("rev") or 0),
        "first_seen": record.get("first_seen") or "",
        "last_seen": record.get("last_seen") or "",
        "seen_count": int(record.get("seen_count") or 0),
        "matched": bool(record.get("matched", True)),
        "items": list(record.get("items") or []),
        "history": list(record.get("history") or []),
        "price_points": list(record.get("price_points") or []),
        "rating": record.get("rating") if isinstance(record.get("rating"), dict) else None,
        "notified": dict(record.get("notified") or {}),
    }
    for field in _SNAPSHOT_FIELDS:
        value = snapshot.get(field)
        row[field] = value if isinstance(value, str) else ("" if value is None else str(value))
    if not row["post_url"]:
        row["post_url"] = _fallback_url(marketplace, listing_id)
    return row


def clamp_limit(limit: Optional[int]) -> int:
    """Keep a client-supplied page size inside sane bounds."""
    if not limit or limit <= 0:
        return DEFAULT_LIMIT
    return min(int(limit), MAX_LIMIT)


def build_sync_response(
    local_cache: Cache,
    since: int = 0,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Answer one "what changed since ``since``?" request.

    ``epoch`` identifies the store, and ``revision`` is where the store is now.
    A client whose epoch differs, or whose cursor is ahead of ``revision``, is
    holding data from a store that has since been cleared and should drop its
    copy and start from zero.
    """
    page = clamp_limit(limit)
    records, cursor, more = observations_since(
        since=max(0, since), limit=page, local_cache=local_cache
    )
    rows: List[Dict[str, Any]] = [serialize_record(record) for record in records]
    return {
        "epoch": store_epoch(local_cache),
        "revision": current_revision(local_cache),
        "cursor": cursor,
        "more": more,
        "count": len(rows),
        "records": rows,
    }
