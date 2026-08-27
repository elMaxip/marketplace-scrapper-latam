"""What a shop is, as against what a marketplace is.

Facebook and Mercado Libre are marketplaces: a listing is one object, sold once,
by a person, in a place, and it disappears when it is sold.  Lider and Sodimac
are shops: a "listing" is a catalogue entry that exists whether or not anybody
buys it, has a stock level, has no location worth filtering on, and is always
new.  The difference is not cosmetic and it decides most of this module:

* **No location options.**  A shop searches its whole catalogue.  ``search_city``,
  ``radius``, ``search_region`` and ``seller_locations`` have no counterpart, and
  are ignored with one warning rather than approximated.
* **No condition options.**  Everything is new.
* **Stock and availability exist**, and nothing else in the monitor has them --
  see :attr:`~ai_marketplace_monitor.listing.Listing.stock`.
* **Price bounds are applied to results, not to the URL.**  Both sites do have
  price facets, and neither one's URL grammar for them was verified against
  live pages, so the monitor filters what it got rather than sending a
  parameter it is guessing at.  A filter that silently does nothing is worse
  than one that costs a few extra rows of parsing.
* **A dead entry is not evidence of anything.**  A shop that stops selling
  something takes the page down, and so does a shop that is having a bad
  afternoon.  Only an explicit "out of stock" from the page counts, exactly as
  ``ListingStatus`` demands.

Both shops are Next.js applications and both are read through
:mod:`ai_marketplace_monitor.nextdata` rather than through their rendered HTML.
That is where their similarity ends: the payloads have nothing in common, so
each subclass supplies four things -- a search URL, a search-payload parser, a
product-payload parser and a status reading -- and inherits the rest.

The four are deliberately *pure functions of a payload* rather than methods that
drive a browser.  Every rule about what a price or a stock level means is
therefore testable against a captured payload, with no browser and no network,
which is the only way selectors for a site that redeploys weekly stay honest.
"""

from __future__ import annotations

from dataclasses import dataclass
from logging import Logger
from typing import Any, Dict, Generator, List, Tuple, Type

from playwright.sync_api import BrowserContext, Page  # type: ignore

from . import control
from .listing import Listing
from .marketplace import (
    ItemConfig,
    ListingStatus,
    Marketplace,
    MarketplaceConfig,
)
from .nextdata import from_page
from .observations import is_known, record_observation
from .session import save_site_session
from .utils import (
    BaseConfig,
    CounterItem,
    KeyboardMonitor,
    aimm_event,
    counter,
    hilight,
    is_substring,
    price_value,
)

#: Result pages walked per search phrase unless the config says otherwise.
#:
#: One, because a shop's first page is already fifty-odd catalogue entries --
#: more than a Facebook search returns for the same phrase -- and because a
#: monitor that walks twenty pages of a retailer every cycle is a monitor that
#: gets rate limited.
DEFAULT_MAX_PAGES = 1

#: Seconds between two product-page reads.  The same pacing the other scrapers
#: use, for the same reason: a burst of page loads from one session is what
#: makes a site start asking who we are.
SECONDS_BETWEEN_PRODUCTS = 2

#: Options that only mean something for a marketplace with a physical location.
IGNORED_LOCATION_OPTIONS: Tuple[str, ...] = (
    "search_city",
    "city_name",
    "radius",
    "search_region",
    "seller_locations",
)

#: What a shop says about whether it can sell something right now.
IN_STOCK = "in_stock"
OUT_OF_STOCK = "out_of_stock"


@dataclass
class RetailerItemCommonConfig(BaseConfig):
    """The options every shop understands, per item or per marketplace.

    Small on purpose, and it is the whole list: a shop has no location, no
    condition and no seller to speak of, so inventing options for those would be
    offering settings that cannot do anything.
    """

    #: Result pages to walk per search phrase.
    max_pages: int | None = None
    #: Skip catalogue entries the shop cannot actually sell right now.
    #:
    #: Off by default, which is the conservative reading: an entry the search
    #: page did not label is not the same as one labelled out of stock, and
    #: turning this on by default would silently drop every entry on a site that
    #: only publishes availability on the product page.
    in_stock_only: bool | None = None

    def handle_max_pages(self: "RetailerItemCommonConfig") -> None:
        if self.max_pages is None:
            return
        if not isinstance(self.max_pages, int) or isinstance(self.max_pages, bool):
            raise ValueError(f"Item {hilight(self.name)} max_pages must be a positive integer.")
        if self.max_pages < 1:
            raise ValueError(f"Item {hilight(self.name)} max_pages must be a positive integer.")

    def handle_in_stock_only(self: "RetailerItemCommonConfig") -> None:
        if self.in_stock_only is None:
            return
        if not isinstance(self.in_stock_only, bool):
            raise ValueError(f"Item {hilight(self.name)} in_stock_only must be true or false.")


@dataclass
class RetailerMarketplaceConfig(MarketplaceConfig, RetailerItemCommonConfig):
    """The ``[marketplace.<shop>]`` section.  Defaults for every item."""


@dataclass
class RetailerItemConfig(ItemConfig, RetailerItemCommonConfig):
    """The ``[item.<name>.<shop>]`` section."""


class RetailerMarketplace(Marketplace):
    """A shop, read through its own page payload.

    Subclasses supply the four site-specific pieces and nothing else:

    ``search_url(phrase, item_config, page)``   where the results are
    ``parse_search(payload, item_name)``        payload -> listings
    ``parse_product(payload, url, item_name)``  payload -> one listing
    ``product_status(payload)``                 payload -> ACTIVE / GONE / UNKNOWN
    """

    #: Set by each subclass.
    name = "retailer"
    #: The shop's own name, for log lines.
    label = "Retailer"
    #: Hosts whose URLs this shop can read.
    hosts: Tuple[str, ...] = ()
    #: A shop is asked for, never assumed.  See :attr:`Marketplace.opt_in`.
    opt_in = True
    #: The page a health check loads.  The shop's front door, because it is the
    #: one address that exists on every deployment and answers the only two
    #: questions worth asking: does the site serve us, and does it know us.
    home_url: str = ""
    #: Cookie names the shop sets for a signed-in visitor, in its own spelling.
    #:
    #: Empty for a shop with no sign-in worth having -- and empty is not a gap:
    #: a catalogue is public, and reporting "not signed in" about a site that
    #: never asked anybody to sign in is an alarm about nothing.
    session_cookies: Tuple[str, ...] = ()

    def __init__(
        self: "RetailerMarketplace",
        name: str,
        context: BrowserContext | None,
        keyboard_monitor: KeyboardMonitor | None = None,
        logger: Logger | None = None,
    ) -> None:
        super().__init__(name, context, keyboard_monitor, logger)
        self.page: Page | None = None
        self._warned_about_location = False
        #: Whether this object has already kept what the browser earned.  Once
        #: per object and not once per page: the interesting cookie is set on
        #: the first page that is served, and writing the file on every load
        #: would be a disk write per product.
        self._session_kept = False

    # ------------------------------------------------------------------ #
    # What a subclass must provide
    # ------------------------------------------------------------------ #

    def search_url(
        self: "RetailerMarketplace", phrase: str, item_config: ItemConfig, page: int = 1
    ) -> str:
        raise NotImplementedError

    def parse_search(
        self: "RetailerMarketplace", payload: Dict[str, Any], item_name: str
    ) -> List[Listing]:
        raise NotImplementedError

    def parse_product(
        self: "RetailerMarketplace", payload: Dict[str, Any], url: str, item_name: str
    ) -> Listing | None:
        raise NotImplementedError

    def product_status(
        self: "RetailerMarketplace", payload: Dict[str, Any]
    ) -> ListingStatus:
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # Config plumbing
    # ------------------------------------------------------------------ #

    @classmethod
    def validate_item_config(
        cls: Type["RetailerMarketplace"],
        item_config: ItemConfig,
        marketplace_config: MarketplaceConfig,
    ) -> None:
        """A search phrase is enough: a shop has no city to anchor results to."""
        return None

    def _option(self: "RetailerMarketplace", item_config: ItemConfig, key: str) -> Any:
        """An option from the item, falling back to the marketplace section."""
        value = getattr(item_config, key, None)
        if value is None:
            value = getattr(self.config, key, None)
        return value

    def _warn_about_ignored_options(
        self: "RetailerMarketplace", item_config: ItemConfig
    ) -> None:
        """Say once that the location options do nothing here.

        Once per marketplace object and not once per search: the same warning on
        every phrase of every cycle is a log nobody reads.
        """
        if self._warned_about_location or self.logger is None:
            return
        ignored = [
            key for key in IGNORED_LOCATION_OPTIONS if getattr(item_config, key, None)
        ]
        if not ignored:
            return
        self._warned_about_location = True
        self.logger.info(
            f"""{hilight("[Search]", "info")} {self.label} searches a whole catalogue and """
            f"""has no location filter, so {", ".join(ignored)} """
            f"""{"is" if len(ignored) == 1 else "are"} ignored here."""
        )

    # ------------------------------------------------------------------ #
    # Is the stored session doing anything?
    # ------------------------------------------------------------------ #

    def session_health(self: "RetailerMarketplace") -> Tuple[bool, str] | None:
        """Whether the cookies just loaded got us a page, and what was found.

        Asked after an import, because an import that silently does nothing is
        indistinguishable from one that worked until the next search comes back
        empty -- which is a terrible way to find out, and is how Lider and
        Sodimac imports were reported until now: "loaded 9 cookies", and never a
        word about whether the site accepted any of them.

        Not :meth:`Marketplace.is_signed_in`, which is a question about a login.
        A shop's catalogue is public, so the useful question is the one a shop
        can actually fail: *does it serve us its pages*.  The sign-in, where
        there is one, is reported alongside rather than instead -- Lider prices
        differ for a signed-in customer, so it is worth saying, and it is read
        from the cookies the site itself set rather than from a phrase on a page.

        ``None`` means the check could not be run at all (no browser yet), which
        is not an answer and must not be reported as a bad one.
        """
        if not self.home_url or (self.context is None and self.page is None):
            return None
        # A tab of the check's own when there is none to borrow, and given back
        # afterwards: a health check must not leave a window open in a browser
        # somebody is watching.
        temporary = False
        if self.page is None:
            assert self.context is not None
            self.page = self.context.new_page()
            temporary = True
        try:
            payload = self.open_payload(self.home_url)
            names = self._cookie_names()
            signed_in = bool(self.session_cookies) and any(
                name in names for name in self.session_cookies
            )
            if payload is None:
                return (
                    False,
                    f"{self.label} did not serve its own home page, so the cookies "
                    "reached a browser the site is currently refusing.",
                )
            if not self.session_cookies:
                return (True, f"{self.label} is serving its pages.")
            return (
                True,
                f"{self.label} is serving its pages"
                + (" and knows the account." if signed_in else ", signed out."),
            )
        except KeyboardInterrupt:
            raise
        except Exception:
            return None
        finally:
            if temporary and self.page is not None:
                try:
                    self.page.close()
                except Exception:
                    pass
                self.page = None

    def save_session(self: "RetailerMarketplace") -> bool:
        """Keep what this shop's browser earned, and only what is this shop's.

        The base class writes the whole cookie jar, which in this profile also
        holds Facebook and Mercado Libre; this writes the shop's own domains.

        Worth doing at all because of what a shop's cookies *are*.  Lider hands
        out a short-lived clearance token once its bot check has decided we are
        a person; that token lives in the browser profile and nowhere else, so a
        profile discarded or replaced started from being challenged again, with
        the stored session -- login cookies pasted in by hand -- unable to help.
        Saving it means the clearance survives the profile.
        """
        context = self.context or (self.page.context if self.page is not None else None)
        if context is None:
            return False
        return save_site_session(self.name, context, self.hosts)

    def _cookie_names(self: "RetailerMarketplace") -> Tuple[str, ...]:
        """The cookie names this browser holds for the shop's own hosts.

        Names only.  A cookie's value *is* the session, and a log line that
        could carry one is a way to lift it.
        """
        context = self.context or (self.page.context if self.page is not None else None)
        if context is None:
            return ()
        try:
            cookies = context.cookies()
        except Exception:
            return ()
        wanted: List[str] = []
        for cookie in cookies:
            domain = str(cookie.get("domain") or "").lstrip(".").lower()
            if any(domain.endswith(host) for host in self.hosts):
                wanted.append(str(cookie.get("name") or ""))
        return tuple(wanted)

    # ------------------------------------------------------------------ #
    # Reading a page
    # ------------------------------------------------------------------ #

    def blocked_reason(self: "RetailerMarketplace") -> str | None:
        """Why the shop refused this page, or None if it did not.

        Answered by the subclass, and only for a shop that has a refusal worth
        recognising.  The default is None, which restores the old behaviour
        exactly: a page that did not load is just a page that did not load.

        The point of the distinction is not the log line.  It is that "the shop
        is refusing us" is a fact the *rest of the pass* can act on -- stop
        opening pages there, wait longer each time, tell somebody -- and "the
        page did not load" is not.
        """
        return None

    def open_payload(self: "RetailerMarketplace", url: str) -> Dict[str, Any] | None:
        """Navigate and hand back the page's own data, or None.

        None is still every way of not getting the page: a bot check, a sign-in
        wall, an error page, a redirect somewhere else, a layout with no payload
        in it.  The caller's response to all of them is the same -- report
        nothing, change nothing -- and that has not changed.

        What is now separated out is *whether to keep asking*.  A shop that is
        refusing us was being hit at full rate on every cycle, because nothing
        here ever said so and the cooldown gates in the monitor are keyed on a
        marketplace having been reported blocked.  A page that comes back
        normally clears the cooldown, which is the site saying it has forgiven
        us and is worth more than any timer.
        """
        if self.page is None:
            self.page = self.create_page()
        self.goto_url(url)
        payload = from_page(self.page)
        if payload is None:
            reason = self.blocked_reason()
            if reason:
                self._hit_wall(reason)
            return None
        control.clear_marketplace_block(self.name)
        if not self._session_kept:
            self._session_kept = True
            self.save_session()
        return payload

    def _hit_wall(self: "RetailerMarketplace", reason: str) -> None:
        """Record that the shop refused us, and back off further each time.

        **Once per cooldown, not once per page.**  A pass that is already under
        way keeps opening product pages after the wall goes up -- deliberately,
        because the search card is still returned for each of them -- and every
        one of those is refused too.  Counted as separate strikes, one walled
        search took Lider from fifteen minutes to the four-hour ceiling in a
        single pass, with 48 strikes on the board.  The second refusal inside a
        cooldown is not news: it is the same refusal, still in force.
        """
        if control.marketplace_blocked(self.name):
            return
        block = control.block_marketplace(self.name, reason=reason, announce=True)
        if self.logger:
            minutes = int(block["seconds"] // 60)
            self.logger.warning(
                f"""{hilight("[Search]", "fail")} {self.label} {reason} instead of serving """
                f"""the page. Leaving it alone for {minutes} minutes.""",
                extra=aimm_event(
                    "marketplace_blocked",
                    marketplace=self.name,
                    reason=reason,
                    seconds=block["seconds"],
                    strikes=block["strikes"],
                ),
            )

    def is_blocked(self: "RetailerMarketplace") -> bool:
        """Whether this shop is inside a cooldown and must be left alone."""
        if not control.marketplace_blocked(self.name):
            return False
        block = control.marketplace_block(self.name) or {}
        if self.logger:
            self.logger.info(
                f"""{hilight("[Search]", "info")} Skipping {self.label} for another """
                f"""{int(block.get("seconds_left", 0) // 60)} minutes: it """
                f"""{block.get("reason") or "refused us"}."""
            )
        return True

    # ------------------------------------------------------------------ #
    # Searching
    # ------------------------------------------------------------------ #

    def search(
        self: "RetailerMarketplace", item_config: ItemConfig
    ) -> Generator[Listing, None, None]:
        """Walk the search pages and yield what passes the filters.

        The shape is the other scrapers': a listing already in the store is not
        this flow's business (see
        :func:`~ai_marketplace_monitor.observations.is_known`), every sighting is
        recorded whichever way the filters went, and a page that will not load
        yields nothing rather than reporting an empty catalogue as the truth.

        One difference, and it is the shops' doing: the search payload already
        carries the price, the image, the seller and the title, so the product
        page is opened only for the description -- and only for entries that
        passed everything else.  On a marketplace the page load is where the
        filtering data comes from; here it is the last step, which is why a
        retailer search costs a fraction of a Facebook one.
        """
        self._warn_about_ignored_options(item_config)

        # The answer is known in advance, and asking anyway is what kept a
        # refused shop receiving traffic at full rate every cycle -- which is
        # the surest way to stay refused.
        if self.is_blocked():
            return

        if self.page is None:
            self.page = self.create_page()

        max_pages = int(self._option(item_config, "max_pages") or DEFAULT_MAX_PAGES)
        seen: Dict[str, bool] = {}
        opened = 0

        for phrase in item_config.search_phrases:
            for page_number in range(1, max_pages + 1):
                url = self.search_url(phrase, item_config, page=page_number)
                payload = self.open_payload(url)
                if payload is None:
                    if self.logger:
                        self.logger.warning(
                            f"""{hilight("[Search]", "fail")} {self.label} did not serve its """
                            f"""results for {hilight(phrase)}."""
                        )
                    break

                try:
                    found = self.parse_search(payload, item_config.name)
                except KeyboardInterrupt:
                    raise
                except Exception as error:
                    # The payload's shape changed under us.  Loud in the log and
                    # silent in the results: reporting an empty catalogue would
                    # look exactly like a product nobody sells any more.
                    if self.logger:
                        self.logger.error(
                            f"""{hilight("[Search]", "fail")} Could not read {self.label}'s """
                            f"""results for {hilight(phrase)}: {error}""",
                            exc_info=True,
                        )
                    break

                counter.increment(CounterItem.SEARCH_PERFORMED, item_config.name)
                if self.logger:
                    self.logger.debug(
                        f"""{hilight("[Search]", "succ" if found else "fail")} """
                        f"""{len(found)} result(s) on {self.label} page {page_number} """
                        f"""for {phrase}."""
                    )

                for listing in found:
                    key = listing.post_url.split("?")[0]
                    if key in seen:
                        continue
                    seen[key] = True
                    if self.keyboard_monitor is not None and self.keyboard_monitor.is_paused():
                        return
                    counter.increment(CounterItem.LISTING_EXAMINED, item_config.name)

                    if is_known(listing.marketplace, listing.id):
                        continue
                    # Everything except the description can be decided from the
                    # card, so it is: the page load below is the expensive part
                    # and there is no sense paying it for an entry that is about
                    # to be thrown away on its price.
                    if not self.check_listing(listing, item_config, description_available=False):
                        counter.increment(CounterItem.EXCLUDED_LISTING, item_config.name)
                        continue

                    details = self._with_description(
                        listing, item_config, spaced=opened > 0
                    )
                    opened += 1

                    matched = self.check_listing(details, item_config)
                    record_observation(
                        details, matched=matched, item_name=item_config.name
                    )
                    if matched:
                        yield details
                    else:
                        counter.increment(CounterItem.EXCLUDED_LISTING, item_config.name)

                if len(found) == 0:
                    break

    def _with_description(
        self: "RetailerMarketplace",
        listing: Listing,
        item_config: ItemConfig,
        spaced: bool = True,
    ) -> Listing:
        """The catalogue entry plus whatever its own page adds.

        The card back, unchanged, whenever the page cannot be read.  A missing
        description costs a keyword filter some accuracy; throwing the entry
        away costs the user the listing.
        """
        import time

        if spaced and SECONDS_BETWEEN_PRODUCTS > 0:
            time.sleep(SECONDS_BETWEEN_PRODUCTS)
        try:
            details, _cached = self.get_listing_details(
                listing.post_url,
                item_config,
                price=listing.price,
                title=listing.title,
                fallback=listing,
            )
        except KeyboardInterrupt:
            raise
        except Exception as error:
            if self.logger:
                self.logger.debug(
                    f"Could not read the {self.label} product page {listing.post_url}: {error}"
                )
            return listing
        return details

    # ------------------------------------------------------------------ #
    # One product page
    # ------------------------------------------------------------------ #

    def get_listing_details(
        self: "RetailerMarketplace",
        post_url: str,
        item_config: ItemConfig,
        price: str | None = None,
        title: str | None = None,
        fallback: Listing | None = None,
    ) -> Tuple[Listing, bool]:
        """Read one product page.  Same signature as the other marketplaces'.

        ``fallback`` is the card this entry was found on, and it is not just a
        safety net: the card carries the struck-through original price, which
        several shops print on the grid and not on the product page.
        """
        post_url = post_url.split("?")[0]
        cached = Listing.from_cache(post_url)
        if (
            cached is not None
            and (price is None or cached.price == price)
            and (title is None or cached.title == title)
        ):
            return cached, True

        payload = self.open_payload(post_url)
        if payload is None:
            if fallback is not None:
                return fallback, True
            raise ValueError(f"{self.label} did not serve the product page {post_url}.")

        counter.increment(CounterItem.LISTING_QUERY, item_config.name)
        details = self.parse_product(payload, post_url, item_config.name)
        if details is None:
            if fallback is not None:
                return fallback, True
            raise ValueError(f"Could not read the {self.label} product page {post_url}.")

        if fallback is not None:
            details = self._merge_card(details, fallback)
        details.to_cache(post_url)
        return details, False

    @staticmethod
    def _merge_card(details: Listing, card: Listing) -> Listing:
        """Fill in from the card whatever the product page did not say.

        Field by field rather than "the page wins" or "the card wins", because
        neither is true: the page has the description and the stock, the card
        has the struck-through price and sometimes the only image, and each of
        them leaves things blank that the other filled in.

        The **id is the card's**, and that one is not a blank-filling rule but
        an identity rule.  A shop may serve a product page under a different id
        from the one its own results grid used -- Sodimac does exactly this, the
        grid's ``5787254`` redirecting to article ``110005070`` -- and the id the
        search will hand over again next cycle is the grid's.  Storing the page's
        instead means :func:`~ai_marketplace_monitor.observations.is_known` asks
        about an id nothing was ever stored under, so every product counts as
        new on every pass: the whole catalogue re-notified, every cycle, for
        ever.
        """
        for field in (
            "title",
            "image",
            "price",
            "location",
            "seller",
            "condition",
            "description",
            "stock",
            "availability",
        ):
            if not getattr(details, field, "") and getattr(card, field, ""):
                setattr(details, field, getattr(card, field))
        if card.id:
            details.id = card.id
        return details

    def recheck_listing(
        self: "RetailerMarketplace", post_url: str, item_config: ItemConfig
    ) -> Tuple[ListingStatus, Listing | None]:
        """Re-read a stored entry: the price, the stock, whether it still sells.

        This is where a shop differs most from a marketplace, and in the useful
        direction: a shop *publishes* whether it can sell something, so "out of
        stock" is a fact read off the page rather than a guess about a listing
        that stopped appearing in search.  That verdict is
        :attr:`ListingStatus.GONE` and it removes the entry.

        Everything else is undecided.  A page that will not load, a payload
        whose shape changed, a bot check -- none of them says the shop stopped
        selling the thing, and the monitor's rule is that only evidence deletes.
        """
        # A shop inside a cooldown says nothing about its listings, which is
        # UNKNOWN and not GONE: only evidence deletes, and being refused is
        # evidence about us rather than about the product.
        if control.marketplace_blocked(self.name):
            return ListingStatus.UNKNOWN, None
        counter.increment(CounterItem.LISTING_RECHECKED, item_config.name)
        try:
            payload = self.open_payload(post_url)
        except KeyboardInterrupt:
            raise
        except Exception:
            return ListingStatus.UNKNOWN, None
        if payload is None:
            return ListingStatus.UNKNOWN, None

        try:
            status = self.product_status(payload)
            if status in (ListingStatus.GONE, ListingStatus.SOLD):
                return status, None
            details = self.parse_product(payload, post_url, item_config.name)
        except KeyboardInterrupt:
            raise
        except Exception as error:
            if self.logger:
                self.logger.debug(
                    f"Could not re-read the {self.label} product page {post_url}: {error}"
                )
            return ListingStatus.UNKNOWN, None

        if details is None:
            return ListingStatus.UNKNOWN, None
        details.to_cache(post_url)
        return ListingStatus.ACTIVE, details

    # ------------------------------------------------------------------ #
    # Filtering
    # ------------------------------------------------------------------ #

    def check_listing(
        self: "RetailerMarketplace",
        item: Listing,
        item_config: ItemConfig,
        description_available: bool = True,
    ) -> bool:
        """The filters, in the order the monitor's rules put them.

        The junk-price patterns first, always: an excluded price is not a number
        to compare against a minimum or a maximum, it is a field somebody filled
        in to get past a form.  The bounds come next, then the keyword and
        seller filters, then stock.
        """
        if self.junk_price(item, item_config):
            return False

        if not self._within_price_bounds(item, item_config):
            return False

        antikeywords = item_config.antikeywords or getattr(self.config, "antikeywords", None)
        if antikeywords and is_substring(
            antikeywords, f"{item.title} {item.description}", logger=self.logger
        ):
            if self.logger:
                self.logger.info(
                    f"""{hilight("[Skip]", "fail")} Exclude {hilight(item.title)} due to """
                    f"""{hilight("excluded keywords", "fail")}: {", ".join(antikeywords)}"""
                )
            return False

        keywords = item_config.keywords
        if (
            description_available
            and keywords
            and not is_substring(
                keywords, f"{item.title}  {item.description}", logger=self.logger
            )
        ):
            if self.logger:
                self.logger.info(
                    f"""{hilight("[Skip]", "fail")} Exclude {hilight(item.title)} """
                    f"""{hilight("without required keywords", "fail")}."""
                )
            return False

        exclude_sellers = item_config.exclude_sellers or getattr(
            self.config, "exclude_sellers", None
        )
        if (
            item.seller
            and exclude_sellers
            and is_substring(exclude_sellers, item.seller, logger=self.logger)
        ):
            if self.logger:
                self.logger.info(
                    f"""{hilight("[Skip]", "fail")} Exclude {hilight(item.title)} sold by """
                    f"""{hilight("banned seller", "fail")} {hilight(item.seller)}"""
                )
            return False

        if self._option(item_config, "in_stock_only") and item.availability == OUT_OF_STOCK:
            # Only an explicit "out of stock" excludes.  An entry the search
            # page said nothing about is not the same thing, and treating it as
            # such would empty the results of a shop that only publishes
            # availability on the product page.
            if self.logger:
                self.logger.info(
                    f"""{hilight("[Skip]", "fail")} Exclude {hilight(item.title)}: """
                    """out of stock."""
                )
            return False

        return True

    def _within_price_bounds(
        self: "RetailerMarketplace", item: Listing, item_config: ItemConfig
    ) -> bool:
        """Whether the price is inside ``min_price`` / ``max_price``.

        Applied to the result rather than sent to the shop, and that is a
        decision rather than an oversight: both sites have price facets whose
        URL grammar was not verified against live pages, and a filter parameter
        that a site silently ignores is a filter that looks like it works.

        A price that cannot be parsed passes.  The alternative is throwing away
        an entry because the shop wrote something this program did not expect,
        and the user can see the price for themselves.  The currency suffix
        (``"300 USD"``) is dropped rather than converted: a shop prices in one
        currency, its own, and pretending to convert would be inventing a rate.
        """
        value = price_value(item.price)
        if value is None:
            return True
        for key, low in (("min_price", True), ("max_price", False)):
            bound = self._option(item_config, key)
            if bound is None:
                continue
            limit = price_value(str(bound).split(" ")[0])
            if limit is None:
                continue
            if (low and value < limit) or (not low and value > limit):
                if self.logger:
                    self.logger.debug(
                        f"""{hilight("[Skip]", "fail")} {item.title} at {item.price} is """
                        f"""outside {key} {bound}."""
                    )
                return False
        return True
