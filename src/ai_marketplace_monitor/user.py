from dataclasses import dataclass
from datetime import datetime, timedelta
from logging import Logger
from typing import Any, List, Tuple, Type

from diskcache import Cache  # type: ignore

from .ai import AIResponse  # type: ignore
from .email_notify import EmailNotificationConfig
from .listing import Listing
from .marketplace import TItemConfig
from .messages import build_card
from .notification import NotificationConfig, NotificationStatus
from .observations import record_notification
from .ntfy import NtfyNotificationConfig
from .pushbullet import PushbulletNotificationConfig
from .pushover import PushoverNotificationConfig
from .telegram import TelegramNotificationConfig
from .utils import (
    CacheType,
    CounterItem,
    cache,
    convert_to_seconds,
    counter,
    hilight,
    price_value,
)


@dataclass
class UserConfig(
    EmailNotificationConfig,
    PushbulletNotificationConfig,
    PushoverNotificationConfig,
    NtfyNotificationConfig,
    TelegramNotificationConfig,
):
    """UserConfiguration

    Derive from EmailNotificationConfig, PushbulletNotificationConfig allows
    the user config class to use settings from both classes.

    It is possible to dynamically added these classes as parent class
    of UserConfig, but it is troublesome to make sure that these classes
    are imported.
    """

    notify_with: List[str] | None = None
    remind: int | None = None

    def handle_remind(self: "UserConfig") -> None:
        if self.remind is None:
            return

        if self.remind is False:
            self.remind = None
            return

        if self.remind is True:
            # if set to true but no specific time, set to 1 day
            self.remind = 60 * 60 * 24
            return

        if isinstance(self.remind, str):
            try:
                self.remind = convert_to_seconds(self.remind)
                if self.remind < 60 * 60:
                    raise ValueError(f"Item {hilight(self.name)} remind must be at least 1 hour.")
            except KeyboardInterrupt:
                raise
            except Exception as e:
                raise ValueError(
                    f"Item {hilight(self.name)} remind {self.remind} is not recognized."
                ) from e

        if not isinstance(self.remind, int):
            raise ValueError(
                f"Item {hilight(self.name)} remind must be an time (e.g. 1 day) or false."
            )

    def handle_notify_with(self: "UserConfig") -> None:
        if self.notify_with is None:
            return

        if isinstance(self.notify_with, str):
            self.notify_with = [self.notify_with]

        if not isinstance(self.notify_with, list) or not all(
            isinstance(x, str) for x in self.notify_with
        ):
            raise ValueError(
                f"Item {hilight(self.name)} notify_with must be a list of notification section values."
            )


class User:
    def __init__(self: "User", config: UserConfig, logger: Logger | None = None) -> None:
        self.name = config.name
        self.config = config
        self.logger = logger

    @classmethod
    def get_config(cls: Type["User"], **kwargs: Any) -> UserConfig:
        return UserConfig(**kwargs)

    def notified_key(self: "User", listing: Listing) -> Tuple[str, str, str, str]:
        return (CacheType.USER_NOTIFIED.value, listing.marketplace, listing.id, self.name)

    def to_cache(self: "User", listing: Listing, local_cache: Cache | None = None) -> None:
        (cache if local_cache is None else local_cache).set(
            self.notified_key(listing),
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), listing.hash, listing.price),
            tag=CacheType.USER_NOTIFIED.value,
        )
        # Mirror onto the observation so the dashboard can tell which listings
        # were acted on without joining across cache namespaces.
        record_notification(listing, self.name, local_cache=local_cache)

    def _is_discounted(self: "User", old_price: str | None, new_price: str | None) -> bool:
        """Whether the listing is cheaper now than when the user was notified.

        Parsing goes through :func:`price_value`, which understands the formats
        the scraper actually stores -- including the space-grouped thousands
        Facebook uses for CLP ("450\u00a0000") and the "current | original"
        pair.  The parser this replaced stripped plain spaces only, so every
        Chilean price failed to convert and no price drop was ever noticed.

        A price that cannot be read counts as infinitely expensive, which keeps
        the original intent: an unreadable *old* price must not hide a real
        drop, and an unreadable *new* one must not be reported as one.  Infinity
        rather than a large constant, because a real price can exceed any
        constant -- the previous 999999999 is an ordinary asking price in COP.
        """

        def to_price(price_str: str | None) -> float:
            value = price_value(price_str)
            return float("inf") if value is None else value

        return to_price(old_price) > to_price(new_price)

    def notification_status(
        self: "User", listing: Listing, local_cache: Cache | None = None
    ) -> NotificationStatus:
        notified = (cache if local_cache is None else local_cache).get(self.notified_key(listing))
        # not notified before, or saved information is of old type
        if notified is None:
            return NotificationStatus.NOT_NOTIFIED

        if isinstance(notified, str):
            # old style cache
            notification_date, listing_hash, listing_price = notified, None, None
        else:
            assert isinstance(notified, tuple)
            if len(notified) == 2:
                notification_date, listing_hash, listing_price = (*notified, None)
            else:
                notification_date, listing_hash, listing_price = notified

        if listing_price is not None and self._is_discounted(listing_price, listing.price):
            return NotificationStatus.LISTING_DISCOUNTED

        # if listing_hash is not None, we need to check if the listing is still valid
        if listing_hash is not None and listing_hash != listing.hash:
            return NotificationStatus.LISTING_CHANGED

        # notified before and remind is None, so one notification will remain valid forever
        if self.config.remind is None:
            return NotificationStatus.NOTIFIED

        # if remind is not None, we need to check the time
        expired = datetime.strptime(notification_date, "%Y-%m-%d %H:%M:%S") + timedelta(
            seconds=self.config.remind
        )
        # if expired is in the future, user is already notified.
        return (
            NotificationStatus.NOTIFIED if expired > datetime.now() else NotificationStatus.EXPIRED
        )

    def last_notification(
        self: "User", listing: Listing, local_cache: Cache | None = None
    ) -> Tuple[datetime | None, str | None]:
        """When this user was last told about this listing, and at what price.

        The price is the whole reason this exists.  It is already in the cache
        -- :meth:`notification_status` reads it to decide whether a listing got
        cheaper -- but it was thrown away immediately afterwards, so a message
        could say "discounted" without being able to say discounted *from what*.
        Both halves are optional: entries written by older versions hold only a
        date, and one written before there was a price holds no price.
        """
        notified = (cache if local_cache is None else local_cache).get(
            self.notified_key(listing)
        )
        if notified is None:
            return None, None
        if isinstance(notified, str):
            stamp, price = notified, None
        elif len(notified) == 2:
            stamp, price = notified[0], None
        else:
            stamp, price = notified[0], notified[2]
        try:
            when = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            when = None
        return when, price if isinstance(price, str) else None

    def time_since_notification(
        self: "User", listing: Listing, local_cache: Cache | None = None
    ) -> int:
        key = self.notified_key(listing)
        notified = (cache if local_cache is None else local_cache).get(key)
        if notified is None:
            return -1

        notification_date = notified if isinstance(notified, str) else notified[0]
        return (datetime.now() - datetime.strptime(notification_date, "%Y-%m-%d %H:%M:%S")).seconds

    def notify(
        self: "User",
        listings: List[Listing],
        ratings: List[AIResponse],
        item_config: TItemConfig,
        local_cache: Cache | None = None,
        force: bool = False,
        language: str | None = None,
        marketplace_label: str | None = None,
        description_words: int | None = None,
    ) -> None:
        """Tell this user about these listings, once, through every channel.

        The cards are built here rather than inside the channels because this
        is the only place that can see the two facts that make a notification
        worth reading: what the user was last told this listing cost, which
        lives in this user's own cache entry, and which language to say it in.
        A channel asked to build its own card would have neither.
        """
        if self.config.enabled is False:
            if self.logger:
                self.logger.info(
                    f"""{hilight("[Notify]", "skip")} User {hilight(self.name)} is disabled."""
                )
            return
        statuses = [self.notification_status(listing, local_cache) for listing in listings]
        cards = []
        for listing, rating, status in zip(listings, ratings, statuses):
            when, price = self.last_notification(listing, local_cache)
            cards.append(
                build_card(
                    listing,
                    rating,
                    status,
                    previous_price=price,
                    notified_at=when,
                    language=language,
                    marketplace_label=marketplace_label,
                )
            )

        if NotificationConfig.notify_all(
            self.config,
            listings,
            ratings,
            statuses,
            force=force,
            logger=self.logger,
            cards=cards,
            language=language,
            description_words=description_words,
        ):
            counter.increment(CounterItem.NOTIFICATIONS_SENT, item_config.name)
            for listing, ns in zip(listings, statuses):
                if force or ns != NotificationStatus.NOTIFIED:
                    self.to_cache(listing, local_cache=local_cache)
