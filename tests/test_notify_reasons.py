"""Which notifications go out, and which the user switched off.

Pure: a monitor section in, a set of allowed reasons out.  No channel, no cache
and no browser -- the whole point of naming the reasons was to make the rule
something that could be read off a test rather than out of a running scraper.
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_marketplace_monitor.notification import NotificationStatus
from ai_marketplace_monitor.notify_reasons import (
    NotifyReason,
    NotifyReasons,
    reason_of,
    reasons_from_config,
)


@dataclass
class Section:
    """Just enough of a ``[monitor]`` section to read three keys off."""

    notify_new: bool | None = None
    notify_price_drop: bool | None = None
    notify_top_listing: bool | None = None


# --------------------------------------------------------------------------- #
# Statuses map to reasons
# --------------------------------------------------------------------------- #


def test_each_status_names_its_reason() -> None:
    assert reason_of(NotificationStatus.NOT_NOTIFIED) is NotifyReason.NEW
    assert reason_of(NotificationStatus.LISTING_DISCOUNTED) is NotifyReason.PRICE_DROP
    assert reason_of(NotificationStatus.TOP_LISTING) is NotifyReason.TOP_LISTING


def test_the_two_unswitchable_statuses_are_other() -> None:
    # The seller edited the post, and the `remind` interval coming round.
    # Neither is one of the three things the user asked to control.
    assert reason_of(NotificationStatus.LISTING_CHANGED) is NotifyReason.OTHER
    assert reason_of(NotificationStatus.EXPIRED) is NotifyReason.OTHER


def test_an_unrecognised_status_is_delivered_not_dropped() -> None:
    assert reason_of("something new") is NotifyReason.OTHER
    assert NotifyReasons(new=False, price_drop=False).allows("something new")


# --------------------------------------------------------------------------- #
# Defaults are the behaviour that already existed
# --------------------------------------------------------------------------- #


def test_a_config_that_says_nothing_keeps_the_old_behaviour() -> None:
    reasons = reasons_from_config(Section())
    assert reasons.new is True
    assert reasons.price_drop is True
    # New behaviour that sends messages nobody asked for stays silent.
    assert reasons.top_listing is False


def test_no_monitor_section_at_all() -> None:
    assert reasons_from_config(None) == NotifyReasons()


def test_absent_key_is_the_default_not_false() -> None:
    # The mistake this guards: reading `None` as False would silence every
    # monitor whose config file simply predates the setting.
    assert reasons_from_config(Section(notify_new=None)).new is True


# --------------------------------------------------------------------------- #
# The switches
# --------------------------------------------------------------------------- #


def test_new_listings_can_be_switched_off() -> None:
    reasons = reasons_from_config(Section(notify_new=False))
    assert not reasons.allows(NotificationStatus.NOT_NOTIFIED)
    # And switching one off leaves the others alone.
    assert reasons.allows(NotificationStatus.LISTING_DISCOUNTED)


def test_price_drops_can_be_switched_off() -> None:
    reasons = reasons_from_config(Section(notify_price_drop=False))
    assert not reasons.allows(NotificationStatus.LISTING_DISCOUNTED)
    assert reasons.allows(NotificationStatus.NOT_NOTIFIED)


def test_top_listing_can_be_switched_on() -> None:
    reasons = reasons_from_config(Section(notify_top_listing=True))
    assert reasons.allows(NotificationStatus.TOP_LISTING)


def test_top_listing_is_off_until_asked_for() -> None:
    assert not reasons_from_config(Section()).allows(NotificationStatus.TOP_LISTING)


def test_the_unswitchable_reasons_survive_everything_being_off() -> None:
    reasons = NotifyReasons(new=False, price_drop=False, top_listing=False)
    assert reasons.allows(NotificationStatus.LISTING_CHANGED)
    assert reasons.allows(NotificationStatus.EXPIRED)


def test_any_enabled() -> None:
    assert NotifyReasons().any_enabled
    assert not NotifyReasons(new=False, price_drop=False, top_listing=False).any_enabled
    assert NotifyReasons(new=False, price_drop=False, top_listing=True).any_enabled


def test_truthy_values_are_read_as_booleans() -> None:
    # TOML gives booleans, but a hand-edited file can hold anything.
    assert reasons_from_config(Section(notify_new=1)).new is True  # type: ignore[arg-type]
    assert reasons_from_config(Section(notify_new=0)).new is False  # type: ignore[arg-type]
