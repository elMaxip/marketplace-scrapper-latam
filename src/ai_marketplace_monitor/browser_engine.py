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
from pathlib import Path
from typing import Any, Callable

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
