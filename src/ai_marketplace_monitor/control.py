"""Live control of the scraping loop, shared by the web UI thread and the monitor.

:mod:`ai_marketplace_monitor.pause` answers "should anything new start?" and is
persisted, because a pause has to survive a restart.  This module answers the
questions that only make sense while the process is up, and is therefore purely
in memory:

* **Is a scrape running right now, and what is it doing?**  The web UI shows it,
  and "force a scrape" needs it so a second run cannot be stacked on top of one
  already under way.
* **Has someone asked for a scrape right now?**  A flag the monitor picks up at
  its next checkpoint, rather than the older trick of touching the config file
  to wake the sleeper.
* **Has someone asked the running scrape to stop?**  Playwright's sync API is
  bound to the thread that created it, so the web UI cannot close a page or a
  context itself.  Cancellation is therefore cooperative: this module carries
  the request, the scraping thread notices it at a checkpoint, unwinds, and
  closes its own browser.
* **Is the search under way still the one the user wants?**  The configuration
  can be saved at any moment, including in the middle of a search.  A checkpoint
  cannot answer that by itself -- it means reading files, which only the scraping
  thread may do -- so the monitor installs a guard here and the checkpoints ask
  it.  When the answer is no, the search is abandoned and the next one starts.
* **Which change did the loop just take up, and what did it cost?**  So the
  interface can say "your change is in use now", and say so honestly, rather
  than the user watching two hashes and inferring it.
* **Who is currently re-checking a given listing?**  The search flow and the
  listing-refresh flow can both land on the same listing, and they must not
  fetch it twice (nor, worse, both decide to delete it).
* **Has a marketplace started refusing us?**  When a site answers with a sign-in
  wall instead of the page asked for, the worst possible response is to keep
  asking.  A marketplace can be put on a cooldown here, which every flow reads
  before it opens a page -- and both flows have to read the *same* one, or the
  second tab would keep hammering a site the first one already knows is angry.
* **What is the loop doing, and on which configuration?**  The phase it is in,
  the configuration version it actually loaded, when each search last ran and
  when it runs next, and how the listing updates are getting on.  All of it is
  written by the scraping thread and only read by the web UI, so the interface
  reports what the monitor *is* doing rather than guessing from the file the
  user just saved.  This is the one place that state lives: a second copy kept
  by the interface is a copy that drifts.

Everything here is safe to call from any thread.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple


class ScrapeInterrupted(Exception):
    """Base for the two ways a search can be abandoned part-way through.

    Both are raised at checkpoints, never inside a half-written record, and
    both are deliberately *not* ``KeyboardInterrupt``: the scraping code lets
    that one through untouched, and neither of these is a user pressing Ctrl-C.
    They are kept apart because what the loop does next differs -- one closes
    the browser and stops, the other picks up the next search.
    """


class CancelledScrape(ScrapeInterrupted):
    """Raised at a checkpoint when the running scrape was told to stop.

    Caught by the monitor loop, which unwinds to a clean state and closes the
    browser.
    """


class SearchSuperseded(ScrapeInterrupted):
    """Raised at a checkpoint when the configuration moved under the search.

    Only when the change touches *this* search: it was deleted, switched off,
    edited, or its platform's settings changed.  Finishing would spend a page
    load and a round of AI calls producing results judged against settings the
    user has already replaced, so the loop drops it and goes on to the next
    search -- with the browser left open, because nothing is wrong.
    """

    def __init__(
        self: "SearchSuperseded",
        item: str = "",
        marketplace: str = "",
        reason: str = "",
    ) -> None:
        super().__init__(
            f"""The search for {item or "?"} on {marketplace or "?"} was """
            f"""{reason or "superseded"} while it was running."""
        )
        self.item = item
        self.marketplace = marketplace
        self.reason = reason


class SearchStopped(ScrapeInterrupted):
    """Raised at a checkpoint when the user ended *this* search from the web UI.

    The sibling of :class:`SearchSuperseded`, and handled identically by the
    loop: the browser stays open and the next search starts.  It is kept apart
    because the two mean different things to the person reading the log -- one
    is the configuration moving under a search, the other is somebody pressing
    a button that says "stop this one and go on" -- and because only this one
    can name a platform without naming a change.
    """

    def __init__(
        self: "SearchStopped",
        item: str = "",
        marketplace: str = "",
        scope: str = "search",
    ) -> None:
        super().__init__(
            f"""The search for {item or "?"} on {marketplace or "?"} was ended """
            """from the web UI."""
        )
        self.item = item
        self.marketplace = marketplace
        #: ``"platform"`` when only this platform was stopped, ``"search"``
        #: when every platform of the product was.
        self.scope = scope


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def has_passed(stamp: str | None) -> bool:
    """Whether this moment is already behind us.

    Parsed rather than compared as text, and that is a fix rather than a
    refinement.  The two timestamps being compared are written by different
    parts of the program and carry *different offsets*: this module stamps in
    UTC, while the scheduler publishes a slot as local time with its own offset
    (``2026-08-23T13:37:55-04:00``).  Comparing those as strings compares two
    wall clocks in two zones, which in Chile made every waiting search look
    permanently overdue and, four hours the other way, would make an overdue
    one look like it had not come round yet.  Either way the interface is
    saying something that is not true about when the next search is.

    A timestamp that cannot be read is treated as still to come: "we do not
    know" must not render as "it is late".
    """
    if not stamp:
        return False
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if when.tzinfo is None:
        # Written before offsets were published, or by something that does not
        # send one: local time is the only reading that was ever meant.
        when = when.astimezone()
    return when <= datetime.now(timezone.utc)


_lock = threading.Lock()

#: The name of the lane the monitor thread itself drives.  There is always one,
#: and when nothing runs in parallel it is the only one.
MAIN_LANE = "main"

#: The lane the listing re-checks run on when they are given one of their own.
UPDATES_LANE = "updates"

#: lane name -> what that lane is doing, absent when the lane is between jobs.
#:
#: A dictionary rather than the single slot this used to be, because searching
#: two marketplaces at once means two jobs are genuinely under way and an
#: interface told about only one of them is an interface that is wrong.  With
#: nothing running in parallel it holds at most one entry, under
#: :data:`MAIN_LANE`, and everything downstream reads exactly as it did.
_lanes: Dict[str, Dict[str, Any]] = {}
#: When the last completed run finished, and how it ended.
_last: Optional[Dict[str, Any]] = None
#: Set when someone asks for an immediate scrape; cleared when it is taken up.
_run_requested: Optional[Dict[str, Any]] = None
#: Set when the running scrape is asked to stop at its next checkpoint.
_cancel = threading.Event()
#: Listings currently being fetched, so two flows never re-check the same one.
_claims: Set[Tuple[str, str]] = set()
#: Marketplace name -> ``{"until": monotonic deadline, "strikes": int, ...}``.
_blocks: Dict[str, Dict[str, Any]] = {}
#: Marketplaces whose stored session has changed and needs loading into the
#: live browser.  Set by the web UI thread, drained by the scraping one.
_session_imports: Set[str] = set()

#: How the running scrape was told to stop: ``"stop"`` closes every browser,
#: ``"pause"`` leaves them open.  Both cut the search off at the next
#: checkpoint -- the difference is only what is left standing afterwards, which
#: is exactly the difference the two buttons promise.  Read by the scraping
#: thread while it unwinds; meaningless when :data:`_cancel` is not set.
_cancel_mode: str = "stop"

#: Searches the user asked to end early, keyed by ``(item, marketplace)`` or by
#: ``(item, None)`` for "every platform of this product".
#:
#: Deliberately not the same flag as :data:`_cancel`.  That one means "stop
#: scraping"; this one means "this particular search is finished, get on with
#: the next" -- the loop carries on, the browser stays open, and nothing else
#: in the queue is touched.
_stops: Dict[Tuple[str, Optional[str]], Dict[str, Any]] = {}

#: The product the user picked to run next, jumping the queue, or None.
#:
#: One slot rather than a list: the button says "run this one next", and a
#: second press means the user changed their mind about which one -- a queue
#: would quietly promise both and honour neither in the order shown.
_next_search: Optional[Dict[str, Any]] = None

#: What the loop is doing at the coarsest useful grain, and since when.  Not a
#: second copy of :data:`_current`: that answers "is a job running", this
#: answers "and if not, what is it waiting for" -- which is the difference
#: between a monitor with nothing to do and one that is stuck.
_phase: Dict[str, Any] = {"name": "starting", "since": _now(), "detail": ""}
#: The configuration the scraping thread actually loaded, with the version it
#: was loaded under.  ``None`` until the first successful load.
_loaded_config: Optional[Dict[str, Any]] = None
#: The last configuration change the loop took up *while it was running*, and
#: what it did about it.  ``None`` until one happens -- the first load is not a
#: change, and announcing it would put a notice on every start-up.
_config_applied: Optional[Dict[str, Any]] = None
#: Rises once per applied change, so the interface can tell a new notice from
#: the same one polled again without comparing timestamps it did not issue.
_config_applied_seq: int = 0
#: Called at every checkpoint, by the scraping thread, on its own thread.  The
#: monitor installs it; see :func:`set_checkpoint_guard`.
_guard: Optional[Callable[[], None]] = None
#: ``(item, marketplace)`` -> when it last ran, how it ended, when it runs next.
_searches: Dict[Tuple[str, str], Dict[str, Any]] = {}
#: item name -> ISO timestamp of its next scheduled run, as the scheduler has
#: it.  Kept apart from :data:`_searches` because the schedule is per item.
_next_runs: Dict[str, str] = {}
#: ``(item, marketplace)`` -> the same answer for one platform of one product,
#: which is the grain the interface actually shows a row at.  A product can be
#: due on one platform and not on another -- one of them ran a minute ago on a
#: lane of its own -- and a single per-item figure has to lie about one of them.
_next_runs_by_pair: Dict[Tuple[str, str], str] = {}
#: How the re-checking of stored listings is going.
_updates: Dict[str, Any] = {
    "enabled": None,
    "parallel": False,
    "interval": None,
    "running": False,
    "marketplaces": [],
    "pending": None,
    "current": None,
    "last": None,
    "started_at": None,
    # When the next round is due, when the last one actually ran, how many
    # listings a round takes, and the schedule that decides all three.  Without
    # these the interface can say a review is configured but never when it will
    # happen, which is the one thing a user waiting for one wants to know.
    "next_run": None,
    "last_run": None,
    "batch": None,
    "schedule": None,
    "lane": None,
}


def _blank_updates() -> Dict[str, Any]:
    return {
        "enabled": None,
        "parallel": False,
        "interval": None,
        "running": False,
        "marketplaces": [],
        "pending": None,
        "current": None,
        "last": None,
        "started_at": None,
        "next_run": None,
        "last_run": None,
        "batch": None,
        "schedule": None,
        "lane": None,
    }

#: Every phase the loop can report.  Named rather than free text so the
#: interface can style them; ``detail`` carries whatever else is worth saying.
PHASES: Tuple[str, ...] = (
    "starting",
    "waiting_for_config",
    "waiting_for_credentials",
    "idle",
    "searching",
    "updating",
    "pausing",
    "paused",
    "error",
)

#: How long a marketplace is left alone after it refuses us, by consecutive
#: refusal.  It grows because a site that is still refusing after fifteen
#: minutes is not going to be talked round by asking again in another fifteen,
#: and it is capped because a monitor that gives up for a day is no use either.
BLOCK_BACKOFF: Tuple[int, ...] = (15 * 60, 30 * 60, 60 * 60, 2 * 60 * 60, 4 * 60 * 60)


# --------------------------------------------------------------------------- #
# What is running
# --------------------------------------------------------------------------- #


@contextmanager
def running(
    item: str | None = None,
    marketplace: str | None = None,
    lane: str = MAIN_LANE,
) -> Iterator[None]:
    """Mark a scrape as under way on one lane, for the duration of the block.

    One lane runs one job at a time; several lanes may each be running one, and
    each keeps its own slot here.  Nested use on the *same* lane is not expected
    and not supported.
    """
    global _last
    started = _now()
    with _lock:
        _lanes[lane] = {
            "lane": lane,
            "item": item or "",
            "marketplace": marketplace or "",
            "started_at": started,
        }
    outcome = "finished"
    try:
        yield
    except CancelledScrape:
        outcome = "cancelled"
        raise
    except SearchSuperseded:
        # Not a failure: the job was dropped because the user replaced the
        # configuration under it, which is the system working as asked.
        outcome = "superseded"
        raise
    except SearchStopped:
        # Nor is this one: the user pressed the button that ends this search.
        # Both have to be named before the catch-all below, or a control that
        # worked is reported to the user as a fault.
        outcome = "stopped"
        raise
    except BaseException:
        outcome = "failed"
        raise
    finally:
        with _lock:
            _lanes.pop(lane, None)
            _last = {
                "lane": lane,
                "item": item or "",
                "marketplace": marketplace or "",
                "started_at": started,
                "finished_at": _now(),
                "outcome": outcome,
            }


def _primary() -> Optional[Dict[str, Any]]:
    """The one job to name when only one can be named.  Caller holds the lock.

    The main lane when it is busy, because that is the lane the interface has
    always been describing; otherwise whichever other lane started first, so a
    parallel pass with nothing on the main lane still reads as "searching".
    """
    if not _lanes:
        return None
    if MAIN_LANE in _lanes:
        return dict(_lanes[MAIN_LANE])
    return dict(min(_lanes.values(), key=lambda entry: str(entry.get("started_at"))))


def lanes() -> List[Dict[str, Any]]:
    """Every lane with a job under way, in a stable order."""
    with _lock:
        return sorted((dict(entry) for entry in _lanes.values()), key=lambda e: str(e["lane"]))


# --------------------------------------------------------------------------- #
# Marketplaces that have started refusing us
# --------------------------------------------------------------------------- #


def block_marketplace(
    marketplace: str, reason: str = "", seconds: float | None = None
) -> Dict[str, Any]:
    """Stop opening pages on ``marketplace`` for a while.

    Called when a site serves a sign-in or verification wall instead of the page
    that was asked for.  Consecutive refusals back off further (see
    :data:`BLOCK_BACKOFF`); an explicit ``seconds`` overrides that.
    """
    with _lock:
        entry = _blocks.get(marketplace) or {"strikes": 0}
        strikes = int(entry.get("strikes") or 0)
        wait = float(
            BLOCK_BACKOFF[min(strikes, len(BLOCK_BACKOFF) - 1)] if seconds is None else seconds
        )
        entry = {
            "strikes": strikes + 1,
            "until": time.monotonic() + wait,
            "seconds": wait,
            "reason": reason,
            "since": _now(),
            # Wall clock, for the interface.  The deadline itself is monotonic,
            # so a clock change cannot strand a marketplace in the future.
            "until_iso": (
                datetime.now(timezone.utc) + timedelta(seconds=wait)
            ).isoformat(timespec="seconds"),
        }
        _blocks[marketplace] = entry
        return dict(entry)


def marketplace_blocked(marketplace: str) -> bool:
    """Whether pages on this marketplace should be left alone right now."""
    with _lock:
        entry = _blocks.get(marketplace)
        if entry is None:
            return False
        if time.monotonic() >= float(entry["until"]):
            # Expired: the cooldown is over, but the strike count is kept so a
            # site that refuses again straight away waits longer next time.
            entry["until"] = 0.0
            return False
        return True


def marketplace_block(marketplace: str) -> Optional[Dict[str, Any]]:
    """The live cooldown for one marketplace, or None when it is free to use."""
    if not marketplace_blocked(marketplace):
        return None
    with _lock:
        entry = dict(_blocks[marketplace])
    entry["seconds_left"] = max(0.0, float(entry["until"]) - time.monotonic())
    entry.pop("until", None)
    return entry


def marketplace_blocks() -> Dict[str, Dict[str, Any]]:
    """Every marketplace currently on a cooldown."""
    names: List[str]
    with _lock:
        names = list(_blocks)
    blocks = {}
    for name in names:
        entry = marketplace_block(name)
        if entry is not None:
            blocks[name] = entry
    return blocks


def request_session_import(marketplace: str) -> None:
    """Ask the scraping thread to load this marketplace's stored session.

    The web UI can write the session file itself, but it cannot put the cookies
    into the running browser: Playwright's objects belong to the thread that
    made them.  So it leaves a note here and the monitor picks it up between
    jobs -- the same shape as every other cross-thread request in this module.
    """
    with _lock:
        _session_imports.add(marketplace)


def pending_session_imports() -> Set[str]:
    with _lock:
        return set(_session_imports)


def take_session_imports() -> Set[str]:
    """Claim the pending imports.  Each one is handed out exactly once."""
    global _session_imports
    with _lock:
        pending = set(_session_imports)
        _session_imports = set()
        return pending


def clear_marketplace_block(marketplace: str) -> None:
    """A page came back normally: let the marketplace be used again.

    The strike count decays by one rather than resetting, so a site that lets us
    through once and refuses again does not get the shortest cooldown all over
    again.
    """
    with _lock:
        entry = _blocks.get(marketplace)
        if entry is None:
            return
        strikes = max(0, int(entry.get("strikes") or 0) - 1)
        if strikes == 0:
            del _blocks[marketplace]
        else:
            entry["strikes"] = strikes
            entry["until"] = 0.0


def is_running() -> bool:
    """Whether any lane has a scrape job in progress."""
    with _lock:
        return bool(_lanes)


def state() -> Dict[str, Any]:
    """A snapshot of everything the web UI shows about the scraping loop."""
    with _lock:
        snapshot = {
            "running": bool(_lanes),
            # The single job the interface names in its one-line summary.  Kept
            # for every caller that has only ever had room for one; `lanes`
            # below is the whole truth when more than one is running.
            "current": _primary(),
            "lanes": sorted(
                (dict(entry) for entry in _lanes.values()), key=lambda e: str(e["lane"])
            ),
            "last": dict(_last) if _last else None,
            "run_requested": dict(_run_requested) if _run_requested else None,
            "cancelling": _cancel.is_set(),
            # What the stop under way will leave standing.  The interface says
            # "pausando" or "deteniendo" from this rather than from which
            # button was pressed, because the button is a request and this is
            # what the loop actually took up.
            "cancel_mode": _cancel_mode if _cancel.is_set() else None,
            # Searches told to end early, and the one picked to go next.  Both
            # are requests the loop has not acted on yet, which is exactly why
            # the interface has to be able to see them: a button that appears
            # to do nothing for the twenty seconds until the next checkpoint is
            # a button the user presses again.
            "stops": [dict(entry) for entry in _stops.values()],
            "next_search": dict(_next_search) if _next_search else None,
        }
    # Outside the lock: marketplace_blocks takes it itself.
    snapshot["blocked"] = marketplace_blocks()
    return snapshot


# --------------------------------------------------------------------------- #
# What the loop is doing, and on which configuration
# --------------------------------------------------------------------------- #
#
# Written only by the scraping thread, read only by the web UI.  The interface
# therefore never has to infer what the monitor is doing from the file the user
# just saved -- which is the whole point: saving a search and the scraper using
# it are two different events, and only the monitor knows when the second one
# happened.


def set_phase(name: str, detail: str = "") -> Dict[str, Any]:
    """Say what the loop is doing now.  ``since`` only moves on a real change.

    Re-declaring the phase it is already in is a no-op on the clock: the loop
    says "idle" on every pass round the sleep, and a timestamp that reset each
    time would make a monitor that has been quiet for an hour look like it just
    got there.
    """
    global _phase
    with _lock:
        if _phase["name"] == name and _phase["detail"] == detail:
            return dict(_phase)
        _phase = {"name": name, "since": _now(), "detail": detail}
        return dict(_phase)


def phase() -> Dict[str, Any]:
    with _lock:
        return dict(_phase)


def set_loaded_config(version: str, snapshot: Dict[str, Any]) -> None:
    """Record the configuration the scraping thread has actually taken up.

    ``version`` is the hash of the configuration files as they were read.  The
    web UI compares it with the hash of what is on disk now, which is the only
    unambiguous way to answer "has the scraper got my change yet?" -- a
    timestamp written by the browser answers a different question.
    """
    global _loaded_config
    with _lock:
        _loaded_config = {
            "version": version,
            "loaded_at": _now(),
            **snapshot,
        }


def loaded_config() -> Optional[Dict[str, Any]]:
    with _lock:
        return dict(_loaded_config) if _loaded_config else None


def set_config_applied(
    version: str,
    change: Dict[str, Any],
    interrupted: Optional[Dict[str, Any]] = None,
    live: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Announce a configuration change the running loop has taken up.

    Distinct from :func:`set_loaded_config`, which says *what* is loaded.  This
    says *that a change landed*, what was in it, and whether a search had to be
    dropped for it -- the three things the user who just pressed save wants to
    read back, and none of which can be worked out from two hashes being equal.

    ``interrupted`` is the search that was abandoned, when there was one:
    ``{"item", "marketplace", "reason"}``.

    ``live`` is what happened to the search that was *running* when the change
    landed: ``{"item", "marketplace", "applied": [...], "deferred": [...]}``.
    Both lists are field names.  ``deferred`` is the honest half and the reason
    this exists at all -- a maximum price already spent on the URL the search
    is paging through cannot be taken up by that search, however much the
    interface would like to show a tick, so it is named and reported as waiting
    for that search's next run.
    """
    global _config_applied, _config_applied_seq
    with _lock:
        _config_applied_seq += 1
        _config_applied = {
            "seq": _config_applied_seq,
            "at": _now(),
            "version": version,
            "change": change,
            "interrupted": dict(interrupted) if interrupted else None,
            "live": dict(live) if live else None,
        }
        return dict(_config_applied)


def config_applied() -> Optional[Dict[str, Any]]:
    """The last change the loop took up while running, or None if there was none."""
    with _lock:
        return dict(_config_applied) if _config_applied else None


def loaded_version() -> Optional[str]:
    with _lock:
        return _loaded_config["version"] if _loaded_config else None


@contextmanager
def search(item: str, marketplace: str) -> Iterator[None]:
    """Mark one (item, marketplace) search as under way.

    Finer-grained than :func:`running`, which reports that *a* job is going:
    this is the pair actually being searched, and its history is what lets the
    interface say when each search last ran and how it ended.
    """
    started = _now()
    key = (item, marketplace)
    with _lock:
        entry = dict(_searches.get(key) or {})
        entry.update({"item": item, "marketplace": marketplace, "started_at": started,
                      "running": True})
        _searches[key] = entry
    outcome = "finished"
    try:
        yield
    except CancelledScrape:
        outcome = "cancelled"
        raise
    except SearchSuperseded:
        outcome = "superseded"
        raise
    except SearchStopped:
        outcome = "stopped"
        raise
    except BaseException:
        outcome = "failed"
        raise
    finally:
        with _lock:
            entry = dict(_searches.get(key) or {})
            entry.update(
                {
                    "item": item,
                    "marketplace": marketplace,
                    "running": False,
                    "last_started_at": started,
                    "last_finished_at": _now(),
                    "last_outcome": outcome,
                }
            )
            entry.pop("started_at", None)
            _searches[key] = entry


def record_found(item: str, marketplace: str, count: int) -> None:
    """How many listings the last search of this pair turned up."""
    with _lock:
        entry = dict(_searches.get((item, marketplace)) or {})
        entry.update({"item": item, "marketplace": marketplace, "last_found": count})
        _searches[(item, marketplace)] = entry


def set_next_runs(
    next_runs: Dict[str, str],
    by_pair: Dict[Tuple[str, str], str] | None = None,
) -> None:
    """Publish when each search is next due, as the scheduler has it.

    Timestamps carry an offset.  They are read by a browser in some other time
    zone as often as not, and a bare ``2026-08-22T20:30:00`` is a different
    instant depending on who reads it -- which is exactly how a next run ends
    up rendered as something that already happened.
    """
    global _next_runs, _next_runs_by_pair
    with _lock:
        _next_runs = dict(next_runs)
        _next_runs_by_pair = dict(by_pair or {})


def next_run_for(item: str, marketplace: str) -> Optional[str]:
    """When the scheduler says this pair is next due, history or no history.

    :func:`searches` can only answer for a pair it has *seen run*, because that
    is what it keeps.  After a restart nothing has run yet, and the scheduler's
    slots -- which survive, being seeded from what each pair last did -- had no
    way of reaching the interface: a search genuinely scheduled for 18:20 was
    shown as "sin programar".  The platform's own slot wins over the product's,
    which is the grain a row on the screen is.
    """
    with _lock:
        return _next_runs_by_pair.get((item, marketplace)) or _next_runs.get(item)


def forget_searches(keep: Set[Tuple[str, str]]) -> None:
    """Drop the history of pairs that are no longer configured.

    Called after a reload, so a search the user deleted stops being reported as
    something the scraper runs -- which is exactly what a deletion has to mean
    for the interface to be trustworthy.
    """
    with _lock:
        for key in [key for key in _searches if key not in keep]:
            del _searches[key]
        for name in [name for name in _next_runs if name not in {item for item, _ in keep}]:
            del _next_runs[name]
        for pair in [pair for pair in _next_runs_by_pair if pair not in keep]:
            del _next_runs_by_pair[pair]


def searches() -> List[Dict[str, Any]]:
    """Every (item, marketplace) pair the scraper has, with its history.

    ``next_run`` is the platform's own next slot when the scheduler has one,
    and the product's otherwise.  ``due_now`` goes with it: a search whose slot
    has passed is not running *late*, it is waiting its turn behind whatever is
    running -- and a timestamp on its own leaves the interface to render
    "próxima ejecución: hace ocho minutos", which is not a thing that can be
    true.
    """
    with _lock:
        entries = [dict(entry) for entry in _searches.values()]
        next_runs = dict(_next_runs)
        by_pair = dict(_next_runs_by_pair)
    for entry in entries:
        item = str(entry.get("item"))
        marketplace = str(entry.get("marketplace"))
        when = by_pair.get((item, marketplace)) or next_runs.get(item)
        entry["next_run"] = when
        entry["due_now"] = bool(has_passed(when) and not entry.get("running"))
    entries.sort(key=lambda entry: (str(entry.get("item")), str(entry.get("marketplace"))))
    return entries


def set_updates_config(
    enabled: bool,
    parallel: bool,
    interval: float | None,
    marketplaces: List[str],
    batch: int | None = None,
    schedule: Dict[str, Any] | None = None,
    lane: str | None = None,
) -> None:
    """What the re-checking of stored listings is configured to do.

    ``schedule`` is the plain-data description of *when* a round happens -- the
    same shape the interface renders -- rather than a sentence built here: the
    loop knows the numbers, and only the interface knows what language to say
    them in.
    """
    with _lock:
        _updates.update(
            {
                "enabled": bool(enabled),
                "parallel": bool(parallel),
                "interval": interval,
                "marketplaces": list(marketplaces),
                "batch": None if batch is None else int(batch),
                "schedule": dict(schedule) if schedule else None,
                "lane": lane,
            }
        )


def set_updates_next_run(when: str | None) -> None:
    """Publish when the next round of re-checks is due, as an ISO timestamp.

    Written by whichever thread owns the review, read by the web UI.  ``None``
    means nothing is scheduled -- reviews are switched off, or there is nothing
    to review.
    """
    with _lock:
        _updates["next_run"] = when


@contextmanager
def updating(marketplaces: List[str], lane: str | None = None) -> Iterator[None]:
    """Mark a round of listing re-checks as under way."""
    started = _now()
    with _lock:
        _updates.update(
            {
                "running": True,
                "started_at": started,
                "marketplaces": list(marketplaces),
                **({"lane": lane} if lane is not None else {}),
            }
        )
    try:
        yield
    finally:
        with _lock:
            _updates.update(
                {
                    "running": False,
                    "current": None,
                    "started_at": None,
                    # When a round last actually happened, which is a different
                    # question from what the round found: a round that checked
                    # nothing still ran, and a user waiting for one needs to see
                    # that it did.
                    "last_run": _now(),
                }
            )


def updates_pending(count: int) -> None:
    """How many stored listings are currently overdue for a re-check."""
    with _lock:
        _updates["pending"] = int(count)


def updates_current(marketplace: str | None, listing_id: str | None) -> None:
    """Which stored listing is being re-read right now."""
    with _lock:
        _updates["current"] = (
            None
            if listing_id is None
            else {"marketplace": marketplace or "", "listing_id": listing_id, "since": _now()}
        )


def updates_done(report: Dict[str, Any]) -> None:
    """What the slice that just finished did."""
    with _lock:
        _updates["last"] = {"at": _now(), **report}


def updates() -> Dict[str, Any]:
    with _lock:
        snapshot = dict(_updates)
    snapshot["marketplaces"] = list(snapshot.get("marketplaces") or [])
    return snapshot


# --------------------------------------------------------------------------- #
# Asking for a scrape
# --------------------------------------------------------------------------- #


def request_run(reason: str = "web UI") -> Dict[str, Any]:
    """Ask the monitor to scrape everything as soon as it can.

    Returns ``{"accepted": bool, "reason": str, ...}``.  A request made while a
    scrape is already running is refused rather than queued: the point of the
    button is "go now", and stacking a second full pass behind the current one
    is exactly the concurrent hammering of the marketplace this refuses to do.
    A second request made while one is still pending is idempotent.
    """
    global _run_requested
    with _lock:
        if _lanes:
            return {
                "accepted": False,
                "status": "already_running",
                "current": _primary(),
            }
        if _run_requested is not None:
            return {"accepted": True, "status": "already_requested", **_run_requested}
        _run_requested = {"requested_at": _now(), "reason": reason}
        return {"accepted": True, "status": "requested", **_run_requested}


def run_pending() -> bool:
    """Whether a scrape has been asked for and not yet taken up."""
    with _lock:
        return _run_requested is not None


def take_run_request() -> bool:
    """Claim a pending request.  True exactly once per request."""
    global _run_requested
    with _lock:
        if _run_requested is None:
            return False
        _run_requested = None
        return True


def clear_run_request() -> None:
    """Drop a pending request without acting on it (e.g. on a forced pause)."""
    global _run_requested
    with _lock:
        _run_requested = None


# --------------------------------------------------------------------------- #
# Cancelling the running scrape
# --------------------------------------------------------------------------- #


def request_cancel(mode: str = "stop") -> None:
    """Ask the running scrape to stop at its next checkpoint.

    ``mode`` says what to leave standing.  ``"stop"`` is the full halt: the
    browsers go too, and nothing can carry on without opening one again.
    ``"pause"`` cuts the search off just as promptly but leaves every browser,
    tab and signed-in session exactly where it was, so resuming costs a search
    rather than a login.

    Both are requests, not acts: Playwright objects belong to the thread that
    made them, so the scraping thread is the only one that may close a page.
    """
    global _cancel_mode
    clear_run_request()
    with _lock:
        _cancel_mode = "pause" if mode == "pause" else "stop"
    _cancel.set()


def cancel_requested() -> bool:
    return _cancel.is_set()


def cancel_mode() -> str:
    """``"stop"`` or ``"pause"`` -- whether the browsers go with the search."""
    with _lock:
        return _cancel_mode


def clear_cancel() -> None:
    """Called by the scraping thread once it has actually unwound."""
    _cancel.clear()


# --------------------------------------------------------------------------- #
# Ending one search without stopping the scraper
# --------------------------------------------------------------------------- #
#
# "Stop this search and go on to the next" is not a pause and not a
# cancellation: the loop is behaving perfectly, the user simply has no more
# interest in what it is doing at this instant.  So it gets its own register
# rather than borrowing the cancel flag, and the checkpoints raise
# :class:`SearchStopped`, which the loop treats exactly as it treats a search
# the configuration superseded -- browser kept, queue continued.
#
# A stop covers searches that have not started yet as well as the one running,
# because the button is pressed on a product and a product usually runs on more
# than one platform.  It lasts for the pass it was made in: the monitor clears
# the register when the pass ends, so the same product is searched normally
# next time round.  Anything longer-lived is what switching the search off is
# for.


def request_search_stop(
    item: str, marketplace: str | None = None, reason: str = "web UI"
) -> Dict[str, Any]:
    """End this search early.  ``marketplace`` None means every platform of it."""
    with _lock:
        entry = {
            "item": item,
            "marketplace": marketplace,
            "scope": "platform" if marketplace else "search",
            "reason": reason,
            "at": _now(),
        }
        _stops[(item, marketplace)] = entry
        return dict(entry)


def stop_requested(item: str, marketplace: str | None = None) -> Optional[Dict[str, Any]]:
    """The standing stop for this pair, if there is one.

    A product-wide stop answers for every platform under it, which is what
    makes "stop this search" reach the platforms it has not started yet.
    """
    with _lock:
        entry = _stops.get((item, None))
        if entry is None and marketplace is not None:
            entry = _stops.get((item, marketplace))
        return dict(entry) if entry else None


def clear_search_stop(item: str, marketplace: str | None = None) -> None:
    """Drop one standing stop.  ``marketplace`` None drops the product-wide one."""
    with _lock:
        _stops.pop((item, marketplace), None)


def clear_search_stops() -> None:
    """Forget every standing stop, because the pass they applied to is over."""
    with _lock:
        _stops.clear()


def clear_search_stops_for(items: Iterable[str]) -> None:
    """Forget the standing stops on these products, product-wide ones included.

    For a pass that was narrowed to certain searches -- the ones a save just
    touched -- which is over for the searches it held even though it was never
    the whole queue.  ``clear_search_stops`` cannot be used there: it would
    withdraw a stop meant for a search this pass never ran.

    This is a fix, not an optimisation.  A stop made during such a pass was
    cleared by nobody: the checkpoint spends only a *platform*-level stop, the
    end-of-pass sweep runs on a whole pass alone, and saving a new search is
    exactly what starts a narrowed one.  The stop then outlived its search by
    however long the interval was, which the interface showed, accurately, as a
    search stuck on "deteniendose..." while its next run counted down beside it.
    """
    with _lock:
        for item in set(items):
            for key in [key for key in _stops if key[0] == item]:
                del _stops[key]


def search_stops() -> List[Dict[str, Any]]:
    """Every standing stop, so the interface can show a platform as stopping."""
    with _lock:
        return [dict(entry) for entry in _stops.values()]


# --------------------------------------------------------------------------- #
# Choosing what runs next
# --------------------------------------------------------------------------- #


def set_next_search(item: str | None, reason: str = "web UI") -> Optional[Dict[str, Any]]:
    """Put one product at the head of the queue, without touching what is running.

    ``None`` clears the choice and hands the order back to the schedule.  The
    search under way is deliberately left alone: the button says *next*, and a
    button that stopped the current search would be the other one.
    """
    global _next_search
    with _lock:
        _next_search = None if item is None else {"item": item, "reason": reason, "at": _now()}
        return dict(_next_search) if _next_search else None


def next_search() -> Optional[Dict[str, Any]]:
    """The product the user picked to go next, or None for the usual order."""
    with _lock:
        return dict(_next_search) if _next_search else None


def take_next_search() -> Optional[str]:
    """Claim the choice.  Returns the product name once, then None.

    Claimed rather than read, because the promise is "this one next" and not
    "this one from now on": once it has had its turn the schedule takes over
    again.
    """
    global _next_search
    with _lock:
        chosen = _next_search
        _next_search = None
        return None if chosen is None else str(chosen["item"])


def set_checkpoint_guard(guard: Optional[Callable[[], None]]) -> None:
    """Install a second question for every checkpoint to ask.

    Cancellation is a flag any thread can set, so the checkpoint can read it
    itself.  "Is the configuration still the one I started this search under?"
    is not: answering it means reading and parsing files, which only the
    scraping thread may do at a moment of its own choosing.  So the monitor
    hands in a callable here and the checkpoints call it -- on the scraping
    thread, where it is safe -- rather than every scraping site growing a
    second import and a second condition.

    The guard raises :class:`SearchSuperseded` to abandon the search, or
    returns to let it carry on.  ``None`` uninstalls it.
    """
    global _guard
    _guard = guard


def raise_if_cancelled() -> None:
    """Checkpoint: give up the current scrape if there is reason to.

    Sprinkled through the scraping path at the points where abandoning the work
    costs nothing -- between listings, between search phrases, before a
    navigation.  Never inside a partially written record.

    Two reasons, in order of severity: the scrape was told to stop, or the
    configuration it is running under has been replaced by one that no longer
    wants this search.  The first wins, because a forced pause has to happen
    whatever the file says.
    """
    if _cancel.is_set():
        raise CancelledScrape("Scraping was stopped from the web UI")
    guard = _guard
    if guard is not None:
        guard()


# --------------------------------------------------------------------------- #
# Claiming a listing
# --------------------------------------------------------------------------- #


@contextmanager
def claim(marketplace: str, listing_id: str) -> Iterator[bool]:
    """Take exclusive hold of one listing for the duration of the block.

    Yields False when somebody else already holds it, in which case the caller
    must skip the listing rather than wait: both holders would be doing the same
    fetch, and the loser has plenty of other listings to get on with.
    """
    key = (marketplace, listing_id)
    with _lock:
        taken = key in _claims
        if not taken:
            _claims.add(key)
    try:
        yield not taken
    finally:
        if not taken:
            with _lock:
                _claims.discard(key)


def is_claimed(marketplace: str, listing_id: str) -> bool:
    with _lock:
        return (marketplace, listing_id) in _claims


def reset_for_tests() -> None:
    """Forget every in-memory flag.  For tests, which share the process."""
    global _last, _run_requested, _phase, _loaded_config, _next_runs
    global _config_applied, _config_applied_seq, _guard, _cancel_mode, _next_search
    _guard = None
    with _lock:
        _lanes.clear()
        _stops.clear()
        _next_search = None
        _cancel_mode = "stop"
        _config_applied = None
        _config_applied_seq = 0
        _last = None
        _run_requested = None
        _claims.clear()
        _blocks.clear()
        _session_imports.clear()
        _phase = {"name": "starting", "since": _now(), "detail": ""}
        _loaded_config = None
        _searches.clear()
        _next_runs = {}
        _next_runs_by_pair.clear()
        _updates.clear()
        _updates.update(_blank_updates())
    _cancel.clear()
