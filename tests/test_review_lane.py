"""Reviewing stored listings while the search carries on.

The behaviour that changed: a review used to stop the searching.  The loop
reached a gap between two searches, spent it re-reading stored listings, and
only then went on to the next search -- so "revisar" and "buscar" were two
things the one browser did in turn.

Now, with the setting on, the review has a browser and a thread of its own and
the two happen at the same time.  What these tests are about is that the two
flows do not end up doing each other's work:

* a listing being read right now is claimed, and the other flow skips it rather
  than waiting -- both would be doing the same fetch;
* a listing the search has just fetched is not stale, so it is not in the queue
  the review builds; freshness is decided from ``last_seen``, which both flows
  write, and not from a second timestamp meaning the same thing;
* a platform that has refused either flow is on a cooldown both read, so the
  second browser does not go on knocking at a door the first one found shut.

No browser is opened: the marketplace is a stub that records which listings it
was asked for.
"""

from __future__ import annotations

import logging
import threading
from typing import Iterator, List, Tuple

import pytest
from diskcache import Cache

from ai_marketplace_monitor import control
from ai_marketplace_monitor.listing import Listing
from ai_marketplace_monitor.marketplace import ListingStatus
from ai_marketplace_monitor.observations import (
    delete_observations,
    is_known,
    record_observation,
    reset_index_cache,
)
from ai_marketplace_monitor.refresh import (
    DEFAULT_RECHECK_INTERVAL,
    ListingRefresher,
    stale_records,
    was_checked_recently,
)


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    control.reset_for_tests()
    reset_index_cache()
    yield
    control.reset_for_tests()
    reset_index_cache()


@pytest.fixture
def store(tmp_path) -> Iterator[Cache]:
    cache = Cache(str(tmp_path / "cache"))
    yield cache
    cache.close()


def listing(listing_id: str, marketplace: str = "facebook", price: str = "$100") -> Listing:
    return Listing(
        marketplace=marketplace,
        name="ps5",
        id=listing_id,
        title=f"listing {listing_id}",
        image="",
        price=price,
        post_url=f"https://example.test/item/{listing_id}",
        location="houston, tx",
        seller="someone",
        condition="Used",
        description="something",
    )


class StubMarketplace:
    """Records what it was asked to re-read, and answers instantly."""

    def __init__(self, name: str = "facebook") -> None:
        self.name = name
        self.seen: List[str] = []
        self._lock = threading.Lock()

    def recheck_listing(self, post_url: str, item_config) -> Tuple[ListingStatus, Listing]:
        with self._lock:
            self.seen.append(post_url)
        listing_id = post_url.rsplit("/", 1)[-1]
        return ListingStatus.ACTIVE, listing(listing_id, price="$90")

    def check_listing(self, details, item_config) -> bool:
        return True


def refresher(store: Cache, marketplace: StubMarketplace, **kwargs) -> ListingRefresher:
    made = ListingRefresher(
        marketplace_for=lambda name: marketplace if name == marketplace.name else None,
        item_config_for=lambda name, item: None,
        logger=logging.getLogger("test-review-lane"),
        local_cache=store,
        **kwargs,
    )
    made.slice_interval = 0.0
    made.listing_interval = 0.0
    return made


# --------------------------------------------------------------------------- #
# The two flows keep off each other's listings
# --------------------------------------------------------------------------- #


def test_a_listing_the_search_is_reading_is_skipped_not_waited_for(store):
    record_observation(listing("1"), local_cache=store)
    # Make it stale enough to be a candidate.
    _age(store, "facebook", "1", hours=48)

    marketplace = StubMarketplace()
    review = refresher(store, marketplace, recheck_interval=3600)

    with control.claim("facebook", "1") as mine:
        assert mine  # the search flow holds it
        report = review.run_slice(("facebook",), limit=5)

    assert marketplace.seen == []
    assert report.checked == 0
    assert report.skipped == 1
    assert "being read by the search" in report.skips


def test_a_listing_just_fetched_by_a_search_is_not_in_the_review_queue(store):
    # The exact case the user asked about: something the search has only just
    # picked up must not be handed straight to the review as though it were old.
    record_observation(listing("fresh"), local_cache=store)
    record_observation(listing("old"), local_cache=store)
    _age(store, "facebook", "old", hours=48)

    due = stale_records(store, within=6 * 3600, marketplaces=("facebook",))
    assert [record["id"] for record in due] == ["old"]


def test_the_two_flows_ask_different_questions(store):
    # They used to share one: "was this read in the last fifteen minutes?".
    # That made them race -- a search sixteen minutes after a review re-opened
    # every page it had just read, and one fourteen minutes after skipped
    # listings that had genuinely changed.  Now the search asks whether it has
    # ever seen the listing, and only the review asks about time.
    record_observation(listing("1"), local_cache=store)

    # The search: known from the first sighting, and it stays known.
    assert is_known("facebook", "1", local_cache=store)
    _age(store, "facebook", "1", hours=48)
    assert is_known("facebook", "1", local_cache=store)

    # The review: fresh at first, overdue two days later.
    record_observation(listing("2"), local_cache=store)
    assert was_checked_recently(
        "facebook", "2", within=DEFAULT_RECHECK_INTERVAL, local_cache=store
    )
    _age(store, "facebook", "2", hours=48)
    assert not was_checked_recently(
        "facebook", "2", within=DEFAULT_RECHECK_INTERVAL, local_cache=store
    )


def test_a_listing_nobody_has_seen_is_the_searchs_business(store):
    assert not is_known("facebook", "never-seen", local_cache=store)


def test_a_deleted_listing_stays_known_to_the_search(store):
    # The marketplace has no idea we threw it away, so it keeps turning up in
    # results.  Re-fetching its page on every cycle forever was the most
    # wasteful thing the search did.
    record_observation(listing("1"), local_cache=store)
    delete_observations([("facebook", "1")], local_cache=store)
    assert is_known("facebook", "1", local_cache=store)


def test_a_review_claims_what_it_reads_so_a_search_will_not_repeat_it(store):
    record_observation(listing("1"), local_cache=store)
    _age(store, "facebook", "1", hours=48)

    held: List[bool] = []
    marketplace = StubMarketplace()

    class Watching(StubMarketplace):
        def recheck_listing(self, post_url, item_config):
            # While the review has it open, the search flow must be told no.
            held.append(control.is_claimed("facebook", "1"))
            return super().recheck_listing(post_url, item_config)

    watching = Watching()
    refresher(store, watching, recheck_interval=3600).run_slice(("facebook",), limit=5)
    assert held == [True]
    # And it is let go afterwards, or the listing would never be read again.
    assert not control.is_claimed("facebook", "1")
    assert marketplace.seen == []


def test_a_platform_on_a_cooldown_is_left_alone_by_both_flows():
    control.block_marketplace("mercadolibre", reason="sign-in wall")
    assert control.marketplace_blocked("mercadolibre")
    # The review reads the same register the search does, so a second browser
    # does not keep knocking at a door the first one already found shut.
    assert "mercadolibre" in control.marketplace_blocks()
    assert not control.marketplace_blocked("facebook")


def test_two_reviewers_never_read_the_same_listing_twice(store):
    """Two flows, one queue: the claim is what stops the duplicate work."""
    for index in range(6):
        record_observation(listing(str(index)), local_cache=store)
        _age(store, "facebook", str(index), hours=48)

    marketplace = StubMarketplace()
    first = refresher(store, marketplace, recheck_interval=3600)
    second = refresher(store, marketplace, recheck_interval=3600)

    done = threading.Barrier(2, timeout=10)

    def run(instance: ListingRefresher) -> None:
        done.wait()
        instance.run_slice(("facebook",), limit=6)

    threads = [threading.Thread(target=run, args=(instance,)) for instance in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    # Every listing read at most once across both flows, even though both were
    # handed the same queue at the same moment.
    assert len(marketplace.seen) == len(set(marketplace.seen))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _age(store: Cache, marketplace: str, listing_id: str, hours: float) -> None:
    """Backdate a stored listing's ``last_seen`` so it counts as overdue."""
    from datetime import datetime, timedelta, timezone

    from ai_marketplace_monitor.observations import observation_key

    key = observation_key(marketplace, listing_id)
    record = store.get(key)
    assert isinstance(record, dict)
    record["last_seen"] = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat(timespec="seconds")
    store.set(key, record, tag="listing-observation")


def test_the_backdating_helper_actually_backdates(store):
    # The rest of this file rests on it, so it is worth a test of its own.
    record_observation(listing("1"), local_cache=store)
    _age(store, "facebook", "1", hours=48)
    due = stale_records(store, within=3600, marketplaces=("facebook",))
    assert [record["id"] for record in due] == ["1"]
