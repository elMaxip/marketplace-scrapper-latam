"""Which Playwright drives the browsers, and which browser it drives.

Two questions that used to be answered by an import line and a default, and both
turned out to matter to whether Lider serves us a page.

Why there is a choice of driver at all
--------------------------------------

Lider is behind PerimeterX and refuses this scraper about half the time.  The
obvious tells were dealt with long ago -- ``--enable-automation`` is off,
``navigator.webdriver`` is cleared -- and the refusals continued, which points at
the tell that is *not* a flag: Playwright drives Chromium over CDP, and enabling
the ``Runtime`` domain to evaluate scripts leaves traces a page can read.  That
is a property of the driver, and no amount of launch options fixes it.

`patchright <https://pypi.org/project/patchright/>`_ is a fork of Playwright that
does fix it, by running injected scripts in isolated execution contexts instead.
Its API is Playwright's, so nothing above this module has to know which one it
got.

**Soft, not a swap.**  The import falls back to Playwright when patchright is not
installed, so the container, CI and a checkout that never ran ``pip install``
behave exactly as before.  That keeps the new dependency additive -- uninstall it
and the monitor goes back to what it was, with no code change -- which is the
only way a dependency earns its place under this project's rule about not adding
them.

Two things patchright asks of its callers, and both cost code elsewhere:

* **No custom user agent and no extra headers.**  ``_hide_headless_marker`` does
  exactly that and has to stand down; see :data:`PATCHES_CDP`.
* **No ``add_init_script`` for stealth.**  The ``navigator.webdriver`` override
  is redundant under patchright and an injected script is itself a thing to
  notice.

It also only patches Chromium.  The Firefox and WebKit fallbacks still exist and
are still worth having -- a monitor that cannot open any browser is worse than
one running an unpatched engine -- but they are a last resort and the log says
so rather than pretending the choice was free.

Why there is a choice of browser
--------------------------------

Playwright's Chromium is not Chrome.  It ships without the proprietary codecs and
Widevine, its ``navigator.userAgentData`` brands differ, and its build strings
differ.  None of that matters to reading a ``__NEXT_DATA__`` payload and all of
it is free surface for whoever is deciding whether we are a person.  Where the
machine has real Chrome, use it; where it does not -- the container -- carry on
with the bundled build rather than refusing to start.
"""

from __future__ import annotations

import os
import shutil
import sys
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

#: The driver actually in use, for the log and for the two behaviour switches.
ENGINE_NAME: str
#: Whether that driver closes the CDP leaks by itself, which is what decides
#: whether this codebase should still be doing it by hand.
PATCHES_CDP: bool

try:  # pragma: no cover - which branch runs is an installation fact
    from patchright.sync_api import sync_playwright as _sync_playwright  # type: ignore

    ENGINE_NAME = "patchright"
    PATCHES_CDP = True
except ImportError:  # pragma: no cover
    from playwright.sync_api import sync_playwright as _sync_playwright  # type: ignore

    ENGINE_NAME = "playwright"
    PATCHES_CDP = False

sync_playwright: Callable[[], Any] = _sync_playwright


def _api_types(name: str) -> Dict[str, type]:
    """The handle classes of one driver, or nothing when it is not installed."""
    try:
        module = import_module(f"{name}.sync_api")
    except ImportError:  # pragma: no cover - an installation fact
        return {}
    return {
        attribute: getattr(module, attribute)
        for attribute in ("Locator", "ElementHandle")
        if isinstance(getattr(module, attribute, None), type)
    }


_ENGINE_TYPES = [_api_types("patchright"), _api_types("playwright")]

#: Every class a driver may hand us for a locator, and for an element handle.
#:
#: Both drivers, not the one in use, and this is the whole point.  patchright is
#: a *fork*: its ``Locator`` is a different class object from Playwright's, so
#: ``isinstance(element, playwright.sync_api.Locator)`` is False for every
#: locator the monitor actually holds when the stealth extra is installed --
#: which is the case in the container.  The code that converts a locator to an
#: element handle read that False as "this is already a handle", kept the
#: locator, and called ``query_selector_all`` on it:
#:
#:     get_condition failed: AttributeError: 'Locator' object has no attribute
#:     'query_selector_all'
#:
#: Intermittent, because only the layouts that climb the DOM from a locator
#: reach it -- a listing read through ``page.query_selector`` was fine, which is
#: why some listings parsed and others lost their condition and location.
#:
#: Named as tuples rather than fixed by an import, so the next fork costs one
#: line here instead of a bug per call site.
LOCATOR_TYPES: Tuple[type, ...] = tuple(
    types["Locator"] for types in _ENGINE_TYPES if "Locator" in types
)
ELEMENT_HANDLE_TYPES: Tuple[type, ...] = tuple(
    types["ElementHandle"] for types in _ENGINE_TYPES if "ElementHandle" in types
)


def as_element_handle(element: Any) -> Any:
    """The element handle behind ``element``, whichever driver made it.

    A locator is a *query*, re-run on every use; an element handle is a node.
    Only the second can be walked with ``query_selector_all``, so everything
    that climbs the DOM resolves the locator here first.

    Unknown objects are returned untouched: the test fakes in ``tests/`` are
    neither driver's classes and are already node-shaped, and refusing them
    would mean the DOM walking could only be tested through a browser.  What
    must not happen is the reverse -- a *real* locator passed through as if it
    were a node -- and the tuple above is what stops that.
    """
    if element is None:
        return None
    if LOCATOR_TYPES and isinstance(element, LOCATOR_TYPES):
        return element.element_handle()
    # Duck-typing after the type test, not instead of it: a driver this build
    # has never heard of still answers to `element_handle`, and a handle does
    # not have the attribute at all.
    if not isinstance(element, ELEMENT_HANDLE_TYPES or ()) and hasattr(
        element, "element_handle"
    ):
        return element.element_handle()
    return element


def restart_driver(playwright: Any, start: Callable[[], Any], logger: Any = None) -> Any:
    """Stop this driver process and start another.  Returns the new one.

    A ``Playwright`` object is a connection to a *node process*, and that
    process can die: a container running out of memory, a crash, a Docker
    Desktop hiccup.  Nothing about the Python object changes when it does --
    and the next call that has to reach the driver behaves in one of two ways,
    which is what makes this worth a function of its own:

    * a call on an object that already exists (``context.close()``,
      ``context.pages``) raises **at once**, with "Connection closed while
      reading from the driver";
    * a call that has to *create* something -- above all
      ``launch_persistent_context`` -- **blocks for ever**.  Its ``timeout`` is
      enforced by the driver, so a missing driver is a missing timeout, and the
      scraping thread is a synchronous Playwright thread: nothing else can
      interrupt it.  Measured, not inferred: the monitor sat on "Attempting to
      launch chromium browser..." with no further line for as long as it was
      left.

    So a driver is never reused across a browser that has gone.  Stopping a
    dead one is free (it returns immediately rather than raising or hanging)
    and starting a fresh one costs about half a second, which is nothing beside
    a monitor that has stopped searching and does not say so.

    Must be called on the thread that owns the driver -- the same rule as every
    other Playwright call here.

    ``start`` rather than :data:`sync_playwright` directly, because the caller
    is the one that knows how it made its driver in the first place, and a
    replacement made some other way is not a replacement.
    """
    if playwright is not None:
        try:
            playwright.stop()
        except Exception:
            # A driver that is already gone cannot be stopped, and does not need
            # to be.  What matters is that the reference is dropped.
            if logger is not None:
                logger.debug("Could not stop the Playwright driver", exc_info=True)
    return start()


#: Where Google Chrome installs itself, per platform.
#:
#: Checked directly rather than asked of Playwright: ``channel="chrome"`` fails
#: at launch when Chrome is absent, and the launch is the one place where a
#: recoverable "use the other browser" must not become a crash.
_CHROME_PATHS = {
    "win32": (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ),
    "darwin": ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",),
    "linux": (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/opt/google/chrome/chrome",
    ),
}

#: Names to try on PATH when none of the well-known locations has it.
_CHROME_COMMANDS = ("google-chrome", "google-chrome-stable", "chrome")


def chrome_is_installed() -> bool:
    """Whether real Google Chrome is on this machine.

    Answered once and cached: it is asked on every browser launch, including
    every lane's, and it is a question about the filesystem that cannot change
    while the process runs.
    """
    cached = getattr(chrome_is_installed, "_answer", None)
    if cached is None:
        cached = _find_chrome()
        setattr(chrome_is_installed, "_answer", cached)
    return cached


def _find_chrome() -> bool:
    for candidate in _CHROME_PATHS.get(sys.platform, ()):
        if candidate and Path(candidate).exists():
            return True
    return any(shutil.which(name) for name in _CHROME_COMMANDS)
