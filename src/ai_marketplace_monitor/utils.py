import copy
import hashlib
import json
import os
import random
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, fields
from enum import Enum
from logging import Logger
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple, TypeVar

import parsedatetime  # type: ignore
import requests  # type: ignore
import rich
from diskcache import Cache  # type: ignore
from playwright.sync_api import ProxySettings
from pyparsing import (
    CharsNotIn,
    Keyword,
    ParserElement,
    ParseResults,
    Word,
    alphanums,
    infix_notation,
    opAssoc,
)
from requests.exceptions import RequestException, Timeout  # type: ignore
from rich.pretty import pretty_repr

try:
    from pynput import keyboard  # type: ignore

    pynput_enabled = os.environ.get("DISABLE_PYNPUT", "").lower() not in ("1", "y", "true", "yes")
except ImportError:
    # some platforms are not supported
    pynput_enabled = False

import io

import rich.pretty
from PIL import Image
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

# home directory for all settings and caches
amm_home = Path.home() / ".ai-marketplace-monitor"
amm_home.mkdir(parents=True, exist_ok=True)

cache = Cache(amm_home)


TConfigType = TypeVar("TConfigType", bound="BaseConfig")


class SleepStatus(Enum):
    NOT_DISRUPTED = 0
    BY_KEYBOARD = 1
    BY_FILE_CHANGE = 2
    BY_CONDITION = 3


def aimm_event(kind: str, **fields: Any) -> Dict[str, Any]:
    """Build a structured-event payload for a log call.

    Usage:
        logger.info(message, extra=aimm_event("ai_eval", score=5, ...))

    The web UI surfaces these structured fields in its filter dropdowns
    (kind / item / score) and in the expand-row detail pane.
    """
    return {"aimm": {"kind": kind, **fields}}


class CacheType(Enum):
    LISTING_DETAILS = "listing-details"
    AI_INQUIRY = "ai-inquiries"
    USER_NOTIFIED = "user-notifications"
    COUNTERS = "counters"
    LISTING_OBSERVATION = "listing-observations"
    LISTING_OBSERVATION_META = "listing-observations-meta"
    #: When each (item, marketplace) search last actually ran.  Persisted so a
    #: restart does not hand every search a fresh interval: a product searched
    #: two minutes before the monitor was stopped is not due again the instant
    #: it comes back, and "Iniciar" has to be able to say so.
    SEARCH_RUNS = "search-runs"


class CounterItem(Enum):
    SEARCH_PERFORMED = "Search performed"
    LISTING_EXAMINED = "Total listing examined"
    LISTING_QUERY = "New listing fetched"
    LISTING_RECHECKED = "Stored listing re-checked"
    LISTING_REMOVED = "Stored listing removed (sold or gone)"
    EXCLUDED_LISTING = "Listing excluded"
    NEW_VALIDATED_LISTING = "New validated listing"
    AI_QUERY = "Total AI Queries"
    NEW_AI_QUERY = "New AI Queries"
    FAILED_AI_QUERY = "Failed AI Queries)"
    NOTIFICATIONS_SENT = "Notifications sent"
    REMINDERS_SENT = "Reminders sent"


class Currency(Enum):
    USD = "USD"
    JPY = "JPY"
    BGN = "BGN"
    CYP = "CYP"
    EUR = "EUR"
    CZK = "CZK"
    DKK = "DKK"
    EEK = "EEK"
    GBP = "GBP"
    HUF = "HUF"
    LTL = "LTL"
    LVL = "LVL"
    MTL = "MTL"
    PLN = "PLN"
    ROL = "ROL"
    RON = "RON"
    SEK = "SEK"
    SIT = "SIT"
    SKK = "SKK"
    CHF = "CHF"
    ISK = "ISK"
    NOK = "NOK"
    HRK = "HRK"
    RUB = "RUB"
    TRL = "TRL"
    TRY = "TRY"
    AUD = "AUD"
    BRL = "BRL"
    CAD = "CAD"
    CNY = "CNY"
    HKD = "HKD"
    IDR = "IDR"
    ILS = "ILS"
    INR = "INR"
    KRW = "KRW"
    MXN = "MXN"
    MYR = "MYR"
    NZD = "NZD"
    PHP = "PHP"
    SGD = "SGD"
    THB = "THB"
    ZAR = "ZAR"
    ARS_unsupported = "ARS"


class KeyboardMonitor:
    confirm_character = "c"

    def __init__(self: "KeyboardMonitor") -> None:
        self._paused: bool = False
        self._listener: keyboard.Listener | None = None
        self._sleeping: bool = False
        self._confirmed: bool | None = None

    def start(self: "KeyboardMonitor") -> None:
        if pynput_enabled:
            self._listener = keyboard.Listener(on_press=self.handle_key_press)
            self._listener.start()  # start to listen on a separate thread

    def stop(self: "KeyboardMonitor") -> None:
        if self._listener:
            self._listener.stop()  # stop the listener

    def start_sleeping(self: "KeyboardMonitor") -> None:
        self._sleeping = True

    def confirm(self: "KeyboardMonitor", msg: str | None = None) -> bool:
        self._confirmed = False
        rich.print(
            msg
            or f"Press {hilight(self.confirm_character)} to enter interactive mode in 10 seconds: ",
            end="",
            flush=True,
        )
        try:
            count = 0
            while self._confirmed is False:
                time.sleep(0.1)
                if self._confirmed:
                    return True
                count += 1
                # wait a total of 10s
                if count > 100:
                    break
            return self._confirmed
        finally:
            # whether or not confirm is successful, reset paused and confirmed flag
            self._paused = False
            self._confirmed = None

    def is_sleeping(self: "KeyboardMonitor") -> bool:
        return self._sleeping

    def is_paused(self: "KeyboardMonitor") -> bool:
        return self._paused

    def is_confirmed(self: "KeyboardMonitor") -> bool:
        return self._confirmed is True

    def set_paused(self: "KeyboardMonitor", paused: bool = True) -> None:
        self._paused = paused

    if pynput_enabled:

        def handle_key_press(
            self: "KeyboardMonitor", key: keyboard.Key | keyboard.KeyCode | None
        ) -> None:
            # is sleeping, wake up
            if self._sleeping:
                if key == keyboard.Key.esc:
                    self._sleeping = False
                    return
            # if waiting for confirmation, set confirm
            if self._confirmed is False:
                if getattr(key, "char", "") == self.confirm_character:
                    self._confirmed = True
                    return
            # if being paused
            if self.is_paused():
                if key == keyboard.Key.esc:
                    print("Still searching ... will pause as soon as I am done.")
                    return
            if key == keyboard.Key.esc:
                print("Pausing search ...")
                self._paused = True


class Counter:
    def increment(self: "Counter", counter_key: CounterItem, item_name: str, by: int = 1) -> None:
        key = (CacheType.COUNTERS.value, counter_key.value, item_name)
        try:
            cache.incr(key, by, default=None)
        except KeyError:
            # if key does not exist, set it to by, and set tag
            cache.set(key, by, tag=CacheType.COUNTERS.value)

    def __str__(self: "Counter") -> str:
        """Return pretty form of all non-zero counters"""
        # this is super inefficient. Thankfully we are not calling this often.
        # See https://github.com/grantjenks/python-diskcache/issues/341
        # for details
        counters = {
            key: cache.get(key) for key in cache.iterkeys() if key[0] == CacheType.COUNTERS.value
        }
        item_names = {x[2] for x in counters.keys()}
        cnts = {}
        for item_name in item_names:
            # per-item statistics
            cnts[item_name] = {
                x.value: counters.get((CacheType.COUNTERS.value, x.value, item_name), 0)
                for x in CounterItem
                if counters.get((CacheType.COUNTERS.value, x.value, item_name), 0)
            }
        # total statistics
        cnts["Total"] = {
            x.value: sum(
                counters.get((CacheType.COUNTERS.value, x.value, item_name), 0)
                for item_name in item_names
            )
            for x in CounterItem
            if sum(
                counters.get((CacheType.COUNTERS.value, x.value, item_name), 0)
                for item_name in item_names
            )
        }
        return pretty_repr(cnts)


counter = Counter()


def hash_dict(obj: Dict[str, Any]) -> str:
    """Hash a dictionary to a string."""
    dict_string = json.dumps(obj).encode("utf-8")
    return hashlib.sha256(dict_string).hexdigest()


@dataclass
class BaseConfig:
    name: str
    enabled: bool | None = None

    def __post_init__(self: "BaseConfig") -> None:
        """Handle all methods that start with 'handle_' in the dataclass."""
        for f in fields(self):
            # test the type of field f, if it is a string or a list of string
            # try to expand the string with environment variables
            fvalue = getattr(self, f.name)
            if isinstance(fvalue, str):
                setattr(self, f.name, self._value_from_environ(fvalue))
            elif isinstance(fvalue, list) and all(isinstance(x, str) for x in fvalue):
                setattr(self, f.name, [self._value_from_environ(x) for x in fvalue])

            handle_method = getattr(self, f"handle_{f.name}", None)
            if handle_method:
                handle_method()

    def _value_from_environ(self: "BaseConfig", key: str) -> str | None:
        """Replace key with value from an environment variable if it has a format of ${KEY}.

        Returns None (with a warning) when the variable is not set, so
        that optional credentials degrade gracefully to anonymous mode.
        """
        if not isinstance(key, str) or not key.startswith("${") or not key.endswith("}"):
            return key
        var_name = key[2:-1]
        if var_name not in os.environ:
            import warnings

            warnings.warn(
                f"Environment variable {var_name} is not set — ignored.",
                stacklevel=2,
            )
            return None
        return os.environ[var_name]

    def handle_enabled(self: "BaseConfig") -> None:
        if self.enabled is None:
            return
        if not isinstance(self.enabled, bool):
            raise ValueError(f"Item {hilight(self.name)} enabled must be a boolean.")

    @property
    def hash(self: "BaseConfig") -> str:
        return hash_dict(asdict(self))


@dataclass
class MonitorConfig(BaseConfig):
    """The ``[monitor]`` section: how the program runs, not what it looks for.

    The schedule lives here because *when* to scrape is a property of the
    program, not of a product or of a marketplace: the same three keys used to
    exist on every ``[item.*]`` and ``[marketplace.*]`` section, which meant the
    same question was answered in as many places as the file had sections.
    Those are still read as a fallback (see ``MarketplaceMonitor.schedule_jobs``)
    but nothing writes them any more.
    """

    proxy_server: List[str] | None = None
    proxy_bypass: str | None = None
    proxy_username: str | None = None
    proxy_password: str | None = None
    #: Seconds between searches, or the low end of the random range.
    search_interval: int | None = None
    #: The high end of the random range.  Equal to ``search_interval`` (or
    #: absent) means a fixed interval.
    max_search_interval: int | None = None
    #: Times of day to search at, on top of whatever interval is set.
    start_at: List[str] | None = None
    #: Whether re-checking listings already stored gets a browser of its own.
    #:
    #: On, so a price change or a sold listing is noticed while the searches
    #: carry on rather than in the gaps between them.  Off, the same work
    #: happens on the one tab the search already uses, in between searches --
    #: which on a busy schedule means barely at all.
    #:
    #: The cost is a second browser, and it is only paid when there is
    #: something to re-check: the lane is not started while the store has
    #: nothing overdue in it (see ``MarketplaceMonitor._listings_to_review``).
    parallel_listing_updates: bool = True
    #: Whether the platforms search at the same time instead of one after the
    #: other.
    #:
    #: On, each platform gets a browser and a thread of its own and they run
    #: side by side, each keeping its own cycle: one platform being slow, being
    #: cancelled or failing does not hold up the other.  Off, the monitor works
    #: through a single queue, which means every Facebook search finishes before
    #: the first Mercado Libre one starts -- and a Facebook pass over a handful
    #: of products is the better part of an hour.
    #:
    #: The cost is a browser per platform, in memory and in traffic.  Turn it
    #: off on a machine where that hurts.
    parallel_marketplaces: bool = True
    #: Whether a listing is notified the moment it passes, rather than at the
    #: end of the search it was found in.
    #:
    #: Off by default, and the reason is the message rather than the delay: a
    #: search that turns up six listings sends one notification about six of
    #: them, and switching this on turns that into six notifications.  On a
    #: platform that takes half an hour to search, though, the last of those
    #: six was found twenty-nine minutes before it was sent -- which for a
    #: marketplace where the good listing is gone in ten is the whole game.
    #:
    #: Sending never happens on the scraping thread either way: see
    #: :mod:`ai_marketplace_monitor.dispatch`.
    notify_immediately: bool = False
    #: How many words of a listing's description a notification carries.
    #:
    #: Twenty-five, because a Mercado Libre seller who pastes their whole
    #: catalogue into the description produced messages Telegram refused
    #: outright ("Message is too long") and, when they did arrive, a wall of
    #: text with the price somewhere in the middle of it.  Zero or below means
    #: no limit.  What the scraper *stores* is never shortened -- this is the
    #: notification's copy only.
    #:
    #: Words rather than lines, which is what this counted first and was wrong:
    #: a line is a property of the screen, not of the text.  Five lines is five
    #: short ones on a desktop and fifteen wrapped ones on a phone, and a
    #: seller who writes one unbroken paragraph gets past a line limit with a
    #: description that still fills the screen.
    max_description_words: int | None = None
    #: Retired: this counted lines, which measured the screen rather than the
    #: text.  Accepted so a file written while it existed still loads, ignored
    #: so it cannot quietly mean something else, and dropped from the file by
    #: the web UI the next time the notification settings are saved.
    max_description_lines: int | None = None
    #: How old a listing's last check has to be before it is worth repeating.
    listing_recheck_interval: int | None = None
    #: When a round of re-checks happens: a fixed interval, or the floor of a
    #: random range.  See :mod:`ai_marketplace_monitor.review`.
    listing_review_interval: int | None = None
    #: The ceiling of that random range.
    listing_review_max_interval: int | None = None
    #: Times of day to review at, on top of whatever interval is set.
    listing_review_start_at: List[str] | None = None
    #: How many stored listings one round re-checks.
    listing_review_batch: int | None = None
    #: What happens when the search running right now is edited.
    #:
    #: On (the default), the loop takes the new settings into the search it is
    #: already doing and carries on with it.  Off, it abandons the search and
    #: goes to the next one, which is what it used to do always -- defensible,
    #: because results judged against replaced settings are of no use, but
    #: expensive: the page load and the AI calls already spent are thrown away,
    #: and a user tweaking a maximum price watches their search restart every
    #: time they save.
    #:
    #: Not everything can be taken up mid-search whatever this says: see
    #: :meth:`ai_marketplace_monitor.monitor.MarketplaceMonitor._apply_live_config`
    #: for what the running search can and cannot absorb.
    apply_changes_while_running: bool = True
    #: What happens when the search running right now is deleted.
    #:
    #: ``"stop"`` (the default) ends it at the next checkpoint and moves on --
    #: the natural reading of deleting something.  ``"finish"`` lets it run to
    #: the end and notify as usual, and only then forgets it, which is what to
    #: choose when a search that is nearly done is worth more than the tidiness.
    #: Either way the scraper itself keeps going.
    on_delete_running: str = "stop"

    def handle_parallel_listing_updates(self: "MonitorConfig") -> None:
        if self.parallel_listing_updates is None:
            self.parallel_listing_updates = True
            return
        if not isinstance(self.parallel_listing_updates, bool):
            raise ValueError(
                f"Monitor {hilight('parallel_listing_updates')} must be true or false."
            )

    def handle_parallel_marketplaces(self: "MonitorConfig") -> None:
        if self.parallel_marketplaces is None:
            self.parallel_marketplaces = True
            return
        if not isinstance(self.parallel_marketplaces, bool):
            raise ValueError(f"Monitor {hilight('parallel_marketplaces')} must be true or false.")

    def handle_notify_immediately(self: "MonitorConfig") -> None:
        if self.notify_immediately is None:
            self.notify_immediately = False
            return
        if not isinstance(self.notify_immediately, bool):
            raise ValueError(f"Monitor {hilight('notify_immediately')} must be true or false.")

    def handle_max_description_words(self: "MonitorConfig") -> None:
        if self.max_description_words is None:
            return
        # `false` is how a TOML file says "no limit", and it has to keep
        # working: the setting can be switched off, and a boolean is what the
        # web UI's checkbox would write if it ever became one.
        if self.max_description_words is False:
            self.max_description_words = 0
            return
        if self.max_description_words is True:
            self.max_description_words = None
            return
        if not isinstance(self.max_description_words, int):
            raise ValueError(
                f"Monitor {hilight('max_description_words')} must be a number of words."
            )

    def handle_max_description_lines(self: "MonitorConfig") -> None:
        # Retired in favour of `max_description_words`.  Not an error, because
        # refusing to load over a key this program itself wrote would turn an
        # upgrade into a broken monitor; and not silently re-read as words
        # either, because "5 lines" and "5 words" are not the same request.
        if self.max_description_lines is not None:
            self.max_description_lines = None

    def handle_apply_changes_while_running(self: "MonitorConfig") -> None:
        if self.apply_changes_while_running is None:
            self.apply_changes_while_running = True
            return
        if not isinstance(self.apply_changes_while_running, bool):
            raise ValueError(
                f"Monitor {hilight('apply_changes_while_running')} must be true or false."
            )

    def handle_on_delete_running(self: "MonitorConfig") -> None:
        if self.on_delete_running is None:
            self.on_delete_running = "stop"
            return
        if not isinstance(self.on_delete_running, str):
            raise ValueError(f"Monitor {hilight('on_delete_running')} must be a string.")
        self.on_delete_running = self.on_delete_running.strip().lower()
        if self.on_delete_running not in ("stop", "finish"):
            raise ValueError(
                f"""Monitor {hilight('on_delete_running')} must be "stop" or "finish"."""
            )

    def handle_listing_review_interval(self: "MonitorConfig") -> None:
        if self.listing_review_interval is None:
            return
        self.listing_review_interval = interval_in_seconds(
            self.listing_review_interval, "Monitor", "listing_review_interval"
        )

    def handle_listing_review_max_interval(self: "MonitorConfig") -> None:
        if self.listing_review_max_interval is None:
            return
        self.listing_review_max_interval = interval_in_seconds(
            self.listing_review_max_interval, "Monitor", "listing_review_max_interval"
        )

    def handle_listing_review_start_at(self: "MonitorConfig") -> None:
        if self.listing_review_start_at is None:
            return
        self.listing_review_start_at = validated_start_at(
            self.listing_review_start_at, "Monitor listing_review"
        )

    def handle_listing_review_batch(self: "MonitorConfig") -> None:
        if self.listing_review_batch is None:
            return
        if isinstance(self.listing_review_batch, bool) or not isinstance(
            self.listing_review_batch, int
        ):
            raise ValueError(f"Monitor {hilight('listing_review_batch')} must be a whole number.")
        if self.listing_review_batch < 1:
            raise ValueError(f"Monitor {hilight('listing_review_batch')} must be at least 1.")

    def handle_listing_recheck_interval(self: "MonitorConfig") -> None:
        if self.listing_recheck_interval is None:
            return
        self.listing_recheck_interval = interval_in_seconds(
            self.listing_recheck_interval, "Monitor", "listing_recheck_interval"
        )

    def handle_search_interval(self: "MonitorConfig") -> None:
        if self.search_interval is None:
            return
        self.search_interval = interval_in_seconds(self.search_interval, "Monitor", "search_interval")

    def handle_max_search_interval(self: "MonitorConfig") -> None:
        if self.max_search_interval is None:
            return
        self.max_search_interval = interval_in_seconds(
            self.max_search_interval, "Monitor", "max_search_interval"
        )

    def handle_start_at(self: "MonitorConfig") -> None:
        if self.start_at is None:
            return
        self.start_at = validated_start_at(self.start_at, "Monitor")

    def handle_proxy_server(self: "MonitorConfig") -> None:
        if self.proxy_server is None:
            return

        if isinstance(self.proxy_server, str):
            self.proxy_server = [self.proxy_server]

        if not all(isinstance(x, str) for x in self.proxy_server):
            raise ValueError(f"Item {hilight(self.name)} proxy_server must be a string.")
        if not all(x.startswith("http://") or x.startswith("https://") for x in self.proxy_server):
            raise ValueError(
                f"Item {hilight(self.name)} proxy_server must start with http:// or https://"
            )

    def handle_proxy_bypass(self: "MonitorConfig") -> None:
        if self.proxy_bypass is None:
            return
        if not isinstance(self.proxy_bypass, str):
            raise ValueError(f"Item {hilight(self.name)} proxy_bypass must be a string.")

    def handle_proxy_username(self: "MonitorConfig") -> None:
        if self.proxy_username is None:
            return

        if not isinstance(self.proxy_username, str):
            raise ValueError(f"Item {hilight(self.name)} proxy_username must be a string.")

    def handle_proxy_password(self: "MonitorConfig") -> None:
        if self.proxy_password is None:
            return

        if not isinstance(self.proxy_password, str):
            raise ValueError(f"Item {hilight(self.name)} proxy_password must be a string.")

    def get_proxy_options(self: "MonitorConfig") -> ProxySettings | None:
        if not self.proxy_server:
            return None
        res = ProxySettings(server=random.choice(self.proxy_server))
        if self.proxy_username and self.proxy_password:
            res["username"] = self.proxy_username
            res["password"] = self.proxy_password
        if self.proxy_bypass:
            res["bypass"] = self.proxy_bypass
        return res


def fold_text(text: str) -> str:
    """Lowercase, strip accents and collapse whitespace, for marker matching.

    Marketplaces render the same phrase with and without accents depending on
    the locale pack, and a stray non-breaking space is common; comparing folded
    text keeps marker lists short and stops a missed accent from silently
    disabling a check.
    """
    stripped = unicodedata.normalize("NFD", text or "")
    stripped = "".join(char for char in stripped if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", stripped).strip().lower()


def calculate_file_hash(file_paths: List[Path]) -> str:
    """Calculate the SHA-256 hash of the file content."""
    hasher = hashlib.sha256()
    # they should exist, just to make sure
    for file_path in file_paths:
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        #
        with open(file_path, "rb") as file:
            while chunk := file.read(8192):
                hasher.update(chunk)
    return hasher.hexdigest()


def merge_dicts(dicts: list) -> dict:
    """Merge a list of dictionaries into a single dictionary, including nested dictionaries.

    :param dicts: A list of dictionaries to merge.
    :return: A single merged dictionary.
    """

    def merge(d1: dict, d2: dict) -> dict:
        for key, value in d2.items():
            if key in d1:
                if isinstance(d1[key], dict) and isinstance(value, dict):
                    d1[key] = merge(d1[key], value)
                elif isinstance(d1[key], list) and isinstance(value, list):
                    d1[key].extend(value)
                else:
                    d1[key] = value
            else:
                d1[key] = value
        return d1

    result: Dict[str, Any] = {}
    for dictionary in dicts:
        result = merge(result, dictionary)
    return result


def normalize_string(string: str) -> str:
    """Normalize a string by replacing multiple spaces (including space, tab, and newline) with a single space."""
    return re.sub(r"\s+", " ", string).lower()


ParserElement.enable_packrat()
double_quoted_string = ('"' + CharsNotIn('"').leaveWhitespace() + '"').setParseAction(
    lambda t: t[1]
)  # removes quotes, keeps only the content
single_quoted_string = ("'" + CharsNotIn("'").leaveWhitespace() + "'").setParseAction(
    lambda t: t[1]
)  # removes quotes, keeps only the content

special_chars = "!@#$%^&*-_=+[]{}|;:'\",.<>?/\\`~"
unquoted_string = Word(alphanums + special_chars)

operand = double_quoted_string | single_quoted_string | unquoted_string
and_op = Keyword("AND")
or_op = Keyword("OR")
not_op = Keyword("NOT")

# Define the grammar for parsing
expr = infix_notation(
    operand,
    [
        (not_op, 1, opAssoc.RIGHT),
        (and_op, 2, opAssoc.LEFT),
        (or_op, 2, opAssoc.LEFT),
    ],
)


def is_substring(
    var1: str | List[str], var2: str | List[str], logger: Logger | None = None
) -> bool:
    """Check if var1 is a substring of var2, after normalizing both strings. One of them can be a list of strings.

    var1: can be a single string, or a list of string, for which a condition of OR is assumed.
          this program will parse var11 for "AND", "OR" and "NOT", and return the results of the
          logical expression.

    var2: one or more strings for testing if strings in  "var1" is a substring.
    """
    if isinstance(var1, list):
        return any(is_substring(x, var2, logger) for x in var1)

    # parse the expression
    parsed = ""
    try:
        parsed = expr.parseString(var1, parseAll=True)[0]
    except Exception:
        # treat var1 as literal string for searching.
        if any(x in var1 for x in (" AND ", " OR ", " NOT ", "(NOT ")) or var1.startswith("NOT "):
            if logger:
                logger.warning(
                    f"Failed to parse {var1} as a logical expression. Treating it as literal string."
                )
        if isinstance(var2, str):
            return normalize_string(var1) in normalize_string(var2)
        return any(normalize_string(var1) in normalize_string(s2) for s2 in var2)

    def evaluate_expression(parsed_expression: str | ParseResults) -> bool:
        if isinstance(parsed_expression, str):
            if isinstance(var2, str):
                return normalize_string(parsed_expression) in normalize_string(var2)
            return any(normalize_string(parsed_expression) in normalize_string(s) for s in var2)

        if len(parsed_expression) == 1:
            return evaluate_expression(parsed_expression[0])

        if parsed_expression[0] == "NOT":
            return not evaluate_expression(parsed_expression[1])

        if parsed_expression[-2] == "AND":
            return evaluate_expression(parsed_expression[:-2]) and evaluate_expression(
                parsed_expression[-1]
            )

        if parsed_expression[-2] == "OR":
            return evaluate_expression(parsed_expression[:-2]) or evaluate_expression(
                parsed_expression[-1]
            )
        if logger:
            logger.error(f"Invalid expression: {parsed_expression}")
        return False

    return evaluate_expression(parsed)


class ChangeHandler(FileSystemEventHandler):
    def __init__(self: "ChangeHandler", files: List[str]) -> None:
        self.changed = False
        # Normalize to real paths — on macOS /var/folders is a symlink
        # to /private/var/folders and watchdog reports the resolved form.
        self.files = {os.path.realpath(f) for f in files}

    def _mark_if_watched(self: "ChangeHandler", path: "str | bytes | None") -> None:
        if path and os.path.realpath(path) in self.files:
            self.changed = True

    def on_modified(self: "ChangeHandler", event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._mark_if_watched(event.src_path)

    def on_created(self: "ChangeHandler", event: FileSystemEvent) -> None:
        # Atomic writes via os.replace() may appear as a create on the
        # destination path (depending on platform + watchdog backend).
        if not event.is_directory:
            self._mark_if_watched(event.src_path)

    def on_deleted(self: "ChangeHandler", event: FileSystemEvent) -> None:
        # On macOS, os.replace() over an existing file fires a 'deleted'
        # event on the destination path, not 'moved'. Treat it as a change.
        if not event.is_directory:
            self._mark_if_watched(event.src_path)

    def on_moved(self: "ChangeHandler", event: FileSystemEvent) -> None:
        # On Linux (inotify), atomic writes via tempfile + os.replace()
        # land here: src_path is the temp file, dest_path is the real one.
        if not event.is_directory:
            self._mark_if_watched(getattr(event, "dest_path", None))
            self._mark_if_watched(event.src_path)


def doze(
    duration: int,
    files: List[Path] | None = None,
    keyboard_monitor: KeyboardMonitor | None = None,
    stop_when: Callable[[], bool] | None = None,
) -> SleepStatus:
    """Sleep for a specified duration while monitoring the change of files.

    ``stop_when`` is polled once a second alongside the file watcher, so a
    caller can cut a long sleep short on something the watcher cannot see --
    the web UI's pause switch being the reason it exists.

    Return:
        0: if doze was done naturally.
        1: if doze was disrupted by keyboard
        2: if doze was disrupted by file change
        3: if doze was disrupted by ``stop_when``
    """
    event_handler = ChangeHandler([str(x) for x in (files or [])])
    observers = []
    if keyboard_monitor:
        keyboard_monitor.start_sleeping()

    for filename in files or []:
        if not filename.exists():
            raise FileNotFoundError(f"File not found: {filename}")
        observer = Observer()
        # we can only monitor a directory
        observer.schedule(event_handler, str(filename.parent), recursive=False)
        observer.start()
        observers.append(observer)

    start_time = time.time()
    try:
        while time.time() - start_time < duration:
            if event_handler.changed:
                return SleepStatus.BY_FILE_CHANGE
            if stop_when is not None and stop_when():
                return SleepStatus.BY_CONDITION
            time.sleep(1)
            if keyboard_monitor and not keyboard_monitor.is_sleeping():
                return SleepStatus.BY_KEYBOARD
        return SleepStatus.NOT_DISRUPTED
    finally:
        for observer in observers:
            observer.stop()
            observer.join()


# One price: an optional currency symbol or code, then a number whose thousands
# may be grouped by a dot, comma, space or non-breaking space, with an optional
# 1-2 digit decimal tail.
#
# The space alternatives matter: Facebook renders CLP as "100 000", and a pattern
# that only knows about commas reads that as two separate prices -- or, with a
# currency prefix, throws the thousands away entirely and turns $100.000 into
# $100.
_THOUSANDS_SEP = r"[.,\s\u00a0\u202f]"

_PRICE_RE = re.compile(
    r"(?P<currency>[^\d\s.,|]{0,4})[\s\u00a0]?"
    r"(?P<amount>\d{1,3}(?:" + _THOUSANDS_SEP + r"\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    # A trailing code ("150.000 CLP"), letters only so that the "$" opening the
    # next price in a concatenated pair is never swallowed as this one's suffix.
    r"(?:[\s\u00a0]?(?P<code>[A-Za-z]{2,4})\b)?"
)


def extract_price(price: str) -> str:
    """Pull the price (and the struck-through original, if any) out of raw text.

    Facebook concatenates the current and original price into one string, so up
    to two are returned joined by " | ".  Each is kept in the marketplace's own
    formatting rather than normalized; only the split is our doing.
    """
    if not price or price == "**unspecified**":
        return price

    matches = [match.group(0).strip() for match in _PRICE_RE.finditer(price)]
    matches = [match for match in matches if match]
    if matches:
        return " | ".join(matches[:2])
    return price


#: The first run of digits and separators inside a price string.  Deliberately
#: greedy about spaces: "100 000" is one number, not two.
_PRICE_NUMBER_RE = re.compile(r"\d[\d.,\s\u00a0\u202f]*")

#: Whitespace that groups thousands: plain, non-breaking and narrow non-breaking
#: spaces, all of which Facebook emits depending on the locale.
_PRICE_SPACE_RE = re.compile(r"[\s\u00a0\u202f]")

#: What the marketplace prints instead of a zero price.
_FREE_RE = re.compile(r"^(free|gratis|gratuito|grátis|gratuit|kostenlos|regalo)$", re.I)


def price_value(price: str | None) -> float | None:
    """The amount in a scraped price string, or None when there is no number.

    Prices are stored exactly as the marketplace printed them, so this has to
    cope with a currency symbol or code on either side, thousands grouped by a
    dot, a comma or a (non-breaking) space, and the " | " pair that
    :func:`extract_price` produces for a discounted listing -- whose first half
    is the current price.

    The separator is the hard part: "$100.000" is a hundred thousand Chilean
    pesos while "$100.00" is a hundred dollars.  When a dot and a comma both
    appear, the last of them is the decimal point; when only one appears it is a
    decimal point if exactly one or two digits follow it, and grouping
    otherwise.
    """
    if not price:
        return None

    # "$180.000 | $200.000" -- the current price first, the struck-through
    # original second.
    text = price.split("|")[0].strip()
    if not text or text == "**unspecified**":
        return None
    if _FREE_RE.match(text):
        return 0.0

    matched = _PRICE_NUMBER_RE.search(text)
    if matched is None:
        return None

    digits = _PRICE_SPACE_RE.sub("", matched.group(0)).strip(".,")
    if not digits:
        return None

    last_dot = digits.rfind(".")
    last_comma = digits.rfind(",")
    decimal_at = -1
    if last_dot >= 0 and last_comma >= 0:
        decimal_at = max(last_dot, last_comma)
    elif last_dot >= 0 or last_comma >= 0:
        only = max(last_dot, last_comma)
        # One or two trailing digits is a decimal point; three is grouping.
        if len(digits) - only - 1 in (1, 2):
            decimal_at = only

    if decimal_at >= 0:
        whole = digits[:decimal_at].replace(".", "").replace(",", "")
        digits = f"{whole}.{digits[decimal_at + 1:]}"
    else:
        digits = digits.replace(".", "").replace(",", "")

    try:
        return float(digits)
    except ValueError:
        return None


def convert_to_seconds(time_str: str) -> int:
    cal = parsedatetime.Calendar(version=parsedatetime.VERSION_CONTEXT_STYLE)
    time_struct, _ = cal.parse(time_str)
    return int(time.mktime(time_struct) - time.mktime(time.localtime()))


def interval_in_seconds(value: Any, context: str, key: str) -> int:
    """One scheduling interval, in seconds.

    Accepts what the config file accepts: a number of seconds, or a phrase the
    calendar parser understands (``"30m"``, ``"2h"``, ``"1d"``).  Shared by
    every section that can carry a schedule -- the monitor's global one and the
    deprecated per-item/per-marketplace ones -- so they cannot drift apart.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{context} {key} must be a number of seconds or a duration.")
    if isinstance(value, str):
        try:
            value = convert_to_seconds(value)
        except Exception as e:
            raise ValueError(f"{context} {key} {value} is not recognized.") from e
    if not isinstance(value, int) or value < 1:
        raise ValueError(f"{context} {key} must be at least 1 second.")
    return value


def validated_start_at(value: Any, context: str) -> List[str]:
    """The times of day a search may start at.

    ``HH:MM``, ``HH:MM:SS``, ``*:MM`` (every hour), ``*:MM:SS`` or ``*:*:SS``
    (every minute).  Returns the list form, so a lone string is accepted.
    """
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not all(isinstance(x, str) for x in values):
        raise ValueError(f"{context} start_at must be a string or list of string.")

    for val in values:
        if (
            val.count(":") not in (1, 2)
            or val.count("*") == 3
            or not all(x == "*" or (x.isdigit() and len(x) == 2) for x in val.split(":"))
        ):
            raise ValueError(f"{context} start_at {val} is not recognized.")
        if not any(
            _parses_as_time(val, pattern)
            for pattern in ("%H:%M:%S", "%H:%M", "*:%M:%S", "*:%M", "*:*:%S")
        ):
            raise ValueError(f"{context} start_at {val} is not recognized.")
    return values


def _parses_as_time(value: str, pattern: str) -> bool:
    try:
        time.strptime(value, pattern)
        return True
    except ValueError:
        return False


def hilight(text: str, style: str = "name") -> str:
    """Highlight the keywords in the text with the specified color."""
    color = {
        "name": "cyan",
        "fail": "red",
        "info": "blue",
        "succ": "green",
        "dim": "gray",
    }.get(style, "blue")
    return f"[{color}]{text}[/{color}]"


def fetch_with_retry(
    url: str,
    timeout: int = 10,
    max_retries: int = 3,
    backoff_factor: float = 1.5,
    logger: Logger | None = None,
) -> Tuple[bytes, str] | None:
    """Fetch URL content with retry logic

    Args:
        url: URL to fetch
        timeout: Timeout in seconds
        max_retries: Maximum number of retry attempts
        backoff_factor: Multiplier for exponential backoff
        logger: logger object

    Returns:
        Tuple of (content, content_type) if successful, None if failed
    """
    if logger:
        logger.debug(f"Fetching {url} with timeout {timeout}s")
    for attempt in range(max_retries):
        try:
            response = requests.get(
                url,
                timeout=timeout,
                stream=True,  # Good practice for downloading files
            )
            response.raise_for_status()  # Raises exception for 4XX/5XX status codes

            return response.content, response.headers["Content-Type"]

        except Timeout:
            wait_time = backoff_factor**attempt
            if logger:
                logger.warning(
                    f"Timeout fetching {url} (attempt {attempt + 1}/{max_retries}). "
                    f"Waiting {wait_time:.1f}s before retry"
                )

            if attempt < max_retries - 1:
                time.sleep(wait_time)

        except RequestException as e:
            if logger:
                logger.error(f"Error fetching {url}: {e!s}")
            return None

    if logger:
        logger.error(f"Failed to fetch {url} after {max_retries} attempts")
    return None


def resize_image_data(image_data: bytes, max_width: int = 800, max_height: int = 600) -> bytes:
    # Create image object from binary data
    try:
        image = Image.open(io.BytesIO(image_data))
        if image.format == "GIF":
            return image_data
    except Exception:
        # if unacceptable file format, just return
        return image_data

    # Calculate new dimensions maintaining aspect ratio
    width, height = image.size
    ratio = min(max_width / width, max_height / height)
    if ratio >= 1:
        return image_data

    new_width = int(width * ratio)
    new_height = int(height * ratio)

    # Resize image
    resized_image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)

    # Convert back to bytes
    buffer = io.BytesIO()
    resized_image.save(buffer, format=image.format)
    return buffer.getvalue()


class Translator:
    def __init__(
        self: "Translator", locale: str | None = None, dictionary: Dict[str, str] | None = None
    ) -> None:
        self.locale = locale
        self._dictionary: Dict[str, str] = copy.deepcopy(dictionary or {})

    def __call__(self: "Translator", word: str) -> str:
        """Return translated version"""
        return self._dictionary.get(word, word)
