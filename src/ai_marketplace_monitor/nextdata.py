"""Reading a Next.js page's own data instead of its rendered HTML.

Both retailers this package added -- Lider and Sodimac -- are Next.js
applications, and both ship the *entire* payload the page was rendered from
inside a single ``<script id="__NEXT_DATA__">`` tag: the product list, the
prices, the stock, the seller, the pagination.  Scraping the rendered HTML of
either would mean guessing at class names that are generated at build time and
change with every deployment; reading the payload means reading the same object
the site's own JavaScript reads.

That is not a shortcut, it is the more honest source.  A price in the DOM has
already been through a formatter, a badge and a stylesheet; a price in the
payload is what the shop's own API said it was.  A layout change breaks a
selector and does not break this.

The trade is that the payload's *shape* can change instead, which fails loudly
rather than quietly: a missing key raises here, at the top of the parse, instead
of producing a listing with an empty title.  Everything below therefore returns
``None`` rather than a half-built object, and the caller treats that the same
way it treats a page that did not load.

Nothing here knows about either shop.  It finds the tag, parses it, and walks a
dotted path -- which is the whole of what the two adapters share.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence

#: The tag every Next.js page carries.  Matched with a regex rather than parsed
#: as HTML because the payload is megabytes of JSON and an HTML parser would
#: walk all of it to hand back a string this reads in one pass.
_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    re.DOTALL,
)

#: How the browser is asked for the same thing.  Preferred over parsing the
#: page source when a page object is to hand: it survives a client-side
#: navigation, where the served HTML is whatever the *first* page was.
_PAGE_SCRIPT = """
() => {
  const tag = document.getElementById('__NEXT_DATA__');
  return tag ? tag.textContent : null;
}
"""


def from_html(html: str) -> Optional[Dict[str, Any]]:
    """The payload inside a served page's HTML, or None.

    None covers a page with no such tag at all -- which is what a redirect to a
    sign-in wall, an error page or a bot check looks like -- as well as one
    whose tag holds something that is not JSON.
    """
    if not html:
        return None
    matched = _NEXT_DATA_RE.search(html)
    if matched is None:
        return None
    try:
        data = json.loads(matched.group(1))
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def from_page(page: Any) -> Optional[Dict[str, Any]]:
    """The payload of the page a browser currently has open, or None.

    Read out of the live DOM rather than out of the response body, because a
    Next.js application replaces the tag's contents on a client-side navigation
    and the response body still holds the payload of whatever page was loaded
    first.
    """
    try:
        text = page.evaluate(_PAGE_SCRIPT)
    except KeyboardInterrupt:
        raise
    except Exception:
        return None
    if not isinstance(text, str) or not text:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def dig(data: Any, *path: str) -> Any:
    """Follow a path of keys, giving None the moment one is missing.

    ``dig(payload, "props", "pageProps", "productData")``.  The alternative is a
    chain of ``.get({}, {})`` calls that is unreadable at four levels and wrong
    at five -- a list in the middle of the path makes ``.get`` raise rather than
    return the default.
    """
    node = data
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def dig_list(data: Any, *path: str) -> List[Any]:
    """:func:`dig`, but "not a list" and "missing" both mean an empty list.

    Every caller of this wants to iterate; none of them wants to find out
    whether the absence was a missing key or a null.
    """
    found = dig(data, *path)
    return found if isinstance(found, list) else []


def text_of(value: Any) -> str:
    """A payload value as a stripped string, with ``None`` becoming "".

    The shops put ``null`` where a field does not apply and an empty string
    where it applies and is empty, and nothing downstream cares which: both are
    "the site did not say".
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        # Before the number check: `True` is an int in Python, and "True" is
        # not a value any caller here wants to see rendered into a listing.
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def first_text(node: Any, *keys: str) -> str:
    """The first of ``keys`` that holds something, as text.

    Both payloads carry the same fact under two names depending on how the page
    was reached -- ``sellerName`` on one route and ``sellerId`` on another -- and
    asking for both in order is shorter and clearer than an ``or`` chain that
    has to guard each one for ``None``.
    """
    if not isinstance(node, dict):
        return ""
    for key in keys:
        text = text_of(node.get(key))
        if text:
            return text
    return ""


_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t\r\f\v]+")


def strip_html(value: Any) -> str:
    """A description field with its markup taken off.

    Both shops publish descriptions as HTML.  The monitor stores a listing's
    description as text -- it is what the AI reads, what a keyword filter
    searches and what a notification carries -- so the markup is removed here
    rather than everywhere downstream.

    Line structure is kept: ``<br>`` and ``</p>`` become newlines, because a
    specification list flattened into one paragraph is a specification list
    nobody can read.
    """
    text = text_of(value)
    if not text:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", text)
    text = _TAG_RE.sub(" ", text)
    # Entities the shops actually emit.  A full unescape would pull in a module
    # to handle a handful of cases that are already covered.
    for entity, char in (
        ("&nbsp;", " "),
        ("&amp;", "&"),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&#39;", "'"),
        ("&aacute;", "á"),
        ("&eacute;", "é"),
        ("&iacute;", "í"),
        ("&oacute;", "ó"),
        ("&uacute;", "ú"),
        ("&ntilde;", "ñ"),
    ):
        text = text.replace(entity, char)
    lines = [_SPACE_RE.sub(" ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def joined_price(current: str, was: str) -> str:
    """Two prices as the one string the rest of the monitor stores.

    ``"$149.990 | $219.990"`` -- what it costs, then what the shop is showing
    crossed out.  The same shape :func:`ai_marketplace_monitor.utils.extract_price`
    produces for the other marketplaces, so the price parser, the notification
    card and the dashboard all read a retailer's discount exactly the way they
    read Facebook's.
    """
    current, was = current.strip(), was.strip()
    if not current:
        return was
    if not was or was == current:
        return current
    return f"{current} | {was}"


def any_of(values: Sequence[Any]) -> str:
    """The first value in ``values`` that has any text in it."""
    for value in values:
        text = text_of(value)
        if text:
            return text
    return ""
