import json
import sys
import threading
import time
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from datetime import datetime, timezone
from enum import Enum
from logging import Logger
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, List, Set, Tuple

import humanize
import inflect
import rich
import schedule  # type: ignore
from playwright.sync_api import BrowserContext, Playwright  # type: ignore

from .browser_engine import (
    ENGINE_NAME,
    PATCHES_CDP,
    chrome_is_installed,
    sync_playwright,
)
from rich.pretty import pretty_repr
from rich.prompt import Prompt

from . import control
from .ai import AIBackend, AIResponse
from .config import Config, all_marketplaces, supported_ai_backends, supported_marketplaces
from .control import CancelledScrape, ScrapeInterrupted, SearchStopped, SearchSuperseded
from .dispatch import NotificationDispatcher
from .lanes import BrowserLane, context_is_alive
from .listing import Listing
from .live_config import (
    MARKETPLACE,
    MODIFIED,
    REMOVED,
    VOLATILE_OPTIONS,
    ConfigChange,
    Pair,
    diff_config,
    fingerprints,
)
from .marketplace import Marketplace, TItemConfig, TMarketplaceConfig
from .messages import DEFAULT_DESCRIPTION_WORDS
from .notification import NotificationConfig, NotificationStatus
from .notify_reasons import NotifyReasons, reasons_from_config
from .toplist import new_top, new_tops, remember_top
from .tracking import (
    LABEL as TRACKED_LABEL,
    PLATFORM as TRACKED_PLATFORM,
    reader_for as tracking_reader_for,
    stock_alert,
    tracked_id,
)
from .observations import get_observation, is_known, record_observation, record_rating
from .pause import is_force_paused, is_paused
from .refresh import (
    DEFAULT_RECHECK_INTERVAL,
    ListingRefresher,
    RefreshReport,
    stale_records,
)
from .review import ReviewSchedule, schedule_from_config
from .session import (
    import_is_pending,
    load_session,
    mark_import_applied,
    profile_dir,
    profile_is_new,
    release_stale_profile_lock,
    reset_profile,
)
from .user import User
from .utils import (
    CacheType,
    CounterItem,
    KeyboardMonitor,
    SleepStatus,
    Translator,
    aimm_event,
    amm_home,
    cache,
    calculate_file_hash,
    counter,
    doze,
    hilight,
)


#: What each platform is called in a message to a person.  The monitor's own
#: name for a platform is a key in a configuration file ("mercadolibre"), and
#: printing that in a notification is printing an implementation detail at
#: somebody reading their phone.
MARKETPLACE_LABELS: Dict[str, str] = {
    "facebook": "Facebook Marketplace",
    "mercadolibre": "Mercado Libre",
    "lider": "Lider",
    "sodimac": "Sodimac",
}


def _marketplace_label(marketplace: str) -> str:
    """What a notification calls the place a listing came from.

    `tracked` is deliberately not in the table above -- it is not a marketplace,
    and listing it as one is how it ends up offered as somewhere to search -- so
    it is named here instead.  Without this, the top-1 of a followed page was
    the one notification in the whole program that said "tracked" at the reader
    in English while every other tracker message said "Seguimiento".
    """
    name = marketplace.lower()
    if name == TRACKED_PLATFORM:
        return TRACKED_LABEL
    return MARKETPLACE_LABELS.get(name, marketplace)


#: How long a gap before the next search has to be before the browsers are
#: worth closing.  A search every two minutes should not pay a Chromium start
#: each time; a search every thirty should not hold a browser (and its several
#: hundred megabytes, and its window) for twenty-nine of them.
IDLE_BROWSER_RELEASE = 120.0

#: How long the monitor thread blocks on one lane before looking at the others.
#: Short, because the point of looking is to notice a lane that has finished
#: while its neighbours have not: a longer wait would put back the barrier this
#: exists to remove, and a shorter one would spin for nothing.
LANE_REAP_INTERVAL = 0.5


#: Playwright's default Chromium flags that a person's Chrome never carries.
#:
#: Playwright starts Chromium with thirty-five of them.  This is deliberately a
#: *subset*: the ones left in are load bearing and dropping them trades a
#: fingerprint for a browser that falls over.  ``--disable-dev-shm-usage`` in
#: particular is the difference, in a container, between working and dying on
#: the first page; the sandbox and the GPU flags are the same kind of thing.
#:
#: What is dropped is the housekeeping a real browser does and an automated one
#: is told not to: updating components, syncing, loading extensions, checking
#: whether it is the default browser.  A page cannot read the command line, but
#: it can read what these flags *do* -- an extensions API with nothing in it, a
#: component updater that never ran, feature flags that do not match the build.
#: Let Chromium fall back to software WebGL when there is no GPU to use.
#:
#: This is the opposite kind of flag to the ones above: those are removed
#: because a person's browser does not carry them, and this one is added
#: because of what its absence *does*.  Measured, in the container, with
#: patchright's Chromium: without it ``canvas.getContext('webgl')`` returns
#: null and the page sees a browser with **no WebGL at all**, which is a
#: stronger tell than software rendering could ever be -- every desktop browser
#: on earth has WebGL.  With it, the context comes back with the same renderer
#: string and the same thirty-five extensions Playwright's build reported on
#: its own.
#:
#: Harmless where a GPU exists: it permits the software fallback, it does not
#: force it, so the machines that have hardware WebGL keep using it.
SOFTWARE_WEBGL_FLAG = "--enable-unsafe-swiftshader"

TELLTALE_DEFAULT_ARGS: Tuple[str, ...] = (
    "--disable-back-forward-cache",
    "--disable-client-side-phishing-detection",
    "--disable-component-extensions-with-background-pages",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-field-trial-config",
    "--disable-hang-monitor",
    "--disable-popup-blocking",
    "--disable-prompt-on-repost",
    "--disable-renderer-backgrounding",
    "--disable-sync",
    "--metrics-recording-only",
    "--no-default-browser-check",
    "--no-first-run",
)


class JobOutcome(Enum):
    """How one scheduled search ended, from the loop's point of view."""

    #: It ran to the end, or failed in a way the search itself absorbed.
    DONE = "done"
    #: A forced pause cut it short; the browser is gone and the loop unwinds.
    CANCELLED = "cancelled"
    #: The configuration changed under it and no longer wanted it.  Nothing is
    #: wrong: the loop adopts the new configuration and moves to the next search.
    SUPERSEDED = "superseded"
    #: The user ended this one search from the web UI.  Same handling as
    #: ``SUPERSEDED`` -- browser kept, next search started -- and kept apart so
    #: the log says which of the two happened.
    STOPPED = "stopped"


@dataclass
class ConfigProbe:
    """The answer to "has the configuration on disk moved away from mine?".

    Three answers, not two, because "it changed and I cannot read it" is a real
    state with its own handling: a file caught halfway through a save, or one
    the user has genuinely broken.  Neither is a reason to stop searching under
    the configuration already loaded.
    """

    #: Whether the files differ from the version the loop is running.
    changed: bool
    #: The new configuration, or None when it could not be parsed.
    config: Config | None = None
    #: The hash of the files as they were read.
    version: str | None = None
    #: What differs, or None when the new configuration could not be read.
    change: ConfigChange | None = None
    #: Why it could not be read, when it could not.
    error: str | None = None

    @property
    def readable(self: "ConfigProbe") -> bool:
        return self.config is not None and self.change is not None


def _stored_rating(rating: Dict[str, Any] | None) -> AIResponse:
    """The AI verdict an observation holds, as the object a card expects.

    A listing announced as the cheapest one has almost always been evaluated
    already -- just not by this flow, which reads the store rather than the
    scraper.  Rebuilding the verdict from the record is what keeps the top-1
    message from being the one notification with no stars on it.

    Anything missing or unreadable comes back as "not evaluated", which
    :func:`~ai_marketplace_monitor.messages.build_card` renders by dropping the
    rating line -- the honest result of not knowing.
    """
    if not isinstance(rating, dict):
        return AIResponse(score=3, comment=AIResponse.NOT_EVALUATED)
    score = rating.get("score")
    comment = rating.get("comment")
    if not isinstance(score, int) or score not in range(1, 6) or not isinstance(comment, str):
        return AIResponse(score=3, comment=AIResponse.NOT_EVALUATED)
    return AIResponse(score=score, comment=comment, name=str(rating.get("name") or ""))


class MarketplaceMonitor:
    active_marketplaces: ClassVar = {}

    #: Set when a configuration was adopted somewhere that may not touch the
    #: ``schedule`` registry.  A class default as well as an ``__init__``
    #: assignment, for the same reason the locks below are made on demand: a
    #: monitor is not always built through the constructor, and a flag that
    #: only exists when it was turns a missing test fixture into an
    #: AttributeError three frames from the cause.
    _schedule_dirty: bool = False

    #: Options a running search has already spent by the time it yields its
    #: first listing: every one of them went into the URL it is now paging
    #: through, or into the loop over phrases and cities that built it.  Editing
    #: one cannot change the search under way -- the request has been made --
    #: so the new value is reported as waiting for that search's next run
    #: rather than quietly claimed as applied.  Everything not named here is
    #: read again for each listing (keywords, sellers, ratings, prompts) and
    #: therefore genuinely does take effect mid-search.
    URL_BOUND_OPTIONS: ClassVar[frozenset] = frozenset(
        {
            "search_phrases",
            "search_city",
            "city_name",
            "search_region",
            "radius",
            "min_price",
            "max_price",
            "date_listed",
            "delivery_method",
            "availability",
            "sort_by",
            "currency",
            "site",
            "free_shipping",
            "shipping_origin",
            "max_pages",
            "start_at",
            "search_interval",
            "max_search_interval",
            "language",
        }
    )

    #: How often a running search may stop to look at the configuration file.
    #: The look itself is a ``stat`` of one or two files, so the interval is
    #: not about cost -- it is about not hashing and re-parsing a file that a
    #: text editor is writing a few bytes at a time.
    CONFIG_PROBE_INTERVAL = 2.0

    #: True while a tracker's first read has a browser open on its own thread.
    #: A class attribute so that asking is safe before anything has started one.
    _ingesting = False
    #: Guards the check-and-set above.  A configuration can be adopted by a
    #: lane's checkpoint as well as by the monitor thread, and two of them
    #: reading "no ingest is running" at the same instant would open two
    #: browsers on the same tracker.
    _ingest_lock = threading.Lock()

    def __init__(
        self: "MarketplaceMonitor",
        config_files: List[Path] | None,
        headless: bool | None,
        logger: Logger | None,
    ) -> None:
        for file_path in config_files or []:
            if not file_path.exists():
                raise FileNotFoundError(f"Config file {file_path} not found.")
        default_config = amm_home / "config.toml"
        self.config_files = ([default_config] if default_config.exists() else []) + (
            [x.expanduser().resolve() for x in config_files or []]
        )
        #
        self.config: Config | None = None
        self.config_hash: str | None = None
        # The configuration as it was when it was loaded, before the search
        # code mutated anything in it.  Diffing against this rather than
        # against the live objects is what keeps a counter incremented by a
        # search from reading as an edit the user made.
        self._loaded_snapshot: Dict[str, Any] = {}
        # Per search: a short string that changes exactly when its work does.
        # Lets a pass that has already run half the searches adopt an edit
        # without starting over and without skipping what was edited.
        self._fingerprints: Dict[Pair, str] = {}
        # The (item, marketplace) being searched right now, or None between
        # searches.  Read by the checkpoint guard, which has nothing to decide
        # unless a search is actually under way.
        #
        # Per thread, because searches can run on lanes of their own: a guard
        # firing on the Mercado Libre lane must ask about the Mercado Libre
        # search, not about whatever the monitor thread happens to be doing.
        self._thread_state = threading.local()
        # Reading and adopting the configuration is one thread's job at a time.
        # Any lane's checkpoint may probe, and probing mutates the monitor's
        # own idea of what it has loaded, so two of them at once would leave
        # half of one configuration and half of another.
        self._lock = threading.RLock()
        # Only the monitor thread may touch the `schedule` package's registry:
        # it is a module-level singleton with no locking of its own.  Lanes are
        # handed the work already chosen and never see a Job.
        self.lanes: Dict[str, BrowserLane] = {}
        # Whether the browsers were closed because there was nothing to do,
        # as opposed to being closed by a stop.  The difference decides whether
        # a review that comes due while nothing is running may open one:
        # after a stop it must not, and after an idle release it must.
        self._browsers_idle = False
        # Platform -> the browser its searches run on: a lane name, or "" for
        # the monitor's own.  Decided once and kept, so a platform cannot end
        # up driving whichever browser happened to be free; see
        # `_bind_platforms` for why that is a correctness matter and not
        # tidiness.  Filled in through the property of the same name.
        self.__dict__["_browser_of"] = {}
        # When re-checking stored listings happens, and how much of it happens
        # at a time.  Rebuilt from the configuration on every load.
        self.review_schedule: ReviewSchedule = ReviewSchedule()
        #: Epoch seconds of the next round; 0 before the first one is planned.
        self._review_due: float = 0.0
        self._review_thread: threading.Thread | None = None
        self._review_stop = threading.Event()
        # Cheap change detection: file stats, and when they were last taken.
        self._probe_at: float = 0.0
        self._probe_signature: Tuple[Tuple[float, int], ...] | None = None
        # Versions already complained about or already announced, so a file
        # left broken -- or a change that has to wait for the current search --
        # is said once rather than at every checkpoint.
        self._reported_bad_version: str | None = None
        self._announced_pending: str | None = None
        # Set when a configuration was adopted somewhere that may not touch the
        # `schedule` registry -- a checkpoint on a lane, or a checkpoint in the
        # middle of a search on this thread.  The monitor thread rebuilds at its
        # next safe moment.  Without it, a search created while another one runs
        # is loaded (and shown) but never scheduled until something else happens
        # to rebuild.
        self._schedule_dirty = False
        self.headless = headless
        # When True, start_monitor blocks until every enabled marketplace
        # has a username + password in the config. The web UI sets this
        # so Playwright doesn't race the web UI for Facebook credentials.
        self.defer_login_until_credentials: bool = False
        self.ai_agents: List[AIBackend] = []
        self.keyboard_monitor: KeyboardMonitor | None = None
        self.playwright: Playwright = sync_playwright().start()
        self.context: BrowserContext | None = None
        self.logger = logger
        # The refresher that shares the search's tab.  A second one, bound to
        # the review lane's own browser, is made by that lane when it starts:
        # a refresher belongs to one browser, so they cannot be one object.
        self.refresher: ListingRefresher | None = None
        # Sending happens here rather than on whichever thread found the
        # listing.  Telegram waits out its own rate limit and SMTP waits for a
        # handshake, and both of those on the scraping thread are a page left
        # open and a "detener" button that does not answer.  Set through the
        # property, so a monitor built with `__new__` gets one too.
        self.__dict__["notifier"] = NotificationDispatcher(logger=logger)

    # Both of these are created on demand rather than only in ``__init__``.
    # A monitor is not always built through it -- the reload tests stand one up
    # with ``__new__`` and fill in the handful of fields they exercise -- and a
    # lock that only exists when the constructor ran is a lock that turns a
    # missing test fixture into an AttributeError three frames away from the
    # cause.

    @property
    def _config_lock(self: "MarketplaceMonitor") -> threading.RLock:
        """Held while the configuration is probed or adopted."""
        lock = getattr(self, "_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._lock = lock
        return lock

    @property
    def _searching(self: "MarketplaceMonitor") -> Pair | None:
        """The pair this thread is searching, or None between searches."""
        state = getattr(self, "_thread_state", None)
        return None if state is None else getattr(state, "searching", None)

    @_searching.setter
    def _searching(self: "MarketplaceMonitor", pair: Pair | None) -> None:
        state = getattr(self, "_thread_state", None)
        if state is None:
            state = threading.local()
            self._thread_state = state
        state.searching = pair

    def load_config_file(self: "MarketplaceMonitor") -> Config:
        """Load the configuration file."""
        last_invalid_hash = None
        while True:
            new_file_hash = calculate_file_hash(self.config_files)
            config_changed = self.config_hash is None or new_file_hash != self.config_hash
            if not config_changed:
                assert self.config is not None
                return self.config
            try:
                # if the config file is ok, break
                assert self.logger is not None
                self.config = Config(self.config_files, self.logger)
                self.config_hash = new_file_hash
                # self.logger.debug(self.config)
                assert self.config is not None
                self._publish_config()
                # The probe compares against this, so a load it did not make
                # must not look to it like a change somebody else made.
                self._probe_signature = self._config_signature()
                self._announced_pending = None
                return self.config
            except KeyboardInterrupt:
                raise
            except Exception as e:
                control.set_phase("error", f"The configuration file cannot be read: {e}")
                if last_invalid_hash != new_file_hash:
                    last_invalid_hash = new_file_hash
                    if self.logger:
                        self.logger.error(
                            f"""{hilight("[Config]", "fail")} Error parsing:\n\n{hilight(str(e), "fail")}\n\nPlease fix the configuration and I will try again as soon as you are done."""
                        )
                doze(60, self.config_files, self.keyboard_monitor)
                continue

    # ------------------------------------------------------------------ #
    # Telling the web UI what is actually loaded and what is actually due
    # ------------------------------------------------------------------ #
    #
    # The interface must never have to infer either from the file on disk.
    # Saving a search and the scraper taking it up are two different events,
    # and only this thread knows when the second one happened -- so it is this
    # thread that says so, and :mod:`ai_marketplace_monitor.control` is where
    # it says it.

    def _publish_config(self: "MarketplaceMonitor") -> None:
        """Record the configuration this loop has actually taken up.

        The version is the hash of the files as they were read, which is what
        lets the web UI answer "has my change reached the scraper?" without
        guessing.  Anything that cannot be described is not worth crashing the
        monitor over, so a failure here is logged and dropped.
        """
        if self.config is None or self.config_hash is None:
            return
        try:
            snapshot = self.config.describe()
            self._loaded_snapshot = snapshot
            self._fingerprints = fingerprints(snapshot)
            control.set_loaded_config(self.config_hash, snapshot)
            # A search the user deleted must stop being reported as something
            # the scraper runs; its history goes with it.
            control.forget_searches(
                {(item_name, name) for (name, item_name) in self.config.items}
            )
            names = [
                name
                for name, marketplace_config in self.config.marketplace.items()
                if marketplace_config.enabled is not False
            ]
            # The review's own schedule is rebuilt from the file on every
            # load, so editing it in the web UI takes effect on the next round
            # rather than on the next restart.
            self.review_schedule = schedule_from_config(self.config.monitor)
            control.set_updates_config(
                enabled=bool(names),
                parallel=self._updates_run_in_parallel(),
                interval=self._recheck_interval(),
                marketplaces=names,
                batch=self.review_schedule.batch,
                schedule=self.review_schedule.describe(),
                lane=(
                    control.UPDATES_LANE
                    if self._updates_run_in_parallel()
                    else control.MAIN_LANE
                ),
            )
            # A schedule that has changed makes the round already planned under
            # the old one meaningless: plan the next one again from now.
            self._plan_next_review()
        except KeyboardInterrupt:
            raise
        except Exception as e:  # pragma: no cover - observability must not break the loop
            if self.logger:
                self.logger.debug(f"Could not publish the loaded configuration: {e}")

    def _publish_schedule(self: "MarketplaceMonitor") -> None:
        """Publish when each search is next due, as the scheduler has it now.

        Two details that look like presentation and are not.  The timestamps
        carry an offset: ``schedule`` keeps naive local times, and a browser
        reading ``20:30`` with no offset resolves it against *its* clock, which
        is how a next run comes out rendered as a past one.  And they are
        published per ``(item, marketplace)`` as well as per item, because that
        is the grain a row on the screen is: one product can be due on one
        platform and not on the other.
        """
        next_runs: Dict[str, str] = {}
        by_pair: Dict[Pair, str] = {}
        for job in schedule.get_jobs():
            if job.next_run is None:
                continue
            stamp = job.next_run.astimezone().isoformat(timespec="seconds")
            pair: Pair = getattr(job, "amm_pair", ("", ""))
            if pair[0]:
                # Several jobs can carry the same pair (an interval and a fixed
                # time of day); the soonest is the one that will fire.
                if pair not in by_pair or stamp < by_pair[pair]:
                    by_pair[pair] = stamp
            for tag in job.tags or []:
                name = str(tag)
                if name not in next_runs or stamp < next_runs[name]:
                    next_runs[name] = stamp
        control.set_next_runs(next_runs, by_pair)

    # ------------------------------------------------------------------ #
    # Taking up a configuration saved while the loop is running
    # ------------------------------------------------------------------ #
    #
    # The monitor used to notice a changed file only between searches and
    # while asleep, and to answer it by throwing the whole schedule away and
    # searching everything again from the top.  Both are wrong for the way the
    # web UI is used: a search deleted while it is running goes on running for
    # however long it takes to finish, and adding one search re-searches every
    # other one as a side effect.
    #
    # What follows is the same reload, made specific.  The file is watched at
    # every checkpoint the scraping code already stops at; what changed is
    # compared against what is loaded; and the answer depends on whether the
    # change touches the search under way.  It usually does not, and then the
    # search finishes untouched -- interrupting it would cost a page load and a
    # round of AI calls to gain nothing.

    def _config_signature(self: "MarketplaceMonitor") -> Tuple[Tuple[float, int], ...] | None:
        """A cheap stand-in for "the files have not been touched".

        Modification time and size, which is a ``stat`` per file and no read at
        all.  It can say "changed" when nothing did -- a save that rewrites the
        same bytes -- and the hash behind it settles those; what it must never
        do is say "unchanged" when something did, which is why size is in it.
        """
        try:
            stats = [path.stat() for path in self.config_files]
        except OSError:
            return None
        return tuple((stat.st_mtime, stat.st_size) for stat in stats)

    def _probe_config(self: "MarketplaceMonitor", force: bool = False) -> ConfigProbe | None:
        """Look at the configuration files.  None when there is nothing to say.

        ``force`` skips the throttle and the stat short-circuit, for the points
        between searches where the loop is going to reload anyway and a
        one-second-old answer is not good enough.

        A file that cannot be parsed comes back as ``changed`` with no
        configuration on it.  That is deliberately not an exception: the loop
        is in the middle of something, the file may simply be halfway through
        being written, and carrying on with the configuration already loaded is
        the right answer to both.
        """
        # Every lane's checkpoints come through here, and a probe rewrites the
        # monitor's own record of what it has seen.  One at a time.
        with self._config_lock:
            now = time.monotonic()
            if not force:
                if now - self._probe_at < self.CONFIG_PROBE_INTERVAL:
                    return None
                self._probe_at = now
                signature = self._config_signature()
                if signature is None or signature == self._probe_signature:
                    return None
                self._probe_signature = signature
            else:
                self._probe_at = now
                self._probe_signature = self._config_signature()

            try:
                version = calculate_file_hash(self.config_files)
            except OSError:
                # Gone or unreadable this instant.  Nothing to adopt, and the
                # file watcher will bring us back when it reappears.
                return None
            if version == self.config_hash:
                return None

            try:
                candidate = Config(self.config_files, None)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                if self._reported_bad_version != version:
                    self._reported_bad_version = version
                    if self.logger:
                        self.logger.warning(
                            f"""{hilight("[Config]", "fail")} The configuration changed but """
                            f"""cannot be read: {e}  Carrying on with the one already loaded.""",
                            extra=aimm_event("config_unreadable", error=str(e)),
                        )
                return ConfigProbe(changed=True, version=version, error=str(e))

            self._reported_bad_version = None
            change = diff_config(self._loaded_snapshot, candidate.describe())
            # The files differ -- the hashes said so -- but nothing the snapshot
            # renders does: a changed secret, most likely.  It is still a
            # change, and still not one to abandon a search over.
            return ConfigProbe(
                changed=True,
                config=candidate,
                version=version,
                change=change if change else ConfigChange(general=True),
            )

    def _adopt_config(
        self: "MarketplaceMonitor",
        probe: ConfigProbe,
        interrupted: Dict[str, str] | None = None,
        live: Dict[str, Any] | None = None,
    ) -> None:
        """Make the probed configuration the one the loop runs, and say so.

        Saying so is half the job.  "Saved" and "in use" are two different
        events, and only this thread knows when the second one happened -- so
        it is announced here, with what was in the change and what it cost, and
        the interface repeats it rather than inferring it from two hashes.
        """
        assert probe.config is not None and probe.version is not None
        change = probe.change or ConfigChange(general=True)
        with self._config_lock:
            self.config = probe.config
            self.config_hash = probe.version
            self._probe_signature = self._config_signature()
            self._announced_pending = None
            self._publish_config()
        control.set_config_applied(
            version=probe.version,
            change=change.to_dict(),
            interrupted=interrupted,
            live=live,
        )
        if self.logger:
            self.logger.info(
                f"""{hilight("[Config]", "succ")} New configuration in use: """
                f"""{hilight(change.summary())}."""
                + (
                    f""" The search for {interrupted["item"]} was dropped """
                    f"""({interrupted["reason"]})."""
                    if interrupted
                    else ""
                )
                + (
                    f""" Taken into the running search for {live["item"]}: """
                    f"""{", ".join(live["applied"]) or "nothing"}."""
                    + (
                        f""" Waiting for its next run: {", ".join(live["deferred"])}."""
                        if live.get("deferred")
                        else ""
                    )
                    if live
                    else ""
                ),
                extra=aimm_event(
                    "config_applied",
                    version=probe.version,
                    interrupted=interrupted,
                    **change.to_dict(),
                ),
            )
        # A tracker added in this change has no schedule entry to wait for and
        # no search that will ever pick it up: its first read is this, and it
        # happens now rather than at the end of whatever is running.
        self._ingest_trackers()

    def _refresh_config(self: "MarketplaceMonitor") -> bool:
        """Re-read the files and take up what is there.  True when it changed.

        The counterpart of :meth:`_config_guard` for the moments when nothing is
        running.  A checkpoint inside a search asks "has the file moved under
        me?"; this asks "what does the file say?", which is a different question
        and the one that has to be answered before the loop decides it has
        nothing to do.

        Both places that call it were the same bug.  A stopped monitor holds the
        configuration it was stopped with, and nothing between the "Iniciar"
        button and the schedule being rebuilt used to consult the file: the loop
        resumed on the old object, ``_configured_searches`` counted the old
        searches, and a search added while the monitor was stopped was invisible
        to it.  ``doze`` could not save it either -- it starts its file watcher
        when it is called, so a change that already happened is not a change it
        can ever see -- which is why the wait that followed lasted an hour and
        why stopping and starting again sometimes appeared to help and sometimes
        did not.  A start reads the configuration.  That is all this is.
        """
        probe = self._probe_config(force=True)
        if probe is None or not probe.readable:
            return False
        self._adopt_config(probe)
        self._schedule_dirty = True
        return True

    def _apply_changes_while_running(self: "MarketplaceMonitor") -> bool:
        """Whether an edit to the running search is taken into it, or ends it."""
        if self.config is None:
            return True
        return bool(getattr(self.config.monitor, "apply_changes_while_running", True))

    def _on_delete_running(self: "MarketplaceMonitor") -> str:
        """``"stop"`` or ``"finish"``: what deleting the running search does."""
        if self.config is None:
            return "stop"
        return str(getattr(self.config.monitor, "on_delete_running", "stop") or "stop")

    def _apply_live_config(
        self: "MarketplaceMonitor",
        candidate: Config,
        item: str,
        marketplace: str,
    ) -> Dict[str, Any]:
        """Copy the edited settings onto the objects the running search holds.

        This is the difference between "the file changed" and "the search is
        running differently", and it has to be done by mutation rather than by
        replacement: the generator inside ``Marketplace.search`` is holding
        *these* two dataclass instances, and rebinding the names in
        ``self.config`` would leave it reading the old ones to the end.

        What the running search can absorb is decided by when it reads a
        setting, not by how much we would like it to.  Filters consulted once
        per listing -- keywords, banned sellers, allowed locations, the rating
        threshold, the AI prompts, who gets notified -- take effect on the very
        next listing.  Anything in :data:`URL_BOUND_OPTIONS` was spent building
        the request the search is now paging through, so changing it here would
        set a field nobody will read again; those are reported as waiting for
        the next run instead of counted as applied.  See the module note in
        ``docs/webui.md``.

        Returns ``{"applied": [...], "deferred": [...]}``, both field names.
        """
        applied: List[str] = []
        deferred: List[str] = []

        def merge(current: Any, incoming: Any) -> None:
            if current is None or incoming is None or type(current) is not type(incoming):
                return
            for field in dataclass_fields(current):
                key = field.name
                if key in VOLATILE_OPTIONS or key in ("name", "monitor_config"):
                    # `searched_count` and friends are the loop's own tally, not
                    # the user's intent: copying them back would undo the count
                    # this very search is in the middle of making.
                    continue
                was = getattr(current, key, None)
                now = getattr(incoming, key, None)
                if was == now:
                    continue
                if key in self.URL_BOUND_OPTIONS:
                    deferred.append(key)
                    continue
                setattr(current, key, now)
                applied.append(key)

        merge(self.config.items.get((marketplace, item)) if self.config else None,
              candidate.items.get((marketplace, item)))
        merge(self.config.marketplace.get(marketplace) if self.config else None,
              candidate.marketplace.get(marketplace))
        return {
            "item": item,
            "marketplace": marketplace,
            "applied": sorted(set(applied)),
            "deferred": sorted(set(deferred)),
        }

    def _config_guard(self: "MarketplaceMonitor") -> None:
        """Checkpoint: has the configuration moved, and what does that mean here?

        Installed into :mod:`control`, so every place that already stops to ask
        "was I cancelled?" asks this too, and the scraping code needs to know
        nothing about it.

        Three questions, in order:

        1. **Was this search told to end?**  Somebody pressed "stop this search"
           or "stop this platform" in the web UI.  Nothing is wrong and nothing
           else is affected: the search ends here and the loop takes the next.
        2. **Did the file change?**  If so it is adopted *now*, mid-search,
           whether or not it touches this search -- which is what makes a search
           created while another one runs appear in the interface at once
           instead of whenever the current one happens to finish.  The
           ``schedule`` registry is not touched from here (a lane may be the
           one asking); the monitor thread is told to rebuild it.
        3. **Does the change touch this search?**  By default the new settings
           are taken into the search under way and it carries on -- a maximum
           price the user just lowered is not an instruction to abandon a page
           already loaded.  Deleting it is the exception, and switching it off
           is the other: neither leaves anything worth finishing.
        """
        pair = self._searching
        if pair is None:
            return
        item, marketplace = pair

        stop = control.stop_requested(item, marketplace)
        if stop is not None:
            # A platform-level stop is spent by the search it stopped.  A
            # product-level one is not: it still has to reach the platforms of
            # the same product that have not started yet, and the monitor drops
            # it when the pass is over.
            if stop.get("marketplace"):
                control.clear_search_stop(item, str(stop["marketplace"]))
            raise SearchStopped(
                item=item, marketplace=marketplace, scope=str(stop.get("scope") or "search")
            )

        probe = self._probe_config()
        if probe is None or not probe.changed or not probe.readable:
            return
        assert probe.change is not None and probe.config is not None
        reason = probe.change.affects(item, marketplace)

        if reason is None:
            # Nothing to do with this search.  Take it up anyway rather than
            # sitting on it: everything else the user can see -- the list of
            # searches, the resolved configuration, the sync state -- is behind
            # until we do, and none of that is worth making them wait for.
            self._adopt_config(probe)
            self._schedule_dirty = True
            return

        if reason == MODIFIED or reason == MARKETPLACE:
            if self._apply_changes_while_running():
                live = self._apply_live_config(probe.config, item, marketplace)
                self._adopt_config(probe, live=live)
                self._schedule_dirty = True
                if self.logger:
                    self.logger.info(
                        f"""{hilight("[Config]", "succ")} The running search for """
                        f"""{hilight(item)} on {hilight(marketplace)} took up the change """
                        """and is carrying on"""
                        + (
                            f""" ({", ".join(live["deferred"])} apply from its next run)."""
                            if live["deferred"]
                            else "."
                        ),
                        extra=aimm_event(
                            "config_live_applied",
                            item=item,
                            marketplace=marketplace,
                            applied=live["applied"],
                            deferred=live["deferred"],
                        ),
                    )
                return
        elif reason == REMOVED and self._on_delete_running() == "finish":
            # The user asked for the opposite of the default: let a search that
            # is nearly done finish and notify, and forget it afterwards.
            self._adopt_config(probe)
            self._schedule_dirty = True
            if self.logger:
                self.logger.info(
                    f"""{hilight("[Config]", "info")} The search for {hilight(item)} was """
                    """deleted; it is being allowed to finish first, as configured.""",
                    extra=aimm_event("config_delete_finishing", item=item,
                                     marketplace=marketplace),
                )
            return

        self._adopt_config(
            probe, interrupted={"item": item, "marketplace": marketplace, "reason": reason}
        )
        self._schedule_dirty = True
        raise SearchSuperseded(item=item, marketplace=marketplace, reason=reason)

    def _rebuild_schedule(self: "MarketplaceMonitor") -> None:
        """Throw the schedule away and build it from the configuration in hand."""
        schedule.clear()
        self.schedule_jobs()
        self._publish_schedule()

    # ------------------------------------------------------------------ #
    # When each search last ran, across restarts
    # ------------------------------------------------------------------ #
    #
    # ``schedule`` starts every job's clock at the moment it is created, so a
    # rebuilt schedule -- which happens on every configuration change, and on
    # every start -- used to hand all of them a fresh interval.  Two visible
    # consequences: pressing "Iniciar" searched everything at once whatever the
    # intervals said, and editing one search quietly reset the timer of all the
    # others.  Both are fixed by remembering the one fact ``schedule`` cannot:
    # when the pair actually last ran.

    def _remember_run(
        self: "MarketplaceMonitor", item: str, marketplace: str, when: float | None = None
    ) -> None:
        """Record that this pair has just been searched.  Never fatal."""
        try:
            cache.set(
                (CacheType.SEARCH_RUNS.value, marketplace, item),
                float(time.time() if when is None else when),
                tag=CacheType.SEARCH_RUNS.value,
            )
        except KeyboardInterrupt:
            raise
        except Exception:  # pragma: no cover - a cache write must not stop a search
            if self.logger:
                self.logger.debug("Could not record the run time of a search", exc_info=True)

    def _remembered_run(self: "MarketplaceMonitor", item: str, marketplace: str) -> float | None:
        try:
            value = cache.get((CacheType.SEARCH_RUNS.value, marketplace, item))
        except KeyboardInterrupt:
            raise
        except Exception:  # pragma: no cover
            return None
        return float(value) if isinstance(value, (int, float)) else None

    def _seed_job_from_memory(
        self: "MarketplaceMonitor", job: schedule.Job, pair: Pair
    ) -> None:
        """Start an interval job from its last real run rather than from now.

        Only interval jobs: a job pinned to a time of day already knows when it
        next comes round, and its answer does not depend on when it was built.

        A pair that has never run is due immediately: there is no interval to
        wait out when nothing has happened yet, and a brand new search that sat
        idle for its first half hour would look broken.  A pair whose interval
        has already elapsed is due too, and one whose interval has not is not --
        which together are the whole of what "Iniciar" is supposed to respect.
        """
        if getattr(job, "at_time", None) is not None:
            return
        last = self._remembered_run(pair[0], pair[1])
        try:
            if last is None:
                job.next_run = datetime.now()
                return
            # The gap `schedule` just chose for this round, taken as the
            # difference it produced rather than read off the job.  A random
            # interval is drawn inside `_schedule_next_run` and this version of
            # the library keeps the draw in a local, so subtracting is the only
            # way to learn which number it picked -- and picking our own would
            # quietly turn a random schedule into a fixed one.
            job._schedule_next_run()
            drawn = job.next_run - datetime.now()
            job.last_run = datetime.fromtimestamp(last)
            job.next_run = job.last_run + drawn
        except KeyboardInterrupt:
            raise
        except Exception:  # pragma: no cover - private API, guarded
            if self.logger:
                self.logger.debug("Could not restore a job's schedule", exc_info=True)

    def _job_key(self: "MarketplaceMonitor", job: schedule.Job) -> Tuple[Any, ...]:
        """What makes one job the same job across a reload.

        The pair it searches, which of that pair's slots it is, and a
        fingerprint of the search itself.  The fingerprint is the point: a
        search that survived a reload untouched keeps its key and its place in
        the queue, and one the user edited comes back with a different key and
        is searched again under its new settings.
        """
        pair: Pair = getattr(job, "amm_pair", ("", ""))
        return (pair, getattr(job, "amm_slot", 0), self._fingerprints.get(pair, ""))

    @staticmethod
    def _launch_options(browser_name: str) -> dict:
        """Per-engine launch flags that keep this browser from announcing itself.

        Playwright's Chromium advertises itself as automated: it sets
        ``--enable-automation``, which makes ``navigator.webdriver`` true.  Sites
        that read that flag can bounce an ordinary interactive login into a
        challenge loop -- the CAPTCHA is answered correctly and the login page
        just comes back, because what was rejected is the browser, not the
        answer.

        ``--enable-automation`` was never the only tell, though.  Playwright
        starts Chromium with thirty-five flags a person's Chrome never carries,
        and several are readable from the page one way or another.
        :data:`TELLTALE_DEFAULT_ARGS` is the subset dropped and, just as
        importantly, it is a subset: the rest are load bearing.

        ``channel`` is not decided here -- see :meth:`_browser_channel`.

        Chromium-only: Firefox and WebKit reject these arguments.
        """
        if browser_name != "chromium":
            return {}
        return {
            "args": [
                "--disable-blink-features=AutomationControlled",
                SOFTWARE_WEBGL_FLAG,
            ],
            "ignore_default_args": ["--enable-automation", *TELLTALE_DEFAULT_ARGS],
            # The window is a window, not a fixed viewport.  A browser whose
            # inner size never matches the screen it claims to be on is one more
            # thing that does not add up.
            "no_viewport": True,
        }

    def _browser_channel(self: "MarketplaceMonitor", browser_name: str) -> str | None:
        """Real Chrome when this machine has it, otherwise Playwright's Chromium.

        Not the same browser, and the differences are all visible from a page:
        Chromium ships without the proprietary codecs, without Widevine, with
        different ``navigator.userAgentData`` brands and a different build
        string.  None of that matters to the scraping and all of it is free
        surface for whoever is deciding whether we are a person.

        ``None`` -- Playwright's own build -- is a fallback and not a failure:
        the container has no Chrome in it, and this must not be the reason it
        stops starting.
        """
        if browser_name != "chromium":
            return None
        return "chrome" if chrome_is_installed() else None

    def _proxy_for_launch(self: "MarketplaceMonitor") -> Any:
        """The proxy to bind to the persistent profile, if one is configured.

        A persistent profile fixes its proxy at launch, so a rotating list can no
        longer be sampled per page.  Say so rather than silently honouring only
        the first entry.
        """
        monitor_config = getattr(self.config, "monitor", None) if self.config else None
        if monitor_config is None:
            return None
        servers = getattr(monitor_config, "proxy_server", None)
        if isinstance(servers, list) and len(servers) > 1 and self.logger:
            self.logger.warning(
                f"""{hilight("[Browser]", "fail")} {len(servers)} proxies configured, but a """
                """persistent browser profile binds one proxy for its whole lifetime. """
                """Using the first; rotation is not applied."""
            )
        return monitor_config.get_proxy_options()

    def _hide_headless_marker(self: "MarketplaceMonitor", context: BrowserContext) -> None:
        """Drop "HeadlessChrome" from the user agent when running headless.

        Chromium puts it in both the JS value and the HTTP header, which is an
        outright declaration of automation -- enough on its own to put a login
        back into the challenge loop the rest of this setup exists to avoid.  So
        a profile that signed in fine with a visible window would start failing
        the moment the monitor was switched to ``--headless``.
        """
        try:
            page = context.pages[0] if context.pages else context.new_page()
            agent = page.evaluate("() => navigator.userAgent")
            if "Headless" not in agent:
                return
            cleaned = agent.replace("HeadlessChrome", "Chrome")
            # The header is what the server sees; the init script keeps the
            # value scripts on the page read consistent with it.
            context.set_extra_http_headers({"User-Agent": cleaned})
            context.add_init_script(
                "Object.defineProperty(navigator, 'userAgent', "
                f"{{get: () => {json.dumps(cleaned)}}});"
            )
        except Exception:
            if self.logger:
                self.logger.debug("Could not adjust the headless user agent", exc_info=True)

    def _seed_profile_sessions(
        self: "MarketplaceMonitor",
        context: BrowserContext,
        lane: str | None,
        first_run: bool,
    ) -> None:
        """Put the stored sessions this profile still owes into it, at launch.

        Two jobs that used to be one, and the second one was missing.

        *On a profile the browser has never written*, every stored session is
        seeded.  That is the upgrade path from the versions that kept cookies
        instead of a profile: without it, upgrading silently threw away a
        working session and demanded a fresh login.

        *On every launch, new profile or not*, any session the user **imported**
        and that **this** profile has not taken is seeded as well.  A lane is a
        second browser on a profile of its own, and an established profile was
        never re-seeded from disk -- so an import that arrived while the lane's
        browser was closed (which is most of the time: the monitor releases idle
        browsers) reached the monitor's browser, was marked applied, and never
        went anywhere near the browser that actually searches that platform.
        That is what "importar la sesión de Lider/Sodimac no hace nada" was.
        """
        if self.config is None:
            return
        for marketplace_config in self.config.marketplace.values():
            name = marketplace_config.name
            imported = import_is_pending(name, lane)
            if not first_run and not imported:
                continue
            state = load_session(name)
            if not state or not state.get("cookies"):
                continue
            try:
                context.add_cookies(state["cookies"])
            except Exception:
                if self.logger:
                    self.logger.debug(
                        f"Could not seed the {name} session into "
                        f"{'the ' + lane + ' lane' if lane else 'the main profile'}",
                        exc_info=True,
                    )
                continue
            if imported:
                # Recorded per profile, so this browser is not asked again and
                # the others still are.
                mark_import_applied(name, lane)
            if self.logger:
                self.logger.info(
                    f"""{hilight("[Login]", "succ")} Loaded the """
                    f"""{"imported " if imported else "saved "}{hilight(name)} session into """
                    f"""the {lane + " lane's" if lane else "main"} browser profile """
                    f"""({len(state["cookies"])} cookies).""",
                    extra=aimm_event(
                        "session_seeded",
                        marketplace=name,
                        lane=lane,
                        imported=imported,
                        cookies=len(state["cookies"]),
                    ),
                )

    def _launch_context(
        self: "MarketplaceMonitor",
        playwright: Playwright | None = None,
        lane: str | None = None,
    ) -> BrowserContext:
        """Open the browser on a persistent on-disk profile.

        A profile directory rather than a throwaway browser: sites that decide
        whether to challenge a login partly on whether they recognize the browser
        will re-challenge forever against a fresh install every run.  The profile
        makes the second run the same browser coming back.

        ``lane`` names a *second* browser, running beside the first one.  It
        gets a profile directory of its own because Chromium holds its profile
        exclusively -- two windows cannot share one -- and that new profile is
        seeded from the stored sessions, so it opens already signed in rather
        than demanding a second login.  ``playwright`` is the lane's own
        instance: Playwright's synchronous objects belong to the thread that
        made them, which is the reason lanes exist at all.
        """
        engine = playwright or self.playwright
        user_data_dir = str(profile_dir(lane))
        first_run = profile_is_new(lane)
        proxy = self._proxy_for_launch()

        # Before anything is launched, because a profile still claimed by a
        # browser that no longer exists is not a browser that starts slowly --
        # it is one that refuses outright, for ever, and takes the whole monitor
        # down with it on every start.  See `release_stale_profile_lock`.
        stale = release_stale_profile_lock(lane)
        if stale and self.logger:
            self.logger.warning(
                f"""{hilight("[Browser]", "info")} The """
                f"""{f"{lane} " if lane else ""}browser profile was still claimed by """
                f"""{stale}; releasing it.""",
                extra=aimm_event("profile_lock_released", lane=lane, owner=stale),
            )
        # Kept so the failure below can say what actually went wrong.  Every
        # launch error used to go to the debug log, which is off by default, and
        # what the user was left with was "no browser could be launched" and no
        # way to find out why -- while the real answer, "the profile appears to
        # be in use by another Chromium process", was one line further down.
        failures: List[str] = []

        # Try browsers in order of preference
        browser_types = [
            ("chromium", engine.chromium),
            ("firefox", engine.firefox),
            ("webkit", engine.webkit),
        ]

        for browser_name, browser_type in browser_types:
            try:
                if self.logger:
                    self.logger.debug(f"Attempting to launch {browser_name} browser...")
                channel = self._browser_channel(browser_name)
                context = browser_type.launch_persistent_context(
                    user_data_dir,
                    headless=self.headless,
                    proxy=proxy,
                    **({"channel": channel} if channel else {}),
                    **self._launch_options(browser_name),
                )
                if not PATCHES_CDP:
                    # Chromium leaves navigator.webdriver defined even with the
                    # automation flag off; clear it before any page script runs.
                    #
                    # Skipped under a driver that already handles it: the
                    # override is then redundant, and an injected init script is
                    # itself something a page can notice -- which is the whole
                    # reason for using such a driver.
                    try:
                        context.add_init_script(
                            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                        )
                    except Exception:
                        # Not supported on every engine; the launch flag already
                        # covers the common case.
                        pass
                if self.headless and not PATCHES_CDP:
                    # patchright asks callers not to set a user agent or extra
                    # headers, and this does both.  A forged UA that disagrees
                    # with the rest of the browser is worse than the marker it
                    # hides.
                    self._hide_headless_marker(context)
                self._seed_profile_sessions(context, lane, first_run)
                if self.logger:
                    self.logger.info(
                        f"""{hilight("[Browser]", "info")} Successfully launched """
                        f"""{"Google Chrome" if channel == "chrome" else browser_name} """
                        f"""via {ENGINE_NAME}"""
                        f"""{f" for the {lane} lane" if lane else ""}"""
                        f""" ({"new" if first_run else "existing"} profile).""",
                        extra=aimm_event(
                            "browser_ready",
                            engine=browser_name,
                            channel=channel or "bundled",
                            driver=ENGINE_NAME,
                            new_profile=first_run,
                            lane=lane,
                        ),
                    )
                return context
            except Exception as e:
                # First line only: Playwright's message carries the browser's
                # whole command line and its stderr, which is what the debug log
                # is for.  The first line is the sentence that names the cause.
                first = str(e).strip().splitlines()[0] if str(e).strip() else e.__class__.__name__
                failures.append(f"{browser_name}: {first}")
                if self.logger:
                    self.logger.debug(f"Failed to launch {browser_name}: {e}", exc_info=True)
                continue

        # If all fail, raise an error -- carrying the reason.  A message that
        # says only "install a browser" sends the reader looking for a missing
        # package when the browser is installed and something else is wrong.
        raise RuntimeError(
            "No browser could be launched"
            + (f" ({'; '.join(failures)})" if failures else "")
            + ". Please ensure Chromium, Firefox, or WebKit is installed."
        )

    # ------------------------------------------------------------------ #
    # Lanes: a second browser, on a thread of its own
    # ------------------------------------------------------------------ #
    #
    # See :mod:`ai_marketplace_monitor.lanes` for why running two things at
    # once needs two browsers rather than two tabs.  Everything here is about
    # keeping the *decisions* on the monitor thread: a lane is handed work that
    # has already been chosen, and reports back through the same process-wide
    # flags every other checkpoint reads.

    def _lane(self: "MarketplaceMonitor", name: str) -> BrowserLane:
        """The lane called ``name``, created (but not started) on first ask."""
        lane = self.lanes.get(name)
        if lane is None:
            lane = BrowserLane(name, launch=self._launch_context, logger=self.logger)
            self.lanes[name] = lane
        return lane

    # ------------------------------------------------------------------ #
    # Which browser a platform searches on
    # ------------------------------------------------------------------ #
    #
    # Decided once per platform and then kept, which it was not before, and
    # that is a bug rather than an inefficiency.  The rule used to be "the
    # first platform in this pass runs on the monitor's own browser, the rest
    # get lanes", and the pass was built from whatever happened to be *due at
    # that instant*.  So a platform that ran on its own lane at 14:00 -- because
    # another platform was due at the same time -- ran on the monitor's browser
    # at 14:20, because by then it was the only one due and was therefore
    # "first".  From the outside that is a search inheriting the browser
    # another platform had been using, which is exactly what was reported.
    #
    # The binding also has to survive a platform being absent from a pass, so
    # it lives on the monitor rather than being recomputed, and is pruned only
    # when the platform stops being configured at all.

    @property
    def notifier(self: "MarketplaceMonitor") -> NotificationDispatcher:
        """The thread that sends notifications, made on first ask.

        Created on demand for the reason ``_config_lock`` is: a monitor is not
        always built through ``__init__``, and a dispatcher that only exists
        when the constructor ran turns a missing test fixture into an
        AttributeError three frames from the cause.
        """
        sender = self.__dict__.get("notifier")
        if sender is None:
            sender = NotificationDispatcher(logger=getattr(self, "logger", None))
            self.__dict__["notifier"] = sender
        return sender

    @notifier.setter
    def notifier(self: "MarketplaceMonitor", sender: NotificationDispatcher) -> None:
        """Assignable, because a property that is not is a property that breaks
        every caller that used to write to the attribute."""
        self.__dict__["notifier"] = sender

    @property
    def _browser_of(self: "MarketplaceMonitor") -> Dict[str, str]:
        """Platform -> the browser its searches run on; "" is the monitor's own.

        Created on demand for the same reason ``_config_lock`` is: a monitor is
        not always built through ``__init__`` -- the pass tests stand one up
        with ``__new__`` and fill in the handful of fields they exercise -- and
        a mapping that only exists when the constructor ran turns a missing
        test fixture into an AttributeError three frames from the cause.
        """
        bound = self.__dict__.get("_browser_of")
        if bound is None:
            bound = {}
            self.__dict__["_browser_of"] = bound
        return bound

    def _bind_platforms(self: "MarketplaceMonitor", names: List[str]) -> None:
        """Give every configured platform a browser, and keep it.

        One platform runs on the monitor's own browser and thread -- the empty
        string here -- and it matters which: that profile is the one an
        interactive ``--login`` signed in, and a lane's profile is seeded from
        stored sessions only.  So the first platform ever bound keeps it, and
        every later one gets a lane of its own named after it.
        """
        bound = self._browser_of
        for name in [name for name in bound if name not in names]:
            del bound[name]
        for name in names:
            if name in bound:
                continue
            bound[name] = "" if not any(lane == "" for lane in bound.values()) else name

    def _main_platform(self: "MarketplaceMonitor") -> str | None:
        """The platform that runs on the monitor's own browser, if any is bound."""
        return next((name for name, lane in self._browser_of.items() if lane == ""), None)

    def _close_lanes(self: "MarketplaceMonitor") -> None:
        """Let every lane finish what it holds and release its browser.

        Each lane closes its own browser on its own thread, which is the only
        thread allowed to: this method asks, it does not close.
        """
        for lane in list(self.lanes.values()):
            try:
                lane.close()
            except Exception:
                if self.logger:
                    self.logger.debug(f"Could not close lane {lane.name!r}", exc_info=True)
        self.lanes.clear()

    def _lane_marketplace(
        self: "MarketplaceMonitor",
        lane: BrowserLane,
        context: BrowserContext,
        marketplace_config: TMarketplaceConfig,
    ) -> Marketplace:
        """The marketplace object for one platform on one lane's browser.

        Held by the lane rather than in :attr:`active_marketplaces`, because a
        marketplace owns a page and a page belongs to exactly one browser.  The
        two must never be mixed up: handing the monitor's instance to a lane
        would have that lane driving the monitor's tab from another thread,
        which is the failure this whole design exists to avoid.
        """
        marketplace = lane.marketplaces.get(marketplace_config.name)
        if marketplace is None:
            marketplace_class = all_marketplaces[
                (marketplace_config.market_type or "facebook").lower()
            ]
            marketplace = marketplace_class(
                marketplace_config.name, context, self.keyboard_monitor, self.logger
            )
            # Recovery runs on this lane's thread, which is where its searches
            # run, so the lane can both close and open its own browser inline.
            marketplace.renew_browser = lane.renew_context
            lane.marketplaces[marketplace_config.name] = marketplace
        elif marketplace.context is not context:
            # Only when the browser behind it has actually been replaced.
            # `set_context` drops the page it holds, so calling it every time
            # would open a fresh tab for every single search and leave the old
            # one behind.
            marketplace.set_context(context)
        self._prepare_tracked(marketplace)
        marketplace.configure(
            marketplace_config,
            translator=self._select_translator(marketplace_config.language),
        )
        return marketplace

    def _ensure_browser(self: "MarketplaceMonitor") -> BrowserContext:
        """The browser, launched if it is missing or no longer real.

        Two ways it can be missing, and only the first was handled.  A forced
        pause tears the browser down rather than leaving a window and a Chromium
        process idling for however long the pause lasts, and the next search
        brings it back on the same persistent profile.  The other is the user
        closing the window themselves -- or Chromium falling over -- which left
        ``self.context`` pointing at a browser that no longer exists, and the
        monitor went on reporting that it was searching while there was nothing
        to search with.  :func:`context_is_alive` is what tells the two apart.
        """
        if self.context is not None and not context_is_alive(self.context):
            if self.logger:
                self.logger.warning(
                    f"""{hilight("[Browser]", "fail")} The browser is gone -- closed by hand, """
                    """or it crashed. Opening a new one.""",
                    extra=aimm_event("browser_lost", lane=None),
                )
            # Let go of the pages hanging off it before the reference goes, or
            # the marketplaces keep handing work to a dead tab.
            self._close_browser()
        if self.context is None:
            self.context = self._launch_context()
            for marketplace in self.active_marketplaces.values():
                marketplace.set_context(self.context)
            # A profile that already exists is never re-seeded from disk, so an
            # imported session would otherwise never reach a browser that was
            # started after the import.
            self._seed_imported_sessions()
        self._browsers_idle = False
        self._report_browser()
        return self.context

    def _report_browser(self: "MarketplaceMonitor") -> None:
        """Publish the monitor's own browser and its tab count.

        The counterpart of :meth:`BrowserLane._report`, and on this thread for
        the same reason: ``context.pages`` reaches the Playwright driver, and
        the driver belongs to the thread that started it, so the web UI is told
        the number rather than allowed to ask for it.
        """
        try:
            if context_is_alive(self.context):
                assert self.context is not None
                control.report_browser(
                    control.MAIN_LANE, len(self.context.pages), "browser-profile-main"
                )
            else:
                control.forget_browser(control.MAIN_LANE)
        except KeyboardInterrupt:
            raise
        except Exception:  # pragma: no cover - reporting must not break a search
            control.forget_browser(control.MAIN_LANE)

    def _renew_main_browser(self: "MarketplaceMonitor") -> BrowserContext:
        """Throw the monitor's own browser away, profile and all, and reopen it.

        The counterpart of :meth:`BrowserLane.renew_context` for the platform
        that runs on this thread.  Called from inside a search, which is the one
        place it is safe: this thread owns the browser it is about to close.

        Every marketplace on this browser is pointed at the new context, not
        only the one that asked.  They share it, and leaving the others holding
        a closed browser would turn one shop's recovery into the next platform's
        failure.
        """
        if self.context is not None:
            try:
                self.context.close()
            except Exception:
                if self.logger:
                    self.logger.debug("Could not close the browser being renewed", exc_info=True)
            self.context = None
        reset_profile(None)
        self.context = self._launch_context()
        for marketplace in self.active_marketplaces.values():
            marketplace.set_context(self.context)
        self._browsers_idle = False
        self._report_browser()
        return self.context

    def _release_idle_browsers(self: "MarketplaceMonitor", idle_seconds: float) -> None:
        """Close the browsers while there is nothing for them to do.

        The monitor used to hold every window it had ever opened for as long as
        the process lived: a search every half hour meant a Chromium (or two,
        with parallel platforms) sitting on several hundred megabytes and a
        visible window for twenty-nine minutes out of every thirty.  Closing
        one by hand then left the monitor's own idea of the world wrong -- it
        went on reporting that it was searching -- which is the other half of
        this, handled in :meth:`_ensure_browser` and :meth:`BrowserLane._live_context`.

        Only for a gap long enough to be worth it, and never while anything is
        actually running.  The review lane is left alone on purpose: when
        reviews have a browser of their own they are *using* it right now, and
        the gap between searches is exactly when they get the most done.
        """
        if idle_seconds < IDLE_BROWSER_RELEASE:
            return
        if self.context is None and not any(
            lane.alive for name, lane in self.lanes.items() if name != control.UPDATES_LANE
        ):
            return
        if control.is_running() or control.cancel_requested() or self.notifier.pending:
            return
        if self.logger:
            self.logger.info(
                f"""{hilight("[Browser]", "info")} Nothing to search for """
                f"""{humanize.naturaldelta(idle_seconds)}; closing the browsers until then.""",
                extra=aimm_event("browser_released", idle_seconds=int(idle_seconds)),
            )
        self._close_browser()
        for name in [name for name in self.lanes if name != control.UPDATES_LANE]:
            lane = self.lanes.pop(name)
            try:
                lane.close()
            except Exception:
                if self.logger:
                    self.logger.debug(f"Could not close lane {name!r}", exc_info=True)
        # Said explicitly, because "there is no browser" has two meanings and
        # only one of them may be answered by opening a new one.
        self._browsers_idle = True

    def _close_browser(self: "MarketplaceMonitor") -> None:
        """Let go of every page, tab and process the scrape was holding.

        Called from the scraping thread only.  Playwright's sync objects belong
        to the thread that made them, so the web UI can ask for a stop but can
        never do this itself -- which is the whole reason cancellation is a flag
        the loop reads rather than a close() from the request handler.
        """
        control.forget_browser(control.MAIN_LANE)
        for marketplace in self.active_marketplaces.values():
            try:
                marketplace.stop()
            except Exception:
                if self.logger:
                    self.logger.debug("Could not stop a marketplace cleanly", exc_info=True)
        if self.context is not None:
            try:
                self.context.close()
            except Exception:
                if self.logger:
                    self.logger.debug("Could not close the browser context", exc_info=True)
            self.context = None

    def _seed_lanes_with_session(
        self: "MarketplaceMonitor", name: str, cookies: List[Dict[str, Any]]
    ) -> None:
        """Put the same cookies into every browser that is not the main one.

        A lane is a second browser on a profile of its own -- that is what makes
        parallel searching possible at all -- and an import that reached only
        the main context therefore missed the browser that does the searching
        for every platform running in parallel.  Sodimac is the case that showed
        it: it runs on ``browser-profile-sodimac``, so importing a Sodimac
        session did nothing at all to the browser that visits Sodimac.

        Handed to the lane rather than applied here, because a Playwright object
        belongs to the thread that made it.  Queued and not waited on: this runs
        between jobs on the monitor thread, and a lane in the middle of a search
        should take the cookies when it comes up for air, not hold the monitor.
        """
        for lane in list(self.lanes.values()):
            if not lane.alive:
                # Nothing to do and no thread to do it on.  The lane takes the
                # same session from disk when its browser opens -- see
                # `_seed_profile_sessions`, which asks per profile, so a lane
                # whose profile already exists is still owed it.
                continue
            try:
                lane.submit(
                    lambda context, lane_name=lane.name: self._take_session(
                        context, name, cookies, lane_name
                    )
                )
            except KeyboardInterrupt:
                raise
            except Exception:
                if self.logger:
                    self.logger.debug(
                        f"Could not hand the {name} session to lane {lane.name!r}",
                        exc_info=True,
                    )

    @staticmethod
    def _take_session(
        context: BrowserContext, name: str, cookies: List[Dict[str, Any]], lane: str | None
    ) -> None:
        """Add the cookies and record that this profile now has them.

        Runs on the lane's own thread, which is the only one allowed to touch
        that browser.  The record is written here rather than by the caller
        because the caller does not wait for the answer: marking it applied
        before the lane had taken it would leave the lane owed a session that
        nothing would ever offer it again.
        """
        context.add_cookies(cookies)
        mark_import_applied(name, lane)

    def _load_session_into_browser(self: "MarketplaceMonitor", name: str) -> bool:
        """Put an imported session into every live browser.  False if it did not.

        The cookies are added to the persistent context, which is the same thing
        a sign-in would have produced, and the marketplace's cooldown is lifted:
        a new session is exactly the reason to give a site that refused us
        another go.
        """
        if self.context is None:
            return False
        state = load_session(name)
        cookies = (state or {}).get("cookies") or []
        if not cookies:
            return False
        try:
            self.context.add_cookies(cookies)
            self._seed_lanes_with_session(name, cookies)
        except KeyboardInterrupt:
            raise
        except Exception as error:
            if self.logger:
                self.logger.error(
                    f"""{hilight("[Login]", "fail")} Could not load the imported {name} """
                    f"""session into the browser: {error}""",
                    extra=aimm_event("session_imported", marketplace=name, ok=False),
                )
            return False

        # The monitor's own profile, and only it: every lane records its own.
        mark_import_applied(name, None)
        control.clear_marketplace_block(name)
        if self.logger:
            self.logger.info(
                f"""{hilight("[Login]", "succ")} Loaded the imported session for """
                f"""{hilight(name)} ({len(cookies)} cookies).""",
                extra=aimm_event("session_imported", marketplace=name, cookies=len(cookies), ok=True),
            )
        self._report_session_health(name)
        return True

    def _report_session_health(self: "MarketplaceMonitor", name: str) -> None:
        """Say whether the cookies actually produced a signed-in session.

        Costs one page load, and it is worth it exactly here: an import that
        silently fails to log in is indistinguishable from one that worked until
        the next search quietly comes back empty, which is a terrible way to
        find out.  Marketplaces that cannot answer the question keep quiet.

        **Asked of the browser that will do the searching**, which is not always
        this one.  A platform bound to a lane is searched by the lane's browser
        on the lane's profile, and checking the monitor's browser instead
        answered a question nobody asked: the reassuring "Sodimac is serving its
        pages" in the log was about a profile that never visits Sodimac.
        """
        lane_name = self._browser_of.get(name)
        if lane_name:
            self._check_session_on_lane(name, lane_name)
            return
        marketplace = self.active_marketplaces.get(name)
        if marketplace is None:
            return
        # A shop answers a different question, and it is the one that matters
        # for a shop: a catalogue is public, so "am I signed in" is not the
        # thing an import can fail.  See `RetailerMarketplace.session_health`.
        health = getattr(marketplace, "session_health", None)
        if health is not None:
            self._report_shop_session(name, health)
            return
        check = getattr(marketplace, "is_signed_in", None)
        if check is None:
            return
        try:
            signed_in = bool(check())
        except KeyboardInterrupt:
            raise
        except Exception:
            return
        self._report_signed_in(name, signed_in, lane=None)

    def _report_signed_in(
        self: "MarketplaceMonitor", name: str, signed_in: bool, lane: str | None
    ) -> None:
        """Say what a marketplace made of the cookies, and in which browser."""
        if not self.logger:
            return
        where = f""" (navegador de {lane})""" if lane else ""
        if signed_in:
            self.logger.info(
                f"""{hilight("[Login]", "succ")} {hilight(name)} accepts the imported """
                f"""session — signed in{where}.""",
                extra=aimm_event(
                    "session_checked", marketplace=name, signed_in=True, lane=lane
                ),
            )
        else:
            self.logger.error(
                f"""{hilight("[Login]", "fail")} {hilight(name)} does not recognise the """
                f"""imported session{where}. The cookies were probably copied from a """
                """different country's site, or they have expired — export them again from """
                """the site the monitor searches, while signed in there.""",
                extra=aimm_event(
                    "session_checked", marketplace=name, signed_in=False, lane=lane
                ),
            )

    def _check_session_on_lane(self: "MarketplaceMonitor", name: str, lane_name: str) -> None:
        """Run the check on the lane that owns this platform's browser.

        Submitted rather than waited on.  The lane may be an hour into a pass,
        and blocking the monitor thread until it comes up for air would stall
        every other platform to print one log line.  The lane logs the answer
        itself, which is also the honest place for it: it is the browser the
        sentence is about.
        """
        lane = self.lanes.get(lane_name)
        if lane is None or not lane.alive or self.config is None:
            # No browser yet is not a bad answer, it is no answer.  The lane
            # takes the session from disk when it opens (`_seed_profile_sessions`)
            # and the next search reports what the shop does with it.
            return
        marketplace_config = self.config.marketplace.get(name)
        if marketplace_config is None:
            return

        def check(context: BrowserContext) -> None:
            marketplace = self._lane_marketplace(lane, context, marketplace_config)
            health = getattr(marketplace, "session_health", None)
            if health is not None:
                self._report_shop_session(name, health, lane=lane_name)
                return
            probe = getattr(marketplace, "is_signed_in", None)
            if probe is not None:
                self._report_signed_in(name, bool(probe()), lane=lane_name)

        try:
            lane.submit(check)
        except KeyboardInterrupt:
            raise
        except Exception:
            if self.logger:
                self.logger.debug(
                    f"Could not check the {name} session on lane {lane_name!r}", exc_info=True
                )

    def _report_shop_session(
        self: "MarketplaceMonitor", name: str, health: Any, lane: str | None = None
    ) -> None:
        """Say what the shop did with the cookies that were just loaded.

        ``lane`` names the browser that answered, because with several profiles
        open "Sodimac is serving its pages" is only useful if you know which one
        it served.
        """
        try:
            answer = health()
        except KeyboardInterrupt:
            raise
        except Exception:
            return
        if answer is None or not self.logger:
            # None is "could not ask", which is not an answer and must not be
            # reported as a bad one.
            return
        served, sentence = answer
        where = f""" (navegador de {lane})""" if lane else ""
        if served:
            self.logger.info(
                f"""{hilight("[Login]", "succ")} {sentence}{where}""",
                extra=aimm_event(
                    "session_checked", marketplace=name, signed_in=True, lane=lane
                ),
            )
        else:
            self.logger.error(
                f"""{hilight("[Login]", "fail")} {sentence}{where}""",
                extra=aimm_event(
                    "session_checked", marketplace=name, signed_in=False, lane=lane
                ),
            )

    def _seed_imported_sessions(self: "MarketplaceMonitor") -> None:
        """Take up any session the user imported but no browser has loaded yet.

        Called on every browser launch as well as between jobs, because the two
        moments an import can arrive are "while the monitor is running" and
        "while it is not", and only the file survives the second one.
        """
        if self.config is None or self.context is None:
            return
        live = [None, *(lane.name for lane in self.lanes.values() if lane.alive)]
        for name in self.config.marketplace:
            # Asked of every browser that is up, not only the monitor's: a lane
            # that started after the import was taken by the main profile is
            # still owed it, and it is the browser that does that platform's
            # searching.
            if any(import_is_pending(name, lane) for lane in live):
                self._load_session_into_browser(name)

    def _apply_pending_sessions(self: "MarketplaceMonitor") -> None:
        """Load sessions the web UI imported into the running browser.

        Only this thread may touch Playwright, so an import made while the
        monitor is running lands here rather than in the request handler.
        """
        if self.context is None:
            # Leave the request where it is: taking it now would drop it, and
            # the browser is about to be launched anyway.
            return
        for name in sorted(control.take_session_imports()):
            self._load_session_into_browser(name)
        # Belt and braces: an import that arrived while this process was not
        # running left no in-memory note, only the file.
        self._seed_imported_sessions()

    def _abandon_scrape(self: "MarketplaceMonitor") -> None:
        """Unwind after a pause or a stop: say so, and drop what was asked for.

        Both cut the search off at the same checkpoint and just as promptly.
        What they leave behind is the whole difference between them, and it is
        read from the request rather than decided here: a stop takes the
        browsers with it, a pause leaves every window, tab and signed-in
        session exactly where it was, so resuming costs one search rather than
        one login.

        The flag is cleared only at the end, once whatever was to be closed
        actually is, so nothing can start a new search believing the stop has
        already happened.
        """
        stopping = control.cancel_mode() != "pause"
        if self.logger:
            self.logger.warning(
                f"""{hilight("[Pause]", "fail")} """
                + (
                    """Stopped: the search under way was cut off and every browser """
                    """closed."""
                    if stopping
                    else """Paused: the search under way was cut off.  The browsers are """
                    """left open, so resuming picks up without signing in again."""
                ),
                extra=aimm_event(
                    "scraping_cancelled", paused=True, mode="stop" if stopping else "pause"
                ),
            )
        if stopping:
            self._close_browser()
            # Lanes hold browsers of their own; a stop that left one of them
            # open would be a stop the user can see is not a stop.
            self._stop_review_lane()
            self._close_lanes()
        control.clear_cancel()
        control.set_phase(
            "pausing",
            "The running search was stopped and the browsers closed."
            if stopping
            else "The running search was stopped; the browsers are still open.",
        )

    def _announce_marketplace_blocks(self: "MarketplaceMonitor") -> None:
        """Tell the user when a site has started refusing us.

        Worth a message rather than only a log line, because it is the one
        failure that looks like nothing at all: the monitor is healthy, the
        searches run, and one platform quietly finds nothing for hours.  On a
        server nobody is watching the interface, which is exactly where this
        happens.

        Said once per refusal, claimed from `control` so a cooldown that is read
        on every poll does not become a message on every poll.  No link: the
        browser view is served relative to whatever address the interface was
        opened on -- localhost, a Tailscale name -- and this process does not
        know which, so a URL invented here would be a URL that does not work.
        """
        pending = control.take_new_blocks()
        if not pending or self.config is None:
            return
        users = list(self.config.user.keys())
        if not users:
            return
        for block in pending:
            name = str(block.get("marketplace") or "")
            label = _marketplace_label(name)
            minutes = int(float(block.get("seconds") or 0) // 60)
            reason = str(block.get("reason") or "refused us")
            body = (
                f"{label} {reason} instead of serving the page, so it is being left "
                f"alone for {minutes} minutes.\n\n"
                "If it keeps happening, open the monitor's browser view and solve the "
                "check once by hand -- the clearance is saved and reused afterwards."
            )
            for user in users:
                if self.config.user[user].enabled is False:
                    continue
                try:
                    NotificationConfig.message_all(
                        self.config.user[user],
                        f"{label} is being left alone",
                        body,
                        self.logger,
                    )
                except KeyboardInterrupt:
                    raise
                except Exception:
                    if self.logger:
                        self.logger.debug(
                            f"Could not tell {user!r} about the {name} cooldown",
                            exc_info=True,
                        )

    def _skip_blocked(self: "MarketplaceMonitor", name: str) -> bool:
        """Whether this platform is on a cooldown, said once where it happens.

        Read before a browser is opened rather than inside the search, so a
        platform being left alone costs nothing at all -- not a window, not a
        Chromium process, not a tab at about:blank.
        """
        if not name or not control.marketplace_blocked(name):
            return False
        if self.logger:
            block = control.marketplace_block(name) or {}
            self.logger.info(
                f"""{hilight("[Search]", "info")} Not opening a browser for """
                f"""{hilight(name)}: it {block.get("reason") or "refused us"} and is """
                f"""being left alone for another """
                f"""{int(block.get("seconds_left", 0) // 60)} minutes. """
                """Press "ejecutar ahora" to try it before then.""",
                extra=aimm_event("search_skipped", marketplace=name),
            )
        return True

    def _run_job(self: "MarketplaceMonitor", job: schedule.Job) -> JobOutcome:
        """Run one scheduled search and say how it ended."""
        self._ensure_browser()
        self._apply_pending_sessions()
        self._announce_marketplace_blocks()
        tags = sorted(str(tag) for tag in (job.tags or []))
        pair: Pair = getattr(job, "amm_pair", ("", ""))
        control.set_phase("searching", tags[0] if tags else "")
        try:
            with control.running(item=tags[0] if tags else None):
                job.run()
            # Between two searches on this thread nothing can open or close a
            # tab, so this is the moment the count is both settled and current.
            self._report_browser()
        except CancelledScrape:
            self._abandon_scrape()
            return JobOutcome.CANCELLED
        except SearchStopped as stopped:
            # Not a fault and not a configuration change: somebody pressed the
            # button that says "this one is done, get on with the next".
            if self.logger:
                self.logger.info(
                    f"""{hilight("[Search]", "info")} Stopped searching """
                    f"""{hilight(stopped.item)} on {hilight(stopped.marketplace)} """
                    """on request.  Moving on to the next one.""",
                    extra=aimm_event(
                        "search_stopped",
                        item=stopped.item,
                        marketplace=stopped.marketplace,
                        scope=stopped.scope,
                    ),
                )
            # It counts as having run: the interval starts again from here,
            # or "stop this search" would put it straight back at the front of
            # the queue it was just taken out of.
            if pair[0]:
                self._remember_run(pair[0], pair[1])
            return JobOutcome.STOPPED
        except SearchSuperseded as superseded:
            # The configuration has already been adopted by the guard that
            # raised this; all that is left is to say what became of the search
            # and let the caller rebuild the schedule around the new one.
            if self.logger:
                self.logger.info(
                    f"""{hilight("[Search]", "info")} Stopped searching """
                    f"""{hilight(superseded.item)}: the search was """
                    f"""{superseded.reason} while it ran.  Moving on to the next one.""",
                    extra=aimm_event(
                        "search_superseded",
                        item=superseded.item,
                        marketplace=superseded.marketplace,
                        reason=superseded.reason,
                    ),
                )
            return JobOutcome.SUPERSEDED
        finally:
            self._publish_schedule()
        # Recorded here rather than inside `search_item`, because this is where
        # the *schedule* is: what has to survive a restart is "the scheduler ran
        # this pair", and a search that raised its way out did not.
        if pair[0]:
            self._remember_run(pair[0], pair[1])
        return JobOutcome.DONE

    def _scheduled_pairs(self: "MarketplaceMonitor") -> Set[Pair]:
        """Every (item, marketplace) the scheduler currently holds a job for."""
        return {getattr(job, "amm_pair", ("", "")) for job in schedule.get_jobs()}

    @staticmethod
    def _marketplace_of(job: schedule.Job) -> str:
        return str(getattr(job, "amm_pair", ("", ""))[1])

    @staticmethod
    def _interleave(
        jobs: List[schedule.Job],
        served: Dict[str, int] | None = None,
        first: str | None = None,
    ) -> List[schedule.Job]:
        """Deal the queue out one platform at a time, round-robin.

        The queue is built platform by platform -- every Facebook search, then
        every Mercado Libre one -- because that is the order the configuration
        is read in.  Working through it as built means the second platform is
        not touched until the first one is completely finished, and a Facebook
        pass over a handful of products is the better part of an hour.  Add a
        forced pause or a restart, which sends the pass back to the top of the
        queue, and the tail of it is never reached at all: the platform at the
        end is scheduled, is reported as configured, and never actually runs.

        Alternating is the whole fix.  Order *within* a platform is untouched,
        so nothing else about the queue changes.

        ``served`` is how many searches each platform has already had this pass.
        It matters because the caller re-derives the queue after every single
        search -- deliberately, so that a search deleted mid-pass is not run --
        and a round-robin that started from scratch each time would hand every
        turn to whichever platform the configuration happens to name first.

        ``first`` is a product the user picked to go next.  It jumps the whole
        queue, platforms and all, and only that: the rest keep their order and
        the alternation among them is untouched, so "run this one next" is a
        promotion rather than a reshuffle.
        """
        seen: Dict[str, int] = {}
        ranked: List[Tuple[int, int, int, schedule.Job]] = []
        for index, job in enumerate(jobs):
            name = MarketplaceMonitor._marketplace_of(job)
            turn = (served or {}).get(name, 0) + seen.get(name, 0)
            seen[name] = seen.get(name, 0) + 1
            chosen = 0 if first and getattr(job, "amm_pair", ("", ""))[0] == first else 1
            ranked.append((chosen, turn, index, job))
        # The original position breaks ties, so platforms keep their
        # configuration order and each platform keeps its own internal order.
        ranked.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
        return [job for _chosen, _turn, _index, job in ranked]

    def _marketplaces_run_in_parallel(self: "MarketplaceMonitor") -> bool:
        return bool(
            self.config is not None
            and getattr(self.config.monitor, "parallel_marketplaces", False)
        )

    def _job_groups(
        self: "MarketplaceMonitor",
        wanted: Set[Pair] | None,
        due_only: bool = False,
    ) -> Dict[str, List[schedule.Job]]:
        """The queue split by platform, in configuration order.

        Only ever called on the monitor thread: it reads the ``schedule``
        registry, which no lane may touch.
        """
        groups: Dict[str, List[schedule.Job]] = {}
        for job in schedule.get_jobs():
            pair: Pair = getattr(job, "amm_pair", ("", ""))
            if wanted is not None and pair not in wanted:
                continue
            if due_only and not job.should_run:
                continue
            groups.setdefault(str(pair[1]), []).append(job)
        return groups

    @staticmethod
    def _searches_of(jobs: List[schedule.Job]) -> List[Tuple[Any, Any]]:
        """The (marketplace config, item config) pairs a list of jobs searches.

        Taken from the jobs on the monitor thread and handed to a lane as plain
        configuration objects, so the lane never holds a ``Job``: the
        ``schedule`` registry stays single-threaded even while two searches are
        genuinely running at once.
        """
        searches: List[Tuple[Any, Any]] = []
        for job in jobs:
            func = getattr(job, "job_func", None)
            args = getattr(func, "args", ()) if func is not None else ()
            if len(args) >= 3:
                # (marketplace_config, marketplace, item_config), as bound in
                # `schedule_jobs`.  The marketplace object is the monitor's own
                # and is deliberately dropped: the lane makes its own, on its
                # own browser.
                searches.append((args[0], args[2]))
        return searches

    def _mark_ran(self: "MarketplaceMonitor", pairs: Set[Pair]) -> None:
        """Move the schedule on for searches a lane ran rather than this thread.

        A lane is handed configurations, not jobs, so the job standing for the
        search it just did has no idea it happened and would come round as due
        again immediately.

        The schedule is republished on the way out, and that is the fix for a
        specific complaint: cancel a platform and "próxima ejecución" showed a
        slot that had already gone by, sometimes for minutes.  Nothing was
        wrong with the interface.  The job really did still hold the old slot
        when the state was last published -- ``_run_job`` publishes in its
        ``finally``, which runs *before* the caller here advances the job, and
        nothing published again until the next search ended.  Publishing where
        the change actually happens is what makes the answer right immediately;
        the web UI re-reads within a second of the log line the cancellation
        already writes.
        """
        if not pairs:
            return
        now = datetime.now()
        for item, marketplace in pairs:
            self._remember_run(item, marketplace)
        for job in schedule.get_jobs():
            if getattr(job, "amm_pair", ("", "")) not in pairs:
                continue
            job.last_run = now
            try:
                job._schedule_next_run()
            except Exception:  # pragma: no cover - private API, guarded
                if self.logger:
                    self.logger.debug("Could not advance a job's schedule", exc_info=True)
        self._publish_schedule()

    def _lane_pass(
        self: "MarketplaceMonitor",
        lane_name: str,
        searches: List[Tuple[Any, Any]],
    ) -> Callable[[BrowserContext], bool]:
        """The work one lane does for one pass, as a callable it can run.

        Runs *on the lane's thread*, against the lane's own browser.  It makes
        no decision the monitor has not already made: the searches were chosen
        before they were handed over, and the only reasons it stops early are
        the two every checkpoint already reads -- the monitor was paused, or the
        running scrape was told to stop.

        False when it was cut short, which is the same answer the sequential
        path gives for the same reason.
        """

        def work(context: BrowserContext) -> bool:
            lane = self.lanes.get(lane_name)
            if lane is None:
                return True
            # A queue rather than a `for`, because "ejecutar ahora" can arrive
            # while this lane is halfway down it and the answer to it is a
            # different order, not a different pass.
            queue = list(searches)
            while queue:
                # Asked for *now*, and this lane still has it to run: it goes to
                # the front instead of waiting for its turn.  Claimed in the
                # same breath, on whichever lane gets here first -- a promotion
                # that is read and not claimed promotes the same product on
                # every turn round every queue, for ever.
                urgent = control.next_search_now()
                if (
                    urgent is not None
                    and any(pair[1].name == urgent for pair in queue)
                    and control.take_next_search_if(urgent)
                ):
                    # Stable, so everything else keeps the order it had.
                    queue.sort(key=lambda pair: pair[1].name != urgent)
                marketplace_config, item_config = queue.pop(0)
                if is_paused() or control.cancel_requested():
                    return False
                # A platform that has just refused us is not one to open a
                # second browser at.  Both flows read the same cooldown.
                if control.marketplace_blocked(marketplace_config.name):
                    continue
                # Asked to stop before its turn came round.  Skipping it here
                # is the whole of "stop this search": the platforms it has not
                # reached yet must not be started either, or the button would
                # only ever stop the one page that happened to be open.
                if self._skip_stopped(item_config.name, marketplace_config.name):
                    continue
                try:
                    marketplace = self._lane_marketplace(lane, context, marketplace_config)
                    with control.running(
                        item=item_config.name,
                        marketplace=marketplace_config.name,
                        lane=lane_name,
                    ):
                        self.search_item(marketplace_config, marketplace, item_config)
                    # The lane's counterpart of the line in `_run_job`: a lane
                    # holds configurations rather than jobs, so nothing else
                    # here knows the pair has had its turn.
                    self._remember_run(item_config.name, marketplace_config.name)
                except CancelledScrape:
                    if self.logger:
                        self.logger.warning(
                            f"""{hilight("[Pause]", "fail")} The {lane_name} search was """
                            """stopped; its browser is being closed.""",
                            extra=aimm_event(
                                "scraping_cancelled", paused=True, lane=lane_name
                            ),
                        )
                    return False
                except SearchStopped as stopped:
                    if self.logger:
                        self.logger.info(
                            f"""{hilight("[Search]", "info")} Stopped searching """
                            f"""{hilight(stopped.item)} on {lane_name} on request.""",
                            extra=aimm_event(
                                "search_stopped",
                                item=stopped.item,
                                marketplace=stopped.marketplace,
                                scope=stopped.scope,
                                lane=lane_name,
                            ),
                        )
                    continue
                except SearchSuperseded as superseded:
                    # The configuration moved under this search.  One lane's
                    # search being dropped says nothing about the others, so the
                    # lane carries on with the rest of its queue.
                    if self.logger:
                        self.logger.info(
                            f"""{hilight("[Search]", "info")} Stopped searching """
                            f"""{hilight(superseded.item)} on {lane_name}: it was """
                            f"""{superseded.reason} while it ran.""",
                            extra=aimm_event(
                                "search_superseded",
                                item=superseded.item,
                                marketplace=superseded.marketplace,
                                reason=superseded.reason,
                                lane=lane_name,
                            ),
                        )
                    continue
                except KeyboardInterrupt:
                    raise
                except Exception as error:
                    # One platform failing is not a reason to stop the other:
                    # the lanes are independent, and that is the point of them.
                    if self.logger:
                        self.logger.error(
                            f"""{hilight("[Search]", "fail")} The search for """
                            f"""{hilight(item_config.name)} on {lane_name} failed: {error}""",
                            extra=aimm_event(
                                "search_failed",
                                item=item_config.name,
                                marketplace=marketplace_config.name,
                                lane=lane_name,
                                error=str(error),
                            ),
                        )
                    continue
            return True

        return work

    def _skip_stopped(self: "MarketplaceMonitor", item: str, marketplace: str) -> bool:
        """Whether this search was told to end before it got its turn.

        The checkpoint inside a running search raises; this is the same question
        asked of a search that has not started, where there is nothing to raise
        out of and skipping is the whole answer.  A platform-level stop is spent
        by the platform it names; a product-level one stands until the pass ends,
        because it has to reach every platform of that product.
        """
        stop = control.stop_requested(item, marketplace)
        if stop is None:
            return False
        if stop.get("marketplace"):
            control.clear_search_stop(item, str(stop["marketplace"]))
        if self.logger:
            self.logger.info(
                f"""{hilight("[Search]", "info")} Skipping {hilight(item)} on """
                f"""{hilight(marketplace)}: stopped from the web UI before it started.""",
                extra=aimm_event(
                    "search_stopped",
                    item=item,
                    marketplace=marketplace,
                    scope=str(stop.get("scope") or "search"),
                    started=False,
                ),
            )
        return True

    def _run_jobs(
        self: "MarketplaceMonitor",
        only: Set[Pair] | None = None,
        due_only: bool = False,
    ) -> bool:
        """Run searches now.  False when a forced pause cut the pass short.

        Sequentially by default, which is what the monitor has always done.
        With ``parallel_marketplaces`` on and more than one platform in the
        queue, each platform gets a browser and a thread of its own and they run
        side by side -- see :meth:`_run_jobs_in_parallel`.
        """
        if self._marketplaces_run_in_parallel():
            groups = self._job_groups(only, due_only=due_only)
            if groups:
                return self._run_jobs_in_parallel(groups, only=only, due_only=due_only)
        return self._run_jobs_sequentially(only, due_only=due_only)

    def _run_jobs_in_parallel(
        self: "MarketplaceMonitor",
        groups: Dict[str, List[schedule.Job]],
        only: Set[Pair] | None = None,
        due_only: bool = False,
    ) -> bool:
        """Search the platforms at the same time, each on the browser it owns.

        The platform bound to the monitor's own browser runs on this thread;
        every other one runs on its own lane.  Which platform that is comes
        from :meth:`_bind_platforms` and does not change, so a lane's browser
        belongs to one platform for the life of the process.

        The part that is not obvious is the *reaping*.  This method used to
        hand out the work and then join every lane before it touched the
        schedule again, which made a parallel pass a barrier: a lane that
        finished its queue in two minutes sat idle for the fifty it took the
        slowest participant to finish, its next search not even chosen, and its
        "next run" left showing a slot that had already passed.  Two searches
        genuinely ran at once, and their *cycles* were still locked together --
        which is the whole thing parallel searching was meant to stop.

        So the lanes are reaped as they finish rather than at the end.  A lane
        that comes back has its searches marked as run, the schedule
        republished (so the interface has the new "next run" within seconds
        rather than at the end of the pass), and anything of its platform's
        that has since come due handed straight back to it.  Reaping happens on
        this thread only -- between two of its own searches, and in the loop
        below -- because choosing work means reading the ``schedule`` registry,
        and that stays single-threaded.
        """
        self._bind_platforms(list(self._job_groups(None)))
        main_name = self._main_platform()
        # A pass over the whole queue owns the promotion and the stops, exactly
        # as the sequential pass does -- and here it has to be claimed once, up
        # front, because both this thread and the lanes need to act on it.
        # Neither worked in a parallel pass before: this method handed its own
        # half to `_run_jobs_sequentially` narrowed to one platform, which is
        # precisely the case that declines to honour a promotion or spend a
        # stop.  That was survivable while parallel searching was off by
        # default and is not now.
        whole_pass = only is None
        promoted = control.take_next_search() if whole_pass else None
        tasks: Dict[str, Any] = {}
        handed: Dict[str, Set[Pair]] = {}
        # What this pass has already run, so re-deriving the queue for a lane
        # that has just come free offers it what is *newly* due rather than the
        # same searches again.  A pass that is not `due_only` would otherwise
        # hand a lane the same work for ever.
        done: Set[Pair] = set()
        fallback: List[str] = []
        ok = True

        def dispatch(name: str, jobs: List[schedule.Job]) -> None:
            """Hand one platform's searches to its own lane."""
            searches = self._searches_of(jobs)
            if not searches:
                return
            # A platform on a cooldown, or every one of its searches already
            # told to stop, has nothing for a browser to do.  The lane used to
            # be started first and find that out afterwards, which left a
            # window open at about:blank for a pass that never ran.
            if control.marketplace_blocked(name) or all(
                control.stop_requested(item_config.name, marketplace_config.name)
                for marketplace_config, item_config in searches
            ):
                return
            try:
                lane = self._lane(name)
                lane.start()
            except KeyboardInterrupt:
                raise
            except Exception as error:
                # A lane that will not open is not a reason to skip a platform:
                # it is a reason to search it the old way, on this thread, once
                # the parallel part is done.  Said plainly in the log, because
                # the visible effect -- this platform driving the browser
                # another one normally uses -- otherwise reads as a fault.
                if self.logger:
                    self.logger.warning(
                        f"""{hilight("[Browser]", "fail")} Could not open a browser of its """
                        f"""own for {hilight(name)} ({error}). It will be searched in turn, """
                        """on the monitor's browser.""",
                        extra=aimm_event("lane_failed", marketplace=name, error=str(error)),
                    )
                if name not in fallback:
                    fallback.append(name)
                return
            pairs = {getattr(job, "amm_pair", ("", "")) for job in jobs}
            done.update(pairs)
            handed[name] = pairs
            if promoted:
                # The promoted product goes to the front of this lane's queue
                # too. "Search this one next" is a promise about the next
                # search, and a lane that ignored it would keep that promise on
                # one platform and break it on the other.
                searches.sort(key=lambda pair: pair[1].name != promoted)
            tasks[name] = lane.submit(self._lane_pass(name, searches))

        def reap(name: str, task: Any) -> None:
            """Take one finished lane's answer and move its schedule on."""
            nonlocal ok
            del tasks[name]
            try:
                if not task.wait(0):
                    ok = False
            except KeyboardInterrupt:
                raise
            except Exception as error:
                ok = False
                if self.logger:
                    self.logger.error(
                        f"""{hilight("[Search]", "fail")} The {hilight(name)} lane ended """
                        f"""badly: {error}""",
                        extra=aimm_event("lane_failed", marketplace=name, error=str(error)),
                    )
            # A lane is handed configurations rather than jobs, so nothing else
            # knows its searches happened.  Publishing straight afterwards is
            # what makes "próxima ejecución" right the moment a platform is
            # cancelled instead of at the end of the pass.
            self._mark_ran(handed.pop(name, set()))
            self._publish_schedule()

        def service() -> None:
            """Reap whatever has finished and give it more, without waiting."""
            for name in list(tasks):
                if tasks[name].done.is_set():
                    reap(name, tasks[name])
            if is_paused() or control.cancel_requested():
                return
            for name in [name for name in self._browser_of if name not in tasks]:
                if self._browser_of[name] == "" or name in fallback:
                    continue
                jobs = [
                    job
                    for job in self._job_groups(only, due_only=due_only).get(name, [])
                    if getattr(job, "amm_pair", ("", "")) not in done
                ]
                if jobs:
                    dispatch(name, jobs)

        for name, jobs in groups.items():
            if name == main_name or self._browser_of.get(name) == "":
                continue
            dispatch(name, jobs)

        if self.logger and tasks:
            running = [*([main_name] if main_name in groups else []), *tasks]
            self.logger.info(
                f"""{hilight("[Schedule]", "info")} Searching """
                f"""{", ".join(running)} at the same time.""",
                extra=aimm_event("parallel_pass", marketplaces=running),
            )

        if main_name in groups:
            main_pairs = {getattr(job, "amm_pair", ("", "")) for job in groups[main_name]}
            done |= main_pairs
            # `between` is what stops this thread's own queue from becoming the
            # barrier the lanes used to wait on: between two of its searches it
            # is free, and free is when it may touch the schedule.
            ok = (
                self._run_jobs_sequentially(
                    main_pairs,
                    marketplaces={main_name},
                    due_only=due_only,
                    between=service,
                    promoted=promoted,
                )
                and ok
            )

        # Everything still out is waited for, but one at a time and never
        # longer than a moment, so a lane that comes free is fed rather than
        # left holding an open browser until the last one is done.
        while tasks:
            for name in list(tasks):
                task = tasks.get(name)
                if task is None:
                    continue
                if task.done.wait(LANE_REAP_INTERVAL):
                    reap(name, task)
            service()

        for name in fallback:
            # The jobs this pass chose, not everything the platform has: a pass
            # narrowed to what is due must stay narrowed when a lane refuses to
            # open, or a browser that would not start turns into a full sweep.
            pairs = {getattr(job, "amm_pair", ("", "")) for job in groups.get(name, [])}
            if pairs and not self._run_jobs_sequentially(
                pairs, marketplaces={name}, due_only=False
            ):
                ok = False

        if whole_pass:
            # The pass is over, so the stops that belonged to it are spent.
            # Keeping them would silently skip the same searches next time
            # round, which is what switching a search off is for.  Here rather
            # than inside the sequential half, which only ever saw one
            # platform's share and could not know the lanes had finished with
            # theirs.
            control.clear_search_stops()
        elif only:
            # And the same for a pass narrowed to certain searches, which is
            # what saving one starts -- the case the report was about, since
            # parallel searching is on by default and this is the method that
            # runs it.  Narrowed to the products this pass actually held: the
            # rest may still be waiting for a stop of their own to be honoured.
            control.clear_search_stops_for(item for item, _marketplace in only)
        return ok

    def _run_jobs_sequentially(
        self: "MarketplaceMonitor",
        only: Set[Pair] | None = None,
        marketplaces: Set[str] | None = None,
        due_only: bool = False,
        between: Callable[[], None] | None = None,
        promoted: str | None = None,
    ) -> bool:
        """Run searches one at a time, adopting configuration changes as they arrive.

        False when a forced pause cut the pass short.

        ``between`` is called before each search, on this thread, at the one
        moment it holds nothing: a parallel pass uses it to take in the lanes
        that have finished and give them their next work, so this thread's own
        queue stops being something the lanes wait behind.

        ``only`` limits the pass to certain pairs -- what "the user just edited
        these two searches" needs, so that saving one search does not re-search
        the other nine.  ``None`` is everything, which is what the first pass
        and the "search now" button both want.  ``marketplaces`` narrows it
        further, to the platforms this thread is responsible for while lanes
        take the others.  ``due_only`` runs what the schedule says is due rather
        than everything.

        The queue is re-derived after every reload rather than held, because
        holding it is how a deleted search still gets searched.  What has
        already run is remembered by :meth:`_job_key`, so a reload does not
        send the pass back to the beginning -- and a search the reload *edited*
        gets a new key, so it is searched again under its new settings.
        """
        wanted: Set[Pair] | None = None if only is None else set(only)
        done: Set[Tuple[Any, ...]] = set()
        # How many searches each platform has had so far this pass.  Kept here
        # rather than derived from `done`, whose keys carry fingerprints that a
        # reload can change under it.
        served: Dict[str, int] = {}
        # A product the user promoted, claimed once so the promotion cannot
        # outlive the pass it was made for.
        #
        # Only on a pass over the whole queue.  A pass narrowed to certain
        # pairs (the searches a save just touched) or to certain platforms (this
        # thread's half of a parallel pass) cannot honour a promotion -- the
        # promoted product may not be in its queue at all -- and must not clear
        # stops that belong to the searches it is not running.
        whole_pass = only is None and marketplaces is None
        # A parallel pass claims the promotion for the whole pass and hands
        # this thread its share, because the lanes need to know about it too --
        # so it arrives here already claimed rather than being taken again.
        first = promoted or (control.take_next_search() if whole_pass else None)
        while True:
            if between is not None:
                between()
            # Adopted from a checkpoint, which may have been on a lane and so
            # could not touch the `schedule` registry.  This thread can.
            if self._schedule_dirty:
                self._schedule_dirty = False
                self._rebuild_schedule()
                if wanted is not None:
                    wanted |= self._scheduled_pairs()
            if first is None and whole_pass:
                # Picked mid-pass: the button is pressed on a screen watching a
                # search that is running, and the promise is the *next* search,
                # not the next pass.
                first = control.take_next_search()
            candidates = [
                job
                for job in schedule.get_jobs()
                if self._job_key(job) not in done
                and (wanted is None or getattr(job, "amm_pair", ("", "")) in wanted)
                and (marketplaces is None or self._marketplace_of(job) in marketplaces)
                and (not due_only or job.should_run)
            ]
            if first is None and not whole_pass:
                # A narrowed pass does not honour an ordinary promotion -- the
                # promoted product may not be in its queue at all -- but
                # "ejecutar ahora" is a different promise, and refusing it here
                # would make the button mean "at the end of the pass" for the
                # half of a parallel pass that runs on this thread.  Only when
                # this queue really holds it, and claimed as it is honoured.
                urgent = control.next_search_now()
                if (
                    urgent is not None
                    and any(getattr(job, "amm_pair", ("", ""))[0] == urgent for job in candidates)
                    and control.take_next_search_if(urgent)
                ):
                    first = urgent
            pending = self._interleave(candidates, served, first)
            if not pending:
                # The pass is over, so the stops that belonged to it are spent.
                # Keeping them would silently skip the same searches next time
                # round, which is what switching a search off is for.
                #
                # Only when this pass was the whole queue.  During a parallel
                # pass this method is one platform's half, and the lanes running
                # beside it have not necessarily reached the stops meant for
                # them yet -- clearing here would withdraw a request the user
                # can still see pending.
                if whole_pass:
                    control.clear_search_stops()
                elif marketplaces is None and wanted:
                    # A pass narrowed to certain searches (what saving one
                    # starts) is still over for the searches it held, and it is
                    # the only thing that will ever spend their stops.  Left
                    # standing, they outlived the pass entirely: the interface
                    # went on showing "deteniendose..." beside a next run that
                    # was counting down perfectly normally, and the next whole
                    # pass skipped the search once for a button pressed an hour
                    # earlier.  Narrowed to *these* searches, never the others.
                    control.clear_search_stops_for(item for item, _marketplace in wanted)
                return True
            job = pending[0]
            done.add(self._job_key(job))
            name = self._marketplace_of(job)
            served[name] = served.get(name, 0) + 1
            pair: Pair = getattr(job, "amm_pair", ("", ""))
            if pair[0] == first:
                first = None
            if self._skip_stopped(pair[0], pair[1]):
                # Told to stop before its turn came.  It counts as having run,
                # so the queue moves past it rather than offering it again.
                self._mark_ran({pair})
                self._publish_schedule()
                continue

            if self._skip_blocked(name):
                # Before the browser, not after it.  The parallel path has asked
                # this for a while; this one opened Chromium and found out
                # inside `Marketplace.search` that the platform was on a
                # cooldown -- so a skipped search still cost a window, which sat
                # at about:blank until the idle release closed it again.  That
                # window is the whole of "se abre una página about:blank y luego
                # se cierra".
                self._mark_ran({pair})
                self._publish_schedule()
                continue

            self.wait_while_paused()
            outcome = self._run_job(job)
            if outcome is JobOutcome.CANCELLED:
                return False
            if outcome is JobOutcome.STOPPED:
                self._mark_ran({pair})
            self.handle_pause()
            # Between two searches the browser is free: spend a little of that
            # on the listings already stored.  A no-op when the review has a
            # lane of its own, where it is already running.
            if not self._refresh_slice():
                return False

            if outcome is JobOutcome.SUPERSEDED:
                # The guard adopted the configuration on its way out; the
                # schedule is what is left to bring in line with it.
                self._schedule_dirty = False
                self._rebuild_schedule()
                if wanted is not None:
                    wanted |= self._scheduled_pairs()
                continue
            if outcome is JobOutcome.STOPPED:
                continue
            probe = self._probe_config(force=True)
            if probe is None or not probe.changed:
                continue
            if not probe.readable:
                # Broken or half-written.  Hand it to the loader, which reports
                # it properly and waits for it to be fixed.
                schedule.clear()
                return True
            change = probe.change
            self._adopt_config(probe)
            self._rebuild_schedule()
            if wanted is not None and change is not None:
                # A search added or edited during a targeted pass belongs in
                # that pass: it is exactly what the user has just asked for.
                wanted |= change.to_run(self._scheduled_pairs())

    def _run_all_jobs(self: "MarketplaceMonitor") -> bool:
        """Search everything now.  False when a forced pause cut it short."""
        return self._run_jobs()

    def _run_due_jobs(self: "MarketplaceMonitor") -> bool:
        """Run whatever the schedule says is due.  False on a forced pause.

        ``schedule.run_pending()`` would do this in one call, and used to.  It
        runs the jobs itself, though, which means no phase reported while they
        run, no pause honoured between them, and -- now that a search can be
        abandoned because the user replaced it -- an exception escaping the
        monitor loop entirely.  Choosing them here costs a comprehension and
        buys all three back, plus the option of handing a platform to a lane.
        """
        while True:
            if not any(job.should_run for job in schedule.get_jobs()):
                return True
            if not self._run_jobs(due_only=True):
                return False
            probe = self._probe_config(force=True)
            if probe is None or not probe.changed:
                continue
            if not probe.readable:
                schedule.clear()
                return True
            change = probe.change
            self._adopt_config(probe)
            self._rebuild_schedule()
            if change is not None and not self._run_changed(change):
                return False

    def _run_changed(self: "MarketplaceMonitor", change: ConfigChange) -> bool:
        """Search straight away the ones the change actually touched.

        The old behaviour was to search everything whenever the file changed,
        which turned adding one product into a full pass over all of them --
        a burst of traffic the marketplace notices, for results nobody asked
        to refresh.  The searches the user did not touch keep their places in
        the schedule; the ones they added or edited run now, which is what
        makes the change feel applied rather than merely saved.
        """
        pairs = change.to_run(self._scheduled_pairs())
        if not pairs:
            return True
        if self.logger:
            names = ", ".join(sorted({item for item, _marketplace in pairs}))
            self.logger.info(
                f"""{hilight("[Schedule]", "info")} Searching {hilight(names)} now under """
                """the configuration just saved.""",
                extra=aimm_event(
                    "config_search_now",
                    items=sorted({item for item, _marketplace in pairs}),
                ),
            )
        return self._run_jobs(only=pairs)

    # ------------------------------------------------------------------ #
    # Updating listings already stored
    # ------------------------------------------------------------------ #

    def _recheck_interval(self: "MarketplaceMonitor") -> float:
        configured = getattr(self.config.monitor, "listing_recheck_interval", None) if self.config else None
        return float(configured) if configured else float(DEFAULT_RECHECK_INTERVAL)

    def _updates_run_in_parallel(self: "MarketplaceMonitor") -> bool:
        return bool(
            self.config is not None
            and getattr(self.config.monitor, "parallel_listing_updates", False)
        )

    def _review_marketplaces(self: "MarketplaceMonitor") -> Tuple[str, ...]:
        """The platforms whose stored listings may be re-read right now.

        A platform that has just refused us is left out: the cooldown is shared
        by both flows precisely so that a second browser does not go on knocking
        at a door the first one already found shut.
        """
        if self.config is None:
            return ()
        return tuple(
            name
            for name, marketplace_config in self.config.marketplace.items()
            if marketplace_config.enabled is not False and not control.marketplace_blocked(name)
        )

    def _listings_to_review(self: "MarketplaceMonitor") -> bool:
        """Whether any stored listing is overdue for a re-check right now.

        Asked before a browser is opened for the review, never to decide what a
        round does once it is running.  One record is enough of an answer, so
        the scan stops at the first.
        """
        names = self._review_marketplaces()
        if not names:
            return False
        try:
            return bool(
                stale_records(None, within=self._recheck_interval(), marketplaces=names, limit=1)
            )
        except Exception:
            # The store being unreadable is not a reason to refuse to review;
            # let the round itself find out.
            if self.logger:
                self.logger.debug("Could not scan for listings to review", exc_info=True)
            return True

    def _marketplace_for_refresh(
        self: "MarketplaceMonitor", marketplace_name: str
    ) -> Marketplace | None:
        """The marketplace the shared-tab refresher should drive.

        The very instance the search uses, so both kinds of work share the one
        tab and take turns on it.  This is the path taken when reviews do *not*
        have a lane of their own; when they do, the refresher is handed a
        marketplace bound to that lane's browser instead (see
        :meth:`_review_lane_loop`), which is what lets the two run at the same
        time rather than in turn.
        """
        if self.config is None or self.context is None:
            return None
        marketplace_config = self.config.marketplace.get(marketplace_name)
        if marketplace_config is None or marketplace_config.enabled is False:
            return None
        return self.active_marketplaces.get(marketplace_name)

    def _item_config_for(
        self: "MarketplaceMonitor", marketplace_name: str, item_name: str | None
    ) -> TItemConfig | None:
        """The configuration a stored listing should be re-judged against.

        None when the search it came from is no longer in the config -- deleted
        or renamed.  The refresher still re-checks such a listing (it is still
        in the dashboard, its price still moves and it can still sell); it just
        keeps the keep/discard verdict the listing already carried rather than
        recomputing it from some other product's filters.
        """
        if self.config is None or not item_name:
            return None
        return self.config.items.get((marketplace_name, item_name))

    def _new_refresher(
        self: "MarketplaceMonitor",
        marketplace_for: Callable[[str], Marketplace | None],
    ) -> ListingRefresher:
        """A refresher bound to one browser.

        Its own object per browser, because a refresher holds the failure
        cooldowns for the listings *it* tried; sharing one between two browsers
        would be sharing nothing that matters and confusing what does.  What
        must not be duplicated -- which listing is being read right now -- is
        not held here at all: it is a claim in
        :mod:`ai_marketplace_monitor.control`, taken by whichever flow gets
        there first, and that is what keeps two browsers off the same page.
        """
        return ListingRefresher(
            marketplace_for=marketplace_for,
            item_config_for=self._item_config_for,
            logger=self.logger,
            stop_when=lambda: is_paused() or control.cancel_requested(),
            # The rhythm is the review schedule's business now, not the
            # refresher's: it runs a round when one is due and not otherwise.
            recheck_interval=self._recheck_interval(),
        )

    def _get_refresher(self: "MarketplaceMonitor") -> ListingRefresher:
        if self.refresher is None:
            self.refresher = self._new_refresher(self._marketplace_for_refresh)
            self.refresher.slice_interval = 0.0
        # Read from the config every time: it is reloaded whenever the file
        # changes, and the refresher outlives those reloads.
        self.refresher.recheck_interval = self._recheck_interval()
        return self.refresher

    # ------------------------------------------------------------------ #
    # When a round of re-checks happens
    # ------------------------------------------------------------------ #
    #
    # It used to happen whenever the loop passed a gap between two searches,
    # which is a reasonable thing to do and an impossible thing to report: "at
    # some point" is not an answer to "when is the next review?".  Now it has a
    # schedule of its own -- see :mod:`ai_marketplace_monitor.review` -- and the
    # next round is a timestamp the interface can show.

    def _plan_next_review(self: "MarketplaceMonitor", after: float | None = None) -> None:
        """Fix the moment of the next round, and publish it.

        Drawn once and stored, rather than computed whenever someone asks:
        a random interval asked twice gives two different answers, and a
        "next review" that moves every time the page refreshes is worse than
        none at all.
        """
        self._review_due = self.review_schedule.next_after(after)
        control.set_updates_next_run(
            datetime.fromtimestamp(self._review_due, timezone.utc).isoformat(timespec="seconds")
        )

    def _review_due_now(self: "MarketplaceMonitor") -> bool:
        if self._review_due <= 0.0:
            # Nothing planned yet: the first round is due as soon as there is a
            # browser to do it with.
            self._plan_next_review()
            return True
        return time.time() >= self._review_due

    def _log_review(self: "MarketplaceMonitor", report: Any, lane: str | None = None) -> None:
        if not report or not self.logger:
            return
        # The skips are part of the sentence, not a hidden counter: a round that
        # checked nothing has to say what it did instead, or "0 of 0" looks like
        # an empty queue when it is anything but.
        skipped = f""" Skipped {report.skipped} ({report.why_skipped()}).""" if report.skipped else ""
        self.logger.info(
            f"""{hilight("[Update]", "succ" if report.updated else "info")} """
            f"""Re-checked {report.checked} stored listing(s): {report.updated} updated, """
            f"""{report.removed} removed, {report.failed} undecided.{skipped}""",
            extra=aimm_event(
                "listing_refresh",
                checked=report.checked,
                updated=report.updated,
                removed=report.removed,
                failed=report.failed,
                skipped=report.skipped,
                skips=report.skips,
                parallel=self._updates_run_in_parallel(),
                lane=lane,
            ),
        )

    def _refresh_slice(self: "MarketplaceMonitor", limit: int | None = None) -> bool:
        """Bring a few stored listings up to date on the search's own tab.

        False when a forced pause cut it short.  Called between searches and
        while waiting for the next one, which is where that tab is otherwise
        doing nothing at all.

        Nothing at all when reviews have a lane of their own: the round is
        already happening over there, on its own browser, at the same time as
        the search -- and doing it twice would be two browsers reading the same
        listings.
        """
        if self.config is None:
            return True
        if self._updates_run_in_parallel():
            return True
        if is_paused() or control.cancel_requested():
            # The round that was due is not going to happen while the monitor is
            # held back.  Moving its slot rather than leaving it behind is the
            # difference between "próxima revisión: en 40 minutos" and "próxima
            # revisión: hace 8 minutos", and only one of those can be true.
            if self._review_due_now():
                self._plan_next_review()
            return True
        if not self._review_due_now():
            return True

        names = self._review_marketplaces()
        if not names:
            # Nothing to review anywhere: try again at the next slot rather
            # than asking on every pass round the loop.
            self._plan_next_review()
            return True

        if self.context is None:
            if not self._browsers_idle:
                # No browser and no business opening one: the user stopped the
                # scraper, and a review that quietly started a Chromium would
                # be the stop button not working.
                return True
            # Closed because there was nothing to do, and now there is.
            self._ensure_browser()

        control.set_phase("updating", ", ".join(names))
        try:
            with control.running(item=None, marketplace="updates"), control.updating(
                list(names), lane=control.MAIN_LANE
            ):
                report = self._get_refresher().run_slice(
                    names, limit=self.review_schedule.batch if limit is None else limit
                )
        except CancelledScrape:
            self._abandon_scrape()
            return False
        except KeyboardInterrupt:
            raise
        except Exception as e:
            if self.logger:
                self.logger.debug(f"Listing refresh slice failed: {e}", exc_info=True)
            self._plan_next_review()
            return True

        self._plan_next_review()
        self._log_review(report)
        # A re-check is where a price actually moves, so this is the other
        # moment a search's cheapest listing can change -- and the only one that
        # catches a seller dropping their price between two searches.
        self._announce_price_drops(report)
        self._announce_top_listings_after_review()
        self._announce_low_stock()
        return True

    # ------------------------------------------------------------------ #
    # Reviewing while the search carries on
    # ------------------------------------------------------------------ #

    def _review_lane_loop(self: "MarketplaceMonitor", context: BrowserContext) -> bool:
        """Re-check stored listings for as long as the monitor is running.

        This is the whole of "review in parallel".  It runs on the updates
        lane's thread, against the updates lane's browser, so a round of
        re-checks and a search are genuinely happening at the same time rather
        than taking turns on one tab.

        The two flows are kept off each other's work by what was already there
        rather than by anything new:

        * a listing read minutes ago is not stale, so a listing the search has
          just fetched is not in the queue this builds -- the queue is ordered
          by ``last_seen`` and both flows write it;
        * a listing being read *right now* is claimed in
          :mod:`ai_marketplace_monitor.control`, and the loser of the claim
          skips it rather than waiting;
        * a platform that has refused either flow is on a cooldown both read.

        Ends when :attr:`_review_stop` is set, which is what makes the lane
        closable: a task that never returned would sit in front of the
        sentinel forever.
        """
        refresher = self._new_refresher(
            lambda name: self._review_lane_marketplace(name, context)
        )
        refresher.slice_interval = 0.0
        while not self._review_stop.is_set():
            if is_paused() or control.cancel_requested():
                # Paused means paused for both flows.  Nothing is torn down: the
                # lane keeps its browser so resuming costs no relaunch.  The
                # slot moves with the wait, so the interface is never showing a
                # "next review" that is already behind.
                if self._review_due_now():
                    self._plan_next_review()
                self._review_stop.wait(2.0)
                continue
            if not self._review_due_now():
                self._review_stop.wait(min(5.0, max(0.5, self._review_due - time.time())))
                continue

            names = self._review_marketplaces()
            if not names:
                self._plan_next_review()
                continue

            refresher.recheck_interval = self._recheck_interval()
            report = None
            try:
                with control.running(
                    item=None, marketplace="updates", lane=control.UPDATES_LANE
                ), control.updating(list(names), lane=control.UPDATES_LANE):
                    report = refresher.run_slice(names, limit=self.review_schedule.batch)
            except CancelledScrape:
                # The stop was meant for the searches; the review simply waits
                # it out rather than tearing its own browser down, which would
                # cost a relaunch for a pause measured in seconds.
                self._review_stop.wait(2.0)
                continue
            except KeyboardInterrupt:
                raise
            except Exception as error:
                if self.logger:
                    self.logger.debug(f"A review round failed: {error}", exc_info=True)
            self._plan_next_review()
            self._log_review(report, lane=control.UPDATES_LANE)
            self._announce_price_drops(report)
            self._announce_top_listings_after_review()
            self._announce_low_stock()
        return True

    def _review_lane_marketplace(
        self: "MarketplaceMonitor", name: str, context: BrowserContext
    ) -> Marketplace | None:
        """The marketplace object the review lane drives for one platform."""
        lane = self.lanes.get(control.UPDATES_LANE)
        if lane is None or self.config is None:
            return None
        marketplace_config = self.config.marketplace.get(name)
        if marketplace_config is None or marketplace_config.enabled is False:
            return None
        return self._lane_marketplace(lane, context, marketplace_config)

    def _start_review_lane(self: "MarketplaceMonitor") -> None:
        """Give the review a browser and a thread of its own, if it wants one.

        Idempotent, and cheap when the setting is off: no thread is created and
        no browser is opened.
        """
        if not self._updates_run_in_parallel():
            return
        lane = self.lanes.get(control.UPDATES_LANE)
        if lane is not None and lane.alive:
            return
        if not self._listings_to_review():
            # Nothing to re-check, so nothing to open a browser for.  Starting
            # it anyway is what put a second window on the screen sitting at
            # about:blank -- most visibly on a fresh install, where the store is
            # empty and the review lane had nothing to do for its whole life.
            # This is re-asked from the loop, so the lane starts the moment
            # there is something to look at.
            return
        self._review_stop.clear()
        try:
            lane = self._lane(control.UPDATES_LANE)
            lane.start()
        except KeyboardInterrupt:
            raise
        except Exception as error:
            if self.logger:
                self.logger.warning(
                    f"""{hilight("[Browser]", "fail")} Could not open a browser for the """
                    f"""review lane ({error}). Reviews will share the search's tab.""",
                    extra=aimm_event("lane_failed", marketplace="updates", error=str(error)),
                )
            self.lanes.pop(control.UPDATES_LANE, None)
            return
        if self.logger:
            self.logger.info(
                f"""{hilight("[Update]", "info")} Reviewing stored listings in a browser of """
                """its own, alongside the searches.""",
                extra=aimm_event("review_lane", started=True),
            )
        lane.submit(self._review_lane_loop)

    def _stop_review_lane(self: "MarketplaceMonitor") -> None:
        """Ask the review to finish its round and let go of its browser."""
        self._review_stop.set()
        lane = self.lanes.pop(control.UPDATES_LANE, None)
        if lane is not None:
            try:
                lane.close()
            except Exception:
                if self.logger:
                    self.logger.debug("Could not close the review lane", exc_info=True)

    def load_ai_agents(self: "MarketplaceMonitor") -> None:
        """Load the AI agents named by the configuration in hand.

        Rebuilt rather than added to: this runs on every reschedule, and the
        schedule is now rebuilt whenever the configuration changes.  Appending
        would leave the loop asking a service the user has just removed, once
        more on each reload.
        """
        assert self.config is not None
        self.ai_agents = []
        for ai_config in (self.config.ai or {}).values():
            if ai_config.enabled is False:
                continue
            if (
                ai_config.provider is not None
                and ai_config.provider.lower() in supported_ai_backends
            ):
                ai_class = supported_ai_backends[ai_config.provider.lower()]
            elif ai_config.name.lower() in supported_ai_backends:
                ai_class = supported_ai_backends[ai_config.name.lower()]
            else:
                if self.logger:
                    self.logger.error(
                        f"""{hilight("[Config]", "fail")} Cannot determine an AI service provider from service name or provider."""
                    )
                continue

            try:
                self.ai_agents.append(ai_class(config=ai_config, logger=self.logger))
                # self.ai_agents[-1].connect()
                # self.logger.info(
                #     f"""{hilight("[AI]", "succ")} Connected to {hilight(ai_config.name)}"""
                # )
            except KeyboardInterrupt:
                raise
            except Exception as e:
                if self.logger:
                    self.logger.error(
                        f"""{hilight("[AI]", "fail")} Failed to connect to {hilight(ai_config.name, "fail")}: {e}"""
                    )
                continue

    def search_item(
        self: "MarketplaceMonitor",
        marketplace_config: TMarketplaceConfig,
        marketplace: Marketplace,
        item_config: TItemConfig,
    ) -> None:
        """Search for an item on the marketplace, on the record.

        The pair being searched is what the web UI shows -- ``running()`` only
        says that *a* job is going, and the job's tag is the item alone.  Which
        platform it is on is half of the answer to "what is the scraper doing",
        so it is reported here, where both configurations are in hand.

        It is also what the checkpoint guard needs: "has the configuration
        changed under me?" is only answerable against a specific search, and
        this is the one place that knows which one is running.
        """
        self._searching = (item_config.name, marketplace_config.name)
        try:
            self._apply_item_language(marketplace, marketplace_config, item_config)
            with control.search(item_config.name, marketplace_config.name):
                self._search_item(marketplace_config, marketplace, item_config)
        finally:
            self._searching = None

    def _apply_item_language(
        self: "MarketplaceMonitor",
        marketplace: Marketplace,
        marketplace_config: TMarketplaceConfig,
        item_config: TItemConfig,
    ) -> None:
        """Read the pages of this search in the language the search asked for.

        The language is a property of the search, not of the platform: one
        product looked for in Chile and another in the United States are read
        off pages in different languages by the same monitor.  A marketplace
        object is shared by every search on its lane, so the translator is set
        here, right before the search runs, rather than once when the platform
        was prepared.

        Falls back to the platform's own ``language`` (and through that to its
        default), so a configuration written before the move is unaffected.
        """
        language = getattr(item_config, "language", None) or marketplace_config.language
        # Always a translator, never None: `configure` leaves the existing one
        # in place when handed None, which would carry the previous search's
        # language into this one.
        self._prepare_tracked(marketplace)
        marketplace.configure(
            marketplace_config, translator=self._select_translator(language) or Translator()
        )

    def _search_item(
        self: "MarketplaceMonitor",
        marketplace_config: TMarketplaceConfig,
        marketplace: Marketplace,
        item_config: TItemConfig,
    ) -> None:
        """Search for an item on the marketplace.

        A search that is cut off part-way still says what it found.  That is not
        a nicety: with ``notify_immediately`` off -- the default -- the one
        notification a search sends is built here, *after* the loop, and every
        way of ending a search early raises through that point.  So pressing
        "detener esta búsqueda", stopping a platform, or pausing the scraper
        threw away every listing the search had already found and scored, and
        the user saw it as Telegram going quiet after a stop.  The listings were
        never marked as notified, so nothing was lost permanently -- but the
        next notification about them waited for the whole interval to come round
        again, which on a marketplace where a well-priced console is gone in ten
        minutes is the same thing as never.

        Everything after the loop therefore runs either way, and the interruption
        is re-raised at the end so the caller still sees a stop as a stop.
        """
        new_listings: List[Listing] = []
        listing_ratings = []
        # users to notify is determined from item, then marketplace, then all users
        assert self.config is not None
        users_to_notify = (
            item_config.notify or marketplace_config.notify or list(self.config.user.keys())
        )
        # Settled once, here, because this is the only place that has both
        # configurations in hand: a notification channel has neither, and a
        # message that says "mercadolibre" in English to a user who configured
        # Spanish is a message written by whoever happened to be nearest the data.
        language = getattr(item_config, "language", None) or marketplace_config.language
        label = MARKETPLACE_LABELS.get(
            str(marketplace_config.market_type or marketplace_config.name).lower(),
            marketplace_config.name,
        )
        immediately = self._notifies_immediately()
        # The interruption is caught rather than allowed through, so that
        # everything below -- the summary, the count, and above all the one
        # notification a non-immediate search sends -- still happens for what
        # was found before the stop.  Re-raised at the end: a stop is still a
        # stop to the caller, it simply no longer costs the listings.
        interrupted: ScrapeInterrupted | None = None
        try:
            for listing in marketplace.search(item_config):
                control.raise_if_cancelled()
                # duplicated ID should not happen, but sellers could repost the same listing,
                # potentially under different seller names
                if listing.id in [x.id for x in new_listings] or listing.content in [
                    x.content for x in new_listings
                ]:
                    if self.logger:
                        self.logger.debug(f"Found duplicated result for {listing}")
                    continue
                # if everyone has been notified.  `users_to_notify` first, because
                # `all()` of nothing is true: with no users configured this read as
                # "everybody already knows" and threw away every listing found,
                # which is the opposite of what a monitor with no notification
                # channel should do -- it still collects, it just tells nobody.
                if users_to_notify and all(
                    User(self.config.user[user], self.logger).notification_status(listing)
                    == NotificationStatus.NOTIFIED
                    for user in users_to_notify
                ):
                    if self.logger:
                        self.logger.info(
                            f"""{hilight("[Skip]", "info")} Already sent notification for item {hilight(listing.title)}, skipping.""",
                            extra=aimm_event(
                                "listing_skip",
                                reason="already_notified",
                                listing_id=listing.id,
                                title=listing.title,
                                item=item_config.name,
                            ),
                        )
                    continue
                # for x in self.find_new_items(found_items)
                res = self.evaluate_by_ai(
                    listing, item_config=item_config, marketplace_config=marketplace_config
                )
                record_rating(
                    listing,
                    score=res.score,
                    comment=res.comment,
                    conclusion=res.conclusion,
                    ai_name=res.name,
                )
                if self.logger:
                    if res.comment == AIResponse.NOT_EVALUATED:
                        if res.name:
                            self.logger.info(
                                f"""{hilight("[AI]", res.style)} {res.name or "AI"} did not evaluate {hilight(listing.title)}."""
                            )
                        else:
                            self.logger.info(
                                f"""{hilight("[AI]", res.style)} No AI available to evaluate {hilight(listing.title)}."""
                            )
                    else:
                        self.logger.info(
                            f"""{hilight("[AI]", res.style)} {res.name or "AI"} concludes {hilight(f"{res.conclusion} ({res.score}): {res.comment}", res.style)} for listing {hilight(listing.title)}.""",
                            extra=aimm_event(
                                "ai_eval",
                                listing_id=listing.id,
                                title=listing.title,
                                url=getattr(listing, "post_url", None)
                                or getattr(listing, "url", None),
                                price=getattr(listing, "price", None),
                                score=res.score,
                                conclusion=res.conclusion,
                                comment=res.comment,
                                ai_name=res.name,
                                item=item_config.name,
                            ),
                        )
                if item_config.rating:
                    acceptable_rating = item_config.rating[
                        0 if item_config.searched_count == 0 else -1
                    ]
                elif marketplace_config.rating:
                    acceptable_rating = marketplace_config.rating[
                        0 if item_config.searched_count == 0 else -1
                    ]
                else:
                    acceptable_rating = 3

                if res.score < acceptable_rating:
                    if self.logger:
                        self.logger.info(
                            f"""{hilight("[Skip]", "fail")} Rating {hilight(f"{res.conclusion} ({res.score})")} for {listing.title} is below threshold {acceptable_rating}.""",
                            extra=aimm_event(
                                "listing_skip",
                                reason="below_threshold",
                                listing_id=listing.id,
                                title=listing.title,
                                item=item_config.name,
                                score=res.score,
                                threshold=acceptable_rating,
                            ),
                        )
                    counter.increment(CounterItem.EXCLUDED_LISTING, item_config.name)
                    continue
                new_listings.append(listing)
                listing_ratings.append(res)
                if immediately:
                    # The listing has passed every filter and been scored: it is as
                    # validated as it is ever going to be, and waiting for the rest
                    # of the platform to be searched adds nothing to it but delay.
                    # `_notify` only queues -- the sending is somebody else's thread
                    # -- so the next listing is being parsed while this one is in
                    # flight.
                    self._notify(
                        users_to_notify,
                        [listing],
                        [res],
                        item_config,
                        language=language,
                        label=label,
                        immediate=True,
                    )

        except ScrapeInterrupted as stopped:
            interrupted = stopped

        p = inflect.engine()
        if self.logger:
            self.logger.info(
                f"""{hilight("[Search]", "succ" if len(new_listings) > 0 else "fail")} {hilight(str(len(new_listings)))} new {p.plural_noun("listing", len(new_listings))} for {item_config.name} {p.plural_verb("is", len(new_listings))} found"""
                + (" before it was stopped." if interrupted is not None else "."),
                extra=aimm_event(
                    "search_summary",
                    item=item_config.name,
                    marketplace=marketplace_config.name,
                    new_count=len(new_listings),
                    interrupted=interrupted is not None,
                ),
            )
        control.record_found(item_config.name, marketplace_config.name, len(new_listings))
        if new_listings:
            counter.increment(
                CounterItem.NEW_VALIDATED_LISTING, item_config.name, len(new_listings)
            )
            if not immediately:
                # The batch notification: one message about everything this
                # search turned up.  Skipped outright when each listing was
                # already sent as it was found, or every listing would arrive
                # twice -- once on its own and once in the summary.
                #
                # Sent for a stopped search too, which is the whole reason the
                # interruption is held rather than allowed through: `_notify`
                # only puts the send on the dispatcher's queue, so this costs
                # the stop nothing -- no page is held open and no channel is
                # waited for on this thread.
                self._notify(
                    users_to_notify,
                    new_listings,
                    listing_ratings,
                    item_config,
                    language=language,
                    label=label,
                    immediate=False,
                )
        # After the batch, and after a stopped search too: the cheapest listing
        # of a search is a fact about everything stored for it, not only about
        # what this pass turned up, so a search that ended early can still have
        # produced a new floor -- and a listing found before the stop that
        # happens to be the cheapest one is exactly the message worth having.
        self._announce_top_listing(users_to_notify, item_config, language=language)
        if interrupted is not None:
            # Now that what was found has been handed over.  The caller decides
            # what a stop means -- next search, closed browser, paused monitor --
            # and none of that changes here.
            raise interrupted
        time.sleep(5)

    def _top_scopes(self: "MarketplaceMonitor") -> Dict[str, str]:
        """Which names share a cheapest-of, for the trackers a user grouped.

        A search competes with itself and nothing else -- its own listings are
        the whole market it knows about -- so it is absent here and
        :mod:`~ai_marketplace_monitor.toplist` treats an absent name as its own
        scope.  A tracker is the case that needed a second answer: one watched
        page has no cheapest, it just has a price, and "the cheapest of the five
        shops I am watching" is the fact the group was created to produce.
        """
        scopes: Dict[str, str] = {}
        if self.config is None:
            return scopes
        for (marketplace_name, item_name), item_config in self.config.items.items():
            if marketplace_name != TRACKED_PLATFORM:
                continue
            group = getattr(item_config, "group", None)
            if group:
                scopes[item_name] = str(group)
        return scopes

    def _item_label(self: "MarketplaceMonitor", item_config: TItemConfig) -> str | None:
        """What ``{item}`` should say, or None to keep the name on the listing.

        The same fact :meth:`_top_scopes` is built out of, asked for a different
        purpose, and deliberately not a second mapping: a tracker in a group
        competes under the group's name *because* that is the name of the thing
        the user is watching, and a message that called it
        ``juego-de-sabanas-menta-lisas-1-plaza-tex`` instead would be naming the
        slug the web interface made up rather than the name they chose.

        None for everything else, which is a search and an ungrouped tracker:
        both are already stamped on their listings under the only name they
        have, so there is nothing to say here and nothing to get wrong.
        """
        name = getattr(item_config, "name", None)
        if not name:
            return None
        return self._top_scopes().get(str(name))

    def _announce_top_listing(
        self: "MarketplaceMonitor",
        users: List[str],
        item_config: TItemConfig,
        language: str | None = None,
    ) -> None:
        """Say so when one search's cheapest valid listing gets cheaper.

        The search flow's entry point: it already knows who to tell and in what
        language, having just worked both out for the batch notification.
        """
        if not self._notify_reasons().top_listing:
            return
        scope = self._top_scopes().get(item_config.name, item_config.name)
        top = self._new_top_for(item_config.name, scope)
        if top is not None:
            self._send_top(top, item_config, users, language, scope=scope)

    def _announce_top_listings_after_review(self: "MarketplaceMonitor") -> None:
        """The same question, asked for every search, after a round of re-checks.

        This is the half of the feature the search flow cannot cover.  A
        re-check is where a price actually *moves*: the search only ever adds
        listings, and a seller who drops their price by 40.000 changes the
        cheapest offer of a search that may not run again for hours.

        One pass over the store for every search rather than one pass each --
        see :func:`~ai_marketplace_monitor.toplist.current_tops`.  Who to tell
        is resolved per search here, because unlike the search flow there is no
        marketplace in hand: a top-1 can belong to any platform the search runs
        on.
        """
        if self.config is None or not self._notify_reasons().top_listing:
            return
        names = list(self.config.item.keys())
        if not names:
            return
        scopes = self._top_scopes()
        try:
            tops = new_tops(names, scope_of=scopes)
        except KeyboardInterrupt:
            raise
        except Exception:
            if self.logger:
                self.logger.debug("Could not work out the cheapest listings", exc_info=True)
            return
        for scope, top in tops.items():
            item_config = self.config.item.get(scope)
            if item_config is None:
                # A group of trackers: the scope is the group's name and no
                # section is called that.  The winning listing names the tracker
                # it belongs to, and that tracker's settings -- who to notify, in
                # what language -- are the ones that apply, because the message
                # is about its page.
                owner = str((top.snapshot or {}).get("name") or "")
                item_config = self.config.item.get(owner)
            if item_config is None:
                continue
            self._send_top(top, item_config, users=None, language=None, scope=scope)

    def _prepare_tracked(self: "MarketplaceMonitor", marketplace: Any) -> None:
        """Give the tracked platform an AI to fall back on, when one exists.

        Not passed through the config like a filter, because it is not a
        setting: it is the same AI service the searches already use, borrowed
        for the one job the parsers cannot always do.  A monitor with no AI
        configured simply gets ``None`` and the five other strategies.
        """
        if self.config is None or getattr(marketplace, "name", "") != TRACKED_PLATFORM:
            return
        try:
            marketplace.ai_reader = tracking_reader_for(self.config)
        except KeyboardInterrupt:
            raise
        except Exception:
            marketplace.ai_reader = None

    # ------------------------------------------------------------------ #
    # Reading a tracker for the first time
    # ------------------------------------------------------------------ #
    #
    # The one moment a tracker needs a browser of its own.  Everything after it
    # is the review's work, on the review's browser, exactly as for a listing a
    # search found -- which is the whole idea of a tracker: a stored listing
    # with no search behind it.

    def _trackers_to_ingest(self: "MarketplaceMonitor") -> List[Tuple[Any, Any]]:
        """The trackers whose page has never been read.

        Asked before a browser is opened rather than after, because "already in
        the store" is the answer for every tracker but the one just added, and
        opening a window to find that out is the cost this avoids.
        """
        if self.config is None:
            return []
        marketplace_config = self.config.marketplace.get(TRACKED_PLATFORM)
        if marketplace_config is None or marketplace_config.enabled is False:
            return []
        pending: List[Tuple[Any, Any]] = []
        for (marketplace_name, _item_name), item_config in self.config.items.items():
            if marketplace_name != TRACKED_PLATFORM or item_config.enabled is False:
                continue
            url = str(getattr(item_config, "url", "") or "")
            if not url or is_known(TRACKED_PLATFORM, tracked_id(url)):
                continue
            pending.append((marketplace_config, item_config))
        return pending

    def _ingest_trackers(self: "MarketplaceMonitor") -> None:
        """Read every tracker nobody has read yet, now, on a browser of its own.

        On a thread of its own as well, and that is the requirement rather than
        an implementation detail: a tracker is added by a person who has just
        pasted an address and is watching the screen, and the monitor may be
        forty minutes into a Facebook pass.  Neither may wait for the other, so
        this borrows the machinery a parallel platform already uses -- a lane:
        a thread, a Playwright and a browser on its own profile.

        The browser is opened for this and closed after it.  The lane is
        deliberately *not* kept in :attr:`lanes`, because everything in there is
        something the monitor may hand more work to later, and this one has
        exactly one thing to do in its life.
        """
        if is_paused():
            return
        pending = self._trackers_to_ingest()
        if not pending:
            return
        with self._ingest_lock:
            if self._ingesting:
                return
            self._ingesting = True
        threading.Thread(
            target=self._ingest_pass,
            args=(pending,),
            name="amm-track-ingest",
            daemon=True,
        ).start()

    def _ingest_pass(self: "MarketplaceMonitor", pending: List[Tuple[Any, Any]]) -> None:
        """Open a browser, read the new trackers, close it again."""
        lane = BrowserLane(
            control.TRACKED_LANE, launch=self._launch_context, logger=self.logger
        )
        try:
            if self.logger:
                names = ", ".join(item_config.name for _marketplace, item_config in pending)
                self.logger.info(
                    f"""{hilight("[Track]", "info")} Reading {hilight(names)} for the """
                    """first time.""",
                    extra=aimm_event(
                        "tracker_ingest",
                        items=[item_config.name for _marketplace, item_config in pending],
                    ),
                )
            lane.run(lambda context: self._read_trackers(lane, context, pending))
        except KeyboardInterrupt:
            raise
        except Exception as error:
            # A browser that will not open is the usual one.  Nothing is lost:
            # the tracker is still unknown to the store, so the next time this
            # is asked -- the next configuration change, or the next turn round
            # the loop -- it is offered again.
            if self.logger:
                self.logger.warning(
                    f"""{hilight("[Track]", "fail")} Could not read the new trackers """
                    f"""({error}). They will be read on the next attempt.""",
                    extra=aimm_event("tracker_ingest_failed", error=str(error)),
                )
        finally:
            try:
                lane.close()
            except KeyboardInterrupt:
                raise
            except Exception:
                if self.logger:
                    self.logger.debug("Could not close the tracker lane", exc_info=True)
            self._ingesting = False

    def _read_trackers(
        self: "MarketplaceMonitor",
        lane: BrowserLane,
        context: BrowserContext,
        pending: List[Tuple[Any, Any]],
    ) -> bool:
        """Read each new tracker once, on the lane's thread and browser."""
        for marketplace_config, item_config in pending:
            if is_paused() or control.cancel_requested():
                return False
            url = str(getattr(item_config, "url", "") or "")
            if not url or is_known(TRACKED_PLATFORM, tracked_id(url)):
                # Read while this was waiting for its browser to open, by an
                # attempt that was slower than it looked.
                continue
            try:
                marketplace = self._lane_marketplace(lane, context, marketplace_config)
                with control.running(
                    item=item_config.name,
                    marketplace=marketplace_config.name,
                    lane=control.TRACKED_LANE,
                ):
                    self.search_item(marketplace_config, marketplace, item_config)
            except CancelledScrape:
                return False
            except (SearchStopped, SearchSuperseded):
                continue
            except KeyboardInterrupt:
                raise
            except Exception as error:
                if self.logger:
                    self.logger.error(
                        f"""{hilight("[Track]", "fail")} Could not read """
                        f"""{hilight(item_config.name)}: {error}""",
                        extra=aimm_event(
                            "tracker_ingest_failed",
                            item=item_config.name,
                            error=str(error),
                        ),
                    )
                continue
        return True

    def _announce_price_drops(
        self: "MarketplaceMonitor", report: RefreshReport | None
    ) -> None:
        """Tell the user when a stored listing is cheaper than what they were told.

        The third thing a round of re-checks can turn up, beside a new cheapest
        listing and a tracker running out -- and the one that had nowhere to go.
        The code that writes a "bajó de precio" message has always existed
        (:attr:`~ai_marketplace_monitor.notification.NotificationStatus.LISTING_DISCOUNTED`),
        but its only door was the search, and a search never hands over a
        listing it already knows
        (:func:`~ai_marketplace_monitor.observations.is_known`).  So the price
        moved, the store recorded it, the log said so, and the user was told
        nothing -- with ``notify_price_drop`` switched on the whole time.

        Whether it is cheaper is asked **per user**, and that is the point
        rather than a detail: the fall the refresher saw is against the stored
        snapshot, and what makes a message worth sending is the fall against
        *the price this user was last told*.  Two users with different
        ``remind`` intervals honestly have different answers, and a user who was
        told at a price the listing is still above hears nothing.

        A user who was never told about the listing at all is the exception, and
        it used to be a silence.  The reasoning was that announcing a fall on a
        listing they have never heard of is a message about a stranger -- true
        as far as it goes, and it made ``notify_price_drop`` depend on
        ``notify_new`` having been on at the time.  Switch new listings off,
        which is exactly what somebody who only wants to hear about falls does,
        and nothing is ever written to that user's cache, so no fall ever
        reaches the test above and the switch they turned *on* does nothing.
        It is worse for a tracker, where "never heard of it" is false by
        construction: they pasted the address themselves.

        So a listing with no cache entry falls back to the store's own answer --
        the price it held before this very re-check, which the refresher hands
        over in :class:`~ai_marketplace_monitor.refresh.PriceDrop`.  That cannot
        turn into a flood: ``report.drops`` holds only listings that got cheaper
        *in this slice*, never the backlog.
        """
        if self.config is None or report is None or not report.drops:
            return
        if not self._notify_reasons().price_drop:
            return
        for drop in report.drops:
            listing = drop.listing
            item_config = self._item_config_for(listing.marketplace, drop.item_name)
            if item_config is None or item_config.enabled is False:
                # Deleted, renamed, or switched off.  The listing is still
                # re-checked -- it is still in the dashboard -- but a message
                # about a product the user removed or paused is noise, and a
                # deleted search has no `notify` list to read anyway.
                continue
            marketplace_config = self.config.marketplace.get(listing.marketplace)
            users = (
                item_config.notify
                or (getattr(marketplace_config, "notify", None) if marketplace_config else None)
                or list(self.config.user.keys())
            )
            told = [
                user
                for user in users
                if user in self.config.user
                and User(self.config.user[user], self.logger).notification_status(listing)
                in (
                    NotificationStatus.LISTING_DISCOUNTED,
                    # Never told about this listing, so there is no price of
                    # theirs to be cheaper than; the store's own fall stands in.
                    NotificationStatus.NOT_NOTIFIED,
                )
            ]
            if not told:
                continue
            record = get_observation(listing.marketplace, listing.id)
            rating = _stored_rating(
                record.get("rating") if isinstance(record, dict) else None
            )
            if self.logger:
                self.logger.info(
                    f"""{hilight("[Notify]", "succ")} {hilight(listing.title)} is cheaper: """
                    f"""{drop.previous} to {hilight(listing.price)}.""",
                    extra=aimm_event(
                        "price_drop",
                        item=item_config.name,
                        listing_id=listing.id,
                        marketplace=listing.marketplace,
                        title=listing.title,
                        price_from=drop.previous,
                        price_to=listing.price,
                        url=listing.post_url,
                    ),
                )
            # Forced, because the filter above has already established the
            # reason and one of the two answers it accepts does not survive the
            # round trip: a user with no cache entry reads back as NOT_NOTIFIED,
            # whose reason is "new" -- and a monitor with `notify_new` off would
            # drop the very message this exists to send.
            self._notify(
                told,
                [listing],
                [rating],
                item_config,
                forced_status=NotificationStatus.LISTING_DISCOUNTED,
                # What it cost before this re-check, for whoever has no price of
                # their own on file: without it the message says a listing got
                # cheaper without saying cheaper than what.
                previous_prices=[drop.previous],
                language=getattr(item_config, "language", None)
                or getattr(marketplace_config, "language", None),
                label=(
                    TRACKED_LABEL
                    if listing.marketplace == TRACKED_PLATFORM
                    else MARKETPLACE_LABELS.get(
                        listing.marketplace.lower(), listing.marketplace
                    )
                ),
                # A round of re-checks has no "end of the search" to save it up
                # for: this *is* the end of the only thing it was doing.
                immediate=True,
            )

    def _announce_low_stock(self: "MarketplaceMonitor") -> None:
        """Tell the user when a tracked product is running out.

        Asked after a round of re-checks, which is the only place a stock number
        moves: a tracker is read once when it is created and by the review from
        then on.

        Silent for everything that is not a tracker, and for every tracker whose
        page publishes no stock -- which is most pages.  A number that is not
        there is not zero, and firing on it would mean an alert about every
        product on every site that does not count.
        """
        if self.config is None:
            return
        for (marketplace_name, item_name), item_config in self.config.items.items():
            if marketplace_name != TRACKED_PLATFORM:
                continue
            minimum = getattr(item_config, "min_stock", None)
            if minimum is None or getattr(item_config, "enabled", None) is False:
                continue
            record = get_observation(TRACKED_PLATFORM, tracked_id(str(item_config.url or "")))
            if not isinstance(record, dict) or record.get("deleted"):
                continue
            snapshot = record.get("listing")
            if not isinstance(snapshot, dict):
                continue
            try:
                listing = Listing(**snapshot)
            except (TypeError, ValueError):
                continue
            if not stock_alert(item_name, listing, minimum):
                continue

            users = item_config.notify or list(self.config.user.keys())
            if not users:
                continue
            if self.logger:
                self.logger.info(
                    f"""{hilight("[Track]", "info")} {hilight(listing.title)} is down to """
                    f"""{hilight(listing.stock)} in stock.""",
                    extra=aimm_event(
                        "low_stock",
                        item=item_name,
                        listing_id=listing.id,
                        title=listing.title,
                        stock=listing.stock,
                        min_stock=minimum,
                        url=listing.post_url,
                    ),
                )
            self._notify(
                users,
                [listing],
                [_stored_rating(record.get("rating"))],
                item_config,
                language=getattr(item_config, "language", None),
                label=TRACKED_LABEL,
                immediate=True,
                forced_status=NotificationStatus.LOW_STOCK,
            )

    def _new_top_for(
        self: "MarketplaceMonitor", item_name: str, scope: str | None = None
    ) -> Any:
        """The cheapest listing of one search when it is worth announcing.

        Wrapped rather than called directly so that a store that cannot be read
        -- a cache mid-eviction, a record written by a version that stored
        something else -- costs one silent round rather than the search it was
        called from.

        ``scope`` is the name it competes under, which differs from its own only
        for a tracker in a group.  The whole group is asked, not just this one
        name: the cheapest of five watched pages does not change because the
        sixth was read, and asking about one of them would announce it as the
        cheapest whatever the other four cost.
        """
        scope = scope or item_name
        try:
            if scope == item_name:
                return new_top(item_name)
            members = [
                name for name, owner in self._top_scopes().items() if owner == scope
            ]
            return new_tops(members, scope_of=self._top_scopes()).get(scope)
        except KeyboardInterrupt:
            raise
        except Exception:
            if self.logger:
                self.logger.debug(
                    f"Could not work out the cheapest listing for {item_name!r}", exc_info=True
                )
            return None

    def _send_top(
        self: "MarketplaceMonitor",
        top: Any,
        item_config: TItemConfig,
        users: List[str] | None,
        language: str | None,
        scope: str | None = None,
    ) -> None:
        """Hand one top-1 to the notification path, then remember it.

        The record is written only after the send has been queued.  A top-1 that
        could not be handed over is one that gets announced next round rather
        than one that is silently marked as told.

        ``scope`` is what the record is filed under and what the message names:
        the search, or the group of trackers this one belongs to.  The listing
        handed over is still the individual page that won -- a group decides who
        competes, never what is sent -- so the card and the link name the page,
        while ``{item}`` names the group, exactly as it does in every other
        message about a grouped tracker (see :meth:`_item_label`).
        """
        listing = top.as_listing()
        if listing is None or self.config is None:
            return
        scope = scope or item_config.name

        if users is None:
            # The review flow has no marketplace in hand -- a top-1 can come
            # from any platform the search runs on -- so the platform's own
            # `notify` list is taken from whichever platform this listing is on.
            marketplace_config = self.config.marketplace.get(listing.marketplace)
            users = (
                item_config.notify
                or (getattr(marketplace_config, "notify", None) if marketplace_config else None)
                or list(self.config.user.keys())
            )
            language = getattr(item_config, "language", None) or getattr(
                marketplace_config, "language", None
            )
        if not users:
            return

        if self.logger:
            self.logger.info(
                f"""{hilight("[Notify]", "succ")} New cheapest listing for """
                f"""{hilight(scope)}: {hilight(top.price)}.""",
                extra=aimm_event(
                    "top_listing",
                    item=scope,
                    tracker=item_config.name if scope != item_config.name else None,
                    listing_id=listing.id,
                    marketplace=listing.marketplace,
                    title=listing.title,
                    price=top.price,
                    url=listing.post_url,
                ),
            )
        self._notify(
            users,
            [listing],
            [_stored_rating(top.rating)],
            item_config,
            language=language,
            label=_marketplace_label(listing.marketplace),
            immediate=True,
            forced_status=NotificationStatus.TOP_LISTING,
        )
        remember_top(scope, top)

    def _notify_reasons(self: "MarketplaceMonitor") -> NotifyReasons:
        """Which of "new", "cheaper" and "top 1" the user asked to hear about.

        Read on every send rather than cached on the monitor: the settings are
        editable while the scraper runs, and a value read once at startup would
        mean a checkbox that only takes effect after a restart.
        """
        return reasons_from_config(None if self.config is None else self.config.monitor)

    def _notifies_immediately(self: "MarketplaceMonitor") -> bool:
        """Whether a listing is told about the moment it passes."""
        return bool(
            self.config is not None and getattr(self.config.monitor, "notify_immediately", False)
        )

    def _description_words(self: "MarketplaceMonitor") -> int | None:
        """How many words of a listing's own text a notification carries."""
        if self.config is None:
            return DEFAULT_DESCRIPTION_WORDS
        configured = getattr(self.config.monitor, "max_description_words", None)
        return DEFAULT_DESCRIPTION_WORDS if configured is None else configured

    def _notify(
        self: "MarketplaceMonitor",
        users: List[str],
        listings: List[Listing],
        ratings: List[AIResponse],
        item_config: TItemConfig,
        language: str | None,
        label: str | None,
        immediate: bool,
        forced_status: NotificationStatus | None = None,
        previous_prices: List[str | None] | None = None,
    ) -> None:
        """Hand a notification to the sender, and do not wait for it.

        Both moments -- one listing as it is found, or all of them at the end
        of the search -- go through here, so "what a notification says" is
        decided in one place and only "when" differs.

        Nothing is sent on this thread.  A channel blocks for as long as its
        service wants (Telegram waits out a 429, SMTP waits for a handshake),
        and the checkpoints that read the pause and cancel flags live in the
        scraping code -- so a notification sent inline is a page left open and
        a "detener" button that does not answer until it finishes.  If the
        dispatcher has been closed, sending inline is the honest fallback:
        better a slow shutdown than a notification that was silently dropped.
        """
        assert self.config is not None
        words = self._description_words()
        reasons = self._notify_reasons()
        item_label = self._item_label(item_config)
        configs = [self.config.user[user] for user in users if user in self.config.user]
        if not configs:
            return

        def send() -> None:
            for user_config in configs:
                User(user_config, logger=self.logger).notify(
                    listings,
                    ratings,
                    item_config,
                    language=language,
                    marketplace_label=label,
                    description_words=words,
                    reasons=reasons,
                    forced_status=forced_status,
                    item_label=item_label,
                    previous_prices=previous_prices,
                )

        if not self.notifier.submit(send):
            send()
        elif immediate and self.logger:
            self.logger.debug(
                f"Queued an immediate notification for {listings[0].title!r}",
                extra=aimm_event(
                    "notify_queued",
                    listing_id=listings[0].id,
                    item=item_config.name,
                    pending=self.notifier.pending,
                ),
            )

    def _select_translator(
        self: "MarketplaceMonitor", language: str | None = None
    ) -> Translator | None:
        """Select the language for the marketplace."""
        # self.config.translator.get(marketplace_config.language, None)
        assert self.config is not None
        if not language:
            return None
        # English is the source language: every label the parsers look for is
        # already written in it, and a translation table exists precisely to map
        # away from it.  So there is none for `en`, and asking for one is not an
        # error -- it is the identity, which is what `None` means to `configure`
        # and to `_apply_item_language`.
        if language.split("_")[0].lower() == "en":
            return None
        if language in self.config.translator:
            return self.config.translator[language]
        # if there is no exact match, we are going to match the language code
        # e.g. 'en' to 'en_US'
        if "_" in language:
            # if a more general languge exists?
            if language.split("_")[0] in self.config.translator:
                translator = self.config.translator[language.split("_")[0]]
                if self.logger:
                    self.logger.info(
                        f"""{hilight("[Translator]", "info")} Using language {language.split("_")[0]} (locale {translator.locale}) for {language} translation."""
                    )
                return translator
            # if not, we are going to match the language code
            # e.g. 'en' to 'en_US'
            for name, translator in self.config.translator.items():
                if name.startswith(language.split("_")[0] + "_"):
                    if self.logger:
                        self.logger.info(
                            f"""{hilight("[Translator]", "info")} Using language {name} (locale {translator.locale}) for {language} translation."""
                        )
                    return translator
        # if there is no match, we are going to match the language code
        # e.g. 'en' to 'en_US'
        for name, translator in self.config.translator.items():
            if name.startswith(language + "_"):
                if self.logger:
                    self.logger.info(
                        f"""{hilight("[Translator]", "info")} Using language {name} (locale {translator.locale}) for {language} translation."""
                    )
                return translator
        raise RuntimeError(f"Cannot find translator for language {language}.")

    #: What the monitor searches at when nothing in the config says.
    DEFAULT_SEARCH_INTERVAL = 30 * 60
    DEFAULT_MAX_SEARCH_INTERVAL = 60 * 60

    def _schedule_for(
        self: "MarketplaceMonitor",
        item_config: TItemConfig,
        marketplace_config: TMarketplaceConfig,
    ) -> List[schedule.Job]:
        """Every job one (item, marketplace) pair runs on.

        The schedule is global: it comes from ``[monitor]`` and says nothing
        about which product or which platform it applies to.  An interval and a
        list of fixed times are not alternatives -- both can be on at once, and
        the searches they trigger are the same search.

        The per-item and per-marketplace keys are the old way of saying this,
        one copy per section.  They are still read when ``[monitor]`` is silent
        about the schedule, so an existing config keeps its behaviour, but the
        web UI no longer writes them.
        """
        assert self.config is not None
        monitor_config = self.config.monitor
        legacy = not any(
            (
                monitor_config.search_interval,
                monitor_config.max_search_interval,
                monitor_config.start_at,
            )
        )

        if legacy:
            start_at_list = item_config.start_at or marketplace_config.start_at or []
            search_interval = item_config.search_interval or marketplace_config.search_interval
            max_search_interval = (
                item_config.max_search_interval or marketplace_config.max_search_interval
            )
            # Fixed times used to *replace* the interval, and a config written
            # that way means it, so that reading is kept for it.
            if start_at_list:
                search_interval = max_search_interval = None
            elif search_interval is None and max_search_interval is None:
                search_interval = self.DEFAULT_SEARCH_INTERVAL
                max_search_interval = self.DEFAULT_MAX_SEARCH_INTERVAL
        else:
            start_at_list = monitor_config.start_at or []
            search_interval = monitor_config.search_interval
            max_search_interval = monitor_config.max_search_interval

        jobs: List[schedule.Job] = []
        for start_at in start_at_list:
            jobs.append(self._job_at(start_at, item_config.name))

        if search_interval is not None or max_search_interval is not None:
            low = max(search_interval or max_search_interval or self.DEFAULT_SEARCH_INTERVAL, 1)
            high = max(max_search_interval or low, low)
            if self.logger:
                self.logger.info(
                    f"""{hilight("[Schedule]", "info")} Scheduling to search for """
                    f"""{item_config.name} every {humanize.naturaldelta(low)}"""
                    f"""{"" if low == high else f" to {humanize.naturaldelta(high)}"}"""
                )
            jobs.append(schedule.every(low).to(high).seconds)

        if not jobs:
            raise ValueError(
                f"Cannot determine a schedule for {item_config.name} from configuration file."
            )
        return jobs

    def _job_at(self: "MarketplaceMonitor", start_at: str, item_name: str) -> schedule.Job:
        """One fixed time of day (or of every hour, or of every minute)."""
        if start_at.startswith("*:*:"):
            # '*:*:12' to ':12'
            if self.logger:
                self.logger.info(
                    f"""{hilight("[Schedule]", "info")} Scheduling to search for {item_name} """
                    f"""every minute at {start_at[3:]}s"""
                )
            return schedule.every().minute.at(start_at[3:])
        if start_at.startswith("*:"):
            # '*:12:12' or '*:12'
            if self.logger:
                self.logger.info(
                    f"""{hilight("[Schedule]", "info")} Scheduling to search for {item_name} """
                    f"""every hour at {start_at[1:]}m"""
                )
            return schedule.every().hour.at(
                start_at[1:] if start_at.count(":") == 1 else start_at[2:]
            )
        # '12:12:12' or '12:12'
        if self.logger:
            self.logger.info(
                f"""{hilight("[Schedule]", "info")} Scheduling to search for {item_name} """
                f"""every day at {start_at}"""
            )
        return schedule.every().day.at(start_at)

    def schedule_jobs(self: "MarketplaceMonitor") -> None:
        """Schedule jobs to run periodically."""
        # we reload the config file each time when a scan action is completed
        # this allows users to add/remove products dynamically.
        self.load_config_file()
        self.load_ai_agents()

        assert self.config is not None
        for marketplace_config in self.config.marketplace.values():
            if marketplace_config.enabled is False:
                continue
            marketplace_class = all_marketplaces[
                (marketplace_config.market_type or "facebook").lower()
            ]
            if marketplace_config.name in self.active_marketplaces:
                marketplace = self.active_marketplaces[marketplace_config.name]
            else:
                marketplace = marketplace_class(
                    marketplace_config.name, self.context, self.keyboard_monitor, self.logger
                )
                marketplace.renew_browser = self._renew_main_browser
                self.active_marketplaces[marketplace_config.name] = marketplace

            # Configure might have been changed
            self._prepare_tracked(marketplace)
            marketplace.configure(
                marketplace_config,
                translator=self._select_translator(marketplace_config.language),
            )

            # A tracker is not a search and gets no schedule entry.  It is
            # one address, read once when it is added and by the review from
            # then on -- so a repeating job for it could only ever open a
            # browser, find the listing already known and close it again.  That
            # is exactly what it did: an empty window every half hour, and a
            # "búsqueda" in the interface that could not, by design, ever find
            # anything.  The marketplace object above is still built and
            # configured, because the review drives it.
            if marketplace_config.name == TRACKED_PLATFORM:
                continue

            # One item can run on several marketplaces; each pair has its own
            # configuration, because the platforms take different options.
            for item_config in self.config.items_of(marketplace_config.name).values():
                if item_config.enabled is False:
                    continue

                for slot, job in enumerate(
                    self._schedule_for(item_config, marketplace_config)
                ):
                    job.do(
                        self.search_item,
                        marketplace_config,
                        marketplace,
                        item_config,
                    ).tag(item_config.name)
                    # The tag is the item alone, because that is what the
                    # interface shows and what several jobs share.  A reload
                    # needs more than that -- which platform, and which of this
                    # pair's slots -- so it travels on the job itself.
                    job.amm_pair = (item_config.name, marketplace_config.name)
                    job.amm_slot = slot
                    # Before anything reads `next_run`: a rebuilt schedule must
                    # not hand a search that ran two minutes ago a whole fresh
                    # interval, or every save would silently postpone
                    # everything the user did not touch.
                    self._seed_job_from_memory(job, job.amm_pair)

    def handle_pause(self: "MarketplaceMonitor") -> None:
        """Handle interruption signal."""
        if self.keyboard_monitor is None or not self.keyboard_monitor.is_paused():
            return

        rich.print(counter)
        if not self.keyboard_monitor.confirm():
            return

        # now we should go to an interactive session
        while True:
            while True:
                url = (
                    Prompt.ask(
                        f"""\nEnter an {hilight("ID")} or a {hilight("URL")} to check, or {hilight("exit")}."""
                    )
                    .strip("\x1b")
                    .strip()
                )

                if not url.isnumeric() and not url.startswith("https://"):
                    if url.endswith("exit"):
                        url = "exit"
                        break
                    if url:
                        print(f'Invalid input "{url}". Please try again.')
                else:
                    break

            if url == "exit":
                break

            try:
                self.check_items([url], for_item=None)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                if self.logger:
                    self.logger.debug(f"Failed to check item {url}: {e}")

    def wait_while_paused(self: "MarketplaceMonitor") -> None:
        """Block while the web UI's pause switch is on.

        Distinct from :meth:`handle_pause`, which is the keyboard's "stop and
        let me inspect an item" mode.  This one is a plain on/off switch with
        no interactive session behind it.

        The switch is read between searches, never inside one: a search already
        under way finishes and is notified about as usual, and nothing new
        starts until the switch is released.  Stopping mid-search would leave a
        half-scanned item looking like it had genuinely run.
        """
        if not is_paused():
            return
        control.set_phase(
            "paused",
            "Stopped from the web UI: the running search was cut off and the "
            "browsers closed."
            if is_force_paused()
            else "Paused from the web UI: the running search was cut off, and the "
            "browsers are still open.",
        )
        if self.logger:
            self.logger.info(
                f"""{hilight("[Pause]", "info")} Searching is paused from the web UI. """
                + (
                    """The search under way was stopped and every browser closed."""
                    if is_force_paused()
                    else """The search under way was stopped; the browsers are left """
                    """open so resuming does not have to sign in again."""
                ),
                extra=aimm_event("scraping_paused", paused=True, force=is_force_paused()),
            )
        # Only a stop takes the browsers.  A pause deliberately leaves them --
        # that is the whole difference between the two buttons, and it is what
        # makes resuming cost one search rather than one sign-in.  Every browser
        # on a stop, though: a second window still running would be a monitor
        # visibly carrying on after being told not to.
        if is_force_paused():
            self._close_browser()
            self._stop_review_lane()
            self._close_lanes()
        control.clear_run_request()
        # While the monitor is held back there is no next review, and saying
        # otherwise leaves the interface showing a slot that quietly slides into
        # the past -- "próxima revisión: hace 20 segundos", which is the same
        # impossible sentence in a second place.  It is planned again on the way
        # out, from the moment reviewing can actually resume.
        control.set_updates_next_run(None)
        while is_paused():
            # One long doze rather than a poll loop: doze already ticks once a
            # second, and stop_when lets it return the moment we are resumed.
            doze(
                3600,
                self.config_files,
                self.keyboard_monitor,
                stop_when=lambda: not is_paused(),
            )
        # Resuming cancels the cancellation: whatever was asked for is over.
        control.clear_cancel()
        # And it reads the configuration, before anything is scheduled from it.
        # Starting means starting on what the file says now: the whole reason a
        # person stops the monitor is often to change something, and the change
        # they made while it was stopped is exactly the one nothing else would
        # have noticed.  See `_refresh_config`.
        self._refresh_config()
        # From now, not from whenever the round was due before the pause: a slot
        # drawn an hour ago has no claim on a monitor that has just come back.
        self._plan_next_review()
        if self.logger:
            self.logger.info(
                f"""{hilight("[Pause]", "succ")} Resumed — searching again.""",
                extra=aimm_event("scraping_paused", paused=False),
            )

    def _configured_searches(self: "MarketplaceMonitor") -> int:
        """How many (item, marketplace) pairs are actually enabled right now.

        Zero is the answer on a fresh install, and after the user deletes their
        last search.  Both are ordinary states, not faults.
        """
        if self.config is None:
            return 0
        count = 0
        for (marketplace_name, _item_name), item_config in self.config.items.items():
            if item_config.enabled is False:
                continue
            marketplace_config = self.config.marketplace.get(marketplace_name)
            if marketplace_config is not None and marketplace_config.enabled is False:
                continue
            count += 1
        return count

    def _wait_for_searches(self: "MarketplaceMonitor") -> None:
        """Do nothing at all until at least one search exists.

        Nothing means nothing: no browser, no page, no poll.  A monitor with no
        searches configured has no work, and the old behaviour -- an error line
        and a sixty-second cycle -- burned a wake-up a minute complaining about
        a state the user may well have chosen on purpose.

        ``doze`` returns the moment the configuration file changes, so the wait
        ends as soon as a search is added from the web UI, without a restart.
        """
        announced = False
        while True:
            # The file first, then the decision.  This used to read a snapshot
            # taken before the monitor was stopped, so a search added while it
            # was stopped was not there to be counted -- and the wait below then
            # waited for a *future* change, which the one that had already
            # happened could never be.  See `_refresh_config`.
            self._refresh_config()
            if self._configured_searches() > 0:
                break
            control.set_phase(
                "waiting_for_config", "No searches are configured; nothing to do."
            )
            if not announced:
                announced = True
                if self.logger:
                    self.logger.info(
                        f"""{hilight("[Config]", "info")} No searches are configured. """
                        """Waiting - add one from the web UI and it will be picked up """
                        """without a restart.""",
                        extra=aimm_event("waiting_for_searches", searches=0),
                    )
            # Whatever the browser was holding is of no use with nothing to
            # search for, and a Chromium window open over an empty config reads
            # as a monitor that is stuck.
            if self.context is not None:
                self._close_browser()
            self._stop_review_lane()
            self._close_lanes()
            doze(
                3600,
                self.config_files,
                self.keyboard_monitor,
                stop_when=is_paused,
            )
            if is_paused():
                return
        if announced and self.logger:
            self.logger.info(
                f"""{hilight("[Config]", "succ")} """
                f"""{self._configured_searches()} search(es) configured - starting.""",
                extra=aimm_event("waiting_for_searches", searches=self._configured_searches()),
            )

    def _has_marketplace_credentials(self: "MarketplaceMonitor") -> bool:
        """True if every enabled marketplace has a username and password.

        Used to defer launching the Playwright browser until the user has
        provided credentials (typically via the web UI), to avoid the
        confusing state of two places asking for Facebook login at once.
        """
        assert self.config is not None
        for mp in self.config.marketplace.values():
            if getattr(mp, "enabled", True) is False:
                continue
            if not getattr(mp, "username", None) or not getattr(mp, "password", None):
                return False
        return True

    def _wait_for_marketplace_credentials(self: "MarketplaceMonitor") -> None:
        """Block until config has marketplace credentials.

        Reloads the config whenever the file changes on disk.
        No-op if credentials are already present.
        """
        assert self.config is not None
        while not self._has_marketplace_credentials():
            control.set_phase(
                "waiting_for_credentials",
                "Waiting for marketplace credentials before launching the browser.",
            )
            if self.logger:
                self.logger.info(
                    f"""{hilight("[Login]", "info")} Waiting for Facebook credentials. Sign in via the web UI or add username/password under [marketplace.facebook] in your config. The Playwright browser will launch once credentials are available.""",
                    extra=aimm_event("credentials_wait", status="waiting"),
                )
            # doze wakes up on file change OR keyboard interrupt OR timeout.
            doze(300, self.config_files, self.keyboard_monitor)
            # File may have changed — reload the config (non-fatal on parse error).
            try:
                self.load_config_file()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                if self.logger:
                    self.logger.debug(f"Config reload failed during credential wait: {e}")
                continue
        if self.logger:
            self.logger.info(
                f"""{hilight("[Login]", "succ")} Facebook credentials found — launching browser.""",
                extra=aimm_event("credentials_wait", status="found"),
            )

    def start_monitor(self: "MarketplaceMonitor") -> None:
        """Main function to monitor the marketplace."""
        # start a browser with playwright, cannot use with statement since the jobs will be
        # executed outside of the scope by schedule job runner
        self.keyboard_monitor = KeyboardMonitor()
        self.keyboard_monitor.start()
        # From here on, every checkpoint in the scraping code also asks whether
        # the configuration has moved under the search it is in the middle of.
        control.set_checkpoint_guard(self._config_guard)

        # Open a new browser page.
        self.load_config_file()
        assert self.config is not None
        # If requested (by the web UI), defer browser launch until
        # marketplace credentials are set. Without this, Playwright
        # navigates to the Facebook login page and waits for manual
        # input even though the user has a web UI open that's also
        # asking for those same credentials.
        if self.defer_login_until_credentials:
            self._wait_for_marketplace_credentials()
        # Before the browser, not after: starting up paused should cost nothing,
        # and a Playwright window opening on a paused monitor reads as a bug.
        self.wait_while_paused()
        # Same reasoning, for the same reason: a monitor with no searches has
        # nothing to open a browser for.
        self._wait_for_searches()
        while True:
            self.wait_while_paused()
            self.handle_pause()
            self._wait_for_searches()
            # `_wait_for_searches` returns early when the switch is thrown while
            # it waits, and what follows opens a browser: without this, pressing
            # "Detener" on a monitor with nothing to search opened a Chromium
            # window as its answer.  Back to the top, where a stop is waited out.
            if is_paused():
                continue
            self._ensure_browser()
            # Reviews with a lane of their own start here and keep going while
            # everything below searches: that concurrency is the whole point of
            # the setting.  A no-op when it is off.
            self._start_review_lane()
            # Rebuilt, not added to.  `schedule_jobs` registers jobs and the
            # `schedule` package keeps them for the life of the process, so a
            # search switched off while the monitor was stopped kept the entry
            # it had before the stop: the loop found a non-empty registry, took
            # the "idle" branch, and the interface reported "próxima búsqueda:
            # consola" for a search that could not run.  Clearing first is the
            # same step `_config_guard` takes when a change lands mid-pass, and
            # it costs nothing: `_seed_job_from_memory` puts each job's last run
            # back, so rebuilding does not hand anything a fresh interval.
            self._rebuild_schedule()
            self._publish_schedule()
            if not schedule.get_jobs():
                # Searches exist but none could be scheduled -- every one of
                # them is switched off, or its platform is.  Same answer as no
                # searches at all: wait for the file to change, quietly.
                control.set_phase(
                    "waiting_for_config",
                    "Nothing to search; stored listings are still reviewed.",
                )
                if self.logger:
                    self.logger.warning(
                        f"""{hilight("[Schedule]", "fail")} No search is scheduled: every """
                        """configured search is switched off, or everything configured is """
                        """a tracker, which is not a search."""
                    )
                # Nothing to *search* is not nothing to do, and since trackers
                # stopped being scheduled as searches this is the normal state
                # of a monitor that only follows pages rather than an odd one:
                # a tracker's first read and every re-read after it both happen
                # here.  Sleeping an hour through it -- which is what this did,
                # when the only way to be here was to switch every search off --
                # would be a monitor that never looks at the one thing it was
                # asked to watch.
                #
                # A loop of its own rather than going back round the outer one,
                # because the outer one begins by reloading the configuration
                # and rebuilding the schedule, and doing that once a minute to
                # discover the same emptiness again is exactly the wake-up-a-
                # minute this branch was written to avoid.
                while not schedule.get_jobs():
                    self.handle_pause()
                    self._ingest_trackers()
                    self._apply_pending_sessions()
                    # Re-asked every round: the lane is not started until there
                    # is something to re-check, and with a tracker just added
                    # the thing to re-check appears *here*, seconds after the
                    # ingest above stores it.
                    self._start_review_lane()
                    if not self._refresh_slice():
                        break
                    status = doze(
                        60,
                        self.config_files,
                        self.keyboard_monitor,
                        stop_when=lambda: is_paused() or control.run_pending(),
                    )
                    if status == SleepStatus.BY_KEYBOARD:
                        self.keyboard_monitor.set_paused(True)
                    if status in (SleepStatus.BY_KEYBOARD, SleepStatus.BY_FILE_CHANGE):
                        break
                    if is_paused() or control.run_pending():
                        break
                continue
            # Run what is actually ready, then let each search keep to its own
            # schedule.  Deliberately not "everything, now": starting the
            # monitor means letting it work, not overriding the intervals the
            # user set, and a search that ran four minutes before a restart is
            # not due again because the process came back.  What each search
            # last did is remembered across restarts (`_seed_job_from_memory`),
            # so this is a real answer rather than "nothing has run yet, so run
            # it all".  The explicit "buscar ahora" button is what overrides the
            # schedule, and it still does.
            if not self._run_due_jobs():
                # Back to the top, where the forced pause is waited out.
                continue
            if not schedule.get_jobs():
                continue
            # subsequent runs will be scheduled runs
            while True:
                if not schedule.get_jobs():
                    # Everything that was scheduled has since been deleted or
                    # switched off -- a configuration adopted while we waited.
                    # Back to the top, where having nothing to search is a state
                    # to wait in; falling through would take the "no more active
                    # job" exit below and stop the monitor over an empty list
                    # the user is perfectly entitled to have.
                    break
                next_job: schedule.Job | None = None
                for job in schedule.jobs:
                    if job.next_run is None:
                        continue
                    if next_job is None or (
                        next_job.next_run and next_job.next_run > job.next_run
                    ):
                        next_job = job

                if next_job is None:
                    # no more job
                    if self.logger:
                        self.logger.warning(
                            f"""{hilight("[Schedule]", "fail")} No more active search job."""
                        )
                    sys.exit(0)
                # assert next_job is not None
                assert next_job.next_run is not None
                self._publish_schedule()
                # Nothing is running, so no stop can still be waiting to be
                # honoured: a stop lasts for the pass it was made in, and there
                # is no pass.  The safety net under the two places that spend
                # them properly -- without it a stop that no pass ever reached
                # stayed in the register for ever, and the interface honestly
                # reported what it was told: a search "deteniendose..." whose
                # next run was quietly counting down beside it.
                control.clear_search_stops()
                idle_seconds = schedule.idle_seconds() or 0
                control.set_phase(
                    "idle",
                    f"""Next search: {next(iter(next_job.tags), "")} at """
                    f"""{next_job.next_run.strftime("%Y-%m-%d %H:%M:%S")}.""",
                )
                if idle_seconds > 60:
                    # the sleep time might not be enough, causing this message
                    # to be sent repeatedly. Having a idle_seconds > 60 helps
                    # to reduce the frequency of this message.
                    if self.logger:
                        self.logger.info(
                            f"""{hilight("[Schedule]", "info")} Next job to search {hilight(str(next(iter(next_job.tags))))} scheduled to run in {humanize.naturaldelta(idle_seconds)} at {next_job.next_run.strftime("%Y-%m-%d %H:%M:%S")}"""
                        )

                # Waiting for the next search is the browser's idle time, and
                # re-checking stored listings is exactly what it is for.  A
                # short slice, then back to sleep -- so a pause or a forced run
                # is still noticed within seconds.
                self._apply_pending_sessions()
                # A tracker whose first read never happened -- its browser
                # would not open, or the monitor was paused when it was added.
                # A no-op once every tracker is in the store, which is after
                # one read each, for ever.
                self._ingest_trackers()
                # Asked again here, not only at the top of the outer loop: the
                # review lane is not started until there is something to
                # re-check, and what makes something worth re-checking is a
                # search that has just stored listings -- which happens down
                # here, not up there.  A no-op once the lane is alive.
                self._start_review_lane()
                if not self._refresh_slice():
                    break
                idle_seconds = schedule.idle_seconds() or 0

                # Nothing to search for a while and nothing running: give the
                # windows and the Chromium processes back until there is.  The
                # next search opens them again on the same persistent profiles,
                # so this costs a browser start and no sign-in.
                self._release_idle_browsers(idle_seconds)

                # sleep at most 1 hr, and print updated "next job" message
                res = doze(
                    min(max(5, int(idle_seconds)), 60 * 60),
                    self.config_files,
                    self.keyboard_monitor,
                    # Cut the sleep short when the switch is thrown, so the
                    # "paused" line reaches the web UI's log while the user is
                    # still looking at the button they just pressed -- and when
                    # a search is asked for, so the button acts at once.  A
                    # search asked for *now* counts: without it "ejecutar ahora"
                    # on an idle monitor would be honoured at the end of a sleep
                    # that can be an hour long, which is not what it says.
                    stop_when=lambda: is_paused()
                    or control.run_pending()
                    or control.next_search_now() is not None,
                )
                # An explicit "search now" from the web UI.  Older versions had
                # to touch the config file to wake the monitor; this is the same
                # thing said directly, and it is refused while a search is
                # already running rather than stacking a second pass on it.
                if control.take_run_request():
                    if self.logger:
                        self.logger.info(
                            f"""{hilight("[Schedule]", "info")} Searching all items now, """
                            """on request.""",
                            extra=aimm_event("forced_run", source="web UI"),
                        )
                    if not self._run_all_jobs():
                        break
                    continue
                # A search the user promoted while nothing was running has
                # nothing to wait for: the "next" it was promised is now.
                #
                # Claimed here rather than peeked at, because the pass below is
                # a targeted one and targeted passes deliberately do not claim.
                # Peeking would leave the choice standing and promote the same
                # product on every turn round this loop, forever.
                #
                # Not while paused, though: the sleep above also ends on the
                # switch, and claiming here would spend the promise on a pass
                # that is about to be turned back at the top of the loop -- the
                # user's chosen search silently dropped by a pause, which is not
                # something a pause is supposed to do.  It waits, and is claimed
                # on the way out of the pause.
                chosen = None if is_paused() else control.take_next_search()
                if chosen is not None:
                    pairs = {pair for pair in self._scheduled_pairs() if pair[0] == chosen}
                    if pairs:
                        if self.logger:
                            self.logger.info(
                                f"""{hilight("[Schedule]", "info")} Searching """
                                f"""{hilight(chosen)} next, as asked.""",
                                extra=aimm_event("next_search", item=chosen),
                            )
                        if not self._run_jobs(only=pairs):
                            break
                        continue
                    # Promoted and then deleted, or switched off.  The claim
                    # above has already dropped the promise, which is the right
                    # answer: carrying it forever would be worse.
                # Adopted at a checkpoint that could not touch the schedule.
                if self._schedule_dirty:
                    self._schedule_dirty = False
                    self._rebuild_schedule()
                    self._publish_schedule()
                    continue
                if res == SleepStatus.BY_FILE_CHANGE:
                    probe = self._probe_config(force=True)
                    if probe is not None and probe.changed:
                        if not probe.readable:
                            # Broken or half-written: the loader reports it
                            # properly and waits for it to be fixed.
                            schedule.clear()
                            break
                        change = probe.change
                        self._adopt_config(probe)
                        self._rebuild_schedule()
                        # Only what the change touched.  The rest keep their
                        # places: editing one search is not a reason to re-run
                        # the others, and doing so is a burst of traffic the
                        # marketplace notices.
                        if change is not None and not self._run_changed(change):
                            break
                        continue
                    # Same content, only the timestamp moved. That is the web
                    # UI's "search now" button, which touches the config on
                    # purpose to wake us. Falling through here would send us
                    # straight back to sleep and make the button a no-op, so
                    # run every job now instead of waiting for its next slot.
                    if self.logger:
                        self.logger.info(
                            f"""{hilight("[Schedule]", "info")} Woken on request — searching all items now."""
                        )
                    if not self._run_all_jobs():
                        break
                elif res == SleepStatus.BY_KEYBOARD:
                    self.keyboard_monitor.set_paused(True)

                self.handle_pause()
                self.wait_while_paused()
                if not self._run_due_jobs():
                    break

    def interactive_login(self: "MarketplaceMonitor") -> bool:
        """Sign in to every enabled marketplace by hand and save the session.

        The scheduled path has to give up on a login eventually, or a stuck
        challenge would wedge the monitor forever.  That deadline is exactly
        wrong when a site keeps looping the challenge: every attempt is abandoned
        half-way and nothing is ever saved.  This runs the same login with no
        deadline and a visible browser, so the sign-in can be finished properly
        once and reused from then on.
        """
        self.load_config_file()
        assert self.config is not None
        self.context = self._launch_context()

        signed_in = 0
        attempted = 0
        for marketplace_config in self.config.marketplace.values():
            if marketplace_config.enabled is False:
                continue
            attempted += 1
            marketplace_class = all_marketplaces[
                (marketplace_config.market_type or "facebook").lower()
            ]
            marketplace = marketplace_class(marketplace_config.name, self.context, None, self.logger)
            self._prepare_tracked(marketplace)
            marketplace.configure(
                marketplace_config,
                translator=self._select_translator(marketplace_config.language),
            )
            if self.logger:
                self.logger.info(
                    f"""{hilight("[Login]", "info")} Opening {hilight(marketplace_config.name)} — """
                    """finish the sign-in in the browser window. """
                    """Press Ctrl-C here to give up."""
                )
            try:
                # A long ceiling rather than a true infinity: this still returns
                # the moment the session goes live, so the number only bounds an
                # abandoned attempt.
                if marketplace.login_interactively(timeout=3600):
                    signed_in += 1
                    if self.logger:
                        self.logger.info(
                            f"""{hilight("[Login]", "succ")} Session for {hilight(marketplace_config.name)} saved."""
                        )
                elif self.logger:
                    self.logger.error(
                        f"""{hilight("[Login]", "fail")} Gave up on {hilight(marketplace_config.name)}; nothing saved."""
                    )
            except KeyboardInterrupt:
                if self.logger:
                    self.logger.warning(f"""{hilight("[Login]", "fail")} Cancelled.""")
                break
            except NotImplementedError:
                # A marketplace with no concept of signing in. Not a failure of
                # this command, and certainly not a reason to abandon the ones
                # that do have one.
                attempted -= 1
                if self.logger:
                    self.logger.info(
                        f"""{hilight("[Login]", "info")} {marketplace_config.name} needs no """
                        """sign-in; skipping it."""
                    )

        if attempted == 0 and self.logger:
            self.logger.error(
                f"""{hilight("[Login]", "fail")} No enabled marketplace in the configuration."""
            )
        return signed_in > 0

    def stop_monitor(self: "MarketplaceMonitor") -> None:
        """Stop the monitor."""
        # Before the browsers, because this one waits for something to finish
        # rather than for something to die: a listing found a second ago is
        # still queued, and dropping it would make "notify immediately" quietly
        # untrue at exactly the moment it mattered.
        self.notifier.close()
        # The lanes first: each closes its own browser on its own thread, which
        # is the only thread allowed to, and a lane still holding a page would
        # keep a Chromium process alive after this returns.
        self._stop_review_lane()
        self._close_lanes()
        for marketplace in self.active_marketplaces.values():
            marketplace.stop()
        # Close the persistent context so Chromium flushes cookies and the rest
        # of the profile to disk; an abandoned profile can lose the session that
        # persistence exists to keep.
        if self.context is not None:
            try:
                self.context.close()
            except Exception:
                pass
            self.context = None
        self.playwright.stop()
        if self.keyboard_monitor:
            self.keyboard_monitor.stop()
        cache.close()

    def check_items(
        self: "MarketplaceMonitor", items: List[str] | None = None, for_item: str | None = None
    ) -> None:
        """Main function to monitor the marketplace."""
        # we reload the config file each time when a scan action is completed
        # this allows users to add/remove products dynamically.
        self.load_config_file()

        if for_item is not None:
            assert self.config is not None
            if for_item not in self.config.item:
                raise ValueError(
                    f"Item {for_item} not found in config, available items are {', '.join(self.config.item.keys())}."
                )

        self.load_ai_agents()

        post_urls = []
        for post_url in items or []:
            if post_url.isnumeric():
                post_url = f"https://www.facebook.com/marketplace/item/{post_url}/"

            if not any(
                marketplace.handles_url(post_url)
                for marketplace in supported_marketplaces.values()
            ):
                raise ValueError(f"URL {post_url} does not belong to a supported marketplace.")
            post_urls.append(post_url)

        if not post_urls:
            raise ValueError("No URLs to check.")

        # Open a new browser page.
        for post_url in post_urls or []:
            # check if item in config
            assert self.config is not None

            # which marketplace to check it?
            for marketplace_config in self.config.marketplace.values():
                if marketplace_config.enabled is False:
                    continue
                marketplace_class = all_marketplaces[
                (marketplace_config.market_type or "facebook").lower()
            ]
                if marketplace_config.name in self.active_marketplaces:
                    marketplace = self.active_marketplaces[marketplace_config.name]
                else:
                    marketplace = marketplace_class(
                        marketplace_config.name, None, None, self.logger
                    )
                    self.active_marketplaces[marketplace_config.name] = marketplace

                if not marketplace_class.handles_url(post_url):
                    # Another marketplace's URL: it cannot read that page.
                    continue

                # Configure might have been changed
                self._prepare_tracked(marketplace)
                marketplace.configure(
                    marketplace_config,
                    translator=self._select_translator(marketplace_config.language),
                )

                # do we need a browser?
                if Listing.from_cache(post_url) is None:
                    if self.context is None:
                        if self.logger:
                            self.logger.info(
                                f"""{hilight("[Search]", "info")} Starting a browser because the item was not checked before."""
                            )
                        self.context = self._launch_context()
                        marketplace.set_context(self.context)

                # ignore enabled
                if for_item is None:
                    # get by asking user
                    name = None
                    item_names = list(self.config.item.keys())
                    if len(item_names) > 1:
                        name = Prompt.ask(
                            f"""Enter name of {hilight("search item")}""", choices=item_names
                        )
                    item_config = self.config.item[name or item_names[0]]
                else:
                    item_config = self.config.item[for_item]

                # do not search, get the item details directly
                listing_result = marketplace.get_listing_details(post_url, item_config)

                # get_listing_details returns a tuple (Listing, bool) - unpack it properly
                if isinstance(listing_result, tuple) and len(listing_result) == 2:
                    listing, from_cache = listing_result
                else:
                    # Fallback - treat as direct listing (shouldn't happen but defensive)
                    listing = listing_result

                if self.logger:
                    self.logger.info(
                        f"""{hilight("[Retrieve]", "succ")} Details of the item is found: {pretty_repr(listing)}"""
                    )

                if self.logger:
                    self.logger.info(
                        f"""{hilight("[Search]", "succ")} Checking {post_url} for item {item_config.name} with configuration {pretty_repr(item_config)}"""
                    )
                matched = marketplace.check_listing(listing, item_config)
                record_observation(listing, matched=matched, item_name=item_config.name)
                rating = self.evaluate_by_ai(
                    listing, item_config=item_config, marketplace_config=marketplace_config
                )
                record_rating(
                    listing,
                    score=rating.score,
                    comment=rating.comment,
                    conclusion=rating.conclusion,
                    ai_name=rating.name,
                )
                if self.logger:
                    if rating.comment == AIResponse.NOT_EVALUATED:
                        if rating.name:
                            self.logger.info(
                                f"""{hilight("[AI]", rating.style)} {rating.name or "AI"} did not evaluate {hilight(listing.title)}."""
                            )
                        else:
                            self.logger.info(
                                f"""{hilight("[AI]", rating.style)} No AI available to evaluate {hilight(listing.title)}."""
                            )
                    else:
                        self.logger.info(
                            f"""{hilight("[AI]", rating.style)} {rating.name or "AI"} concludes {hilight(f"{rating.conclusion} ({rating.score}): {rating.comment}", rating.style)} for listing {hilight(listing.title)}."""
                        )
                # notification status?
                users_to_notify = (
                    item_config.notify
                    or marketplace_config.notify
                    or list(self.config.user.keys())
                )
                # for notification usages
                listing.name = item_config.name
                for user in users_to_notify:
                    ns = User(self.config.user[user], self.logger).notification_status(listing)
                    if self.logger:
                        if ns == NotificationStatus.NOTIFIED:
                            self.logger.info(
                                f"""{hilight("[Notify]", "succ")} Notified {user} about {post_url}."""
                            )
                        elif ns == NotificationStatus.EXPIRED:
                            self.logger.info(
                                f"""{hilight("[Notify]", "info")} Already notified {user} about {post_url}. The notification is ow expired."""
                            )
                        elif ns == NotificationStatus.LISTING_CHANGED:
                            self.logger.info(
                                f"""{hilight("[Notify]", "info")} Already notified {user} about {post_url}, but the listing is now changed."""
                            )
                        elif ns == NotificationStatus.LISTING_DISCOUNTED:
                            self.logger.info(
                                f"""{hilight("[Notify]", "info")} Already notified {user} about {post_url}, but the listing is now discounted."""
                            )
                        else:
                            self.logger.info(
                                f"""{hilight("[Notify]", "info")} Not notified {user} about {post_url} yet."""
                            )

                    # testing notification
                    # User(self.config.user[user], logger=self.logger).notify(
                    #     [listing], [rating], item_config, force=True
                    # )

    def evaluate_by_ai(
        self: "MarketplaceMonitor",
        item: Listing,
        item_config: TItemConfig,
        marketplace_config: TMarketplaceConfig,
    ) -> AIResponse:
        if item_config.ai is not None:
            ai_agents = item_config.ai
        elif marketplace_config.ai is not None:
            ai_agents = marketplace_config.ai
        else:
            ai_agents = None
        #
        for agent in self.ai_agents:
            if ai_agents is not None and agent.config.name not in ai_agents:
                continue
            try:
                return agent.evaluate(item, item_config, marketplace_config)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                if self.logger:
                    self.logger.error(
                        f"""{hilight("[AI]", "fail")} Failed to get an answer from {agent.config.name}: {e}"""
                    )
                continue
        return AIResponse(5, AIResponse.NOT_EVALUATED)
