"""Mercado Libre: URL grammar, card parsing, and one item on two platforms.

The URL suffixes asserted here were read off the site's own filter links and
then checked against live result counts; the fixture is a real search page.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

import pytest
from pytest_playwright.pytest_playwright import CreateContextCallback  # type: ignore

from ai_marketplace_monitor import mercadolibre
from ai_marketplace_monitor.config import Config
from ai_marketplace_monitor.listing import Listing
from ai_marketplace_monitor.mercadolibre import (
    MercadoLibreMarketplace,
    MercadoLibreSearchResultPage,
    listing_id_from_url,
)

CHILE = "https://listado.mercadolibre.cl"


def marketplace(**config_options: object) -> MercadoLibreMarketplace:
    market = MercadoLibreMarketplace("mercadolibre", None)
    market.configure(MercadoLibreMarketplace.get_config(name="mercadolibre", **config_options))
    return market


def item(**options: object):
    return MercadoLibreMarketplace.get_item_config(
        name="ps5", marketplace="mercadolibre", search_phrases=["playstation 5"], **options
    )


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, f"{CHILE}/playstation-5_NoIndex_True"),
        (
            {"min_price": "300000", "max_price": "600000"},
            f"{CHILE}/playstation-5_PriceRange_300000CLP-600000CLP_NoIndex_True",
        ),
        # An open-ended range is expressed with a zero, as the site does.
        ({"max_price": "600000"}, f"{CHILE}/playstation-5_PriceRange_0CLP-600000CLP_NoIndex_True"),
        ({"condition": ["used"]}, f"{CHILE}/playstation-5_ITEM*CONDITION_2230581_NoIndex_True"),
        ({"condition": ["new"]}, f"{CHILE}/playstation-5_ITEM*CONDITION_2230284_NoIndex_True"),
        # Two conditions cannot be expressed in one URL, so none is; they are
        # matched against the parsed cards instead.
        ({"condition": ["used", "refurbished"]}, f"{CHILE}/playstation-5_NoIndex_True"),
        ({"free_shipping": True}, f"{CHILE}/playstation-5_CostoEnvio_Gratis_NoIndex_True"),
        (
            {"shipping_origin": "local"},
            f"{CHILE}/playstation-5_SHIPPING*ORIGIN_10215068_NoIndex_True",
        ),
    ],
)
def test_search_url(options: dict, expected: str) -> None:
    assert marketplace().search_url("PlayStation 5", item(**options)) == expected


def test_search_url_pagination() -> None:
    """`_Desde_` counts results, not pages: the second page starts at 51."""
    url = marketplace().search_url("playstation 5", item(), offset=50)
    assert url == f"{CHILE}/playstation-5_Desde_51_NoIndex_True"


def test_search_url_uses_the_configured_site() -> None:
    market = marketplace(site="MLA")
    url = market.search_url("playstation 5", item(min_price="1000"))
    assert url.startswith("https://listado.mercadolibre.com.ar/")
    assert "1000ARS" in url


def test_item_options_override_the_marketplace() -> None:
    market = marketplace(condition=["new"])
    assert "2230581" in market.search_url("ps5", item(condition=["used"]))


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.mercadolibre.cl/consola-ps5/p/MLC61403702", "MLC61403702"),
        ("https://www.mercadolibre.cl/sony-ps5-slim/up/MLCU4708792716", "MLCU4708792716"),
        ("https://articulo.mercadolibre.cl/MLC-1502963372-play-5-_JM", "MLC1502963372"),
    ],
)
def test_listing_id_from_url(url: str, expected: str) -> None:
    assert listing_id_from_url(url) == expected


def test_search_page(
    new_context: CreateContextCallback, filename: str = "mercadolibre_search_result.html"
) -> None:
    """Parse a saved search page the way the scraper parses a live one."""
    local_file_path = Path(__file__).parent / filename
    page = new_context(java_script_enabled=True).new_page()
    page.goto(f"file://{local_file_path}")

    listings = []
    for _ in range(10):
        page.wait_for_load_state("domcontentloaded")
        listings = MercadoLibreSearchResultPage(page).get_listings(item_name="ps5")
        if listings:
            break
        time.sleep(1)

    assert len(listings) > 20
    for listing in listings:
        assert listing.marketplace == "mercadolibre"
        assert listing.name == "ps5"
        assert listing.title
        assert listing.id.startswith("MLC")
        assert listing.post_url.startswith("https://")
        assert "?" not in listing.post_url
        assert listing.price
        assert listing.image
        # Mercado Libre shows no seller location in this category, and the
        # scraper leaves it empty rather than inventing one.
        assert listing.location == ""


MULTI_PLATFORM_CONFIG = """
[marketplace.facebook]
search_city = 'santiago'

[marketplace.mercadolibre]
site = 'MLC'

[item.'playstation 5']
search_phrases = 'playstation 5'
min_price = '300000'
max_price = '600000'

[item.'playstation 5'.facebook]
search_city = 'santiago'
radius = 60
condition = ['used_good']

[item.'playstation 5'.mercadolibre]
condition = ['used']
free_shipping = true

[user.me]
"""


def test_one_item_runs_on_every_marketplace(config_file: Callable) -> None:
    config = Config([config_file(MULTI_PLATFORM_CONFIG)])

    assert set(config.items) == {
        ("facebook", "playstation 5"),
        ("mercadolibre", "playstation 5"),
    }

    facebook = config.items[("facebook", "playstation 5")]
    meli = config.items[("mercadolibre", "playstation 5")]

    # Shared options reach both platforms.
    assert facebook.min_price == "300000"
    assert meli.min_price == "300000"
    assert facebook.search_phrases == ["playstation 5"]
    assert meli.search_phrases == ["playstation 5"]

    # Platform sections stay on their own platform, including a `condition`
    # whose vocabulary differs between them.
    assert facebook.radius == [60]
    assert facebook.condition == ["used_good"]
    assert meli.condition == ["used"]
    assert meli.free_shipping is True


def test_platform_options_do_not_leak(config_file: Callable) -> None:
    """A filter one platform lacks is dropped there, not approximated."""
    config = Config([config_file(MULTI_PLATFORM_CONFIG)])
    meli = config.items[("mercadolibre", "playstation 5")]
    facebook = config.items[("facebook", "playstation 5")]

    # `date_listed` is a Facebook filter and `free_shipping` a Mercado Libre
    # one; neither config class knows the other's.
    assert not hasattr(meli, "date_listed")
    assert not hasattr(facebook, "free_shipping")


SHARED_FACEBOOK_OPTION_CONFIG = """
[marketplace.facebook]
search_city = 'santiago'

[marketplace.mercadolibre]

[item.ps5]
search_phrases = 'playstation 5'
search_city = 'santiago'
radius = 60
seller_locations = 'santiago'

[user.me]
"""


def test_facebook_only_options_are_ignored_by_mercadolibre(config_file: Callable) -> None:
    """An existing Facebook config keeps working when Mercado Libre is added.

    Location options are the ones that matter here: Mercado Libre's search has
    no location facet at all, so a city or a radius written for Facebook is
    dropped rather than failing the whole config.
    """
    config = Config([config_file(SHARED_FACEBOOK_OPTION_CONFIG)])

    facebook = config.items[("facebook", "ps5")]
    assert facebook.search_city == ["santiago"]
    assert facebook.radius == [60]

    meli = config.items[("mercadolibre", "ps5")]
    assert meli.search_phrases == ["playstation 5"]
    assert not hasattr(meli, "seller_locations")


UNKNOWN_OPTION_CONFIG = """
[marketplace.facebook]
search_city = 'santiago'

[item.ps5]
search_phrases = 'playstation 5'
totally_made_up = 'yes'

[user.me]
"""


def test_unknown_option_is_still_an_error(config_file: Callable) -> None:
    """Dropping unsupported filters must not swallow typos."""
    with pytest.raises(Exception, match="totally_made_up"):
        Config([config_file(UNKNOWN_OPTION_CONFIG)])


UNDECLARED_MARKETPLACE_SECTION_CONFIG = """
[marketplace.facebook]
search_city = 'santiago'

[item.ps5]
search_phrases = 'playstation 5'

[item.ps5.mercadolibre]
condition = ['used']

[user.me]
"""


def test_a_platform_needs_no_section_to_be_used(config_file: Callable) -> None:
    """A search can configure Mercado Libre without anyone declaring it.

    The platforms are built into the monitor, so there is nothing to add before
    a search may use one -- which is the whole point: the step that used to be
    required could only ever be forgotten, and forgetting it produced a search
    that looked configured and ran nowhere.
    """
    config = Config([config_file(UNDECLARED_MARKETPLACE_SECTION_CONFIG)])
    assert config.items[("mercadolibre", "ps5")].condition == ["used"]


UNKNOWN_MARKETPLACE_SECTION_CONFIG = """
[item.ps5]
search_phrases = 'playstation 5'
search_city = 'santiago'

[item.ps5.craigslist]
condition = ['used']

[user.me]
"""


def test_section_for_a_marketplace_nobody_knows_is_an_error(config_file: Callable) -> None:
    """Built in is not the same as anything goes: a name the monitor cannot
    search is a typo, and passing over it would lose the settings silently."""
    with pytest.raises(Exception, match="craigslist"):
        Config([config_file(UNKNOWN_MARKETPLACE_SECTION_CONFIG)])


NO_CITY_CONFIG = """
[item.ps5]
search_phrases = 'playstation 5'

[item.ps5.facebook]
enabled = false

[user.me]
"""


def test_mercadolibre_does_not_need_a_city(config_file: Callable) -> None:
    """Facebook cannot search without a city; Mercado Libre searches a site."""
    config = Config([config_file(NO_CITY_CONFIG)])
    assert ("mercadolibre", "ps5") in config.items


NO_CITY_EVERYWHERE_CONFIG = """
[item.ps5]
search_phrases = 'playstation 5'

[user.me]
"""


def test_a_search_left_on_facebook_still_needs_a_city(config_file: Callable) -> None:
    """The city check is not softened by the platforms being built in.

    A search that runs on Facebook has to say where from, and saying nothing at
    all means it runs there. Switching Facebook off for that search is the way
    to have a Mercado-Libre-only one -- which is exactly what the web UI writes
    when the platform is unticked.
    """
    with pytest.raises(ValueError, match="search_city"):
        Config([config_file(NO_CITY_EVERYWHERE_CONFIG)])


def test_handles_url() -> None:
    assert MercadoLibreMarketplace.handles_url("https://www.mercadolibre.cl/x/p/MLC1")
    assert not MercadoLibreMarketplace.handles_url(
        "https://www.facebook.com/marketplace/item/123/"
    )


class FakePage:
    """Just enough of a Playwright page for `search` to run without a browser."""

    url = "https://listado.mercadolibre.cl/playstation-5"

    def goto(self, url: str, **kwargs: object) -> None:
        self.url = url

    def wait_for_load_state(self, *args: object, **kwargs: object) -> None:
        return None

    def content(self) -> str:
        return ""


def sample_listing() -> Listing:
    return Listing(
        marketplace="mercadolibre",
        name="ps5",
        id="MLC1",
        title="Playstation 5 Slim",
        image="https://http2.mlstatic.com/image.webp",
        price="$500.000",
        post_url="https://www.mercadolibre.cl/playstation-5-slim/p/MLC1",
        location="",
        seller="ZXTECH",
        condition="Usado",
        description="Con dos controles.",
    )


def run_search(monkeypatch: pytest.MonkeyPatch, listing: Listing, **item_options: object):
    """Drive `search` over one fake card, collecting what gets recorded."""
    market = marketplace()
    market.page = FakePage()

    monkeypatch.setattr(
        MercadoLibreSearchResultPage, "get_listings", lambda self, item_name: [listing]
    )
    monkeypatch.setattr(
        MercadoLibreMarketplace,
        "get_listing_details",
        lambda self, post_url, item_config, price=None, title=None, fallback=None: (
            listing,
            True,
        ),
    )
    recorded: list[tuple[Listing, bool, str]] = []
    monkeypatch.setattr(
        mercadolibre,
        "record_observation",
        lambda listing, matched, item_name: recorded.append((listing, matched, item_name)),
    )

    found = list(market.search(item(**item_options)))
    return found, recorded


def test_search_records_every_sighting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every sighting has to reach the observation log.

    It is the only place a listing's own data gets there. Without it the
    dashboard still ends up with a record — `record_rating` creates a blank one
    as a side effect — but with no title, price, image, link or search item, so
    the listings pile up in a nameless group as empty cards.
    """
    listing = sample_listing()
    found, recorded = run_search(monkeypatch, listing)

    assert found == [listing]
    assert recorded == [(listing, True, "ps5")]
    # The item name is what groups a product across marketplaces.
    assert recorded[0][2] == listing.name


def test_excluded_listings_are_recorded_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """A listing rejected on its own page is logged as not matching, not dropped.

    Same shape as the Facebook scraper: the pre-fetch pass drops obvious misses
    without a round trip, and everything that survives it is recorded whichever
    way the remaining filters go, so the dashboard sees the whole market.
    """
    listing = sample_listing()
    found, recorded = run_search(monkeypatch, listing, keywords=["nintendo"])

    assert found == []
    assert len(recorded) == 1
    assert recorded[0][1] is False
