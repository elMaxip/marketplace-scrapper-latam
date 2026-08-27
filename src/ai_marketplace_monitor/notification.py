import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, fields
from enum import Enum
from logging import Logger
from typing import Any, ClassVar, DefaultDict, Deque, List, Optional, Tuple, Type

from .ai import AIResponse  # type: ignore
from .listing import Listing
from .messages import (
    DEFAULT_DESCRIPTION_WORDS,
    PLAIN,
    ListingCard,
    build_card,
    summary_title,
    truncate_description,
)
from .templates import template_for, validate_all as validate_templates
from .utils import BaseConfig, hilight


class NotificationStatus(Enum):
    NOT_NOTIFIED = 0
    EXPIRED = 1
    NOTIFIED = 2
    LISTING_CHANGED = 3
    LISTING_DISCOUNTED = 4
    #: The cheapest valid listing a search has, and it just became cheaper than
    #: the cheapest it had.
    #:
    #: Unlike the others this is not a fact about *this* listing -- it is a fact
    #: about the listing's position among all the others found for the same
    #: search -- which is why it cannot be worked out from the notification
    #: cache the way the rest are, and why it is decided in
    #: :mod:`ai_marketplace_monitor.toplist` and handed in rather than read.
    TOP_LISTING = 5
    #: A tracked product's stock has fallen to or below the number the user
    #: asked to be told about.
    #:
    #: Like ``TOP_LISTING`` and unlike the rest, this cannot be worked out from
    #: the notification cache: it is a comparison between what the page says now
    #: and a threshold in the config, so it is decided in
    #: :mod:`ai_marketplace_monitor.tracking` and handed in.
    LOW_STOCK = 6


@dataclass
class NotificationConfig(BaseConfig):
    required_fields: ClassVar[List[str]] = []

    #: The most characters this channel will carry in one message, or None
    #: when it has no meaningful limit.
    #:
    #: A fact about the service, not a preference, which is why it is a class
    #: attribute and not a configurable field: Telegram refuses a message over
    #: 4096 characters with "Message is too long", and no amount of the user
    #: wanting a longer one changes that.  Every channel that has a limit
    #: declares it here so the one place that renders a message can make it
    #: fit -- see :meth:`ai_marketplace_monitor.messages.ListingCard.render_within`.
    message_limit: ClassVar[int | None] = None

    max_retries: int = 5
    retry_delay: int = 60

    # Rate limiting configuration (disabled by default, but public for user config)
    rate_limit_enabled: bool = False
    instance_rate_limit: float = 1.0  # seconds between sends per instance
    global_rate_limit: int = 10  # messages per second across all instances

    # Subclasses that handle rate limiting in their own send path (e.g.
    # Telegram's async _wait_for_rate_limit) should set this to True so
    # the base class _execute_with_retry does NOT also apply sync rate
    # limiting — preventing double-wait.
    _handles_own_rate_limiting: bool = False

    # Private tracking attributes
    _last_send_time: float | None = None

    # Class-level global tracking (shared across all notification types)
    _global_send_times: ClassVar[Deque[float]] = deque()
    _global_lock: ClassVar[threading.Lock] = threading.Lock()

    def handle_max_retries(self: "NotificationConfig") -> None:
        if not isinstance(self.max_retries, int):
            raise ValueError("max_retries must be an integer.")

    def handle_retry_delay(self: "NotificationConfig") -> None:
        if not isinstance(self.retry_delay, int):
            raise ValueError("retry_delay must be an integer.")

    def _has_required_fields(self: "NotificationConfig") -> bool:
        return all(getattr(self, field, None) is not None for field in self.required_fields)

    @classmethod
    def get_config(
        cls: Type["NotificationConfig"], **kwargs: Any
    ) -> Optional["NotificationConfig"]:
        """Get the specific subclass name from the specified keys, for validation purposes"""
        for subclass in cls.__subclasses__():
            acceptable_keys = {field.name for field in fields(subclass)}
            if all(name in acceptable_keys for name in kwargs.keys()):
                return subclass(**{k: v for k, v in kwargs.items() if k != "type"})
            res = subclass.get_config(**kwargs)
            if res is not None:
                return res
        return None

    @classmethod
    def notify_all(
        cls: type["NotificationConfig"], config: "NotificationConfig", *args, **kwargs: Any
    ) -> bool:
        """Call the notify method of all subclasses"""
        succ = []
        for subclass in cls.__subclasses__():
            flds = {f.name for f in fields(subclass)}
            subclass_obj = subclass(**{k: getattr(config, k) for k in flds})
            if hasattr(subclass_obj, "notify") and subclass.__name__ not in [
                "UserConfig",
                "PushNotificationConfig",
            ]:
                assert hasattr(subclass_obj, "notify")
                succ.append(subclass_obj.notify(*args, **kwargs))
            # subclases
            if hasattr(subclass_obj, "notify_all"):
                succ.append(subclass.notify_all(config, *args, **kwargs))
        return any(succ)

    @classmethod
    def message_all(
        cls: type["NotificationConfig"],
        config: "NotificationConfig",
        title: str,
        message: str,
        logger: Logger | None = None,
    ) -> bool:
        """Send one plain message through every channel this user has.

        The same walk as :meth:`notify_all` and deliberately so, but for the
        notification that is not about a listing.  There is exactly one of those
        so far -- a shop has started refusing us -- and it needs no card, no
        rating and no status: it is a sentence about the monitor itself.

        Channels the user has not configured drop out on their own, inside
        :meth:`_execute_with_retry`, which is where "has the required fields"
        already lives.
        """
        sent = []
        for subclass in cls.__subclasses__():
            flds = {f.name for f in fields(subclass)}
            subclass_obj = subclass(**{k: getattr(config, k) for k in flds})
            if subclass.__name__ not in ("UserConfig", "PushNotificationConfig"):
                try:
                    sent.append(subclass_obj._execute_with_retry(title, message, logger))
                except KeyboardInterrupt:
                    raise
                except Exception:
                    sent.append(False)
            if hasattr(subclass_obj, "message_all"):
                sent.append(subclass.message_all(config, title, message, logger))
        return any(sent)

    def _execute_with_retry(
        self: "NotificationConfig",
        title: str,
        message: str,
        logger: Logger | None = None,
        apply_rate_limiting: bool = False,
    ) -> bool:
        """Common retry logic for message sending with optional rate limiting."""
        if not self._has_required_fields():
            return False

        for attempt in range(self.max_retries):
            try:
                # Apply rate limiting if requested
                if apply_rate_limiting and self.rate_limit_enabled:
                    self._wait_for_rate_limit_sync(logger)

                # Call the send_message method
                res = self.send_message(title=title, message=message, logger=logger)

                if logger:
                    logger.info(
                        f"""{hilight("[Notify]", "succ")} Sent {self.name} a message with title {hilight(title)}"""
                    )
                return res
            except KeyboardInterrupt:
                raise
            except Exception as e:
                if logger:
                    logger.debug(
                        f"""{hilight("[Notify]", "fail")} Attempt {attempt + 1} failed: {e}"""
                    )
                if attempt < self.max_retries - 1:
                    if logger:
                        logger.debug(
                            f"""{hilight("[Notify]", "fail")} Retrying in {self.retry_delay} seconds..."""
                        )
                    time.sleep(self.retry_delay)
                else:
                    if logger:
                        logger.error(
                            f"""{hilight("[Notify]", "fail")} Max retries reached. Failed to push note to {self.name}."""
                        )
                    return False
        return False

    def _send_message_with_rate_limiting_sync(
        self: "NotificationConfig",
        title: str,
        message: str,
        logger: Logger | None = None,
    ) -> bool:
        """Sync version of send_message_with_retry with rate limiting support."""
        return self._execute_with_retry(title, message, logger, apply_rate_limiting=True)

    def send_message_with_retry(
        self: "NotificationConfig",
        title: str,
        message: str,
        logger: Logger | None = None,
    ) -> bool:
        """Enhanced retry method with rate limiting support.

        Subclasses that set ``_handles_own_rate_limiting = True`` (e.g.
        Telegram, which applies async rate limiting inside its own
        ``send_message``) will NOT get sync rate limiting here —
        avoiding a double-wait.
        """
        apply = self.rate_limit_enabled and not self._handles_own_rate_limiting
        return self._execute_with_retry(title, message, logger, apply_rate_limiting=apply)

    def _get_wait_time(self: "NotificationConfig") -> float:
        """Calculate instance-level wait time. Override for custom logic."""
        if not self.rate_limit_enabled or self._last_send_time is None:
            return 0.0

        elapsed = time.time() - self._last_send_time
        return max(0.0, self.instance_rate_limit - elapsed)

    @classmethod
    def _get_global_wait_time(cls: Type["NotificationConfig"]) -> float:
        """Calculate global wait time across all instances.

        Note: this is only called from _wait_for_rate_limit[_sync] which
        already gates on rate_limit_enabled, so non-rate-limited instances
        never reach here and never populate _global_send_times.
        """
        with cls._global_lock:
            # Check if any instance has rate limiting enabled by checking if we have any tracked times
            # This is a more practical approach than checking class attributes
            if not cls._global_send_times:
                return 0.0

            current_time = time.time()

            # Remove timestamps older than 1 second
            while cls._global_send_times and current_time - cls._global_send_times[0] > 1.0:
                cls._global_send_times.popleft()

            # Use a reasonable default global rate limit (30 msg/sec like Telegram)
            # Individual classes can override this behavior
            global_rate_limit = getattr(cls, "global_rate_limit", 30)

            # If we have less than the rate limit, no wait needed
            if len(cls._global_send_times) < global_rate_limit:
                return 0.0

            # If we're at the limit, wait until the oldest message is more than 1 second old
            oldest_send_time = cls._global_send_times[0]
            wait_time = 1.0 - (current_time - oldest_send_time)
            return max(0.0, wait_time)

    @classmethod
    def _record_global_send_time(cls: Type["NotificationConfig"]) -> None:
        """Record the current time as a global send time."""
        with cls._global_lock:
            cls._global_send_times.append(time.time())

    def _wait_for_rate_limit_sync(
        self: "NotificationConfig", logger: Logger | None = None
    ) -> None:
        """Wait for rate limits and record send time (synchronous version)."""
        if not self.rate_limit_enabled:
            return

        # Check both per-instance and global rate limits
        instance_wait = self._get_wait_time()
        global_wait = self._get_global_wait_time()

        # Use the longer of the two wait times
        wait_time = max(instance_wait, global_wait)

        if wait_time > 0:
            if logger:
                if global_wait > instance_wait:
                    logger.debug(
                        f"Rate limiting: waiting {wait_time:.1f} seconds (global limit: {self.global_rate_limit}s)"
                    )
                else:
                    logger.debug(
                        f"Rate limiting: waiting {wait_time:.1f} seconds (instance limit: {self.instance_rate_limit}s)"
                    )

            time.sleep(wait_time)

        # Record both per-instance and global send times
        self._last_send_time = time.time()
        self._record_global_send_time()

    async def _wait_for_rate_limit(
        self: "NotificationConfig", logger: Logger | None = None
    ) -> None:
        """Wait for rate limits and record send time (async version for Telegram)."""
        if not self.rate_limit_enabled:
            return

        import asyncio

        # Check both per-instance and global rate limits
        instance_wait = self._get_wait_time()
        global_wait = self._get_global_wait_time()

        # Use the longer of the two wait times
        wait_time = max(instance_wait, global_wait)

        if wait_time > 0:
            if logger:
                if global_wait > instance_wait:
                    logger.debug(
                        f"Global rate limiting: waiting {wait_time:.1f} seconds (limit: {self.global_rate_limit} msg/sec)"
                    )
                else:
                    logger.debug(
                        f"Rate limiting: waiting {wait_time:.1f} seconds (limit: {self.instance_rate_limit}s)"
                    )

            await asyncio.sleep(wait_time)

        # Record both per-instance and global send times
        self._last_send_time = time.time()
        self._record_global_send_time()

    def send_message(
        self: "NotificationConfig",
        title: str,
        message: str,
        logger: Logger | None = None,
    ) -> bool:
        raise NotImplementedError("send_message needs to be defined.")


@dataclass
class PushNotificationConfig(NotificationConfig):
    notify_method = "push_notification"
    message_format: str | None = None
    with_description: int | None = None
    #: How many words of the seller's own text a notification carries.
    #:
    #: ``None`` means "whatever the monitor was configured with"
    #: (``[monitor] max_description_words``, 25 by default); the field exists
    #: on the channel as well so one user can be told more or less than
    #: another without changing the system-wide answer.  Zero or below is
    #: "no limit", which has to stay expressible or the limit could not be
    #: switched off.
    max_description_words: int | None = None

    #: The message this channel sends, written by the user, one per kind of
    #: notification.  Empty (the default) means the built-in card -- so this
    #: whole feature is inert until somebody asks for it.
    #:
    #: Per channel rather than system-wide because the channels are not alike:
    #: an email has room for the description and a lock-screen notification has
    #: room for a price.  See :mod:`ai_marketplace_monitor.templates` for the
    #: placeholders and for why the user's own text is escaped along with the
    #: values.
    template: str | None = None
    #: Used for a listing nobody has been told about yet.
    template_new: str | None = None
    #: Used when a known listing got cheaper.
    template_price_drop: str | None = None
    #: Used when a search's cheapest valid listing gets cheaper.
    template_top: str | None = None
    #: Used when the seller edited the post.
    template_updated: str | None = None
    #: Used when the ``remind`` interval came round again.
    template_reminder: str | None = None
    #: Used when a tracked product is running out.
    template_low_stock: str | None = None

    def handle_template(self: "PushNotificationConfig") -> None:
        """Refuse a template with a placeholder that is not real.

        At load time and not at send time, which is the whole point: a typo like
        ``{titel}`` renders as nothing, so a template validated on the way out
        would silently drop the title of every notification and look like a
        channel that had simply stopped saying what things are.
        """
        problems = validate_templates(self)
        if problems:
            raise ValueError(" ".join(problems))

    def template_for(self: "PushNotificationConfig", status: Any) -> str | None:
        """The template to use for one kind of notification, or None."""
        return template_for(self, getattr(status, "name", None))

    def handle_message_format(self: "PushNotificationConfig") -> None:
        if self.message_format is None:
            self.message_format = "plain_text"

        if self.message_format not in ["plain_text", "markdown", "html"]:
            raise ValueError("message_format must be 'plain_text', 'markdown', or 'html'.")

    def handle_with_description(self: "PushNotificationConfig") -> None:
        if self.with_description is None:
            return

        if self.with_description is True:
            self.with_description = 1
        elif self.with_description is False:
            self.with_description = 0

        if not isinstance(self.with_description, int) or self.with_description < 0:
            raise ValueError("with_description must be a boolean or a positive integer number.")

    def handle_max_description_words(self: "PushNotificationConfig") -> None:
        if self.max_description_words is None:
            return
        if self.max_description_words is True:
            self.max_description_words = DEFAULT_DESCRIPTION_WORDS
        elif self.max_description_words is False:
            self.max_description_words = 0
        if not isinstance(self.max_description_words, int):
            raise ValueError("max_description_words must be a number of words, or false.")

    def notify(
        self: "PushNotificationConfig",
        listings: List[Listing],
        ratings: List[AIResponse],
        notification_status: List[NotificationStatus],
        force: bool = False,
        logger: Logger | None = None,
        cards: List[ListingCard] | None = None,
        language: str | None = None,
        description_words: int | None = None,
    ) -> bool:
        """Tell this channel about the listings it has not been told about.

        The message itself is not built here any more.  Each listing becomes a
        :class:`~ai_marketplace_monitor.messages.ListingCard` -- the facts,
        resolved, nothing rendered -- and the channel renders it in whatever it
        can show.  That indirection is what lets Telegram attach the listing's
        photo: the card still knows which listing it is, which a pre-joined
        block of text for six listings does not.

        ``cards`` come from :meth:`ai_marketplace_monitor.user.User.notify`,
        which is the only caller that can see the previous price and the
        language.  Built here from the listings alone when it does not -- the
        message is then simply missing the "was 399.990" line, which is the
        honest result of not knowing it.
        """
        if not self._has_required_fields():
            if logger:
                logger.debug(
                    f"Missing required fields  {', '.join(self.required_fields)}. "
                    f"No {self.notify_method} notification sent."
                )
            return False

        if cards is None:
            cards = [
                build_card(listing, rating, status, language=language)
                for listing, rating, status in zip(listings, ratings, notification_status)
            ]

        # Grouped by what happened to them, because that is what the one line a
        # phone shows on the lock screen has to say: three new listings and one
        # that got cheaper are two different pieces of news.
        batches: DefaultDict[NotificationStatus, List[Tuple[Listing, ListingCard]]]
        batches = defaultdict(list)
        for listing, card, status in zip(listings, cards, notification_status):
            if status == NotificationStatus.NOTIFIED and not force:
                continue
            card.description = self._description_for(listing, description_words)
            batches[status].append((listing, card))

        if not batches:
            if logger:
                logger.debug("No new listings to notify.")
            return False

        for status, batch in batches.items():
            title = summary_title(
                [card for _listing, card in batch],
                status.name,
                # The card's name for the platform, not the listing's: one is
                # "Mercado Libre" and the other is the key in a config file.
                batch[0][1].marketplace,
                language=language,
            )
            # Chosen per batch and not per card: a batch is one kind of news
            # by construction (that is what the grouping above is for), and a
            # message that mixed two templates would be a message about two
            # different things.
            if not self.send_items(
                title, batch, logger=logger, template=self.template_for(status)
            ):
                return False
        return True

    def _description_for(
        self: "PushNotificationConfig",
        listing: Listing,
        description_words: int | None = None,
    ) -> str:
        """As much of the seller's own text as this channel was asked to carry.

        Two limits, applied in that order and answering different questions.
        ``with_description`` is per channel and counted in characters -- what
        it always meant.  The word limit is the system-wide one
        (``[monitor] max_description_words``, overridable per channel), and it
        exists because a Mercado Libre seller who pastes their catalogue into
        the description produces a message the service rejects outright.

        Characters first, then words: the character cut is the older, narrower
        setting and doing it second would let it re-cut text the word limit had
        already ended cleanly, leaving two ellipses in a row.
        """
        text = listing.description
        if self.with_description is not None:
            if self.with_description == 0:
                return ""
            if self.with_description > 1 and len(text) >= self.with_description:
                text = text[: self.with_description] + "..."
        # The channel's own answer wins when it has one; otherwise whatever the
        # monitor was configured with reaches here as `description_words`.
        words = (
            self.max_description_words
            if self.max_description_words is not None
            else description_words
        )
        return truncate_description(text, words)

    def send_items(
        self: "PushNotificationConfig",
        title: str,
        items: List[Tuple[Listing, ListingCard]],
        logger: Logger | None = None,
        template: str | None = None,
    ) -> bool:
        """Send one batch of cards.  Text, joined, which is all most channels do.

        Overridden by a channel that can do better -- Telegram sends each card
        as its listing's photo with the text as the caption -- and the reason
        this is a method rather than a branch: adding a channel that can carry
        pictures should not mean editing the one that cannot.

        ``template`` is the user's own wording for this kind of notification,
        already chosen by :meth:`notify` because it is the batch, not the card,
        that has a kind.  None means the built-in card.
        """
        fmt = self.message_format or PLAIN
        if self.message_limit is None:
            message = "\n\n".join(card.render(fmt, template=template) for _listing, card in items)
            return self.send_message_with_retry(title, message, logger=logger)

        # The title counts: most channels put it at the top of the same message
        # the limit applies to, and forgetting it is how a message built to sit
        # exactly on the limit ends up over it.  The margin covers the blank
        # line under the title and the " (2/3)" a split batch adds to it --
        # which is the same mistake one level down, and just as easy to make.
        room = max(1, self.message_limit - len(title) - 16)
        # More listings than fit go in more messages rather than fewer
        # listings: a batch of six that arrives as four is a notification that
        # quietly lied about what was found.
        messages: List[str] = []
        current = ""
        for _listing, card in items:
            text = card.render_within(room, fmt, template=template)
            joined = text if not current else f"{current}\n\n{text}"
            if len(joined) <= room:
                current = joined
                continue
            if current:
                messages.append(current)
            current = text
        if current:
            messages.append(current)

        ok = True
        for index, message in enumerate(messages, 1):
            heading = title if len(messages) == 1 else f"{title} ({index}/{len(messages)})"
            if not self.send_message_with_retry(heading, message, logger=logger):
                ok = False
        return ok
