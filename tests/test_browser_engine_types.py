"""The driver's own classes, and the bug that came of assuming there was one.

    get_condition failed: AttributeError: 'Locator' object has no attribute
    'query_selector_all'

``_parent_with_cond`` walks up the DOM, which only an *element handle* can do: a
locator is a query, re-run on every use, and has no ``query_selector_all`` at
all.  So it resolved a locator to a handle first -- guarded by
``isinstance(element, playwright.sync_api.Locator)``.

patchright is a fork, not a plugin.  ``patchright.sync_api.Locator`` is a
different class object, so that test was False for every locator the monitor
actually held once the stealth extra was installed, which it is in the
container.  False meant "already a handle", the locator went through untouched,
and the next line asked a query for its children.

Intermittent in exactly the way the report said: only the code paths that start
from a locator reach it, so a listing whose layout was read through
``page.query_selector`` parsed perfectly and the one beside it lost its
condition and its location.

The tests here are the two halves of that.  The cheap half pins the invariant --
*every installed driver's classes are known to us* -- and the expensive half
drives a real browser through the real walk, which is the only place the two
class families actually meet.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any, List

import pytest

from ai_marketplace_monitor import browser_engine
from ai_marketplace_monitor.browser_engine import (
    ELEMENT_HANDLE_TYPES,
    LOCATOR_TYPES,
    as_element_handle,
)
from ai_marketplace_monitor.marketplace import WebPage

INSTALLED = [
    name
    for name in ("playwright", "patchright")
    if getattr(import_module(name), "__name__", None)
]


# --------------------------------------------------------------------------- #
# What must be true of every driver on the machine
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("engine", INSTALLED)
def test_every_installed_driver_is_known(engine: str) -> None:
    """The invariant the bug broke, stated once.

    Not "the driver in use": both, always.  A build where only the active
    driver's classes were listed would pass every test and fail the moment the
    other one is installed beside it -- which is precisely what happened.
    """
    api = import_module(f"{engine}.sync_api")
    assert api.Locator in LOCATOR_TYPES
    assert api.ElementHandle in ELEMENT_HANDLE_TYPES


@pytest.mark.parametrize("engine", INSTALLED)
def test_a_locator_cannot_be_walked(engine: str) -> None:
    """Why the resolution has to happen at all, pinned rather than assumed.

    If a locator ever grows ``query_selector_all`` this test fails, and the
    failure is the news: the whole conversion would then be optional.
    """
    api = import_module(f"{engine}.sync_api")
    assert not hasattr(api.Locator, "query_selector_all")
    assert hasattr(api.ElementHandle, "query_selector_all")


def test_the_two_drivers_really_are_different_classes() -> None:
    """The premise.  Without this, the fix is solving nothing."""
    if len(INSTALLED) < 2:
        pytest.skip("only one driver installed")
    playwright_api = import_module("playwright.sync_api")
    patchright_api = import_module("patchright.sync_api")
    assert playwright_api.Locator is not patchright_api.Locator
    assert len(LOCATOR_TYPES) >= 2


# --------------------------------------------------------------------------- #
# `as_element_handle` on things that are not a driver's
# --------------------------------------------------------------------------- #


class FakeNode:
    """A node-shaped thing: it can be asked for its children."""

    def __init__(self: "FakeNode", text: str = "", children: List["FakeNode"] | None = None):
        self.text = text
        self.children = children or []
        self.parent: "FakeNode" | None = None
        for child in self.children:
            child.parent = self

    def query_selector_all(self: "FakeNode", selector: str) -> List["FakeNode"]:
        assert selector == ":scope > *"
        return list(self.children)

    def query_selector(self: "FakeNode", selector: str) -> "FakeNode | None":
        assert selector == "xpath=.."
        return self.parent

    def text_content(self: "FakeNode") -> str:
        return self.text


class FakeLocator:
    """A query-shaped thing: it can only be *resolved*, not walked."""

    def __init__(self: "FakeLocator", node: FakeNode) -> None:
        self.node = node
        self.resolved = 0

    def element_handle(self: "FakeLocator") -> FakeNode:
        self.resolved += 1
        return self.node


def test_none_stays_none() -> None:
    assert as_element_handle(None) is None


def test_a_node_is_left_alone() -> None:
    """The fakes in this suite are node-shaped and belong to no driver; walking
    them without a browser is the only way the DOM logic is testable at all."""
    node = FakeNode("x")
    assert as_element_handle(node) is node


def test_anything_that_answers_to_element_handle_is_resolved() -> None:
    """The duck-typed fallback: a driver this build has never heard of."""
    node = FakeNode("x")
    locator = FakeLocator(node)
    assert as_element_handle(locator) is node
    assert locator.resolved == 1


# --------------------------------------------------------------------------- #
# The walk itself
# --------------------------------------------------------------------------- #


def _page(node: Any) -> WebPage:
    return WebPage(page=node)  # type: ignore[arg-type]


def test_the_walk_starts_from_a_locator() -> None:
    """`get_condition` hands `_parent_with_cond` a locator, not a handle.

    This is the call shape that produced the AttributeError, minus the browser.
    """
    label = FakeNode("Condition")
    value = FakeNode("Usado - como nuevo")
    FakeNode("row", [label, value])
    answer = _page(None)._parent_with_cond(
        FakeLocator(label),  # type: ignore[arg-type]
        lambda children: len(children) >= 2 and "Condition" in (children[0].text_content() or ""),
        1,
    )
    assert answer == "Usado - como nuevo"


def test_the_walk_climbs_until_the_condition_matches() -> None:
    label = FakeNode("Condition")
    value = FakeNode("Nuevo")
    row = FakeNode("row", [label, value])
    FakeNode("section", [row])
    answer = _page(None)._parent_with_cond(
        FakeLocator(label),  # type: ignore[arg-type]
        lambda children: len(children) == 2,
        1,
    )
    assert answer == "Nuevo"


# --------------------------------------------------------------------------- #
# The real thing
# --------------------------------------------------------------------------- #
#
# A browser, because the whole bug lives in the gap between two class families
# and no fake can stand in for that: a fake locator resolved correctly under the
# broken code too.

CONDITION_PAGE = """
<html><body>
  <div><ul><li><span>Condition</span><span>Used - like new</span></li></ul></div>
  <p>The condition of the paint is described below in this Condition report.</p>
</body></html>
"""


@pytest.fixture(scope="module")
def browser_page() -> Any:
    playwright = browser_engine.sync_playwright()
    driver = playwright.start()
    try:
        browser = driver.chromium.launch()
    except Exception as error:  # pragma: no cover - no browser binary here
        driver.stop()
        pytest.skip(f"no browser for {browser_engine.ENGINE_NAME}: {error}")
    page = browser.new_page()
    page.set_content(CONDITION_PAGE)
    yield page
    browser.close()
    driver.stop()


def test_a_real_locator_reaches_the_condition(browser_page: Any) -> None:
    """The regression, end to end, on whichever driver this machine has.

    Before the fix this raised ``AttributeError: 'Locator' object has no
    attribute 'query_selector_all'`` under patchright and passed under
    Playwright -- which is why it was reported as intermittent rather than as
    broken.
    """
    page = WebPage(page=browser_page)
    # `.first` exactly as `FacebookRegularItemPage.get_condition` does it: the
    # word appears twice on the page, and strict mode would refuse the locator.
    locator = browser_page.locator('span:text("Condition")').first
    answer = page._parent_with_cond(
        locator,
        lambda children: len(children) >= 2 and "Condition" in (children[0].text_content() or ""),
        1,
    )
    assert answer == "Used - like new"


def test_a_real_locator_is_resolved_to_a_handle(browser_page: Any) -> None:
    """The single line the fix is: what comes back can be walked."""
    locator = browser_page.locator("li").first
    handle = as_element_handle(locator)
    assert isinstance(handle, ELEMENT_HANDLE_TYPES)
    assert len(handle.query_selector_all(":scope > *")) == 2
