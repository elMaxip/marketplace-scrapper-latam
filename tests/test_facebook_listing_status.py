"""Tests for reading whether a Facebook listing still exists.

This verdict can delete a stored listing, so the bar is deliberately high: only
Facebook saying the item is sold, or that the page does not exist, counts.
Everything else -- a login wall, an empty render, an unfamiliar layout -- has to
come back as "cannot tell".
"""

from __future__ import annotations

from typing import Optional

import pytest

from ai_marketplace_monitor.facebook import FacebookMarketplace
from ai_marketplace_monitor.marketplace import ListingStatus
from ai_marketplace_monitor.utils import Translator


class FakeNode:
    def __init__(self, text: str) -> None:
        self._text = text

    def text_content(self) -> str:
        return self._text


class FakePage:
    """Just enough of a Playwright page for the status check."""

    def __init__(self, url: str = "", heading: str = "", body: str = "") -> None:
        self.url = url
        self.heading = heading
        self.body = body

    def query_selector(self, selector: str) -> Optional[FakeNode]:
        return FakeNode(self.heading) if self.heading else None

    def inner_text(self, selector: str, timeout: int = 0) -> str:
        return self.body


def _marketplace(page: FakePage, translator: Optional[Translator] = None) -> FacebookMarketplace:
    marketplace = FacebookMarketplace("facebook", None, None, None)
    marketplace.page = page  # type: ignore[assignment]
    if translator is not None:
        marketplace.translator = translator
    return marketplace


LISTING_URL = "https://www.facebook.com/marketplace/item/1234/"


def test_a_normal_listing_is_active() -> None:
    page = FakePage(
        url=LISTING_URL,
        heading="PlayStation 5 con dos controles",
        body="PlayStation 5 con dos controles $450.000 Detalles del vendedor",
    )
    assert _marketplace(page)._page_status() is ListingStatus.ACTIVE


@pytest.mark.parametrize(
    "heading",
    [
        "Vendido PlayStation 5",
        "Vendido: PlayStation 5",
        "VENDIDO PlayStation 5",
        "Sold PlayStation 5",
        "Vendida Bicicleta",
    ],
)
def test_a_sold_badge_on_the_heading_is_sold(heading: str) -> None:
    page = FakePage(url=LISTING_URL, heading=heading, body="algo")
    assert _marketplace(page)._page_status() is ListingStatus.SOLD


@pytest.mark.parametrize(
    "heading",
    [
        "PlayStation 5 vendido por un amigo",
        "Repuestos de consola sold out",
        "Se vendio rapido, quedan dos",
    ],
)
def test_the_word_elsewhere_in_the_heading_is_not_sold(heading: str) -> None:
    """"Sold" is an ordinary word in the middle of a sentence. Matching it there
    would delete listings that are perfectly alive."""
    page = FakePage(url=LISTING_URL, heading=heading, body="algo")
    assert _marketplace(page)._page_status() is ListingStatus.ACTIVE


def test_the_translated_word_is_matched() -> None:
    translator = Translator(locale="Swedish", dictionary={"Sold": "Såld"})
    page = FakePage(url=LISTING_URL, heading="Såld Cykel", body="något")
    assert _marketplace(page, translator)._page_status() is ListingStatus.SOLD


@pytest.mark.parametrize(
    "body",
    [
        "This content isn't available right now",
        "Este contenido no está disponible en este momento",
        "Esta página no está disponible. El enlace que has seguido puede estar roto.",
    ],
)
def test_a_missing_page_is_gone(body: str) -> None:
    page = FakePage(url=LISTING_URL, heading="", body=body)
    assert _marketplace(page)._page_status() is ListingStatus.GONE


def test_the_phrase_inside_a_live_listing_is_not_gone() -> None:
    """A seller can write anything in a description. A page that still has its
    own heading is a listing, whatever it quotes."""
    page = FakePage(
        url=LISTING_URL,
        heading="Camiseta rara",
        body="Camiseta rara. Dice \"this content isn't available right now\" en la espalda.",
    )
    assert _marketplace(page)._page_status() is ListingStatus.ACTIVE


@pytest.mark.parametrize(
    "url",
    [
        "https://www.facebook.com/login/?next=marketplace",
        "https://www.facebook.com/checkpoint/12345",
        "https://www.facebook.com/two_step_verification/authentication/",
    ],
)
def test_a_login_wall_is_undecided(url: str) -> None:
    """It looks exactly like a missing listing and means nothing of the sort."""
    page = FakePage(url=url, heading="", body="This content isn't available right now")
    assert _marketplace(page)._page_status() is ListingStatus.UNKNOWN


def test_an_empty_page_is_undecided() -> None:
    """A page that never rendered is not a deleted one."""
    page = FakePage(url=LISTING_URL, heading="", body="")
    assert _marketplace(page)._page_status() is ListingStatus.UNKNOWN


def test_an_unfamiliar_page_with_no_heading_is_not_deleted() -> None:
    """No heading and no "not available" phrase either: something changed, and
    guessing which way would be how a live listing gets thrown away."""
    page = FakePage(url=LISTING_URL, heading="", body="algo completamente distinto")
    assert _marketplace(page)._page_status() is ListingStatus.ACTIVE
