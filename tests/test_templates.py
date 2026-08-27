"""Notification text the user writes, and the rules that keep it sendable.

Two halves.  The first is the template engine on its own -- substitution, the
empty-line rule, unknown placeholders -- which is pure and needs nothing.  The
second is a real :class:`ListingCard` rendered through a template in each of the
four formats, because the promise that matters is not "it substitutes" but "it
cannot produce a message the channel refuses", and only a real render can show
that.
"""

from __future__ import annotations

import pytest

from ai_marketplace_monitor import templates
from ai_marketplace_monitor.listing import Listing
from ai_marketplace_monitor.messages import (
    HTML,
    MARKDOWN_V2,
    PLAIN,
    build_card,
)
from ai_marketplace_monitor.notification import NotificationStatus


# --------------------------------------------------------------------------- #
# Substitution
# --------------------------------------------------------------------------- #


def test_a_placeholder_is_replaced() -> None:
    assert templates.render("Título: {title}", {"title": "PS5"}) == "Título: PS5"


def test_several_on_one_line() -> None:
    out = templates.render("{price} en {location}", {"price": "$100", "location": "Ñuñoa"})
    assert out == "$100 en Ñuñoa"


def test_a_line_with_no_placeholders_is_kept() -> None:
    # A separator or a heading: the user typed it and it depends on nothing.
    assert templates.render("——————\n{title}", {"title": "PS5"}) == "——————\nPS5"


def test_a_line_whose_only_fact_is_missing_is_dropped() -> None:
    # The whole point: "Ubicación: " with nothing after it is worse than no
    # line at all, and half of what a marketplace prints is missing half of
    # the time.
    out = templates.render("Título: {title}\nUbicación: {location}", {"title": "PS5"})
    assert out == "Título: PS5"


def test_a_line_keeps_going_when_only_some_facts_are_missing() -> None:
    # "any missing" would be the wrong rule: the price is still worth printing.
    out = templates.render("{price} · {location}", {"price": "$100", "location": ""})
    assert out == "$100"


def test_the_separator_a_missing_fact_leaves_behind_is_closed_up() -> None:
    values = {"price": "$100"}
    # Whichever side the hole is on.
    assert templates.render("{location} · {price}", values) == "$100"
    assert templates.render("{price} · {location}", values) == "$100"
    # And two holes out of three do not leave a double space.
    assert templates.render("{seller} {price} {location}", values) == "$100"


def test_a_word_between_two_facts_is_left_alone() -> None:
    # A rule that guessed at words would eventually eat one the user meant.
    # The fix for this template is a separator, which is what the examples use.
    out = templates.render("{price} en {location}", {"price": "$100", "location": ""})
    assert out == "$100 en"


def test_whitespace_only_values_count_as_missing() -> None:
    assert templates.render("Vendedor: {seller}", {"seller": "   "}) == ""


def test_runs_of_blank_lines_left_by_dropped_lines_collapse() -> None:
    # Three missing facts in a row must not open a hole in the message.
    out = templates.render(
        "{title}\n\n{location}\n\n{seller}\n\n{price}",
        {"title": "PS5", "price": "$100"},
    )
    assert out == "PS5\n\n$100"


def test_a_deliberate_blank_line_survives() -> None:
    out = templates.render("{title}\n\n{price}", {"title": "PS5", "price": "$100"})
    assert out == "PS5\n\n$100"


def test_trailing_blank_lines_are_trimmed() -> None:
    assert templates.render("{title}\n\n", {"title": "PS5"}) == "PS5"


def test_an_empty_template_renders_to_nothing() -> None:
    assert templates.render("", {"title": "PS5"}) == ""


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def test_a_good_template_has_no_problems() -> None:
    assert templates.validate("{title} — {price}\n{url}") == []


def test_an_empty_template_is_not_an_error() -> None:
    # It is how a channel says "use the built-in card", which has to stay
    # expressible or a template could not be undone once written.
    assert templates.validate("") == []
    assert templates.validate(None) == []
    assert templates.validate("   ") == []


def test_a_typo_is_refused_and_named() -> None:
    problems = templates.validate("{titel}")
    assert len(problems) == 1
    assert "{titel}" in problems[0]


def test_the_message_lists_what_is_available() -> None:
    assert "notification_type" in templates.validate("{nope}")[0]


def test_one_typo_used_three_times_is_one_problem() -> None:
    assert templates.unknown_placeholders("{titel} {titel}\n{titel}") == ["titel"]


def test_several_typos_are_all_named() -> None:
    assert templates.unknown_placeholders("{titel} {pryce}") == ["titel", "pryce"]


def test_every_documented_variable_is_accepted() -> None:
    # The editor lists these; a name in the list that the validator rejects
    # would be documentation promising something that does not work.
    for name, _description in templates.VARIABLES:
        assert templates.validate("{" + name + "}") == []


def test_every_variable_has_something_to_say_about_it() -> None:
    for name, description in templates.VARIABLES:
        assert description.strip(), name


# --------------------------------------------------------------------------- #
# Picking one
# --------------------------------------------------------------------------- #


class Channel:
    """Just the template fields, as a channel config carries them."""

    def __init__(self, **kwargs: str) -> None:
        for key in templates.ALL_TEMPLATE_KEYS:
            setattr(self, key, kwargs.get(key))


def test_the_kinds_template_wins() -> None:
    channel = Channel(template="general", template_price_drop="cheaper")
    assert templates.template_for(channel, "LISTING_DISCOUNTED") == "cheaper"


def test_the_catch_all_covers_a_kind_with_no_template() -> None:
    channel = Channel(template="general")
    assert templates.template_for(channel, "NOT_NOTIFIED") == "general"


def test_no_template_at_all_means_the_built_in_card() -> None:
    assert templates.template_for(Channel(), "NOT_NOTIFIED") is None


def test_a_blank_template_does_not_count_as_one() -> None:
    channel = Channel(template="general", template_new="   ")
    assert templates.template_for(channel, "NOT_NOTIFIED") == "general"


def test_an_unknown_status_falls_back_to_the_catch_all() -> None:
    assert templates.template_for(Channel(template="general"), "SOMETHING") == "general"


def test_every_notification_status_has_a_key() -> None:
    # A status with no key would silently use the catch-all forever, which is
    # a template the user cannot write.
    for status in NotificationStatus:
        if status is NotificationStatus.NOTIFIED:
            # Never sent: it is the "they already know" verdict.
            continue
        assert status.name in templates.TEMPLATE_KEYS


# --------------------------------------------------------------------------- #
# Rendered through a real card
# --------------------------------------------------------------------------- #


def _listing(price: str = "359.990") -> Listing:
    return Listing(
        marketplace="facebook",
        name="ps5",
        id="123",
        title="PlayStation 5 Slim",
        image="https://example.com/ps5.jpg",
        price=price,
        post_url="https://www.facebook.com/marketplace/item/123/?ref=search",
        location="Ñuñoa",
        seller="Camila",
        condition="used_good",
        description="Poco uso, con caja.",
    )


def _card(**kwargs):
    return build_card(_listing(), **kwargs)


def test_a_template_replaces_the_built_in_shape() -> None:
    card = _card()
    out = card.render(PLAIN, template="{title}\n{price}")
    assert out == "PlayStation 5 Slim\n359.990"
    # And the built-in card is still there when no template is given.
    assert "🛒" in card.render(PLAIN)


def test_the_price_movement_is_available_to_a_template() -> None:
    card = build_card(_listing("359.990"), previous_price="399.990")
    out = card.render(PLAIN, template="{old_price} → {new_price} ({discount_percent})")
    assert out == "399.990 → 359.990 (-10%)"


def test_the_difference_carries_the_marketplaces_own_symbol() -> None:
    # A Chilean Facebook listing prints no symbol at all, and inventing one is
    # inventing a fact about the currency.
    card = build_card(_listing("359.990"), previous_price="399.990", language="es")
    assert card.render(PLAIN, template="{discount}") == "40.000"
    card = build_card(_listing("$359.990"), previous_price="$399.990", language="es")
    assert card.render(PLAIN, template="{discount}") == "$40.000"


def test_the_difference_is_grouped_for_the_reader() -> None:
    # Spanish groups thousands with a dot, English with a comma.  This is the
    # one number the monitor formats itself, so it is the one that has to know.
    card = build_card(_listing("359.990"), previous_price="399.990", language="en")
    assert card.render(PLAIN, template="{discount}") == "40,000"


def test_a_listing_with_no_previous_price_drops_those_lines() -> None:
    out = _card().render(PLAIN, template="{title}\nAntes: {old_price}\nBajó: {discount}")
    assert out == "PlayStation 5 Slim"


def test_the_search_name_and_the_seller_are_reachable() -> None:
    out = _card().render(PLAIN, template="{item} · {seller}")
    assert out == "ps5 · Camila"


def test_the_notification_type_is_the_word_for_the_status() -> None:
    card = build_card(
        _listing(), status=NotificationStatus.LISTING_DISCOUNTED, language="es"
    )
    assert card.render(PLAIN, template="{notification_type}") == "más barata"


# --- escaping, which is what this indirection is for ------------------------


def test_the_users_own_text_is_escaped_for_telegram() -> None:
    # An unescaped "." in MarkdownV2 is not a cosmetic problem: Telegram
    # refuses the message and nothing arrives.
    out = _card().render(MARKDOWN_V2, template="Precio: {price}.")
    assert "\\." in out
    assert "359\\.990" in out


def test_a_value_full_of_punctuation_is_escaped_too() -> None:
    card = build_card(_listing("$359.990"), previous_price="$399.990", language="es")
    out = card.render(MARKDOWN_V2, template="{discount} ({discount_percent})")
    # Brackets, hyphen, percent and dot all need backslashing.
    assert "\\(" in out and "\\)" in out and "\\-" in out


def test_html_escapes_what_the_user_typed() -> None:
    out = _card().render(HTML, template="<b>{title}</b>")
    assert "&lt;b&gt;" in out
    assert "<b>" not in out


def test_a_title_with_html_in_it_is_escaped() -> None:
    listing = _listing()
    listing.title = "PS5 <script>alert(1)</script>"
    out = build_card(listing).render(HTML, template="{title}")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_the_url_placeholder_is_the_address() -> None:
    out = _card().render(PLAIN, template="{url}")
    # The query string is a search session, not part of the address.
    assert out == "https://www.facebook.com/marketplace/item/123/"


def test_the_link_placeholder_is_a_link_where_there_are_links() -> None:
    out = _card(language="es").render(HTML, template="{link}")
    assert out.startswith('<a href="https://www.facebook.com/marketplace/item/123/">')
    assert "Ver publicación" in out


def test_the_link_placeholder_degrades_to_the_address() -> None:
    # A channel that puts the link somewhere of its own asks for no anchor;
    # a template that uses {link} then gets the bare address rather than a
    # second copy of a link that is already on a button.
    out = _card().render(HTML, link=False, template="{link}")
    assert out == "https://www.facebook.com/marketplace/item/123/"


# --- length, which the channel still enforces -------------------------------


def test_a_template_is_shortened_to_fit_the_channel() -> None:
    listing = _listing()
    listing.description = "palabra " * 400
    card = build_card(listing, description=listing.description)
    out = card.render_within(200, PLAIN, template="{title}\n{description}")
    assert len(out) <= 200


def test_shortening_a_telegram_template_never_strands_a_backslash() -> None:
    listing = _listing()
    listing.description = "precio. " * 200
    card = build_card(listing, description=listing.description)
    out = card.render_within(180, MARKDOWN_V2, template="{title}\n{description}")
    assert len(out) <= 180
    # An odd number of trailing backslashes means the last one lost the
    # character it was escaping -- which Telegram refuses outright.
    assert (len(out) - len(out.rstrip("\\"))) % 2 == 0


# --------------------------------------------------------------------------- #
# The preview the editor shows
# --------------------------------------------------------------------------- #


def test_the_preview_fills_every_variable() -> None:
    sample = templates.example_values()
    for name, _description in templates.VARIABLES:
        assert name in sample


def test_previewing_a_template() -> None:
    out = templates.preview("{notification_type}\n{title}: {price}")
    assert "Bajó de precio" in out
    assert "359.990" in out


@pytest.mark.parametrize("key", templates.ALL_TEMPLATE_KEYS)
def test_a_channel_refuses_a_bad_template_at_load_time(key: str) -> None:
    from ai_marketplace_monitor.notification import PushNotificationConfig

    with pytest.raises(ValueError, match="titel"):
        PushNotificationConfig(name="x", **{key: "{titel}"})


def test_a_channel_accepts_a_good_template_at_load_time() -> None:
    from ai_marketplace_monitor.notification import PushNotificationConfig

    config = PushNotificationConfig(name="x", template_new="{title} — {price}")
    assert config.template_for(NotificationStatus.NOT_NOTIFIED) == "{title} — {price}"
