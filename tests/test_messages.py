"""Lo que dice una notificación, y en qué orden lo dice.

A notification is read in about a second, standing up, and it has to answer one
question: is this worth opening?  That answer is the price, what the price used
to be, and where the thing is.  The messages this replaced opened with a
paragraph of AI commentary and buried the number underneath it.

So the facts are resolved once, into a :class:`ListingCard`, and each channel
renders that.  What these tests pin is the part that is easy to get subtly
wrong and impossible to notice from the code:

* a missing fact drops its line instead of rendering an empty one -- half of
  what a marketplace prints is missing half of the time;
* a price is never re-formatted, because a Chilean Facebook listing prints
  "450 000" with no symbol and inventing a "$" for it invents a fact;
* the *difference* between two prices is computed, and carries whatever symbol
  the marketplace itself used;
* MarkdownV2 is a format, not a flavour of markdown: it is built escaped rather
  than escaped afterwards, or Telegram shows the asterisks instead of applying
  them.
"""

from __future__ import annotations

from typing import ClassVar, List, Tuple

import pytest

from ai_marketplace_monitor.listing import Listing
from ai_marketplace_monitor.messages import (
    HTML,
    MARKDOWN,
    MARKDOWN_V2,
    PLAIN,
    build_card,
    escape_markdown_v2,
    price_change,
    split_price,
    summary_title,
    symbol_of,
)
from ai_marketplace_monitor.notification import NotificationStatus, PushNotificationConfig


class Rating:
    """Stands in for an AIResponse, which is more than these need."""

    NOT_EVALUATED = "not evaluated"

    def __init__(self, score=4, conclusion="Muy buena", comment="Bajo el promedio.") -> None:
        self.score, self.conclusion, self.comment = score, conclusion, comment


def listing(**overrides) -> Listing:
    fields = {
        "marketplace": "mercadolibre",
        "name": "ps5",
        "id": "123",
        "title": "PS5 Slim 1TB",
        "image": "https://example.test/photo.jpg",
        "price": "$359.990",
        "post_url": "https://example.test/MLC-123?from=search",
        "location": "Santiago",
        "seller": "juan",
        "condition": "Nuevo",
        "description": "",
    }
    fields.update(overrides)
    return Listing(**fields)


# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #


def test_a_price_drop_is_named_and_measured() -> None:
    change = price_change("$399.990", "$359.990")
    assert change is not None
    assert change.direction == "down"
    assert change.amount == pytest.approx(40_000)
    # 40.000 of 399.990 is not exactly a tenth, and the message rounds it to
    # one anyway -- what matters is that it is a percentage of the *old* price.
    assert change.percent == pytest.approx(10, abs=0.01)


def test_a_price_that_did_not_move_is_not_a_change() -> None:
    """Otherwise every re-notification would announce a drop of zero."""
    assert price_change("$359.990", "$359.990") is None


def test_no_previous_price_is_not_a_guess() -> None:
    assert price_change(None, "$359.990") is None
    assert price_change("", "$359.990") is None


def test_an_unreadable_price_produces_no_arithmetic() -> None:
    assert price_change("Consultar", "$359.990") is None


def test_the_difference_carries_the_marketplace_s_own_symbol() -> None:
    card = build_card(listing(), previous_price="$399.990", language="es")
    assert card.change_line() == "Bajó $40.000 (-10%)"


def test_a_marketplace_that_printed_no_symbol_gets_none_invented() -> None:
    """Facebook renders Chilean prices as a bare "450 000".

    A "$" added here would be this program asserting a currency the page never
    named, which is the same mistake as formatting the price itself.
    """
    card = build_card(
        listing(price="450 000", marketplace="facebook"),
        previous_price="500 000",
        language="es",
    )
    assert card.change_line() == "Bajó 50.000 (-10%)"


def test_symbol_of_reads_what_is_in_front_of_the_number() -> None:
    assert symbol_of("$359.990") == "$"
    assert symbol_of("450 000") == ""
    assert symbol_of("US$ 1.200") == "US$"
    # The discounted pair: the current price is the first half.
    assert symbol_of("$180.000 | $200.000") == "$"


# --------------------------------------------------------------------------- #
# The card
# --------------------------------------------------------------------------- #


def test_the_price_leads_and_the_history_is_beside_it() -> None:
    card = build_card(listing(), Rating(), previous_price="$399.990", language="es")
    text = card.render(PLAIN)
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines[0] == "PS5 Slim 1TB"
    assert lines[1] == "💰 $399.990 → $359.990"
    assert lines[2].startswith("📉 Bajó")


def test_a_missing_fact_drops_its_line() -> None:
    bare = listing(
        location="**unspecified**", condition="", image="", price="", description=""
    )
    text = build_card(bare, language="es").render(PLAIN)
    assert "📍" not in text
    assert "🏷️" not in text
    assert "💰" not in text
    # What is left is still a usable notification.
    assert "PS5 Slim 1TB" in text
    assert "🔗" in text


def test_a_listing_with_no_ai_says_nothing_about_ai() -> None:
    """A monitor with no AI configured would otherwise print "not evaluated"
    once per listing, forever, which is a line that says nothing."""
    text = build_card(listing(), language="es").render(PLAIN)
    assert "🤖" not in text
    assert "⭐" not in text


def test_an_unevaluated_rating_is_the_same_as_no_rating() -> None:
    text = build_card(
        listing(), Rating(comment=Rating.NOT_EVALUATED), language="es"
    ).render(PLAIN)
    assert "⭐" not in text


def test_the_search_session_is_stripped_from_the_link() -> None:
    """The query string makes the same listing look like two."""
    assert build_card(listing()).url == "https://example.test/MLC-123"


def test_the_platform_is_named_the_way_a_person_would() -> None:
    card = build_card(listing(), marketplace_label="Mercado Libre")
    assert "🛒 Mercado Libre" in card.render(PLAIN)


# --------------------------------------------------------------------------- #
# Formats
# --------------------------------------------------------------------------- #


def test_markdown_makes_the_title_bold_and_the_link_a_link() -> None:
    text = build_card(listing(), language="es").render(MARKDOWN)
    assert "**PS5 Slim 1TB**" in text
    assert "[Ver publicación](https://example.test/MLC-123)" in text


def test_html_escapes_the_text_and_not_its_own_markup() -> None:
    card = build_card(listing(title="PS5 <slim> & co"), language="es")
    text = card.render(HTML)
    assert "PS5 &lt;slim&gt; &amp; co" in text
    assert '<a href="https://example.test/MLC-123">' in text


def test_markdown_v2_escapes_the_price_but_keeps_the_emphasis() -> None:
    """The dots in "$359.990" are punctuation Telegram rejects unescaped.

    Escaping the rendered markdown instead would backslash the asterisks too and
    deliver them literally, which is why this is a format of its own.
    """
    text = build_card(listing(), previous_price="$399.990", language="es").render(
        MARKDOWN_V2
    )
    assert "*PS5 Slim 1TB*" in text
    assert "$359\\.990" in text
    assert "\\(\\-10%\\)" in text


def test_markdown_v2_escapes_every_character_telegram_minds() -> None:
    assert escape_markdown_v2("a.b-c(d)") == "a\\.b\\-c\\(d\\)"


def test_a_caption_can_leave_the_link_to_the_button() -> None:
    """Telegram puts the URL on a button under the photo; the same address in
    the caption as well is one of them wasted."""
    card = build_card(listing(), language="es")
    assert "🔗" not in card.render(MARKDOWN_V2, link=False)
    assert card.link_label() == "Ver publicación"


# --------------------------------------------------------------------------- #
# The heading over a batch
# --------------------------------------------------------------------------- #


def test_the_heading_counts_and_names_what_kind() -> None:
    card = build_card(listing())
    assert summary_title([card], "NOT_NOTIFIED", "Mercado Libre", "es") == (
        "1 publicación nueva en Mercado Libre"
    )
    assert summary_title([card, card], "LISTING_DISCOUNTED", "Mercado Libre", "es") == (
        "2 publicaciones más baratas en Mercado Libre"
    )


def test_an_unknown_language_falls_back_to_english_not_to_nothing() -> None:
    card = build_card(listing())
    assert summary_title([card], "NOT_NOTIFIED", "Mercado Libre", "de") == (
        "1 new listing on Mercado Libre"
    )


def test_a_regional_tag_is_read_as_its_language() -> None:
    """Configurations say "es_LA", "es-CL", "es" -- all of them Spanish."""
    for tag in ("es", "es_LA", "es-CL", "ES"):
        card = build_card(listing(), previous_price="$399.990", language=tag)
        assert card.change_line().startswith("Bajó"), tag


# --------------------------------------------------------------------------- #
# From cards to a channel
# --------------------------------------------------------------------------- #
#
# `PushNotificationConfig.notify` is the piece that binds the two: it groups the
# listings by what happened to them, renders each card in the format the channel
# asked for, and hands the batch over.  A channel that can do more than text --
# Telegram, which sends each card as its listing's photo -- overrides only the
# last of those steps, which is the whole reason it is a step.


class Collector(PushNotificationConfig):
    """A channel that keeps what it was told instead of sending it."""

    required_fields: ClassVar[List[str]] = []

    def __post_init__(self) -> None:
        super().__post_init__()
        self.sent: List[Tuple[str, str]] = []

    def send_message(self, title: str, message: str, logger=None) -> bool:
        self.sent.append((title, message))
        return True


def test_each_kind_of_news_is_its_own_message() -> None:
    """Three new listings and one that got cheaper are two pieces of news.

    One message headed "4 publicaciones" would have to pick one of the two words
    and be wrong about the other.
    """
    listings = [listing(id="1"), listing(id="2")]
    statuses = [NotificationStatus.NOT_NOTIFIED, NotificationStatus.LISTING_DISCOUNTED]
    channel = Collector(name="me", message_format=PLAIN, with_description=0)

    assert channel.notify(listings, [Rating(), Rating()], statuses, language="es") is True

    titles = sorted(title for title, _message in channel.sent)
    assert titles == [
        "1 publicación más barata en mercadolibre",
        "1 publicación nueva en mercadolibre",
    ]


def test_a_listing_already_notified_is_left_out() -> None:
    channel = Collector(name="me", message_format=PLAIN, with_description=0)
    assert (
        channel.notify(
            [listing()], [Rating()], [NotificationStatus.NOTIFIED], language="es"
        )
        is False
    )
    assert channel.sent == []


def test_forcing_sends_it_anyway() -> None:
    """"Volver a avisar" has to be able to override the record of having done so."""
    channel = Collector(name="me", message_format=PLAIN, with_description=0)
    assert (
        channel.notify(
            [listing()], [Rating()], [NotificationStatus.NOTIFIED], force=True,
            language="es",
        )
        is True
    )
    assert len(channel.sent) == 1


def test_the_description_is_trimmed_to_what_the_channel_asked_for() -> None:
    long = listing(description="x" * 200)
    channel = Collector(name="me", message_format=PLAIN, with_description=20)
    channel.notify([long], [Rating()], [NotificationStatus.NOT_NOTIFIED], language="es")
    _title, message = channel.sent[0]
    assert "x" * 20 + "..." in message
    assert "x" * 21 not in message


def test_a_discounted_price_is_two_facts_not_one_string() -> None:
    """Mercado Libre stores "$74.990 | $99.990": asking price, then struck through.

    Printed raw next to a previous price it comes out as "$89.990 → $74.990 |
    $99.990" — three numbers and no answer. The two halves go to the two places
    the card already has for them.
    """
    card = build_card(listing(price="$74.990 | $99.990"), language="es")
    assert card.price == "$74.990"
    assert card.previous_price == "$99.990"
    assert card.price_line() == "$99.990 → $74.990"
    assert card.change_line() == "Bajó $25.000 (-25%)"


def test_what_the_user_was_told_beats_what_the_seller_crossed_out() -> None:
    """One is a fact about them, the other about the seller's pricing.

    Only the first answers "has it moved since I last saw it?", which is the
    question a notification about a listing they already know exists is for.
    """
    card = build_card(
        listing(price="$74.990 | $99.990"), previous_price="$89.990", language="es"
    )
    assert card.price_line() == "$89.990 → $74.990"


def test_split_price_leaves_an_ordinary_price_alone() -> None:
    assert split_price("$590.000") == ("$590.000", None)
    assert split_price("450 000") == ("450 000", None)
    assert split_price(None) == (None, None)
    assert split_price("**unspecified**") == (None, None)
