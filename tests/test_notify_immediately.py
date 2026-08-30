"""Telling the user about a listing the moment it passes, rather than at the end.

Two separate claims, and both of them matter:

*When.*  With ``[monitor] notify_immediately`` on, a listing that has passed
every filter and been scored is notified there and then -- not fifty minutes
later when the platform has finished being searched.  With it off, nothing
changes: one message about everything the search found, which is what the
monitor has always done and what most people want.

*Not on the scraping thread.*  A notification channel blocks for as long as its
service feels like it -- Telegram waits out a 429, SMTP waits for a handshake
-- and the checkpoints that read the pause and cancel flags live in the
scraping code.  Sending inline would mean a page left open and a "detener"
button that does not answer.  So the scraper's part is a ``put`` on a queue,
and these tests are mostly about proving that the ``put`` is all it is.
"""

from __future__ import annotations

import threading
import time

import pytest

from ai_marketplace_monitor.dispatch import NotificationDispatcher


@pytest.fixture
def dispatcher():
    made = NotificationDispatcher()
    yield made
    made.close(timeout=5)


# --------------------------------------------------------------------------- #
# The dispatcher
# --------------------------------------------------------------------------- #


def test_nothing_is_started_until_there_is_something_to_send(dispatcher):
    """A monitor that notifies at the end of each search pays no thread for it."""
    assert dispatcher.alive is False
    assert dispatcher.pending == 0


def test_submitting_does_not_wait_for_the_send(dispatcher):
    """The whole point: the scraper hands the work over and carries on.

    The send here takes half a second, which is a *short* Telegram message;
    `submit` has to come back in a fraction of that or the next listing is not
    being parsed while this one is in flight.
    """
    started = threading.Event()

    def slow() -> None:
        started.set()
        time.sleep(0.5)

    before = time.monotonic()
    assert dispatcher.submit(slow) is True
    elapsed = time.monotonic() - before

    assert elapsed < 0.1
    assert started.wait(2)
    assert dispatcher.drain(5) is True


def test_sends_happen_on_another_thread(dispatcher):
    where: list[str] = []
    dispatcher.submit(lambda: where.append(threading.current_thread().name))
    assert dispatcher.drain(5)
    assert where and where[0] != threading.current_thread().name


def test_order_is_kept(dispatcher):
    """One worker, so listings are told in the order they were found."""
    seen: list[int] = []
    for index in range(20):
        dispatcher.submit(lambda index=index: seen.append(index))
    assert dispatcher.drain(5)
    assert seen == list(range(20))


def test_a_channel_that_raises_does_not_kill_the_sender(dispatcher):
    """A service being down must not stop every later notification."""
    seen: list[str] = []

    def boom() -> None:
        raise RuntimeError("telegram is having a bad day")

    dispatcher.submit(boom)
    dispatcher.submit(lambda: seen.append("after"))
    assert dispatcher.drain(5)
    assert seen == ["after"]


def test_closing_sends_what_is_still_queued(dispatcher):
    """A listing found a second before the monitor stopped is still sent."""
    sent: list[int] = []
    for index in range(5):
        dispatcher.submit(lambda index=index: (time.sleep(0.02), sent.append(index)))
    dispatcher.close(timeout=10)
    assert sent == list(range(5))


def test_a_closed_dispatcher_refuses_rather_than_swallowing():
    """False, so the caller can send inline instead of losing the message."""
    closed = NotificationDispatcher()
    closed.submit(lambda: None)
    closed.close(timeout=5)
    assert closed.submit(lambda: None) is False


def test_pending_counts_what_is_in_flight_as_well_as_what_is_queued(dispatcher):
    release = threading.Event()
    dispatcher.submit(release.wait)
    # Taken off the queue and running is still pending: an idle-browser check
    # that read only the queue would close a browser mid-notification.
    time.sleep(0.1)
    assert dispatcher.pending == 1
    release.set()
    assert dispatcher.drain(5)
    assert dispatcher.pending == 0


# --------------------------------------------------------------------------- #
# Which moment the monitor uses
# --------------------------------------------------------------------------- #


def _monitor(notify_immediately: bool, users: dict):
    """Just enough monitor to exercise `_notify` and the two moments."""
    from ai_marketplace_monitor.monitor import MarketplaceMonitor

    instance = MarketplaceMonitor.__new__(MarketplaceMonitor)
    instance.logger = None
    instance.notifier = NotificationDispatcher()

    class _Cfg:
        pass

    config = _Cfg()
    config.monitor = _Cfg()
    config.monitor.notify_immediately = notify_immediately
    config.monitor.max_description_words = None
    config.user = users
    # Every real config has one, and `_notify` reads it to work out what
    # ``{item}`` should say -- a group, for a tracker that is in one.  Empty
    # here because none of these scenarios has a tracker in it.
    config.items = {}
    instance.config = config
    return instance


def test_the_setting_is_off_unless_it_is_asked_for():
    assert _monitor(False, {})._notifies_immediately() is False
    assert _monitor(True, {})._notifies_immediately() is True


def test_the_description_limit_defaults_to_twenty_five_words():
    assert _monitor(False, {})._description_words() == 25


def test_a_configured_description_limit_wins():
    monitor = _monitor(False, {})
    monitor.config.monitor.max_description_words = 2
    assert monitor._description_words() == 2


def test_no_such_user_is_not_a_notification(monkeypatch):
    """A search naming a user the file no longer has sends nothing at all."""
    monitor = _monitor(True, {})
    monitor._notify(["ghost"], [object()], [object()], object(), None, None, True)
    assert monitor.notifier.pending == 0
    assert monitor.notifier.alive is False


def test_notifying_goes_through_the_dispatcher(monkeypatch):
    monitor = _monitor(True, {"me": object()})
    sent: list[tuple] = []

    class FakeUser:
        def __init__(self, config, logger=None):
            self.config = config

        def notify(self, listings, ratings, item_config, **kwargs):
            sent.append((listings, kwargs))

    monkeypatch.setattr("ai_marketplace_monitor.monitor.User", FakeUser)
    monitor._notify(["me"], ["listing"], ["rating"], object(), "es", "Mercado Libre", True)
    assert monitor.notifier.drain(5)
    assert len(sent) == 1
    listings, kwargs = sent[0]
    assert listings == ["listing"]
    assert kwargs["language"] == "es"
    assert kwargs["marketplace_label"] == "Mercado Libre"
    assert kwargs["description_words"] == 25
    monitor.notifier.close(timeout=5)


def test_a_closed_dispatcher_still_notifies(monkeypatch):
    """The fallback: better a slow shutdown than a message quietly dropped."""
    monitor = _monitor(True, {"me": object()})
    monitor.notifier.close(timeout=5)
    sent: list = []

    class FakeUser:
        def __init__(self, config, logger=None):
            pass

        def notify(self, listings, ratings, item_config, **kwargs):
            sent.append(listings)

    monkeypatch.setattr("ai_marketplace_monitor.monitor.User", FakeUser)
    monitor._notify(["me"], ["listing"], ["rating"], object(), None, None, True)
    assert sent == [["listing"]]


# --------------------------------------------------------------------------- #
# A search that was stopped still says what it found
# --------------------------------------------------------------------------- #
#
# The other half of "when", and the one that was wrong.  With the setting off --
# the default -- the single notification a search sends is built *after* the
# loop over its listings, and every way of ending a search early raises through
# that point: "detener esta busqueda", "detener esta plataforma", pausing, and
# stopping the scraper.  So a stopped search threw away every listing it had
# already found and scored, and the report was the reasonable reading of it:
# "I stop a search and the listings stop arriving in Telegram."
#
# Nothing was lost for ever -- the listings were never marked as notified, so
# the next run of that search would have found them again -- but "the next run"
# is one whole interval away, which on a marketplace where a well-priced console
# is gone in ten minutes is the same as never.


class _Response:
    """An AI verdict, for a monitor configured with no AI at all."""

    def __init__(self, score: int = 5) -> None:
        self.score = score
        self.comment = "not evaluated"
        self.conclusion = "Good deal"
        self.name = ""
        self.style = "info"


class _Listing:
    def __init__(self, listing_id: str) -> None:
        self.id = listing_id
        self.title = f"listing {listing_id}"
        self.content = listing_id
        self.marketplace = "facebook"


class _Config:
    """A configuration section, with whatever fields the caller sets."""

    def __init__(self, **fields) -> None:
        self.__dict__.update(fields)


def _searching_monitor(monkeypatch, notify_immediately: bool):
    """A monitor that can run `_search_item` against a marketplace we control."""
    from ai_marketplace_monitor import control, monitor as monitor_module

    control.reset_for_tests()
    instance = _monitor(notify_immediately, {"me": object()})
    instance._searching = None
    # No AI is configured in this scenario, which is the ordinary case: every
    # listing passes and the rating threshold is the default 3.
    instance.evaluate_by_ai = lambda listing, item_config, marketplace_config: _Response()
    monkeypatch.setattr(monitor_module, "record_rating", lambda *args, **kwargs: None)
    return instance


def _run_search(monitor, marketplace, interrupt=None):
    """Run one search, catching the interruption the marketplace raises."""
    from ai_marketplace_monitor.control import ScrapeInterrupted

    item = _Config(name="ps5", notify=["me"], language=None, rating=None, searched_count=0)
    section = _Config(
        name="facebook", notify=None, language="es", market_type="facebook", rating=None
    )
    raised = None
    try:
        monitor._search_item(section, marketplace, item)
    except ScrapeInterrupted as stopped:
        raised = stopped
    del interrupt
    return raised


def _fake_users(monkeypatch, sent):
    from ai_marketplace_monitor import monitor as monitor_module
    from ai_marketplace_monitor.notification import NotificationStatus

    class FakeUser:
        def __init__(self, config, logger=None):
            pass

        def notification_status(self, listing, local_cache=None):
            return NotificationStatus.NOT_NOTIFIED

        def notify(self, listings, ratings, item_config, **kwargs):
            sent.append([listing.id for listing in listings])

    monkeypatch.setattr(monitor_module, "User", FakeUser)


class _StoppingMarketplace:
    """Yields some listings and is then told to stop, exactly as a checkpoint
    inside the generator would be."""

    def __init__(self, listings, error) -> None:
        self._listings = listings
        self._error = error

    def search(self, item_config):
        for listing in self._listings:
            yield listing
        raise self._error


def test_a_stopped_search_still_notifies_what_it_found(monkeypatch):
    from ai_marketplace_monitor.control import SearchStopped

    sent: list = []
    _fake_users(monkeypatch, sent)
    monitor = _searching_monitor(monkeypatch, notify_immediately=False)
    marketplace = _StoppingMarketplace(
        [_Listing("1"), _Listing("2")],
        SearchStopped(item="ps5", marketplace="facebook"),
    )

    raised = _run_search(monitor, marketplace)

    assert monitor.notifier.drain(5)
    assert sent == [["1", "2"]], "the two listings found before the stop were dropped"
    assert isinstance(raised, SearchStopped), "the stop must still reach the caller"


def test_a_paused_scraper_still_notifies_what_it_found(monkeypatch):
    """The same for the other interruption: "Detener" from the monitor bar."""
    from ai_marketplace_monitor.control import CancelledScrape

    sent: list = []
    _fake_users(monkeypatch, sent)
    monitor = _searching_monitor(monkeypatch, notify_immediately=False)
    marketplace = _StoppingMarketplace([_Listing("7")], CancelledScrape("stopped"))

    raised = _run_search(monitor, marketplace)

    assert monitor.notifier.drain(5)
    assert sent == [["7"]]
    assert isinstance(raised, CancelledScrape)


def test_a_stopped_search_that_found_nothing_notifies_nothing(monkeypatch):
    from ai_marketplace_monitor.control import SearchStopped

    sent: list = []
    _fake_users(monkeypatch, sent)
    monitor = _searching_monitor(monkeypatch, notify_immediately=False)
    marketplace = _StoppingMarketplace([], SearchStopped(item="ps5", marketplace="facebook"))

    raised = _run_search(monitor, marketplace)

    assert sent == []
    assert isinstance(raised, SearchStopped)


def test_an_immediate_search_does_not_send_the_batch_twice(monkeypatch):
    """With the setting on, each listing was already sent as it was found; the
    stop must not add a summary of the same ones."""
    from ai_marketplace_monitor.control import SearchStopped

    sent: list = []
    _fake_users(monkeypatch, sent)
    monitor = _searching_monitor(monkeypatch, notify_immediately=True)
    marketplace = _StoppingMarketplace(
        [_Listing("1"), _Listing("2")],
        SearchStopped(item="ps5", marketplace="facebook"),
    )

    _run_search(monitor, marketplace)

    assert monitor.notifier.drain(5)
    assert sent == [["1"], ["2"]]
