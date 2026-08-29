"""Lider (lider.cl), read through its own page payload.

Lider runs on Walmart's platform, which shows in the payload: the search
results live under ``itemStacks``, an entry is a ``usItemId``, and the price
object has twenty-five keys of which three matter.  All of it was read off the
live site, not guessed:

* Search: ``https://www.lider.cl/search?query=<phrase>&page=<n>``, whose payload
  is ``props.pageProps.initialData.searchResult``.
* Results: ``itemStacks[0].items``, which is **not** all products -- the site
  mixes ``AdPlaceholder`` entries into the same array, and they have no price,
  no id and no URL.  They are dropped by ``__typename``, which is the site's
  own label for them rather than a guess from a missing field.
* Pagination: ``paginationV2.maxPage``.  ``&page=2`` really does serve different
  items; verified by comparing the first entry of two pages.
* Product: ``props.pageProps.initialData.data.product``, with the long
  description under ``...data.idml.longDescription``.

What Lider gives that a marketplace does not: ``availabilityStatus``
(``IN_STOCK`` / ``OUT_OF_STOCK``), which is a real answer to "can I buy this
right now?" rather than an inference from a listing disappearing.

What it does *not* give, and is worth being precise about: an inventory count.
``orderLimit`` is the most you may put in one order -- twelve for a console
whether the warehouse holds twelve or twelve hundred.  It is stored as the
stock figure because it is the only quantity the site publishes and it does
fall to zero when the item runs out, but it is a ceiling, not a count, and
nothing here pretends otherwise.

Lider's bot wall -- read this before changing anything below
------------------------------------------------------------

**Lider is behind PerimeterX (HUMAN), and it refuses this scraper roughly half
the time.**  That is the normal state of affairs, it is not a regression, and
two separate logs from the same afternoon -- same code, same phrases, same
browser -- contain both outcomes interleaved.  Anyone who reads "Lider found
nothing" and starts changing selectors is about to rewrite a parser that works:
when the page *is* served, it parses forty-eight results without complaint.

How to tell which one you are looking at:

* ``Lider did not serve its results`` -- the wall.  The interstitial carries no
  ``__NEXT_DATA__``, so :func:`~ai_marketplace_monitor.nextdata.from_page`
  returns None and there is nothing to parse.
* ``Could not read Lider's results`` -- the payload came and its shape moved.
  *That* is a parser problem.

The cookies that decide it live in the browser profile, not in the saved
session: ``_px3`` is a short-lived clearance token and ``_pxvid`` is a device
id that accumulates a reputation.  Three consequences, and each one is load
bearing:

1. **A fresh profile seeded from the stored session is the reliable way back
   in.**  It arrives with the account cookies and a clean device id, which is
   exactly the state the one log that worked start to finish began in.
2. **:meth:`RetailerMarketplace.save_session` exists for this.**  It writes the
   shop's own cookies -- clearance included -- so the next profile does not
   start from being challenged again.  Deleting that call because "the session
   file is only for logins" would quietly undo it.
3. **The fallback in
   :meth:`~ai_marketplace_monitor.retailer.RetailerMarketplace.get_listing_details`
   is not defensive clutter.**  When a *product* page is walled, the search
   card is returned instead of raising, which is why listings still come back
   with a title, a price and an image on a half-refused pass.  It is the only
   reason anything at all was salvaged from Lider before this.

What is deliberately *not* here: anything that answers the challenge.  The wall
is a press-and-hold button, and holding it with a synthetic mouse event is
circumvention -- brittle, detectable, and a decision that is the operator's to
make rather than this file's.  What the scraper does instead is recognise the
refusal, stop asking for a while (:meth:`blocked_reason`), and let the operator
solve it once in the browser it can see, after which the clearance is saved and
reused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, Type
from urllib.parse import quote, urlparse

from .extract import looks_blocked
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

HOST = "https://www.lider.cl"

#: Entries the site mixes into the results that are not products.  Dropped by
#: the site's own label rather than by noticing they have no price: an ad and a
#: product that failed to load look identical from the outside, and only one of
#: them is worth a warning.
NON_PRODUCT_TYPENAMES = ("AdPlaceholder",)

#: ``availabilityStatus`` -> what the monitor calls it.  Anything else -- and
#: the field is absent on search cards entirely -- reads as "the site did not
#: say", which is not the same as out of stock.
AVAILABILITY = {
    "IN_STOCK": IN_STOCK,
    "OUT_OF_STOCK": OUT_OF_STOCK,
}


@dataclass
class LiderMarketplaceConfig(RetailerMarketplaceConfig):
    """The ``[marketplace.lider]`` section."""

    market_type: str | None = "lider"

    def handle_market_type(self: "LiderMarketplaceConfig") -> None:
        if self.market_type is None:
            return
        if not isinstance(self.market_type, str) or self.market_type.lower() != "lider":
            raise ValueError(f"Marketplace {hilight(self.name)} market_type must be lider.")


@dataclass
class LiderItemConfig(RetailerItemConfig):
    """The ``[item.<name>.lider]`` section."""


# --------------------------------------------------------------------------- #
# The parsers -- pure functions of a payload
# --------------------------------------------------------------------------- #


def _product_url(canonical: Any) -> str:
    """A catalogue entry's address, absolute and without its query string."""
    path = text_of(canonical).split("?")[0]
    if not path:
        return ""
    if path.startswith("http"):
        return path
    return f"{HOST}/{path.lstrip('/')}"


def _card_price(item: Dict[str, Any]) -> str:
    """A search card's price as the monitor stores it.

    ``linePrice`` is what it costs today and ``wasPrice`` is what the shop is
    showing crossed out.  ``itemPrice`` is the same number as ``wasPrice`` on
    every discounted entry seen and the only one present on entries that are
    not discounted, which is why it is the fallback rather than the first
    choice: taking it first would report the pre-discount price as the price.
    """
    info = item.get("priceInfo")
    if not isinstance(info, dict):
        return ""
    current = text_of(info.get("linePrice"))
    was = text_of(info.get("wasPrice"))
    if not current:
        current = text_of(info.get("itemPrice"))
        was = ""
    return joined_price(current, was)


def parse_search(payload: Dict[str, Any], item_name: str) -> List[Listing]:
    """Every product on one results page, as listings.

    Raises nothing of its own: a payload whose shape moved produces an empty
    list here and a loud log line at the call site, because a shape change and
    "this shop sells none of that" must not look the same to the user.
    """
    stacks = dig_list(payload, "props", "pageProps", "initialData", "searchResult", "itemStacks")
    listings: List[Listing] = []
    for stack in stacks:
        for item in stack.get("items", []) if isinstance(stack, dict) else []:
            if not isinstance(item, dict):
                continue
            if text_of(item.get("__typename")) in NON_PRODUCT_TYPENAMES:
                continue
            listing_id = text_of(item.get("usItemId")) or text_of(item.get("id"))
            url = _product_url(item.get("canonicalUrl"))
            title = text_of(item.get("name"))
            if not listing_id or not url or not title:
                continue
            listings.append(
                Listing(
                    marketplace="lider",
                    name=item_name,
                    id=listing_id,
                    title=title,
                    image=text_of(dig(item, "imageInfo", "thumbnailUrl")),
                    price=_card_price(item),
                    post_url=url,
                    # A shop has no location.  Left empty rather than filled in
                    # with the country: a location filter that always matches is
                    # a filter that lies about having been applied.
                    location="",
                    seller=first_text(item, "sellerName", "sellerId"),
                    # Everything a shop sells is new.
                    condition="new",
                    description=text_of(item.get("shortDescription")),
                    # The search payload carries neither, on any entry seen.
                    stock="",
                    availability="",
                )
            )
    return listings


def _product_price(product: Dict[str, Any]) -> str:
    """The product page's price.

    A different shape from the card's: nested objects with both a number and a
    formatted string.  The string is used, because the monitor stores prices
    exactly as the shop printed them -- the number is what
    :func:`~ai_marketplace_monitor.utils.price_value` works out again when it
    needs one.
    """
    info = product.get("priceInfo")
    if not isinstance(info, dict):
        return ""
    current = text_of(dig(info, "currentPrice", "priceString"))
    was = text_of(dig(info, "wasPrice", "priceString"))
    return joined_price(current, was)


def _description(payload: Dict[str, Any], product: Dict[str, Any]) -> str:
    """The seller's own text, longest version first.

    ``idml.longDescription`` is HTML and is the real description; the two short
    ones are usually the title again, which is why they are only a fallback.
    """
    idml = dig(payload, "props", "pageProps", "initialData", "data", "idml") or {}
    for value in (
        idml.get("longDescription") if isinstance(idml, dict) else None,
        idml.get("shortDescription") if isinstance(idml, dict) else None,
        product.get("shortDescription"),
    ):
        text = strip_html(value)
        if text:
            return text
    return ""


def parse_product(payload: Dict[str, Any], url: str, item_name: str) -> Listing | None:
    """One product page as a listing, or None when there is no product on it."""
    product = dig(payload, "props", "pageProps", "initialData", "data", "product")
    if not isinstance(product, dict):
        return None
    listing_id = text_of(product.get("usItemId")) or text_of(product.get("id"))
    title = text_of(product.get("name"))
    if not listing_id or not title:
        return None

    status = text_of(product.get("availabilityStatus")).upper()
    images = dig_list(product, "imageInfo", "allImages")
    image = text_of(dig(product, "imageInfo", "thumbnailUrl"))
    if not image and images:
        image = text_of(images[0].get("url")) if isinstance(images[0], dict) else ""

    return Listing(
        marketplace="lider",
        name=item_name,
        id=listing_id,
        title=title,
        image=image,
        price=_product_price(product),
        post_url=url.split("?")[0],
        location="",
        seller=first_text(product, "sellerName", "sellerId"),
        condition="new",
        description=_description(payload, product),
        # A ceiling on one order, not an inventory count -- see the module
        # docstring.  Stored because it is the only quantity Lider publishes.
        stock=text_of(product.get("orderLimit")),
        availability=AVAILABILITY.get(status, ""),
    )


def total_pages(payload: Dict[str, Any]) -> int | None:
    """The number of results pages, or None when the payload does not say.

    ``None`` rather than 1 for a missing key: "the shop did not tell us" and
    "there is one page" lead to different behaviour, and guessing the second
    would stop every search after its first page -- which is the bug this whole
    change exists to remove.
    """
    value = dig(payload, "props", "pageProps", "initialData", "searchResult", "paginationV2", "maxPage")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    pages = int(value)
    return pages if pages > 0 else None


def product_status(payload: Dict[str, Any]) -> ListingStatus:
    """Whether Lider still sells this.

    ``OUT_OF_STOCK`` is the one reading that removes an entry, and it is
    positive evidence: the shop said so.  A page with no product object at all
    is *not* -- that is what a bot check, a redirect and an error page all look
    like -- so it comes back undecided and the entry is left alone.
    """
    product = dig(payload, "props", "pageProps", "initialData", "data", "product")
    if not isinstance(product, dict):
        return ListingStatus.UNKNOWN
    status = text_of(product.get("availabilityStatus")).upper()
    if status == "OUT_OF_STOCK":
        return ListingStatus.GONE
    if status == "IN_STOCK":
        return ListingStatus.ACTIVE
    return ListingStatus.UNKNOWN


# --------------------------------------------------------------------------- #
# The marketplace
# --------------------------------------------------------------------------- #


class LiderMarketplace(RetailerMarketplace):
    name = "lider"
    label = "Lider"
    hosts = ("lider.cl",)
    home_url = HOST
    #: What the site sets once it knows who is asking.  Read off a signed-in
    #: session rather than guessed: these are the names in the stored session
    #: file, and `customer` is the one that survives longest.
    session_cookies = ("customer", "auth", "CID")
    #: PerimeterX's own cookies, read off a live jar rather than guessed:
    #: ``_px3`` is the short-lived clearance, ``_pxvid``/``__pxvid`` are the
    #: device id that accumulates a reputation, ``pxcts`` is its telemetry.
    #: ``_pxde`` and ``_pxhd`` are the other two the vendor sets and are listed
    #: so a jar that has them is cleaned too.
    #:
    #: Dropping these on a refusal is what makes the recovery this module's
    #: docstring describes actually happen: a reseeded profile arrives with the
    #: account and a device id the wall has no history for.  Before, the id it
    #: had just decided against came back with the login attached.
    challenge_cookies = ("_px3", "_pxvid", "__pxvid", "pxcts", "_pxde", "_pxhd")

    @classmethod
    def get_config(cls: Type["LiderMarketplace"], **kwargs: Any) -> LiderMarketplaceConfig:
        return LiderMarketplaceConfig(**kwargs)

    @classmethod
    def get_item_config(cls: Type["LiderMarketplace"], **kwargs: Any) -> LiderItemConfig:
        return LiderItemConfig(**kwargs)

    @classmethod
    def item_config_class(cls: Type["LiderMarketplace"]) -> Type[LiderItemConfig]:
        return LiderItemConfig

    @classmethod
    def session_domains(cls: Type["LiderMarketplace"]) -> Tuple[str, ...]:
        return ("lider.cl",)

    @classmethod
    def handles_url(cls: Type["LiderMarketplace"], url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return host == "lider.cl" or host.endswith(".lider.cl")

    def blocked_reason(self: "LiderMarketplace") -> str | None:
        """Whether the page we landed on is the bot wall.  See the module docstring.

        Asked only after the payload came back empty, so this never has to tell
        a wall from a product -- only a wall from a page that failed for some
        duller reason, which is the difference between waiting and retrying.

        Two signals, and both are read off the page rather than compared with a
        fixed address.  The block page has lived at ``/blocked`` so far, and
        that is a fact about today's deployment: a scraper that only recognises
        that one path stops recognising the wall the day it moves, and goes back
        to hammering the site through it.  What does not move is that the wall
        is *not the page that was asked for* and that it announces itself in its
        own title, which is what
        :func:`~ai_marketplace_monitor.extract.looks_blocked` reads -- strictly,
        needing both a wall-ish title and no product markup at all.
        """
        if self.page is None:
            return None
        try:
            landed = self.page.url or ""
            html = self.page.content()
        except KeyboardInterrupt:
            raise
        except Exception:
            return None
        path = urlparse(landed).path.strip("/").lower()
        if path.startswith("blocked"):
            return "sent us to its bot check"
        if looks_blocked(html):
            return "served a bot check"
        return None

    def search_url(
        self: "LiderMarketplace", phrase: str, item_config: ItemConfig, page: int = 1
    ) -> str:
        """Where the results for one phrase are.

        No price parameters: Lider has a price facet, its URL grammar was not
        verified against live pages, and a bound the site silently ignores is
        worse than one applied to the results -- which is what
        :meth:`RetailerMarketplace._within_price_bounds` does instead.
        """
        url = f"{HOST}/search?query={quote(phrase)}"
        return url if page <= 1 else f"{url}&page={page}"

    def parse_search(
        self: "LiderMarketplace", payload: Dict[str, Any], item_name: str
    ) -> List[Listing]:
        return parse_search(payload, item_name)

    def parse_product(
        self: "LiderMarketplace", payload: Dict[str, Any], url: str, item_name: str
    ) -> Listing | None:
        return parse_product(payload, url, item_name)

    def product_status(
        self: "LiderMarketplace", payload: Dict[str, Any]
    ) -> ListingStatus:
        return product_status(payload)

    def total_pages(self: "LiderMarketplace", payload: Dict[str, Any]) -> int | None:
        """How many results pages Lider says there are.

        ``paginationV2.maxPage``, which the module docstring has recorded as
        real since this file was written -- ``&page=2`` genuinely serves
        different items, verified by comparing the first entry of two pages.
        Reading it means a search stops where the catalogue does rather than by
        asking for a page that turns out to be empty.
        """
        return total_pages(payload)
