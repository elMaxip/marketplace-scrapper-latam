"""Why a notification is being sent, and whether the user asked to hear it.

The monitor has always had reasons -- it just never named them.  A listing was
told about because it was new, or because it had got cheaper since the last
message, or because the reminder interval had come round; the reason was implied
by a :class:`~ai_marketplace_monitor.notification.NotificationStatus` and then
thrown away.  That was fine while every reason was always on.  It stops being
fine the moment the user can switch one off, because "do not tell me about new
listings, only about price drops" is a sentence about *reasons*, and there was
nothing in the program that a checkbox could be attached to.

So each status maps to a reason, and the reasons the user wants are three
booleans in ``[monitor]``:

``notify_new``           a listing nobody has been told about yet
``notify_price_drop``    a listing already known, now cheaper
``notify_top_listing``   a search's cheapest valid listing, when that changes

The first two default to **on**, because that is exactly what the monitor did
before there was a switch and a configuration file written yesterday must keep
meaning what it meant.  The third defaults to **off**: it is new behaviour that
sends messages nobody has asked for yet, and the honest default for that is
silence.

Two statuses are deliberately *not* switchable.  ``LISTING_CHANGED`` and
``EXPIRED`` are the seller editing the post and the ``remind`` interval coming
round -- both predate this, neither is one of the three things the user asked to
control, and inventing a switch for them would mean inventing a default for a
question nobody asked.  They map to :attr:`NotifyReason.OTHER`, which is always
allowed.

Nothing here sends anything or reads a config file: it turns a monitor section
into a set of allowed reasons and answers yes or no about one status.  That is
what lets the rules be tested without a browser, a channel or a cache.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, List, Sequence, Tuple, TypeVar

from .notification import NotificationStatus


class NotifyReason(Enum):
    """Why this message is going out."""

    NEW = "new"
    PRICE_DROP = "price_drop"
    TOP_LISTING = "top_listing"
    #: The seller edited the post, or the reminder came round.  Not switchable.
    OTHER = "other"


#: Which reason each status is.  A status missing from here is ``OTHER``, which
#: is the safe direction: an unrecognised reason is delivered rather than
#: silently dropped.
_REASON_OF = {
    NotificationStatus.NOT_NOTIFIED: NotifyReason.NEW,
    NotificationStatus.LISTING_DISCOUNTED: NotifyReason.PRICE_DROP,
    NotificationStatus.TOP_LISTING: NotifyReason.TOP_LISTING,
}

#: The keys in ``[monitor]``, in the order the settings panel shows them.
NEW_KEY = "notify_new"
PRICE_DROP_KEY = "notify_price_drop"
TOP_LISTING_KEY = "notify_top_listing"


def reason_of(status: Any) -> NotifyReason:
    """The reason a status stands for."""
    return _REASON_OF.get(status, NotifyReason.OTHER)


@dataclass(frozen=True)
class NotifyReasons:
    """The three switches, resolved.

    Defaults chosen so that ``NotifyReasons()`` is the behaviour the monitor had
    before any of this existed -- which is what a config file that says nothing
    must keep getting.
    """

    new: bool = True
    price_drop: bool = True
    top_listing: bool = False

    def allows(self: "NotifyReasons", status: Any) -> bool:
        """Whether a notification with this status may go out."""
        reason = reason_of(status)
        if reason is NotifyReason.NEW:
            return self.new
        if reason is NotifyReason.PRICE_DROP:
            return self.price_drop
        if reason is NotifyReason.TOP_LISTING:
            return self.top_listing
        return True

    @property
    def any_enabled(self: "NotifyReasons") -> bool:
        """Whether anything at all can be sent.

        Worth asking before doing the work of building cards for a batch that
        cannot produce a single message.
        """
        return self.new or self.price_drop or self.top_listing


def _flag(section: Any, key: str, default: bool) -> bool:
    """One switch out of a ``[monitor]`` section.

    ``None`` -- which is what an absent key parses to -- means the default, not
    False.  Reading it as False is the mistake that would silence a monitor
    whose config file simply predates the setting.
    """
    value = getattr(section, key, None)
    if value is None:
        return default
    return bool(value)


def reasons_from_config(monitor_config: Any) -> NotifyReasons:
    """Read the three switches out of a ``[monitor]`` section."""
    if monitor_config is None:
        return NotifyReasons()
    return NotifyReasons(
        new=_flag(monitor_config, NEW_KEY, True),
        price_drop=_flag(monitor_config, PRICE_DROP_KEY, True),
        top_listing=_flag(monitor_config, TOP_LISTING_KEY, False),
    )


T = TypeVar("T")


def keep_allowed(
    rows: Iterable[Tuple[T, ...]],
    statuses: Sequence[Any],
    reasons: NotifyReasons,
) -> List[Tuple[T, ...]]:
    """The rows whose status the user asked to hear about.

    Takes parallel tuples rather than a listing type on purpose: the caller
    zips listings with ratings, cards and statuses, and this module has no
    business knowing what any of those are.
    """
    return [row for row, status in zip(rows, statuses) if reasons.allows(status)]
