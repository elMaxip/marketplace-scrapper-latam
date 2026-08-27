"""Mercado Libre support, driven through the same browser Facebook uses.

Why not the official API: since April 2025 Mercado Libre answers
``GET /sites/{site}/search`` with ``403 forbidden`` unless the caller presents an
OAuth token *linked to a user account* -- an app-only token is refused with
"you must use an access token linked to a user".  That would mean registering an
application, running the authorization-code flow against a real account and
refreshing a token every six hours, for data the site serves to any visitor.  So
this reads the public search pages instead, exactly as the Facebook marketplace
does, and reuses the browser context the monitor already runs.

One operational caveat, found by testing rather than assumed: Mercado Libre
serves ``403`` to a headless Chromium even with the automation flags turned off,
while the same profile in headed mode gets the real page.  Running the monitor
with ``--headless`` therefore disables this marketplace in practice, and the
scraper says so in the log rather than reporting an empty market.

The search URL grammar below was likewise read off the site's own filter links
and then verified against live result counts, not guessed.
"""

import re
import time
from dataclasses import dataclass
from logging import Logger
from typing import Any, Dict, Generator, List, Tuple, Type
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page  # type: ignore

from . import control
from .listing import Listing
from .marketplace import ItemConfig, ListingStatus, Marketplace, MarketplaceConfig
from .observations import is_known, record_observation
from .session import load_session
from .utils import (
    BaseConfig,
    CounterItem,
    KeyboardMonitor,
    aimm_event,
    counter,
    extract_price,
    fold_text,
    hilight,
    is_substring,
)

#: Site id -> the host that serves its search pages.  Only the ids Mercado Libre
#: itself publishes; the Chilean one is the one verified against live pages.
SEARCH_HOSTS: Dict[str, str] = {
    "MLC": "https://listado.mercadolibre.cl",
    "MLA": "https://listado.mercadolibre.com.ar",
    "MLM": "https://listado.mercadolibre.com.mx",
    "MLU": "https://listado.mercadolibre.com.uy",
    "MCO": "https://listado.mercadolibre.com.co",
    "MPE": "https://listado.mercadolibre.com.pe",
    "MLB": "https://lista.mercadolivre.com.br",
}

#: Currency each site prices in, needed by the price-range filter, whose syntax
#: is ``_PriceRange_300000CLP-600000CLP``.
SITE_CURRENCIES: Dict[str, str] = {
    "MLC": "CLP",
    "MLA": "ARS",
    "MLM": "MXN",
    "MLU": "UYU",
    "MCO": "COP",
    "MPE": "PEN",
    "MLB": "BRL",
}

#: Condition -> Mercado Libre's filter id.  Each one was confirmed by loading
#: ``..._ITEM*CONDITION_<id>`` and reading back the filter chip the site shows.
CONDITION_IDS: Dict[str, str] = {
    "new": "2230284",  # "Nuevo"
    "used": "2230581",  # "Usado"
    "refurbished": "2230582",  # "Reacondicionado"
    "open_box": "46759135",  # "Caja abierta"
}

#: The label the site prints on a card, folded to the config's vocabulary.
CONDITION_LABELS: Dict[str, str] = {
    "nuevo": "new",
    "new": "new",
    "usado": "used",
    "used": "used",
    "reacondicionado": "refurbished",
    "recondicionado": "refurbished",
    "refurbished": "refurbished",
    "caja abierta": "open_box",
    "open box": "open_box",
}

#: Where the seller ships from.
SHIPPING_ORIGINS: Dict[str, str] = {
    "local": "10215068",
    "international": "10215069",
}

#: The host each site signs in on.  Only used to send the user somewhere sane;
#: the login flow itself is whatever the site decides to show.
LOGIN_HOSTS: Dict[str, str] = {
    "MLC": "https://www.mercadolibre.cl",
    "MLA": "https://www.mercadolibre.com.ar",
    "MLM": "https://www.mercadolibre.com.mx",
    "MLU": "https://www.mercadolibre.com.uy",
    "MCO": "https://www.mercadolibre.com.co",
    "MPE": "https://www.mercadolibre.com.pe",
    "MLB": "https://www.mercadolivre.com.br",
}

#: Parts of a URL that mean we were sent somewhere other than the page we asked
#: for: a sign-in gateway, a registration form, a device challenge.
#:
#: Mercado Libre does not answer an over-eager visitor with an error page.  It
#: answers with an invitation to create an account, which parses as a perfectly
#: valid page and would otherwise be read as "this listing has no title".
WALL_URL_MARKERS: Tuple[str, ...] = (
    "login.mercadoli",
    "login.mercadoliv",
    "/lgz/login",
    "/jms/",
    "account-verification",
    "/registration",
    "myaccount.mercadoli",
    "challenge",
    "captcha",
)

#: Phrases the same walls carry, for the cases that do not change the URL.
WALL_TEXT_MARKERS: Tuple[str, ...] = (
    "hubo un error accediendo",
    "ingresa a tu cuenta",
    "crea tu cuenta",
    "para continuar, inicia sesion",
    "para continuar, ingresa a tu cuenta",
    "verifica que eres una persona",
    "acesse sua conta",
    "crie sua conta",
    "sign in to your account",
)

#: Results per page, which is what ``_Desde_`` counts in.
PAGE_SIZE = 50

#: Pages walked per search phrase unless the config says otherwise.  One page is
#: fifty listings, which is already more than a Facebook search returns.
DEFAULT_MAX_PAGES = 1

#: Options that only make sense for a marketplace with a physical location.
#: Mercado Libre's search has no location facet at all -- its equivalents are
#: about shipping -- so these are ignored here rather than approximated.
IGNORED_LOCATION_OPTIONS = ("search_city", "city_name", "radius", "search_region",
                            "seller_locations")


class MercadoLibreWall(Exception):
    """Mercado Libre answered with a sign-in or verification page.

    Not an error in the ordinary sense -- the request succeeded, the site simply
    declined to serve it.  It is its own type because the only correct response
    is to stop asking for a while, which is different from a retry.
    """

    def __init__(self: "MercadoLibreWall", reason: str, url: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.url = url


def _slugify(phrase: str) -> str:
    """Turn a search phrase into the path segment the site uses.

    "PlayStation 5" -> "playstation-5", which is what the site's own search form
    produces.
    """
    folded = (
        phrase.strip()
        .lower()
        .replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
        .replace("ñ", "n")
    )
    kept = [char if char.isalnum() else " " for char in folded]
    return "-".join("".join(kept).split()) or "-"


#: A listing id: the three-letter site code, an optional ``U`` for a used
#: catalog product, then the number -- with or without the hyphen the classic
#: listing URLs put there.
_LISTING_ID_RE = re.compile(r"([A-Z]{3})(U?)-?(\d{6,})")


def listing_id_from_url(url: str) -> str:
    """The item id inside a Mercado Libre URL.

    Three shapes exist and all carry the same id: ``/p/MLC61403702`` (catalog
    product), ``/up/MLCU4708792716`` (used catalog product) and
    ``articulo.mercadolibre.cl/MLC-1234567890-titulo-_JM`` (a plain listing).
    """
    path = urlparse(url).path
    matched = _LISTING_ID_RE.search(path)
    if matched:
        return "".join(matched.groups())
    return path.rstrip("/").split("/")[-1]


@dataclass
class MercadoLibreItemCommonConfig(BaseConfig):
    """Options that Mercado Libre understands, settable per item or per market.

    Deliberately small: every option here maps to a filter the site actually
    has.  Facebook's location options have no counterpart and are ignored (see
    ``IGNORED_LOCATION_OPTIONS``).
    """

    #: One or more of new / used / refurbished / open_box.
    condition: List[str] | None = None
    #: Only listings whose shipping is free.
    free_shipping: bool | None = None
    #: "local" or "international" -- where the seller ships from.
    shipping_origin: str | None = None
    #: Result pages to walk per search phrase.
    max_pages: int | None = None
    #: Which Mercado Libre site to search: MLC (Chile), MLA (Argentina), ...
    #: Per item, because two products can live in two countries; the
    #: ``[marketplace.mercadolibre]`` section sets the default for the ones
    #: that do not say.
    site: str | None = None

    def handle_condition(self: "MercadoLibreItemCommonConfig") -> None:
        if self.condition is None:
            return
        if isinstance(self.condition, str):
            self.condition = [self.condition]
        if not isinstance(self.condition, list) or not all(
            isinstance(x, str) for x in self.condition
        ):
            raise ValueError(f"Item {hilight(self.name)} condition must be a list of strings.")
        self.condition = [x.lower() for x in self.condition]
        unknown = [x for x in self.condition if x not in CONDITION_IDS]
        if unknown:
            raise ValueError(
                f"Item {hilight(self.name)} has unsupported Mercado Libre condition(s) "
                f"{', '.join(unknown)}. Supported: {', '.join(CONDITION_IDS)}."
            )

    def handle_free_shipping(self: "MercadoLibreItemCommonConfig") -> None:
        if self.free_shipping is None:
            return
        if not isinstance(self.free_shipping, bool):
            raise ValueError(f"Item {hilight(self.name)} free_shipping must be true or false.")

    def handle_shipping_origin(self: "MercadoLibreItemCommonConfig") -> None:
        if self.shipping_origin is None:
            return
        if not isinstance(self.shipping_origin, str):
            raise ValueError(f"Item {hilight(self.name)} shipping_origin must be a string.")
        self.shipping_origin = self.shipping_origin.lower()
        if self.shipping_origin not in SHIPPING_ORIGINS:
            raise ValueError(
                f"Item {hilight(self.name)} shipping_origin must be one of "
                f"{', '.join(SHIPPING_ORIGINS)}."
            )

    def handle_max_pages(self: "MercadoLibreItemCommonConfig") -> None:
        if self.max_pages is None:
            return
        if not isinstance(self.max_pages, int) or self.max_pages < 1:
            raise ValueError(f"Item {hilight(self.name)} max_pages must be a positive integer.")

    def handle_site(self: "MercadoLibreItemCommonConfig") -> None:
        if self.site is None:
            return
        if not isinstance(self.site, str):
            raise ValueError(f"Item {hilight(self.name)} site must be a string.")
        self.site = self.site.upper()
        if self.site not in SEARCH_HOSTS:
            raise ValueError(
                f"Item {hilight(self.name)} site {self.site} is not supported. "
                f"Supported sites: {', '.join(SEARCH_HOSTS)}."
            )


@dataclass
class MercadoLibreMarketplaceConfig(MarketplaceConfig, MercadoLibreItemCommonConfig):
    """The ``[marketplace.mercadolibre]`` section.

    ``site`` is inherited from :class:`MercadoLibreItemCommonConfig`: set here
    it is the default for every item that does not name one of its own.
    """

    #: Overrides the generic default, which is Facebook: a section that builds
    #: this class is a Mercado Libre section by definition.
    market_type: str | None = "mercadolibre"

    def handle_market_type(self: "MercadoLibreMarketplaceConfig") -> None:
        """Accept this marketplace's own type.

        The generic handler only knows Facebook; a `[marketplace.mercadolibre]`
        section is otherwise rejected before it is ever used.
        """
        if self.market_type is None:
            return
        if not isinstance(self.market_type, str):
            raise ValueError(f"Marketplace {hilight(self.name)} market_type must be a string.")
        if self.market_type.lower() != MercadoLibreMarketplace.name:
            raise ValueError(
                f"Marketplace {hilight(self.name)} market_type must be "
                f"{MercadoLibreMarketplace.name}."
            )


@dataclass
class MercadoLibreItemConfig(ItemConfig, MercadoLibreItemCommonConfig):
    pass


class MercadoLibreMarketplace(Marketplace):
    name = "mercadolibre"

    #: The site searched when the config does not say.
    DEFAULT_SITE = "MLC"

    def __init__(
        self: "MercadoLibreMarketplace",
        name: str,
        context: BrowserContext | None,
        keyboard_monitor: KeyboardMonitor | None = None,
        logger: Logger | None = None,
    ) -> None:
        super().__init__(name, context, keyboard_monitor, logger)
        self.page: Page | None = None
        self._warned_about_location = False

    @classmethod
    def get_config(
        cls: Type["MercadoLibreMarketplace"], **kwargs: Any
    ) -> MercadoLibreMarketplaceConfig:
        return MercadoLibreMarketplaceConfig(**kwargs)

    @classmethod
    def get_item_config(
        cls: Type["MercadoLibreMarketplace"], **kwargs: Any
    ) -> MercadoLibreItemConfig:
        return MercadoLibreItemConfig(**kwargs)

    @classmethod
    def item_config_class(cls: Type["MercadoLibreMarketplace"]) -> Type[MercadoLibreItemConfig]:
        return MercadoLibreItemConfig

    @classmethod
    def session_domains(cls: Type["MercadoLibreMarketplace"]) -> Tuple[str, ...]:
        """Every Mercado Libre host family, derived from the sites it knows.

        ``https://www.mercadolibre.com.ar`` -> ``mercadolibre.com.ar``: the
        first label is dropped and the rest is the domain cookies are set on.
        """
        domains = {urlparse(host).netloc.split(".", 1)[1] for host in LOGIN_HOSTS.values()}
        return tuple(sorted(domains))

    @classmethod
    def handles_url(cls: Type["MercadoLibreMarketplace"], url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return host.endswith("mercadolibre.cl") or "mercadolibre." in host or "mercadolivre." in host

    @classmethod
    def validate_item_config(
        cls: Type["MercadoLibreMarketplace"],
        item_config: ItemConfig,
        marketplace_config: MarketplaceConfig,
    ) -> None:
        """Nothing extra is required: a search phrase is enough.

        Facebook demands a ``search_city`` because its results are anchored to
        one; Mercado Libre searches a whole site.
        """
        return None

    # ------------------------------------------------------------------ #
    # Being let in
    # ------------------------------------------------------------------ #

    def home_url(self: "MercadoLibreMarketplace", item_config: ItemConfig | None = None) -> str:
        """The site's own front page, for signing in and for probing."""
        site = self.site_of(item_config) if item_config is not None else self.DEFAULT_SITE
        return LOGIN_HOSTS.get(site, LOGIN_HOSTS[self.DEFAULT_SITE])

    def account_url(self: "MercadoLibreMarketplace", item_config: ItemConfig | None = None) -> str:
        """The page that only a signed-in visitor is allowed to see."""
        return self.home_url(item_config).replace("https://www.", "https://myaccount.")

    def _on_account_page(
        self: "MercadoLibreMarketplace", url: str, item_config: ItemConfig | None = None
    ) -> bool:
        """Whether this is somewhere inside the account area, wherever it lives.

        Asked instead of "is the host still ``myaccount``", and that is a fix.
        Mercado Libre has moved the account area behind that host: asking for
        ``myaccount.mercadolibre.cl`` while signed in now ends on
        ``www.mercadolibre.cl/resumen`` -- the page titled "Resumen".  A check
        that demanded the host stay put therefore read the site's own redirect
        as "bounced back to the front page", which is how a perfectly good
        session came to be reported as unrecognised.

        Verified against the live site, both ways: signed in ends on
        ``/resumen``; anonymous ends on ``.../jms/mlc/lgz/login``, which the wall
        markers catch.

        So the test is deliberately loose about *which* page and strict about
        one thing only -- that we were handed a page rather than dumped on the
        front door.  The next time the site renames it, this still holds.
        """
        landed = urlparse(url or "")
        host = landed.netloc.lower()
        if not host:
            return False
        if host.startswith("myaccount."):
            return True
        if host != urlparse(self.home_url(item_config)).netloc.lower():
            return False
        return landed.path.strip("/") != ""

    def is_signed_in(self: "MercadoLibreMarketplace") -> bool:
        """Whether the browser is carrying a Mercado Libre session.

        Answered by loading the account page and looking at where we ended up.
        A signed-in visitor is handed a page of their account; an anonymous one
        is sent to the sign-in gateway.  Reading the page for a phrase would be
        a question about language; where it landed is not, and it is the same
        answer in every country.

        Costs one navigation, so it is asked at the points where that is worth
        it -- confirming a sign-in, checking an imported session -- and never in
        a polling loop.

        Three things here are fixes rather than design, and all three produced
        the same complaint: "it says the imported session is not recognised, and
        the browser is plainly signed in".

        * ``myaccount.mercadoli`` is one of :data:`WALL_URL_MARKERS`, and the
          account page *is* on that host.  Asked plainly, :meth:`wall_reason`
          therefore reported the page we deliberately opened as a redirect away
          from it, so this method could never return True for anybody -- with an
          imported session, a saved one, or a sign-in just completed by hand.
          The markers answer "were we sent somewhere else?", so the page that was
          asked for is passed in and a marker matching it is not an answer.
        * The account area has moved off that host -- see
          :meth:`_on_account_page` -- so the second half of the old test, "we are
          still on ``myaccount``", said no to every signed-in session too.
        * The probe used to run on the marketplace's own tab and leave it parked
          on the account page: the stray Mercado Libre tab titled "Resumen",
          open in the window Facebook was searching in.  It now borrows a tab of
          its own when the marketplace has not opened one, and gives it back.
        """
        if self.context is None and self.page is None:
            return False
        account = self.account_url()
        # A tab of the probe's own when there is no working tab to borrow, and
        # never left behind: a health check must not add a window to a browser
        # somebody is watching.
        working = self.page
        temporary = False
        if working is None:
            assert self.context is not None
            self.page = self.context.new_page()
            temporary = True
        try:
            self.goto_url(account)
            landed = self.page.url or ""
            on_account = self._on_account_page(landed)
            # Asked either way rather than only when it can change the verdict:
            # when the answer is "not signed in", *which* refusal it was is the
            # whole of what the reader needs.
            reason = self.wall_reason(asked_for=account)
            signed_in = on_account and not reason
            if self.logger:
                # Enough to tell "the cookies are wrong" from "the site sent us
                # somewhere else", without printing anything that is a session.
                # This is the line that answered it here: asked for
                # `myaccount.mercadolibre.cl`, landed on `www...` -- a redirect
                # the site performs for signed-in visitors, not a refusal.
                self.logger.debug(
                    f"""{hilight("[Login]", "info")} Mercado Libre session probe: asked """
                    f"""for {account}, landed on {landed or "nothing"}"""
                    f"""{f", {reason}" if reason else ""}.""",
                    extra=aimm_event(
                        "session_probe",
                        marketplace=self.name,
                        asked_for=account,
                        landed=landed,
                        on_account=on_account,
                        wall=reason or None,
                        cookies=self._session_cookie_names(),
                        signed_in=signed_in,
                    ),
                )
            return signed_in
        except KeyboardInterrupt:
            raise
        except Exception as error:
            if self.logger:
                self.logger.debug(
                    f"Mercado Libre session probe could not reach {account}: {error}"
                )
            return False
        finally:
            if temporary:
                probe, self.page = self.page, working
                try:
                    # Never the last tab: a persistent context with no pages is
                    # a browser that has closed itself.
                    if probe is not None and self.context is not None and len(self.context.pages) > 1:
                        probe.close()
                except Exception:
                    pass

    def _session_cookie_names(self: "MercadoLibreMarketplace") -> List[str]:
        """Which Mercado Libre cookies this browser carries -- names only.

        Names and never values: a cookie value *is* the session, and a log line
        carrying one is a log line that signs somebody in.  The names answer the
        question that actually comes up when a session is refused -- whether the
        sign-in cookies reached this browser at all, or only anonymous ones did.
        """
        if self.context is None:
            return []
        try:
            cookies = self.context.cookies()
        except Exception:
            return []
        wanted = self.session_domains()
        return sorted(
            {
                str(cookie.get("name"))
                for cookie in cookies
                if any(str(cookie.get("domain", "")).endswith(domain) for domain in wanted)
            }
        )

    def wall_reason(self: "MercadoLibreMarketplace", asked_for: str = "") -> str:
        """Why the open page is not the page that was asked for, or "".

        Two signals, because the wall arrives in two shapes: a redirect to a
        sign-in host (the URL changes) and an in-place "create an account to
        continue" panel (it does not).  A password field is the third, and is
        what catches a login form this list has never seen.

        ``asked_for`` is the URL this navigation requested.  A marker that
        matches it as well as the page we are on says nothing -- we are where we
        meant to be -- and reading that as a wall is how the account page came to
        be reported as a redirect to itself.
        """
        page = self.page
        if page is None:
            return ""
        try:
            url = fold_text(page.url or "")
        except Exception:
            return ""
        target = fold_text(asked_for or "")
        for marker in WALL_URL_MARKERS:
            if marker in url and marker not in target:
                return f"redirected to {marker}"

        try:
            if page.query_selector('input[type="password"]') is not None:
                return "a sign-in form"
        except Exception:
            pass

        try:
            body = fold_text(page.inner_text("body", timeout=5000))
        except Exception:
            return ""
        for marker in WALL_TEXT_MARKERS:
            if marker in body:
                return f"asked us to sign in ({marker!r})"
        return ""

    def has_saved_session(self: "MercadoLibreMarketplace") -> bool:
        """Whether a Mercado Libre sign-in has ever been completed and saved."""
        state = load_session(self.name)
        return bool(state and state.get("cookies"))

    def _sign_in_advice(self: "MercadoLibreMarketplace") -> str:
        """What to do about a refusal, in the order worth trying.

        Headless first because it is the cheaper mistake: Mercado Libre serves
        this same wall to a headless Chromium on the very first page, so a
        monitor started with ``--headless`` never gets anywhere and no amount of
        signing in will help.
        """
        return (
            "Mercado Libre answers a headless browser this way from the first page, so check "
            "the monitor is not running with --headless. Otherwise it is asking for an "
            "account: run `ai-marketplace-monitor --login` once and sign in to Mercado Libre "
            "in the window it opens, and the session is kept in the browser profile."
        )

    def _hit_wall(self: "MercadoLibreMarketplace", reason: str) -> None:
        """Record that the site refused us, and say what to do about it.

        The cooldown grows with each consecutive refusal, whether or not a
        session is signed in: there is no configuration that stops Mercado Libre
        from being read anonymously, so a wall always means "wait, then try
        again" rather than "give up until somebody signs in".
        """
        block = control.block_marketplace(self.name, reason=reason, announce=True)
        if self.logger:
            minutes = int(block["seconds"] // 60)
            self.logger.warning(
                f"""{hilight("[Search]", "fail")} Mercado Libre {reason} instead of serving the """
                f"""page. Leaving it alone for {minutes} minutes. {self._sign_in_advice()}""",
                extra=aimm_event(
                    "marketplace_blocked",
                    marketplace=self.name,
                    reason=reason,
                    seconds=block["seconds"],
                    strikes=block["strikes"],
                ),
            )

    def open_page(self: "MercadoLibreMarketplace", url: str) -> None:
        """Navigate, then check we were actually served what we asked for.

        Every Mercado Libre navigation goes through here.  A page that came back
        normally also clears any cooldown: the site has evidently forgiven us.
        """
        if self.page is None:
            self.page = self.create_page()
        self.goto_url(url)
        # The URL we asked for goes with the question: landing on the page that
        # was requested is never a redirect, whatever its host looks like.
        reason = self.wall_reason(asked_for=url)
        if reason:
            self._hit_wall(reason)
            raise MercadoLibreWall(reason, url)
        control.clear_marketplace_block(self.name)

    def login(self: "MercadoLibreMarketplace") -> bool:
        """Whether Mercado Libre may be read right now.

        Unlike Facebook's, this signs nothing in by itself: Mercado Libre serves
        its listings to anonymous visitors, and an automated sign-in is exactly
        the thing that gets an account challenged.  A missing session is
        therefore never a reason not to search -- the monitor always tries, and
        says what it ran into if the site refuses.  The one thing that stops it
        is a cooldown already in force, where the answer is known in advance.
        """
        if control.marketplace_blocked(self.name):
            block = control.marketplace_block(self.name) or {}
            if self.logger:
                self.logger.info(
                    f"""{hilight("[Search]", "info")} Skipping Mercado Libre for another """
                    f"""{int(block.get("seconds_left", 0) // 60)} minutes: it {block.get("reason")}.""",
                )
            return False

        if not self.has_saved_session() and self.logger:
            # Not a refusal: anonymous browsing works, and saying so once per
            # pass is more use than a switch that turns the platform off.
            self.logger.info(
                f"""{hilight("[Search]", "info")} Searching Mercado Libre without a signed-in """
                f"""session. It answers anonymous visitors, but may start asking for an """
                f"""account after enough pages. {self._sign_in_advice()}""",
                extra=aimm_event("session_missing", marketplace=self.name),
            )
        return True

    def login_interactively(self: "MercadoLibreMarketplace", timeout: int = 3600) -> bool:
        """Open the site and let the user sign in, then keep the session.

        Nothing is typed in and nothing is on a clock the user can lose: Mercado
        Libre's sign-in involves a code by mail or phone often enough that any
        deadline is the wrong one.

        The waiting is deliberately *passive*.  Re-loading a page every few
        seconds to ask "are you done yet?" would yank the browser out from under
        someone in the middle of typing a password, so this only watches the URL
        the user is on.  Only once they have visited the sign-in gateway and
        come back off it does it spend one navigation confirming, and if that
        confirmation says no it waits again rather than nagging.

        Mercado Libre is also known to accept a sign-in inside an automated
        browser and quietly return the visitor to the front page without a
        session.  When that happens no amount of waiting helps, and the way
        through is to import a session from a normal browser instead --
        ``session.import_session``, which the web UI exposes.
        """
        assert self.context is not None
        self.page = self.create_page()

        if self.is_signed_in():
            if self.logger:
                self.logger.info(
                    f"""{hilight("[Login]", "succ")} Mercado Libre session already in place."""
                )
            control.clear_marketplace_block(self.name)
            return self.save_session()

        home = self.home_url()
        self.goto_url(home)
        if self.logger:
            self.logger.info(
                f"""{hilight("[Login]", "info")} Sign in to Mercado Libre in the browser """
                f"""window ({home}). Press Ctrl-C here to give up. If the sign-in appears to """
                """work but drops you back on the front page, import the session from your """
                """own browser in the web UI instead — Mercado Libre does refuse some """
                """sign-ins made inside an automated browser.""",
                extra=aimm_event("credentials_wait", status="interactive", marketplace=self.name),
            )

        deadline = time.time() + timeout
        visited_login = False
        next_confirm = 0.0
        while time.time() < deadline:
            time.sleep(3)
            try:
                url = fold_text(self.page.url or "")
            except KeyboardInterrupt:
                raise
            except Exception:
                continue

            on_login = any(marker in url for marker in WALL_URL_MARKERS)
            if on_login:
                visited_login = True
                continue
            # Nothing to confirm until they have actually been through the
            # sign-in, and never more than once every half minute.
            if not visited_login or time.time() < next_confirm:
                continue

            next_confirm = time.time() + 30
            if self.is_signed_in():
                control.clear_marketplace_block(self.name)
                if self.logger:
                    self.logger.info(
                        f"""{hilight("[Login]", "succ")} Mercado Libre session saved.""",
                        extra=aimm_event(
                            "credentials_wait", status="found", marketplace=self.name
                        ),
                    )
                return self.save_session()
            # Not signed in after all: put them back where they can try again.
            try:
                self.goto_url(home)
            except KeyboardInterrupt:
                raise
            except Exception:
                pass

        if self.logger:
            self.logger.error(
                f"""{hilight("[Login]", "fail")} Gave up waiting for the Mercado Libre sign-in. """
                """If it kept returning you to the front page, import the session from your own """
                """browser in the web UI (Ajustes -> Plataformas -> Mercado Libre).""",
                extra=aimm_event("credentials_wait", status="failed", marketplace=self.name),
            )
        return False

    # ------------------------------------------------------------------ #
    # Search URL
    # ------------------------------------------------------------------ #

    def site_of(self: "MercadoLibreMarketplace", item_config: ItemConfig) -> str:
        """The site this item is searched on.

        The item's own choice first, then the marketplace section's default,
        then Chile -- the site the parsing was verified against.
        """
        return str(self._option(item_config, "site") or self.DEFAULT_SITE).upper()

    def _option(self: "MercadoLibreMarketplace", item_config: ItemConfig, key: str) -> Any:
        """An option from the item, falling back to the marketplace section."""
        value = getattr(item_config, key, None)
        if value is None:
            value = getattr(self.config, key, None)
        return value

    def search_url(
        self: "MercadoLibreMarketplace",
        phrase: str,
        item_config: ItemConfig,
        offset: int = 0,
    ) -> str:
        """Build one search URL.

        The site expresses filters as ``_Key_Value`` suffixes on the search
        path, in this order, and drops the whole URL back to an unfiltered
        search if it cannot parse it -- so the pieces below are only the ones
        that were verified to come back with their filter applied.
        """
        site = self.site_of(item_config)
        host = SEARCH_HOSTS.get(site, SEARCH_HOSTS[self.DEFAULT_SITE])
        url = f"{host}/{_slugify(phrase)}"

        min_price = self._option(item_config, "min_price")
        max_price = self._option(item_config, "max_price")
        if min_price or max_price:
            currency = SITE_CURRENCIES.get(site, "")
            low = str(min_price or 0).split(" ")[0]
            high = str(max_price or 0).split(" ")[0]
            url += f"_PriceRange_{low}{currency}-{high}{currency}"

        conditions = self._option(item_config, "condition") or []
        # One condition can be expressed in the URL; several cannot, and are
        # applied to the parsed cards instead (their condition is on the card).
        if len(conditions) == 1:
            url += f"_ITEM*CONDITION_{CONDITION_IDS[conditions[0]]}"

        if self._option(item_config, "free_shipping"):
            url += "_CostoEnvio_Gratis"

        origin = self._option(item_config, "shipping_origin")
        if origin:
            url += f"_SHIPPING*ORIGIN_{SHIPPING_ORIGINS[origin]}"

        if offset:
            url += f"_Desde_{offset + 1}"

        return url + "_NoIndex_True"

    # ------------------------------------------------------------------ #
    # Searching
    # ------------------------------------------------------------------ #

    def search(
        self: "MercadoLibreMarketplace", item_config: ItemConfig
    ) -> Generator[Listing, None, None]:
        self._warn_about_ignored_options(item_config)

        # Same shape as the Facebook scraper: when the site will not serve us,
        # yield nothing and let the next cycle try, rather than recording an
        # empty market as if it were the truth.
        if not self.login():
            return

        if self.page is None:
            self.page = self.create_page()

        max_pages = int(self._option(item_config, "max_pages") or DEFAULT_MAX_PAGES)
        found: Dict[str, bool] = {}

        for phrase in item_config.search_phrases:
            for page_number in range(max_pages):
                url = self.search_url(phrase, item_config, offset=page_number * PAGE_SIZE)
                try:
                    self.open_page(url)
                except MercadoLibreWall:
                    # Already logged and already on a cooldown; stop the whole
                    # search rather than working through the remaining phrases,
                    # every one of which would be refused too.
                    return
                counter.increment(CounterItem.SEARCH_PERFORMED, item_config.name)

                listings = MercadoLibreSearchResultPage(self.page, self.logger).get_listings(
                    item_name=item_config.name
                )
                if self.logger:
                    if listings:
                        self.logger.debug(
                            f"""{hilight("[Search]", "succ")} {hilight(str(len(listings)))} """
                            f"""result(s) on Mercado Libre for {phrase}"""
                        )
                    else:
                        self.logger.warning(
                            f"""{hilight("[Search]", "fail")} No results on Mercado Libre for """
                            f"""{phrase}.{self._blocked_hint()}"""
                        )

                for listing in listings:
                    key = listing.post_url.split("?")[0]
                    if key in found:
                        continue
                    found[key] = True
                    if self.keyboard_monitor is not None and self.keyboard_monitor.is_paused():
                        return
                    counter.increment(CounterItem.LISTING_EXAMINED, item_config.name)

                    # Only listings nobody has recorded yet: see
                    # `observations.is_known`.  A stored listing's price and
                    # stock are the review's business, read off its own page,
                    # and re-reading it here is the same page load done twice.
                    if is_known(listing.marketplace, listing.id):
                        if self.logger:
                            self.logger.debug(
                                f"""{hilight("[Skip]", "info")} {listing.title} is already """
                                """stored; the review keeps it up to date."""
                            )
                        continue

                    if not self.check_listing(listing, item_config, description_available=False):
                        counter.increment(CounterItem.EXCLUDED_LISTING, item_config.name)
                        continue

                    try:
                        details, from_cache = self.get_listing_details(
                            listing.post_url,
                            item_config,
                            price=listing.price,
                            title=listing.title,
                            fallback=listing,
                        )
                    except KeyboardInterrupt:
                        raise
                    except MercadoLibreWall:
                        # The site has stopped serving listing pages.  Keep what
                        # the card already told us -- it is a real sighting --
                        # and then stop, because the next page would be refused.
                        matched = self.check_listing(listing, item_config)
                        record_observation(
                            listing, matched=matched, item_name=item_config.name
                        )
                        if matched:
                            yield listing
                        return
                    except Exception as error:
                        if self.logger:
                            self.logger.debug(
                                f"Failed to read the Mercado Libre listing {key}: {error}"
                            )
                        # The card alone is still a usable listing, minus the
                        # description; losing it entirely would be worse.
                        details, from_cache = listing, True

                    if not from_cache:
                        # Space out item-page requests, as the Facebook
                        # scraper does between listings.
                        time.sleep(2)

                    matched = self.check_listing(details, item_config)
                    # Log the sighting whichever way the filters went: the
                    # dashboard reports on the whole market, not only the hits,
                    # and this is the only place the listing's own data reaches
                    # the observation log.
                    record_observation(details, matched=matched, item_name=item_config.name)
                    if matched:
                        yield details
                    else:
                        counter.increment(CounterItem.EXCLUDED_LISTING, item_config.name)

                if len(listings) < PAGE_SIZE:
                    # A short page is the last page.
                    break

    def _blocked_hint(self: "MercadoLibreMarketplace") -> str:
        """Say the likely reason for an empty page, when there is a known one.

        An outright refusal is caught before this by :meth:`open_page`; what is
        left here is the quieter case of a page that loaded but held nothing,
        which for this marketplace usually means a headless browser.
        """
        reason = self.wall_reason()
        # No wall and no results is simply an empty search.  Saying something
        # speculative here would be worse than saying nothing.
        return f" Mercado Libre {reason}. {self._sign_in_advice()}" if reason else ""

    def _warn_about_ignored_options(
        self: "MercadoLibreMarketplace", item_config: ItemConfig
    ) -> None:
        """Say once which location options do not apply here.

        Mercado Libre's search has no location facet: results are site-wide and
        the questions its filters answer are about shipping.  Silently honouring
        a city or radius would mean inventing a filter the platform does not
        have; silently dropping it would look like a bug.
        """
        if self._warned_about_location or self.logger is None:
            return
        ignored = [
            option
            for option in IGNORED_LOCATION_OPTIONS
            if getattr(item_config, option, None) or getattr(self.config, option, None)
        ]
        if ignored:
            self.logger.info(
                f"""{hilight("[Search]", "info")} Mercado Libre has no location filter, so """
                f"""{', '.join(ignored)} do not apply to it. Use free_shipping or """
                """shipping_origin instead."""
            )
        self._warned_about_location = True

    def get_listing_details(
        self: "MercadoLibreMarketplace",
        post_url: str,
        item_config: ItemConfig,
        price: str | None = None,
        title: str | None = None,
        fallback: Listing | None = None,
    ) -> Tuple[Listing, bool]:
        """Read one listing page: description, seller and condition live there.

        Same signature as the Facebook marketplace's, so the monitor can hand a
        URL to whichever marketplace recognises it.  ``fallback`` is the card the
        listing was found on, when there was one: the card carries things the
        page does not, such as the struck-through original price.
        """
        post_url = post_url.split("?")[0]
        cached = Listing.from_cache(post_url)
        if (
            cached is not None
            and (price is None or cached.price == price)
            and (title is None or cached.title == title)
        ):
            return cached, True

        self.open_page(post_url)
        counter.increment(CounterItem.LISTING_QUERY, item_config.name)
        details = MercadoLibreItemPage(self.page, self.logger).parse(
            post_url, item_config.name, fallback=fallback
        )
        if details is None:
            raise ValueError(f"Failed to read the Mercado Libre listing {post_url}.")
        details.to_cache(post_url)
        return details, False

    def recheck_listing(
        self: "MercadoLibreMarketplace", post_url: str, item_config: ItemConfig
    ) -> Tuple[ListingStatus, Listing | None]:
        """Re-read a stored listing to pick up a new price.

        Never reports SOLD or GONE.  Mercado Libre leaves a finished listing up,
        relabelled ("pausada", "finalizada") rather than removed, and this
        monitor has no tested reading of those states -- so an unreadable or
        unexpected page is reported as undecided and nothing is deleted.  The
        automatic removal of sold and dead listings is Facebook's alone.
        """
        if not self.login():
            return ListingStatus.UNKNOWN, None
        try:
            self.open_page(post_url)
        except MercadoLibreWall:
            # A wall is a fact about us, not about the listing.  Undecided, so
            # nothing is deleted; the cooldown keeps the refresher off the site.
            return ListingStatus.UNKNOWN, None
        counter.increment(CounterItem.LISTING_RECHECKED, item_config.name)
        assert self.page is not None
        details = MercadoLibreItemPage(self.page, self.logger).parse(post_url, item_config.name)
        if details is None:
            return ListingStatus.UNKNOWN, None
        details.to_cache(post_url)
        return ListingStatus.ACTIVE, details

    def check_listing(
        self: "MercadoLibreMarketplace",
        item: Listing,
        item_config: ItemConfig,
        description_available: bool = True,
    ) -> bool:
        """The filters that are applied to results rather than to the URL."""
        # Before everything else, including the price bounds baked into the
        # search URL: an excluded pattern says this number is not a price.
        if self.junk_price(item, item_config):
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

        # Several conditions cannot be expressed in one search URL, so they are
        # matched here against the condition the card carried.
        conditions = self._option(item_config, "condition") or []
        if len(conditions) > 1 and item.condition:
            folded = CONDITION_LABELS.get(item.condition.strip().lower())
            if folded is not None and folded not in conditions:
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

        return True


class MercadoLibreSearchResultPage:
    """One page of search results."""

    #: Cards the site injects as ads link through a click tracker rather than to
    #: the listing; the same items also show up organically.
    AD_HOST = "click1."

    def __init__(
        self: "MercadoLibreSearchResultPage", page: Page, logger: Logger | None = None
    ) -> None:
        self.page = page
        self.logger = logger

    def get_listings(
        self: "MercadoLibreSearchResultPage", item_name: str
    ) -> List[Listing]:
        try:
            self.page.wait_for_selector("li.ui-search-layout__item", timeout=15000)
        except KeyboardInterrupt:
            raise
        except Exception:
            return []

        raw = self.page.evaluate(CARD_SCRIPT)
        listings: List[Listing] = []
        for entry in raw:
            url = (entry.get("url") or "").split("?")[0]
            if not url or self.AD_HOST in url:
                continue
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            listings.append(
                Listing(
                    marketplace="mercadolibre",
                    name=item_name,
                    id=listing_id_from_url(url),
                    title=title,
                    image=entry.get("image") or "",
                    # Current price first, struck-through original second, the
                    # same shape extract_price produces for Facebook.
                    price=extract_price(
                        " | ".join(
                            x for x in (entry.get("price"), entry.get("previous")) if x
                        )
                    ),
                    post_url=url,
                    location=(entry.get("location") or "").strip(),
                    seller=(entry.get("seller") or "").strip(),
                    condition=(entry.get("condition") or "").strip(),
                    description="",
                )
            )
        return listings


#: Read in the page rather than through locators: one round trip for fifty
#: cards instead of ten per card.
CARD_SCRIPT = """
() => [...document.querySelectorAll('li.ui-search-layout__item')].map((card) => {
  const text = (selector) => card.querySelector(selector)?.textContent?.trim() || null;
  const picture = card.querySelector('.poly-component__picture');
  return {
    url: card.querySelector('a.poly-component__title')?.href
      || card.querySelector('a')?.href || null,
    title: text('.poly-component__title'),
    price: text('.poly-price__current .andes-money-amount'),
    previous: text('s.andes-money-amount'),
    condition: text('.poly-component__item-condition'),
    seller: text('.poly-component__seller'),
    location: text('.poly-component__location'),
    image: picture?.getAttribute('src') || picture?.getAttribute('data-src') || null,
  };
})
"""

#: The item page carries what a card cannot: the description, the seller's name
#: and an explicit condition ("Usado | 2 vendidos").
ITEM_SCRIPT = r"""
() => {
  const text = (selector) => document.querySelector(selector)?.textContent?.trim() || null;
  return {
    title: text('h1.ui-pdp-title'),
    price: text('.ui-pdp-price__second-line .andes-money-amount .andes-money-amount__fraction'),
    currency: text('.ui-pdp-price__second-line .andes-money-amount__currency-symbol'),
    subtitle: text('.ui-pdp-subtitle'),
    description: text('.ui-pdp-description__content'),
    seller: (text('.ui-seller-data-header__title') || text('.ui-pdp-seller__link-trigger-button') || '')
      .replace(/^Vendido por\s*/i, '').trim() || null,
    image: document.querySelector('figure.ui-pdp-gallery__figure img')?.getAttribute('src') || null,
  };
}
"""


class MercadoLibreItemPage:
    """One listing page."""

    def __init__(self: "MercadoLibreItemPage", page: Page, logger: Logger | None = None) -> None:
        self.page = page
        self.logger = logger

    def parse(
        self: "MercadoLibreItemPage",
        post_url: str,
        item_name: str,
        fallback: Listing | None = None,
    ) -> Listing | None:
        """The listing as its own page describes it.

        ``fallback`` is the search card it came from, when there was one: the
        card knows things the page does not (the struck-through original price)
        and the page knows things the card does not (the description).  What
        neither has stays empty -- Mercado Libre shows no seller location in
        most categories, and no plausible-looking substitute is invented.
        """
        try:
            self.page.wait_for_selector("h1.ui-pdp-title", timeout=15000)
        except KeyboardInterrupt:
            raise
        except Exception:
            return fallback

        data = self.page.evaluate(ITEM_SCRIPT)
        if not data.get("title") and fallback is None:
            return None

        # "Usado  |  2 vendidos" -- the condition is the part before the pipe.
        subtitle = (data.get("subtitle") or "").split("|")[0].strip()

        price = fallback.price if fallback else ""
        if data.get("price") and (not price or "|" not in price):
            # The page shows only the current price, so a card that carried the
            # discounted pair keeps its own.
            price = extract_price(f"{data.get('currency') or ''}{data['price']}")

        return Listing(
            marketplace=MercadoLibreMarketplace.name,
            name=item_name,
            id=listing_id_from_url(post_url),
            title=(data.get("title") or (fallback.title if fallback else "")).strip(),
            image=data.get("image") or (fallback.image if fallback else ""),
            price=price,
            post_url=post_url,
            location=fallback.location if fallback else "",
            seller=(data.get("seller") or (fallback.seller if fallback else "") or "").strip(),
            condition=subtitle or (fallback.condition if fallback else ""),
            description=(data.get("description") or "").strip(),
        )
