"""Avisar cuando una publicación guardada baja de precio.

The message itself is old -- ``LISTING_DISCOUNTED`` and ``notify_price_drop``
have always existed -- but its only door was the search, and a search never
hands over a listing it already knows
(:func:`~ai_marketplace_monitor.observations.is_known`).  So the price moved,
the store recorded it, the log said so, and nobody was told.  A re-check is the
only place a price moves, so the announcement belongs to the review, beside the
two that were already there (top 1 and low stock).

Nothing here opens a browser or sends anything: ``_notify`` is replaced by a
recorder, which is exactly what is under test -- who gets told, about what, and
who deliberately does not.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any, Iterator, List, Tuple

import pytest
from diskcache import Cache  # type: ignore

from ai_marketplace_monitor import control
from ai_marketplace_monitor import user as user_module
from ai_marketplace_monitor.listing import Listing
from ai_marketplace_monitor.monitor import MarketplaceMonitor
from ai_marketplace_monitor.notification import NotificationStatus
from ai_marketplace_monitor.observations import record_observation, reset_index_cache
from ai_marketplace_monitor.refresh import PriceDrop, RefreshReport
from ai_marketplace_monitor.user import User

CONFIG = """
[marketplace.facebook]
search_city = "santiago"

[user.ana]
pushbullet_token = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

[user.beto]
pushbullet_token = "yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"

[item.ps5]
search_phrases = "playstation 5"
"""


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    control.reset_for_tests()
    reset_index_cache()
    yield
    control.reset_for_tests()
    reset_index_cache()


@pytest.fixture
def store(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Cache]:
    """A cache of this test's own, for what a user was last told."""
    cache = Cache(str(tmp_path / "cache"))
    monkeypatch.setattr(user_module, "cache", cache)
    yield cache
    cache.close()


def listing(price: str = "$80 000", marketplace: str = "facebook") -> Listing:
    return Listing(
        marketplace=marketplace,
        name="ps5",
        id="1",
        title="PS5 slim",
        image="",
        price=price,
        post_url="https://www.facebook.com/marketplace/item/1/",
        location="Nunoa, RM",
        seller="Ana",
        condition="used_good",
        description="works",
    )


class Recorder:
    """Stands in for ``_notify``.  Records who would be told about what."""

    def __init__(self) -> None:
        self.calls: List[Tuple[List[str], str, str]] = []

    def __call__(self, users, listings, ratings, item_config, **kwargs: Any) -> None:
        self.calls.append((list(users), listings[0].id, listings[0].price))


def build(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    config_text: str = CONFIG,
) -> Tuple[MarketplaceMonitor, Recorder]:
    path = tmp_path / "config.toml"
    path.write_text(config_text, encoding="utf-8")
    monitor = MarketplaceMonitor.__new__(MarketplaceMonitor)
    monitor.config_files = [path]
    monitor.config = None
    monitor.config_hash = None
    monitor._loaded_snapshot = {}
    monitor._fingerprints = {}
    monitor._probe_at = 0.0
    monitor._probe_signature = None
    monitor._reported_bad_version = None
    monitor._announced_pending = None
    monitor._schedule_dirty = False
    monitor.logger = logging.getLogger("test-price-drop")
    monitor.keyboard_monitor = None
    monitor.context = None
    monitor.lanes = {}
    monitor.refresher = None
    monitor.active_marketplaces = {}
    monitor.load_config_file()
    recorder = Recorder()
    monkeypatch.setattr(monitor, "_notify", recorder)
    return monitor, recorder


def told(store: Cache, user: str, monitor: MarketplaceMonitor, price: str) -> None:
    """Pretend this user was already notified about the listing at ``price``."""
    assert monitor.config is not None
    User(monitor.config.user[user]).to_cache(listing(price=price), local_cache=store)


def report_of(previous: str, now: str, item_name: str | None = "ps5") -> RefreshReport:
    report = RefreshReport(checked=1, updated=1)
    report.drops.append(
        PriceDrop(listing=listing(price=now), previous=previous, item_name=item_name)
    )
    return report


# --------------------------------------------------------------------------- #
# The message the review had nowhere to send
# --------------------------------------------------------------------------- #


def test_a_fall_is_announced_to_the_users_who_were_told(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, store: Cache
) -> None:
    monitor, recorder = build(tmp_path, monkeypatch)
    told(store, "ana", monitor, "$100 000")
    told(store, "beto", monitor, "$100 000")

    monitor._announce_price_drops(report_of("$100 000", "$80 000"))

    assert recorder.calls == [(["ana", "beto"], "1", "$80 000")]


def test_a_user_who_was_never_told_hears_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, store: Cache
) -> None:
    """Otherwise the first round after this existed would announce, as a
    bargain, every listing in the store the user has never heard of."""
    monitor, recorder = build(tmp_path, monkeypatch)
    told(store, "ana", monitor, "$100 000")

    monitor._announce_price_drops(report_of("$100 000", "$80 000"))

    assert recorder.calls == [(["ana"], "1", "$80 000")]


def test_cheaper_is_measured_against_what_the_user_was_told(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, store: Cache
) -> None:
    """The store's fall and the user's fall are different questions.

    The seller went 100 -> 90 -> 80 while the user was told 90: the store sees
    two falls and the user has one thing worth hearing.  Beto, already told 80,
    has none.
    """
    monitor, recorder = build(tmp_path, monkeypatch)
    told(store, "ana", monitor, "$90 000")
    told(store, "beto", monitor, "$80 000")

    monitor._announce_price_drops(report_of("$90 000", "$80 000"))

    assert recorder.calls == [(["ana"], "1", "$80 000")]


def test_the_switch_silences_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, store: Cache
) -> None:
    monitor, recorder = build(
        tmp_path, monkeypatch, CONFIG + "\n[monitor]\nnotify_price_drop = false\n"
    )
    told(store, "ana", monitor, "$100 000")

    monitor._announce_price_drops(report_of("$100 000", "$80 000"))

    assert recorder.calls == []


def test_only_the_users_the_search_names_are_told(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, store: Cache
) -> None:
    """The search's own ``notify`` list, exactly as a new listing would use it."""
    monitor, recorder = build(
        tmp_path,
        monkeypatch,
        CONFIG.replace(
            'search_phrases = "playstation 5"',
            'search_phrases = "playstation 5"\nnotify = "beto"',
        ),
    )
    told(store, "ana", monitor, "$100 000")
    told(store, "beto", monitor, "$100 000")

    monitor._announce_price_drops(report_of("$100 000", "$80 000"))

    assert recorder.calls == [(["beto"], "1", "$80 000")]


def test_a_listing_whose_search_is_gone_is_not_announced(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, store: Cache
) -> None:
    """It is still re-checked -- it is still in the dashboard -- but a message
    about a product the user deleted is noise, and there is no ``notify`` list
    to read anyway."""
    monitor, recorder = build(tmp_path, monkeypatch)
    told(store, "ana", monitor, "$100 000")

    monitor._announce_price_drops(report_of("$100 000", "$80 000", item_name="borrada"))

    assert recorder.calls == []


def test_a_paused_search_says_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, store: Cache
) -> None:
    """Switching a search off is the one way to stop hearing about it, and it
    has to mean the whole of it -- the same rule the low-stock alert follows."""
    monitor, recorder = build(
        tmp_path,
        monkeypatch,
        CONFIG.replace(
            'search_phrases = "playstation 5"',
            'search_phrases = "playstation 5"\nenabled = false',
        ),
    )
    told(store, "ana", monitor, "$100 000")

    monitor._announce_price_drops(report_of("$100 000", "$80 000"))

    assert recorder.calls == []


def test_nothing_to_announce_is_not_a_message(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, store: Cache
) -> None:
    monitor, recorder = build(tmp_path, monkeypatch)
    monitor._announce_price_drops(RefreshReport(checked=3, updated=1))
    monitor._announce_price_drops(None)
    assert recorder.calls == []


def test_the_status_the_user_gets_is_the_discount(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, store: Cache
) -> None:
    """The filter and the card have to agree, or the message would be built as
    something else -- a "nueva publicación" about a listing from last week."""
    monitor, _recorder = build(tmp_path, monkeypatch)
    told(store, "ana", monitor, "$100 000")
    assert monitor.config is not None

    status = User(monitor.config.user["ana"]).notification_status(
        listing(price="$80 000"), local_cache=store
    )
    assert status is NotificationStatus.LISTING_DISCOUNTED


def test_a_tracked_page_is_announced_like_anything_else(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, store: Cache
) -> None:
    monitor, recorder = build(
        tmp_path,
        monkeypatch,
        CONFIG + '\n[track.sabanas]\nurl = "https://t.cl/p/sabanas"\nnotify = "ana"\n',
    )
    tracked = Listing(**{**listing().__dict__, "marketplace": "tracked", "name": "sabanas"})
    assert monitor.config is not None
    User(monitor.config.user["ana"]).to_cache(
        Listing(**{**tracked.__dict__, "price": "$100 000"}), local_cache=store
    )
    # `local_cache` is not optional here even though the signature allows it:
    # without it this writes a listing into the real observation store under
    # ~/.ai-marketplace-monitor, where it shows up as a phantom card on the
    # dashboard of whoever ran the tests, and stays there.
    record_observation(tracked, matched=True, item_name="sabanas", local_cache=store)

    report = RefreshReport(checked=1, updated=1)
    report.drops.append(PriceDrop(listing=tracked, previous="$100 000", item_name="sabanas"))
    monitor._announce_price_drops(report)

    assert recorder.calls == [(["ana"], "1", "$80 000")]
