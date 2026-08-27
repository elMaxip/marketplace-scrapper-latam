"""Tests for re-visiting listings the monitor has already stored."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
from diskcache import Cache  # type: ignore

from ai_marketplace_monitor import control
from ai_marketplace_monitor import observations as obs
from ai_marketplace_monitor.listing import Listing
from ai_marketplace_monitor.marketplace import ItemConfig, ListingStatus
from ai_marketplace_monitor.refresh import (
    ListingRefresher,
    _interleave,
    is_cheaper,
    stale_records,
    was_checked_recently,
)


def _listing(listing_id: str = "1", price: str = "$100 000", title: str = "PS5") -> Listing:
    return Listing(
        marketplace="facebook",
        name="ps5",
        id=listing_id,
        title=title,
        image="",
        price=price,
        post_url=f"https://www.facebook.com/marketplace/item/{listing_id}/",
        location="Nunoa, RM",
        seller="Ana",
        condition="used_good",
        description="works",
    )


@pytest.fixture
def temp_cache(tmp_path: Path) -> Iterator[Cache]:
    cache = Cache(str(tmp_path / "cache"))
    obs.reset_index_cache()
    control.reset_for_tests()
    yield cache
    control.reset_for_tests()
    obs.reset_index_cache()
    cache.close()


def _age(record_key: Tuple[str, str], cache: Cache, minutes: int) -> None:
    """Backdate a record's ``last_seen`` so it reads as overdue."""
    key = obs.observation_key(record_key[0], record_key[1])
    record = cache.get(key)
    record["last_seen"] = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes)
    ).isoformat(timespec="seconds")
    cache.set(key, record, tag=obs.OBSERVATION_TAG)


class FakeMarketplace:
    """A marketplace that answers with whatever the test told it to."""

    def __init__(
        self,
        status: ListingStatus = ListingStatus.ACTIVE,
        details: Optional[Listing] = None,
        error: Optional[Exception] = None,
        matched: bool = True,
    ) -> None:
        self.status = status
        self.details = details
        self.error = error
        self.matched = matched
        self.visited: List[str] = []

    def recheck_listing(
        self, post_url: str, item_config: ItemConfig
    ) -> Tuple[ListingStatus, Optional[Listing]]:
        self.visited.append(post_url)
        if self.error is not None:
            raise self.error
        return self.status, self.details

    def check_listing(self, listing: Listing, item_config: ItemConfig) -> bool:
        return self.matched


def _refresher(cache: Cache, marketplace: Any, **kwargs: Any) -> ListingRefresher:
    refresher = ListingRefresher(
        marketplace_for=lambda name: marketplace,
        item_config_for=lambda name, item: ItemConfig(name=item or "ps5", search_phrases=["ps5"]),
        local_cache=cache,
        recheck_interval=kwargs.pop("recheck_interval", 3600),
        **kwargs,
    )
    # The tests are about what a slice does, not about how it is paced.
    refresher.listing_interval = 0
    refresher.slice_interval = 0
    return refresher


# --------------------------------------------------------------------------- #
# Choosing what to re-check
# --------------------------------------------------------------------------- #


def test_a_freshly_seen_listing_is_not_due(temp_cache: Cache) -> None:
    obs.record_observation(_listing(), item_name="ps5", local_cache=temp_cache)
    assert stale_records(temp_cache, within=3600, marketplaces=("facebook",), limit=10) == []


def test_an_old_listing_is_due(temp_cache: Cache) -> None:
    obs.record_observation(_listing(), item_name="ps5", local_cache=temp_cache)
    _age(("facebook", "1"), temp_cache, minutes=120)
    due = stale_records(temp_cache, within=3600, marketplaces=("facebook",), limit=10)
    assert [record["id"] for record in due] == ["1"]


def test_the_most_overdue_comes_first(temp_cache: Cache) -> None:
    for listing_id, minutes in (("1", 120), ("2", 600), ("3", 300)):
        obs.record_observation(_listing(listing_id), item_name="ps5", local_cache=temp_cache)
        _age(("facebook", listing_id), temp_cache, minutes=minutes)
    due = stale_records(temp_cache, within=3600, marketplaces=("facebook",), limit=10)
    assert [record["id"] for record in due] == ["2", "3", "1"]


def test_a_deleted_listing_is_never_due(temp_cache: Cache) -> None:
    obs.record_observation(_listing(), item_name="ps5", local_cache=temp_cache)
    _age(("facebook", "1"), temp_cache, minutes=600)
    obs.delete_observations([("facebook", "1")], local_cache=temp_cache)
    assert stale_records(temp_cache, within=3600, marketplaces=("facebook",), limit=10) == []


def test_a_marketplace_that_is_gone_is_left_alone(temp_cache: Cache) -> None:
    """Its platform is no longer configured, so nothing can read the page. The
    listing stays exactly as it is rather than being dropped."""
    obs.record_observation(_listing(), item_name="ps5", local_cache=temp_cache)
    _age(("facebook", "1"), temp_cache, minutes=600)
    assert stale_records(temp_cache, within=3600, marketplaces=("mercadolibre",), limit=10) == []


def test_was_checked_recently_reads_last_seen(temp_cache: Cache) -> None:
    obs.record_observation(_listing(), item_name="ps5", local_cache=temp_cache)
    assert was_checked_recently("facebook", "1", within=3600, local_cache=temp_cache) is True
    _age(("facebook", "1"), temp_cache, minutes=120)
    assert was_checked_recently("facebook", "1", within=3600, local_cache=temp_cache) is False


def test_a_listing_never_seen_was_not_checked_recently(temp_cache: Cache) -> None:
    assert was_checked_recently("facebook", "nope", within=3600, local_cache=temp_cache) is False


# --------------------------------------------------------------------------- #
# What a slice does
# --------------------------------------------------------------------------- #


def test_an_active_listing_is_updated_and_its_price_recorded(temp_cache: Cache) -> None:
    obs.record_observation(_listing(price="$100 000"), item_name="ps5", local_cache=temp_cache)
    _age(("facebook", "1"), temp_cache, minutes=600)

    marketplace = FakeMarketplace(ListingStatus.ACTIVE, details=_listing(price="$80 000"))
    report = _refresher(temp_cache, marketplace).run_slice(("facebook",))

    assert (report.checked, report.updated, report.removed) == (1, 1, 0)
    record = obs.get_observation("facebook", "1", local_cache=temp_cache)
    assert record["listing"]["price"] == "$80 000"
    assert [point["price"] for point in record["price_points"]] == ["$100 000", "$80 000"]


def test_a_sold_listing_is_removed_for_good(temp_cache: Cache) -> None:
    obs.record_observation(_listing(), item_name="ps5", local_cache=temp_cache)
    _age(("facebook", "1"), temp_cache, minutes=600)

    report = _refresher(temp_cache, FakeMarketplace(ListingStatus.SOLD)).run_slice(("facebook",))

    assert (report.removed, report.updated) == (1, 0)
    record = obs.get_observation("facebook", "1", local_cache=temp_cache)
    assert obs.is_deleted(record)
    # The tombstone is what keeps the next search from putting it straight back.
    obs.record_observation(_listing(), item_name="ps5", local_cache=temp_cache)
    assert obs.is_deleted(obs.get_observation("facebook", "1", local_cache=temp_cache))


def test_a_dead_link_is_removed(temp_cache: Cache) -> None:
    obs.record_observation(_listing(), item_name="ps5", local_cache=temp_cache)
    _age(("facebook", "1"), temp_cache, minutes=600)
    report = _refresher(temp_cache, FakeMarketplace(ListingStatus.GONE)).run_slice(("facebook",))
    assert report.removed == 1


@pytest.mark.parametrize(
    "marketplace",
    [
        FakeMarketplace(ListingStatus.UNKNOWN),
        FakeMarketplace(error=TimeoutError("navigation timed out")),
        FakeMarketplace(error=RuntimeError("net::ERR_CONNECTION_RESET")),
    ],
    ids=["undecided", "timeout", "network error"],
)
def test_an_unclear_failure_never_deletes(temp_cache: Cache, marketplace: FakeMarketplace) -> None:
    """A timeout, a dropped connection or an unreadable page all look the same
    from here, and none of them says the listing is gone."""
    obs.record_observation(_listing(), item_name="ps5", local_cache=temp_cache)
    _age(("facebook", "1"), temp_cache, minutes=600)

    report = _refresher(temp_cache, marketplace).run_slice(("facebook",))

    assert (report.removed, report.failed) == (0, 1)
    record = obs.get_observation("facebook", "1", local_cache=temp_cache)
    assert not obs.is_deleted(record)
    assert record["listing"]["price"] == "$100 000"


def test_a_failed_listing_is_left_alone_for_a_while(temp_cache: Cache) -> None:
    """Otherwise a listing that always fails would be retried on every slice,
    which is the hot loop this whole feature exists to avoid."""
    obs.record_observation(_listing(), item_name="ps5", local_cache=temp_cache)
    _age(("facebook", "1"), temp_cache, minutes=600)

    marketplace = FakeMarketplace(error=TimeoutError("nope"))
    refresher = _refresher(temp_cache, marketplace)
    refresher.run_slice(("facebook",))
    refresher.run_slice(("facebook",))

    assert len(marketplace.visited) == 1


def test_a_claimed_listing_is_skipped(temp_cache: Cache) -> None:
    """The search flow is reading this very page; opening it twice would be
    duplicated traffic and a lost update."""
    obs.record_observation(_listing(), item_name="ps5", local_cache=temp_cache)
    _age(("facebook", "1"), temp_cache, minutes=600)

    marketplace = FakeMarketplace(ListingStatus.ACTIVE, details=_listing(price="$80 000"))
    with control.claim("facebook", "1"):
        report = _refresher(temp_cache, marketplace).run_slice(("facebook",))

    assert (report.skipped, report.checked) == (1, 0)
    assert marketplace.visited == []


def _orphan_refresher(cache: Cache, marketplace: Any, **kwargs: Any) -> ListingRefresher:
    """A refresher for which no listing's search still exists in the config."""
    refresher = ListingRefresher(
        marketplace_for=lambda name: marketplace,
        item_config_for=lambda name, item: None,
        local_cache=cache,
        recheck_interval=kwargs.pop("recheck_interval", 3600),
        **kwargs,
    )
    refresher.listing_interval = 0
    refresher.slice_interval = 0
    return refresher


def test_a_listing_from_a_deleted_search_is_still_re_checked(temp_cache: Cache) -> None:
    """It is still a listing the user is looking at, and re-checking it needs
    nothing from the search: the price is on the page and so is the sold badge."""
    obs.record_observation(_listing(price="$100 000"), item_name="ps5", local_cache=temp_cache)
    _age(("facebook", "1"), temp_cache, minutes=600)

    marketplace = FakeMarketplace(ListingStatus.ACTIVE, details=_listing(price="$80 000"))
    report = _orphan_refresher(temp_cache, marketplace).run_slice(("facebook",))

    assert (report.checked, report.updated, report.skipped) == (1, 1, 0)
    record = obs.get_observation("facebook", "1", local_cache=temp_cache)
    assert record["listing"]["price"] == "$80 000"


def test_a_listing_from_a_deleted_search_is_still_removed_when_sold(temp_cache: Cache) -> None:
    obs.record_observation(_listing(), item_name="ps5", local_cache=temp_cache)
    _age(("facebook", "1"), temp_cache, minutes=600)

    report = _orphan_refresher(temp_cache, FakeMarketplace(ListingStatus.SOLD)).run_slice(
        ("facebook",)
    )
    assert report.removed == 1


def test_a_listing_from_a_deleted_search_keeps_its_verdict(temp_cache: Cache) -> None:
    """Its filters are gone; re-judging it with some other product's would be a
    verdict the user never asked for."""
    obs.record_observation(_listing(), matched=False, item_name="ps5", local_cache=temp_cache)
    _age(("facebook", "1"), temp_cache, minutes=600)

    marketplace = FakeMarketplace(
        ListingStatus.ACTIVE, details=_listing(price="$80 000"), matched=True
    )
    _orphan_refresher(temp_cache, marketplace).run_slice(("facebook",))

    record = obs.get_observation("facebook", "1", local_cache=temp_cache)
    assert record["matched"] is False


def test_a_configured_search_is_re_checked_before_an_orphan(temp_cache: Cache) -> None:
    """The regression that started this: a backlog from a deleted search is the
    oldest thing in the store, precisely because nothing has touched it, so
    oldest-first alone would let it hold up the search actually running."""
    for listing_id, item, minutes in (("1", "ps5", 6000), ("2", "switch", 120)):
        listing = _listing(listing_id)
        listing.name = item
        obs.record_observation(listing, item_name=item, local_cache=temp_cache)
        _age(("facebook", listing_id), temp_cache, minutes=minutes)

    marketplace = FakeMarketplace(ListingStatus.ACTIVE, details=_listing(price="$90 000"))
    refresher = ListingRefresher(
        marketplace_for=lambda name: marketplace,
        item_config_for=lambda name, item: (
            ItemConfig(name=item, search_phrases=[item]) if item == "switch" else None
        ),
        local_cache=temp_cache,
        recheck_interval=3600,
    )
    refresher.listing_interval = 0
    refresher.slice_interval = 0

    assert [record["id"] for record in refresher.due(("facebook",), limit=2)] == ["2", "1"]


def test_a_skip_does_not_consume_the_slice(temp_cache: Cache) -> None:
    """A skip is not work.  Letting one eat a slot is how a handful of
    unskippable listings at the front stall the queue for good."""
    for listing_id in ("1", "2", "3"):
        obs.record_observation(_listing(listing_id), item_name="ps5", local_cache=temp_cache)
        _age(("facebook", listing_id), temp_cache, minutes=600)
    # Nothing can be read for the first two: no URL to open.
    for listing_id in ("1", "2"):
        key = obs.observation_key("facebook", listing_id)
        record = temp_cache.get(key)
        record["listing"]["post_url"] = ""
        temp_cache.set(key, record, tag=obs.OBSERVATION_TAG)

    marketplace = FakeMarketplace(ListingStatus.ACTIVE, details=_listing(price="$90 000"))
    report = _refresher(temp_cache, marketplace).run_slice(("facebook",), limit=1)

    assert report.checked == 1
    assert report.skipped == 2
    assert report.skips == {"with no URL stored": 2}


def test_the_report_says_why_it_skipped(temp_cache: Cache) -> None:
    obs.record_observation(_listing(), item_name="ps5", local_cache=temp_cache)
    _age(("facebook", "1"), temp_cache, minutes=600)

    refresher = ListingRefresher(
        marketplace_for=lambda name: None,
        item_config_for=lambda name, item: None,
        local_cache=temp_cache,
        recheck_interval=3600,
    )
    refresher.slice_interval = 0
    report = refresher.run_slice(("facebook",))

    assert report.checked == 0
    assert "unconfigured marketplace" in report.why_skipped()


def test_slices_are_paced(temp_cache: Cache) -> None:
    """Two slices back to back must not double the traffic: the second one does
    nothing until the interval has passed."""
    for listing_id in ("1", "2"):
        obs.record_observation(_listing(listing_id), item_name="ps5", local_cache=temp_cache)
        _age(("facebook", listing_id), temp_cache, minutes=600)

    marketplace = FakeMarketplace(ListingStatus.ACTIVE, details=_listing(price="$90 000"))
    refresher = _refresher(temp_cache, marketplace)
    refresher.slice_interval = 600

    assert refresher.run_slice(("facebook",), limit=1).checked == 1
    assert refresher.run_slice(("facebook",), limit=1).checked == 0


def test_a_slice_stops_when_asked(temp_cache: Cache) -> None:
    for listing_id in ("1", "2", "3"):
        obs.record_observation(_listing(listing_id), item_name="ps5", local_cache=temp_cache)
        _age(("facebook", listing_id), temp_cache, minutes=600)

    marketplace = FakeMarketplace(ListingStatus.ACTIVE, details=_listing(price="$90 000"))
    refresher = _refresher(temp_cache, marketplace, stop_when=lambda: True)
    assert refresher.run_slice(("facebook",)).checked == 0


# --------------------------------------------------------------------------- #
# Saying that something got cheaper
# --------------------------------------------------------------------------- #
#
# A re-check is the only place a price moves -- the search never opens a listing
# it already knows -- so a fall that does not leave this module is a fall
# nobody will ever hear about.  Reported rather than announced: who to tell and
# whether they asked to hear it belong to the monitor.


def test_a_fall_is_reported(temp_cache: Cache) -> None:
    obs.record_observation(_listing(price="$100 000"), item_name="ps5", local_cache=temp_cache)
    _age(("facebook", "1"), temp_cache, minutes=600)

    marketplace = FakeMarketplace(ListingStatus.ACTIVE, details=_listing(price="$80 000"))
    report = _refresher(temp_cache, marketplace).run_slice(("facebook",))

    assert len(report.drops) == 1
    assert (report.drops[0].previous, report.drops[0].listing.price) == ("$100 000", "$80 000")
    assert report.drops[0].item_name == "ps5"


def test_a_rise_is_not_a_fall(temp_cache: Cache) -> None:
    obs.record_observation(_listing(price="$100 000"), item_name="ps5", local_cache=temp_cache)
    _age(("facebook", "1"), temp_cache, minutes=600)

    marketplace = FakeMarketplace(ListingStatus.ACTIVE, details=_listing(price="$120 000"))
    assert _refresher(temp_cache, marketplace).run_slice(("facebook",)).drops == []


def test_the_same_amount_written_differently_is_not_a_fall(temp_cache: Cache) -> None:
    # Prices are stored exactly as the site printed them, so a site that changes
    # its formatting would otherwise announce a bargain on every listing it has.
    obs.record_observation(_listing(price="$100.000"), item_name="ps5", local_cache=temp_cache)
    _age(("facebook", "1"), temp_cache, minutes=600)

    marketplace = FakeMarketplace(ListingStatus.ACTIVE, details=_listing(price="100000"))
    assert _refresher(temp_cache, marketplace).run_slice(("facebook",)).drops == []


def test_a_rejected_listing_does_not_announce_a_bargain(temp_cache: Cache) -> None:
    # A listing the filters throw away is not a bargain; it is a rejection that
    # happens to cost less.
    obs.record_observation(_listing(price="$100 000"), item_name="ps5", local_cache=temp_cache)
    _age(("facebook", "1"), temp_cache, minutes=600)

    marketplace = FakeMarketplace(
        ListingStatus.ACTIVE, details=_listing(price="$80 000"), matched=False
    )
    assert _refresher(temp_cache, marketplace).run_slice(("facebook",)).drops == []


def test_a_price_that_cannot_be_read_is_not_a_fall() -> None:
    # Not cheaper, not dearer: unknown.  A page that stops printing a price is
    # the normal way this happens, and it must not read as free.
    assert is_cheaper("$100 000", "") is False
    assert is_cheaper("", "$80 000") is False
    assert is_cheaper("$100 000", "**unspecified**") is False
    assert is_cheaper("$100 000", "$80 000") is True


def test_the_filters_verdict_is_recorded(temp_cache: Cache) -> None:
    obs.record_observation(_listing(), item_name="ps5", local_cache=temp_cache)
    _age(("facebook", "1"), temp_cache, minutes=600)

    marketplace = FakeMarketplace(
        ListingStatus.ACTIVE, details=_listing(price="$90 000"), matched=False
    )
    _refresher(temp_cache, marketplace).run_slice(("facebook",))

    record: Dict[str, Any] = obs.get_observation("facebook", "1", local_cache=temp_cache)
    assert record["matched"] is False


# --------------------------------------------------------------------------- #
# Spreading the load
# --------------------------------------------------------------------------- #


def test_a_slice_alternates_between_marketplaces() -> None:
    """A run of page loads at one site is what makes it start asking who we
    are; the same work dealt out alternately halves the rate each site sees."""
    records = [
        {"marketplace": "facebook", "id": "f1"},
        {"marketplace": "facebook", "id": "f2"},
        {"marketplace": "facebook", "id": "f3"},
        {"marketplace": "mercadolibre", "id": "m1"},
        {"marketplace": "mercadolibre", "id": "m2"},
    ]
    assert [r["id"] for r in _interleave(records)] == ["f1", "m1", "f2", "m2", "f3"]


def test_interleaving_keeps_each_marketplace_in_order() -> None:
    records = [
        {"marketplace": "mercadolibre", "id": "m1"},
        {"marketplace": "facebook", "id": "f1"},
        {"marketplace": "mercadolibre", "id": "m2"},
    ]
    dealt = [r["id"] for r in _interleave(records)]
    assert dealt.index("m1") < dealt.index("m2")


def test_one_marketplace_is_left_exactly_as_it_was() -> None:
    records = [{"marketplace": "facebook", "id": str(n)} for n in range(4)]
    assert _interleave(records) == records
