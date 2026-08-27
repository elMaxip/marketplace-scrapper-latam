"""Watching one product page, on a site nobody wrote a scraper for.

A search asks "what is for sale?".  A tracker asks "what is *this* doing?" --
one address, pasted by the user, re-read on the same schedule as everything
else.  It is the answer to the case a search cannot cover: the product is on a
shop this monitor has never heard of, or it is one particular listing among
thousands and the user only cares about that one.

**Almost none of this is new machinery, and that is the design.**  A tracked
product is a listing with no search behind it, so it goes into the same
observation store, is re-checked by the same
:class:`~ai_marketplace_monitor.refresh.ListingRefresher`, notifies through the
same channels, appears in the same dashboard and is grouped the same way.  What
had to be built is a pseudo-marketplace whose "search" is reading one URL --
everything downstream already worked.

**"A listing with no search behind it" is meant literally, and it was not at
first.**  A tracker used to be scheduled exactly like a search, on a repeating
job.  That could not work and failed silently: a search only ever looks for
listings nobody has recorded yet (:func:`~ai_marketplace_monitor.observations.is_known`),
and a tracker's one listing is recorded the first time it is read -- so every
run after that opened a browser, found the page already known, closed it, and
reported "0 new listings", while the price on that page moved and only the
review ever noticed.  So the schedule holds no trackers at all.  The single read
below happens once, when the tracker is added, on a browser of its own that is
closed straight after (``MarketplaceMonitor._ingest_trackers``), and everything
from then on is the review's.

``[track.<name>]`` is therefore a section that becomes an item on the
``tracked`` platform:

.. code-block:: toml

    [track.mi-notebook]
    url = "https://tienda.cl/producto/notebook-x"
    min_stock = 2
    notify = "maxi"

The one thing a tracker has that a search does not is **stock mínimo**: a
notification when the shop's own counter falls to or below a number.  It is here
rather than on searches because it only makes sense for a page being watched on
purpose -- "tell me when there are two left" is a sentence about a specific
thing, and a search that matched forty products would fire it forty times.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from logging import Logger
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple, Type
from urllib.parse import urlparse
from urllib.request import url2pathname

from playwright.sync_api import BrowserContext, Page  # type: ignore

from .extract import Extraction, ai_reader, extract, is_usable, looks_blocked
from .listing import Listing
from .marketplace import ItemConfig, ListingStatus, Marketplace, MarketplaceConfig
from .observations import is_known, record_observation
from .utils import (
    BaseConfig,
    CacheType,
    CounterItem,
    KeyboardMonitor,
    cache,
    counter,
    hilight,
    price_value,
)

logger = logging.getLogger(__name__)

#: The platform name a tracked product's listings carry.
PLATFORM = "tracked"

#: How the interface and the log refer to it.
LABEL = "Seguimiento"


def tracked_id(url: str) -> str:
    """A stable id for one tracked address.

    The address itself would do, and is what the store is keyed by anyway --
    but an id is also a cache key, a log line and part of a filename, and a URL
    with a query string in it is a bad one of each.  A hash of the address
    without its query string is short, stable across restarts, and unique for
    what a tracker actually watches: two links to the same product page with
    different tracking parameters are one product.
    """
    base = (url or "").split("?")[0].rstrip("/")
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]  # noqa: S324 - not a secret


#: What a ``file://`` tracker is allowed to point at.
LOCAL_SUFFIXES = (".html", ".htm")


def local_page(url: str) -> Optional[Path]:
    """The saved page a ``file://`` address points at, or None if it is not one.

    Trackers accept a page saved on disk so the whole path can be exercised
    without waiting for a shop to change something: save the product page, add
    it, edit the price in the file, run a review, and see whether the drop is
    noticed and notified.  That is the only way to test the parts that only
    happen on a *change*, and a real catalogue changes when it feels like it.

    Restricted to saved HTML, and that restriction is the point rather than
    tidiness: this address arrives from the web interface and is read by the
    server process, so without it "analizar página" would be a way to read any
    file on the machine and print it back.
    """
    parsed = urlparse((url or "").strip())
    if parsed.scheme != "file":
        return None
    # netloc is a host (``file://server/share``), which is somebody else's
    # machine and not what "saved on my pc" means.  Empty or ``localhost``.
    if parsed.netloc not in ("", "localhost"):
        return None
    if not parsed.path:
        return None
    path = Path(url2pathname(parsed.path))
    if path.suffix.lower() not in LOCAL_SUFFIXES:
        return None
    return path


def is_watchable(url: str) -> bool:
    """Whether an address is one a tracker can be created for."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme == "file":
        return local_page(url) is not None
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


@dataclass
class TrackedItemConfig(ItemConfig):
    """One ``[track.<name>]`` section.

    Derives from :class:`~ai_marketplace_monitor.marketplace.ItemConfig` rather
    than standing alone, so that everything an item already understands --
    ``notify``, ``ai``, ``rating``, ``enabled``, the AI prompts -- works on a
    tracker with no extra code.  Two of the parent's fields are supplied here
    instead of by the user: a tracker has an address rather than search phrases,
    and it runs on the ``tracked`` platform by definition.
    """

    #: The product page to watch.
    url: str | None = None
    #: Tell me when the shop says there are this many or fewer left.
    #:
    #: ``None`` is off, and off is the default: most pages publish no stock at
    #: all, and a threshold on a number that is never there would be a setting
    #: that silently does nothing.
    min_stock: int | None = None
    #: Trackers watching the same product, gathered under one name.
    #:
    #: Optional, and a tracker without one is not lesser: it is its own group of
    #: one.  What the name buys is the two things a lone tracker cannot have --
    #: one card on the dashboard instead of several, and a cheapest-of that
    #: means something, because "the cheapest offer" is only a question when
    #: there is more than one offer (see :mod:`ai_marketplace_monitor.toplist`).
    #:
    #: It shares a namespace with the search names and the tracker names, and
    #: has to: the top-1 record is keyed by it.  ``Config.get_tracker_config``
    #: is where that collision is refused, because only the whole file knows
    #: what else is called what.
    group: str | None = None

    def handle_url(self: "TrackedItemConfig") -> None:
        if not self.url or not isinstance(self.url, str):
            raise ValueError(f"Tracker {hilight(self.name)} needs a url.")
        if not is_watchable(self.url):
            raise ValueError(
                f"Tracker {hilight(self.name)} url must be a full address starting with "
                "http:// or https://, or a saved page (file:///... ending in .html)."
            )
        self.url = self.url.strip()

    def handle_min_stock(self: "TrackedItemConfig") -> None:
        if self.min_stock is None:
            return
        if self.min_stock is False:
            self.min_stock = None
            return
        if not isinstance(self.min_stock, int) or isinstance(self.min_stock, bool):
            raise ValueError(f"Tracker {hilight(self.name)} min_stock must be a whole number.")
        if self.min_stock < 0:
            raise ValueError(f"Tracker {hilight(self.name)} min_stock cannot be negative.")

    def handle_group(self: "TrackedItemConfig") -> None:
        if self.group is None:
            return
        if not isinstance(self.group, str):
            raise ValueError(f"Tracker {hilight(self.name)} group must be a name.")
        # An empty string is how the interface says "no group" when the user
        # clears the field, and it must mean the same thing as the key being
        # absent -- not a group whose name is nothing.
        self.group = self.group.strip() or None

    def handle_search_phrases(self: "TrackedItemConfig") -> None:
        """A tracker has an address, so the phrase is its own name.

        The parent insists on one and every counter and log line is labelled
        with it; filling it in here is cheaper than threading "this kind of item
        has no phrase" through all of them.
        """
        if not self.search_phrases:
            self.search_phrases = [self.name]


@dataclass
class TrackedMarketplaceConfig(MarketplaceConfig):
    """The ``[marketplace.tracked]`` section, which nobody has to write."""

    market_type: str | None = PLATFORM

    def handle_market_type(self: "TrackedMarketplaceConfig") -> None:
        if self.market_type is None:
            return
        if not isinstance(self.market_type, str) or self.market_type.lower() != PLATFORM:
            raise ValueError(f"Marketplace {hilight(self.name)} market_type must be {PLATFORM}.")


def listing_from(
    found: Extraction, url: str, item_name: str
) -> Optional[Listing]:
    """One extraction as a listing, or None when it is not worth storing.

    The same shape every other platform produces, which is what lets a tracked
    product sit in the same store, the same dashboard and the same notification
    as a Facebook listing.  The fields a generic page cannot supply -- location,
    seller, condition -- are left empty rather than filled in with the domain: a
    seller that is really a hostname is a fact about the URL, not about who is
    selling.
    """
    if not is_usable(found):
        return None
    return Listing(
        marketplace=PLATFORM,
        name=item_name,
        id=tracked_id(url),
        title=found.values.get("title", ""),
        image=found.values.get("image", ""),
        price=found.values.get("price", ""),
        post_url=url.split("?")[0],
        location="",
        seller="",
        condition="",
        description=found.values.get("description", ""),
        stock=found.values.get("stock", ""),
        availability=found.values.get("availability", ""),
    )


def below_minimum(listing: Listing, minimum: int | None) -> bool:
    """Whether the shop's own counter has fallen to or below ``minimum``.

    Three things have to be true and each absence is a different silence: the
    user asked for a threshold, the page publishes a number, and the number is a
    number.  A page that says nothing about stock produces no alert rather than
    an alert about zero -- which is the reading that would fire on every page
    that has no counter at all, which is most of them.
    """
    if minimum is None:
        return False
    text = (listing.stock or "").strip()
    if not text:
        return False
    try:
        return int(float(text)) <= minimum
    except (TypeError, ValueError):
        return False


class TrackedMarketplace(Marketplace):
    """One product page, read the way a page nobody scripted has to be read.

    Not really a marketplace, and deliberately shaped like one anyway: being a
    ``Marketplace`` is what gets a tracked product into the observation store,
    the review queue, the notification path and the dashboard without any of
    them learning a new type.

    ``opt_in`` because a tracker is not a search: an ``[item.*]`` must never
    quietly acquire one.
    """

    name = PLATFORM
    label = LABEL
    opt_in = True

    def __init__(
        self: "TrackedMarketplace",
        name: str,
        context: BrowserContext | None,
        keyboard_monitor: KeyboardMonitor | None = None,
        logger: Logger | None = None,
    ) -> None:
        super().__init__(name, context, keyboard_monitor, logger)
        self.page: Page | None = None
        #: Supplied by the monitor when an AI is configured; see
        #: :func:`ai_marketplace_monitor.extract.extract`.
        self.ai_reader: Optional[Callable[[str], Dict[str, str]]] = None

    @classmethod
    def get_config(
        cls: Type["TrackedMarketplace"], **kwargs: Any
    ) -> TrackedMarketplaceConfig:
        return TrackedMarketplaceConfig(**kwargs)

    @classmethod
    def get_item_config(cls: Type["TrackedMarketplace"], **kwargs: Any) -> TrackedItemConfig:
        return TrackedItemConfig(**kwargs)

    @classmethod
    def item_config_class(cls: Type["TrackedMarketplace"]) -> Type[TrackedItemConfig]:
        return TrackedItemConfig

    @classmethod
    def validate_item_config(
        cls: Type["TrackedMarketplace"],
        item_config: ItemConfig,
        marketplace_config: MarketplaceConfig,
    ) -> None:
        if not getattr(item_config, "url", None):
            raise ValueError(f"Tracker {hilight(item_config.name)} needs a url.")

    @classmethod
    def handles_url(cls: Type["TrackedMarketplace"], url: str) -> bool:
        """Never: a tracker is chosen by the user, not matched to an address.

        Claiming every URL here would make the ``--check`` path hand Facebook
        listings to the generic reader instead of to the scraper that knows
        Facebook.
        """
        return False

    # ------------------------------------------------------------------ #
    # Reading the page
    # ------------------------------------------------------------------ #

    def read(self: "TrackedMarketplace", url: str, skip: Tuple[str, ...] = ()) -> Extraction:
        """Load the page and extract what it says about the product."""
        if self.page is None:
            self.page = self.create_page()
        self.goto_url(url)
        assert self.page is not None
        try:
            html = self.page.content()
        except KeyboardInterrupt:
            raise
        except Exception:
            return Extraction()
        return extract(html, ai=self.ai_reader, skip=skip)

    def search(
        self: "TrackedMarketplace", item_config: ItemConfig
    ) -> Generator[Listing, None, None]:
        """Read the tracked page once, the first time it is seen.

        A "search" for a tracker is a single page load, and only for a product
        the store has never recorded: after that it is the review's business,
        exactly like every other listing (see
        :func:`~ai_marketplace_monitor.observations.is_known`).  So this yields
        once in a tracker's life and nothing thereafter, which is why a hundred
        trackers cost a hundred page loads *once* rather than every cycle.

        Called by ``MarketplaceMonitor._ingest_trackers`` when the tracker is
        added, and by nothing else -- there is no repeating job behind it.  The
        ``is_known`` check below is therefore a guard rather than the mechanism:
        two attempts overlapping (a browser that was slow to open, a
        configuration saved twice) must not read the page twice.
        """
        url = str(getattr(item_config, "url", "") or "")
        if not url:
            return
        listing_id = tracked_id(url)
        if is_known(PLATFORM, listing_id):
            return

        counter.increment(CounterItem.LISTING_QUERY, item_config.name)
        try:
            found = self.read(url)
        except KeyboardInterrupt:
            raise
        except Exception as error:
            if self.logger:
                self.logger.warning(
                    f"""{hilight("[Track]", "fail")} Could not read {url}: {error}"""
                )
            return

        listing = listing_from(found, url, item_config.name)
        if listing is None:
            if self.logger:
                self.logger.warning(
                    f"""{hilight("[Track]", "fail")} {url} did not give a title and a """
                    """readable price, so there is nothing to follow yet."""
                )
            return

        listing.to_cache(url)
        record_observation(listing, matched=True, item_name=item_config.name)
        if self.logger:
            self.logger.info(
                f"""{hilight("[Track]", "succ")} Following {hilight(listing.title)} """
                f"""at {hilight(listing.price)}."""
            )
        yield listing

    def recheck_listing(
        self: "TrackedMarketplace", post_url: str, item_config: ItemConfig
    ) -> Tuple[ListingStatus, Listing | None]:
        """Re-read a tracked page: the price, the stock, whether it still exists.

        Never GONE.  A generic page has no reliable way of saying "this product
        is finished" -- a 404 and a site having a bad afternoon reach this code
        identically -- so a tracker is never deleted automatically.  The user
        added it on purpose and removes it on purpose, which is the only rule
        that cannot silently lose something they were watching.
        """
        counter.increment(CounterItem.LISTING_RECHECKED, item_config.name)
        try:
            found = self.read(post_url)
        except KeyboardInterrupt:
            raise
        except Exception:
            return ListingStatus.UNKNOWN, None

        listing = listing_from(found, post_url, item_config.name)
        if listing is None:
            return ListingStatus.UNKNOWN, None
        listing.to_cache(post_url)
        return ListingStatus.ACTIVE, listing

    def check_listing(
        self: "TrackedMarketplace",
        item: Listing,
        item_config: ItemConfig,
        description_available: bool = True,
    ) -> bool:
        """A tracked product is kept unless its price is one to ignore.

        Deliberately almost no filtering.  The user pasted this address: they
        have already decided it is the thing they want, and running a keyword
        filter over it could only throw away the one product they asked for.
        The junk-price patterns still apply, because a page that reports 999999
        while its real price loads is a page that would otherwise poison the
        history it is being tracked for.
        """
        return not self.junk_price(item, item_config)


def tracker_names(config: Any) -> List[str]:
    """Every tracker in a loaded configuration, in file order."""
    return [
        item_name
        for (marketplace_name, item_name) in getattr(config, "items", {})
        if marketplace_name == PLATFORM
    ]


# --------------------------------------------------------------------------- #
# "Quedan dos" -- said once, not on every round
# --------------------------------------------------------------------------- #


def _alert_key(item_name: str) -> Tuple[str, str]:
    return (CacheType.STOCK_ALERT.value, item_name)


def remembered_alert(item_name: str, local_cache: Any = None) -> Optional[int]:
    """The stock level this tracker was last warned about, or None."""
    store = cache if local_cache is None else local_cache
    try:
        value = store.get(_alert_key(item_name))
    except KeyboardInterrupt:
        raise
    except Exception:
        return None
    return value if isinstance(value, int) else None


def remember_alert(item_name: str, level: int, local_cache: Any = None) -> None:
    store = cache if local_cache is None else local_cache
    try:
        store.set(_alert_key(item_name), int(level), tag=CacheType.STOCK_ALERT.value)
    except KeyboardInterrupt:
        raise
    except Exception:
        logger.debug("Could not remember the stock alert for %r", item_name, exc_info=True)


def clear_alert(item_name: str, local_cache: Any = None) -> None:
    """Re-arm: the shelf was restocked, so the next fall is news again."""
    store = cache if local_cache is None else local_cache
    try:
        store.delete(_alert_key(item_name))
    except KeyboardInterrupt:
        raise
    except Exception:
        logger.debug("Could not clear the stock alert for %r", item_name, exc_info=True)


def stock_level(listing: Listing) -> Optional[int]:
    """The number the page published, or None when it published none.

    None and zero are different answers and the difference matters: zero is "we
    are out", None is "this site does not count", and only one of them is worth
    a message.
    """
    text = (getattr(listing, "stock", "") or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def stock_alert(
    item_name: str,
    listing: Listing,
    minimum: int | None,
    local_cache: Any = None,
) -> bool:
    """Whether to say "it is running out" right now, and remember having said it.

    Three rules, and the second is the one that keeps the channel usable:

    * **Above the threshold re-arms it.**  A restock clears what was said, so the
      next fall is news again.
    * **The same level is not said twice.**  A tracker sitting at two units for a
      fortnight is a fortnight of one message, not of one message a round.
    * **A further fall is news.**  Two left and then one left are different
      things to somebody deciding whether to buy today.

    A page that publishes no stock at all produces nothing, ever -- which is
    most pages, and is why this is silent rather than firing on zero.
    """
    if minimum is None:
        return False
    level = stock_level(listing)
    if level is None:
        return False
    if level > minimum:
        if remembered_alert(item_name, local_cache) is not None:
            clear_alert(item_name, local_cache)
        return False

    said = remembered_alert(item_name, local_cache)
    if said is not None and level >= said:
        return False
    remember_alert(item_name, level, local_cache)
    return True


# --------------------------------------------------------------------------- #
# "Analizar página" -- the preview, before anything is saved
# --------------------------------------------------------------------------- #

#: What the preview sends as its User-Agent.
#:
#: A real browser's, because a plain ``python-requests`` string is refused by a
#: fair number of shops -- and being refused would be reported to the user as
#: "this page publishes nothing", which is a different and wrong answer.
PREVIEW_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

#: How long to wait for the page.  Short: this is a person clicking a button and
#: watching a spinner, not a scrape that can afford to be patient.
PREVIEW_TIMEOUT = 15


def fetch_page(url: str, timeout: int = PREVIEW_TIMEOUT) -> str:
    """The page's HTML, fetched directly, or "" when it could not be had.

    Deliberately *not* through the scraper's browser, and the reason is a fact
    about how this program is built rather than a shortcut: the browser belongs
    to the scraping thread, and the web interface runs on another one.  Driving
    it from here would mean queueing a request, waiting for the monitor to pick
    it up between jobs, and holding an HTTP request open meanwhile -- for a
    preview.

    The cost is honest and bounded: a page that renders entirely in JavaScript
    comes back empty here.  That is not the end of the tracker -- when it is
    created, the monitor reads the page with the real browser, which does run
    the JavaScript.  The preview is a preview, and it says so when it could not
    read something.
    """
    saved = local_page(url)
    if saved is not None:
        # No request at all: a saved page is already the answer, and the
        # extractor only ever wanted the HTML.  ``errors="replace"`` because a
        # page saved by a browser can carry whatever encoding the shop used, and
        # a mojibake character in the description is a far better outcome for a
        # preview than reporting the page as unreadable.
        try:
            return saved.read_text(encoding="utf-8", errors="replace")
        except KeyboardInterrupt:
            raise
        except OSError:
            logger.debug("Could not read %s for a preview", saved, exc_info=True)
            return ""

    try:
        import requests  # imported here so the module stays importable without it

        response = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": PREVIEW_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
            },
        )
        response.raise_for_status()
    except KeyboardInterrupt:
        raise
    except Exception:
        logger.debug("Could not fetch %s for a preview", url, exc_info=True)
        return ""
    return response.text or ""


def reader_for(config: Any) -> Optional[Callable[[str], Dict[str, str]]]:
    """The AI reader for a loaded configuration, or None when there is no AI.

    The first configured service, because the choice between two of them is a
    question about judging listings -- which is what `[item] ai = ...` answers --
    and reading a page is not a judgement.  Any of them can do it.
    """
    services = getattr(config, "ai", None) or {}
    for backend in services.values():
        reader = ai_reader(backend)
        if reader is not None:
            return reader
    return None


def preview(
    url: str,
    skip: Tuple[str, ...] = (),
    ai: Optional[Callable[[str], Dict[str, str]]] = None,
    fetch: Callable[[str], str] = fetch_page,
) -> Dict[str, Any]:
    """Read a page and report what was found, for the "analizar página" step.

    Plain data, including **where each field came from**: the difference between
    a title published as JSON-LD and one guessed from an ``<h1>`` is the whole
    reason the interface offers a retry, and hiding it would leave somebody
    approving a guess they were never told was one.

    ``fetch`` is injected so the rules above can be exercised against saved
    pages, and so a caller that already has a browser open can hand its HTML in
    instead of asking for the address twice.
    """
    html = fetch(url)
    if not html:
        return {
            "url": url.split("?")[0],
            "ok": False,
            "reason": "no-page",
            "fields": Extraction().describe(),
            "usable": False,
            "strategies": [],
        }

    if looks_blocked(html):
        # A challenge page parses perfectly well and the heuristics will happily
        # report its heading as the product's title.  Saying "no pudimos leerla"
        # is the true answer; showing "Robot or human?" as a detected title is a
        # confident wrong one, which is worse than none.
        return {
            "url": url.split("?")[0],
            "ok": False,
            "reason": "blocked",
            "fields": Extraction().describe(),
            "usable": False,
            "strategies": [],
        }

    found = extract(html, ai=ai, skip=skip)
    listing = listing_from(found, url, "preview")
    return {
        "url": url.split("?")[0],
        "ok": True,
        "reason": "" if listing is not None else "no-product",
        "fields": found.describe(),
        "usable": listing is not None,
        # Parsed rather than echoed, so the interface can show what the monitor
        # will actually compare instead of handing the string back.
        "price_value": price_value(found.values.get("price")),
        "strategies": sorted(set(found.sources.values())),
    }
