"""Reading a product off a page nobody wrote a scraper for.

The marketplaces and shops this monitor supports each have a module that knows
their payload by name.  This one has to work on a page it has never seen: the
user pastes an address, and something has to come back with a title, a price and
whether the thing is in stock.

There is no single way to do that, so there are six, tried in the order of how
much they can be trusted:

1. **JSON-LD** -- a ``<script type="application/ld+json">`` carrying
   ``@type: Product``.  This is the shop *telling* you the price, in a format it
   publishes on purpose so that Google gets it right, and it is right far more
   often than anything below it.
2. **Microdata** -- the same vocabulary spelled with ``itemprop`` attributes.
   Older, still common, and just as deliberate.
3. **OpenGraph** -- ``og:title``, ``product:price:amount``.  Published for
   social previews, so the title is reliable and the price is present about half
   the time.
4. **Next.js payload** -- the whole page state, for the many sites built on it.
   Searched by field name rather than by path, because the path is different on
   every site and the names are not.
5. **Heuristics** -- the ``<h1>``, the first thing on the page that looks like
   money, the largest image.  Frequently right and never trustworthy, which is
   why it is fifth.
6. **The AI**, when one is configured -- handed the page's text and asked for
   the four fields.  Last because it costs a call and can be confidently wrong,
   and *present* because it is the only one that can read a page that publishes
   none of the above.

Strategies do not compete; they fill gaps.  Each returns whatever it could find
and the results are merged field by field, first strategy wins, with a note of
*which* one supplied each field.  That note is the whole point of the "reintentar
extracción" button: a title that came from the ``<h1>`` and a title that came
from JSON-LD are worth different amounts of confidence, and the person about to
create a tracker deserves to be told which they are looking at.

Everything here takes HTML and returns data.  No browser, no network, no config
-- which is what lets the rules be tested against pages saved from real shops.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .nextdata import from_html, strip_html, text_of
from .utils import price_value

#: What the extractor tries to find.  Deliberately short: these are the four
#: things a tracker acts on, and a field nobody acts on is a field that can be
#: wrong without anybody noticing.
FIELDS: Tuple[str, ...] = ("title", "price", "image", "stock", "availability", "description")

IN_STOCK = "in_stock"
OUT_OF_STOCK = "out_of_stock"

#: schema.org availability values, folded to the monitor's two words.
_SCHEMA_AVAILABILITY = {
    "instock": IN_STOCK,
    "in_stock": IN_STOCK,
    "onlineonly": IN_STOCK,
    "limitedavailability": IN_STOCK,
    "presale": IN_STOCK,
    "preorder": IN_STOCK,
    "backorder": OUT_OF_STOCK,
    "outofstock": OUT_OF_STOCK,
    "out_of_stock": OUT_OF_STOCK,
    "soldout": OUT_OF_STOCK,
    "discontinued": OUT_OF_STOCK,
}


@dataclass
class Extraction:
    """What one strategy found, and nothing about what it did not.

    Missing is missing: a strategy that could not read the price leaves it out
    rather than guessing, so the merge below can fall through to the next one.
    """

    #: field -> value, only for fields this strategy actually found.
    values: Dict[str, str] = field(default_factory=dict)
    #: field -> the name of the strategy that supplied it.
    sources: Dict[str, str] = field(default_factory=dict)

    def set(self: "Extraction", key: str, value: Any, source: str) -> None:
        text = value if isinstance(value, str) else text_of(value)
        text = (text or "").strip()
        if not text or key in self.values:
            return
        self.values[key] = text
        self.sources[key] = source

    def merge(self: "Extraction", other: "Extraction") -> None:
        """Take from ``other`` only what this one is still missing."""
        for key, value in other.values.items():
            self.set(key, value, other.sources.get(key, ""))

    @property
    def complete(self: "Extraction") -> bool:
        """Whether there is enough here to be worth tracking.

        A title and a price.  Stock and availability are genuinely absent on
        most pages -- a marketplace listing has neither -- so demanding them
        would refuse to track the majority of what people paste.
        """
        return bool(self.values.get("title") and self.values.get("price"))

    def describe(self: "Extraction") -> List[Dict[str, str]]:
        """The findings as rows for the interface: field, value, where from."""
        return [
            {
                "field": name,
                "value": self.values.get(name, ""),
                "source": self.sources.get(name, ""),
            }
            for name in FIELDS
        ]


# --------------------------------------------------------------------------- #
# 1. JSON-LD
# --------------------------------------------------------------------------- #

_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def _walk_json(node: Any) -> Iterable[Dict[str, Any]]:
    """Every object inside a JSON-LD blob, however it is nested.

    Shops publish the same thing four ways: a bare object, a list of objects, a
    ``@graph`` of them, and a ``Product`` buried inside a ``BreadcrumbList``.
    Walking is shorter than handling each shape and does not miss the fifth one.
    """
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_json(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_json(value)


def _is_product(node: Dict[str, Any]) -> bool:
    kinds = node.get("@type")
    if isinstance(kinds, str):
        kinds = [kinds]
    if not isinstance(kinds, list):
        return False
    return any(str(kind).lower() in ("product", "itempage", "individualproduct") for kind in kinds)


def _first_offer(product: Dict[str, Any]) -> Dict[str, Any]:
    offers = product.get("offers")
    for node in _walk_json(offers):
        if node.get("price") is not None or node.get("lowPrice") is not None:
            return node
    return offers if isinstance(offers, dict) else {}


def _schema_availability(value: Any) -> str:
    """``https://schema.org/InStock`` -> ``in_stock``."""
    text = text_of(value).rsplit("/", 1)[-1].rsplit("#", 1)[-1].strip().lower()
    return _SCHEMA_AVAILABILITY.get(text, "")


def _image_of(value: Any) -> str:
    """The first usable image out of the several shapes schema.org allows."""
    for node in _walk_json(value) if isinstance(value, (dict, list)) else []:
        url = text_of(node.get("url") or node.get("contentUrl"))
        if url:
            return url
    if isinstance(value, list) and value:
        return text_of(value[0])
    return text_of(value)


def from_json_ld(html: str) -> Extraction:
    """What the page publishes about itself, in the format it publishes it in."""
    found = Extraction()
    for blob in _LD_RE.findall(html or ""):
        try:
            data = json.loads(blob)
        except (ValueError, TypeError):
            # One malformed block must not hide the good one next to it: shops
            # routinely ship both.
            continue
        for node in _walk_json(data):
            if not _is_product(node):
                continue
            offer = _first_offer(node)
            found.set("title", node.get("name"), "json-ld")
            found.set("price", offer.get("price") or offer.get("lowPrice"), "json-ld")
            found.set("image", _image_of(node.get("image")), "json-ld")
            found.set("description", strip_html(node.get("description")), "json-ld")
            found.set(
                "availability", _schema_availability(offer.get("availability")), "json-ld"
            )
            found.set("stock", offer.get("inventoryLevel"), "json-ld")
            if found.complete:
                return found
    return found


# --------------------------------------------------------------------------- #
# 2. Microdata
# --------------------------------------------------------------------------- #

#: An ``itemprop`` element, with whatever text follows it up to the next tag.
#:
#: Two shapes, and both are common: ``<meta itemprop="price" content="10">``
#: carries the value in an attribute, while ``<span itemprop="name">X</span>``
#: carries it as text.  Reading only the attributes -- which is the obvious way
#: to write this -- silently misses every field a shop marked up on a visible
#: element, which is most of the names and half of the prices.
_ITEMPROP_RE = re.compile(
    r'<(\w+)[^>]*\bitemprop=["\']([\w:]+)["\'][^>]*>([^<]*)',
    re.IGNORECASE,
)
_ATTR_RE = re.compile(r'\b(content|href|src|value|datetime)=["\']([^"\']*)["\']', re.IGNORECASE)

_MICRO_FIELDS = {
    "name": "title",
    "price": "price",
    "image": "image",
    "availability": "availability",
    "description": "description",
}


def from_microdata(html: str) -> Extraction:
    """The same vocabulary as JSON-LD, spelled as attributes."""
    found = Extraction()
    for match in _ITEMPROP_RE.finditer(html or ""):
        name = match.group(2).split(":")[-1].lower()
        target = _MICRO_FIELDS.get(name)
        if target is None:
            continue
        tag = match.group(0).split(">", 1)[0]
        attrs = dict((key.lower(), value) for key, value in _ATTR_RE.findall(tag))
        value = (
            attrs.get("content")
            or attrs.get("href")
            or attrs.get("src")
            or attrs.get("value")
            # The text of a visible element, when the value is not an attribute.
            or match.group(3).strip()
        )
        if not value:
            continue
        if target == "availability":
            value = _schema_availability(value)
        elif target == "description":
            value = strip_html(value)
        found.set(target, value, "microdata")
    return found


# --------------------------------------------------------------------------- #
# 3. OpenGraph and friends
# --------------------------------------------------------------------------- #

_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_META_NAME_RE = re.compile(r'\b(?:property|name)=["\']([^"\']+)["\']', re.IGNORECASE)
_META_CONTENT_RE = re.compile(r'\bcontent=["\']([^"\']*)["\']', re.IGNORECASE)

#: meta name -> field.  Ordered per field: the first one present wins, and the
#: order is "most specific to this page" first.
_META_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("og:title", "title"),
    ("twitter:title", "title"),
    ("product:price:amount", "price"),
    ("og:price:amount", "price"),
    ("twitter:data1", "price"),
    ("og:image", "image"),
    ("twitter:image", "image"),
    ("og:description", "description"),
    ("description", "description"),
    ("product:availability", "availability"),
    ("og:availability", "availability"),
)


def meta_tags(html: str) -> Dict[str, str]:
    """Every ``<meta>`` on the page, by its name or property."""
    tags: Dict[str, str] = {}
    for tag in _META_RE.findall(html or ""):
        name = _META_NAME_RE.search(tag)
        content = _META_CONTENT_RE.search(tag)
        if name is None or content is None:
            continue
        key = name.group(1).strip().lower()
        if key not in tags:
            tags[key] = content.group(1).strip()
    return tags


def from_meta(html: str) -> Extraction:
    """What the page tells a social network about itself."""
    tags = meta_tags(html)
    found = Extraction()
    for key, target in _META_FIELDS:
        value = tags.get(key)
        if not value:
            continue
        if target == "availability":
            value = _schema_availability(value)
        found.set(target, value, "opengraph")
    return found


# --------------------------------------------------------------------------- #
# 4. A Next.js payload, searched by name
# --------------------------------------------------------------------------- #

#: field -> the keys sites actually call it, most specific first.
#:
#: Searched by name rather than by path on purpose: the path is different on
#: every site built with Next.js and the names are not, and a list of paths
#: would be a list that is out of date the day after it is written.
_PAYLOAD_KEYS: Dict[str, Tuple[str, ...]] = {
    "title": ("displayName", "productName", "name", "title"),
    "price": ("priceString", "currentPrice", "salePrice", "listPrice", "price"),
    "image": ("thumbnailUrl", "imageUrl", "mainImage", "image"),
    "stock": ("availableQuantity", "stockLevel", "quantityAvailable", "stock"),
    "availability": ("availabilityStatus", "availability", "inStock"),
    "description": ("longDescription", "description"),
}

#: How deep to look.  Payloads nest an entire application's state, and past this
#: the matches are somebody else's product in a "you may also like" rail.
_PAYLOAD_DEPTH = 8


def _scalar(value: Any) -> str:
    """A payload value as text, when it is a value at all.

    Objects are skipped rather than stringified: ``{"price": 1, "symbol": "$"}``
    rendered as its ``repr`` is not a price, it is a bug that looks like one.
    """
    if isinstance(value, bool):
        return IN_STOCK if value else OUT_OF_STOCK
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    return ""


def _search_payload(
    node: Any, keys: Tuple[str, ...], max_depth: int = _PAYLOAD_DEPTH, depth: int = 0
) -> str:
    """The first of ``keys`` holding a scalar, at most ``max_depth`` levels down.

    ``max_depth`` is a parameter so the same walk can answer a *shallow*
    question -- "does this object itself carry a price?" -- which is what
    :func:`_product_node` needs and what the default depth cannot express.
    """
    if depth > max_depth:
        return ""
    if isinstance(node, dict):
        for key in keys:
            if key in node:
                text = _scalar(node[key])
                if text:
                    return text
                # A nested object under the right name: the value is very often
                # one level in (`currentPrice: {priceString: "$1"}`).
                if isinstance(node[key], dict):
                    for inner in node[key].values():
                        text = _scalar(inner)
                        if text:
                            return text
                    continue
                # Or a list of them (`prices: [{price: ["149.990"]}]`).
                if isinstance(node[key], list):
                    for inner in node[key][:5]:
                        text = _scalar(inner)
                        if text:
                            return text
        for value in node.values():
            text = _search_payload(value, keys, max_depth, depth + 1)
            if text:
                return text
    elif isinstance(node, list):
        for value in node[:20]:
            text = _search_payload(value, keys, max_depth, depth + 1)
            if text:
                return text
    return ""


#: How far a candidate is allowed to be from its own **name** for it to be one
#: product: zero, meaning the key is on the object itself.
#:
#: This is the half that rules out *containers*.  ``{"items": [...]}`` has a
#: name somewhere under it and a price somewhere under it, and a check that
#: allowed either to be nested would settle on the container and then take each
#: field from whichever child happened to come first -- which is how a title and
#: a price from two different products end up side by side.
_NAME_DEPTH = 0

#: How far it may be from its **price**: two, because that is where prices
#: genuinely live.  ``prices: [{price: ["149.990"]}]`` on Falabella's platform,
#: ``currentPrice: {priceString: ...}`` on Walmart's.  Demanding the price on
#: the object itself was tried and is wrong on both -- it walks past the real
#: product and settles on some banner further down.
_PRICE_DEPTH = 2

#: How deep the chosen node is then asked for its fields.  The same two: a
#: product carries its facts on itself, and asking it deeper walks into its
#: promotions and accessories -- see :func:`from_payload`.
_PRODUCT_NODE_DEPTH = _PRICE_DEPTH

#: Keys whose contents are somebody else's product.
#:
#: Every site names these, and the names are much more stable than the paths:
#: a "también te puede interesar" rail is a rail on every deployment, whatever
#: the surrounding structure was refactored into this quarter.
_RAIL_KEYS = (
    "reco",
    "recommend",
    "related",
    "similar",
    "suggest",
    "carousel",
    "alsobought",
    "alsoviewed",
    "youmayalsolike",
    "crosssell",
    "upsell",
    "complementary",
    "accessor",
)

#: Objects that are an advert rather than a product, by the site's own label.
_AD_TYPENAMES = ("AdPlaceholder",)


def _is_rail(key: str) -> bool:
    folded = key.replace("_", "").replace("-", "").lower()
    return any(marker in folded for marker in _RAIL_KEYS)


def _product_node(node: Any, depth: int = 0) -> Optional[Dict[str, Any]]:
    """The first object in the payload that is *one* product.

    "Is one product" is two different questions, asked at two different depths,
    and getting either wrong was observed on a live page:

    * the **name** must be on the object itself (:data:`_NAME_DEPTH`), which is
      what rules out containers -- see there;
    * the **price** may be two levels in (:data:`_PRICE_DEPTH`), which is where
      every platform actually writes it.

    Searching field by field across the whole payload does not do this, and the
    difference is not academic: it was found by watching a real Sodimac page
    report a title from the product and a price of $19.990 from a "también te
    puede interesar" rail further down the same payload.  A price belonging to a
    different product than the title beside it is worse than no price -- it
    looks like an answer.

    Recommendation rails are skipped by key name, and adverts by the site's own
    ``__typename``.  Neither is a guess: both are labels the sites write
    themselves.
    """
    if depth > _PAYLOAD_DEPTH:
        return None
    if isinstance(node, dict):
        if node.get("__typename") not in _AD_TYPENAMES:
            has_name = _search_payload(node, _PAYLOAD_KEYS["title"], _NAME_DEPTH)
            has_price = _search_payload(node, _PAYLOAD_KEYS["price"], _PRICE_DEPTH)
            if has_name and has_price:
                return node
        for key, value in node.items():
            if _is_rail(str(key)):
                continue
            found = _product_node(value, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node[:20]:
            found = _product_node(value, depth + 1)
            if found is not None:
                return found
    return None


def from_payload(html: str) -> Extraction:
    """The page's own state, for the many sites that ship it."""
    payload = from_html(html)
    found = Extraction()
    if payload is None:
        return found

    # One coherent product first, then the whole payload for whatever that node
    # did not carry.  The second pass earns its place: stock and availability
    # frequently sit beside the product rather than inside it.
    #
    # The product is asked *shallowly* and the payload deeply, and that is the
    # whole difference between a right answer and a plausible one.  A product
    # object carries its price on itself; asking it at full depth walks into its
    # promotions, its bundles and its accessories, and comes back with the first
    # `priceString` it meets down there -- which on a real Sodimac page was
    # 204.590 for a drill that costs 149.990.
    for source, reach in ((_product_node(payload), _PRODUCT_NODE_DEPTH), (payload, _PAYLOAD_DEPTH)):
        if source is None:
            continue
        for target, keys in _PAYLOAD_KEYS.items():
            if target in found.values:
                continue
            value = _search_payload(source, keys, reach)
            if not value:
                continue
            if target == "availability":
                value = _schema_availability(value) or (
                    value if value in (IN_STOCK, OUT_OF_STOCK) else ""
                )
            elif target == "description":
                value = strip_html(value)
            found.set(target, value, "next-data")
    return found


# --------------------------------------------------------------------------- #
# 5. Heuristics
# --------------------------------------------------------------------------- #

_H1_RE = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.DOTALL | re.IGNORECASE)
_TITLE_RE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)

#: Something that looks like money: a symbol or code, then a grouped number.
#: Anchored on the *currency* rather than on the digits, because a page is full
#: of numbers and only a few of them are prices.
_MONEY_RE = re.compile(
    r"(?:US\$|CLP|MXN|ARS|COP|PEN|BRL|EUR|USD|R\$|\$|€|£)\s?\d{1,3}(?:[.,\s ]\d{3})*"
    r"(?:[.,]\d{1,2})?"
)


def from_heuristics(html: str) -> Extraction:
    """The ``<h1>``, the first thing that looks like money, the page title.

    Frequently right and never trustworthy, which is why it is tried last
    before the AI -- and why the interface says where each field came from.
    """
    found = Extraction()
    text = html or ""

    heading = _H1_RE.search(text)
    if heading:
        title = strip_html(heading.group(1))
        found.set("title", title, "heurística")
    if "title" not in found.values:
        page_title = _TITLE_RE.search(text)
        if page_title:
            # A page title is usually "Product | Shop"; the shop's name is not
            # part of what is being tracked.
            title = strip_html(page_title.group(1)).split("|")[0].split(" - ")[0]
            found.set("title", title, "heurística")

    money = _MONEY_RE.search(strip_html(text))
    if money:
        found.set("price", money.group(0), "heurística")
    return found


# --------------------------------------------------------------------------- #
# The whole thing
# --------------------------------------------------------------------------- #

#: The strategies, in the order they are tried, with the name the interface
#: shows for each.
STRATEGIES: Tuple[Tuple[str, Callable[[str], Extraction]], ...] = (
    ("json-ld", from_json_ld),
    ("microdata", from_microdata),
    ("opengraph", from_meta),
    ("next-data", from_payload),
    ("heurística", from_heuristics),
)


def extract(
    html: str,
    ai: Optional[Callable[[str], Dict[str, str]]] = None,
    skip: Iterable[str] = (),
) -> Extraction:
    """Everything the page will give up, best source per field.

    ``ai`` is called only when the cheaper strategies left the result
    incomplete, and only when one is configured: it costs a request and it can
    be confidently wrong, so it is the fallback rather than the first answer.

    ``skip`` names strategies not to use, which is what "reintentar extracción"
    passes when the user says a field is wrong: dropping the strategy that
    supplied it makes the next one down speak instead of returning the same
    answer again.
    """
    skipped = {name.lower() for name in skip}
    found = Extraction()
    for name, strategy in STRATEGIES:
        if name in skipped:
            continue
        try:
            found.merge(strategy(html))
        except KeyboardInterrupt:
            raise
        except Exception:
            # One strategy failing on a strange page must not stop the others:
            # the point of having six is that any of them can be wrong.
            continue

    if found.complete or ai is None or "ia" in skipped:
        return found
    try:
        answered = ai(strip_html(html)) or {}
    except KeyboardInterrupt:
        raise
    except Exception:
        return found
    for key in FIELDS:
        found.set(key, answered.get(key), "ia")
    return found


def normalized_price(value: str) -> str:
    """A price as the monitor stores it: exactly what the page printed.

    Never re-formatted and never given a symbol it did not have -- the same rule
    the rest of the monitor follows, and for the same reason: a Chilean page
    prints "450.000" with no symbol at all and inventing a "$" for it is
    inventing a fact.  A bare number published in a JSON-LD ``price`` field is
    left bare, because that is what it is.
    """
    return (value or "").strip()


def is_usable(found: Extraction) -> bool:
    """Whether this is worth offering as a tracker.

    A title and a price that parses.  A price the monitor cannot read is worse
    than no price: it silently never triggers a drop, so the tracker would sit
    there looking like it worked.
    """
    return bool(found.values.get("title")) and price_value(found.values.get("price")) is not None


# --------------------------------------------------------------------------- #
# The AI as one more strategy
# --------------------------------------------------------------------------- #

#: How much of a page to show the model.
#:
#: A product page's own text is a few hundred words; the rest is navigation,
#: footers and recommendations.  Cutting keeps the cost bounded and the answer
#: focused -- and what matters is at the top, because that is where a shop puts
#: the thing it is selling.
AI_TEXT_LIMIT = 6000

AI_PROMPT = """Extract the product on this page.

Answer with JSON and nothing else, using exactly these keys:
  "title"        the product's name
  "price"        the price, copied exactly as the page writes it, symbol included
  "stock"        how many units are left, as a plain number, or ""
  "availability" "in_stock", "out_of_stock", or ""
  "description"  one or two sentences describing the product, or ""

Use "" for anything the page does not say. Do not convert currencies, do not
reformat the price, and do not guess a number that is not written.

PAGE TEXT:
{text}
"""

#: A JSON object, with or without the ``` fence a model adds anyway.
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def ai_reader(backend: Any) -> Optional[Callable[[str], Dict[str, str]]]:
    """An ``ai=`` callback for :func:`extract`, or None when there is no AI.

    None rather than a callback that fails, so the extractor can tell "no AI
    configured" from "the AI was asked and could not answer" -- the first is the
    ordinary case and the second is worth a debug line.
    """
    if backend is None or not hasattr(backend, "ask"):
        return None

    def read(text: str) -> Dict[str, str]:
        answer = backend.ask(AI_PROMPT.format(text=text[:AI_TEXT_LIMIT]))
        return parse_ai_answer(answer)

    return read


def parse_ai_answer(answer: str) -> Dict[str, str]:
    """The model's reply as the fields it was asked for.

    Tolerant of the two things models do to JSON regardless of instructions:
    wrapping it in a markdown fence, and writing a sentence before it.  Anything
    that is still not JSON, or that is JSON of the wrong shape, comes back empty
    -- which the merge then treats as "the AI found nothing", and the page's own
    answers stand.
    """
    matched = _JSON_RE.search(answer or "")
    if matched is None:
        return {}
    try:
        data = json.loads(matched.group(0))
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: str(data[key]).strip()
        for key in FIELDS
        if key in data and data[key] is not None and str(data[key]).strip()
    }


# --------------------------------------------------------------------------- #
# A page that is not the page
# --------------------------------------------------------------------------- #

#: Titles a bot check, a sign-in wall or a consent gate puts on itself.
#:
#: Found by testing rather than assumed: a plain fetch of a Lider product page
#: comes back titled "Robot or human?" and one of a Mercado Libre listing comes
#: back as the site's front page.  Both parse perfectly well, and the heuristic
#: strategy dutifully reports the challenge's own heading as the product's
#: title -- a confident-looking wrong answer, which is worse than no answer.
_WALL_TITLES: Tuple[str, ...] = (
    "robot or human",
    "are you a robot",
    "verifica que eres",
    "verificando que eres",
    "just a moment",
    "access denied",
    "acceso denegado",
    "attention required",
    "pardon our interruption",
    "checking your browser",
    "ingresa a tu cuenta",
    "inicia sesion",
    "iniciar sesion",
    "sign in to",
    "log in to",
    "captcha",
)


def looks_blocked(html: str) -> bool:
    """Whether this is a challenge or a wall rather than the page asked for.

    Deliberately strict on *both* halves: the title has to look like a wall
    **and** the page has to publish no product markup at all.  A real product
    called "Just a Moment" (a board game, a book) still has its JSON-LD, and
    refusing to read it because of its name would be the same mistake in the
    other direction.
    """
    if not html:
        return False
    if from_json_ld(html).values or from_microdata(html).values:
        return False
    if from_meta(html).values.get("price") or from_payload(html).values.get("price"):
        return False
    title = strip_html((_TITLE_RE.search(html) or _H1_RE.search(html) or _Empty()).group(1))
    folded = title.strip().lower()
    return any(marker in folded for marker in _WALL_TITLES)


class _Empty:
    """Stands in for a regex match that is not there, so the check reads flat."""

    @staticmethod
    def group(_index: int) -> str:
        return ""
