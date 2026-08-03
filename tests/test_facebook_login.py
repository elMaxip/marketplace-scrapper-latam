"""Tests for Facebook login detection.

The monitor used to type its credentials, sleep for a fixed minute and carry on
regardless.  When two-factor verification took longer than that it searched
unauthenticated, which silently returns the marketplace's own default city --
so these cover the signal that decides whether a session is really live.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

from ai_marketplace_monitor.facebook import FacebookMarketplace


class FakePage:
    """Minimal stand-in for a Playwright Page plus its context."""

    def __init__(self, cookies: List[Dict[str, Any]], url: str, fail: bool = False) -> None:
        self._cookies = cookies
        self.url = url
        self.fail = fail
        self.context = self

    def cookies(self) -> List[Dict[str, Any]]:
        if self.fail:
            raise RuntimeError("browser is gone")
        return self._cookies


def _marketplace(page: Any) -> FacebookMarketplace:
    marketplace = FacebookMarketplace.__new__(FacebookMarketplace)
    marketplace.page = page
    return marketplace


SIGNED_IN = [{"name": "c_user", "value": "42"}]


def test_session_cookie_on_a_normal_page_means_signed_in() -> None:
    page = FakePage(SIGNED_IN, "https://www.facebook.com/marketplace/")
    assert _marketplace(page).is_logged_in()


def test_two_factor_screen_is_not_signed_in() -> None:
    """The cookie can already exist while the challenge is still open."""
    page = FakePage(SIGNED_IN, "https://www.facebook.com/two_step_verification/authentication/")
    assert not _marketplace(page).is_logged_in()


def test_login_wall_is_not_signed_in() -> None:
    page = FakePage(SIGNED_IN, "https://www.facebook.com/login/device-based/regular/login/")
    assert not _marketplace(page).is_logged_in()


def test_checkpoint_is_not_signed_in() -> None:
    """A restored-but-invalidated session still carries c_user."""
    page = FakePage(SIGNED_IN, "https://www.facebook.com/checkpoint/1234")
    assert not _marketplace(page).is_logged_in()


def test_without_the_session_cookie_is_not_signed_in() -> None:
    page = FakePage([{"name": "datr", "value": "x"}], "https://www.facebook.com/")
    assert not _marketplace(page).is_logged_in()


def test_empty_session_cookie_is_not_signed_in() -> None:
    page = FakePage([{"name": "c_user", "value": ""}], "https://www.facebook.com/")
    assert not _marketplace(page).is_logged_in()


def test_no_page_is_not_signed_in() -> None:
    assert not _marketplace(None).is_logged_in()


def test_unreadable_cookies_are_not_signed_in() -> None:
    """A closed browser must read as "not signed in", not blow up the search."""
    page = FakePage(SIGNED_IN, "https://www.facebook.com/", fail=True)
    assert not _marketplace(page).is_logged_in()


class SigningInPage(FakePage):
    """A page that completes its two-factor challenge after a few polls."""

    def __init__(self, clears_after: int) -> None:
        super().__init__([], "https://www.facebook.com/two_step_verification/authentication/")
        self.clears_after = clears_after
        self.polls = 0

    def cookies(self) -> List[Dict[str, Any]]:
        self.polls += 1
        if self.polls > self.clears_after:
            self._cookies = SIGNED_IN
            self.url = "https://www.facebook.com/marketplace/"
        return self._cookies


def test_await_login_returns_as_soon_as_the_session_is_live() -> None:
    """The wait ends on the signal, not on the clock."""
    page = SigningInPage(clears_after=1)
    marketplace = _marketplace(page)
    marketplace.logger = None
    marketplace.keyboard_monitor = None

    # Budget is 30s but the challenge clears on the second poll, so this must
    # return long before the budget expires.
    started = time.monotonic()
    assert marketplace._await_login(30) is True
    assert time.monotonic() - started < 15


def test_await_login_gives_up_when_the_challenge_never_clears() -> None:
    page = FakePage([], "https://www.facebook.com/two_step_verification/x")
    marketplace = _marketplace(page)
    marketplace.logger = None
    marketplace.keyboard_monitor = None
    assert marketplace._await_login(0) is False
