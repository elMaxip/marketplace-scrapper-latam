"""Which tabs a browser keeps, and when it has to let go of them.

A tab belongs to a *task*: a search, a round of re-checks, a session probe, the
one read of a tracked page.  While the task runs the tab is in use and nothing
here touches it.  When the task ends -- normally, by an exception, by a
cancellation, by a timeout -- the tab has no owner any more and is closed.

That rule is the whole module, and it is worth writing down because the code it
replaces had the opposite default.  A ``Marketplace`` took a tab on its first
search and held it for the life of the process, so what the browser showed
between rounds was the last thing each flow happened to look at: a Facebook
results page parked for the twenty-nine minutes until the next search, a listing
left over from a review that finished an hour ago, and -- the one that made it
obvious -- a Mercado Libre tab sitting on the account page, opened to ask whether
the session was still good and never given back.  On a server with a gigabyte to
spare, three abandoned tabs of a heavy site are not cosmetic.

Two things this deliberately does *not* do:

* It does not close everything that is not the first tab.  Lanes exist because
  several flows really do drive one browser each, and a sweep that ran while
  another task held a tab would close the page that task is reading.  Every
  sweep therefore names what to keep, and is called from the thread that owns
  the browser -- the same rule every other Playwright call in this codebase
  follows.
* It never closes the last tab.  A persistent context with no pages left is a
  browser that has closed itself, taking the profile lock and the session with
  it.  The last one is parked on ``about:blank`` instead, which costs nothing to
  hold and is what :meth:`Marketplace.create_page` claims on the way back in.
"""

from __future__ import annotations

from logging import Logger
from typing import Any, Iterable, List

#: What a parked tab shows.  Also what `create_page` looks for when it claims a
#: tab back, so the two halves have to agree on the string.
BLANK = "about:blank"

#: The URLs a tab shows when it is holding nothing.
IDLE_URLS = (BLANK, "")


def _pages_of(context: Any) -> List[Any]:
    """This browser's tabs, or nothing when it cannot be asked.

    A context whose process has died raises on ``pages`` rather than answering,
    and a browser we cannot ask about is one we must not act on.
    """
    if context is None:
        return []
    try:
        return list(context.pages)
    except Exception:
        return []


def park(page: Any, logger: Logger | None = None) -> bool:
    """Send one tab to ``about:blank``, dropping what it was holding.

    For the tab that may not be closed.  Navigating away is what actually frees
    the document, the images and the scripts -- a tab kept "just in case" on a
    results page is the memory this module exists to give back.
    """
    if page is None:
        return False
    try:
        if (page.url or "") in IDLE_URLS:
            return True
        page.goto(BLANK)
        return True
    except Exception:
        if logger:
            logger.debug("Could not park a tab on about:blank", exc_info=True)
        return False


def close_page(page: Any, context: Any, logger: Logger | None = None) -> bool:
    """Give one tab back.  True when it was closed rather than parked.

    Parked instead of closed when it is the only one left, for the reason in the
    module docstring: closing it would close the browser.
    """
    if page is None:
        return False
    pages = _pages_of(context)
    if len(pages) <= 1:
        park(page, logger)
        return False
    try:
        page.close()
        return True
    except Exception:
        # A tab that will not close is not worth failing a search over; the
        # sweep below will meet it again next time round.
        if logger:
            logger.debug("Could not close a tab", exc_info=True)
        return False


def close_idle_pages(
    context: Any, keep: Iterable[Any] = (), logger: Logger | None = None
) -> int:
    """Close every tab of this browser that no task is using.  Returns how many.

    ``keep`` is the tabs that still have an owner, compared by identity: two
    ``Page`` objects are never equal-but-distinct, and identity is the only
    comparison that survives a driver that overrides ``__eq__``.

    This is what catches the tabs no ``Marketplace`` has a name for -- a pop-up
    a site opened with ``target=_blank``, a tab left by a task that raised
    before it could tidy up -- and it is the reason the policy can be stated as
    "what is not in use is closed" rather than "each flow remembers to close its
    own".
    """
    kept = list(keep)
    pages = _pages_of(context)
    doomed = [page for page in pages if not any(page is other for other in kept)]
    closed = 0
    for index, page in enumerate(doomed):
        last_one = not kept and index == len(doomed) - 1
        if last_one:
            # Nothing else would be left: park it rather than take the browser
            # down with it.
            park(page, logger)
            continue
        try:
            page.close()
            closed += 1
        except Exception:
            if logger:
                logger.debug("Could not close an idle tab", exc_info=True)
    return closed
