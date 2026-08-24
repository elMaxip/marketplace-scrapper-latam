"""Tests for the per-listing observation log."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from diskcache import Cache  # type: ignore

from ai_marketplace_monitor import observations as obs
from ai_marketplace_monitor.listing import Listing


def _listing(listing_id: str = "123", price: str = "$100", title: str = "iPhone 13") -> Listing:
    return Listing(
        marketplace="facebook",
        name="iphone",
        id=listing_id,
        title=title,
        image="http://img/x.jpg?sig=abc",
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
    # The revision index is process-global and keyed by cache directory; clear it
    # so a test never inherits another test's throwaway cache.
    obs.reset_index_cache()
    yield cache
    obs.reset_index_cache()
    cache.close()


def test_first_sighting_has_no_history(temp_cache: Cache) -> None:
    record = obs.record_observation(_listing(), item_name="iphone", local_cache=temp_cache)
    assert record is not None
    assert record["seen_count"] == 1
    assert record["first_seen"] == record["last_seen"]
    # The initial snapshot is the origin, not a change.
    assert record["history"] == []
    assert record["price_points"] == [{"ts": record["first_seen"], "price": "$100"}]
    assert record["items"] == ["iphone"]


def test_repeat_sighting_records_only_what_changed(temp_cache: Cache) -> None:
    obs.record_observation(_listing(price="$100"), local_cache=temp_cache)
    record = obs.record_observation(_listing(price="$90"), local_cache=temp_cache)

    assert record is not None
    assert record["seen_count"] == 2
    assert len(record["history"]) == 1
    assert record["history"][0]["changes"] == {"price": {"from": "$100", "to": "$90"}}
    assert [point["price"] for point in record["price_points"]] == ["$100", "$90"]


def test_unchanged_sighting_adds_no_history(temp_cache: Cache) -> None:
    obs.record_observation(_listing(), local_cache=temp_cache)
    record = obs.record_observation(_listing(), local_cache=temp_cache)

    assert record is not None
    assert record["seen_count"] == 2
    assert record["history"] == []


def test_image_query_string_churn_is_not_a_change(temp_cache: Cache) -> None:
    """Facebook image URLs carry an expiring signature; it must not read as news."""
    first = _listing()
    first.image = "http://img/x.jpg?sig=one"
    obs.record_observation(first, local_cache=temp_cache)

    second = _listing()
    second.image = "http://img/x.jpg?sig=two"
    record = obs.record_observation(second, local_cache=temp_cache)

    assert record is not None
    assert record["history"] == []
    # The newest URL is still what gets stored.
    assert record["listing"]["image"] == "http://img/x.jpg?sig=two"


def test_items_accumulate_across_searches(temp_cache: Cache) -> None:
    obs.record_observation(_listing(), item_name="iphone", local_cache=temp_cache)
    record = obs.record_observation(_listing(), item_name="phones", local_cache=temp_cache)

    assert record is not None
    assert record["items"] == ["iphone", "phones"]


def test_matched_flag_tracks_the_latest_verdict(temp_cache: Cache) -> None:
    obs.record_observation(_listing(), matched=True, local_cache=temp_cache)
    record = obs.record_observation(_listing(), matched=False, local_cache=temp_cache)

    assert record is not None
    assert record["matched"] is False


def test_rating_is_recorded_once(temp_cache: Cache) -> None:
    listing = _listing()
    obs.record_observation(listing, local_cache=temp_cache)
    first = obs.record_rating(
        listing, score=4, comment="ok", conclusion="Good", local_cache=temp_cache
    )
    assert first is not None
    revision_after_rating = first["rev"]

    # An identical verdict must not burn a revision: AI results are themselves
    # cached, so re-evaluating would otherwise force clients to resync forever.
    repeat = obs.record_rating(
        listing, score=4, comment="ok", conclusion="Good", local_cache=temp_cache
    )
    assert repeat is not None
    assert repeat["rev"] == revision_after_rating

    changed = obs.record_rating(
        listing, score=5, comment="ok", conclusion="Good", local_cache=temp_cache
    )
    assert changed is not None
    assert changed["rev"] > revision_after_rating
    assert changed["rating"]["score"] == 5


def test_notification_is_recorded_once_per_user(temp_cache: Cache) -> None:
    listing = _listing()
    obs.record_observation(listing, local_cache=temp_cache)
    obs.record_notification(listing, "me", local_cache=temp_cache)
    before = obs.get_observation("facebook", "123", local_cache=temp_cache)
    assert before is not None

    obs.record_notification(listing, "me", local_cache=temp_cache)
    after = obs.get_observation("facebook", "123", local_cache=temp_cache)
    assert after is not None
    assert after["rev"] == before["rev"]
    assert list(after["notified"]) == ["me"]

    obs.record_notification(listing, "other", local_cache=temp_cache)
    final = obs.get_observation("facebook", "123", local_cache=temp_cache)
    assert final is not None
    assert sorted(final["notified"]) == ["me", "other"]


def test_history_is_capped(temp_cache: Cache) -> None:
    for step in range(obs.MAX_HISTORY + 20):
        obs.record_observation(_listing(price=f"${step}"), local_cache=temp_cache)
    record = obs.get_observation("facebook", "123", local_cache=temp_cache)
    assert record is not None
    assert len(record["history"]) == obs.MAX_HISTORY
    # The cap drops the oldest entries, so the newest change survives.
    assert record["history"][-1]["changes"]["price"]["to"] == f"${obs.MAX_HISTORY + 19}"


def test_observations_since_returns_only_newer_records(temp_cache: Cache) -> None:
    obs.record_observation(_listing("1"), local_cache=temp_cache)
    obs.record_observation(_listing("2"), local_cache=temp_cache)

    records, cursor, more = obs.observations_since(since=0, local_cache=temp_cache)
    assert {record["id"] for record in records} == {"1", "2"}
    assert more is False

    empty, next_cursor, _ = obs.observations_since(since=cursor, local_cache=temp_cache)
    assert empty == []
    assert next_cursor == cursor

    obs.record_observation(_listing("3"), local_cache=temp_cache)
    delta, _, _ = obs.observations_since(since=cursor, local_cache=temp_cache)
    assert [record["id"] for record in delta] == ["3"]


def test_observations_since_paginates(temp_cache: Cache) -> None:
    for index in range(5):
        obs.record_observation(_listing(str(index)), local_cache=temp_cache)

    page, cursor, more = obs.observations_since(since=0, limit=2, local_cache=temp_cache)
    assert len(page) == 2
    assert more is True

    rest: list = []
    while more:
        page, cursor, more = obs.observations_since(since=cursor, limit=2, local_cache=temp_cache)
        rest.extend(page)
    assert len(rest) == 3


def test_index_rebuilds_from_a_cold_start(temp_cache: Cache) -> None:
    obs.record_observation(_listing("1"), local_cache=temp_cache)
    obs.record_observation(_listing("2"), local_cache=temp_cache)

    # Simulate a fresh process: the in-memory index is gone but the cache is not.
    obs.reset_index_cache()
    records, _, _ = obs.observations_since(since=0, local_cache=temp_cache)
    assert {record["id"] for record in records} == {"1", "2"}


def test_epoch_is_stable_and_non_empty(temp_cache: Cache) -> None:
    first = obs.store_epoch(temp_cache)
    assert first
    assert obs.store_epoch(temp_cache) == first


def test_listing_without_identity_is_skipped(temp_cache: Cache) -> None:
    listing = _listing()
    listing.id = ""
    assert obs.record_observation(listing, local_cache=temp_cache) is None
    assert obs.current_revision(temp_cache) == 0


# --------------------------------------------------------------------------- #
# Deleting
# --------------------------------------------------------------------------- #


def test_delete_replaces_the_record_with_a_tombstone(temp_cache: Cache) -> None:
    obs.record_observation(_listing("1"), local_cache=temp_cache)
    deleted, revision = obs.delete_observations([("facebook", "1")], local_cache=temp_cache)

    assert deleted == 1
    record = obs.get_observation("facebook", "1", local_cache=temp_cache)
    assert obs.is_deleted(record)
    assert record is not None
    # The tombstone carries its own revision, which is what lets a client that
    # already synced the listing learn that it is gone.
    assert record["rev"] == revision
    assert "listing" not in record


def test_deleting_twice_burns_no_revision(temp_cache: Cache) -> None:
    obs.record_observation(_listing("1"), local_cache=temp_cache)
    obs.delete_observations([("facebook", "1")], local_cache=temp_cache)
    before = obs.current_revision(temp_cache)

    deleted, revision = obs.delete_observations([("facebook", "1")], local_cache=temp_cache)
    assert deleted == 0
    assert revision == before


def test_delete_ignores_listings_that_were_never_seen(temp_cache: Cache) -> None:
    deleted, _ = obs.delete_observations(
        [("facebook", "nope"), ("", "x"), ("facebook", "")], local_cache=temp_cache
    )
    assert deleted == 0
    assert obs.current_revision(temp_cache) == 0


def test_a_deleted_listing_is_not_recorded_again(temp_cache: Cache) -> None:
    """The scraper has no memory of what the user threw away; the store does."""
    obs.record_observation(_listing("1"), local_cache=temp_cache)
    obs.delete_observations([("facebook", "1")], local_cache=temp_cache)

    assert obs.record_observation(_listing("1"), local_cache=temp_cache) is None
    assert obs.record_rating(_listing("1"), score=5, local_cache=temp_cache) is None
    assert obs.is_deleted(obs.get_observation("facebook", "1", local_cache=temp_cache))


def test_deletion_reaches_a_synced_client(temp_cache: Cache) -> None:
    obs.record_observation(_listing("1"), local_cache=temp_cache)
    obs.record_observation(_listing("2"), local_cache=temp_cache)
    _, cursor, _ = obs.observations_since(since=0, local_cache=temp_cache)

    obs.delete_observations([("facebook", "1")], local_cache=temp_cache)
    delta, _, _ = obs.observations_since(since=cursor, local_cache=temp_cache)

    assert [record["id"] for record in delta] == ["1"]
    assert obs.is_deleted(delta[0])
