import time
from dataclasses import dataclass, field
from enum import Enum
from logging import Logger
from typing import Any, Callable, Generator, Generic, List, Tuple, Type, TypeVar

from playwright.sync_api import BrowserContext, ElementHandle, Locator, Page  # type: ignore

from .control import raise_if_cancelled
from .listing import Listing
from .price_patterns import compile_patterns, matches as price_pattern_match, validate_patterns
from .session import drop_cookies, load_session, save_session
from .utils import (
    BaseConfig,
    Currency,
    KeyboardMonitor,
    MonitorConfig,
    Translator,
    aimm_event,
    hilight,
    interval_in_seconds,
    validated_start_at,
)


class MarketPlace(Enum):
    FACEBOOK = "facebook"


class ListingStatus(Enum):
    """What a re-visit to a listing page established about that listing.

    The three failure-ish outcomes are kept apart because only two of them may
    ever cause a deletion.  ``SOLD`` and ``GONE`` are *positive evidence* that
    the listing is over -- the page said so.  ``UNKNOWN`` is everything else: a
    timeout, a network blip, a rate limit, a login wall, a layout the parser did
    not recognise.  Those look identical from the outside and none of them is a
    reason to throw a listing away, so the refresher leaves such a listing
    exactly where it was and tries again later.
    """

    ACTIVE = "active"
    SOLD = "sold"
    GONE = "gone"
    UNKNOWN = "unknown"


@dataclass
class MarketItemCommonConfig(BaseConfig):
    """Item options that can be specified in market (non-marketplace specifc)

    This class defines and processes options that can be specified
    in both marketplace and item sections, generic to all marketplaces
    """

    ai: List[str] | None = None
    exclude_sellers: List[str] | None = None
    notify: List[str] | None = None
    search_city: List[str] | None = None
    city_name: List[str] | None = None
    # radius must be processed after search_city
    radius: List[int] | None = None
    currency: List[str] | None = None
    search_interval: int | None = None
    max_search_interval: int | None = None
    start_at: List[str] | None = None
    search_region: List[str] | None = None
    max_price: str | None = None
    min_price: str | None = None
    #: What the user hopes to pay on this platform.  Not a search filter -- the
    #: monitor never sends it anywhere -- but the number the dashboard measures
    #: the best price found against, which is why it lives per platform: the
    #: same product is worth a different price on Facebook and on Mercado Libre.
    target_price: str | None = None
    #: Prices that are not asking prices: 999999, 123456, 0, "gratis".
    #:
    #: Per platform because the noise is: Facebook's placeholder of choice is a
    #: run of nines typed to get past a required field, Mercado Libre's is the
    #: 1 that means "write to me".  See
    #: :mod:`ai_marketplace_monitor.price_patterns` for the syntax, and
    #: :meth:`Marketplace.junk_price` for where in the filtering it applies --
    #: before every other price test, because a junk price is not a cheap
    #: listing or an expensive one, it is a listing whose price is unknown.
    excluded_price_patterns: List[str] | None = None
    #: Saved pattern lists this search uses, by name (``[price_patterns.*]``).
    #:
    #: A reference rather than a copy, exactly like ``search_region``: the same
    #: four rules are wanted by every search somebody has, and retyping them is
    #: how two searches end up excluding ``9*`` and ``99999`` respectively.
    #: :meth:`Config.expand_price_patterns` folds the named lists into
    #: ``excluded_price_patterns`` before anything runs, so nothing below this
    #: point -- :meth:`Marketplace.junk_price` included -- knows sets exist.
    excluded_price_pattern_sets: List[str] | None = None
    rating: List[int] | None = None
    prompt: str | None = None
    extra_prompt: str | None = None
    rating_prompt: str | None = None

    def handle_ai(self: "MarketItemCommonConfig") -> None:
        if self.ai is None:
            return

        if isinstance(self.ai, str):
            self.ai = [self.ai]
        if not all(isinstance(x, str) for x in self.ai):
            raise ValueError(f"Item {hilight(self.name)} ai must be a string or list.")

    def handle_exclude_sellers(self: "MarketItemCommonConfig") -> None:
        if self.exclude_sellers is None:
            return

        if isinstance(self.exclude_sellers, str):
            self.exclude_sellers = [self.exclude_sellers]
        if not isinstance(self.exclude_sellers, list) or not all(
            isinstance(x, str) for x in self.exclude_sellers
        ):
            raise ValueError(f"Item {hilight(self.name)} exclude_sellers must be a list.")

    def handle_max_search_interval(self: "MarketItemCommonConfig") -> None:
        """Deprecated: the schedule is the ``[monitor]`` section's business now.

        Still parsed, because a config written before the move must keep
        working, and still honored as a fallback when ``[monitor]`` says
        nothing about the schedule.
        """
        if self.max_search_interval is None:
            return
        self.max_search_interval = interval_in_seconds(
            self.max_search_interval, f"Item {hilight(self.name)}", "max_search_interval"
        )

    def handle_notify(self: "MarketItemCommonConfig") -> None:
        if self.notify is None:
            return

        if isinstance(self.notify, str):
            self.notify = [self.notify]
        if not all(isinstance(x, str) for x in self.notify):
            raise ValueError(
                f"Item {hilight(self.name)} notify must be a string or list of string."
            )

    def handle_radius(self: "MarketItemCommonConfig") -> None:
        if self.radius is None:
            return

        if self.search_city is None:
            raise ValueError(
                f"Item {hilight(self.name)} radius must be None if search_city is None."
            )

        if isinstance(self.radius, int):
            self.radius = [self.radius]

        if not all(isinstance(x, int) for x in self.radius):
            raise ValueError(
                f"Item {hilight(self.name)} radius must be one or a list of integers."
            )

        if len(self.radius) != len(self.search_city):
            raise ValueError(
                f"Item {hilight(self.name)} radius must be the same length as search_city."
            )

    def handle_search_city(self: "MarketItemCommonConfig") -> None:
        if self.search_city is None:
            return

        if isinstance(self.search_city, str):
            self.search_city = [self.search_city]

        if not isinstance(self.search_city, list) or not all(
            isinstance(x, str) for x in self.search_city
        ):
            raise ValueError(
                f"Item {hilight(self.name)} search_city must be a string or list of string."
            )

        # Validate format of each search_city entry
        for city in self.search_city:
            # Check if the city contains only lowercase letters and numbers
            if not city.replace("_", "").replace("-", "").isalnum() or any(
                c.isupper() for c in city
            ):
                # Provide helpful guidance on obtaining the correct format
                raise ValueError(
                    f"Item {hilight(self.name)} search_city '{city}' has incorrect format.\n"
                    f"Expected: lowercase letters and numbers only (e.g., 'sanfrancisco', 'newyork', 'toronto').\n"
                    f"To get the correct value:\n"
                    f"  1. Visit Facebook Marketplace\n"
                    f"  2. Perform a search in your desired location\n"
                    f"  3. Look at the URL: https://www.facebook.com/marketplace/XXXXX/search?query=...\n"
                    f"  4. Use the XXXXX value (the text after 'marketplace/') as your search_city\n"
                    f"Example: If URL is https://www.facebook.com/marketplace/sanfrancisco/search?query=item\n"
                    f"         Then search_city = 'sanfrancisco'"
                )

    def handle_city_name(self: "MarketItemCommonConfig") -> None:
        if self.city_name is None:
            if self.search_city is None:
                return
            self.city_name = [x.capitalize() for x in self.search_city]
            return

        if self.search_city is None:
            raise ValueError(
                f"Item {hilight(self.name)} city_name must be None if search_city is None."
            )
        if isinstance(self.city_name, str):
            self.city_name = [self.city_name]
        # check if city_name is a list of strings
        if not isinstance(self.city_name, list) or not all(
            isinstance(x, str) for x in self.city_name
        ):
            raise ValueError(f"Region {self.name} city_name must be a list of strings.")

        if len(self.city_name) != len(self.search_city):
            raise ValueError(
                f"Region {self.name} city_name ({self.city_name}) must be the same length as search_city ({self.search_city})."
            )

    def handle_currency(self: "MarketItemCommonConfig") -> None:
        if self.currency is None:
            return

        if self.search_city is None:
            raise ValueError(
                f"Item {hilight(self.name)} currency must be None if search_city is None."
            )

        if isinstance(self.currency, str):
            self.currency = [self.currency] * len(self.search_city)

        if not all(isinstance(x, str) for x in self.currency):
            raise ValueError(
                f"Item {hilight(self.name)} currency must be one or a list of strings."
            )

        for currency in self.currency:
            try:
                Currency(currency)
            except ValueError as e:
                raise ValueError(
                    f"Item {hilight(self.name)} currency {currency} is not recognized."
                ) from e

        if len(self.currency) != len(self.search_city):
            raise ValueError(
                f"Region {self.name} city_name ({self.city_name}) must be the same length as search_city ({self.search_city})."
            )

    def handle_search_interval(self: "MarketItemCommonConfig") -> None:
        """Deprecated, like :meth:`handle_max_search_interval`."""
        if self.search_interval is None:
            return
        self.search_interval = interval_in_seconds(
            self.search_interval, f"Item {hilight(self.name)}", "search_interval"
        )

    def handle_search_region(self: "MarketItemCommonConfig") -> None:
        if self.search_region is None:
            return

        if isinstance(self.search_region, str):
            self.search_region = [self.search_region]

        if not isinstance(self.search_region, list) or not all(
            isinstance(x, str) for x in self.search_region
        ):
            raise ValueError(
                f"Item {hilight(self.name)} search_region must be one or a list of string."
            )

    def handle_max_price(self: "MarketItemCommonConfig") -> None:
        if self.max_price is None:
            return

        if isinstance(self.max_price, int):
            self.max_price = str(self.max_price)

        # the price should be a number followed by currency name (e.g. 100 USD)
        if not isinstance(self.max_price, str):
            raise ValueError(f"Item {hilight(self.name)} max_price must be a string.")

        if " " in self.max_price:
            price, currency = self.max_price.split(" ", 1)
            if not price.isdigit():
                raise ValueError(
                    f"Item {hilight(self.name)} max_price must be a number followed by currency name."
                )
            try:
                Currency(currency)
            except ValueError as e:
                raise ValueError(
                    f"Item {hilight(self.name)} max_price currency {currency} is not recognized."
                ) from e
        elif not self.max_price.isdigit():
            raise ValueError(
                f"Item {hilight(self.name)} max_price must be a number followed by currency name."
            )

    def handle_min_price(self: "MarketItemCommonConfig") -> None:
        if self.min_price is None:
            return

        if isinstance(self.min_price, int):
            self.min_price = str(self.min_price)

        # the price should be a number followed by currency name (e.g. 100 USD)
        if not isinstance(self.min_price, str):
            raise ValueError(f"Item {hilight(self.name)} min_price must be a string.")

        if " " in self.min_price:
            price, currency = self.min_price.split(" ", 1)
            if not price.isdigit():
                raise ValueError(
                    f"Item {hilight(self.name)} min_price must be a number followed by currency name."
                )
            try:
                Currency(currency)
            except ValueError as e:
                raise ValueError(
                    f"Item {hilight(self.name)} min_price currency {currency} is not recognized."
                ) from e
        elif not self.min_price.isdigit():
            raise ValueError(
                f"Item {hilight(self.name)} min_price must be a number followed by currency name."
            )

    def handle_target_price(self: "MarketItemCommonConfig") -> None:
        """Same shape as ``max_price``: a number, optionally with a currency."""
        if self.target_price is None:
            return

        if isinstance(self.target_price, (int, float)):
            self.target_price = str(int(self.target_price))

        if not isinstance(self.target_price, str):
            raise ValueError(f"Item {hilight(self.name)} target_price must be a string.")

        amount = self.target_price.split(" ", 1)[0] if " " in self.target_price else self.target_price
        if not amount.isdigit():
            raise ValueError(
                f"Item {hilight(self.name)} target_price must be a number, optionally "
                "followed by a currency name."
            )
        if " " in self.target_price:
            currency = self.target_price.split(" ", 1)[1]
            try:
                Currency(currency)
            except ValueError as e:
                raise ValueError(
                    f"Item {hilight(self.name)} target_price currency {currency} is not recognized."
                ) from e

    def handle_excluded_price_patterns(self: "MarketItemCommonConfig") -> None:
        """Accept one pattern or a list, and refuse anything unparseable here.

        Validated at load time rather than at match time on purpose: a pattern
        that cannot be compiled would otherwise fail silently in the middle of a
        search -- and a filter that quietly matches nothing looks exactly like a
        filter that is working and finding nothing to exclude.
        """
        if self.excluded_price_patterns is None:
            return

        if isinstance(self.excluded_price_patterns, str):
            self.excluded_price_patterns = [self.excluded_price_patterns]

        if not isinstance(self.excluded_price_patterns, list) or not all(
            isinstance(x, str) for x in self.excluded_price_patterns
        ):
            raise ValueError(
                f"Item {hilight(self.name)} excluded_price_patterns must be a string "
                "or a list of strings."
            )

        self.excluded_price_patterns = [
            pattern.strip() for pattern in self.excluded_price_patterns if pattern.strip()
        ]
        problems = validate_patterns(self.excluded_price_patterns)
        if problems:
            raise ValueError(f"Item {hilight(self.name)}: {' '.join(problems)}")

    def handle_excluded_price_pattern_sets(self: "MarketItemCommonConfig") -> None:
        """Accept one name or a list.  Whether they exist is the loader's job.

        Checked there and not here because a section cannot see the rest of the
        file, and "you named a set that does not exist" needs the rest of the
        file to be answerable -- see :meth:`Config.expand_price_patterns`.
        """
        if self.excluded_price_pattern_sets is None:
            return
        if isinstance(self.excluded_price_pattern_sets, str):
            self.excluded_price_pattern_sets = [self.excluded_price_pattern_sets]
        if not isinstance(self.excluded_price_pattern_sets, list) or not all(
            isinstance(x, str) for x in self.excluded_price_pattern_sets
        ):
            raise ValueError(
                f"Item {hilight(self.name)} excluded_price_pattern_sets must be a string "
                "or a list of strings."
            )
        self.excluded_price_pattern_sets = [
            name.strip() for name in self.excluded_price_pattern_sets if name.strip()
        ]

    def handle_start_at(self: "MarketItemCommonConfig") -> None:
        """Deprecated, like :meth:`handle_search_interval`."""
        if self.start_at is None:
            return
        self.start_at = validated_start_at(self.start_at, f"Item {hilight(self.name)}")

    def handle_rating(self: "MarketItemCommonConfig") -> None:
        if self.rating is None:
            return
        if isinstance(self.rating, int):
            self.rating = [self.rating]

        if not all(isinstance(x, int) and x >= 1 and x <= 5 for x in self.rating):
            raise ValueError(
                f"Item {hilight(self.name)} rating must be one or a list of integers between 1 and 5 inclusive."
            )

    def handle_prompt(self: "MarketItemCommonConfig") -> None:
        if self.prompt is None:
            return
        if not isinstance(self.prompt, str):
            raise ValueError(f"Item {hilight(self.name)} requires a string prompt, if specified.")

    def handle_extra_prompt(self: "MarketItemCommonConfig") -> None:
        if self.extra_prompt is None:
            return
        if not isinstance(self.extra_prompt, str):
            raise ValueError(
                f"Item {hilight(self.name)} requires a string extra_prompt, if specified."
            )

    def handle_rating_prompt(self: "MarketItemCommonConfig") -> None:
        if self.rating_prompt is None:
            return
        if not isinstance(self.rating_prompt, str):
            raise ValueError(
                f"Item {hilight(self.name)} requires a string rating_prompt, if specified."
            )


@dataclass
class MarketplaceConfig(MarketItemCommonConfig):
    """Generic marketplace config"""

    # name of market, right now facebook is the only supported one
    market_type: str | None = MarketPlace.FACEBOOK.value
    language: str | None = None
    monitor_config: MonitorConfig | None = None

    def handle_market_type(self: "MarketplaceConfig") -> None:
        if self.market_type is None:
            return
        if not isinstance(self.market_type, str):
            raise ValueError(f"Marketplace {hilight(self.market_type)} market must be a string.")
        if self.market_type.lower() != MarketPlace.FACEBOOK.value:
            raise ValueError(
                f"Marketplace {hilight(self.market_type)} market must be {MarketPlace.FACEBOOK.value}."
            )

    def handle_language(self: "MarketplaceConfig") -> None:
        if self.language is None:
            return
        if not isinstance(self.language, str):
            raise ValueError(
                f"Marketplace {hilight(self.market_type)} language, if specified, must be a string."
            )


@dataclass
class ItemConfig(MarketItemCommonConfig):
    """This class defined options that can only be specified for items."""

    # the number of times that this item has been searched
    searched_count: int = 0

    # keywords is required, all others are optional
    search_phrases: List[str] = field(default_factory=list)
    #: Words the listing must have, anywhere -- title or description.
    keywords: List[str] | None = None
    #: Words that exclude it, anywhere.
    antikeywords: List[str] | None = None
    #: The same two rules, narrowed to one half of the listing.
    #:
    #: Both of the keys above read the title and the description glued
    #: together, which is the right default and a poor only option: "no busco
    #: fundas" is a rule about the *title* (every listing of a console mentions
    #: a case somewhere in its description) and "tiene que decir sellado" is a
    #: rule about the *description* (a title has room for four words).  The
    #: two above are unchanged, so a search written before these existed
    #: behaves exactly as it did.
    #:
    #: Where a rule looks decides when it can be answered, which is the part
    #: that reaches the scrapers: a title is on a shop's results grid and a
    #: description is not.  See :mod:`ai_marketplace_monitor.keyword_filters`.
    keywords_title: List[str] | None = None
    keywords_description: List[str] | None = None
    antikeywords_title: List[str] | None = None
    antikeywords_description: List[str] | None = None
    description: str | None = None
    marketplace: str | None = None

    def handle_search_phrases(self: "ItemConfig") -> None:
        if isinstance(self.search_phrases, str):
            self.search_phrases = [self.search_phrases]

        if not isinstance(self.search_phrases, list) or not all(
            isinstance(x, str) for x in self.search_phrases
        ):
            raise ValueError(f"Item {hilight(self.name)} search_phrases must be a list.")
        if len(self.search_phrases) == 0:
            raise ValueError(f"Item {hilight(self.name)} search_phrases list is empty.")

    def handle_antikeywords(self: "ItemConfig") -> None:
        if self.antikeywords is None:
            return

        if isinstance(self.antikeywords, str):
            self.antikeywords = [self.antikeywords]

        if not isinstance(self.antikeywords, list) or not all(
            isinstance(x, str) for x in self.antikeywords
        ):
            raise ValueError(f"Item {hilight(self.name)} antikeywords must be a list of strings.")

    def handle_keywords(self: "ItemConfig") -> None:
        if self.keywords is None:
            return

        if isinstance(self.keywords, str):
            self.keywords = [self.keywords]

        if not isinstance(self.keywords, list) or not all(
            isinstance(x, str) for x in self.keywords
        ):
            raise ValueError(f"Item {hilight(self.name)} keywords must be a list.")

    def _handle_word_list(self: "ItemConfig", key: str) -> None:
        """One of the four scoped word lists: a string, or a list of them.

        Shared rather than written out four times because the four are the same
        rule, and a copy that drifts is how one of them ends up accepting a
        shape the other three reject.
        """
        value = getattr(self, key)
        if value is None:
            return
        if isinstance(value, str):
            value = [value]
            setattr(self, key, value)
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ValueError(
                f"Item {hilight(self.name)} {key} must be a string or a list of strings."
            )

    def handle_keywords_title(self: "ItemConfig") -> None:
        self._handle_word_list("keywords_title")

    def handle_keywords_description(self: "ItemConfig") -> None:
        self._handle_word_list("keywords_description")

    def handle_antikeywords_title(self: "ItemConfig") -> None:
        self._handle_word_list("antikeywords_title")

    def handle_antikeywords_description(self: "ItemConfig") -> None:
        self._handle_word_list("antikeywords_description")

    def handle_description(self: "ItemConfig") -> None:
        if self.description is None:
            return
        if not isinstance(self.description, str):
            raise ValueError(f"Item {hilight(self.name)} description must be a string.")


TMarketplaceConfig = TypeVar("TMarketplaceConfig", bound=MarketplaceConfig)
TItemConfig = TypeVar("TItemConfig", bound=ItemConfig)


class Marketplace(Generic[TMarketplaceConfig, TItemConfig]):
    #: Whether a search has to ask for this platform before it runs there.
    #:
    #: False for the marketplaces the monitor was built around: a search that
    #: says nothing runs on all of them, which is what "a platform is a
    #: capability of the program, not something you install" means in practice.
    #:
    #: True for the shops, and the reason is that they are a different kind of
    #: result rather than more of the same.  A search watching for a used PS5 on
    #: Facebook does not want a page of retail boxes at list price, and switching
    #: one on for every existing search on upgrade is a change nobody asked for
    #: -- arriving as notifications.  Opting in is one section in the file and
    #: one switch in the interface.
    opt_in: bool = False

    #: Cookie names that are the site's *bot check* talking about this browser.
    #:
    #: A third class of cookie, and telling it from the other two is the whole
    #: point of declaring it:
    #:
    #: * a **login** cookie is the site's relationship with the user.  It is what
    #:   an import is for, it cannot be re-earned by the monitor, and it is never
    #:   thrown away.
    #: * a **device** cookie is the site saying "I have seen this browser
    #:   before".  Usually worth keeping -- see
    #:   :func:`~ai_marketplace_monitor.session.save_device_state`, which hangs on
    #:   to Facebook's ``datr`` precisely so a failed login looks like one device
    #:   retrying rather than a stream of new ones.
    #: * a **bot-check** cookie is the same idea after the site has ruled
    #:   *against* that identity.  Then keeping it is the opposite of helpful,
    #:   and that is the case this list exists for.
    #:
    #: Two moments act on it, and neither needs the platform to do anything
    #: beyond naming the cookies:
    #:
    #: * a refusal drops them (:meth:`discard_challenge_state`), so the next
    #:   browser profile starts with the account and an identity the wall has no
    #:   history for.  Not on a good page: while they work they are a clearance
    #:   worth more than any timer.
    #: * an import drops them, because the ones in an export belong to the
    #:   browser it came from.
    #:
    #: **Empty is an answer, not an omission**, and a platform that has none
    #: should say so with a comment rather than leaving the default: Mercado
    #: Libre's wall is its own and hands out no clearance token, so there is
    #: nothing here to throw away.  A platform behind PerimeterX, Cloudflare,
    #: DataDome or Kasada almost certainly has one, and it is found by looking at
    #: the jar rather than by guessing -- see `lider.py` and `sodimac.py`.
    challenge_cookies: Tuple[str, ...] = ()

    def discard_challenge_state(self: "Marketplace") -> None:
        """Throw away the device identity the site has just decided against.

        Called from a platform's own "we were refused" path, and a no-op for the
        platforms that declare no such cookies.

        Both halves matter and they are different stores.  The **file** is what
        seeds the next profile, so a burned identity left there comes back on
        every fresh start for ever -- which is exactly what happened: a user who
        deleted every profile to start clean was walled one second after the
        browser opened, wearing the id we had just handed it. The **live
        browser** is what would keep sending it for the rest of this run, so the
        cooldown would expire and the very next request would arrive flagged.
        """
        if not self.challenge_cookies:
            return
        label = getattr(self, "label", self.name)
        try:
            removed = drop_cookies(self.name, self.challenge_cookies)
        except KeyboardInterrupt:
            raise
        except Exception:
            removed = 0
            if self.logger:
                self.logger.debug(
                    f"Could not clear the stored {self.name} challenge cookies", exc_info=True
                )
        # Resolved defensively, unlike the places that ask on a served page: this
        # runs on the refusal path, where the browser may well be the thing that
        # went wrong.  A page whose process has died raises on `.context`, and a
        # refusal must not become a crash on the way to reporting itself.
        context = self.context
        if context is None and self.page is not None:
            try:
                context = self.page.context
            except Exception:
                context = None
        if context is not None:
            for cookie_name in self.challenge_cookies:
                try:
                    context.clear_cookies(name=cookie_name)
                except KeyboardInterrupt:
                    raise
                except Exception:
                    # An older Playwright without the filter, or a context that
                    # is going away.  The stored file is the half that decides
                    # what the next profile starts from.
                    continue
        if removed and self.logger:
            self.logger.info(
                f"""{hilight("[Login]", "info")} Dropped {removed} {label} bot-check """
                """cookie(s); the next browser profile starts with the account and a """
                """clean device id.""",
                extra=aimm_event(
                    "challenge_state_dropped", marketplace=self.name, cookies=removed
                ),
            )

    def __init__(
        self: "Marketplace",
        name: str,
        context: BrowserContext | None,
        keyboard_monitor: KeyboardMonitor | None = None,
        logger: Logger | None = None,
    ) -> None:
        self.name = name
        self.context = context
        self.keyboard_monitor = keyboard_monitor
        self.translator = Translator()
        self.logger = logger
        self.page: Page | None = None
        #: Throw this browser away and come back with a new one on a new profile.
        #:
        #: Filled in by whoever built this object, because opening a browser
        #: needs things a marketplace does not have: the launch options, the
        #: proxy, and -- for a lane -- that lane's own ``Playwright``.  ``None``
        #: for a marketplace nobody offered one to, and the callers treat that
        #: as "recovery is not available here" rather than as an error.
        #:
        #: Safe to call inline from a search, and only from there: a Playwright
        #: object belongs to the thread that made it, and during a search that
        #: thread is this one.
        self.renew_browser: Callable[[], BrowserContext] | None = None

    def renew_browser_now(self: "Marketplace") -> bool:
        """Ask for a fresh browser on a fresh profile.  False if there is none.

        Drops the page and context this object was holding before asking, so a
        failure cannot leave it driving a browser that has been closed -- the
        next :meth:`create_page` then builds against whatever comes back.
        """
        renew = self.renew_browser
        if renew is None:
            return False
        self.page = None
        try:
            context = renew()
        except KeyboardInterrupt:
            raise
        except Exception as error:
            if self.logger:
                self.logger.error(
                    f"""{hilight("[Browser]", "fail")} Could not open a fresh browser for """
                    f"""{hilight(self.name)}: {error}""",
                    extra=aimm_event("browser_renew", marketplace=self.name, ok=False),
                )
            return False
        if context is None:
            return False
        self.context = context
        return True

    def junk_price(
        self: "Marketplace", item: Listing, item_config: TItemConfig
    ) -> bool:
        """Whether this listing's price is one the user told us to ignore.

        The first thing every ``check_listing`` asks, and deliberately so: the
        excluded patterns describe prices that are not asking prices at all, and
        letting one through into the minimum/maximum/target comparisons is how a
        placeholder 999999 becomes a group's "highest price" and a 0 becomes its
        "cheapest".

        Read from the item's platform section first and the platform's own
        defaults second, which is the precedence every other option here uses.
        A pattern list that somehow failed to compile excludes nothing rather
        than everything: the loader has already refused the bad ones, so
        reaching here at all means something unexpected, and the safe reading of
        an unusable filter is that it filters nothing.
        """
        patterns = getattr(item_config, "excluded_price_patterns", None) or getattr(
            self.config, "excluded_price_patterns", None
        )
        if not patterns:
            return False
        try:
            compiled = compile_patterns(patterns)
        except ValueError:
            return False
        hit = price_pattern_match(item.price, compiled)
        if hit is None:
            return False
        if self.logger:
            self.logger.info(
                f"""{hilight("[Skip]", "fail")} Exclude {hilight(item.title)}: price """
                f"""{hilight(item.price or "", "fail")} matches the excluded pattern """
                f"""{hilight(hit.source)}."""
            )
        return True

    @classmethod
    def get_config(cls: Type["Marketplace"], **kwargs: Any) -> TMarketplaceConfig:
        raise NotImplementedError("get_config method must be implemented by subclasses.")

    @classmethod
    def get_item_config(cls: Type["Marketplace"], **kwargs: Any) -> TItemConfig:
        raise NotImplementedError("get_config method must be implemented by subclasses.")

    @classmethod
    def item_config_class(cls: Type["Marketplace"]) -> Type[TItemConfig]:
        """The dataclass this marketplace reads an item's options into.

        The config loader inspects it to decide which of an item's options this
        platform understands, so it has to be reachable without building one.
        """
        raise NotImplementedError("item_config_class must be implemented by subclasses.")

    @classmethod
    def session_domains(cls: Type["Marketplace"]) -> Tuple[str, ...]:
        """Domains whose cookies belong to this marketplace.

        Used to filter a session imported from the user's own browser: a paste
        that turns out to be the wrong export is a normal mistake, and loading
        somebody's unrelated session into the scraping profile would be a bad
        way to find out.  Empty means "cannot say", which disables the check.
        """
        return ()

    @classmethod
    def handles_url(cls: Type["Marketplace"], url: str) -> bool:
        """Whether a listing URL belongs to this marketplace.

        Used by the interactive ``--check <url>`` path to pick the marketplace
        that can actually read the page.
        """
        return False

    @classmethod
    def validate_item_config(
        cls: Type["Marketplace"],
        item_config: "ItemConfig",
        marketplace_config: "MarketplaceConfig",
    ) -> None:
        """Refuse an item this marketplace cannot actually search for.

        What is missing depends on the platform, so each one answers for itself.
        The default is that a search phrase (already required) is enough.
        """
        return None

    def configure(
        self: "Marketplace", config: TMarketplaceConfig, translator: Translator | None = None
    ) -> None:
        self.config = config
        if translator is not None:
            self.translator = translator

    def set_context(self: "Marketplace", context: BrowserContext | None = None) -> None:
        if context is not None:
            self.context = context
            self.page = None

    def stop(self: "Marketplace") -> None:
        if self.context is not None:
            # stop closing the browser since Ctrl-C will kill playwright,
            # leaving browser in a dysfunctional status.
            # see
            #   https://github.com/microsoft/playwright-python/issues/1170
            # for details.  The monitor closes the persistent context on a
            # clean shutdown so the profile is flushed to disk.
            self.context = None
            self.page = None

    def create_page(self: "Marketplace", swap_proxy: bool = False) -> Page:
        """Take a page on the shared persistent context.

        ``swap_proxy`` is accepted for call-site compatibility but no longer does
        anything: a persistent profile binds its proxy for the whole browser
        lifetime, so a page cannot be moved onto a different one.  The monitor
        warns at launch when a rotating proxy list is configured.
        """
        assert self.context is not None
        del swap_proxy

        if self.page is None:
            # Claim the blank page the persistent context opens with, so a
            # visible browser does not sit there with a stray about:blank tab.
            blank = next(
                (page for page in self.context.pages if page.url in ("about:blank", "")), None
            )
            self.page = blank or self.context.new_page()
        self._close_stray_blanks()
        return self.page

    def _close_stray_blanks(self: "Marketplace") -> None:
        """Close any leftover blank tab beside the one actually in use.

        Two marketplaces sharing one browser each want a tab, and the first one
        takes the blank page the profile opened with -- so the second opens its
        own.  Get that order wrong once (a search skipped before it navigated, a
        page dropped and remade) and the browser is left showing an empty
        about:blank beside the real one, which reads as the monitor having
        opened a window for nothing.

        Never the last page: closing every tab of a persistent context takes the
        browser down with it.  Safe to do here without any locking because a
        Playwright context belongs to exactly one thread -- the whole reason
        lanes exist -- so no other flow can be halfway through claiming a tab of
        this browser while this runs.
        """
        if self.context is None or self.page is None:
            return
        try:
            pages = list(self.context.pages)
        except Exception:
            return
        # Counted down rather than re-read, and iterated over a copy: closing a
        # page removes it from `context.pages`, so mutating the list being
        # walked would skip every second tab.
        remaining = len(pages)
        for page in pages:
            if page is self.page or remaining <= 1:
                continue
            try:
                if page.url in ("about:blank", ""):
                    page.close()
                    remaining -= 1
            except Exception:
                # A tab that will not close is not worth failing a search over.
                continue

    def seed_session(self: "Marketplace") -> bool:
        """Import a previously saved storage state into a brand-new profile.

        Only for the first run on a fresh profile: it carries a session saved by
        an older version (which stored cookies rather than a profile) across the
        upgrade, instead of forcing a re-login.  An established profile owns its
        own cookies and must not be overwritten from a stale file.
        """
        if self.context is None:
            return False
        state = load_session(self.name)
        if not state or not state.get("cookies"):
            return False
        try:
            self.context.add_cookies(state["cookies"])
            return True
        except Exception:
            return False

    def save_session(self: "Marketplace") -> bool:
        """Persist the current session for the next run."""
        if self.page is None:
            return False
        return save_session(self.name, self.page.context)

    def login_interactively(self: "Marketplace", timeout: int = 3600) -> bool:
        """Sign in with the user driving, then save the session.

        Marketplaces without a login concept have nothing to do here.
        """
        raise NotImplementedError(f"{self.name} does not support interactive login.")

    def goto_url(self: "Marketplace", url: str, attempt: int = 0) -> None:
        # Before the navigation, and so before each of the ten retries: a page
        # that keeps failing is exactly where a forced pause would otherwise
        # wait out a minute of retries for nothing.
        raise_if_cancelled()
        try:
            assert self.page is not None
            if self.logger:
                self.logger.debug(f"{hilight('[Retrieve]', 'info')} Navigating to {url}")
            self.page.goto(url, timeout=0)
            self.page.wait_for_load_state("domcontentloaded")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            if attempt == 10:
                raise RuntimeError(f"Failed to navigate to {url} after 10 attempts. {e}") from e
            time.sleep(5)
            self.goto_url(url, attempt + 1)

    def search(self: "Marketplace", item: TItemConfig) -> Generator[Listing, None, None]:
        raise NotImplementedError("Search method must be implemented by subclasses.")

    def recheck_listing(
        self: "Marketplace", post_url: str, item_config: TItemConfig
    ) -> Tuple[ListingStatus, Listing | None]:
        """Re-read one listing page and say what became of it.

        The second half of the pair is the refreshed snapshot, present only for
        :attr:`ListingStatus.ACTIVE`.  Anything the marketplace cannot decide
        must come back as :attr:`ListingStatus.UNKNOWN` rather than as an
        exception with a guessed meaning -- see the enum for why that matters.
        """
        raise NotImplementedError(f"{self.name} cannot re-check a listing.")


class WebPage:
    def __init__(
        self: "WebPage",
        page: Page,
        translator: Translator | None = None,
        logger: Logger | None = None,
    ) -> None:
        self.page = page
        self.translator: Translator = Translator() if translator is None else translator
        self.logger = logger

    def _parent_with_cond(
        self: "WebPage",
        element: Locator | ElementHandle | None,
        cond: Callable,
        ret: Callable | int,
    ) -> str:
        """Finding a parent element

        Starting from `element`, finding its parents, until `cond` matches, then return the `ret`th children,
        or a callable.
        """
        if element is None:
            return ""
        # get up at the DOM level, testing the children elements with cond,
        # apply the res callable to return a string
        parent: ElementHandle | None = (
            element.element_handle() if isinstance(element, Locator) else element
        )
        # look for parent of approximate_element until it has two children and the first child is the heading
        while parent:
            children = parent.query_selector_all(":scope > *")
            if cond(children):
                if isinstance(ret, int):
                    return children[ret].text_content() or self.translator("**unspecified**")
                else:
                    return ret(children)
            parent = parent.query_selector("xpath=..")
        raise ValueError("Could not find parent element with condition.")

    def _children_with_cond(
        self: "WebPage",
        element: Locator | ElementHandle | None,
        cond: Callable,
        ret: Callable | int,
    ) -> str:
        if element is None:
            return ""
        # Getting the children of an element, test condition, return the `index` or apply res
        # on the children element if the condition is met. Otherwise locate the first child and repeat the process.
        child: ElementHandle | None = (
            element.element_handle() if isinstance(element, Locator) else element
        )
        # look for parent of approximate_element until it has two children and the first child is the heading
        while child:
            children = child.query_selector_all(":scope > *")
            if cond(children):
                if isinstance(ret, int):
                    return children[ret].text_content() or self.translator("**unspecified**")
                return ret(children)
            if not children:
                raise ValueError("Could not find child element with condition.")
            # or we could use query_selector("./*[1]")
            child = children[0]
        raise ValueError("Could not find child element with condition.")
