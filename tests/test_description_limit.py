"""How much of a seller's description a notification carries, and why.

Two limits that are easy to confuse and are not the same thing.

*How many words* is a choice the user makes: Mercado Libre sellers paste their
whole catalogue, their shipping policy and their opening hours into the
description, and a notification that repeats all of it buries the one number
the reader opened it for.  That is `truncate_description`.

It counts words rather than lines, and that is a correction rather than a
preference.  A line is not a property of the text -- it is a property of the
screen showing it, so the same "five lines" is five short lines on a desktop
and fifteen wrapped ones on a phone; and one unbroken paragraph slips past a
line limit entirely while still filling the screen.  Which is exactly what was
reported: messages that had been limited and were still far too long.

*How many characters the service will take* is not a choice at all.  Telegram
answers "Message is too long" to anything over 4096 and delivers nothing --
which is the error that started this -- and Pushover refuses over 1024.  A word
limit does not settle that either: the AI's commentary and a long title can
exceed the cap between them with no description at all.  That is
`render_within`, and what matters about it is that it *rebuilds* the card
rather than cutting the rendered text, because cut MarkdownV2 is rejected just
as firmly for a different reason.
"""

from __future__ import annotations

import pytest

from ai_marketplace_monitor.listing import Listing
from ai_marketplace_monitor.messages import (
    DEFAULT_DESCRIPTION_WORDS,
    ELLIPSIS,
    MARKDOWN_V2,
    ListingCard,
    build_card,
    shorten,
    truncate_description,
)
from ai_marketplace_monitor.notification import NotificationStatus, PushNotificationConfig
from ai_marketplace_monitor.telegram import (
    CAPTION_LIMIT,
    MESSAGE_LIMIT,
    TelegramNotificationConfig,
)

BLANK_LINES = "uno\n\n\ndos\n\n\ntres"
WRAPPED = "uno dos\ntres cuatro\ncinco seis"


# --------------------------------------------------------------------------- #
# Words
# --------------------------------------------------------------------------- #


def test_a_short_description_is_returned_untouched():
    text = "PS5 Slim con dos controles y caja original"
    assert truncate_description(text, 25) == text


def test_a_description_exactly_at_the_limit_is_not_marked_as_cut():
    text = " ".join(f"w{n}" for n in range(25))
    assert truncate_description(text, 25) == text
    assert ELLIPSIS not in truncate_description(text, 25)


def test_one_word_over_the_limit_is_cut_and_marked():
    text = " ".join(f"w{n}" for n in range(26))
    cut = truncate_description(text, 25)
    assert cut == " ".join(f"w{n}" for n in range(25)) + ELLIPSIS
    assert "w25" not in cut


def test_the_marker_is_three_dots():
    assert ELLIPSIS == "..."
    assert truncate_description("uno dos tres", 2) == "uno dos..."


def test_an_empty_description_stays_empty():
    assert truncate_description("", 25) == ""
    assert truncate_description(None, 25) == ""
    assert truncate_description("   \n  \n ", 25) == ""


def test_one_enormous_paragraph_is_cut_like_anything_else():
    """The case a line limit could not touch, which is the reported one.

    A seller who writes without ever pressing Enter had a description of one
    line, so "five lines" kept every word of it -- and the message was a wall
    of text that had supposedly been limited.
    """
    text = "palabra " * 400
    cut = truncate_description(text, 25)
    assert len(cut.split()) == 25
    assert cut.endswith(ELLIPSIS)


def test_line_breaks_inside_what_is_kept_are_left_alone():
    """Only the tail is dropped; the seller's paragraphs are not reflowed."""
    assert truncate_description(WRAPPED, 4) == "uno dos\ntres cuatro..."


def test_blank_lines_do_not_spend_the_allowance():
    """Whitespace is not a word, however much of it there is."""
    assert truncate_description(BLANK_LINES, 3) == BLANK_LINES


def test_the_limit_can_be_switched_off():
    text = " ".join(str(n) for n in range(200))
    assert truncate_description(text, None) == text
    assert truncate_description(text, 0) == text
    assert truncate_description(text, -1) == text


@pytest.mark.parametrize("limit", [1, 2, 3, 8, 25, 60])
def test_whatever_the_user_configured_is_what_is_kept(limit):
    text = " ".join(f"w{n}" for n in range(200))
    assert len(truncate_description(text, limit).split()) == limit


def test_the_default_is_twenty_five_words():
    assert DEFAULT_DESCRIPTION_WORDS == 25


def test_only_the_notification_copy_is_shortened(listing_with_long_description):
    """The stored listing keeps the seller's whole text; the card does not."""
    card = build_card(
        listing_with_long_description,
        description=listing_with_long_description.description,
        description_words=3,
    )
    assert len(card.description.split()) == 3
    assert len(listing_with_long_description.description.split()) > 3


# --------------------------------------------------------------------------- #
# Characters the service will take
# --------------------------------------------------------------------------- #


def _big_card(description_chars: int = 20_000) -> ListingCard:
    return ListingCard(
        title="PlayStation 5 Slim con dos controles",
        url="https://www.mercadolibre.cl/p/MLC123456",
        marketplace="Mercado Libre",
        price="$399.990",
        location="Santiago",
        rating=5,
        verdict="Muy buena",
        comment="Precio bajo el promedio del mercado para este modelo.",
        description="palabra " * (description_chars // 8),
    )


def test_a_message_under_the_limit_is_left_alone():
    card = _big_card(50)
    assert card.render_within(MESSAGE_LIMIT, MARKDOWN_V2) == card.render(MARKDOWN_V2)


def test_a_message_that_would_be_refused_is_made_to_fit():
    card = _big_card()
    assert len(card.render(MARKDOWN_V2)) > MESSAGE_LIMIT
    assert len(card.render_within(MESSAGE_LIMIT, MARKDOWN_V2)) <= MESSAGE_LIMIT


def test_fitting_keeps_the_facts_the_message_is_for():
    """Shortened, not cut: the price, the platform and the link survive."""
    fitted = _big_card().render_within(MESSAGE_LIMIT, MARKDOWN_V2)
    assert "399" in fitted
    assert "Mercado Libre" in fitted
    assert "MLC123456" in fitted


def test_fitting_never_strands_a_markdown_v2_escape():
    """The reason the card is rebuilt rather than the text cut.

    MarkdownV2 wants a backslash before every one of a dozen punctuation
    characters.  Slicing the rendered string can leave the backslash at the end
    with nothing after it, which Telegram rejects -- the same "message refused"
    outcome, arrived at a different way.
    """
    for limit in (60, 200, 1000, MESSAGE_LIMIT):
        fitted = _big_card().render_within(limit, MARKDOWN_V2)
        assert len(fitted) <= limit
        # A trailing backslash means an escape lost the character it escaped.
        assert not fitted.endswith("\\")
        # And no even-length run of backslashes was broken in the middle.
        assert "\\\\\\" not in fitted


def test_a_card_with_nothing_left_to_drop_still_fits():
    """A title longer than the whole allowance is the last thing to give up."""
    card = ListingCard(title="x" * 5000, url="https://example.com/1", marketplace="ML")
    assert len(card.render_within(200, MARKDOWN_V2)) <= 200


def test_telegram_declares_the_limit_it_actually_has():
    assert TelegramNotificationConfig.message_limit == MESSAGE_LIMIT == 4096
    assert CAPTION_LIMIT == 1024


def test_shorten_prefers_a_word_boundary():
    assert shorten("palabra otra tercera", 14) == "palabra" + ELLIPSIS
    # ... but not when backing up would throw the message away.
    assert shorten("a" * 100, 10) == "a" * 7 + ELLIPSIS


# --------------------------------------------------------------------------- #
# Where the limit comes from
# --------------------------------------------------------------------------- #


def test_the_monitor_s_setting_reaches_the_channel(listing_with_long_description):
    channel = PushNotificationConfig(name="c")
    assert len(channel._description_for(listing_with_long_description, 4).split()) == 4


def test_a_channel_of_its_own_overrides_the_monitor(listing_with_long_description):
    channel = PushNotificationConfig(name="c", max_description_words=2)
    assert len(channel._description_for(listing_with_long_description, 10).split()) == 2


def test_with_description_still_means_a_character_count(listing_with_long_description):
    """The older setting is untouched: characters, and its own "..." marker."""
    channel = PushNotificationConfig(name="c", with_description=40)
    text = channel._description_for(listing_with_long_description, None)
    assert len(text) == 43
    assert text.endswith("...")


def test_with_description_and_the_word_limit_are_both_honoured(
    listing_with_long_description,
):
    """Characters first, then words, and whichever bites harder is what shows."""
    channel = PushNotificationConfig(name="c", with_description=40)
    text = channel._description_for(listing_with_long_description, 2)
    assert len(text.split()) == 2
    assert text.endswith(ELLIPSIS)
    assert len(text) < 43


def test_no_limit_anywhere_carries_the_whole_description(listing_with_long_description):
    channel = PushNotificationConfig(name="c")
    assert channel._description_for(
        listing_with_long_description, None
    ) == listing_with_long_description.description


# --------------------------------------------------------------------------- #
# A whole batch
# --------------------------------------------------------------------------- #


class RecordingChannel(PushNotificationConfig):
    """Counts the messages a batch turned into and how long each one was."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.sent: list[tuple[str, str]] = []

    def _has_required_fields(self) -> bool:
        return True

    def send_message(self, title, message, logger=None) -> bool:
        self.sent.append((title, message))
        return True


def test_a_batch_too_big_for_one_message_becomes_several(
    listing_with_long_description, monkeypatch
):
    """More messages, not fewer listings.

    A batch of six that arrives as four is a notification that quietly lied
    about what was found, which is worse than two notifications.
    """
    channel = RecordingChannel(name="c", message_format="plain_text")
    monkeypatch.setattr(RecordingChannel, "message_limit", 400)
    cards = [
        build_card(listing_with_long_description, description="x" * 300) for _ in range(4)
    ]
    items = [(listing_with_long_description, card) for card in cards]
    assert channel.send_items("4 publicaciones", items) is True
    assert len(channel.sent) > 1
    # Title, blank line and body all go into the one thing the service counts.
    for title, message in channel.sent:
        assert len(title) + 2 + len(message) <= 400


def test_a_channel_with_no_limit_still_sends_one_message(listing_with_long_description):
    channel = RecordingChannel(name="c", message_format="plain_text")
    cards = [build_card(listing_with_long_description) for _ in range(4)]
    channel.send_items("4", [(listing_with_long_description, card) for card in cards])
    assert len(channel.sent) == 1


def test_notify_passes_the_limit_down_to_the_cards(listing_with_long_description):
    channel = RecordingChannel(name="c", message_format="plain_text")
    channel.notify(
        [listing_with_long_description],
        [None],
        [NotificationStatus.NOT_NOTIFIED],
        description_words=2,
    )
    assert channel.sent
    _title, message = channel.sent[0]
    assert "palabra5" not in message
    assert ELLIPSIS in message


@pytest.fixture
def listing_with_long_description() -> Listing:
    return Listing(
        marketplace="mercadolibre",
        name="ps5",
        id="MLC1",
        title="PS5",
        image="",
        price="$399.990",
        post_url="https://www.mercadolibre.cl/p/MLC1",
        location="Santiago",
        seller="alguien",
        condition="Usado",
        description=" ".join(f"palabra{n}" for n in range(60)),
    )
