"""Notification text the user writes, instead of the one this program picked.

:mod:`ai_marketplace_monitor.messages` decides what a notification says: a
title, a price, the movement, the facts, the link, in that order, because that
is the order those things get read in.  It is a good default and it is still the
default.  It is not, however, an opinion anyone else has to share -- somebody
watching for a resale margin wants the numbers and nothing else, somebody
watching for a present wants the picture and the description, and neither of
them should have to edit Python to get it.

So a channel may carry a **template**: a string with ``{placeholders}`` in it,
one per kind of notification.  A channel with no template for the kind being
sent renders the built-in card, so this whole module is inert until somebody
asks for it.

Four rules, and the last one is the one that is easy to get wrong.

**Every placeholder is optional.**  Half of what a marketplace prints is missing
half of the time, and a template that says ``Ubicación: {location}`` must not
produce "Ubicación: " for a listing with no location.  A line whose placeholders
all came out empty is dropped whole -- which is exactly what the built-in card
does, and the reason it never has a dangling label on it.  A line with no
placeholders at all is always kept, because that is a separator or a heading and
the user typed it on purpose.

**The user's own text is escaped along with the values.**  Templates are plain
text, not markup.  A Telegram message is MarkdownV2, where an unescaped ``.``
is a syntax error and the message is refused outright; if the template's literal
text went through unescaped, writing "Precio: $399.990" into the editor would
produce a notification that never arrives.  Markup would have to be a separate
feature with its own escaping, and it is not worth breaking every message to get
it.

**Links go through the channel.**  ``{url}`` is the address, escaped for the
format; ``{link}`` is whatever that channel makes a link out of -- an ``<a>``, a
markdown link, or on a channel that has no links at all, the address.  Telegram
puts the link on a native button and asks for the text without one, so a
template that uses neither is not missing anything there.

**An unknown placeholder is an error, not empty text.**  ``{titel}`` is a typo,
and rendering it as nothing means a template that silently drops the title.  The
config loader refuses it and names it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Sequence, Tuple

#: ``{name}`` -- letters, digits and underscores only, so a stray brace in the
#: user's prose ("{sic}") is reported as an unknown placeholder rather than
#: quietly swallowing the rest of the line.
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: Placeholders that are not text but a link, and therefore never escaped as
#: text.  Kept as a set because both of them are resolved by the caller's
#: ``anchor``/``url`` writers rather than read off the card.
LINK_FIELDS = frozenset({"url", "link"})


#: Every placeholder a template may use, in the order the editor lists them.
#:
#: Pairs rather than a class: the second half is shown in the web UI beside the
#: field, which is the only place a user ever finds out what exists, and it is
#: in Spanish for the same reason every other string the UI shows is.
#:
#: The order is not alphabetical: it is the order of a message, so somebody
#: reading the list top to bottom is reading the shape of a notification.
VARIABLES: Tuple[Tuple[str, str], ...] = (
    ("notification_type", "Por qué llega el aviso: “Nueva publicación”, “Bajó de precio”…"),
    ("title", "El título tal como lo escribió quien publica"),
    ("price", "El precio actual, tal como lo imprimió el marketplace"),
    ("new_price", "Lo mismo que {price}, para plantillas de baja de precio"),
    ("old_price", "El precio anterior, cuando se sabe cuál era"),
    ("discount", "Cuánto bajó, con el mismo símbolo que el precio"),
    ("discount_percent", "Cuánto bajó, en porcentaje"),
    ("location", "La ubicación que muestra la publicación"),
    ("condition", "Estado del producto, si la plataforma lo dice"),
    ("stock", "Cuántas unidades dice la tienda que quedan, cuando lo publica"),
    ("availability", "“in_stock” o “out_of_stock”, cuando la plataforma lo dice"),
    ("marketplace", "La plataforma: “Facebook Marketplace”, “Mercado Libre”…"),
    (
        "item",
        "El nombre de la búsqueda que la encontró; en un seguimiento, "
        "el grupo al que pertenece, o su propio nombre si no está en ninguno",
    ),
    ("seller", "Quien vende, si la plataforma lo muestra"),
    ("rating", "El puntaje de la IA, de 1 a 5"),
    ("verdict", "El veredicto de la IA en una palabra"),
    ("comment", "El comentario de la IA"),
    ("description", "La descripción del vendedor, recortada como digan los ajustes"),
    ("url", "La dirección de la publicación"),
    ("link", "Un enlace con texto (“Ver publicación”), o la dirección donde no haya enlaces"),
    ("image", "La dirección de la imagen"),
)

#: Just the names, for validating.
VARIABLE_NAMES = frozenset(name for name, _description in VARIABLES)


class TemplateError(ValueError):
    """A template the renderer cannot make sense of."""


def placeholders(template: str) -> List[str]:
    """Every placeholder the template uses, in order, duplicates included."""
    return _PLACEHOLDER_RE.findall(template or "")


def unknown_placeholders(template: str) -> List[str]:
    """The placeholders that are not real, deduplicated, in order of appearance.

    Deduplicated because a template that uses ``{titel}`` three times has one
    mistake in it, and being told about it three times is being told about it
    once, badly.
    """
    seen: List[str] = []
    for name in placeholders(template):
        if name not in VARIABLE_NAMES and name not in seen:
            seen.append(name)
    return seen


def validate(template: str | None, label: str = "template") -> List[str]:
    """What is wrong with a template, or an empty list.

    An empty template is not an error: it is how a channel says "use the
    built-in card", which has to stay expressible or a template could not be
    undone once written.
    """
    if template is None or not template.strip():
        return []
    if not isinstance(template, str):
        return [f"{label} must be a string."]
    bad = unknown_placeholders(template)
    if bad:
        known = ", ".join(sorted(VARIABLE_NAMES))
        return [
            f"{label} uses {', '.join('{' + name + '}' for name in bad)}, which "
            f"{'is not a variable' if len(bad) == 1 else 'are not variables'}. "
            f"Available: {known}."
        ]
    return []


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


#: Separators a template puts *between* two facts.  When one of the facts turns
#: out to be missing, the separator is left with nothing on one side of it.
#:
#: No "/" in here, deliberately: a line that is just ``{url}`` ends in one, and
#: trimming it turns a working address into a redirect at best.
_SEPARATORS = " ·-–—:,|"


def _tidy(line: str) -> str:
    """Close the gaps a missing fact leaves behind on a line that survived.

    "``{price} · {location}``" for a listing with no location renders as
    "$100 · " and then "$100"; "``{location} · {price}``" renders as "· $100"
    and then "$100"; two facts missing out of three leave a double space, which
    becomes one.

    Only whitespace and separator punctuation.  A template written as
    "``{price} en {location}``" still ends up as "$100 en", and that is left
    alone deliberately: "en" is a word, and a rule that guessed at words would
    eventually eat one the user meant.  The fix for that template is to write
    it with a separator, which is what the editor's examples do.
    """
    collapsed = re.sub(r"[ \t]{2,}", " ", line)
    return collapsed.strip().strip(_SEPARATORS).strip()


def render(
    template: str,
    values: Dict[str, Any],
    esc: Callable[[str], str] | None = None,
    write_link: Callable[[str], str] | None = None,
    newline: str = "\n",
) -> str:
    """The template with its placeholders filled in, empty lines dropped.

    ``esc`` is applied to every piece of text -- the user's own words as much as
    the values, see the module docstring.  ``write_link`` turns the ``{link}``
    placeholder into whatever the channel makes a link out of; without one, the
    address is used.

    Substitution happens line by line rather than over the whole string, because
    the "drop what came out empty" rule is a rule about lines: a template is
    written as a shape, and a shape with a hole in it is not the same shape.
    """
    escape = esc or (lambda text: text)
    lines: List[str] = []

    for line in (template or "").splitlines():
        names = placeholders(line)
        if not names:
            # A separator, a heading, an emoji on its own: the user typed it and
            # it does not depend on anything, so it stays.
            lines.append(escape(line))
            continue

        # A line is dropped when *every* placeholder on it came out empty.  Not
        # "any": "{price} en {location}" with a price and no location should
        # still show the price, and it is the label-with-nothing-after-it case
        # -- one placeholder, one line -- that this exists for.
        if all(_empty(values.get(name)) for name in names):
            continue

        # Walked in pieces rather than substituted with `re.sub`, because the
        # literal halves have to be escaped too and a substitution only ever
        # sees the holes.  Leaving them raw is not a cosmetic bug: a template
        # reading "Precio: {price}." puts an unescaped "." into a MarkdownV2
        # message, which Telegram refuses outright -- nothing arrives.
        pieces: List[str] = []
        cursor = 0
        for match in _PLACEHOLDER_RE.finditer(line):
            pieces.append(escape(line[cursor : match.start()]))
            cursor = match.end()
            name = match.group(1)
            value = values.get(name)
            if _empty(value):
                continue
            text = str(value)
            # `{link}` is the only placeholder the channel writes rather than
            # escapes: it is an anchor, not a piece of text.  `{url}` is the
            # bare address and *is* text, which in MarkdownV2 means its dots
            # and hyphens need backslashes like everything else -- Telegram
            # still auto-links it, and refuses the message without them.
            pieces.append(
                write_link(text) if name == "link" and write_link is not None else escape(text)
            )
        pieces.append(escape(line[cursor:]))
        lines.append(_tidy("".join(pieces)))

    # Blank lines the user typed are kept between blocks, but a run of them left
    # behind by dropped lines is not: three missing facts in a row should not
    # open a hole in the middle of the message.
    tidied: List[str] = []
    for line in lines:
        if not line.strip() and (not tidied or not tidied[-1].strip()):
            continue
        tidied.append(line)
    while tidied and not tidied[-1].strip():
        tidied.pop()

    return newline.join(tidied)


def template_for(config: Any, status_name: str | None) -> str | None:
    """The template this channel uses for this kind of notification, if any.

    Two lookups, most specific first: the template for this kind, then the
    catch-all.  That is what lets somebody write one template for everything and
    override it for the one kind that needs different words.
    """
    key = TEMPLATE_KEYS.get(str(status_name or ""), None)
    for field in ([key] if key else []) + [DEFAULT_TEMPLATE_KEY]:
        value = getattr(config, field, None)
        if isinstance(value, str) and value.strip():
            return value
    return None


#: ``[monitor]``-style key names, one per kind of notification, plus a
#: catch-all.  Keyed by the
#: :class:`~ai_marketplace_monitor.notification.NotificationStatus` name so
#: adding a kind of notification is adding a row here and nothing else.
TEMPLATE_KEYS: Dict[str, str] = {
    "NOT_NOTIFIED": "template_new",
    "LISTING_DISCOUNTED": "template_price_drop",
    "TOP_LISTING": "template_top",
    "LOW_STOCK": "template_low_stock",
    "LISTING_CHANGED": "template_updated",
    "EXPIRED": "template_reminder",
}

#: Used for any kind that has no template of its own.
DEFAULT_TEMPLATE_KEY = "template"

#: Every key a channel may carry, for the loader and the web UI.
ALL_TEMPLATE_KEYS: Tuple[str, ...] = (DEFAULT_TEMPLATE_KEY, *sorted(TEMPLATE_KEYS.values()))


def validate_all(config: Any) -> List[str]:
    """Every problem with every template on one channel."""
    problems: List[str] = []
    for key in ALL_TEMPLATE_KEYS:
        problems.extend(validate(getattr(config, key, None), label=key))
    return problems


def example_values() -> Dict[str, Any]:
    """A plausible listing, for previewing a template in the editor.

    Chilean numbers with no currency symbol, because that is what Facebook
    prints here and it is the case a preview built from dollars would hide.
    """
    return {
        "notification_type": "Bajó de precio",
        "title": "PlayStation 5 Slim con dos controles",
        "price": "359.990",
        "new_price": "359.990",
        "old_price": "399.990",
        "discount": "40.000",
        "discount_percent": "-10%",
        "location": "Ñuñoa, Región Metropolitana",
        "condition": "Usado - Como nuevo",
        "stock": "2",
        "availability": "in_stock",
        "marketplace": "Facebook Marketplace",
        "item": "ps5",
        "seller": "Camila",
        "rating": "4",
        "verdict": "Good match",
        "comment": "Precio bajo el promedio del mercado.",
        "description": "Vendo PS5 poco uso, con caja y boleta.",
        "url": "https://www.facebook.com/marketplace/item/123456789",
        "link": "https://www.facebook.com/marketplace/item/123456789",
        "image": "https://example.com/ps5.jpg",
    }


def preview(template: str, values: Sequence[Tuple[str, Any]] | None = None) -> str:
    """A template rendered against a plausible listing, as plain text."""
    sample = example_values()
    if values:
        sample.update(dict(values))
    return render(template, sample)
