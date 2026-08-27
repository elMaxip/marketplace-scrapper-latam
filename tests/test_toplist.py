"""The cheapest listing of a search, and when that is worth a message.

Everything runs against a real throwaway observation store, because the
question this module answers is a question *about the store*: which of these
records is the cheapest one that passed the filters.  A fake would only be
re-testing the fake.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from diskcache import Cache  # type: ignore

from ai_marketplace_monitor import observations as obs
from ai_marketplace_monitor import toplist
from ai_marketplace_monitor.listing import Listing


def _listing(
    listing_id: str,
    price: str,
    item: str = "ps5",
    marketplace: str = "facebook",
) -> Listing:
    return Listing(
        marketplace=marketplace,
        name=item,
        id=listing_id,
        title=f"PS5 {listing_id}",
        image="http://img/x.jpg",
        price=price,
        post_url=f"https://example.com/item/{listing_id}",
        location="Santiago",
        seller="Someone",
        condition="used_good",
        description="",
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Cache]:
    cache = Cache(str(tmp_path / "cache"))
    obs.reset_index_cache()
    yield cache
    obs.reset_index_cache()
    cache.close()


def see(
    store: Cache,
    listing_id: str,
    price: str,
    item: str = "ps5",
    matched: bool = True,
    marketplace: str = "facebook",
) -> None:
    obs.record_observation(
        _listing(listing_id, price, item, marketplace),
        matched=matched,
        item_name=item,
        local_cache=store,
    )


# --------------------------------------------------------------------------- #
# Finding the floor
# --------------------------------------------------------------------------- #


def test_the_cheapest_of_several(store: Cache) -> None:
    see(store, "a", "$500.000")
    see(store, "b", "$359.990")
    see(store, "c", "$420.000")
    top = toplist.current_top("ps5", local_cache=store)
    assert top is not None and top.id == "b"
    assert top.value == 359990


def test_a_search_with_nothing_stored(store: Cache) -> None:
    assert toplist.current_top("ps5", local_cache=store) is None


def test_listings_the_filters_rejected_are_not_the_floor(store: Cache) -> None:
    # This is what keeps a placeholder "0" from being the cheapest listing of
    # every search forever: junk prices are excluded by `check_listing`, which
    # means they arrive here already marked as not matched.
    see(store, "junk", "0", matched=False)
    see(store, "real", "$400.000")
    top = toplist.current_top("ps5", local_cache=store)
    assert top is not None and top.id == "real"


def test_deleted_listings_are_not_the_floor(store: Cache) -> None:
    see(store, "gone", "$100.000")
    see(store, "here", "$400.000")
    obs.delete_observations([("facebook", "gone")], local_cache=store)
    top = toplist.current_top("ps5", local_cache=store)
    assert top is not None and top.id == "here"


def test_another_search_does_not_count(store: Cache) -> None:
    see(store, "cheap-tv", "$90.000", item="tv")
    see(store, "ps5-one", "$400.000", item="ps5")
    top = toplist.current_top("ps5", local_cache=store)
    assert top is not None and top.id == "ps5-one"


def test_an_unreadable_price_is_skipped_not_treated_as_zero(store: Cache) -> None:
    see(store, "nada", "**unspecified**")
    see(store, "real", "$400.000")
    top = toplist.current_top("ps5", local_cache=store)
    assert top is not None and top.id == "real"


def test_a_search_whose_only_listing_has_no_price(store: Cache) -> None:
    see(store, "nada", "**unspecified**")
    assert toplist.current_top("ps5", local_cache=store) is None


def test_can_be_limited_to_certain_platforms(store: Cache) -> None:
    see(store, "ml-one", "$300.000", marketplace="mercadolibre")
    see(store, "fb-one", "$400.000", marketplace="facebook")
    top = toplist.current_top("ps5", marketplaces=["facebook"], local_cache=store)
    assert top is not None and top.id == "fb-one"


def test_ties_break_the_same_way_every_time(store: Cache) -> None:
    see(store, "bbb", "$400.000")
    see(store, "aaa", "$400.000")
    first = toplist.current_top("ps5", local_cache=store)
    second = toplist.current_top("ps5", local_cache=store)
    assert first is not None and second is not None
    assert first.id == second.id == "aaa"


def test_one_pass_answers_for_every_search(store: Cache) -> None:
    see(store, "ps5-a", "$400.000", item="ps5")
    see(store, "tv-a", "$90.000", item="tv")
    tops = toplist.current_tops(["ps5", "tv"], local_cache=store)
    assert set(tops) == {"ps5", "tv"}
    assert tops["ps5"].id == "ps5-a"
    assert tops["tv"].id == "tv-a"


def test_asking_about_no_searches(store: Cache) -> None:
    see(store, "a", "$1")
    assert toplist.current_tops([], local_cache=store) == {}


def test_a_listing_matching_two_searches_counts_for_both(store: Cache) -> None:
    # `items` accumulates across sightings: one listing really can be the
    # cheapest of two different searches.
    obs.record_observation(
        _listing("shared", "$100.000", item="ps5"), item_name="ps5", local_cache=store
    )
    obs.record_observation(
        _listing("shared", "$100.000", item="ps5"), item_name="consolas", local_cache=store
    )
    tops = toplist.current_tops(["ps5", "consolas"], local_cache=store)
    assert tops["ps5"].id == tops["consolas"].id == "shared"


# --------------------------------------------------------------------------- #
# A group of trackers competes as one
# --------------------------------------------------------------------------- #

SABANAS = {"sabana-falabella": "Sabanas", "sabana-paris": "Sabanas"}


def track(store: Cache, listing_id: str, price: str, item: str) -> None:
    """One followed page, stored the way `TrackedMarketplace.search` stores it."""
    see(store, listing_id, price, item=item, marketplace="tracked")


def test_a_group_of_trackers_has_one_cheapest(store: Cache) -> None:
    track(store, "a", "$16.590", "sabana-falabella")
    track(store, "b", "$9.000", "sabana-paris")
    tops = toplist.current_tops(list(SABANAS), local_cache=store, scope_of=SABANAS)
    assert list(tops) == ["Sabanas"]
    assert tops["Sabanas"].id == "b"
    assert tops["Sabanas"].value == 9000


def test_the_winner_is_still_its_own_listing(store: Cache) -> None:
    # A group decides who competes, never what is sent: the message has to be
    # about the page that won, not about the group as if it were one product.
    track(store, "a", "$16.590", "sabana-falabella")
    track(store, "b", "$9.000", "sabana-paris")
    top = toplist.current_tops(list(SABANAS), local_cache=store, scope_of=SABANAS)["Sabanas"]
    listing = top.as_listing()
    assert listing is not None
    assert listing.id == "b"
    assert listing.name == "sabana-paris"


def test_a_tracker_outside_every_group_keeps_its_own_top(store: Cache) -> None:
    track(store, "a", "$16.590", "sabana-falabella")
    track(store, "z", "$70.000", "notebook")
    tops = toplist.current_tops(
        [*SABANAS, "notebook"], local_cache=store, scope_of=SABANAS
    )
    assert tops["Sabanas"].id == "a"
    assert tops["notebook"].id == "z"


def test_a_search_is_untouched_by_the_grouping(store: Cache) -> None:
    see(store, "ps", "$400.000")
    track(store, "a", "$9.000", "sabana-falabella")
    tops = toplist.current_tops(["ps5", *SABANAS], local_cache=store, scope_of=SABANAS)
    assert tops["ps5"].id == "ps"
    assert tops["Sabanas"].id == "a"


def test_the_group_announces_once_and_then_only_on_a_fall(store: Cache) -> None:
    track(store, "a", "$16.590", "sabana-falabella")
    track(store, "b", "$12.000", "sabana-paris")
    first = toplist.new_tops(list(SABANAS), local_cache=store, scope_of=SABANAS)
    assert set(first) == {"Sabanas"}
    toplist.remember_top("Sabanas", first["Sabanas"], local_cache=store)

    assert toplist.new_tops(list(SABANAS), local_cache=store, scope_of=SABANAS) == {}

    # The *other* tracker falls below it: still news, because the cheapest of
    # the group went down.
    track(store, "a", "$9.000", "sabana-falabella")
    again = toplist.new_tops(list(SABANAS), local_cache=store, scope_of=SABANAS)
    assert again["Sabanas"].id == "a"


def test_a_dearer_member_is_not_news(store: Cache) -> None:
    # It was never the cheapest, so nothing about the group changed.
    track(store, "a", "$9.000", "sabana-falabella")
    first = toplist.new_tops(list(SABANAS), local_cache=store, scope_of=SABANAS)
    toplist.remember_top("Sabanas", first["Sabanas"], local_cache=store)

    track(store, "b", "$16.590", "sabana-paris")
    assert toplist.new_tops(list(SABANAS), local_cache=store, scope_of=SABANAS) == {}


def test_two_members_at_the_same_price_are_not_news(store: Cache) -> None:
    track(store, "a", "$9.000", "sabana-falabella")
    first = toplist.new_tops(list(SABANAS), local_cache=store, scope_of=SABANAS)
    toplist.remember_top("Sabanas", first["Sabanas"], local_cache=store)

    track(store, "b", "$9.000", "sabana-paris")
    assert toplist.new_tops(list(SABANAS), local_cache=store, scope_of=SABANAS) == {}


def test_no_scope_map_behaves_exactly_as_before(store: Cache) -> None:
    track(store, "a", "$16.590", "sabana-falabella")
    track(store, "b", "$9.000", "sabana-paris")
    tops = toplist.current_tops(list(SABANAS), local_cache=store)
    assert set(tops) == set(SABANAS)


# --------------------------------------------------------------------------- #
# When it is worth saying
# --------------------------------------------------------------------------- #


def test_the_first_top_is_always_announced(store: Cache) -> None:
    see(store, "a", "$400.000")
    assert toplist.new_top("ps5", local_cache=store) is not None


def test_the_same_top_is_not_announced_twice(store: Cache) -> None:
    see(store, "a", "$400.000")
    top = toplist.new_top("ps5", local_cache=store)
    assert top is not None
    toplist.remember_top("ps5", top, local_cache=store)
    assert toplist.new_top("ps5", local_cache=store) is None


def test_a_lower_price_is_announced(store: Cache) -> None:
    see(store, "a", "$400.000")
    top = toplist.new_top("ps5", local_cache=store)
    assert top is not None
    toplist.remember_top("ps5", top, local_cache=store)

    see(store, "b", "$350.000")
    fresh = toplist.new_top("ps5", local_cache=store)
    assert fresh is not None and fresh.id == "b"


def test_a_higher_price_is_not_announced(store: Cache) -> None:
    # The record holder sold and the next one up inherited the title.  True,
    # and not news anybody asked for.
    see(store, "cheap", "$300.000")
    top = toplist.new_top("ps5", local_cache=store)
    assert top is not None
    toplist.remember_top("ps5", top, local_cache=store)

    obs.delete_observations([("facebook", "cheap")], local_cache=store)
    see(store, "dearer", "$450.000")
    assert toplist.new_top("ps5", local_cache=store) is None


def test_a_different_listing_at_the_same_price_is_not_news(store: Cache) -> None:
    see(store, "a", "$400.000")
    top = toplist.new_top("ps5", local_cache=store)
    assert top is not None
    toplist.remember_top("ps5", top, local_cache=store)

    see(store, "b", "$400.000")
    assert toplist.new_top("ps5", local_cache=store) is None


def test_the_same_listing_getting_cheaper_is_news(store: Cache) -> None:
    see(store, "a", "$400.000")
    top = toplist.new_top("ps5", local_cache=store)
    assert top is not None
    toplist.remember_top("ps5", top, local_cache=store)

    see(store, "a", "$300.000")
    fresh = toplist.new_top("ps5", local_cache=store)
    assert fresh is not None and fresh.value == 300000


def test_a_corrupt_record_reopens_the_question(store: Cache) -> None:
    # Erring towards one extra message rather than towards permanent silence:
    # a bad record must not be able to switch the feature off for good.
    see(store, "a", "$400.000")
    store.set(toplist.top_key("ps5"), {"price": "nonsense", "value": "nonsense"})
    assert toplist.new_top("ps5", local_cache=store) is not None


def test_forgetting_a_search(store: Cache) -> None:
    see(store, "a", "$400.000")
    top = toplist.new_top("ps5", local_cache=store)
    assert top is not None
    toplist.remember_top("ps5", top, local_cache=store)
    toplist.forget_top("ps5", local_cache=store)
    assert toplist.stored_top("ps5", local_cache=store) is None
    assert toplist.new_top("ps5", local_cache=store) is not None


# --------------------------------------------------------------------------- #
# Turning it back into something sendable
# --------------------------------------------------------------------------- #


def test_the_snapshot_becomes_a_listing_again(store: Cache) -> None:
    see(store, "a", "$400.000")
    top = toplist.current_top("ps5", local_cache=store)
    assert top is not None
    listing = top.as_listing()
    assert listing is not None
    assert listing.id == "a"
    assert listing.post_url == "https://example.com/item/a"


def test_a_snapshot_from_another_version_is_not_announced(store: Cache) -> None:
    # A field added or removed since the record was written.  Ordinary, and not
    # worth an exception on the notification path.
    top = toplist.TopListing(
        marketplace="facebook", id="a", price="$1", value=1.0, snapshot={"nonsense": 1}
    )
    assert top.as_listing() is None


def test_an_empty_snapshot_is_not_announced() -> None:
    top = toplist.TopListing(marketplace="facebook", id="a", price="$1", value=1.0, snapshot={})
    assert top.as_listing() is None


def test_the_ai_verdict_travels_with_the_top(store: Cache) -> None:
    see(store, "a", "$400.000")
    obs.record_rating(
        _listing("a", "$400.000"), score=5, comment="great", conclusion="Great deal",
        local_cache=store,
    )
    top = toplist.current_top("ps5", local_cache=store)
    assert top is not None
    assert top.rating.get("score") == 5
