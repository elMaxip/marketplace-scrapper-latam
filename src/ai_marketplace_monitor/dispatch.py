"""Sending notifications without making the scraper wait for them.

The monitor used to notify at one moment only: the end of a search, with every
listing it had found.  That is the right *message* -- six listings in one
notification rather than six notifications -- and the wrong *moment* for the
thing being monitored.  A Facebook pass over a handful of products is the
better part of an hour, and the listing that made the search worth running was
found in the first two minutes of it.  On a marketplace where a well-priced
console is gone within ten, fifty-odd minutes of silence is the difference
between a monitor and a diary.

So there is now a choice (``[monitor] notify_immediately``), and this module is
what makes the immediate half of it safe.

Why it cannot simply call ``User.notify`` inline
------------------------------------------------

Because notification channels block, for as long as their service feels like
it.  Telegram rate-limits itself to one message a second per chat and three a
second to a group, waits out an HTTP 429 for however many seconds the server
asks for, and retries with exponential backoff; SMTP opens a TLS connection and
waits for a handshake; Pushbullet imports a library that loads libmagic.  Any
of that on the scraping thread is a page left sitting open, a search that takes
longer than it should, and -- because the checkpoints that read the pause and
cancel flags are in the scraping code -- a "detener" button that does nothing
until the notification finishes.  A listing being *found* must not depend on
the last listing having been *delivered*.

So sending moves to a thread of its own with a queue in front of it, and the
scraping thread's part is one ``put``.

What this deliberately is not
-----------------------------

* Not a pool.  One worker, so the order listings were found in is the order
  they are sent in, and so two threads never race to write the same "already
  notified" cache entry for the same listing.
* Not a retry queue.  The channels already retry; a failure that survives them
  is logged and dropped, because a notification about a listing from an hour
  ago is not worth the machinery to redeliver it.
* Not fire-and-forget at shutdown.  :meth:`close` drains what is queued, so
  stopping the monitor a second after a listing was found still sends it.

The work itself is handed over as a callable.  This module knows nothing about
users, cards or channels -- it knows about threads -- and keeping it that way
is what makes it testable without a Telegram token.
"""

from __future__ import annotations

import queue
import threading
import time
from logging import Logger
from typing import Callable, Optional

#: How long :meth:`NotificationDispatcher.close` waits for the queue to empty.
#: Long enough for a channel that is being rate-limited to finish what it
#: started, short enough that Ctrl-C still feels like Ctrl-C.
DEFAULT_DRAIN_TIMEOUT = 30.0


class NotificationDispatcher:
    """A single worker thread that sends whatever is handed to it.

    Not started by construction: a monitor configured to notify at the end of
    each search never submits anything, and should not pay a thread for it.
    """

    def __init__(
        self: "NotificationDispatcher",
        logger: Logger | None = None,
        name: str = "amm-notify",
    ) -> None:
        self.logger = logger
        self.name = name
        self._queue: "queue.Queue[Optional[Callable[[], None]]]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        #: Counted rather than derived from the queue: a task that has been
        #: taken off the queue and is being sent is still pending, and
        #: ``Queue.unfinished_tasks`` is private in spirit if not in name.
        self._pending = 0
        self._idle = threading.Event()
        self._idle.set()
        self._closed = False

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #

    @property
    def pending(self: "NotificationDispatcher") -> int:
        """How many sends are queued or in flight."""
        with self._lock:
            return self._pending

    @property
    def alive(self: "NotificationDispatcher") -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ #
    # Handing it work
    # ------------------------------------------------------------------ #

    def submit(self: "NotificationDispatcher", send: Callable[[], None]) -> bool:
        """Queue one send and return at once.  False if the dispatcher is closed.

        The return value matters to the caller: a monitor shutting down must
        fall back to sending inline rather than believing a notification was
        queued that never will be.
        """
        with self._lock:
            if self._closed:
                return False
            self._pending += 1
            self._idle.clear()
            self._start_locked()
        self._queue.put(send)
        return True

    def _start_locked(self: "NotificationDispatcher") -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def _run(self: "NotificationDispatcher") -> None:
        while True:
            send = self._queue.get()
            if send is None:
                return
            try:
                send()
            except BaseException as error:  # noqa: BLE001 - a channel must not kill the thread
                # Logged and dropped.  The channels have already retried; what
                # is left is a service that is down, and holding the listing
                # for it would silently stop every later notification behind
                # a queue nobody is watching.
                if self.logger:
                    self.logger.error(
                        f"A notification could not be sent: {error}", exc_info=True
                    )
            finally:
                with self._lock:
                    self._pending -= 1
                    if self._pending <= 0:
                        self._pending = 0
                        self._idle.set()

    # ------------------------------------------------------------------ #
    # Shutting down
    # ------------------------------------------------------------------ #

    def drain(self: "NotificationDispatcher", timeout: float = DEFAULT_DRAIN_TIMEOUT) -> bool:
        """Wait until nothing is queued or in flight.  False on timing out.

        Used where the caller genuinely has to know the messages went out --
        the end of a run, a test -- and nowhere in the scraping path, which is
        the entire point of this class.
        """
        return self._idle.wait(timeout)

    def close(
        self: "NotificationDispatcher", timeout: float = DEFAULT_DRAIN_TIMEOUT
    ) -> None:
        """Send what is queued, then let the thread go.

        The sentinel goes *behind* the queue rather than jumping it, so a
        listing found a second before the monitor was stopped is still
        delivered.  Accepting nothing new from the moment this is called is
        what stops that queue growing while it is being drained.
        """
        with self._lock:
            if self._closed:
                thread = self._thread
            else:
                self._closed = True
                thread = self._thread
        if thread is None or not thread.is_alive():
            return
        deadline = time.monotonic() + timeout
        self._queue.put(None)
        thread.join(max(0.0, deadline - time.monotonic()))
        if thread.is_alive() and self.logger:
            self.logger.warning(
                "A notification was still being sent when the monitor stopped; "
                "it was left to finish on its own."
            )
        with self._lock:
            self._thread = None
