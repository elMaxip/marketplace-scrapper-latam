"""What a notification actually says, decided once and rendered per channel.

The messages this replaces were assembled inside
:meth:`ai_marketplace_monitor.notification.PushNotificationConfig.notify`, in
three near-identical branches -- one per message format -- each gluing together
a title, a price, a location, a URL and the whole of the AI's commentary.  Two
things were wrong with that, and only the second one is about taste:

* **The useful facts were buried.**  A phone notification is read in about a
  second, standing up, and what it has to answer is "is this worth opening?".
  That is the price, what the price used to be, and where.  A paragraph of AI
  prose above the number is a paragraph between the reader and the answer.
* **It could not carry a picture.**  Telegram will happily send the listing's
  photo with the text attached to it, which is worth more than any amount of
  description -- but only if something knows the message belongs to *one*
  listing.  A pre-joined block of text for six listings does not.

So a notification is built here as a :class:`ListingCard` per listing: the
facts, already resolved, with nothing rendered yet.  Each channel then renders
the same card in whatever it can display, and a channel that can do better than
text -- Telegram, so far -- has the listing's image and URL to hand because the
card never lost them.

The shape of a rendered card, in the order the facts are wanted:

    🎮 PS5 Slim

    💰 $399.990 → $359.990
    📉 Bajó $40.000 (-10%)

    ⭐ 4,8/5 · Muy buena
    📍 Santiago
    🛒 Mercado Libre

    🔗 Ver publicación

Every line is dropped when its fact is missing, and a card with nothing but a
title and a link is a perfectly good card -- which matters, because half of what
a marketplace prints is missing half of the time.

Prices are never re-formatted.  They are shown exactly as the marketplace
printed them, for the reason the rest of the monitor stores them that way: a
Facebook listing in Chile prints "450 000" with no symbol at all, and inventing
a "$" for it is inventing a fact.  Only the *difference* between two prices is
computed and formatted here, and only when both of them parse.
"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import templates as user_template
from .listing import Listing
from .utils import price_value

#: The formats a channel can ask for.  The first three are the ones
#: :class:`~ai_marketplace_monitor.notification.PushNotificationConfig` has
#: always offered, so a configuration written before this module is unaffected.
PLAIN, MARKDOWN, HTML = "plain_text", "markdown", "html"

#: Telegram's dialect, which is a fourth format and not a flavour of the third.
#: Its escaping rules are the reason: MarkdownV2 requires *every* one of a
#: dozen punctuation characters to be backslashed wherever it appears, so a
#: message cannot be built as markdown and escaped afterwards -- that would
#: escape the markup along with the text and deliver the asterisks literally.
#: The escaping therefore has to happen fragment by fragment as the card is
#: assembled, which is what this format is.
MARKDOWN_V2 = "markdown_v2"

#: Every character MarkdownV2 insists on seeing backslashed.  Prices are full
#: of three of them ("$399.990" has a dot, "(-10%)" has both brackets and a
#: hyphen), which is why getting this wrong shows up immediately as a message
#: Telegram refuses outright rather than as a subtly wrong one.
_MDV2_SPECIAL = r"_*[]()~`>#+-=|{}.!"


def escape_markdown_v2(text: str) -> str:
    """Backslash everything MarkdownV2 treats as punctuation."""
    return "".join("\\" + char if char in _MDV2_SPECIAL else char for char in text)

#: The scraper's placeholder for a field a marketplace never printed.
UNSPECIFIED = "**unspecified**"


# --------------------------------------------------------------------------- #
# Length
# --------------------------------------------------------------------------- #
#
# Two different limits, and confusing them is what produced "Message is too
# long" from Telegram.
#
# The first is a *choice*: a Mercado Libre seller who pastes their whole
# catalogue into the description turns a notification into a wall of text that
# answers nothing, so only the opening of it is carried.  That is
# `truncate_description`, it is configured, and it is about readability.
#
# The second is a *fact about the channel*: Telegram refuses a message over
# 4096 characters outright, Pushover over 1024.  Limiting the description does
# not settle it -- the AI's commentary, a long title and a long URL can exceed
# the cap between them with no description at all -- so the whole rendered card
# is measured against the channel's cap and shortened until it fits.  That is
# `ListingCard.render_within`, it is not configurable, and it is about the
# message arriving at all.

#: How many words of the seller's own text a notification carries when nothing
#: says otherwise.  About two lines on a phone, which is as much of a
#: notification as gets read before the reader decides whether to open it.
DEFAULT_DESCRIPTION_WORDS = 25

#: What marks text that had more after it.
ELLIPSIS = "..."


def truncate_description(text: str | None, max_words: int | None = None) -> str:
    """As much of the seller's own text as ``max_words`` allows.

    Words, not lines, and the difference is the whole point of this function.
    A line is not a property of the text at all -- it is a property of the
    screen showing it.  "Five lines" is five short ones on a desktop and
    fifteen wrapped ones on a phone, and a seller who writes one enormous
    unbroken paragraph slips past a line limit completely with a description
    that fills the screen.  Counting words measures the text itself, so the
    answer is the same on every device.

    ``None`` and any value below 1 mean "all of it" -- the setting switched
    off, which has to stay possible or the limit could not be undone.

    Whitespace inside what is kept is left as the seller wrote it, newlines
    included: only the tail is dropped.  A description at or under the limit
    comes back untouched and unmarked; one over it ends in "...", so it is
    visible that there was more.
    """
    body = (text or "").strip()
    if not body:
        return ""
    if max_words is None or max_words < 1:
        return body

    # Walked rather than `split()`, so the words that are kept keep the spacing
    # and the line breaks they had.  Rebuilding from a split would quietly
    # reflow the seller's paragraphs into a single line.
    words = 0
    cut = len(body)
    index = 0
    while index < len(body):
        if body[index].isspace():
            index += 1
            continue
        words += 1
        while index < len(body) and not body[index].isspace():
            index += 1
        if words == max_words:
            cut = index
            break
    if words < max_words or cut >= len(body):
        # Never reached the allowance, or reached it on the very last word:
        # nothing was dropped, so nothing is marked either.
        return body
    return body[:cut].rstrip() + ELLIPSIS


def shorten(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, on a word boundary where there is one.

    The ellipsis is inside the limit, not added to it -- the caller's limit is
    the channel's, and a message one character over is refused just as firmly
    as one a thousand over.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(ELLIPSIS):
        return ELLIPSIS[:limit]
    head = text[: limit - len(ELLIPSIS)]
    # Only back up to a space when one is near enough that the result is still
    # most of what was asked for; a URL or a CJK sentence has no spaces at all,
    # and hunting for one there would throw away the whole message.
    space = head.rfind(" ")
    if space >= len(head) - 40 and space > 0:
        head = head[:space]
    return head.rstrip() + ELLIPSIS


def _clean(value: str | None) -> str | None:
    """A scraped field, or None when it holds nothing worth showing."""
    text = (value or "").strip()
    return None if not text or text == UNSPECIFIED else text


def split_price(price: str | None) -> tuple[str | None, str | None]:
    """A stored price as ``(asking now, struck through)``.

    The scraper stores a discounted listing as ``"$74.990 | $99.990"`` -- what
    it costs, then the price the marketplace is showing crossed out -- because
    that is what the page said and the store keeps what the page said.  A
    notification must not: printing the pair raw gives "$89.990 → $74.990 |
    $99.990", which reads as three prices and answers nothing.

    The two halves are two different facts and the card has a place for each:
    the first is the price, the second is a "was", and the arrow between them is
    exactly what the marketplace's own strike-through means.
    """
    text = _clean(price)
    if text is None:
        return None, None
    if "|" not in text:
        return text, None
    current, _, original = text.partition("|")
    return _clean(current), _clean(original)


# --------------------------------------------------------------------------- #
# Words
# --------------------------------------------------------------------------- #
#
# A notification is read by the person who configured the monitor, in their own
# language, and there are eight phrases in it.  A full translation layer for
# eight phrases would be a lot of machinery to maintain; a dictionary is not,
# and an unknown language falls back to English rather than to nothing.

PHRASES: Dict[str, Dict[str, str]] = {
    "en": {
        "dropped": "Down",
        "rose": "Up",
        "view": "View listing",
        "sold": "sold",
        "gone": "no longer listed",
        "new": "new",
        "updated": "updated",
        "cheaper": "cheaper",
        "reminder": "reminder",
        "top": "cheapest",
        "low_stock": "running out",
        "new_plural": "new",
        "updated_plural": "updated",
        "cheaper_plural": "cheaper",
        "reminder_plural": "reminder",
        "top_plural": "cheapest",
        "low_stock_plural": "running out",
        "found_one": "{count} {kind} listing on {marketplace}",
        "found_many": "{count} {kind} listings on {marketplace}",
        # The top-1 message names the search rather than counting listings:
        # there is exactly one of them and which product it is for is the
        # whole point.
        "new_top": "New cheapest for {item}",
    },
    "es": {
        "dropped": "Bajó",
        "rose": "Subió",
        "view": "Ver publicación",
        "sold": "vendida",
        "gone": "ya no está publicada",
        "new": "nueva",
        "updated": "actualizada",
        "cheaper": "más barata",
        "reminder": "ya vista",
        "top": "la más barata",
        "low_stock": "por agotarse",
        # Spanish agrees adjectives with the noun, so the plural is a second
        # entry rather than an "s" glued on: "más barata" does not pluralise by
        # suffix, and neither will anything else worth adding here later.
        "new_plural": "nuevas",
        "updated_plural": "actualizadas",
        "cheaper_plural": "más baratas",
        "reminder_plural": "ya vistas",
        "top_plural": "las más baratas",
        "low_stock_plural": "por agotarse",
        "found_one": "{count} publicación {kind} en {marketplace}",
        "found_many": "{count} publicaciones {kind} en {marketplace}",
        "new_top": "Nuevo top 1 en {item}",
    },
}


def phrases_for(language: str | None) -> Dict[str, str]:
    """The phrase table for a language tag, falling back to English.

    Tags arrive in whatever shape the configuration used -- ``es``, ``es_LA``,
    ``es-CL`` -- so only the part before the separator is consulted.
    """
    code = (language or "en").strip().lower().replace("-", "_").split("_")[0]
    return PHRASES.get(code, PHRASES["en"])


# --------------------------------------------------------------------------- #
# Numbers
# --------------------------------------------------------------------------- #


#: Everything a scraped price puts before its first digit.  That is the unit
#: the difference between two such prices is in.
_SYMBOL_RE = re.compile(r"^([^0-9]*)")


def symbol_of(price: str | None) -> str:
    """Whatever the marketplace printed in front of the number, or "".

    Used to mark a computed difference the same way: if the page said
    "$399.990" the drop is "$40.000", and if it said "450 000" -- which is what
    Facebook does in Chile -- the drop is "40.000" with no symbol at all,
    because inventing one is inventing a fact about the currency.
    """
    text = (price or "").split("|")[0].strip()
    matched = _SYMBOL_RE.match(text)
    return matched.group(1).strip() if matched else ""


def _group(amount: float, language: str | None) -> str:
    """A whole number with thousands grouped the way the reader expects.

    Spanish groups with a dot, English with a comma.  Deliberately no currency:
    this is a *difference* between two prices whose own symbols -- if the
    marketplace printed any -- are shown beside it untouched.
    """
    whole = f"{abs(amount):,.0f}" if abs(amount) >= 1 else f"{abs(amount):,.2f}"
    if phrases_for(language) is PHRASES["es"]:
        # "1,234.5" -> "1.234,5", in one pass so the two separators cannot
        # trample each other.
        whole = whole.translate(str.maketrans({",": ".", ".": ","}))
    return whole


@dataclass
class PriceChange:
    """How this price compares with the one the user was last told about."""

    #: ``"down"`` or ``"up"``.  A price that did not move produces no change.
    direction: str
    #: The absolute difference, always positive.
    amount: float
    #: The difference as a percentage of the old price, or None when the old
    #: price was zero (a free listing that is now not free has no percentage).
    percent: float | None

    def sentence(
        self: "PriceChange", language: str | None = None, symbol: str = ""
    ) -> str:
        """"Bajó 40.000 (-10%)" -- the one line that says what happened."""
        words = phrases_for(language)
        word = words["dropped"] if self.direction == "down" else words["rose"]
        text = f"{word} {symbol}{_group(self.amount, language)}"
        if self.percent is not None:
            sign = "-" if self.direction == "down" else "+"
            text += f" ({sign}{self.percent:.0f}%)"
        return text

    @property
    def icon(self: "PriceChange") -> str:
        return "📉" if self.direction == "down" else "📈"


def price_change(previous: str | None, current: str | None) -> Optional[PriceChange]:
    """The movement between two scraped prices, or None when there is none.

    None covers every case where the answer would be a guess: no previous
    price, a price that cannot be parsed, or two prices that are the same.  The
    monitor's own price parser is used rather than a second one here, so a
    Chilean "450 000" and a discounted "180 000 | 200 000" read the same way
    they do everywhere else.
    """
    was, now = price_value(previous), price_value(current)
    if was is None or now is None or was == now:
        return None
    difference = abs(now - was)
    return PriceChange(
        direction="down" if now < was else "up",
        amount=difference,
        percent=(difference / was * 100.0) if was else None,
    )


# --------------------------------------------------------------------------- #
# The card
# --------------------------------------------------------------------------- #


def _safe_cut(text: str, limit: int, message_format: str) -> str:
    """``text`` cut to ``limit``, with the markup the cut broke taken back off.

    Only ever reached for a limit too small to hold a card at all, and even
    then it must not hand back something the service will refuse for a
    *second* reason.  Two ways a cut breaks a message:

    * MarkdownV2 escapes are two characters, and cutting between them leaves a
      backslash with nothing to escape -- which Telegram rejects.
    * HTML tags are not one character either, and half of ``<a href=`` is not
      markup, it is a message ending in a broken tag.
    """
    cut = text[:limit]
    if message_format in (MARKDOWN_V2, MARKDOWN):
        # An odd number of trailing backslashes means the last one lost its
        # character.
        stripped = cut.rstrip("\\")
        if (len(cut) - len(stripped)) % 2:
            cut = cut[:-1]
    elif message_format == HTML:
        opened = cut.rfind("<")
        if opened > cut.rfind(">"):
            cut = cut[:opened]
    return cut


@dataclass
class ListingCard:
    """One listing, as a notification: facts resolved, nothing rendered.

    Built by :func:`build_card` and rendered by :meth:`render`.  Kept as data
    between those two so a channel that can do more than text -- attach the
    photo, make the title a link -- still has the pieces to do it with.
    """

    title: str
    url: str
    marketplace: str
    price: str | None = None
    previous_price: str | None = None
    change: PriceChange | None = None
    location: str | None = None
    condition: str | None = None
    rating: int | None = None
    rating_max: int = 5
    verdict: str | None = None
    comment: str | None = None
    #: A short word for what happened to this listing since last time: new,
    #: updated, cheaper, a reminder.  Empty when there is nothing to say.
    status: str | None = None
    image: str | None = None
    #: When the user was last told about this listing, when they were.
    notified_at: datetime | None = None
    language: str | None = None
    #: Kept for the channels that were built around it, and shown last.
    description: str = ""
    #: The search this listing turned up under, for ``{item}`` in a template.
    item: str | None = None
    #: What the shop says is left, and whether it can be bought at all.
    #:
    #: Never on the built-in card: a marketplace listing has neither, so a line
    #: for them would be blank on almost every notification the monitor sends.
    #: They exist for the one message that is *about* them -- a tracker running
    #: out of stock -- and for a template that asks.
    stock: str | None = None
    availability: str | None = None
    #: Who is selling, for ``{seller}``.  Not on the built-in card -- it is one
    #: line of a phone notification that almost never helps -- but a template
    #: the user wrote is allowed to want it.
    seller: str | None = None
    #: Whatever else the caller wants to carry through, unrendered.
    extra: Dict[str, Any] = field(default_factory=dict)

    def template_values(self: "ListingCard") -> Dict[str, Any]:
        """This card as the flat mapping a user template is filled in from.

        Flat and stringly-typed on purpose: a template is a string the user
        wrote, and every hole in it is filled with text.  The two computed
        entries -- the difference and the percentage -- are why this is a method
        rather than an ``asdict``: they exist only when both prices parsed, and
        a template saying "Bajó {discount}" has to drop that line rather than
        say "Bajó " when they did not.
        """
        change = self.change
        symbol = symbol_of(self.price)
        return {
            "notification_type": self.status or "",
            "title": self.title,
            "price": self.price or "",
            # An alias, so a price-drop template reads the way its subject does:
            # "de {old_price} a {new_price}".
            "new_price": self.price or "",
            "old_price": self.previous_price or "",
            "discount": (
                f"{symbol}{_group(change.amount, self.language)}" if change is not None else ""
            ),
            "discount_percent": (
                f"{'-' if change.direction == 'down' else '+'}{change.percent:.0f}%"
                if change is not None and change.percent is not None
                else ""
            ),
            "location": self.location or "",
            "condition": self.condition or "",
            "stock": self.stock or "",
            "availability": self.availability or "",
            "marketplace": self.marketplace,
            "item": self.item or "",
            "seller": self.seller or "",
            "rating": "" if self.rating is None else str(self.rating),
            "verdict": self.verdict or "",
            "comment": self.comment or "",
            "description": self.description or "",
            "url": self.url,
            "link": self.url,
            "image": self.image or "",
        }

    # -- the individual lines, so a channel can pick and choose --------------

    def price_line(self: "ListingCard") -> str | None:
        """"$399.990 → $359.990", or just the price when there is no history."""
        if not self.price:
            return None
        if self.previous_price and self.previous_price != self.price and self.change:
            return f"{self.previous_price} → {self.price}"
        return self.price

    def change_line(self: "ListingCard") -> str | None:
        if self.change is None:
            return None
        return self.change.sentence(self.language, symbol_of(self.price))

    def facts(self: "ListingCard") -> List[str]:
        """The one-per-line block under the price: rating, place, platform."""
        lines: List[str] = []
        if self.rating is not None:
            score = f"⭐ {self.rating}/{self.rating_max}"
            if self.verdict:
                score += f" · {self.verdict}"
            lines.append(score)
        if self.location:
            lines.append(f"📍 {self.location}")
        if self.condition:
            lines.append(f"🏷️ {self.condition}")
        lines.append(f"🛒 {self.marketplace}")
        return lines

    # -- rendering ----------------------------------------------------------

    def link_label(self: "ListingCard") -> str:
        """The words on the link, for a channel that makes it a button."""
        return phrases_for(self.language)["view"]

    def _writers(
        self: "ListingCard", message_format: str, link: bool
    ) -> Tuple[Any, Any, Any, str]:
        """``(escape, bold, anchor, newline)`` for one format.

        The whole of the difference between the four formats, in one place, so
        that a template and the built-in card are escaped by exactly the same
        rules.  Assembling them separately is how the three original branches
        drifted into saying three slightly different things.
        """
        if message_format == HTML:
            return (
                lambda text: html_module.escape(text, quote=True),
                lambda text: f"<b>{text}</b>",
                (lambda label, url: f'<a href="{url}">{label}</a>') if link else None,
                "<br>",
            )
        if message_format == MARKDOWN_V2:
            return (
                escape_markdown_v2,
                lambda text: f"*{text}*",
                (lambda label, url: f"[{label}]({url})") if link else None,
                "\n",
            )
        if message_format == MARKDOWN:
            return (
                lambda text: text,
                lambda text: f"**{text}**",
                (lambda label, url: f"[{label}]({url})") if link else None,
                "\n",
            )
        return (
            lambda text: text,
            lambda text: text,
            (lambda label, url: f"{label}: {url}") if link else None,
            "\n",
        )

    def render(
        self: "ListingCard",
        message_format: str = PLAIN,
        link: bool = True,
        template: str | None = None,
    ) -> str:
        """The card as text in one of the four formats.

        ``link`` is False for a channel that puts the URL somewhere of its own
        -- a Telegram photo caption with a button under it, say -- so the same
        card does not carry the address twice.

        ``template`` replaces the built-in shape with one the user wrote; see
        :mod:`ai_marketplace_monitor.templates`.  It goes through the very same
        escaper as the built-in card, so a template cannot produce a message the
        channel refuses outright -- which is what this indirection is for, not a
        nicety: an unescaped "." in a MarkdownV2 message is not a cosmetic
        problem, it is a message that never arrives.
        """
        esc, bold, anchor, newline = self._writers(message_format, link)
        if template and template.strip():
            return user_template.render(
                template,
                self.template_values(),
                esc=esc,
                write_link=(
                    (lambda url: anchor(esc(self.link_label()), url))
                    if anchor is not None
                    else None
                ),
                newline=newline,
            )
        return self._blocks(esc, bold=bold, anchor=anchor, newline=newline)

    def render_within(
        self: "ListingCard",
        limit: int | None,
        message_format: str = PLAIN,
        link: bool = True,
        template: str | None = None,
    ) -> str:
        """The card, rendered so that it fits in ``limit`` characters.

        The card is shortened and *re-rendered* rather than rendered and then
        cut.  Cutting the rendered text is what a naive fix would do and it is
        wrong in two separate ways: in MarkdownV2 it can slice a backslash away
        from the character it escapes, which Telegram refuses outright -- the
        very error this exists to stop -- and in HTML it can leave a tag open.
        Shortening a field and rendering again cannot produce either.

        Fields go in order of what the reader can most afford to lose.  The
        seller's own description first, because the point of it was carried by
        the first line; then the AI's commentary; and only then the title,
        which is the last thing to give up because a card whose title is gone
        no longer says what it is about.  The price, the platform and the link
        are never touched: they are the message.
        """
        text = self.render(message_format, link, template)
        if limit is None or len(text) <= limit:
            return text

        card = replace(self)
        # Trim rather than drop, twice, so a long description usually survives
        # as its first sentence or two.  Rendering is what measures, because
        # escaping and markup are part of the length the channel counts.
        for _attempt in range(2):
            if not card.description:
                break
            over = len(text) - limit
            room = max(0, len(card.description) - over - len(ELLIPSIS))
            card = replace(card, description=shorten(card.description, room))
            text = card.render(message_format, link, template)
            if len(text) <= limit:
                return text

        for shorter in (
            replace(card, description=""),
            replace(card, description="", comment=None),
        ):
            text = shorter.render(message_format, link, template)
            if len(text) <= limit:
                return text
            card = shorter

        # Nothing discretionary is left: the title itself is what does not fit.
        over = len(text) - limit
        text = replace(card, title=shorten(card.title, max(1, len(card.title) - over))).render(
            message_format, link, template
        )
        # Even the irreducible card can be too long for an absurdly small
        # limit, and this method's promise is the one thing that must not
        # bend: what it returns is what gets sent, and something over the limit
        # is not sent at all.  So the last resort is a cut -- repaired, because
        # a cut is exactly what the rest of this avoids.
        return _safe_cut(text, limit, message_format) if len(text) > limit else text

    def _blocks(
        self: "ListingCard",
        esc: Any,
        bold: Any,
        anchor: Any = None,
        newline: str = "\n",
    ) -> str:
        """Title, price, facts, comment, description, link.

        One place, so the formats differ in how a link is written and not in
        which facts appear or in what order.  ``esc`` is applied to every piece
        of text that came from the marketplace or the AI -- which is all of it
        except the emoji and the separators this method writes itself.
        """
        # No status badge on the title: the batch these cards were sent in is
        # already headed "1 publicación más barata", and repeating the word on
        # every card is a line of noise above the price.  `status` is kept as
        # data for the channels that show it as a tag of its own -- the email
        # template does.
        blocks: List[str] = [bold(esc(self.title))]

        price = self.price_line()
        if price:
            line = f"💰 {esc(price)}"
            change = self.change_line()
            if change and self.change is not None:
                line += newline + f"{self.change.icon} {esc(change)}"
            blocks.append(line)

        blocks.append(newline.join(esc(fact) for fact in self.facts()))

        if self.comment:
            blocks.append(f"🤖 {esc(self.comment)}")
        if self.description:
            blocks.append(esc(self.description))
        if anchor is not None:
            blocks.append("🔗 " + anchor(esc(self.link_label()), self.url))

        # A blank line between blocks, which in HTML is two breaks.
        return (newline + newline).join(block for block in blocks if block)

    def _badge(self: "ListingCard") -> str:
        """A leading word only when there is something to lead with."""
        return f"[{self.status}] " if self.status else ""


#: What each notification status is worth saying, if anything.  ``NOT_NOTIFIED``
#: is absent on purpose: every listing in a "new listings" notification is new,
#: and stamping each one with the word costs a line and says nothing.
STATUS_WORDS = {
    "LISTING_DISCOUNTED": "cheaper",
    "LISTING_CHANGED": "updated",
    "EXPIRED": "reminder",
    "TOP_LISTING": "top",
    "LOW_STOCK": "low_stock",
}


def build_card(
    listing: Listing,
    rating: Any = None,
    status: Any = None,
    previous_price: str | None = None,
    notified_at: datetime | None = None,
    language: str | None = None,
    description: str = "",
    marketplace_label: str | None = None,
    description_words: int | None = None,
) -> ListingCard:
    """Turn one listing (and what we know about it) into a card.

    Everything is optional except the listing, because everything except the
    listing is a fact the monitor may not have: an AI that was not configured,
    a listing seen for the first time, a marketplace that printed no location.
    A missing fact drops its line rather than rendering an empty one.
    """
    words = phrases_for(language)
    price, struck = split_price(listing.price)
    # What the user was last told this cost beats what the marketplace has
    # crossed out: one is a fact about them, the other about the seller's
    # pricing, and only the first answers "has it moved since I saw it?".
    was = _clean(previous_price) or struck
    change = price_change(was, price)

    score: int | None = None
    verdict: str | None = None
    comment: str | None = None
    if rating is not None:
        not_evaluated = getattr(type(rating), "NOT_EVALUATED", object())
        if getattr(rating, "comment", None) != not_evaluated:
            score = getattr(rating, "score", None)
            verdict = _clean(getattr(rating, "conclusion", None))
            comment = _clean(getattr(rating, "comment", None))

    key = getattr(status, "name", None)
    word = STATUS_WORDS.get(str(key)) if key else None

    return ListingCard(
        title=_clean(listing.title) or listing.id,
        # The query string is a search session, not part of the address, and it
        # is what makes the same listing look like two.
        url=listing.post_url.split("?")[0],
        marketplace=marketplace_label or listing.marketplace,
        price=price,
        previous_price=was,
        change=change,
        location=_clean(listing.location),
        condition=_clean(listing.condition),
        rating=score,
        verdict=verdict,
        comment=comment,
        status=words.get(word) if word else None,
        image=_clean(listing.image),
        notified_at=notified_at,
        language=language,
        # Neither is on the built-in card: the search's name is something the
        # reader already knows and the seller is a line that almost never helps
        # on a phone.  A template the user wrote is allowed to want both.
        item=_clean(getattr(listing, "name", None)),
        seller=_clean(listing.seller),
        stock=_clean(getattr(listing, "stock", None)),
        availability=_clean(getattr(listing, "availability", None)),
        # Only the notification's copy is shortened.  What the scraper stored
        # is the seller's text and stays the seller's text -- the dashboard,
        # the export and the AI all keep reading the whole of it.
        description=truncate_description(description, description_words),
    )


def summary_title(
    cards: List[ListingCard],
    status_key: str,
    marketplace: str,
    language: str | None = None,
) -> str:
    """The one line above a batch: how many, of what kind, from where.

    Short on purpose.  It is the part a phone shows in the lock screen, and the
    interesting detail is in the cards under it.
    """
    words = phrases_for(language)
    key = STATUS_WORDS.get(status_key, "new")
    one = len(cards) == 1
    kind = words.get(key if one else f"{key}_plural", words["new"])
    template = words["found_one"] if one else words["found_many"]
    return template.format(count=len(cards), kind=kind, marketplace=marketplace)
