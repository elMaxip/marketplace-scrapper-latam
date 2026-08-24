"""CSV export of every listing under one search item.

Distinct from :mod:`ai_marketplace_monitor.webui.found_export`, which exports the
*notified* spine -- one row per notification, joined back to whatever details
were still cached.  This one exports what the dashboard actually shows: the
observation store, filtered to one search item, whether or not anything was ever
notified about it, and including the listings the filters rejected.

The rows are built from the observation record alone, so the export covers the
whole group rather than the page the user happens to be looking at, and needs no
second pass over the cache per listing.

Prices are written twice: as the marketplace printed them (``"$180.000 |
$200.000"``) and as a number, because a spreadsheet can do arithmetic on the
second and nothing sensible with the first.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Iterator, List, Optional

from diskcache import Cache  # type: ignore

from ..observations import iter_observations
from ..utils import price_value
from .found_export import iter_csv

#: What makes Excel on Windows read the file as UTF-8.  Without it "Ñuñoa"
#: arrives mangled; every other reader ignores it.
BOM = "\ufeff"

#: Column order.  Named in Spanish to match the interface this is exported from.
CSV_COLUMNS: List[str] = [
    "titulo",
    "precio_actual",
    "precio_actual_valor",
    "precio_anterior_valor",
    "variacion_precio",
    "precio_minimo_historico",
    "plataforma",
    "vendedor",
    "ubicacion",
    "estado_producto",
    "estado",
    "puntaje_ia",
    "comentario_ia",
    "veces_vista",
    "primera_vez_vista",
    "ultima_revision",
    "url",
]


def _fallback_url(marketplace: str, listing_id: str) -> str:
    """Reconstruct a listing URL when the snapshot has none."""
    if marketplace == "facebook":
        return f"https://www.facebook.com/marketplace/item/{listing_id}/"
    return ""


def _number(value: Optional[float]) -> str:
    """A number for a spreadsheet, or an empty cell when there is none.

    Plain ``123456`` / ``123456.5``: no thousands separator and a dot for the
    decimal, so the cell parses as a number in any locale rather than becoming
    text in half of them.
    """
    if value is None:
        return ""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}"


def _prices(record: Dict[str, Any]) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """``(current, previous, lowest ever)`` from the recorded price series.

    ``price_points`` only grows when the price actually moves, so the last point
    is what the listing costs now and the one before it is what it cost before --
    which is exactly what "the price changed by" means here.  The current price
    is taken from the snapshot rather than from the series, because a listing
    whose price never changed has no series at all.
    """
    snapshot = record.get("listing") if isinstance(record.get("listing"), dict) else {}
    current = price_value(snapshot.get("price"))

    points = [point for point in (record.get("price_points") or []) if isinstance(point, dict)]
    previous = price_value(points[-2].get("price")) if len(points) >= 2 else None

    lowest = current
    for point in points:
        value = price_value(point.get("price"))
        if value is not None and (lowest is None or value < lowest):
            lowest = value
    return current, previous, lowest


def to_row(record: Dict[str, Any]) -> Dict[str, str]:
    """One observation as a CSV row."""
    snapshot = record.get("listing") if isinstance(record.get("listing"), dict) else {}
    marketplace = str(record.get("marketplace") or "")
    listing_id = str(record.get("id") or "")
    rating = record.get("rating") if isinstance(record.get("rating"), dict) else {}
    current, previous, lowest = _prices(record)
    change = None if current is None or previous is None else current - previous

    return {
        "titulo": str(snapshot.get("title") or ""),
        "precio_actual": str(snapshot.get("price") or ""),
        "precio_actual_valor": _number(current),
        "precio_anterior_valor": _number(previous),
        "variacion_precio": _number(change),
        "precio_minimo_historico": _number(lowest),
        "plataforma": marketplace,
        "vendedor": str(snapshot.get("seller") or ""),
        "ubicacion": str(snapshot.get("location") or ""),
        "estado_producto": str(snapshot.get("condition") or ""),
        # The monitor's keep/discard verdict for this listing, which is what the
        # dashboard means by a listing being "valid" for a group's statistics.
        "estado": "activa" if record.get("matched", True) else "descartada",
        "puntaje_ia": str(rating.get("score")) if rating.get("score") is not None else "",
        "comentario_ia": str(rating.get("comment") or ""),
        "veces_vista": str(int(record.get("seen_count") or 0)),
        "primera_vez_vista": str(record.get("first_seen") or ""),
        "ultima_revision": str(record.get("last_seen") or ""),
        "url": str(snapshot.get("post_url") or "") or _fallback_url(marketplace, listing_id),
    }


def belongs_to(record: Dict[str, Any], item: str) -> bool:
    """Whether a record is part of the group named ``item``.

    The same two places the dashboard groups by: ``items``, accumulated across
    sightings because one listing can match several searches, and the search
    item stamped on the snapshot.  Matched case-insensitively, since the group
    name travels through a URL.
    """
    wanted = item.strip().casefold()
    if not wanted:
        return False
    snapshot = record.get("listing") if isinstance(record.get("listing"), dict) else {}
    names = [*(record.get("items") or []), snapshot.get("name")]
    return any(str(name).strip().casefold() == wanted for name in names if name)


def iter_group_rows(local_cache: Cache, item: str) -> Iterator[Dict[str, str]]:
    """Every listing of one group, cheapest first, then most recently checked.

    Sorted rather than left in cache order so the export opens on the same
    reading as the group screen: the cheapest offer at the top.  Listings with
    no usable price sink to the bottom instead of sorting as zero.
    """
    records = [
        record
        for record in iter_observations(local_cache)
        if not record.get("deleted") and belongs_to(record, item)
    ]

    def by_price(record: Dict[str, Any]) -> tuple:
        current, _previous, _lowest = _prices(record)
        return (current is None, current if current is not None else 0.0)

    # Two passes rather than one composite key: the recency half runs backwards
    # and a timestamp string cannot be negated.  Python's sort is stable, so the
    # second pass keeps the first one's order inside each price.
    records.sort(key=lambda record: str(record.get("last_seen") or ""), reverse=True)
    records.sort(key=by_price)
    for record in records:
        yield to_row(record)


def iter_group_csv(rows: Iterable[Dict[str, str]]) -> Iterator[str]:
    """Serialize group rows, byte-order mark first (see :data:`BOM`)."""
    first = True
    for chunk in iter_csv(rows, CSV_COLUMNS):
        yield (BOM + chunk) if first else chunk
        first = False


def group_csv(local_cache: Cache, item: str) -> str:
    """The whole export as one string (convenience over :func:`iter_group_csv`)."""
    return "".join(iter_group_csv(iter_group_rows(local_cache, item)))
