"""When the stored listings get looked at again.

Searching has had a schedule the user can see and set for a long time;
re-checking the listings already stored has not.  It simply happened whenever
the loop passed a gap between two searches, which is defensible behaviour and
an indefensible thing to show someone: "it will happen at some point" is not an
answer to "when is the next review?".

This module gives the review the same three ways of saying *when* that the
search schedule already has, and one more that only makes sense here:

* a **fixed interval** -- every 30 minutes, every 2 hours;
* a **random interval** between a floor and a ceiling, so the rhythm is not
  identical every time;
* **fixed times of day** -- 09:00, 15:00, 21:00 -- which may be combined with
  either interval or used alone;
* a **batch size**: how many listings one round re-checks.

Deliberately *not* built on the ``schedule`` package the searches use.  That
package's job registry is a process-wide singleton driven by the monitor
thread, and the review can run on a thread of its own; sharing the registry
would mean two threads mutating one list of jobs.  The arithmetic here is a
dozen lines, needs no registry, and answers the one question the interface
asks -- "what time is the next one?" -- as a value rather than as a side effect.

Nothing here opens a browser or reads a listing: it decides *when*, and
:class:`~ai_marketplace_monitor.refresh.ListingRefresher` decides *what*.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

#: How long after a round the next one is due when nothing is configured.
#:
#: The same sixty seconds the refresher already paced itself by, so a monitor
#: whose configuration says nothing about reviews keeps exactly the behaviour it
#: had before there was anything to say.
DEFAULT_REVIEW_INTERVAL = 60

#: Listings per round when nothing is configured -- the old slice size.
DEFAULT_REVIEW_BATCH = 10


def _next_time_of_day(spec: str, after: datetime) -> Optional[datetime]:
    """The next moment matching one ``start_at`` entry, strictly after ``after``.

    The four forms the config accepts, and what each one repeats on:

    ``HH:MM`` / ``HH:MM:SS``   once a day
    ``*:MM`` / ``*:MM:SS``     once an hour
    ``*:*:SS``                 once a minute

    ``None`` for anything unparseable, so one malformed entry cannot take the
    whole schedule down with it.
    """
    parts = spec.split(":")
    try:
        if len(parts) == 2:
            hour, minute, second = parts[0], parts[1], "00"
        elif len(parts) == 3:
            hour, minute, second = parts
        else:
            return None

        if hour == "*" and minute == "*":
            # Every minute, at this second.
            candidate = after.replace(second=int(second), microsecond=0)
            step = timedelta(minutes=1)
        elif hour == "*":
            # Every hour, at this minute and second.
            candidate = after.replace(minute=int(minute), second=int(second), microsecond=0)
            step = timedelta(hours=1)
        else:
            candidate = after.replace(
                hour=int(hour), minute=int(minute), second=int(second), microsecond=0
            )
            step = timedelta(days=1)
    except (TypeError, ValueError):
        return None

    while candidate <= after:
        candidate += step
    return candidate


@dataclass
class ReviewSchedule:
    """When a round of re-checks is due, and how big a round is.

    Every field is optional and the empty schedule is a working one: it falls
    back to :data:`DEFAULT_REVIEW_INTERVAL`, which is what the monitor did
    before any of this was configurable.
    """

    #: Fixed interval in seconds, or the floor of the random range.
    interval: Optional[int] = None
    #: The ceiling of the random range.  Absent, or equal to ``interval``, means
    #: the interval is fixed.
    max_interval: Optional[int] = None
    #: Times of day, on top of whatever interval is set.
    start_at: List[str] = field(default_factory=list)
    #: Listings one round re-checks.
    batch: int = DEFAULT_REVIEW_BATCH

    def __post_init__(self: "ReviewSchedule") -> None:
        self.start_at = [entry for entry in (self.start_at or []) if isinstance(entry, str)]
        self.batch = max(1, int(self.batch or DEFAULT_REVIEW_BATCH))

    # ------------------------------------------------------------------ #
    # What it is
    # ------------------------------------------------------------------ #

    @property
    def mode(self: "ReviewSchedule") -> str:
        """``"fixed"``, ``"random"``, ``"times"`` or ``"default"``.

        ``times`` means fixed times of day and no interval at all; a schedule
        with both reports the interval, because that is the part that decides
        the *typical* gap between rounds.
        """
        if self.interval is None and self.max_interval is None:
            return "times" if self.start_at else "default"
        low, high = self._range()
        return "random" if high > low else "fixed"

    def _range(self: "ReviewSchedule") -> Tuple[int, int]:
        """The interval band actually in force, in seconds."""
        low = self.interval or self.max_interval or DEFAULT_REVIEW_INTERVAL
        high = max(self.max_interval or low, low)
        return max(1, int(low)), max(1, int(high))

    def describe(self: "ReviewSchedule") -> Dict[str, Any]:
        """Plain data for the web UI: the numbers, not a sentence about them."""
        low, high = self._range()
        uses_interval = self.mode in ("fixed", "random", "default")
        return {
            "mode": self.mode,
            "interval": low if uses_interval else None,
            "max_interval": high if uses_interval else None,
            "start_at": list(self.start_at),
            "batch": self.batch,
            # True when nothing in the file says anything and these are the
            # monitor's own numbers, which is worth showing as such rather than
            # as a setting the user made.
            "default": self.mode == "default",
        }

    # ------------------------------------------------------------------ #
    # When the next one is
    # ------------------------------------------------------------------ #

    def next_after(self: "ReviewSchedule", moment: float | None = None) -> float:
        """Epoch seconds of the next round after ``moment``.

        The interval and the fixed times are not alternatives: whichever comes
        first wins, exactly as the search schedule treats them.  The interval is
        measured from ``moment`` -- the end of the last round -- so a long round
        does not immediately owe another.
        """
        now = time.time() if moment is None else moment
        candidates: List[float] = []

        if self.interval is not None or self.max_interval is not None or not self.start_at:
            low, high = self._range()
            # Drawn once per round rather than per query, which is why this is
            # called at the end of a round and its answer stored: asking twice
            # would give two different times for the same round.
            candidates.append(now + (low if high == low else random.uniform(low, high)))

        if self.start_at:
            after = datetime.fromtimestamp(now)
            for spec in self.start_at:
                nxt = _next_time_of_day(spec, after)
                if nxt is not None:
                    candidates.append(nxt.timestamp())

        # A schedule made entirely of unparseable times still has to produce a
        # next round, or reviews would stop for good over a typo.
        return min(candidates) if candidates else now + DEFAULT_REVIEW_INTERVAL


def schedule_from_config(monitor_config: Any) -> ReviewSchedule:
    """Read the review schedule out of a ``[monitor]`` section.

    Every key is optional, so a configuration written before reviews had a
    schedule of their own produces the default one -- which behaves the way that
    configuration always did.
    """
    return ReviewSchedule(
        interval=getattr(monitor_config, "listing_review_interval", None),
        max_interval=getattr(monitor_config, "listing_review_max_interval", None),
        start_at=list(getattr(monitor_config, "listing_review_start_at", None) or []),
        batch=getattr(monitor_config, "listing_review_batch", None) or DEFAULT_REVIEW_BATCH,
    )
