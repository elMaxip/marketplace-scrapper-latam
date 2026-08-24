"""Running more than one kind of scraping at the same time.

The monitor has always done one thing at a time: search a product on a
platform, finish, search the next.  Two of the things it does have no reason to
wait for each other -- Mercado Libre does not care how far through Facebook we
are, and re-reading a listing already stored is not the same work as looking
for new ones -- so this module lets them run side by side.

Why a thread *and* a browser, and not just a thread:

Playwright's synchronous API is bound to the thread that created it.  Its
objects are driven by a greenlet loop belonging to that thread, and touching a
page from another one does not race, it fails outright.  So a second flow of
work cannot borrow the monitor's browser; it has to have its own
``sync_playwright()``, and therefore its own browser.

Why a browser *profile* of its own as well:

Chromium takes an exclusive lock on its user-data directory.  Two browsers
cannot share one profile, and pointing a second window at the first one's
directory fails to launch rather than sharing anything.  Each lane therefore
gets ``browser-profile-<lane>``, seeded from the same stored sessions the main
profile is seeded from -- so the second window arrives signed in, without a
second login.

What a lane is, then: a thread, a Playwright, a browser on its own profile, and
the marketplace objects bound to that browser.  Work is handed to it as a
callable and runs *on its thread*, which is the whole point -- the caller can
hand work to two lanes and then wait for both.

What a lane deliberately is not:

* It is not a general worker pool.  One lane runs one task at a time, in the
  order they were submitted, because the thing it owns -- a browser -- can only
  do one thing at a time anyway.
* It does not own any decision.  What to search, when to stop, whether the
  configuration moved: all of that stays with the monitor, and reaches the lane
  through the same process-wide flags every other checkpoint reads
  (:mod:`ai_marketplace_monitor.control`).  A lane that decided things for
  itself would be a second monitor.

Everything shared between lanes is already safe to share: the observation store
is SQLite behind ``diskcache`` transactions, the counters are the same cache,
the claim register and the marketplace cooldowns are in
:mod:`ai_marketplace_monitor.control` behind its lock.  The one thing that is
*not* thread-safe -- the ``schedule`` package's global job registry -- is never
touched from a lane; the monitor thread reads it, decides, and hands over the
work already chosen.
"""

from __future__ import annotations

import queue
import threading
from logging import Logger
from typing import Any, Callable, Dict, Optional

from playwright.sync_api import BrowserContext, Playwright, sync_playwright

from .marketplace import Marketplace


def context_is_alive(context: BrowserContext | None) -> bool:
    """Whether this browser still exists, as opposed to being remembered.

    Asked before every use, because a browser can go away without anyone
    telling us: the user closes the window, Chromium runs out of memory, the
    container is restarted under it.  The object stays perfectly valid Python
    afterwards, which is how the monitor came to report that it was reviewing
    listings while there was no browser on the screen at all.

    Two questions, because neither is sufficient on its own.  A persistent
    context (which is what a profile-based launch gives) reports its browser as
    ``None``, so ``is_connected`` cannot be reached through it; and ``pages``
    on a context whose process has died raises rather than answering.  Asking
    both, and treating any exception as "no", is the honest reading -- a
    browser we cannot ask about is one we must not use.
    """
    if context is None:
        return False
    try:
        browser = context.browser
        if browser is not None and not browser.is_connected():
            return False
        # Touching `pages` is what actually reaches the driver; a context that
        # has been closed raises TargetClosedError here.
        context.pages
        return True
    except Exception:
        return False

#: Handed to the lane so it can open its browser on its own thread.  Takes the
#: lane's own Playwright and its lane name (which chooses the profile) and
#: returns the context.  The monitor supplies it, because launch options,
#: proxies and profile seeding are its business, not this module's.
LaunchContext = Callable[[Playwright, str], BrowserContext]


class _Task:
    """One callable handed to a lane, and the answer coming back.

    A hand-rolled future rather than ``concurrent.futures``: an executor would
    bring its own threads, and the whole point of a lane is that the thread is
    not interchangeable -- it is the one thread that may touch this browser.
    """

    __slots__ = ("call", "done", "result", "error")

    def __init__(self: "_Task", call: Callable[[BrowserContext], Any]) -> None:
        self.call = call
        self.done = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None

    def wait(self: "_Task", timeout: float | None = None) -> Any:
        """Block until the task finishes, re-raising whatever it raised.

        The exception is raised in the *caller's* thread on purpose: a search
        abandoned by :class:`~ai_marketplace_monitor.control.CancelledScrape`
        has to unwind where the decision to wait was made, or the monitor would
        carry on believing a lane it started is still going.
        """
        if not self.done.wait(timeout):
            raise TimeoutError("The lane did not finish in time")
        if self.error is not None:
            raise self.error
        return self.result


class BrowserLane:
    """A thread with a browser of its own, running work handed to it.

    Not started by construction: a lane that has never been given anything to do
    should cost nothing, so the thread and the browser both wait for the first
    :meth:`submit`.
    """

    #: How long to wait for the browser to open before giving up on the lane.
    #: Generous, because a cold Chromium on a slow disk is not a failure.
    START_TIMEOUT = 120.0

    def __init__(
        self: "BrowserLane",
        name: str,
        launch: LaunchContext,
        logger: Logger | None = None,
    ) -> None:
        self.name = name
        self.logger = logger
        self._launch = launch
        self._queue: "queue.Queue[Optional[_Task]]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._context: BrowserContext | None = None
        self._ready = threading.Event()
        self._failure: BaseException | None = None
        self._lock = threading.Lock()
        #: Marketplace objects bound to *this* lane's browser.  A marketplace
        #: holds a page, and a page belongs to one context, so these can never
        #: be shared with another lane.
        self.marketplaces: Dict[str, Marketplace] = {}

    # ------------------------------------------------------------------ #
    # The thread
    # ------------------------------------------------------------------ #

    @property
    def alive(self: "BrowserLane") -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self: "BrowserLane") -> None:
        """The lane's whole life: open a browser, do as asked, close it."""
        playwright: Playwright | None = None
        try:
            playwright = sync_playwright().start()
            self._context = self._launch(playwright, self.name)
        except BaseException as error:  # noqa: BLE001 - reported to the caller
            self._failure = error
            self._ready.set()
            if self.logger:
                self.logger.error(
                    f"Could not open the browser for lane {self.name!r}: {error}",
                    exc_info=True,
                )
            # Drain whatever was queued so nobody waits for a lane that will
            # never answer.
            self._fail_pending(error)
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    pass
            return

        self._ready.set()
        try:
            while True:
                task = self._queue.get()
                if task is None:
                    return
                try:
                    context = self._live_context(playwright)
                    task.result = task.call(context)
                except BaseException as error:  # noqa: BLE001 - handed to the caller
                    task.error = error
                finally:
                    task.done.set()
        finally:
            self._teardown(playwright)

    def _live_context(self: "BrowserLane", playwright: Playwright) -> BrowserContext:
        """This lane's browser, opened again if it is no longer there.

        A lane outlives any one search, so between two of them its window can
        be closed by hand or its Chromium can die.  Before, the next task was
        handed the dead context and failed somewhere deep inside Playwright,
        which read as the *search* failing; the lane then kept its useless
        context and failed the search after that in the same way.

        Checked here rather than at submission time because this is the lane's
        own thread, and a Playwright object may only be touched by the thread
        that made it -- which is also why the replacement is opened here.
        """
        if context_is_alive(self._context):
            assert self._context is not None
            return self._context
        if self._context is not None and self.logger:
            self.logger.warning(
                f"The browser for lane {self.name!r} is gone; opening another one."
            )
        # The marketplaces held a page on the browser that has gone.  Dropping
        # them means the next search builds new ones against the new context
        # rather than driving tabs that no longer exist.
        for marketplace in list(self.marketplaces.values()):
            try:
                marketplace.stop()
            except Exception:
                pass
        self.marketplaces.clear()
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        self._context = self._launch(playwright, self.name)
        return self._context

    def _fail_pending(self: "BrowserLane", error: BaseException) -> None:
        while True:
            try:
                task = self._queue.get_nowait()
            except queue.Empty:
                return
            if task is None:
                return
            task.error = error
            task.done.set()

    def _teardown(self: "BrowserLane", playwright: Playwright | None) -> None:
        """Let go of every page and process this lane was holding.

        On the lane's own thread, always: these are Playwright objects, and the
        thread that made them is the only one allowed to close them.
        """
        for marketplace in list(self.marketplaces.values()):
            try:
                marketplace.stop()
            except Exception:
                if self.logger:
                    self.logger.debug(
                        f"Could not stop a marketplace on lane {self.name!r}", exc_info=True
                    )
        self.marketplaces.clear()
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                if self.logger:
                    self.logger.debug(f"Could not close lane {self.name!r}", exc_info=True)
            self._context = None
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                if self.logger:
                    self.logger.debug(f"Could not stop Playwright on lane {self.name!r}")

    def start(self: "BrowserLane") -> None:
        """Open the lane's browser, blocking until it is usable.

        Raises whatever the launch raised.  A lane that cannot open a browser is
        not a lane that quietly does nothing: the caller has work for it and
        needs to know it must do that work itself.
        """
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name=f"amm-lane-{self.name}", daemon=True
                )
                self._thread.start()
        if not self._ready.wait(self.START_TIMEOUT):
            raise TimeoutError(f"The browser for lane {self.name!r} did not open in time")
        if self._failure is not None:
            raise self._failure

    # ------------------------------------------------------------------ #
    # Handing it work
    # ------------------------------------------------------------------ #

    def submit(self: "BrowserLane", call: Callable[[BrowserContext], Any]) -> _Task:
        """Queue work and return at once; the answer comes back through the task."""
        self.start()
        task = _Task(call)
        self._queue.put(task)
        return task

    def run(self: "BrowserLane", call: Callable[[BrowserContext], Any]) -> Any:
        """Queue work and wait for it, re-raising whatever it raised."""
        return self.submit(call).wait()

    # ------------------------------------------------------------------ #
    # Shutting down
    # ------------------------------------------------------------------ #

    def close(self: "BrowserLane", timeout: float = 30.0) -> None:
        """Ask the lane to finish what it is doing and let go of its browser.

        Queued rather than forced: the sentinel goes behind whatever is already
        in the queue, so a task in flight ends at its own next safe point (a
        checkpoint reading the cancel flag) rather than under a closing browser.
        """
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is None or not thread.is_alive():
            return
        self._queue.put(None)
        thread.join(timeout)
        self._ready.clear()
        self._failure = None
