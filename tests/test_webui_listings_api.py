"""Tests for the dashboard's incremental listing sync payload."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from diskcache import Cache  # type: ignore

from ai_marketplace_monitor import observations as obs
from ai_marketplace_monitor.listing import Listing
from ai_marketplace_monitor.webui.listings_api import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    build_sync_response,
    clamp_limit,
    serialize_record,
)


def _listing(listing_id: str = "123", price: str = "$100") -> Listing:
    return Listing(
        marketplace="facebook",
        name="iphone",
        id=listing_id,
        title="iPhone 13",
        image="http://img/x.jpg",
        price=price,
        post_url=f"https://www.facebook.com/marketplace/item/{listing_id}/?ref=search",
        location="Nunoa, Region Metropolitana",
        seller="Jane Doe",
        condition="used_good",
        description="great phone",
    )


@pytest.fixture
def temp_cache(tmp_path: Path) -> Iterator[Cache]:
    cache = Cache(str(tmp_path / "cache"))
    obs.reset_index_cache()
    yield cache
    obs.reset_index_cache()
    cache.close()


def test_clamp_limit_bounds() -> None:
    assert clamp_limit(None) == DEFAULT_LIMIT
    assert clamp_limit(0) == DEFAULT_LIMIT
    assert clamp_limit(-5) == DEFAULT_LIMIT
    assert clamp_limit(10) == 10
    assert clamp_limit(MAX_LIMIT * 10) == MAX_LIMIT


def test_serialize_flattens_the_snapshot(temp_cache: Cache) -> None:
    listing = _listing()
    obs.record_observation(listing, item_name="iphone", local_cache=temp_cache)
    obs.record_rating(listing, score=4, comment="ok", conclusion="Good", local_cache=temp_cache)
    record = obs.get_observation("facebook", "123", local_cache=temp_cache)
    assert record is not None

    row = serialize_record(record)
    assert row["key"] == "facebook:123"
    assert row["title"] == "iPhone 13"
    assert row["price"] == "$100"
    assert row["seller"] == "Jane Doe"
    assert row["location"] == "Nunoa, Region Metropolitana"
    assert row["items"] == ["iphone"]
    assert row["rating"]["score"] == 4
    assert row["seen_count"] == 1


def test_serialize_reconstructs_a_missing_url() -> None:
    row = serialize_record({"marketplace": "facebook", "id": "555", "listing": {}})
    assert row["post_url"] == "https://www.facebook.com/marketplace/item/555/"


def test_serialize_tolerates_a_malformed_record() -> None:
    """A record written by an older version must not take the whole sync down."""
    row = serialize_record(
        {"marketplace": "facebook", "id": "9", "listing": None, "rating": "nope"}
    )
    assert row["title"] == ""
    assert row["rating"] is None
    assert row["items"] == []


def test_sync_response_shape(temp_cache: Cache) -> None:
    obs.record_observation(_listing("1"), local_cache=temp_cache)
    obs.record_observation(_listing("2"), local_cache=temp_cache)

    payload = build_sync_response(temp_cache, since=0)
    assert payload["count"] == 2
    assert payload["more"] is False
    assert payload["epoch"]
    assert payload["cursor"] == payload["revision"]
    assert {row["id"] for row in payload["records"]} == {"1", "2"}


def test_sync_response_is_incremental(temp_cache: Cache) -> None:
    obs.record_observation(_listing("1"), local_cache=temp_cache)
    first = build_sync_response(temp_cache, since=0)

    nothing = build_sync_response(temp_cache, since=first["cursor"])
    assert nothing["count"] == 0
    assert nothing["cursor"] == first["cursor"]

    obs.record_observation(_listing("2"), local_cache=temp_cache)
    delta = build_sync_response(temp_cache, since=first["cursor"])
    assert [row["id"] for row in delta["records"]] == ["2"]


def test_sync_response_paginates(temp_cache: Cache) -> None:
    for index in range(5):
        obs.record_observation(_listing(str(index)), local_cache=temp_cache)

    page = build_sync_response(temp_cache, since=0, limit=2)
    assert page["count"] == 2
    assert page["more"] is True

    seen = list(page["records"])
    cursor = page["cursor"]
    while page["more"]:
        page = build_sync_response(temp_cache, since=cursor, limit=2)
        seen.extend(page["records"])
        cursor = page["cursor"]
    assert {row["id"] for row in seen} == {"0", "1", "2", "3", "4"}


def test_empty_store_reports_a_usable_cursor(temp_cache: Cache) -> None:
    payload = build_sync_response(temp_cache, since=0)
    assert payload["count"] == 0
    assert payload["revision"] == 0
    assert payload["cursor"] == 0
