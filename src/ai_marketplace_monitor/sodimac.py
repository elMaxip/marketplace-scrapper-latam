"""Sodimac (sodimac.cl), read through its own page payload.

Sodimac runs on Falabella's platform.  Everything below was read off the live
site rather than guessed:

**``?Ntt=<phrase>`` lands on one of two different pages, and they do not share
a payload.**  This is the single most important thing about this file, it is
why the scraper found nothing for two months, and it is not visible from one
page:

* A phrase the site can map to a category **redirects**.  ``?Ntt=taladro`` sends
  the browser to ``/lista/cat14080023/Taladros`` (Next page
  ``/category/[[...slug]]``), whose results are ``props.pageProps.results``.
* A phrase it cannot **stays put**.  ``?Ntt=cocina a gas licuado`` is served by
  ``/search`` itself, whose results are
  ``props.pageProps.searchProps.searchData.results``.

Both were read off the live site.  The module was written against the first one
alone, so every multi-word phrase read a key that was not there, got ``[]``
back, and reported "0 results" -- which looks exactly like a shop that sells
none of it.  :func:`parse_search` therefore reads both, and
:func:`ai_marketplace_monitor.sodimac.SodimacMarketplace.parse_search` shouts
when the site says there are matches and this file could not read any, because
that is the shape moving again and it must never be quiet a second time.

**The two routes carry different entries, too**, which is the other half of the
trap -- a category entry parsed with a search entry's rules yields a listing
with no price:

* Category entries have ``url`` (absolute) and Falabella-style prices:
  ``type: "internetPrice"``, ``crossed: true/false``, ``price`` a *list*.
* Search entries have **no address at all** and shouted price types:
  ``type: "INTERNET" / "NORMAL"``, ``price`` a plain string, no ``crossed``.

For the addresses, see :func:`_product_url`: the site builds them in the
browser, and the id is enough.

* Product: ``props.pageProps.productData``, with the sellable facts one level
  further down in ``variants[0]``.  Unchanged, and shared by both routes -- the
  redirect lands on the same ``/articulo/...`` page either way.

**Pagination exists on one route only.**  ``?currentpage=2`` really does serve
the next 28 entries on ``/search``; on ``/lista`` it is accepted and ignored,
and the payload comes back ``currentPage: 1`` every time.  Both were verified.
So the parameter is sent and the duplicate-URL guard in
:mod:`ai_marketplace_monitor.retailer` absorbs the route that ignores it.
``pagination.count`` is the honest total and is logged.

The prices are a list of labelled entries rather than two fields.  The one that
counts is the entry with ``crossed: false``; the crossed one is the "before"
price.  There is usually also a ``cmrPrice`` -- the store card's price, which
requires that card -- and taking the cheapest of the list would quietly report a
number most people cannot pay.

Stock is real here, unlike on Lider: ``variants[0].qtyLimits.limits.availability``
is how many the shop will actually let into a cart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, Type
from urllib.parse import quote, urlparse

from .listing import Listing
from .marketplace import ItemConfig, ListingStatus
from .nextdata import dig, dig_list, first_text, joined_price, strip_html, text_of
from .retailer import (
    IN_STOCK,
    OUT_OF_STOCK,
    RetailerItemConfig,
    RetailerMarketplace,
    RetailerMarketplaceConfig,
)
from .utils import hilight

HOST = "https://www.sodimac.cl"
SITE = "sodimac-cl"

#: The price entry that is what the thing costs, in the order to prefer.
#:
#: ``internetPrice`` is the online price and the one the site shows largest.
#: ``normalPrice`` is the list price, crossed out when there is a discount and
#: the only entry when there is not.  ``cmrPrice`` is deliberately **not** here:
#: it needs the shop's own credit card, and reporting it as the price would tell
#: somebody a drill costs 30% less than they can buy it for.
PRICE_TYPES = ("internetPrice", "normalPrice", "eventPrice")

#: The entry that is the "before" price, when there is one.
CROSSED_TYPES = ("normalPrice", "listPrice")

#: The same three, as the ``/search`` route spells them.
#:
#: Not a second vocabulary anybody chose: the two routes are two applications
#: that happen to share a domain, and this one shouts its price types.  Folded
#: to the names above rather than duplicating every rule that reads them --
#: including the one that matters, which is that ``cmrPrice`` is not in either
#: list and never becomes the price.
PRICE_TYPE_ALIASES = {
    "INTERNET": "internetPrice",
    "NORMAL": "normalPrice",
    "EVENT": "eventPrice",
    "CMR": "cmrPrice",
    "LIST": "listPrice",
}


@dataclass
class SodimacMarketplaceConfig(RetailerMarketplaceConfig):
    """The ``[marketplace.sodimac]`` section."""

    market_type: str | None = "sodimac"

    def handle_market_type(self: "SodimacMarketplaceConfig") -> None:
        if self.market_type is None:
            return
        if not isinstance(self.market_type, str) or self.market_type.lower() != "sodimac":
            raise ValueError(f"Marketplace {hilight(self.name)} market_type must be sodimac.")


@dataclass
class SodimacItemConfig(RetailerItemConfig):
    """The ``[item.<name>.sodimac]`` section."""


# --------------------------------------------------------------------------- #
# The parsers -- pure functions of a payload
# --------------------------------------------------------------------------- #


def _price_text(entry: Any) -> str:
    """One price entry as text, symbol included.

    ``price`` is a *list* of strings -- the site uses it for ranges ("desde X
    hasta Y") -- and the first element is the number to show.  The symbol is a
    separate field with a trailing space in it, which is stripped: the monitor
    stores what the shop printed, and the shop prints "$ 149.990" with the space
    only because of how it lays the two out.
    """
    if not isinstance(entry, dict):
        return ""
    values = entry.get("price")
    number = ""
    if isinstance(values, list) and values:
        number = text_of(values[0])
    elif values is not None:
        number = text_of(values)
    if not number:
        return ""
    symbol = text_of(entry.get("symbol"))
    return f"{symbol}{number}" if symbol else number


def _price_type(entry: Any) -> str:
    """A price entry's type in one vocabulary, whichever route it came from."""
    if not isinstance(entry, dict):
        return ""
    kind = text_of(entry.get("type"))
    return PRICE_TYPE_ALIASES.get(kind.upper(), kind)


def _prices_of(node: Any) -> str:
    """A price list as the one string the monitor stores.

    The uncrossed entry is what it costs; the crossed one is the "before".
    Falling back to the type order when nothing is flagged, because a product
    with no discount has one entry and no flags worth reading -- and because
    the ``/search`` route flags nothing at all, so for its entries the type
    order is the only thing there is to go on.
    """
    entries = node if isinstance(node, list) else []
    by_type = {_price_type(e): e for e in entries if isinstance(e, dict)}

    current = ""
    for entry in entries:
        if isinstance(entry, dict) and not entry.get("crossed"):
            if _price_type(entry) in PRICE_TYPES:
                current = _price_text(entry)
                break
    if not current:
        for kind in PRICE_TYPES:
            current = _price_text(by_type.get(kind))
            if current:
                break

    was = ""
    for entry in entries:
        if isinstance(entry, dict) and entry.get("crossed"):
            if _price_type(entry) in CROSSED_TYPES:
                was = _price_text(entry)
                break
    # No `crossed` flag anywhere means the `/search` route, where the "before"
    # price is simply the normal one when an internet price undercuts it.
    if not was and current:
        normal = _price_text(by_type.get("normalPrice"))
        if normal and normal != current and by_type.get("internetPrice") is not None:
            was = normal
    return joined_price(current, was)


def _clean_url(url: Any) -> str:
    """A product address without its query string.

    Sponsored cards carry a ``sponsoredClickData`` blob there.  The same
    products also appear organically, so the parameter is dropped rather than
    the card: the address without it is the real one, and two copies of the same
    entry collapse into one on the id.
    """
    text = text_of(url).split("?")[0]
    if not text:
        return ""
    if text.startswith("http"):
        return text
    return f"{HOST}/{text.lstrip('/')}"


def _product_url(entry: Dict[str, Any]) -> str:
    """A search-route entry's address, which the payload does not carry.

    The ``/search`` application builds its links in the browser and puts none of
    them in the payload, so the address is composed here.  The slug in the
    middle is **not** looked up and does not need to be: the site answers
    ``/sodimac-cl/product/<id>/p/<sku>/`` and redirects to the canonical
    ``/articulo/<other id>/<Name>/<variant>`` -- verified against the live site
    with a deliberately wrong slug.  So this reproduces no naming rules that the
    next deployment can change; it uses the two ids the payload already gives.
    """
    product_id = text_of(entry.get("productId"))
    sku_id = text_of(entry.get("skuId")) or product_id
    if not product_id:
        return ""
    return f"{HOST}/{SITE}/product/{product_id}/p/{sku_id}/"


def _results_of(payload: Dict[str, Any]) -> List[Any]:
    """The catalogue entries, from whichever of the two routes served the page.

    Both are asked for, in the order that costs nothing: a page has one or the
    other, never both.  See the module docstring for why there are two.
    """
    found = dig_list(payload, "props", "pageProps", "results")
    if found:
        return found
    return dig_list(
        payload, "props", "pageProps", "searchProps", "searchData", "results"
    )


def parse_search(payload: Dict[str, Any], item_name: str) -> List[Listing]:
    """Every product on one results page, as listings."""
    results = _results_of(payload)
    listings: List[Listing] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        listing_id = first_text(entry, "productId", "skuId")
        # The category route publishes the address; the search route does not.
        url = _clean_url(entry.get("url")) or _product_url(entry)
        title = text_of(entry.get("displayName"))
        if not listing_id or not url or not title:
            continue
        media = dig_list(entry, "mediaUrls")
        listings.append(
            Listing(
                marketplace="sodimac",
                name=item_name,
                id=listing_id,
                title=title,
                image=text_of(media[0]) if media else "",
                price=_prices_of(entry.get("prices")),
                post_url=url,
                location="",
                seller=first_text(entry, "sellerName", "sellerId"),
                condition="new",
                # The card has no description at all: the field exists on the
                # product page only, which is why the search opens it.
                description="",
                stock="",
                availability="",
            )
        )
    return listings


def total_found(payload: Dict[str, Any]) -> int:
    """How many results the site says there are, against the page's fifty-odd.

    Worth reporting because the two differ by a lot and only one of them is
    visible: a phrase matching 546 products looks, from the results alone, like
    a phrase matching 56.
    """
    count = dig(payload, "props", "pageProps", "pagination", "count")
    if not isinstance(count, int):
        count = dig(
            payload, "props", "pageProps", "searchProps", "searchData", "pagination", "count"
        )
    return count if isinstance(count, int) else 0


def _variant(product: Dict[str, Any]) -> Dict[str, Any]:
    """The variant a product page is showing.

    The first one.  A product with several (a drill in three voltages) shows one
    of them, and the payload does not mark which -- so the first is used, and it
    is the one whose price and stock the page is displaying.
    """
    variants = product.get("variants")
    if isinstance(variants, list) and variants and isinstance(variants[0], dict):
        return variants[0]
    return {}


def _stock_of(variant: Dict[str, Any]) -> str:
    """How many the shop will let into a cart.

    ``qtyLimits.limits.availability`` and not ``qtyLimits.value``: the value is
    the smaller of the availability limit and the seller's own per-order cap
    (999 for the shop itself), so on a product with plenty of stock they agree
    and on one the *seller* caps they do not -- and the seller's cap is not a
    stock level.
    """
    limit = dig(variant, "qtyLimits", "limits", "availability")
    if isinstance(limit, bool) or not isinstance(limit, (int, float)):
        return ""
    return str(int(limit))


def _availability_of(product: Dict[str, Any], variant: Dict[str, Any]) -> str:
    """Whether Sodimac can sell this right now.

    Three facts have to agree, and they are three different refusals: the
    product can be unpublished, the variant can be off sale online, and the
    variant can be out of stock while still listed.  Any of them means no.

    A payload that says none of the three reads as "did not say" rather than as
    in stock: an entry silently treated as available is one the user is told
    about and cannot buy.
    """
    published = product.get("isPublished")
    sellable = variant.get("isOnlineSellable")
    purchaseable = variant.get("isPurchaseable")
    known = [flag for flag in (published, sellable, purchaseable) if isinstance(flag, bool)]
    if not known:
        return ""
    if all(known):
        stock = _stock_of(variant)
        # Present and zero is out of stock; absent says nothing either way.
        return OUT_OF_STOCK if stock == "0" else IN_STOCK
    return OUT_OF_STOCK


def parse_product(payload: Dict[str, Any], url: str, item_name: str) -> Listing | None:
    """One product page as a listing, or None when there is no product on it."""
    product = dig(payload, "props", "pageProps", "productData")
    if not isinstance(product, dict):
        return None
    listing_id = text_of(product.get("id"))
    title = text_of(product.get("name"))
    if not listing_id or not title:
        return None

    variant = _variant(product)
    medias = variant.get("medias") or variant.get("media") or product.get("mediaList") or []
    image = ""
    if isinstance(medias, list) and medias:
        first = medias[0]
        image = text_of(first.get("url")) if isinstance(first, dict) else text_of(first)

    description = ""
    for value in (product.get("longDescription"), product.get("description")):
        description = strip_html(value)
        if description:
            break

    return Listing(
        marketplace="sodimac",
        name=item_name,
        id=listing_id,
        title=title,
        image=image,
        price=_prices_of(variant.get("prices") or product.get("prices")),
        post_url=url.split("?")[0],
        location="",
        seller=first_text(product, "sellerName", "sellerId") or "Sodimac",
        condition="new",
        description=description,
        stock=_stock_of(variant),
        availability=_availability_of(product, variant),
    )


def product_status(payload: Dict[str, Any]) -> ListingStatus:
    """Whether Sodimac still sells this.

    Only an explicit "no" removes the entry.  A page with no product data is a
    bot check, a redirect or an outage, and none of those is the shop saying it
    stopped stocking a drill.
    """
    product = dig(payload, "props", "pageProps", "productData")
    if not isinstance(product, dict):
        return ListingStatus.UNKNOWN
    availability = _availability_of(product, _variant(product))
    if availability == OUT_OF_STOCK:
        return ListingStatus.GONE
    if availability == IN_STOCK:
        return ListingStatus.ACTIVE
    return ListingStatus.UNKNOWN


# --------------------------------------------------------------------------- #
# The marketplace
# --------------------------------------------------------------------------- #


class SodimacMarketplace(RetailerMarketplace):
    name = "sodimac"
    label = "Sodimac"
    hosts = ("sodimac.cl",)
    home_url = HOST
    #: Deliberately empty: nothing here needs an account.  The catalogue, the
    #: prices and the stock are all public, and the stored session is a
    #: Cloudflare clearance rather than a login -- so "signed out" would be an
    #: alarm about a state that is entirely normal.
    session_cookies = ()

    @classmethod
    def get_config(cls: Type["SodimacMarketplace"], **kwargs: Any) -> SodimacMarketplaceConfig:
        return SodimacMarketplaceConfig(**kwargs)

    @classmethod
    def get_item_config(cls: Type["SodimacMarketplace"], **kwargs: Any) -> SodimacItemConfig:
        return SodimacItemConfig(**kwargs)

    @classmethod
    def item_config_class(cls: Type["SodimacMarketplace"]) -> Type[SodimacItemConfig]:
        return SodimacItemConfig

    @classmethod
    def session_domains(cls: Type["SodimacMarketplace"]) -> Tuple[str, ...]:
        return ("sodimac.cl", "falabella.com")

    @classmethod
    def handles_url(cls: Type["SodimacMarketplace"], url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return host == "sodimac.cl" or host.endswith(".sodimac.cl")

    def search_url(
        self: "SodimacMarketplace", phrase: str, item_config: ItemConfig, page: int = 1
    ) -> str:
        """Where the results for one phrase are.

        ``currentpage`` is sent because on the ``/search`` route it works -- 28
        more entries a page -- and left harmless on the ``/lista`` route, which
        accepts it and answers ``currentPage: 1`` anyway.  The duplicate-URL
        guard in :class:`~ai_marketplace_monitor.retailer.RetailerMarketplace`
        absorbs that, so the cost of the route that ignores it is one page load
        and no wrong results.  See the module docstring.
        """
        url = f"{HOST}/{SITE}/search?Ntt={quote(phrase)}"
        return url if page <= 1 else f"{url}&currentpage={page}"

    def parse_search(
        self: "SodimacMarketplace", payload: Dict[str, Any], item_name: str
    ) -> List[Listing]:
        listings = parse_search(payload, item_name)
        total = total_found(payload)
        if not listings and total > 0:
            # The site says it has matches and nothing here could read one.
            # That is the payload's shape having moved, and it must be loud:
            # quietly returning an empty list is exactly how this scraper
            # reported "Sodimac sells no cocinas" for two months.
            raise ValueError(
                f"Sodimac says {total} products match but none could be read from the "
                "payload -- the results have moved again. See the module docstring for "
                "the two shapes this file knows."
            )
        if self.logger and total > len(listings):
            self.logger.debug(
                f"""{hilight("[Search]", "info")} Sodimac says {total} products match; """
                f"""{len(listings)} came with this page."""
            )
        return listings

    def parse_product(
        self: "SodimacMarketplace", payload: Dict[str, Any], url: str, item_name: str
    ) -> Listing | None:
        return parse_product(payload, url, item_name)

    def product_status(
        self: "SodimacMarketplace", payload: Dict[str, Any]
    ) -> ListingStatus:
        return product_status(payload)
