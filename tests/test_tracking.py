"""Following one product page, on a site nobody wrote a scraper for.

Almost none of this is new machinery -- a tracked product is a listing with no
search behind it -- so most of what is worth testing is the seam: that a
``[track.*]`` section becomes an ordinary item, that a generic page becomes an
ordinary listing, and that the one genuinely new rule (stock mínimo) says its
piece once rather than on every round.
"""

from __future__ import annotations

import pathlib
from typing import Iterator

import pytest
from diskcache import Cache  # type: ignore

from ai_marketplace_monitor import tracking
from ai_marketplace_monitor.config import Config
from ai_marketplace_monitor.extract import Extraction, extract
from ai_marketplace_monitor.listing import Listing

PAGE = """
<html><head>
<script type="application/ld+json">
{"@type":"Product","name":"Notebook X14","image":"https://t.cl/n.jpg",
 "offers":{"price":"899990","availability":"https://schema.org/InStock",
           "inventoryLevel":3}}
</script>
</head><body></body></html>
"""

BASE_CONFIG = """
[marketplace.facebook]
search_city = 'santiago'

[item.ps5]
search_phrases = 'playstation 5'
"""


def config_of(text: str, tmp_path: pathlib.Path) -> Config:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return Config([path])


@pytest.fixture
def store(tmp_path: pathlib.Path) -> Iterator[Cache]:
    cache = Cache(str(tmp_path / "cache"))
    yield cache
    cache.close()


# --------------------------------------------------------------------------- #
# A tracker is an item
# --------------------------------------------------------------------------- #


def test_a_track_section_becomes_an_item_on_the_tracked_platform(tmp_path) -> None:
    config = config_of(
        BASE_CONFIG + '\n[track.notebook]\nurl = "https://t.cl/p/notebook"\nmin_stock = 2\n',
        tmp_path,
    )
    assert ("tracked", "notebook") in config.items
    tracker = config.items[("tracked", "notebook")]
    assert tracker.url == "https://t.cl/p/notebook"
    assert tracker.min_stock == 2


def test_a_tracker_inherits_everything_an_item_understands(tmp_path) -> None:
    # `notify`, `ai`, `enabled` and the rest work with no extra code, which is
    # the reason `TrackedItemConfig` derives from `ItemConfig`.
    config = config_of(
        BASE_CONFIG
        + '\n[user.maxi]\npushbullet_token = "x"\n'
        + '\n[track.notebook]\nurl = "https://t.cl/p/n"\nnotify = "maxi"\nenabled = false\n',
        tmp_path,
    )
    tracker = config.items[("tracked", "notebook")]
    assert tracker.notify == ["maxi"]
    assert tracker.enabled is False


def test_a_tracker_gets_a_search_phrase_it_never_asked_for(tmp_path) -> None:
    # Every counter and log line is labelled with one; filling it in is cheaper
    # than threading "this kind of item has no phrase" through all of them.
    config = config_of(BASE_CONFIG + '\n[track.n]\nurl = "https://t.cl/p/n"\n', tmp_path)
    assert config.items[("tracked", "n")].search_phrases == ["n"]


WITH_TRACKER = BASE_CONFIG + """
[track.notebook]
url = "https://t.cl/p/notebook"
"""


def test_a_tracker_is_not_published_as_one_of_the_scrapers_searches(tmp_path) -> None:
    # `describe()` feeds "Busquedas que el scraper esta usando", and a tracker
    # sitting in that list came with a "run now" and a "search this next" that
    # `schedule_jobs` had no job to honour -- it skips the tracked platform.
    config = config_of(WITH_TRACKER, tmp_path)
    assert ("tracked", "notebook") in config.items
    searches = config.describe()["searches"]
    assert [row for row in searches if row["item"] == "ps5"]
    assert not [row for row in searches if row["marketplace"] == "tracked"]
    assert not [row for row in searches if row["item"] == "notebook"]


def test_the_effective_configuration_still_shows_the_tracker(tmp_path) -> None:
    # Only the *searches* list drops it.  The resolved-config dump is there to
    # prove what the scraper actually loaded, so hiding a tracker there would
    # trade one wrong answer for another.
    config = config_of(WITH_TRACKER, tmp_path)
    assert "notebook" in config.describe()["items"]


def test_a_tracker_can_name_a_group(tmp_path) -> None:
    config = config_of(
        BASE_CONFIG
        + """
[track.zapatilla-falabella]
url = "https://t.cl/p/a"
group = "Zapatillas"
""",
        tmp_path,
    )
    assert config.items[("tracked", "zapatilla-falabella")].group == "Zapatillas"


def test_a_tracker_without_a_group_has_none(tmp_path) -> None:
    config = config_of(WITH_TRACKER, tmp_path)
    assert config.items[("tracked", "notebook")].group is None


def test_an_empty_group_means_no_group(tmp_path) -> None:
    # How the interface says "cleared" when the user empties the field. It must
    # mean the same as the key being absent, not a group whose name is nothing.
    config = config_of(
        BASE_CONFIG
        + """
[track.notebook]
url = "https://t.cl/p/n"
group = "   "
""",
        tmp_path,
    )
    assert config.items[("tracked", "notebook")].group is None


def test_a_group_named_after_a_search_is_refused(tmp_path) -> None:
    # They would share the top-1 record, which is keyed by the name asked about.
    with pytest.raises(ValueError, match="cheapest offer"):
        config_of(
            BASE_CONFIG
            + """
[track.notebook]
url = "https://t.cl/p/n"
group = "ps5"
""",
            tmp_path,
        )


def test_a_group_named_after_another_tracker_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="cheapest offer"):
        config_of(
            BASE_CONFIG
            + """
[track.notebook]
url = "https://t.cl/p/n"

[track.tablet]
url = "https://t.cl/p/t"
group = "notebook"
""",
            tmp_path,
        )


def test_two_trackers_may_share_a_group(tmp_path) -> None:
    config = config_of(
        BASE_CONFIG
        + """
[track.notebook-falabella]
url = "https://t.cl/p/a"
group = "Notebooks"

[track.notebook-paris]
url = "https://t.cl/p/b"
group = "Notebooks"
""",
        tmp_path,
    )
    assert config.items[("tracked", "notebook-falabella")].group == "Notebooks"
    assert config.items[("tracked", "notebook-paris")].group == "Notebooks"


def test_a_config_with_no_trackers_has_no_tracked_platform(tmp_path) -> None:
    # It is not a marketplace: it must not appear as one in the interface, and
    # there is no session to import for it.
    config = config_of(BASE_CONFIG, tmp_path)
    assert "tracked" not in config.marketplace
    assert not any(name == "tracked" for name, _item in config.items)


def test_a_tracker_notifying_somebody_who_does_not_exist_is_refused(tmp_path) -> None:
    # Checked against the real users, which is exactly the reuse this design is
    # for: nothing had to be written for it.
    with pytest.raises(ValueError, match="does not exist"):
        config_of(
            BASE_CONFIG + '\n[track.n]\nurl = "https://t.cl/p"\nnotify = "nadie"\n',
            tmp_path,
        )


def test_a_tracker_needs_a_real_address(tmp_path) -> None:
    for bad in ("", "no-es-una-url", "ftp://t.cl/p"):
        with pytest.raises(ValueError):
            config_of(BASE_CONFIG + f'\n[track.n]\nurl = "{bad}"\n', tmp_path)


def test_min_stock_must_be_a_whole_number_and_can_be_switched_off(tmp_path) -> None:
    with pytest.raises(ValueError):
        config_of(BASE_CONFIG + '\n[track.n]\nurl = "https://t.cl/p"\nmin_stock = -1\n', tmp_path)
    config = config_of(
        BASE_CONFIG + '\n[track.n]\nurl = "https://t.cl/p"\nmin_stock = false\n', tmp_path
    )
    assert config.items[("tracked", "n")].min_stock is None


def test_a_tracker_cannot_share_a_name_with_a_search(tmp_path) -> None:
    # Both would be labelled the same in the log, the counters and the
    # dashboard, and whichever one the user looked at would be the wrong one.
    with pytest.raises(ValueError, match="already a search"):
        config_of(BASE_CONFIG + '\n[track.ps5]\nurl = "https://t.cl/p"\n', tmp_path)


# --------------------------------------------------------------------------- #
# A page is a listing
# --------------------------------------------------------------------------- #


def test_a_page_becomes_an_ordinary_listing() -> None:
    listing = tracking.listing_from(extract(PAGE), "https://t.cl/p/n?ref=x", "notebook")
    assert listing is not None
    assert listing.marketplace == "tracked"
    assert listing.title == "Notebook X14"
    assert listing.price == "899990"
    assert listing.stock == "3"
    assert listing.availability == "in_stock"
    # The query string is not part of what is being watched.
    assert listing.post_url == "https://t.cl/p/n"


def test_the_fields_a_generic_page_cannot_supply_are_left_empty() -> None:
    # A seller that is really a hostname is a fact about the URL, not about who
    # is selling.
    listing = tracking.listing_from(extract(PAGE), "https://t.cl/p/n", "notebook")
    assert listing is not None
    assert listing.seller == "" and listing.location == "" and listing.condition == ""


def test_a_page_with_no_readable_price_is_not_tracked() -> None:
    # It would sit there looking like it worked and never notice a drop.
    found = Extraction()
    found.set("title", "Algo", "heurística")
    assert tracking.listing_from(found, "https://t.cl/p/n", "x") is None


def test_the_id_is_stable_and_ignores_tracking_parameters() -> None:
    first = tracking.tracked_id("https://t.cl/p/n?utm_source=mail")
    second = tracking.tracked_id("https://t.cl/p/n/")
    assert first == second
    assert first != tracking.tracked_id("https://t.cl/p/otro")


# --------------------------------------------------------------------------- #
# Stock mínimo
# --------------------------------------------------------------------------- #


def listing_with(stock: str) -> Listing:
    return Listing(
        marketplace="tracked",
        name="notebook",
        id="abc",
        title="Notebook X14",
        image="",
        price="$899.990",
        post_url="https://t.cl/p/n",
        location="",
        seller="",
        condition="",
        description="",
        stock=stock,
    )


def test_no_threshold_means_no_alert(store: Cache) -> None:
    assert not tracking.stock_alert("n", listing_with("1"), None, store)


def test_a_page_that_publishes_no_stock_never_alerts(store: Cache) -> None:
    # Which is most pages.  A number that is not there is not zero, and firing
    # on it would mean an alert about every product on every site that does not
    # count.
    assert not tracking.stock_alert("n", listing_with(""), 2, store)
    assert not tracking.stock_alert("n", listing_with("varios"), 2, store)


def test_above_the_threshold_is_silence(store: Cache) -> None:
    assert not tracking.stock_alert("n", listing_with("5"), 2, store)


def test_reaching_the_threshold_says_so_once(store: Cache) -> None:
    assert tracking.stock_alert("n", listing_with("2"), 2, store)
    # A tracker sitting at two units for a fortnight is a fortnight of one
    # message, not of one message a round.
    assert not tracking.stock_alert("n", listing_with("2"), 2, store)


def test_a_further_fall_is_news_again(store: Cache) -> None:
    # Two left and one left are different things to somebody deciding whether
    # to buy today.
    assert tracking.stock_alert("n", listing_with("2"), 2, store)
    assert tracking.stock_alert("n", listing_with("1"), 2, store)
    assert tracking.stock_alert("n", listing_with("0"), 2, store)


def test_a_restock_re_arms_it(store: Cache) -> None:
    assert tracking.stock_alert("n", listing_with("1"), 2, store)
    assert not tracking.stock_alert("n", listing_with("9"), 2, store)
    assert tracking.stock_alert("n", listing_with("1"), 2, store)


def test_two_trackers_do_not_share_a_threshold(store: Cache) -> None:
    assert tracking.stock_alert("uno", listing_with("1"), 2, store)
    assert tracking.stock_alert("dos", listing_with("1"), 2, store)


def test_below_minimum_reads_the_page_number() -> None:
    assert tracking.below_minimum(listing_with("1"), 2)
    assert not tracking.below_minimum(listing_with("3"), 2)
    assert not tracking.below_minimum(listing_with(""), 2)
    assert not tracking.below_minimum(listing_with("2"), None)


def test_stock_level_tells_none_apart_from_zero() -> None:
    assert tracking.stock_level(listing_with("0")) == 0
    assert tracking.stock_level(listing_with("")) is None
    assert tracking.stock_level(listing_with("muchos")) is None


# --------------------------------------------------------------------------- #
# The platform's own rules
# --------------------------------------------------------------------------- #


def test_a_tracker_is_never_matched_to_an_address_automatically() -> None:
    # Claiming every URL would hand Facebook listings to the generic reader
    # instead of to the scraper that knows Facebook.
    assert not tracking.TrackedMarketplace.handles_url("https://www.facebook.com/marketplace/item/1")
    assert not tracking.TrackedMarketplace.handles_url("https://cualquiera.cl/p/1")


def test_a_tracker_has_to_be_asked_for() -> None:
    assert tracking.TrackedMarketplace.opt_in is True


def test_a_tracked_product_keeps_almost_no_filters(tmp_path) -> None:
    # The user pasted this address: a keyword filter could only throw away the
    # one product they asked for.
    config = config_of(
        BASE_CONFIG + '\n[track.n]\nurl = "https://t.cl/p/n"\nkeywords = "no aparece"\n',
        tmp_path,
    )
    market = tracking.TrackedMarketplace("tracked", None)
    market.config = config.marketplace["tracked"]
    listing = tracking.listing_from(extract(PAGE), "https://t.cl/p/n", "n")
    assert listing is not None
    assert market.check_listing(listing, config.items[("tracked", "n")])


def test_a_junk_price_is_still_excluded(tmp_path) -> None:
    # A page reporting 999999 while its real price loads would otherwise poison
    # the history it is being tracked for.
    config = config_of(
        BASE_CONFIG
        + '\n[track.n]\nurl = "https://t.cl/p/n"\nexcluded_price_patterns = ["9*"]\n',
        tmp_path,
    )
    market = tracking.TrackedMarketplace("tracked", None)
    market.config = config.marketplace["tracked"]
    listing = listing_with("1")
    listing.price = "$999.999"
    assert not market.check_listing(listing, config.items[("tracked", "n")])


# --------------------------------------------------------------------------- #
# Una página guardada en el disco
# --------------------------------------------------------------------------- #


def saved_page(tmp_path: pathlib.Path, name: str = "producto.html") -> str:
    path = tmp_path / name
    path.write_text(PAGE, encoding="utf-8")
    return path.as_uri()


def test_a_saved_page_can_be_tracked(tmp_path) -> None:
    # The point of allowing it: editing the price in the file and running a
    # review is the only way to exercise the drop-notification path without
    # waiting for a real shop to change something.
    url = saved_page(tmp_path)
    config = config_of(BASE_CONFIG + f'\n[track.n]\nurl = "{url}"\n', tmp_path)
    assert config.items[("tracked", "n")].url == url


def test_a_saved_page_is_read_from_the_disk(tmp_path) -> None:
    found = tracking.preview(saved_page(tmp_path))
    assert found["ok"] and found["usable"]
    assert found["price_value"] == 899990


def test_only_saved_html_is_readable(tmp_path) -> None:
    # Not tidiness: this address arrives from the web interface, so anything
    # else would make "analizar página" a way to read any file on the machine.
    secret = tmp_path / "config.toml"
    secret.write_text("[user.me]\nemail = 'me@example.com'\n", encoding="utf-8")
    assert tracking.local_page(secret.as_uri()) is None
    assert not tracking.is_watchable(secret.as_uri())
    with pytest.raises(ValueError):
        config_of(BASE_CONFIG + f'\n[track.n]\nurl = "{secret.as_uri()}"\n', tmp_path)


def test_a_page_on_another_machine_is_not_a_saved_page() -> None:
    assert tracking.local_page("file://servidor/share/p.html") is None
    assert tracking.local_page("file://localhost/C:/p.html") is not None


def test_a_missing_saved_page_reads_as_unreadable(tmp_path) -> None:
    found = tracking.preview((tmp_path / "no-existe.html").as_uri())
    assert not found["ok"]
    assert found["reason"] == "no-page"
