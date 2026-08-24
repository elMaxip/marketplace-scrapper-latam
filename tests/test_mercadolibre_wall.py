"""Tests for what happens when Mercado Libre stops serving us pages.

The site does not answer an over-eager visitor with an error.  It answers with
an invitation to create an account, which parses as a perfectly good page — so
the failure mode this guards against is not a crash but a monitor that keeps
asking, reads every wall as "this listing has no title", and digs itself in
deeper.
"""

from __future__ import annotations

from typing import Iterator, List, Optional

import pytest

from ai_marketplace_monitor import control
from ai_marketplace_monitor.marketplace import ItemConfig, ListingStatus
from ai_marketplace_monitor.mercadolibre import (
    MercadoLibreMarketplace,
    MercadoLibreMarketplaceConfig,
    MercadoLibreWall,
)


@pytest.fixture(autouse=True)
def clean_control() -> Iterator[None]:
    control.reset_for_tests()
    yield
    control.reset_for_tests()


class FakePage:
    """Just enough of a Playwright page for the wall check."""

    def __init__(self, url: str = "", body: str = "", password: bool = False) -> None:
        self.url = url
        self.body = body
        self.password = password
        self.visited: List[str] = []

    def query_selector(self, selector: str) -> Optional[object]:
        return object() if (self.password and "password" in selector) else None

    def inner_text(self, selector: str, timeout: int = 0) -> str:
        return self.body

    def goto(self, url: str, timeout: int = 0) -> None:
        self.visited.append(url)
        self.url = url

    def wait_for_load_state(self, state: str) -> None:
        return None


LISTING = "https://articulo.mercadolibre.cl/MLC-123-consola/"


def _marketplace(page: FakePage) -> MercadoLibreMarketplace:
    marketplace = MercadoLibreMarketplace("mercadolibre", None, None, None)
    marketplace.configure(MercadoLibreMarketplaceConfig(name="mercadolibre"))
    marketplace.page = page  # type: ignore[assignment]
    return marketplace


def _item() -> ItemConfig:
    return ItemConfig(name="switch", search_phrases=["nintendo switch"])


# --------------------------------------------------------------------------- #
# Recognising the wall
# --------------------------------------------------------------------------- #


def test_a_listing_page_is_not_a_wall() -> None:
    page = FakePage(url=LISTING, body="Consola Nintendo Switch $250.000 Comprar ahora")
    assert _marketplace(page).wall_reason() == ""


@pytest.mark.parametrize(
    "url",
    [
        "https://www.mercadolibre.cl/jms/mlc/lgz/login?platform_id=ML",
        "https://login.mercadolibre.cl/authorization",
        "https://www.mercadolibre.cl/account-verification",
        "https://myaccount.mercadolibre.cl/",
    ],
)
def test_a_redirect_to_a_sign_in_host_is_a_wall(url: str) -> None:
    assert _marketplace(FakePage(url=url, body="algo")).wall_reason() != ""


@pytest.mark.parametrize(
    "body",
    [
        "Ingresa a tu cuenta para continuar",
        "Crea tu cuenta y aprovecha",
        "Hubo un error accediendo",
        "Verifica que eres una persona",
    ],
)
def test_an_in_place_invitation_is_a_wall(body: str) -> None:
    """The URL does not change for this one, which is why the text is read."""
    assert _marketplace(FakePage(url=LISTING, body=body)).wall_reason() != ""


def test_a_password_field_is_a_wall() -> None:
    """Catches a sign-in form whose wording the lists have never seen."""
    page = FakePage(url=LISTING, body="cualquier cosa", password=True)
    assert _marketplace(page).wall_reason() != ""


# --------------------------------------------------------------------------- #
# ... and not mistaking the page we asked for
# --------------------------------------------------------------------------- #
#
# `myaccount.mercadoli` is in the marker list because being *sent* to the
# account area means we were not served what we asked for.  The session check
# asks for that page on purpose, and read plainly the marker turned its own
# destination into a wall: `is_signed_in` could not return True for anybody,
# every imported session was reported as unrecognised, and the tab the probe
# used was left sitting on the account page — the stray "Resumen" window.


def test_the_page_we_asked_for_is_never_a_redirect() -> None:
    account = "https://myaccount.mercadolibre.cl/"
    page = FakePage(url=account, body="Resumen de tu cuenta")
    assert _marketplace(page).wall_reason(asked_for=account) == ""


def test_being_sent_to_the_account_area_is_still_a_wall() -> None:
    """The marker keeps doing its job for every navigation that did not ask
    for that host."""
    page = FakePage(url="https://myaccount.mercadolibre.cl/", body="algo")
    assert _marketplace(page).wall_reason(asked_for=LISTING) != ""


def test_a_signed_in_account_page_reads_as_signed_in() -> None:
    """The whole of the reported bug, end to end: land on the account page and
    the answer is yes."""
    marketplace = _marketplace(FakePage(body="Resumen de tu cuenta"))
    assert marketplace.is_signed_in() is True
    assert marketplace.page.visited == [marketplace.account_url()]  # type: ignore[union-attr]


def test_the_account_area_having_moved_is_not_a_signed_out_session() -> None:
    """Verified against the live site: asking for `myaccount.mercadolibre.cl`
    while signed in ends on `www.mercadolibre.cl/resumen` — the page titled
    "Resumen". The check used to demand the host stay put and so read the
    site's own redirect as a bounce back to the front page."""

    class Moved(FakePage):
        def goto(self, url: str, timeout: int = 0) -> None:
            self.visited.append(url)
            self.url = "https://www.mercadolibre.cl/resumen"

    assert _marketplace(Moved(body="Resumen")).is_signed_in() is True


def test_a_sign_in_wall_on_the_account_page_reads_as_signed_out() -> None:
    marketplace = _marketplace(FakePage(body="Ingresa a tu cuenta"))
    assert marketplace.is_signed_in() is False


def test_the_sign_in_gateway_reads_as_signed_out() -> None:
    """What the same navigation does with no session, taken off the live site:
    a redirect to the login gateway, which the URL markers catch."""

    class Gateway(FakePage):
        def goto(self, url: str, timeout: int = 0) -> None:
            self.visited.append(url)
            self.url = (
                "https://www.mercadolibre.com/jms/mlc/lgz/login"
                "?platform_id=ML&go=https%3A%2F%2Fmyaccount.mercadolibre.cl%2F"
            )

    assert _marketplace(Gateway(body="Ingresa tu e-mail")).is_signed_in() is False


def test_being_dumped_on_the_front_page_reads_as_signed_out() -> None:
    """The other shape of refusal: no gateway, just the front door. Landing on
    the site root is the one place that is not an account page."""

    class Bounced(FakePage):
        def goto(self, url: str, timeout: int = 0) -> None:
            self.visited.append(url)
            self.url = "https://www.mercadolibre.cl/"

    assert _marketplace(Bounced(body="Crea tu cuenta")).is_signed_in() is False


# --------------------------------------------------------------------------- #
# Reacting to it
# --------------------------------------------------------------------------- #


def test_a_wall_stops_the_marketplace_and_raises() -> None:
    marketplace = _marketplace(FakePage(body="Ingresa a tu cuenta"))
    with pytest.raises(MercadoLibreWall):
        marketplace.open_page(LISTING)
    assert control.marketplace_blocked("mercadolibre") is True


def test_a_normal_page_clears_a_cooldown() -> None:
    """The site has evidently forgiven us; carrying the block on would waste the
    rest of it."""
    control.block_marketplace("mercadolibre", reason="test")
    marketplace = _marketplace(FakePage(body="Consola Nintendo Switch"))
    marketplace.open_page(LISTING)
    assert control.marketplace_blocked("mercadolibre") is False


def test_consecutive_refusals_back_off_further() -> None:
    first = control.block_marketplace("mercadolibre", reason="test")
    second = control.block_marketplace("mercadolibre", reason="test")
    assert second["seconds"] > first["seconds"]


def test_a_wall_starts_at_the_shortest_cooldown() -> None:
    """A refusal is a "wait a while", not a "give up": the backoff starts at
    its first step whether or not a session has ever been signed in."""
    marketplace = _marketplace(FakePage(body="Ingresa a tu cuenta"))
    with pytest.raises(MercadoLibreWall):
        marketplace.open_page(LISTING)
    block = control.marketplace_block("mercadolibre")
    assert block is not None
    assert block["seconds"] == control.BLOCK_BACKOFF[0]


def test_searching_stops_while_blocked() -> None:
    control.block_marketplace("mercadolibre", reason="test")
    marketplace = _marketplace(FakePage())
    assert list(marketplace.search(_item())) == []
    # Nothing was even navigated to.
    assert marketplace.page.visited == []  # type: ignore[union-attr]


def test_re_checking_a_listing_while_blocked_is_undecided() -> None:
    """Undecided, never gone: a wall says nothing about whether a listing
    still exists, and this verdict can delete one."""
    control.block_marketplace("mercadolibre", reason="test")
    marketplace = _marketplace(FakePage())
    status, details = marketplace.recheck_listing(LISTING, _item())
    assert (status, details) == (ListingStatus.UNKNOWN, None)


def test_a_wall_during_a_re_check_never_deletes() -> None:
    marketplace = _marketplace(FakePage(body="Ingresa a tu cuenta"))
    status, details = marketplace.recheck_listing(LISTING, _item())
    assert (status, details) == (ListingStatus.UNKNOWN, None)
    assert control.marketplace_blocked("mercadolibre") is True


# --------------------------------------------------------------------------- #
# Searching without a sign-in
# --------------------------------------------------------------------------- #
#
# There is no configuration that stops Mercado Libre from being searched
# anonymously.  It answers anonymous visitors, and a switch that turned the
# platform off on the user's behalf produced the worst possible outcome: a
# monitor that looked healthy and quietly searched nowhere.


def test_no_session_still_searches(monkeypatch: pytest.MonkeyPatch) -> None:
    marketplace = _marketplace(FakePage())
    monkeypatch.setattr(marketplace, "has_saved_session", lambda: False)
    assert marketplace.login() is True


def test_a_session_changes_nothing_about_whether_it_searches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marketplace = _marketplace(FakePage())
    monkeypatch.setattr(marketplace, "has_saved_session", lambda: True)
    assert marketplace.login() is True


def test_the_retired_option_no_longer_exists() -> None:
    """`require_login` is gone from the config class, not merely ignored by the
    code that used to read it."""
    assert not hasattr(MercadoLibreMarketplaceConfig(name="mercadolibre"), "require_login")
