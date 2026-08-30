import sys
from dataclasses import dataclass, field, fields
from enum import Enum
from itertools import chain
from logging import Logger
from pathlib import Path
from typing import Any, Dict, Generic, List, Tuple

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from .ai import (
    AnthropicBackend,
    DeepSeekBackend,
    GeminiBackend,
    OllamaBackend,
    OpenAIBackend,
    TAIConfig,
)
from .facebook import FacebookMarketplace
from .lider import LiderMarketplace
from .marketplace import TItemConfig, TMarketplaceConfig
from .mercadolibre import MercadoLibreMarketplace
from .sodimac import SodimacMarketplace
from .tracking import PLATFORM as TRACKED_PLATFORM, TrackedMarketplace
from .notification import NotificationConfig
from .price_pattern_set import PricePatternsConfig
from .region import RegionConfig
from .user import User, UserConfig
from .utils import MonitorConfig, Translator, hilight, merge_dicts

supported_marketplaces = {
    "facebook": FacebookMarketplace,
    "mercadolibre": MercadoLibreMarketplace,
    "lider": LiderMarketplace,
    "sodimac": SodimacMarketplace,
}

#: Every implementation there is, including the one that is not a marketplace.
#:
#: The split matters in exactly one way, and it is the reason for it: everything
#: in ``supported_marketplaces`` is *created automatically*, listed as a platform
#: in the interface and offered a browser session.  ``tracked`` must not be any
#: of those -- it is one product page watched on purpose, there is no session to
#: import for it and nothing to search on it -- but it still has to be findable
#: by name when a listing says it came from there.  So the class lookups use this
#: mapping and the "which platforms exist" question uses the one above.
all_marketplaces = {
    **supported_marketplaces,
    TRACKED_PLATFORM: TrackedMarketplace,
}


def market_type_of(marketplace_name: str, marketplace_config: Dict[str, Any]) -> str:
    """Which marketplace implementation a ``[marketplace.*]`` section asks for.

    Explicit ``market_type`` wins.  Otherwise the section name is used when it
    names a supported marketplace, so ``[marketplace.mercadolibre]`` needs no
    boilerplate, and anything else (``[marketplace.houston]``) keeps falling
    back to Facebook as it always did.
    """
    declared = marketplace_config.get("market_type")
    if declared:
        return str(declared)
    if marketplace_name in supported_marketplaces:
        return marketplace_name
    return "facebook"


#: Radius used for a region city that names none of its own.
#:
#: Mirrors ``RegionConfig.handle_radius``, which fills the column in for a
#: region that only lists cities; this is the same number for the rare case of
#: a region whose radius column is shorter than its city list.
DEFAULT_REGION_RADIUS = 500

#: Marketplace options that used to exist, and what happens instead now.
#:
#: Dropped with a warning rather than rejected: a file that was valid before an
#: upgrade must keep loading, and a monitor that refuses to start over a setting
#: it decided to stop having is the worst of both.
RETIRED_MARKETPLACE_KEYS: Dict[str, str] = {
    "require_login": (
        "Mercado Libre is now always searched, with or without a signed-in session; "
        "if it starts asking for an account, the monitor waits it out and says so."
    ),
}


supported_ai_backends = {
    "deepseek": DeepSeekBackend,
    "gemini": GeminiBackend,
    "openai": OpenAIBackend,
    "anthropic": AnthropicBackend,
    "ollama": OllamaBackend,
}


def split_options(
    options: Dict[str, Any], factory: Any, item_name: str
) -> Tuple[Dict[str, Any], List[str]]:
    """Split an item's options into the ones this marketplace understands.

    A filter that exists on another platform but not on this one is dropped
    rather than applied approximately -- a Facebook radius means nothing to
    Mercado Libre, which has no location filter at all.  An option no
    marketplace knows is still an error, so a typo does not pass silently.
    """
    accepted_names = {f.name for f in fields(factory)}
    known_elsewhere = set()
    for other in all_marketplaces.values():
        known_elsewhere |= {f.name for f in fields(other.item_config_class())}

    accepted: Dict[str, Any] = {}
    ignored: List[str] = []
    for key, value in options.items():
        if key in accepted_names:
            accepted[key] = value
        elif key in known_elsewhere:
            ignored.append(key)
        else:
            raise ValueError(
                f"Item {hilight(item_name)} has an unknown option {hilight(key)}."
            )
    return accepted, ignored


class ConfigItem(Enum):
    MONITOR = "monitor"
    MARKETPLACE = "marketplace"
    USER = "user"
    ITEM = "item"
    AI = "ai"
    REGION = "region"
    NOTIFICATION = "notification"
    TRANSLATION = "translation"
    #: A named list of excluded price patterns, reusable across searches.
    #: A sibling of `region`: written once, referred to by name, and
    #: resolved into the real values before anything runs.
    PRICE_PATTERNS = "price_patterns"
    #: One product page followed by address.  A sibling of `item` rather than a
    #: kind of it: a search has phrases and filters, a tracker has a URL.
    TRACK = "track"


@dataclass
class Config(Generic[TAIConfig, TItemConfig, TMarketplaceConfig]):
    monitor: MonitorConfig = field(init=False)
    ai: Dict[str, TAIConfig] = field(init=False)
    user: Dict[str, UserConfig] = field(init=False)
    notification: Dict[str, NotificationConfig] = field(init=False)
    marketplace: Dict[str, TMarketplaceConfig] = field(init=False)
    #: One configuration per (marketplace, item): the same product searched
    #: on Facebook and on Mercado Libre needs one config each, because the
    #: platforms accept different options.
    items: Dict[Tuple[str, str], TItemConfig] = field(init=False)
    translator: Dict[str, Translator] = field(init=False)
    region: Dict[str, RegionConfig] = field(init=False)
    price_patterns: Dict[str, PricePatternsConfig] = field(init=False)

    def __init__(self: "Config", config_files: List[Path], logger: Logger | None = None) -> None:
        self.logger = logger
        configs = []
        system_config = Path(__file__).parent / "config.toml"

        for config_file in [system_config, *config_files]:
            try:
                if logger:
                    logger.debug(
                        f"""{hilight("[Monitor]", "succ")} config file {hilight(str(config_file))}"""
                    )
                with open(config_file, "rb") as f:
                    configs.append(tomllib.load(f))
            except tomllib.TOMLDecodeError as e:
                raise ValueError(f"Error parsing config file {config_file}: {e}") from e
        #
        # merge the list of configs into a single dictionary, including dictionaries in the values
        config = merge_dicts(configs)

        self.validate_sections(config)
        self.get_translator_config(config)
        self.get_monitor_config(config)
        self.get_ai_config(config)
        self.get_notification_config(config)
        self.get_marketplace_config(config)
        self.get_user_config(config)
        self.get_region_config(config)
        self.get_price_patterns_config(config)
        self.get_item_config(config)
        self.validate_users()
        self.validate_ais()
        self.expand_notifications(logger)
        self.expand_regions()
        self.expand_price_patterns()
        self.validate_items()

    def get_translator_config(self: "Config", config: Dict[str, Any]) -> None:
        if not isinstance(config.get("translation", {}), dict):
            raise ValueError("translation section must be a dictionary.")

        self.translator = {}
        for key, value in config.get("translation", {}).items():
            if "locale" not in value:
                raise ValueError(f"Translation section {hilight(key)} must contain a locale.")
            self.translator[key] = Translator(
                locale=value["locale"],
                dictionary={k: v for k, v in value.items() if k != "locale"},
            )

    def get_monitor_config(self: "Config", config: Dict[str, Any]) -> None:
        self.monitor = MonitorConfig(name="monitor", **config.get("monitor", {}))

    def get_ai_config(self: "Config", config: Dict[str, Any]) -> None:
        # convert ai config to AIConfig objects
        if not isinstance(config.get("ai", {}), dict):
            raise ValueError("ai section must be a dictionary.")

        self.ai = {}
        for key, value in config.get("ai", {}).items():
            try:
                backend_class = supported_ai_backends[value.get("provider", key).lower()]
            except KeyboardInterrupt:
                raise
            except Exception as e:
                raise ValueError(
                    f"Config file contains an unsupported AI backend {key} in the ai section."
                ) from e
            self.ai[key] = backend_class.get_config(name=key, **value)

    def get_notification_config(self: "Config", config: Dict[str, Any]) -> None:
        if not isinstance(config.get("notification", {}), dict):
            raise ValueError("notification section must be a dictionary.")

        self.notification: Dict[str, NotificationConfig] = {}
        for key, value in config.get("notification", {}).items():
            cfg = NotificationConfig.get_config(name=key, **value)
            if cfg is None:
                raise ValueError(
                    f"Unable to determine notification type for notification section {key}"
                )
            else:
                self.notification[key] = cfg

    def get_marketplace_config(self: "Config", config: Dict[str, Any]) -> None:
        """Build one config per platform the monitor knows how to search.

        Every supported marketplace exists, always.  A platform is a capability
        of this program, not something the user installs: there is nothing
        sensible to say about "Mercado Libre has not been added yet", and making
        people add it before a search could offer it only ever produced a
        config that looked complete and searched nowhere.

        A ``[marketplace.<name>]`` section is therefore optional, and carries
        only what *is* the user's business about a platform -- how to sign in to
        it.  A section named after something other than a supported marketplace
        (``[marketplace.houston]``) still creates one of its own, so a
        configuration written before this keeps working.
        """
        # check for required fields in each marketplace
        self.marketplace = {}
        sections: Dict[str, Dict[str, Any]] = dict(config.get("marketplace", {}) or {})
        # Built-ins first, so they keep a stable order regardless of what the
        # file happens to mention; a section for one of them is merged in below
        # rather than added a second time.
        declared = {
            name: sections.pop(name, {}) or {} for name in supported_marketplaces
        }
        declared.update(sections)
        for marketplace_name, marketplace_config in declared.items():
            marketplace_config = self._drop_retired_keys(marketplace_name, marketplace_config)
            market_type = market_type_of(marketplace_name, marketplace_config)
            if market_type not in all_marketplaces:
                raise ValueError(
                    f"Marketplace {hilight(market_type)} is not supported. Supported marketplaces are: {supported_marketplaces.keys()}"
                )
            marketplace_class = all_marketplaces[market_type]
            self.marketplace[marketplace_name] = marketplace_class.get_config(
                name=marketplace_name,
                monitor_config=self.monitor,
                # Pass the resolved type, so a section named after its
                # marketplace does not have to repeat it.
                **{**marketplace_config, "market_type": market_type},
            )
            self.validate_language(config, self.marketplace[marketplace_name].language)

    def _drop_retired_keys(
        self: "Config", marketplace_name: str, section: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Ignore a marketplace option this version no longer has.

        A retired option is not a typo, and refusing to load the file over one
        would strand a configuration that was valid yesterday.  It is dropped
        and said out loud instead, so the behaviour change is visible.
        """
        retired = {key: RETIRED_MARKETPLACE_KEYS[key] for key in section if key in RETIRED_MARKETPLACE_KEYS}
        if not retired:
            return section
        for key, why in retired.items():
            if self.logger:
                self.logger.warning(
                    f"""{hilight("[Config]", "fail")} [marketplace.{marketplace_name}] """
                    f"""{hilight(key)} no longer exists and is ignored. {why}"""
                )
        return {key: value for key, value in section.items() if key not in retired}

    @staticmethod
    def validate_language(config: Dict[str, Any], language: str | None) -> None:
        """Refuse a language the scrapers have no vocabulary for.

        The parsers find things on the page by their label, so the language is
        the difference between reading a listing and reading half of one.  No
        exact match is required: ``es_LA`` is served by the ``es`` table.
        """
        if not language:
            return
        base = language.split("_")[0]
        # English needs no table: it is the language the parsers are written in,
        # so `en_US` is always available even though nothing translates to it.
        if base.lower() == "en":
            return
        available = config.get(ConfigItem.TRANSLATION.value, {}) or {}
        if base not in {x.split("_")[0] for x in available}:
            raise ValueError(f"Translation for language {language} is not supported.")

    def get_user_config(self: "Config", config: Dict[str, Any]) -> None:
        # Zero users is a legitimate state: a fresh install has none, and a
        # monitor with nobody to notify still searches, stores and shows what it
        # finds in the web UI.  So the section is optional, like `item`.
        self.user: Dict[str, UserConfig] = {}
        for user_name, user_config in (config.get("user", {}) or {}).items():
            self.user[user_name] = User.get_config(name=user_name, **user_config)

    def get_region_config(self: "Config", config: Dict[str, Any]) -> None:
        # check for required fields in each user
        self.region: Dict[str, RegionConfig] = {}
        for region_name, region_config in config.get("region", {}).items():
            self.region[region_name] = RegionConfig(name=region_name, **region_config)

    def get_price_patterns_config(self: "Config", config: Dict[str, Any]) -> None:
        """Build one config per ``[price_patterns.<name>]`` section.

        Zero of them is the normal state: a search may still write its patterns
        out itself, and nothing here replaces that.
        """
        self.price_patterns = {}
        sections = config.get(ConfigItem.PRICE_PATTERNS.value, {}) or {}
        if not isinstance(sections, dict):
            raise ValueError("price_patterns section must be a dictionary.")
        for name, section in sections.items():
            if not isinstance(section, dict):
                raise ValueError(
                    f"Price patterns {hilight(name)} must be a section of settings."
                )
            self.price_patterns[name] = PricePatternsConfig(name=name, **section)

    def get_item_config(self: "Config", config: Dict[str, Any]) -> None:
        """Build one item configuration per marketplace the item runs on.

        An item is a *product the user wants*, not a platform: unless it names a
        marketplace, it is searched on every configured one.  Since the
        platforms take different options, each pair gets its own config object,
        built from the item's shared options plus its
        ``[item.<name>.<marketplace>]`` overrides, minus whatever that platform
        has no filter for.
        """
        self.items = {}
        # Zero searches is a legitimate state -- a fresh install has none, and a
        # user may delete the last one -- so the section is optional.
        for item_name, item_config in config.get("item", {}).items():
            # A nested table is a per-marketplace override; everything else is
            # shared by all of them.
            overrides = {
                key: value for key, value in item_config.items() if isinstance(value, dict)
            }
            shared = {
                key: value for key, value in item_config.items() if not isinstance(value, dict)
            }

            # The platforms are the ones this program knows how to search, plus
            # any extra section the file declares -- never something the user
            # had to add first.
            for override_name in overrides:
                if override_name not in self.marketplace:
                    raise ValueError(
                        f"Item {hilight(item_name)} has a section for marketplace "
                        f"{hilight(override_name)}, which this monitor does not know. "
                        f"Known marketplaces: {', '.join(sorted(self.marketplace))}."
                    )

            # `marketplace` restricts an item to one platform.
            restricted_to = shared.pop("marketplace", None)
            if restricted_to is not None and restricted_to not in self.marketplace:
                raise ValueError(
                    f"Item {hilight(item_name)} specifies a marketplace that does not exist."
                )

            for marketplace_name, marketplace_config in self.marketplace.items():
                if restricted_to is not None and restricted_to != marketplace_name:
                    continue
                marketplace_class = supported_marketplaces[
                    (marketplace_config.market_type or "facebook").lower()
                ]
                if marketplace_class.opt_in and marketplace_name not in overrides:
                    # A shop runs only for a search that asked for it -- see
                    # `Marketplace.opt_in`.  Asking for it is having a
                    # `[item.<name>.<shop>]` section, which is exactly what the
                    # interface writes when the platform is switched on, so
                    # nothing in the file has to say `enabled = true` as well.
                    continue
                options = {**shared, **overrides.get(marketplace_name, {})}
                accepted, ignored = split_options(
                    options, marketplace_class.item_config_class(), item_name
                )
                if ignored and self.logger:
                    self.logger.debug(
                        f"""{hilight("[Config]", "info")} {marketplace_name} has no """
                        f"""{", ".join(sorted(ignored))} filter, so it is not applied to """
                        f"""{hilight(item_name)} there."""
                    )
                built = marketplace_class.get_item_config(
                    name=item_name, marketplace=marketplace_name, **accepted
                )
                # A search may name the language its platform is read in, so it
                # is checked against the available vocabularies here, the same
                # way the platform's own is.
                self.validate_language(config, getattr(built, "language", None))
                self.items[(marketplace_name, item_name)] = built

        self.get_tracker_config(config)

    def get_tracker_config(self: "Config", config: Dict[str, Any]) -> None:
        """Turn every ``[track.<name>]`` into an item on the tracked platform.

        A tracker is a listing with no search behind it, so it becomes an
        ordinary entry in ``self.items`` and everything downstream -- the
        schedule, the review, the notifications, the dashboard -- treats it as
        one.  That reuse is the whole design; see
        :mod:`ai_marketplace_monitor.tracking`.

        A name collision with a search is refused rather than resolved: both
        would be labelled with the same name in every log line, every counter
        and every group on the dashboard, and whichever one the user then looked
        at would be the wrong one.  A tracker's ``group`` is refused on the same
        grounds and for a sharper reason: the top-1 record is keyed by the name
        it is asked about, so a group sharing a name with a search would share
        that search's cheapest-so-far and announce each other's bargains.
        """
        sections = config.get("track", {}) or {}
        if not sections:
            return
        if TRACKED_PLATFORM not in self.marketplace:
            self.marketplace[TRACKED_PLATFORM] = TrackedMarketplace.get_config(
                name=TRACKED_PLATFORM, monitor_config=self.monitor,
                market_type=TRACKED_PLATFORM,
            )
        existing = {item_name for (_marketplace, item_name) in self.items}
        for name, options in sections.items():
            if name in existing:
                raise ValueError(
                    f"There is already a search called {hilight(name)}, so a tracker "
                    "cannot have that name too: they would be impossible to tell apart "
                    "in the log, the counters and the dashboard."
                )
            if not isinstance(options, dict):
                raise ValueError(f"Tracker {hilight(name)} must be a section of settings.")
            built = TrackedMarketplace.get_item_config(
                name=name, marketplace=TRACKED_PLATFORM, **options
            )
            self.items[(TRACKED_PLATFORM, name)] = built

        # After the loop, not inside it: a group is checked against every name
        # in the file, and half the trackers did not exist yet a moment ago.
        taken = {item_name for (_marketplace, item_name) in self.items}
        for name in sections:
            group = getattr(self.items[(TRACKED_PLATFORM, name)], "group", None)
            if group and group in taken:
                raise ValueError(
                    f"Tracker {hilight(name)} is in a group called {hilight(group)}, "
                    "and that is already the name of a search or of another tracker. "
                    "They share the record of the cheapest offer seen so far, so one "
                    "would announce the other's bargains."
                )

    @property
    def item(self: "Config") -> Dict[str, TItemConfig]:
        """One configuration per item, for callers that do not care which
        marketplace it belongs to (the first one wins)."""
        first: Dict[str, TItemConfig] = {}
        for (_marketplace_name, item_name), item_config in self.items.items():
            first.setdefault(item_name, item_config)
        return first

    def items_of(self: "Config", marketplace_name: str) -> Dict[str, TItemConfig]:
        """Every item configured to run on one marketplace."""
        return {
            item_name: item_config
            for (name, item_name), item_config in self.items.items()
            if name == marketplace_name
        }

    def describe(self: "Config") -> Dict[str, Any]:
        """A plain-data picture of this configuration, for the web UI.

        The point is not to re-render the file -- the interface can already read
        that -- but to show the configuration *as resolved*: every default
        applied, every inherited option folded in, one entry per (item,
        marketplace) pair exactly as the scraping loop will use it.  That is
        what makes "the scraper is running your old maximum price" a statement
        the interface can prove rather than guess.

        Secrets are left in place here and masked on the way out of the web
        server, which is the layer that knows what a browser may see.
        """

        def plain(value: Any) -> Any:
            if isinstance(value, (str, int, float, bool)) or value is None:
                return value
            if isinstance(value, (list, tuple, set)):
                return [plain(item) for item in value]
            if isinstance(value, dict):
                return {str(key): plain(item) for key, item in value.items()}
            return str(value)

        def fields_of(config: Any) -> Dict[str, Any]:
            return {
                f.name: plain(getattr(config, f.name, None))
                for f in fields(config)
                # A back-reference, not a setting: rendering it would repeat the
                # whole [monitor] section inside every marketplace.
                if f.name != "monitor_config"
            }

        searches: List[Dict[str, Any]] = []
        for (marketplace_name, item_name), item_config in sorted(
            self.items.items(), key=lambda pair: (pair[0][1], pair[0][0])
        ):
            # A tracker shares this dict with the searches (see
            # `get_tracker_config`) and is not one: `schedule_jobs` skips the
            # tracked platform outright, so publishing it here offered the
            # interface a "run now" and a "search this next" that the monitor
            # had no job to honour, and counted it in `search_count` besides.
            if marketplace_name == TRACKED_PLATFORM:
                continue
            marketplace_config = self.marketplace.get(marketplace_name)
            searches.append(
                {
                    "item": item_name,
                    "marketplace": marketplace_name,
                    "market_type": (
                        getattr(marketplace_config, "market_type", None)
                        if marketplace_config
                        else None
                    ),
                    # Disabled either way: an item switched off, or a platform
                    # switched off under it.  Both mean it will not be searched.
                    "enabled": getattr(item_config, "enabled", None) is not False
                    and getattr(marketplace_config, "enabled", None) is not False,
                    "search_phrases": plain(getattr(item_config, "search_phrases", []) or []),
                    "options": fields_of(item_config),
                }
            )

        return {
            "monitor": fields_of(self.monitor),
            "marketplaces": {
                name: fields_of(config) for name, config in sorted(self.marketplace.items())
            },
            "items": sorted({item_name for _marketplace, item_name in self.items}),
            "searches": searches,
            "users": sorted(self.user),
            "ai": sorted(self.ai),
            "notifications": sorted(self.notification),
            "regions": sorted(self.region),
            "price_patterns": sorted(self.price_patterns),
        }

    def validate_sections(self: "Config", config: Dict[str, Any]) -> None:
        # No section is required.  A monitor with no searches is idle rather
        # than broken (refusing such a file is what used to make the last search
        # undeletable), the platforms are built in rather than declared, and a
        # monitor with nobody to notify still searches and shows what it finds.
        # An empty file is a valid starting point.

        # check allowed keys in config
        for key in config:
            if key not in [x.value for x in ConfigItem]:
                raise ValueError(f"Config file contains an invalid section {key}.")

    def validate_users(self: "Config") -> None:
        """Check if notified users exists"""
        # if user is specified in other section, they must exist
        for config in chain(self.marketplace.values(), self.items.values()):
            for user in config.notify or []:
                if user not in self.user:
                    raise ValueError(
                        f"User {hilight(user)} specified in {hilight(config.name)} does not exist."
                    )

    def validate_ais(self: "Config") -> None:
        # if ai is specified in other section, they must exist
        for config in chain(self.marketplace.values(), self.items.values()):
            for ai in config.ai or []:
                if ai not in self.ai:
                    raise ValueError(
                        f"AI {hilight(config.ai)} specified in {hilight(config.name)} does not exist."
                    )

    def expand_notifications(self: "Config", logger: Logger | None = None) -> None:
        for config in self.user.values():
            for notification_name in (
                config.notify_with if config.notify_with is not None else self.notification.keys()
            ):
                notification_types = set()
                if notification_name not in self.notification:
                    raise ValueError(
                        f"User {hilight(config.name)} specifies an undefined notification method {notification_name}."
                    )
                notification_config = self.notification[notification_name]
                #
                if notification_config.enabled is False:
                    continue
                # add values of notification_config to user config
                if notification_config.__class__.__name__ in notification_types:
                    if logger:
                        logger.warning(
                            f"Ignore additional notification {hilight(notification_name)} with type {notification_config.__class__.__name__} for user {config.name}."
                        )
                    continue
                else:
                    notification_types.add(notification_config.__class__.__name__)

                for key, value in notification_config.__dict__.items():
                    # name is the notification name and should not override username
                    if key not in ("type", "name") and value is not None:
                        if getattr(config, key) is not None:
                            if logger:
                                logger.warning(
                                    f"Overriding {hilight(key)} for user {config.name} with value {value} from notification {hilight(notification_name)}."
                                )
                        setattr(config, key, value)

    def expand_regions(self: "Config") -> None:
        """Turn every named region into the cities the search will actually use.

        Regions are the user's own now -- nothing is shipped with the package --
        so the interesting failure is a search naming one that has been renamed
        or deleted since.  That is said in those words, and checked before the
        lookup rather than after it: reading the missing key first turned a
        deleted region into a bare ``KeyError`` with no name in it.
        """
        for config in chain(self.marketplace.values(), self.items.values()):
            if config.search_region is None:
                continue
            config.city_name = []
            config.search_city = []
            config.radius = []
            config.currency = []
            missing_currency = False

            for region in config.search_region:
                if region not in self.region:
                    raise ValueError(
                        f"Region {hilight(region)} used by {hilight(config.name)} does not "
                        "exist. Saved regions are defined in the web UI under Ajustes -> "
                        "Regiones guardadas; pick another one there, or add its cities to "
                        "the search directly."
                    )
                region_config: RegionConfig = self.region[region]
                if region_config.enabled is False:
                    continue
                # Driven by the cities, not by a zip of all four columns.  A
                # region only has to name its cities -- ``city_name`` and
                # ``radius`` are filled in by RegionConfig, and ``currency`` is
                # genuinely optional -- and zipping the four together made an
                # absent column silently truncate the whole region to nothing.
                # The search then had no city at all and the marketplace
                # rejected it with "No search_city or search_region is
                # specified", naming the one thing the user *had* set.
                cities = region_config.search_city or []
                names = region_config.city_name or []
                radii = region_config.radius or []
                currencies = region_config.currency or []
                for index, search_city in enumerate(cities):
                    if search_city in config.search_city:
                        continue
                    config.search_city.append(search_city)
                    config.city_name.append(
                        names[index] if index < len(names) else search_city.capitalize()
                    )
                    config.radius.append(
                        radii[index] if index < len(radii) else DEFAULT_REGION_RADIUS
                    )
                    if index < len(currencies):
                        config.currency.append(currencies[index])
                    else:
                        missing_currency = True
                        config.currency.append("")
            if missing_currency:
                # A currency is optional -- it only converts a price written as
                # "100 USD" -- and the four lists are read in parallel, so a
                # half-filled column would attach the wrong currency to a city.
                # Better none at all than one that is quietly wrong.
                config.currency = []

    def expand_price_patterns(self: "Config") -> None:
        """Fold every named pattern set into the list the filters actually read.

        The resolved list is written back onto ``excluded_price_patterns``, so
        nothing downstream has to know that sets exist: ``junk_price`` reads one
        flat list, ``describe()`` publishes the resolved one, and "what is the
        scraper really excluding?" has one answer instead of two half-answers.

        The set's patterns come first and the search's own after, deduplicated
        while keeping the first occurrence.  Order is only cosmetic -- the
        matcher tries every rule -- but the duplicate is not: a search that
        names a set *and* copies its patterns is the exact inconsistency the
        sets exist to remove, and leaving both in the resolved list would put
        the same rule twice in every log line and every readback.

        A set that is not there is refused by name, the way a missing region is,
        and for a sharper reason: an unknown *region* leaves a search with no
        city and the marketplace complains, whereas an unknown pattern set would
        leave a search that runs perfectly and silently stops excluding the
        placeholder prices -- which is only visible weeks later, in a group whose
        maximum is 999999.
        """
        for config in chain(self.marketplace.values(), self.items.values()):
            names = getattr(config, "excluded_price_pattern_sets", None)
            if not names:
                continue
            resolved: List[str] = []
            for name in names:
                if name not in self.price_patterns:
                    raise ValueError(
                        f"Price patterns {hilight(name)} used by {hilight(config.name)} do "
                        "not exist. Saved price patterns are defined in the web UI under "
                        "Ajustes -> Patrones de precios guardados; pick another one there, "
                        "or write the patterns into the search directly."
                    )
                pattern_set = self.price_patterns[name]
                if pattern_set.enabled is False:
                    continue
                resolved.extend(pattern_set.patterns)
            for pattern in config.excluded_price_patterns or []:
                resolved.append(pattern)
            # dict.fromkeys rather than a set: the order the user reads back is
            # the order they wrote, and a set would reshuffle it per run.
            config.excluded_price_patterns = list(dict.fromkeys(resolved))

    def validate_items(self: "Config") -> None:
        """Let each marketplace say whether it can run the items assigned to it.

        What an item needs depends on the platform -- Facebook cannot search
        without a city, Mercado Libre searches a whole site -- so the check
        belongs to the marketplace class rather than here.
        """
        for marketplace_name, marketplace_config in self.marketplace.items():
            if marketplace_config.enabled is False:
                continue
            marketplace_class = all_marketplaces[
                (marketplace_config.market_type or "facebook").lower()
            ]
            for item_config in self.items_of(marketplace_name).values():
                if item_config.enabled is False:
                    continue
                marketplace_class.validate_item_config(item_config, marketplace_config)
