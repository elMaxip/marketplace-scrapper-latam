"""Re-visiting listings the monitor has already stored.

Searching answers "what is new?".  It says nothing about the listings already in
the store, which quietly rot: the price moves, the seller edits the title, the
item sells, the post is taken down.  This module is the second kind of work the
browser does -- walking the stored listings oldest-check-first and bringing each
one up to date.

Three rules shape it:

* **Do not re-check what was just checked.**  Freshness is decided from the
  observation's ``last_seen`` -- which is exactly "when we last looked at this"
  and already exists -- rather than from a second timestamp meaning the same
  thing.  This is about not repeating *this* flow's own work within one recheck
  interval; the search no longer opens stored listings at all, so there is
  nothing of its to collide with (see
  :func:`~ai_marketplace_monitor.observations.is_known`).
* **Never let two flows work on the same listing.**  The search flow and the
  refresher can both reach the same listing; :mod:`ai_marketplace_monitor.control`
  hands out an exclusive claim and the loser skips rather than waits.
* **Only delete on evidence.**  A sold badge or a "this content isn't available"
  page removes the listing for good.  A timeout, a network error, a rate limit,
  a login wall or an unparseable layout all come back as
  :attr:`~ai_marketplace_monitor.marketplace.ListingStatus.UNKNOWN` and change
  nothing at all -- the listing is simply tried again later.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from logging import Logger
from typing import Any, Callable, Dict, List, Optional, Tuple

from diskcache import Cache  # type: ignore

from . import control
from .control import claim
from .listing import Listing
from .marketplace import ItemConfig, ListingStatus, Marketplace
from .observations import delete_observations, get_observation, iter_observations, record_observation
from .utils import CacheType, CounterItem, aimm_event, cache, counter, hilight, price_value

#: How stale a listing has to be before re-opening its page is worth the traffic.
DEFAULT_RECHECK_INTERVAL = 6 * 60 * 60

#: Listings per slice.  The refresher runs in slices so it can be interleaved
#: with searching and abandoned promptly when the monitor is paused; a small
#: number keeps any single burst of listing-page traffic modest.
DEFAULT_SLICE_SIZE = 10

#: Shortest gap between two slices, and between two listing pages inside one.
#:
#: Without these the refresher would empty a backlog of hundreds of stale
#: listings as fast as the browser could load pages -- a burst of listing-page
#: traffic from one session that looks nothing like a person browsing, which is
#: the precise behaviour this whole feature is meant to avoid.  The search flow
#: paces itself the same way (a five-second wait after every listing it opens).
MIN_SECONDS_BETWEEN_SLICES = 60
SECONDS_BETWEEN_LISTINGS = 5

#: How long a listing that failed for an unclear reason is left alone.  Kept in
#: memory rather than in the store: a failed attempt is not an observation, and
#: writing one would bump the record's revision and make every dashboard re-sync
#: a row that did not change.
FAILURE_COOLDOWN = 30 * 60


def _parsed(iso: str | None) -> float:
    """Epoch seconds from an ISO timestamp, or 0 when there is none."""
    if not iso:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


def last_checked(record: Dict[str, Any]) -> float:
    """When this listing was last looked at, as epoch seconds.

    ``last_seen`` is that timestamp: both the search flow and this one write it
    through :func:`~ai_marketplace_monitor.observations.record_observation` every
    time they read the listing.
    """
    return _parsed(record.get("last_seen"))


def was_checked_recently(
    marketplace: str,
    listing_id: str,
    within: float,
    local_cache: Cache | None = None,
    now: float | None = None,
) -> bool:
    """Whether this listing was looked at less than ``within`` seconds ago.

    Consulted by both flows before they open a listing page, which is what keeps
    a search that keeps turning up the same listing from re-reading it on every
    cycle.
    """
    record = get_observation(marketplace, listing_id, local_cache=local_cache)
    if not isinstance(record, dict) or record.get("deleted"):
        return False
    stamp = last_checked(record)
    if not stamp:
        return False
    return (time.time() if now is None else now) - stamp < within


def item_name_of(record: Dict[str, Any]) -> Optional[str]:
    """The search a stored listing turned up under, or None.

    ``items`` accumulates across sightings because one listing can match several
    searches; the snapshot's ``name`` is the same thing recorded on the listing
    itself, and is the fallback for a record written before ``items`` existed.
    """
    snapshot = record.get("listing") if isinstance(record.get("listing"), dict) else {}
    return (record.get("items") or [None])[0] or snapshot.get("name") or None


def stale_records(
    local_cache: Cache | None,
    within: float,
    marketplaces: Tuple[str, ...],
    limit: int | None = None,
    skip: Callable[[str, str], bool] | None = None,
    now: float | None = None,
) -> List[Dict[str, Any]]:
    """The listings overdue for a re-check, longest-overdue first.

    ``marketplaces`` filters to the platforms that can actually be re-read right
    now; a listing from a marketplace that has since been removed from the
    config is left untouched rather than dropped.  ``limit`` of ``None`` returns
    every candidate, which is what lets the caller rank them before choosing.
    """
    moment = time.time() if now is None else now
    candidates: List[Tuple[float, Dict[str, Any]]] = []
    for record in iter_observations(local_cache):
        if record.get("deleted"):
            continue
        marketplace = str(record.get("marketplace") or "")
        listing_id = str(record.get("id") or "")
        if not marketplace or not listing_id or marketplace not in marketplaces:
            continue
        if skip is not None and skip(marketplace, listing_id):
            continue
        stamp = last_checked(record)
        # A record with no usable timestamp sorts first: it is the one we know
        # least about.
        if stamp and moment - stamp < within:
            continue
        candidates.append((stamp, record))

    candidates.sort(key=lambda entry: entry[0])
    ordered = [record for _stamp, record in candidates]
    return ordered if limit is None else ordered[: max(0, limit)]


def _interleave(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deal the candidates out one marketplace at a time, round-robin.

    Order within a marketplace is preserved; only the interleaving is new.  A
    slice that walked its queue as sorted would fire every one of its page loads
    at whichever site happens to hold the oldest records -- and a burst of
    listing-page requests from one session is exactly what makes a marketplace
    start asking who we are.  Alternating halves the rate each site sees for the
    same amount of work done.
    """
    queues: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        queues.setdefault(str(record.get("marketplace")), []).append(record)
    if len(queues) < 2:
        return records

    dealt: List[Dict[str, Any]] = []
    order = list(queues)
    while any(queues[name] for name in order):
        for name in order:
            if queues[name]:
                dealt.append(queues[name].pop(0))
    return dealt


def _placeholder_config(item_name: Optional[str]) -> ItemConfig:
    """A stand-in for a search that is no longer in the configuration file.

    Carries a name and nothing else: the only thing the re-check asks of it is
    what to label a counter with.  Its filters are never applied -- see
    :meth:`ListingRefresher._refresh_one`.
    """
    name = item_name or "unknown"
    # ``ItemConfig`` insists on a search phrase.  Nothing here searches.
    return ItemConfig(name=name, search_phrases=[name])


@dataclass
class PriceDrop:
    """One listing that is cheaper than the last time it was read.

    Reported rather than acted on, because who to tell and whether they asked to
    hear it are the monitor's questions, not this module's -- the refresher has
    no users, no reasons and no notification cache.  What it does have, and
    nothing else does, is *both prices*: the one on the page now and the one
    that was stored a moment ago.
    """

    listing: Listing
    #: The price the store held before this re-check.
    previous: str
    #: The search this listing belongs to, or None when it has been deleted.
    item_name: Optional[str]


@dataclass
class RefreshReport:
    """What one slice did.  Summed by the caller for the log line."""

    checked: int = 0
    updated: int = 0
    removed: int = 0
    skipped: int = 0
    failed: int = 0
    removed_keys: List[Tuple[str, str]] = field(default_factory=list)
    #: The listings that got cheaper in this slice.
    #:
    #: A re-check is the *only* place a price moves -- the search never opens a
    #: listing it already knows -- so if this does not travel out of here, the
    #: fact that something got cheaper is known to the log and to nobody else.
    drops: List["PriceDrop"] = field(default_factory=list)
    #: Why listings were skipped, by reason.  A slice that does nothing has to
    #: be able to say why: a silent skip reads exactly like an empty queue, and
    #: the two call for opposite reactions.
    skips: Dict[str, int] = field(default_factory=dict)

    def __bool__(self: "RefreshReport") -> bool:
        return bool(self.checked or self.removed or self.failed or self.skipped)

    def skip(self: "RefreshReport", reason: str) -> None:
        self.skipped += 1
        self.skips[reason] = self.skips.get(reason, 0) + 1

    def why_skipped(self: "RefreshReport") -> str:
        """The skip reasons in one phrase, for the log line."""
        return ", ".join(f"{count} {reason}" for reason, count in sorted(self.skips.items()))


def is_cheaper(previous: Any, current: Any) -> bool:
    """Whether ``current`` is a smaller amount than ``previous``.

    Compared as numbers, never as text: prices are stored exactly as the site
    printed them, so "$1.099.990" and "1099990" are the same amount written two
    ways and a string comparison would call that a change.  Anything that does
    not parse on either side is not a fall -- a price that cannot be read is not
    a cheaper one, it is one we do not know.
    """
    before = price_value(None if previous is None else str(previous))
    after = price_value(None if current is None else str(current))
    if before is None or after is None:
        return False
    return after < before


class ListingRefresher:
    """Brings stored listings up to date, a slice at a time.

    It owns no browser of its own.  ``marketplace_for`` hands it a configured
    :class:`~ai_marketplace_monitor.marketplace.Marketplace`, and *which* one is
    the monitor's decision: the same instance the search uses (one tab, work
    alternating) or a second instance on the same browser context (its own tab).
    That single choice is what the ``parallel_listing_updates`` setting selects.
    """

    def __init__(
        self: "ListingRefresher",
        marketplace_for: Callable[[str], Optional[Marketplace]],
        item_config_for: Callable[[str, Optional[str]], Optional[ItemConfig]],
        logger: Logger | None = None,
        recheck_interval: float | None = None,
        local_cache: Cache | None = None,
        stop_when: Callable[[], bool] | None = None,
    ) -> None:
        self.marketplace_for = marketplace_for
        self.item_config_for = item_config_for
        self.logger = logger
        self.recheck_interval = (
            DEFAULT_RECHECK_INTERVAL if recheck_interval is None else float(recheck_interval)
        )
        self.local_cache = local_cache
        self.stop_when = stop_when
        self.slice_interval = float(MIN_SECONDS_BETWEEN_SLICES)
        self.listing_interval = float(SECONDS_BETWEEN_LISTINGS)
        #: key -> epoch seconds before which not to try again.
        self._cooldown: Dict[Tuple[str, str], float] = {}
        self._next_slice = 0.0
        #: How many stored listings were overdue when the queue was last built.
        #: Reported to the web UI, which otherwise has no way to tell a backlog
        #: of hundreds from an empty queue -- both look like "nothing happened".
        self.pending: int = 0

    # ------------------------------------------------------------------ #
    # Selection
    # ------------------------------------------------------------------ #

    def _cooling(self: "ListingRefresher", marketplace: str, listing_id: str) -> bool:
        until = self._cooldown.get((marketplace, listing_id))
        if until is None:
            return False
        if until <= time.time():
            del self._cooldown[(marketplace, listing_id)]
            return False
        return True

    def _orphaned(self: "ListingRefresher", record: Dict[str, Any]) -> bool:
        """Whether the search this listing came from is no longer configured."""
        return (
            self.item_config_for(str(record.get("marketplace")), item_name_of(record)) is None
        )

    def due(
        self: "ListingRefresher", marketplaces: Tuple[str, ...], limit: int
    ) -> List[Dict[str, Any]]:
        """Which listings this slice should look at, most deserving first.

        Longest-overdue first, except that a listing whose search is still
        configured always outranks one whose search has been deleted or renamed.
        Both are re-checked eventually; the ranking only means that a backlog of
        listings from a search the user has since dropped -- which is the oldest
        thing in the store, precisely because nothing has touched it -- cannot
        hold up the search they are actually running.
        """
        candidates = stale_records(
            self.local_cache,
            within=self.recheck_interval,
            marketplaces=marketplaces,
            limit=None,
            skip=self._cooling,
        )
        candidates.sort(key=lambda record: (self._orphaned(record), last_checked(record)))
        self.pending = len(candidates)
        control.updates_pending(self.pending)
        # More than the budget on purpose: skips must not consume slots, or a
        # handful of unskippable listings at the front would stall the queue for
        # good.  ``run_slice`` stops once it has done ``limit`` real checks.
        return _interleave(candidates)[: max(0, limit) * 4]

    # ------------------------------------------------------------------ #
    # Doing the work
    # ------------------------------------------------------------------ #

    def run_slice(
        self: "ListingRefresher",
        marketplaces: Tuple[str, ...],
        limit: int = DEFAULT_SLICE_SIZE,
    ) -> RefreshReport:
        """Re-check up to ``limit`` overdue listings and report what happened.

        Callable as often as the monitor likes: slices closer together than
        ``slice_interval`` do nothing and report nothing, which is what lets the
        idle loop simply ask on every pass without having to know the pacing.
        """
        report = RefreshReport()
        if not marketplaces or limit <= 0:
            return report
        now = time.time()
        if now < self._next_slice:
            return report
        self._next_slice = now + self.slice_interval

        due = self.due(marketplaces, limit)
        try:
            self._run_due(due, limit, report)
        finally:
            control.updates_current(None, None)
            control.updates_done(
                {
                    "checked": report.checked,
                    "updated": report.updated,
                    "removed": report.removed,
                    "failed": report.failed,
                    "skipped": report.skipped,
                    "pending": self.pending,
                }
            )
        return report

    def _run_due(
        self: "ListingRefresher",
        due: List[Dict[str, Any]],
        limit: int,
        report: RefreshReport,
    ) -> None:
        """Work through the queue this slice built, up to its budget."""
        first = True
        for record in due:
            # The budget is spent on work, not on candidates: a slice that hits
            # nothing but skips has done nothing, and must keep looking.
            if report.checked >= limit:
                break
            if self.stop_when is not None and self.stop_when():
                break
            marketplace_name = str(record.get("marketplace"))
            listing_id = str(record.get("id"))
            with claim(marketplace_name, listing_id) as mine:
                if not mine:
                    # Another flow has this one open right now.
                    report.skip("being read by the search")
                    continue
                # Asked again, here, and not only when the queue was built.
                # The queue is a snapshot: with a search running at the same
                # time as this -- which is the whole point of giving the review
                # a browser of its own -- a listing can be read between the
                # snapshot and its turn, and re-reading it would be the same
                # page load done twice minutes apart.  The claim above cannot
                # catch that: it is let go the moment the other flow finishes.
                if was_checked_recently(
                    marketplace_name,
                    listing_id,
                    within=self.recheck_interval,
                    local_cache=self.local_cache,
                ):
                    report.skip("already re-read since the queue was built")
                    continue
                if not first and self.listing_interval > 0:
                    time.sleep(self.listing_interval)
                first = False
                control.updates_current(marketplace_name, listing_id)
                self._refresh_one(record, report)

    def _refresh_one(
        self: "ListingRefresher", record: Dict[str, Any], report: RefreshReport
    ) -> None:
        marketplace_name = str(record.get("marketplace"))
        listing_id = str(record.get("id"))
        snapshot = record.get("listing") if isinstance(record.get("listing"), dict) else {}
        post_url = str(snapshot.get("post_url") or "")
        if not post_url:
            report.skip("with no URL stored")
            return

        marketplace = self.marketplace_for(marketplace_name)
        if marketplace is None:
            report.skip(f"from the unconfigured marketplace {marketplace_name!r}")
            return

        item_name = item_name_of(record)
        item_config = self.item_config_for(marketplace_name, item_name)
        # A listing whose search has been deleted or renamed is still a listing
        # the user is looking at, and re-checking it needs nothing from that
        # search: the price is on the page and so is the sold badge.  Only the
        # keep/discard verdict does, and that is left exactly as it was rather
        # than recomputed from some other product's filters.
        judge = item_config is not None
        if item_config is None:
            item_config = _placeholder_config(item_name)

        report.checked += 1
        try:
            status, details = marketplace.recheck_listing(post_url, item_config)
        except KeyboardInterrupt:
            raise
        except Exception as error:
            # Any exception is by definition an unclear failure -- a timeout, a
            # dropped connection, a Playwright error.  None of them says the
            # listing is gone.
            self._back_off(marketplace_name, listing_id)
            report.failed += 1
            if self.logger:
                self.logger.debug(
                    f"""{hilight("[Update]", "fail")} Could not re-check {post_url}: {error}""",
                    exc_info=True,
                )
            return

        if status in (ListingStatus.SOLD, ListingStatus.GONE):
            self._remove(record, post_url, status, report)
            return

        if status is ListingStatus.UNKNOWN or details is None:
            self._back_off(marketplace_name, listing_id)
            report.failed += 1
            if self.logger:
                self.logger.debug(
                    f"""{hilight("[Update]", "info")} No verdict for {post_url}; leaving it as it was."""
                )
            return

        self._store(details, marketplace, item_config, item_name, record, report, judge=judge)

    def _back_off(self: "ListingRefresher", marketplace: str, listing_id: str) -> None:
        self._cooldown[(marketplace, listing_id)] = time.time() + FAILURE_COOLDOWN

    def _store(
        self: "ListingRefresher",
        details: Listing,
        marketplace: Marketplace,
        item_config: ItemConfig,
        item_name: Optional[str],
        record: Dict[str, Any],
        report: RefreshReport,
        judge: bool = True,
    ) -> None:
        """Record the refreshed snapshot, which is what moves the price series."""
        if item_name:
            # Keeps the stored snapshot's own idea of its search item intact;
            # the parsed page has no way to know it.
            details.name = item_name
        matched = bool(record.get("matched", True))
        if judge:
            try:
                matched = bool(marketplace.check_listing(details, item_config))
            except KeyboardInterrupt:
                raise
            except Exception:
                # The filters are the search's business; if they cannot be
                # applied here, keep whatever verdict the listing carried.
                matched = bool(record.get("matched", True))

        previous = (record.get("listing") or {}).get("price")
        record_observation(
            details, matched=matched, item_name=item_name, local_cache=self.local_cache
        )
        report.updated += 1
        if matched and is_cheaper(previous, details.price):
            # Only a listing that still passes its filters: one the search would
            # have thrown away is not a bargain, it is a rejection that happens
            # to cost less.  `check_listing` above has already dropped junk
            # prices, which is what keeps a page reporting 999999 while it loads
            # from being read as a price and then as a spectacular fall.
            report.drops.append(
                PriceDrop(listing=details, previous=str(previous), item_name=item_name)
            )
        if self.logger and previous and previous != details.price:
            self.logger.info(
                f"""{hilight("[Update]", "succ")} {hilight(details.title)} changed price: """
                f"""{previous} to {details.price}.""",
                extra=aimm_event(
                    "listing_price_change",
                    listing_id=details.id,
                    marketplace=details.marketplace,
                    item=item_name or "",
                    title=details.title,
                    price_from=previous,
                    price_to=details.price,
                    url=details.post_url,
                ),
            )

    def _remove(
        self: "ListingRefresher",
        record: Dict[str, Any],
        post_url: str,
        status: ListingStatus,
        report: RefreshReport,
    ) -> None:
        """Drop a listing the marketplace says no longer exists.

        A tombstone rather than an erasure, which is what stops the next search
        from putting it straight back (see
        :func:`~ai_marketplace_monitor.observations.delete_observations`).  The
        cached page details go too, so nothing serves the dead snapshot.
        """
        marketplace_name = str(record.get("marketplace"))
        listing_id = str(record.get("id"))
        deleted, _revision = delete_observations(
            [(marketplace_name, listing_id)], local_cache=self.local_cache
        )
        try:
            (cache if self.local_cache is None else self.local_cache).delete(
                (CacheType.LISTING_DETAILS.value, post_url.split("?")[0])
            )
        except KeyboardInterrupt:
            raise
        except Exception:
            if self.logger:
                self.logger.debug("Could not drop cached details for %s", post_url, exc_info=True)

        if not deleted:
            report.skip("already removed")
            return

        report.removed += 1
        report.removed_keys.append((marketplace_name, listing_id))
        counter.increment(CounterItem.LISTING_REMOVED, str((record.get("items") or [""])[0]))
        title = str((record.get("listing") or {}).get("title") or listing_id)
        if self.logger:
            self.logger.info(
                f"""{hilight("[Update]", "info")} Removed {hilight(title)}: """
                f"""{"sold" if status is ListingStatus.SOLD else "the listing no longer exists"}.""",
                extra=aimm_event(
                    "listing_removed",
                    reason=status.value,
                    listing_id=listing_id,
                    marketplace=marketplace_name,
                    title=title,
                    url=post_url,
                ),
            )
