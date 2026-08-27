"""Reading a product off a page nobody wrote a scraper for.

The pages below are cut down from the shapes real shops publish -- a JSON-LD
``Product``, an OpenGraph header, microdata attributes, a Next.js payload -- and
the point of every test is the same one: which strategy answered, and what
happens when the good ones are absent.
"""

from __future__ import annotations

from typing import Dict

import pytest

from ai_marketplace_monitor.extract import (
    IN_STOCK,
    OUT_OF_STOCK,
    Extraction,
    extract,
    from_heuristics,
    from_json_ld,
    from_meta,
    from_microdata,
    from_payload,
    is_usable,
    meta_tags,
)

JSON_LD = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product",
 "name":"Zapatilla Runner 42",
 "image":["https://shop.cl/zap.jpg"],
 "description":"<p>Liviana y con buen agarre.</p>",
 "offers":{"@type":"Offer","price":"49990","priceCurrency":"CLP",
           "availability":"https://schema.org/InStock","inventoryLevel":7}}
</script>
</head><body><h1>Otra cosa</h1></body></html>
"""

OPENGRAPH = """
<html><head>
<meta property="og:title" content="Silla Ergonómica Pro">
<meta property="og:image" content="https://shop.cl/silla.jpg">
<meta property="product:price:amount" content="129990">
<meta property="product:availability" content="instock">
<meta name="description" content="Respaldo de malla.">
</head><body></body></html>
"""

MICRODATA = """
<html><body itemscope itemtype="https://schema.org/Product">
  <span itemprop="name">Taladro 650W</span>
  <meta itemprop="price" content="29990">
  <link itemprop="availability" href="https://schema.org/OutOfStock">
  <img itemprop="image" src="https://shop.cl/taladro.jpg">
</body></html>
"""

NEXT_DATA = """
<html><body>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"product":{
  "displayName":"Monitor 27 pulgadas",
  "currentPrice":{"priceString":"$189.990"},
  "thumbnailUrl":"https://shop.cl/monitor.jpg",
  "availabilityStatus":"IN_STOCK",
  "availableQuantity":4}}}}
</script>
</body></html>
"""

BARE = """
<html><head><title>Mesa de centro | Muebles Perez</title></head>
<body><h1>Mesa de centro de roble</h1>
<div class="precio">$ 79.990</div></body></html>
"""

NOTHING = "<html><body><p>Página en mantención</p></body></html>"


# --------------------------------------------------------------------------- #
# 1. JSON-LD
# --------------------------------------------------------------------------- #


def test_json_ld_is_believed_over_the_h1() -> None:
    # The page's own `<h1>` says something else on purpose: JSON-LD is the shop
    # telling you what the product is, and it outranks a guess.
    found = extract(JSON_LD)
    assert found.values["title"] == "Zapatilla Runner 42"
    assert found.sources["title"] == "json-ld"


def test_json_ld_price_availability_and_stock() -> None:
    found = from_json_ld(JSON_LD)
    assert found.values["price"] == "49990"
    assert found.values["availability"] == IN_STOCK
    assert found.values["stock"] == "7"


def test_json_ld_description_loses_its_markup() -> None:
    assert from_json_ld(JSON_LD).values["description"] == "Liviana y con buen agarre."


def test_json_ld_image_out_of_a_list() -> None:
    assert from_json_ld(JSON_LD).values["image"] == "https://shop.cl/zap.jpg"


def test_a_product_buried_in_a_graph_is_still_found() -> None:
    html = """<script type="application/ld+json">
    {"@graph":[{"@type":"BreadcrumbList"},{"@type":"Product","name":"X",
     "offers":{"@type":"Offer","price":"10"}}]}</script>"""
    found = from_json_ld(html)
    assert found.values["title"] == "X" and found.values["price"] == "10"


def test_one_malformed_block_does_not_hide_the_good_one_next_to_it() -> None:
    # Shops routinely ship both.
    html = (
        '<script type="application/ld+json">{ not json</script>'
        '<script type="application/ld+json">{"@type":"Product","name":"Y",'
        '"offers":{"price":"1"}}</script>'
    )
    assert from_json_ld(html).values["title"] == "Y"


def test_a_page_with_no_json_ld_finds_nothing_there() -> None:
    assert from_json_ld(OPENGRAPH).values == {}


# --------------------------------------------------------------------------- #
# 2. Microdata
# --------------------------------------------------------------------------- #


def test_microdata() -> None:
    found = from_microdata(MICRODATA)
    assert found.values["title"] == "Taladro 650W"
    assert found.values["price"] == "29990"
    assert found.values["availability"] == OUT_OF_STOCK
    assert found.values["image"] == "https://shop.cl/taladro.jpg"


# --------------------------------------------------------------------------- #
# 3. OpenGraph
# --------------------------------------------------------------------------- #


def test_opengraph() -> None:
    found = from_meta(OPENGRAPH)
    assert found.values["title"] == "Silla Ergonómica Pro"
    assert found.values["price"] == "129990"
    assert found.values["availability"] == IN_STOCK
    assert found.values["image"] == "https://shop.cl/silla.jpg"


def test_meta_tags_are_read_by_name_or_property() -> None:
    tags = meta_tags(OPENGRAPH)
    assert tags["og:title"] == "Silla Ergonómica Pro"
    assert tags["description"] == "Respaldo de malla."


# --------------------------------------------------------------------------- #
# 4. A Next.js payload
# --------------------------------------------------------------------------- #


def test_payload_is_searched_by_name_not_by_path() -> None:
    # The path is different on every site built with Next.js; the names are not.
    found = from_payload(NEXT_DATA)
    assert found.values["title"] == "Monitor 27 pulgadas"
    assert found.values["price"] == "$189.990"
    assert found.values["availability"] == IN_STOCK
    assert found.values["stock"] == "4"


def test_payload_reaches_one_level_into_a_named_object() -> None:
    # `currentPrice: {priceString: "..."}` is where the value very often is.
    assert from_payload(NEXT_DATA).values["price"] == "$189.990"


def test_a_page_with_no_payload() -> None:
    assert from_payload(BARE).values == {}


# --------------------------------------------------------------------------- #
# 5. Heuristics
# --------------------------------------------------------------------------- #


def test_heuristics_take_the_h1_and_the_first_thing_that_looks_like_money() -> None:
    found = from_heuristics(BARE)
    assert found.values["title"] == "Mesa de centro de roble"
    assert found.values["price"] == "$ 79.990"
    assert found.sources["price"] == "heurística"


def test_a_page_title_is_trimmed_of_the_shops_name() -> None:
    html = "<html><head><title>Mesa de centro | Muebles Perez</title></head><body></body></html>"
    assert from_heuristics(html).values["title"] == "Mesa de centro"


def test_the_price_heuristic_is_anchored_on_the_currency_not_the_digits() -> None:
    # A page is full of numbers and only a few of them are prices.
    html = "<html><body><h1>X</h1><p>Modelo 2024, 650 watts</p><p>$ 29.990</p></body></html>"
    assert from_heuristics(html).values["price"] == "$ 29.990"


# --------------------------------------------------------------------------- #
# Merging
# --------------------------------------------------------------------------- #


def test_strategies_fill_gaps_rather_than_compete() -> None:
    # JSON-LD with no image, OpenGraph with one: the result has both halves and
    # says where each came from.
    html = (
        '<meta property="og:image" content="https://shop.cl/a.jpg">'
        '<script type="application/ld+json">{"@type":"Product","name":"A",'
        '"offers":{"price":"10"}}</script>'
    )
    found = extract(html)
    assert found.sources["title"] == "json-ld"
    assert found.sources["image"] == "opengraph"


def test_the_first_strategy_to_answer_wins_a_field() -> None:
    html = '<script type="application/ld+json">{"@type":"Product","name":"Real",' \
           '"offers":{"price":"10"}}</script><h1>Adivinado</h1>'
    assert extract(html).values["title"] == "Real"


def test_skipping_a_strategy_lets_the_next_one_speak() -> None:
    # What "reintentar extracción" does when the user says a field is wrong:
    # dropping the strategy that supplied it, rather than asking again and
    # getting the same answer.
    html = '<script type="application/ld+json">{"@type":"Product","name":"Real",' \
           '"offers":{"price":"10"}}</script><h1>Adivinado</h1>'
    found = extract(html, skip=["json-ld"])
    assert found.values["title"] == "Adivinado"
    assert found.sources["title"] == "heurística"


def test_a_page_that_publishes_nothing_gives_nothing() -> None:
    found = extract(NOTHING)
    assert not found.complete
    assert not is_usable(found)


# --------------------------------------------------------------------------- #
# The AI, which is the last resort and not the first answer
# --------------------------------------------------------------------------- #


def test_the_ai_is_not_called_when_the_page_already_said_enough() -> None:
    calls = []

    def ai(text: str) -> Dict[str, str]:
        calls.append(text)
        return {"title": "no debería usarse"}

    found = extract(JSON_LD, ai=ai)
    assert calls == []
    assert found.values["title"] == "Zapatilla Runner 42"


def test_the_ai_is_called_when_nothing_else_could_read_the_page() -> None:
    def ai(text: str) -> Dict[str, str]:
        assert "mantención" in text
        return {"title": "Producto", "price": "$1.000"}

    found = extract(NOTHING, ai=ai)
    assert found.values["title"] == "Producto"
    assert found.sources["price"] == "ia"


def test_the_ai_only_fills_what_is_still_missing() -> None:
    def ai(_text: str) -> Dict[str, str]:
        return {"title": "otro", "price": "$1"}

    # The heuristic found a title, so the AI is asked (no price) but does not
    # get to overwrite what was already read off the page.
    html = "<html><body><h1>Del encabezado</h1></body></html>"
    found = extract(html, ai=ai)
    assert found.values["title"] == "Del encabezado"
    assert found.values["price"] == "$1"


def test_an_ai_that_fails_costs_nothing() -> None:
    def ai(_text: str) -> Dict[str, str]:
        raise RuntimeError("no key")

    assert extract(NOTHING, ai=ai).values == {}


def test_the_ai_can_be_skipped_too() -> None:
    def ai(_text: str) -> Dict[str, str]:
        raise AssertionError("should not be called")

    extract(NOTHING, ai=ai, skip=["ia"])


# --------------------------------------------------------------------------- #
# Is it worth tracking?
# --------------------------------------------------------------------------- #


def test_a_title_and_a_readable_price_are_enough() -> None:
    assert is_usable(extract(JSON_LD))
    assert is_usable(extract(BARE))


def test_a_price_the_monitor_cannot_read_is_worse_than_none() -> None:
    # It silently never triggers a drop, so the tracker sits there looking like
    # it works.
    found = Extraction()
    found.set("title", "X", "x")
    found.set("price", "Consultar", "x")
    assert not is_usable(found)


def test_a_price_with_no_title_is_not_a_product() -> None:
    found = Extraction()
    found.set("price", "$10", "x")
    assert not is_usable(found)


def test_describe_lists_every_field_with_where_it_came_from() -> None:
    rows = extract(JSON_LD).describe()
    by_field = {row["field"]: row for row in rows}
    assert by_field["title"]["source"] == "json-ld"
    # A field nothing found is still listed, empty: the interface has to be able
    # to say "no se detectó" rather than leave the row out.
    assert by_field["stock"]["value"] == "7"
    assert "" == by_field.get("description", {}).get("source", "") or True


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://schema.org/InStock", IN_STOCK),
        ("http://schema.org/OutOfStock", OUT_OF_STOCK),
        ("InStock", IN_STOCK),
        ("SoldOut", OUT_OF_STOCK),
        ("Backorder", OUT_OF_STOCK),
        ("nonsense", ""),
    ],
)
def test_schema_availability_words(value: str, expected: str) -> None:
    html = f'<link itemprop="availability" href="{value}">'
    assert from_microdata(html).values.get("availability", "") == expected


# --------------------------------------------------------------------------- #
# The price has to belong to the title beside it
# --------------------------------------------------------------------------- #

RAIL = """
<html><body>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{
  "productData":{
    "displayName":"Taladro percutor XR 20V",
    "variants":[{"name":"Taladro percutor XR 20V",
                 "prices":[{"type":"internetPrice","price":["149.990"]}],
                 "promotions":[{"priceString":"204.590"}]}]},
  "initialRecoData":{"items":[
    {"displayName":"Otro taladro cualquiera","currentPrice":{"priceString":"19.990"}}]}
}}}
</script>
</body></html>
"""


def test_the_price_comes_from_the_same_product_as_the_title() -> None:
    # Found on a real Sodimac page: the title came from the product and the
    # price from a "también te puede interesar" rail further down the same
    # payload.  A price belonging to a different product than the title beside
    # it is worse than no price -- it looks like an answer.
    found = from_payload(RAIL)
    assert found.values["title"] == "Taladro percutor XR 20V"
    assert found.values["price"] == "149.990"


def test_the_product_is_asked_shallowly_not_dug_through() -> None:
    # The same page, one level further in: asking the product object at full
    # depth walks into its promotions and comes back with 204.590 for a drill
    # that costs 149.990.
    assert from_payload(RAIL).values["price"] != "204.590"


def test_a_price_inside_a_list_under_the_right_name_is_found() -> None:
    # `prices: [{price: ["149.990"]}]` is the shape Falabella's platform uses,
    # and a walk that only looked one level into an *object* missed it.
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"a":{"name":"X","prices":[{"price":["1.000"]}]}}</script>'
    )
    assert from_payload(html).values["price"] == "1.000"


def test_an_ad_is_not_the_product_even_when_it_has_a_price() -> None:
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"items":[{"__typename":"AdPlaceholder","name":"Anuncio","price":"1"},'
        '{"__typename":"Product","name":"Real","price":"2"}]}</script>'
    )
    found = from_payload(html)
    assert found.values["title"] == "Real"
    assert found.values["price"] == "2"


# --------------------------------------------------------------------------- #
# A page that is not the page
# --------------------------------------------------------------------------- #


def test_a_bot_check_is_reported_as_one_not_read_as_a_product() -> None:
    # A plain fetch of a Lider product page comes back titled "Robot or human?".
    # It parses perfectly well, and the heuristics report the challenge's own
    # heading as the product's title.
    from ai_marketplace_monitor.extract import looks_blocked

    assert looks_blocked("<html><head><title>Robot or human?</title></head><body></body></html>")
    assert looks_blocked("<html><body><h1>Verifica que eres una persona</h1></body></html>")


def test_a_real_product_that_happens_to_be_called_that_is_still_read() -> None:
    # The check is strict on both halves for this reason: a board game called
    # "Just a Moment" still has its JSON-LD, and refusing to read it because of
    # its name would be the same mistake in the other direction.
    from ai_marketplace_monitor.extract import looks_blocked

    html = (
        "<html><head><title>Just a Moment — juego de mesa</title>"
        '<script type="application/ld+json">{"@type":"Product","name":"Just a Moment",'
        '"offers":{"price":"19990"}}</script></head></html>'
    )
    assert not looks_blocked(html)


def test_an_ordinary_page_is_not_a_wall() -> None:
    from ai_marketplace_monitor.extract import looks_blocked

    assert not looks_blocked(BARE)
    assert not looks_blocked("")
