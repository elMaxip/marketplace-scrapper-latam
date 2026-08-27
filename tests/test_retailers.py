"""Lider and Sodimac, tested against the shapes their pages actually serve.

Every payload below was captured from the live sites in August 2026 and then
trimmed to the keys the parsers read.  Trimmed, not invented: an invented
fixture tests that the parser agrees with whoever wrote the fixture, which is
the same person who wrote the parser.

No browser and no network.  The four site-specific pieces of a retailer -- the
search URL, the search parser, the product parser and the status reading -- are
pure functions of a payload precisely so that this file can exist.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from ai_marketplace_monitor import lider, sodimac
from ai_marketplace_monitor.listing import Listing
from ai_marketplace_monitor.marketplace import ListingStatus
from ai_marketplace_monitor.nextdata import (
    dig,
    dig_list,
    first_text,
    from_html,
    joined_price,
    strip_html,
    text_of,
)
from ai_marketplace_monitor.retailer import IN_STOCK, OUT_OF_STOCK, RetailerMarketplace


# --------------------------------------------------------------------------- #
# Lider payloads
# --------------------------------------------------------------------------- #


def lider_search() -> Dict[str, Any]:
    """One page of ``lider.cl/search?query=taladro``, trimmed.

    Three real entries and the ``AdPlaceholder`` the site mixes in beside them.
    """
    return {
        "props": {
            "pageProps": {
                "initialData": {
                    "searchResult": {
                        "paginationV2": {"maxPage": 23, "pageProperties": None},
                        "itemStacks": [
                            {
                                "count": 4068,
                                "items": [
                                    {
                                        "__typename": "Product",
                                        "usItemId": "00088591179504",
                                        "name": "Kit esmeril angular 4.5” + taladro percutor 20v + 2bats",
                                        "brand": "Stanley",
                                        "canonicalUrl": (
                                            "/ip/herramientas/"
                                            "kit-esmeril-angular-4-5-taladro-percutor-20v-2bats/"
                                            "00088591179504"
                                        ),
                                        "imageInfo": {
                                            "thumbnailUrl": "https://i5.walmartimages.cl/asr/b05.jpeg"
                                        },
                                        "priceInfo": {
                                            "linePrice": "$229.990",
                                            "wasPrice": "$359.990",
                                            "itemPrice": "$359.990",
                                        },
                                        "sellerName": "Lider",
                                        "shortDescription": None,
                                    },
                                    {
                                        "__typename": "Product",
                                        "usItemId": "00780467605043",
                                        "name": "Taladro percutor inalámbrico 48 Nm",
                                        "brand": "Einhell",
                                        "canonicalUrl": "/ip/herramientas/taladro-perc/00780467605043",
                                        "imageInfo": {
                                            "thumbnailUrl": "https://i5.walmartimages.cl/asr/0cf.jpeg"
                                        },
                                        # No discount: the site sends only itemPrice.
                                        "priceInfo": {"itemPrice": "$149.990"},
                                        "sellerName": "nocnoc",
                                        "shortDescription": None,
                                    },
                                    {
                                        "__typename": "AdPlaceholder",
                                        "usItemId": None,
                                        "name": None,
                                        "canonicalUrl": None,
                                        "priceInfo": None,
                                    },
                                ],
                            }
                        ],
                    }
                }
            }
        }
    }


def lider_product(status: str = "IN_STOCK") -> Dict[str, Any]:
    """One ``lider.cl/ip/...`` page, trimmed."""
    return {
        "props": {
            "pageProps": {
                "initialData": {
                    "data": {
                        "product": {
                            "usItemId": "00071171902355",
                            "name": "Consola PS5 Digital + ASTRO BOT y GT7.",
                            "brand": "Sony",
                            "availabilityStatus": status,
                            "orderLimit": 12,
                            "orderMinLimit": 1,
                            "sellerName": "Lider",
                            "shortDescription": "",
                            "imageInfo": {
                                "thumbnailUrl": "https://i5.walmartimages.cl/asr/ps5.jpeg",
                                "allImages": [{"url": "https://i5.walmartimages.cl/asr/ps5.jpeg"}],
                            },
                            "priceInfo": {
                                "currentPrice": {
                                    "price": 629990,
                                    "priceString": "$629.990",
                                    "currencyUnit": "CLP",
                                },
                                "wasPrice": {"price": 779990, "priceString": "$779.990"},
                            },
                        },
                        "idml": {
                            "longDescription": (
                                "<p>La consola PS5 Digital.</p><br>"
                                "<p>Incluye ASTRO BOT y Gran Turismo&nbsp;7.</p>"
                            ),
                            "shortDescription": "Consola PS5 Digital.",
                        },
                    }
                }
            }
        }
    }


# --------------------------------------------------------------------------- #
# Sodimac payloads
# --------------------------------------------------------------------------- #


def _sodimac_market() -> "sodimac.SodimacMarketplace":
    market = sodimac.SodimacMarketplace("sodimac", None)
    market.config = sodimac.SodimacMarketplaceConfig(name="sodimac")
    return market


def sodimac_search_route() -> Dict[str, Any]:
    """A page of ``/search?Ntt=cocina a gas licuado``, trimmed from the live site.

    The route a phrase lands on when the site cannot map it to a category --
    which is most real phrases, and the one this module could not read at all.
    Three things differ from the category route and each one alone is enough to
    lose every product: the results live four keys deeper, the entries carry no
    address, and the price types are shouted.
    """
    return {
        "props": {
            "pageProps": {
                "searchProps": {
                    "searchTerm": "cocina a gas licuado",
                    "searchData": {
                        "pagination": {"count": 123, "perPage": 28, "currentPage": 1},
                        "results": [
                            {
                                "productId": "5787254",
                                "skuId": "5787254",
                                "displayName": "Encimera a gas licuado 5 quemadores",
                                "brand": "Ursus Trotter",
                                "mediaUrls": [
                                    "https://media.falabella.com/sodimacCL/5787254/public"
                                ],
                                "prices": [
                                    {
                                        "label": "",
                                        "type": "INTERNET",
                                        "symbol": "$",
                                        "price": "299.990",
                                        "unit": "C/U",
                                        "priceWithoutFormatting": 299990,
                                    },
                                    {
                                        "label": "Normal",
                                        "type": "NORMAL",
                                        "symbol": "$",
                                        "price": "379.990",
                                        "unit": "C/U",
                                        "priceWithoutFormatting": 379990,
                                    },
                                ],
                            },
                            {
                                "productId": "7417187",
                                "skuId": "7417188",
                                "displayName": "Anafe bajo 2 quemadores gas licuado",
                                "mediaUrls": [],
                                "prices": [
                                    {
                                        "type": "INTERNET",
                                        "symbol": "$",
                                        "price": "59.990",
                                    }
                                ],
                            },
                        ],
                    },
                },
                "query": {"Ntt": "cocina a gas licuado", "tenant": "sodimac-cl"},
            }
        }
    }


def sodimac_search() -> Dict[str, Any]:
    """A page of the ``/lista/...`` route ``?Ntt=taladro`` redirects to, trimmed."""
    return {
        "props": {
            "pageProps": {
                "pagination": {
                    "count": 546,
                    "perPage": 48,
                    "totalPerPage": 56,
                    "currentPage": 1,
                },
                "results": [
                    {
                        "productId": "153777946",
                        "skuId": "153777947",
                        "displayName": (
                            "Kit Taladro Percutor XR 20V+ 2 Baterias 2AH + "
                            "Cargador + 27 Accesorios"
                        ),
                        "brand": "DEWALT",
                        # A sponsored card: the click blob is in the query string.
                        "url": (
                            "https://www.sodimac.cl/sodimac-cl/articulo/153777946/kit-taladro"
                            "?sponsoredClickData=%257B%2522isXLP%2522%253Atrue%257D"
                        ),
                        "mediaUrls": ["https://media.sodimac.cl/1.jpg"],
                        "prices": [
                            {
                                "symbol": "$ ",
                                "type": "internetPrice",
                                "crossed": False,
                                "price": ["149.990"],
                            },
                            {
                                "symbol": "$ ",
                                "type": "normalPrice",
                                "crossed": True,
                                "price": ["219.990"],
                            },
                        ],
                        "sellerId": "SODIMAC_CHILE",
                        "sellerName": "Sodimac",
                    },
                    {
                        "productId": "113960665",
                        "displayName": "Taladro Percutor 650W",
                        "brand": "Bauker",
                        "url": "https://www.sodimac.cl/sodimac-cl/articulo/113960665/taladro",
                        "mediaUrls": ["https://media.sodimac.cl/2.jpg"],
                        # No discount, and a card-only price the user cannot pay
                        # without the shop's credit card.
                        "prices": [
                            {
                                "symbol": "$ ",
                                "type": "normalPrice",
                                "crossed": False,
                                "price": ["29.990"],
                            },
                            {
                                "symbol": "$ ",
                                "type": "cmrPrice",
                                "crossed": False,
                                "price": ["19.990"],
                            },
                        ],
                        "sellerName": "Sodimac",
                    },
                ],
            }
        }
    }


def sodimac_product(
    published: bool = True,
    sellable: bool = True,
    purchaseable: bool = True,
    availability: int = 45,
) -> Dict[str, Any]:
    """One ``sodimac.cl/sodimac-cl/articulo/...`` page, trimmed."""
    return {
        "props": {
            "pageProps": {
                "productData": {
                    "id": "153777946",
                    "name": "Kit Taladro Percutor XR 20V",
                    "brandName": "DEWALT",
                    "description": "<p>Kit Taladro Percutor XR 20V</p>",
                    "longDescription": (
                        "<div>Taladro percutor inalámbrico.</div><li>2 baterías</li>"
                        "<li>Cargador</li>"
                    ),
                    "isPublished": published,
                    "variants": [
                        {
                            "id": "153777947",
                            "isOnlineSellable": sellable,
                            "isPurchaseable": purchaseable,
                            "qtyLimits": {
                                "value": min(availability, 999),
                                "limits": {"seller": 999, "availability": availability},
                            },
                            "medias": [{"url": "https://media.falabella.com/1.jpg"}],
                            "prices": [
                                {
                                    "symbol": "$ ",
                                    "crossed": False,
                                    "type": "internetPrice",
                                    "price": ["149.990"],
                                },
                                {
                                    "symbol": "$ ",
                                    "crossed": True,
                                    "type": "normalPrice",
                                    "price": ["219.990"],
                                },
                            ],
                        }
                    ],
                }
            }
        }
    }


# --------------------------------------------------------------------------- #
# Lider
# --------------------------------------------------------------------------- #


def test_lider_search_yields_the_products() -> None:
    listings = lider.parse_search(lider_search(), "taladro")
    assert [listing.id for listing in listings] == ["00088591179504", "00780467605043"]


def test_lider_drops_the_ads_the_site_mixes_in() -> None:
    # By the site's own `__typename`, not by noticing a missing price: an ad and
    # a product that failed to load look identical from the outside.
    assert all(listing.title for listing in lider.parse_search(lider_search(), "x"))
    assert len(lider.parse_search(lider_search(), "x")) == 2


def test_lider_card_price_is_current_then_struck_through() -> None:
    first = lider.parse_search(lider_search(), "taladro")[0]
    # The shape `extract_price` produces for every other marketplace, so the
    # price parser and the notification card read a shop's discount the same way.
    assert first.price == "$229.990 | $359.990"


def test_lider_card_without_a_discount_reports_one_price() -> None:
    second = lider.parse_search(lider_search(), "taladro")[1]
    assert second.price == "$149.990"


def test_lider_urls_are_absolute() -> None:
    first = lider.parse_search(lider_search(), "taladro")[0]
    assert first.post_url.startswith("https://www.lider.cl/ip/")


def test_lider_search_records_the_seller_and_calls_everything_new() -> None:
    listings = lider.parse_search(lider_search(), "taladro")
    assert [listing.seller for listing in listings] == ["Lider", "nocnoc"]
    assert all(listing.condition == "new" for listing in listings)


def test_a_shop_has_no_location() -> None:
    # Left empty rather than filled in with the country: a location filter that
    # always matches is a filter that lies about having been applied.
    assert all(not listing.location for listing in lider.parse_search(lider_search(), "x"))


def test_lider_product_page() -> None:
    listing = lider.parse_product(lider_product(), "https://www.lider.cl/ip/x/1", "ps5")
    assert listing is not None
    assert listing.id == "00071171902355"
    assert listing.price == "$629.990 | $779.990"
    assert listing.availability == IN_STOCK
    # A ceiling on one order, not an inventory count -- see the module docstring.
    assert listing.stock == "12"


def test_lider_product_description_is_the_long_one_without_markup() -> None:
    listing = lider.parse_product(lider_product(), "https://www.lider.cl/ip/x/1", "ps5")
    assert listing is not None
    assert "La consola PS5 Digital." in listing.description
    assert "<p>" not in listing.description
    assert "&nbsp;" not in listing.description
    # The line structure survives: a specification list flattened into one
    # paragraph is a specification list nobody can read.
    assert "\n" in listing.description


def test_lider_out_of_stock_removes_the_entry() -> None:
    assert lider.product_status(lider_product("OUT_OF_STOCK")) is ListingStatus.GONE


def test_lider_in_stock_keeps_it() -> None:
    assert lider.product_status(lider_product("IN_STOCK")) is ListingStatus.ACTIVE


def test_a_page_with_no_product_is_undecided_not_gone() -> None:
    # What a bot check, a redirect and an outage all look like.  Only evidence
    # deletes.
    assert lider.product_status({}) is ListingStatus.UNKNOWN
    assert lider.product_status({"props": {"pageProps": {}}}) is ListingStatus.UNKNOWN


def test_an_unknown_availability_word_is_undecided() -> None:
    assert lider.product_status(lider_product("PREORDER")) is ListingStatus.UNKNOWN
    listing = lider.parse_product(lider_product("PREORDER"), "https://x/1", "ps5")
    assert listing is not None and listing.availability == ""


def test_lider_search_urls() -> None:
    market = lider.LiderMarketplace("lider", None)
    market.config = lider.LiderMarketplaceConfig(name="lider")
    item = lider.LiderItemConfig(name="ps5", search_phrases=["playstation 5"])
    assert market.search_url("playstation 5", item, 1).endswith("search?query=playstation%205")
    assert market.search_url("playstation 5", item, 3).endswith("&page=3")


def test_lider_recognises_its_own_urls() -> None:
    assert lider.LiderMarketplace.handles_url("https://www.lider.cl/ip/x/1")
    assert not lider.LiderMarketplace.handles_url("https://www.sodimac.cl/x")


def test_lider_payload_that_moved_yields_nothing_rather_than_raising() -> None:
    # A shape change must not take a search down; the call site logs it loudly.
    assert lider.parse_search({"props": {"pageProps": {}}}, "x") == []
    assert lider.parse_product({}, "https://x/1", "x") is None


# --------------------------------------------------------------------------- #
# Sodimac
# --------------------------------------------------------------------------- #


def test_sodimac_search_yields_the_products() -> None:
    listings = sodimac.parse_search(sodimac_search(), "taladro")
    assert [listing.id for listing in listings] == ["153777946", "113960665"]


def test_sodimac_strips_the_sponsored_click_blob() -> None:
    # The same products appear organically, so the parameter is dropped rather
    # than the card, and the two copies collapse on the id.
    first = sodimac.parse_search(sodimac_search(), "taladro")[0]
    assert "sponsoredClickData" not in first.post_url
    assert first.post_url.endswith("/kit-taladro")


def test_sodimac_price_is_the_uncrossed_one_and_the_was_price() -> None:
    first = sodimac.parse_search(sodimac_search(), "taladro")[0]
    assert first.price == "$149.990 | $219.990"


def test_sodimac_never_reports_the_store_card_price() -> None:
    # It needs the shop's own credit card.  Reporting it would tell somebody a
    # drill costs 30% less than they can buy it for.
    second = sodimac.parse_search(sodimac_search(), "taladro")[1]
    assert second.price == "$29.990"


def test_sodimac_product_page() -> None:
    listing = sodimac.parse_product(
        sodimac_product(), "https://www.sodimac.cl/sodimac-cl/articulo/1/x", "taladro"
    )
    assert listing is not None
    assert listing.id == "153777946"
    assert listing.price == "$149.990 | $219.990"
    assert listing.availability == IN_STOCK
    # A real count here, unlike Lider's: what the shop will let into a cart.
    assert listing.stock == "45"


def test_sodimac_stock_comes_from_the_availability_limit_not_the_sellers_cap() -> None:
    # `qtyLimits.value` is the smaller of the two, and the seller's 999 is not
    # a stock level.
    listing = sodimac.parse_product(sodimac_product(availability=3), "https://x/1", "t")
    assert listing is not None and listing.stock == "3"


def test_sodimac_description_prefers_the_long_one_and_keeps_its_lines() -> None:
    listing = sodimac.parse_product(sodimac_product(), "https://x/1", "t")
    assert listing is not None
    assert "Taladro percutor inalámbrico." in listing.description
    assert "2 baterías" in listing.description
    assert "<li>" not in listing.description


@pytest.mark.parametrize(
    "kwargs",
    [
        {"published": False},
        {"sellable": False},
        {"purchaseable": False},
        {"availability": 0},
    ],
)
def test_sodimac_any_refusal_is_out_of_stock(kwargs: Dict[str, Any]) -> None:
    # Three different refusals plus an empty shelf, and any of them means no.
    assert sodimac.product_status(sodimac_product(**kwargs)) is ListingStatus.GONE


def test_sodimac_all_three_flags_and_stock_means_in_stock() -> None:
    assert sodimac.product_status(sodimac_product()) is ListingStatus.ACTIVE


def test_sodimac_saying_nothing_is_not_the_same_as_available() -> None:
    # An entry silently treated as available is one the user is told about and
    # cannot buy.
    payload = {"props": {"pageProps": {"productData": {"id": "1", "name": "x", "variants": []}}}}
    assert sodimac.product_status(payload) is ListingStatus.UNKNOWN
    listing = sodimac.parse_product(payload, "https://x/1", "t")
    assert listing is not None and listing.availability == ""


def test_sodimac_reports_how_many_it_could_not_see() -> None:
    # 546 matched, 56 on the page: the difference is invisible from the results.
    assert sodimac.total_found(sodimac_search()) == 546
    assert len(sodimac.parse_search(sodimac_search(), "x")) == 2


def test_sodimac_reads_the_search_route_as_well() -> None:
    # The bug this file exists to stop coming back: `?Ntt=<multi word>` does not
    # redirect to a category, so the results are four keys deeper and the old
    # path returned [] -- which reads exactly like "Sodimac sells none of it".
    listings = sodimac.parse_search(sodimac_search_route(), "cocina")
    assert [entry.id for entry in listings] == ["5787254", "7417187"]
    assert listings[0].title == "Encimera a gas licuado 5 quemadores"


def test_sodimac_builds_the_address_the_search_route_omits() -> None:
    # The `/search` application builds its links in the browser and puts none of
    # them in the payload.  The slug is deliberately not reproduced: the site
    # redirects on the id alone, verified live with a wrong slug.
    listing = sodimac.parse_search(sodimac_search_route(), "cocina")[0]
    assert listing.post_url == "https://www.sodimac.cl/sodimac-cl/product/5787254/p/5787254/"


def test_sodimac_reads_the_shouted_price_types() -> None:
    # The two routes spell the same three price types differently, and a
    # category entry's rules applied to a search entry yield no price at all.
    listings = sodimac.parse_search(sodimac_search_route(), "cocina")
    assert listings[0].price == "$299.990 | $379.990"
    assert listings[1].price == "$59.990"


def test_sodimac_still_reads_the_category_route() -> None:
    # Both shapes, not one replacing the other: single-word phrases do redirect.
    listings = sodimac.parse_search(sodimac_search(), "taladro")
    assert [entry.id for entry in listings] == ["153777946", "113960665"]


def test_sodimac_counts_the_total_on_either_route() -> None:
    assert sodimac.total_found(sodimac_search()) == 546
    assert sodimac.total_found(sodimac_search_route()) == 123


def test_sodimac_shouts_when_it_can_read_none_of_what_the_site_says_it_has() -> None:
    # A moved key used to return [] and route to the "zero results" break, which
    # is indistinguishable from an empty catalogue.  It must never be quiet
    # again: the caller logs an error with a traceback for this.
    moved = {"props": {"pageProps": {"pagination": {"count": 42}, "results": []}}}
    with pytest.raises(ValueError, match="moved again"):
        _sodimac_market().parse_search(moved, "taladro")


def test_sodimac_is_silent_when_the_site_really_has_nothing() -> None:
    empty = {"props": {"pageProps": {"pagination": {"count": 0}, "results": []}}}
    assert _sodimac_market().parse_search(empty, "xyzzy") == []


def test_the_card_keeps_its_identity_when_the_page_uses_another_id() -> None:
    # Sodimac serves the grid's 5787254 under article 110005070.  If the stored
    # id became the page's, `is_known` would ask about an id nothing was stored
    # under and every product would be new on every pass -- the whole catalogue
    # re-notified, for ever.
    card = Listing(
        marketplace="sodimac",
        name="cocina",
        id="5787254",
        title="Encimera a gas licuado 5 quemadores",
        image="https://media/1.jpg",
        price="$299.990 | $379.990",
        post_url="https://www.sodimac.cl/sodimac-cl/product/5787254/p/5787254/",
        location="",
        seller="Sodimac",
        condition="new",
        description="",
    )
    page = Listing(
        marketplace="sodimac",
        name="cocina",
        id="110005070",
        title="Encimera Gas Licuado 5 Platos Avantgarde GS-75T GL",
        image="",
        price="$299.990",
        post_url=card.post_url,
        location="",
        seller="Sodimac",
        condition="new",
        description="Un texto largo.",
    )
    merged = RetailerMarketplace._merge_card(page, card)
    assert merged.id == "5787254"
    # And everything the page did say still wins.
    assert merged.description == "Un texto largo."
    assert merged.title.endswith("GS-75T GL")


def test_sodimac_asks_for_the_page_it_wants() -> None:
    # This used to assert the opposite, on the strength of the `/lista` route,
    # which does answer `currentPage: 1` however it is asked.  The `/search`
    # route -- the one every multi-word phrase lands on -- honours the
    # parameter: verified live, page 2 comes back with 28 different products.
    market = _sodimac_market()
    item = sodimac.SodimacItemConfig(name="t", search_phrases=["taladro"])
    assert market.search_url("taladro", item, 1).endswith("search?Ntt=taladro")
    assert market.search_url("taladro", item, 2).endswith("&currentpage=2")


def test_sodimac_recognises_its_own_urls() -> None:
    assert sodimac.SodimacMarketplace.handles_url("https://www.sodimac.cl/sodimac-cl/articulo/1/x")
    assert not sodimac.SodimacMarketplace.handles_url("https://www.lider.cl/ip/x/1")


def test_sodimac_payload_that_moved_yields_nothing_rather_than_raising() -> None:
    assert sodimac.parse_search({"props": {"pageProps": {}}}, "x") == []
    assert sodimac.parse_product({}, "https://x/1", "x") is None


# --------------------------------------------------------------------------- #
# Filtering, which is the base class's job for both shops
# --------------------------------------------------------------------------- #


def _market(price: str = "$149.990", **item_kwargs: Any):
    market = lider.LiderMarketplace("lider", None)
    market.config = lider.LiderMarketplaceConfig(name="lider")
    item = lider.LiderItemConfig(name="t", search_phrases=["taladro"], **item_kwargs)
    listing = Listing(
        marketplace="lider",
        name="t",
        id="1",
        title="Taladro percutor",
        image="",
        price=price,
        post_url="https://www.lider.cl/ip/x/1",
        location="",
        seller="Lider",
        condition="new",
        description="",
    )
    return market, item, listing


def test_price_bounds_are_applied_to_the_results() -> None:
    # Applied here rather than sent to the shop: neither site's price-facet URL
    # grammar was verified, and a parameter a site silently ignores is a filter
    # that looks like it works.
    market, item, listing = _market(max_price="100000")
    assert not market.check_listing(listing, item)

    market, item, listing = _market(min_price="100000")
    assert market.check_listing(listing, item)

    market, item, listing = _market(min_price="200000")
    assert not market.check_listing(listing, item)


def test_a_price_that_cannot_be_read_passes_the_bounds() -> None:
    market, item, listing = _market(price="Consultar", max_price="100000")
    assert market.check_listing(listing, item)


def test_junk_prices_are_excluded_before_the_bounds() -> None:
    market, item, listing = _market(
        price="$999.999", excluded_price_patterns=["9*"], min_price="1"
    )
    assert not market.check_listing(listing, item)


def test_out_of_stock_only_excludes_when_asked() -> None:
    market, item, listing = _market()
    listing.availability = OUT_OF_STOCK
    assert market.check_listing(listing, item)

    market, item, listing = _market(in_stock_only=True)
    listing.availability = OUT_OF_STOCK
    assert not market.check_listing(listing, item)


def test_an_entry_the_site_said_nothing_about_is_not_out_of_stock() -> None:
    # Otherwise `in_stock_only` would empty the results of a shop that publishes
    # availability on the product page only.
    market, item, listing = _market(in_stock_only=True)
    listing.availability = ""
    assert market.check_listing(listing, item)


def test_excluded_sellers_work_on_a_shops_marketplace_sellers() -> None:
    market, item, listing = _market(exclude_sellers=["nocnoc"])
    listing.seller = "nocnoc"
    assert not market.check_listing(listing, item)


def test_a_retailer_ignores_the_location_options_it_cannot_honour() -> None:
    item = lider.LiderItemConfig(
        name="t", search_phrases=["taladro"], search_city=["santiago"]
    )
    assert item.search_city == ["santiago"]
    # Accepted by the loader and ignored at search time: a config shared between
    # Facebook and a shop must not fail to load because of a Facebook option.
    lider.LiderMarketplace.validate_item_config(item, lider.LiderMarketplaceConfig(name="l"))


def test_max_pages_must_be_a_positive_integer() -> None:
    for bad in (0, -1, "two", True):
        with pytest.raises(ValueError):
            lider.LiderItemConfig(name="t", search_phrases=["x"], max_pages=bad)


def test_in_stock_only_must_be_a_boolean() -> None:
    with pytest.raises(ValueError):
        lider.LiderItemConfig(name="t", search_phrases=["x"], in_stock_only="yes")


def test_market_type_must_match_the_section() -> None:
    with pytest.raises(ValueError):
        lider.LiderMarketplaceConfig(name="lider", market_type="sodimac")
    with pytest.raises(ValueError):
        sodimac.SodimacMarketplaceConfig(name="sodimac", market_type="lider")


# --------------------------------------------------------------------------- #
# The shared payload reader
# --------------------------------------------------------------------------- #


def test_next_data_is_read_out_of_a_served_page() -> None:
    html = (
        '<html><body><div>x</div>'
        '<script id="__NEXT_DATA__" type="application/json">{"a": {"b": 1}}</script>'
        "</body></html>"
    )
    assert from_html(html) == {"a": {"b": 1}}


def test_a_page_with_no_payload_reads_as_nothing() -> None:
    # A sign-in wall, an error page and a bot check all look like this.
    assert from_html("<html><body>Acceso denegado</body></html>") is None
    assert from_html("") is None


def test_a_payload_that_is_not_json_reads_as_nothing() -> None:
    assert from_html('<script id="__NEXT_DATA__">not json</script>') is None


def test_dig_stops_at_the_first_missing_key() -> None:
    assert dig({"a": {"b": {"c": 1}}}, "a", "b", "c") == 1
    assert dig({"a": {"b": {}}}, "a", "b", "c") is None
    # A list in the middle of the path is what makes a chain of `.get` raise.
    assert dig({"a": [1, 2]}, "a", "b") is None


def test_dig_list_treats_missing_and_not_a_list_the_same() -> None:
    assert dig_list({"a": [1]}, "a") == [1]
    assert dig_list({"a": None}, "a") == []
    assert dig_list({}, "a", "b") == []


def test_text_of_flattens_what_the_shops_send() -> None:
    assert text_of(None) == ""
    assert text_of("  x  ") == "x"
    assert text_of(12) == "12"
    # A boolean is not a value anybody wants rendered into a listing, and in
    # Python it would otherwise pass the number check.
    assert text_of(True) == ""


def test_first_text_takes_the_first_field_that_says_something() -> None:
    assert first_text({"a": "", "b": "x"}, "a", "b") == "x"
    assert first_text({}, "a") == ""


def test_joined_price_is_the_monitors_own_shape() -> None:
    assert joined_price("$1", "$2") == "$1 | $2"
    assert joined_price("$1", "") == "$1"
    assert joined_price("", "$2") == "$2"
    # The same number twice is one price, not a discount of zero.
    assert joined_price("$1", "$1") == "$1"


def test_strip_html_keeps_the_lines() -> None:
    assert strip_html("<p>a</p><p>b</p>") == "a\nb"
    assert strip_html("a<br>b") == "a\nb"
    assert strip_html("<b>a</b>&amp;b") == "a &b"
    assert strip_html(None) == ""


# --------------------------------------------------------------------------- #
# A shop is asked for, never assumed
# --------------------------------------------------------------------------- #


def _config(text: str, tmp_path):
    from ai_marketplace_monitor.config import Config

    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return Config([path])


SEARCH = """
[marketplace.facebook]
search_city = 'santiago'

[item.ps5]
search_phrases = 'playstation 5'
"""


def test_an_existing_search_does_not_start_using_the_shops(tmp_path) -> None:
    # The whole reason `opt_in` exists.  Two new platforms arriving in an
    # upgrade must not turn every search for a used console into a page of
    # retail boxes at list price -- delivered as notifications.
    config = _config(SEARCH, tmp_path)
    assert sorted(name for name, _item in config.items) == ["facebook", "mercadolibre"]


def test_the_shops_still_exist_as_platforms(tmp_path) -> None:
    # Not searched is not the same as not there: a stored Lider listing is still
    # re-checked, and its session can still be imported.
    config = _config(SEARCH, tmp_path)
    assert "lider" in config.marketplace
    assert "sodimac" in config.marketplace


def test_a_search_that_asks_for_a_shop_gets_it(tmp_path) -> None:
    # Asking is having a `[item.<name>.<shop>]` section, which is exactly what
    # the interface writes when the platform is switched on -- so nothing has to
    # say `enabled = true` as well.
    config = _config(SEARCH + "\n[item.ps5.lider]\nmax_pages = 2\n", tmp_path)
    assert ("lider", "ps5") in config.items
    assert config.items[("lider", "ps5")].max_pages == 2
    assert ("sodimac", "ps5") not in config.items


def test_a_shop_switched_off_in_its_own_section_is_still_off(tmp_path) -> None:
    config = _config(SEARCH + "\n[item.ps5.lider]\nenabled = false\n", tmp_path)
    built = config.items.get(("lider", "ps5"))
    assert built is not None and built.enabled is False
