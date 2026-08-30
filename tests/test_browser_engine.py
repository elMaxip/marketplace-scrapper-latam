"""Choosing the driver and the browser, and what that choice switches off.

Both questions are installation facts -- is patchright here, is Chrome here --
and the thing worth pinning is that neither answer may stop the monitor working.
A container has no Chrome and no patchright, and it has to behave exactly as it
did before either existed.
"""

from __future__ import annotations

import pytest

from ai_marketplace_monitor import browser_engine
from ai_marketplace_monitor.monitor import (
    SOFTWARE_WEBGL_FLAG,
    TELLTALE_DEFAULT_ARGS,
    MarketplaceMonitor,
)


def test_there_is_always_a_driver() -> None:
    assert browser_engine.ENGINE_NAME in ("patchright", "playwright")
    assert callable(browser_engine.sync_playwright)


def test_only_a_patched_driver_claims_to_patch() -> None:
    """`PATCHES_CDP` switches off this codebase's own stealth hacks, so it must
    never be true for plain Playwright -- that would silently drop the
    `navigator.webdriver` override the bundled engine still needs."""
    assert browser_engine.PATCHES_CDP is (browser_engine.ENGINE_NAME == "patchright")


def test_the_chrome_answer_is_cached() -> None:
    """Asked on every browser launch, including every lane's, about a filesystem
    that cannot change mid-process."""
    first = browser_engine.chrome_is_installed()
    assert browser_engine.chrome_is_installed() is first
    assert isinstance(first, bool)


def test_a_machine_without_chrome_falls_back_rather_than_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The container has no Chrome in it, and that must not be why it stops
    starting.  `None` is Playwright's own build, which is what ran before."""
    monkeypatch.setattr(browser_engine, "chrome_is_installed", lambda: False)
    import ai_marketplace_monitor.monitor as monitor_module

    monkeypatch.setattr(monitor_module, "chrome_is_installed", lambda: False)
    instance = MarketplaceMonitor.__new__(MarketplaceMonitor)
    assert instance._browser_channel("chromium") is None


def test_real_chrome_is_used_when_it_is_there(monkeypatch: pytest.MonkeyPatch) -> None:
    import ai_marketplace_monitor.monitor as monitor_module

    monkeypatch.setattr(monitor_module, "chrome_is_installed", lambda: True)
    instance = MarketplaceMonitor.__new__(MarketplaceMonitor)
    assert instance._browser_channel("chromium") == "chrome"


def test_only_chromium_takes_a_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Firefox and WebKit have no Chrome to be."""
    import ai_marketplace_monitor.monitor as monitor_module

    monkeypatch.setattr(monitor_module, "chrome_is_installed", lambda: True)
    instance = MarketplaceMonitor.__new__(MarketplaceMonitor)
    assert instance._browser_channel("firefox") is None
    assert instance._browser_channel("webkit") is None


def test_the_automation_flag_is_still_dropped() -> None:
    options = MarketplaceMonitor._launch_options("chromium")
    assert "--enable-automation" in options["ignore_default_args"]
    assert "--disable-blink-features=AutomationControlled" in options["args"]


def test_the_load_bearing_flags_are_kept() -> None:
    """A subset on purpose.  `--disable-dev-shm-usage` is, in a container, the
    difference between working and dying on the first page; dropping the sandbox
    and GPU flags trades a fingerprint for a browser that falls over."""
    for flag in ("--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"):
        assert flag not in TELLTALE_DEFAULT_ARGS


def test_the_housekeeping_flags_are_dropped() -> None:
    """What a real browser does and an automated one is told not to.  A page
    cannot read the command line but it can read what these flags do."""
    for flag in ("--disable-extensions", "--disable-component-update", "--no-first-run"):
        assert flag in TELLTALE_DEFAULT_ARGS


def test_software_webgl_is_allowed() -> None:
    """Measured in the container: patchright's Chromium returns *no* WebGL
    context without this, and a browser with no WebGL is a stronger tell than
    one rendering in software.  It permits the fallback rather than forcing it,
    so a machine with a GPU is unaffected."""
    options = MarketplaceMonitor._launch_options("chromium")
    assert SOFTWARE_WEBGL_FLAG in options["args"]
    # Not something to "clean up" into the dropped list: those are flags a
    # person's Chrome never carries, and this one exists to make this browser
    # behave more like one, not less.
    assert SOFTWARE_WEBGL_FLAG not in TELLTALE_DEFAULT_ARGS


def test_other_engines_take_no_chromium_arguments() -> None:
    """Firefox and WebKit reject them outright."""
    assert MarketplaceMonitor._launch_options("firefox") == {}
    assert MarketplaceMonitor._launch_options("webkit") == {}


# --------------------------------------------------------------------------- #
# Every platform answers the bot-check question
# --------------------------------------------------------------------------- #
#
# Empty is an answer, not an omission, and the difference is the point: a device
# cookie the site merely *knows* is an asset (Facebook's `datr`, which
# `save_device_state` goes out of its way to keep), and one the site has ruled
# *against* is a liability.  A platform added later that forgets to think about
# this gets the safe default -- but the ones that exist should have decided.


def test_every_platform_declares_the_attribute() -> None:
    """So adding a platform is "name the cookies", never "find the mechanism"."""
    from ai_marketplace_monitor.config import supported_marketplaces

    for name, cls in supported_marketplaces.items():
        assert isinstance(cls.challenge_cookies, tuple), name


def test_the_shops_name_their_walls_vendor() -> None:
    from ai_marketplace_monitor.lider import LiderMarketplace
    from ai_marketplace_monitor.sodimac import SodimacMarketplace

    assert "_pxvid" in LiderMarketplace.challenge_cookies
    assert "__cf_bm" in SodimacMarketplace.challenge_cookies


def test_facebooks_device_cookies_are_never_dropped() -> None:
    """The opposite sign.  `save_device_state` keeps `datr` on a *failed* login
    precisely so the site sees one device retrying rather than a stream of new
    ones; listing it here would recreate the challenge loop that exists to
    break."""
    from ai_marketplace_monitor.facebook import FacebookMarketplace
    from ai_marketplace_monitor.session import DEVICE_COOKIES

    assert FacebookMarketplace.challenge_cookies == ()
    assert not (set(FacebookMarketplace.challenge_cookies) & DEVICE_COOKIES)


def test_discarding_is_a_no_op_where_there_is_nothing_to_discard() -> None:
    """Mercado Libre's wall is its own and issues no clearance token."""
    from ai_marketplace_monitor.mercadolibre import MercadoLibreMarketplace

    market = MercadoLibreMarketplace("mercadolibre", None)
    assert MercadoLibreMarketplace.challenge_cookies == ()
    market.discard_challenge_state()  # must not reach the session file at all
