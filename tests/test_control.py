"""Tests for the live controls the web UI drives the scraping loop with."""

from __future__ import annotations

from typing import Iterator

import pytest

from ai_marketplace_monitor import control
from ai_marketplace_monitor.control import CancelledScrape


@pytest.fixture(autouse=True)
def clean_state() -> Iterator[None]:
    control.reset_for_tests()
    yield
    control.reset_for_tests()


# --------------------------------------------------------------------------- #
# What is running
# --------------------------------------------------------------------------- #


def test_nothing_is_running_by_default() -> None:
    assert control.is_running() is False
    assert control.state()["current"] is None


def test_running_reports_what_and_then_clears() -> None:
    with control.running(item="ps5", marketplace="facebook"):
        state = control.state()
        assert state["running"] is True
        assert state["current"]["item"] == "ps5"
        assert state["current"]["started_at"]
    assert control.is_running() is False
    assert control.state()["last"]["outcome"] == "finished"


def test_a_cancelled_run_is_recorded_as_cancelled() -> None:
    with pytest.raises(CancelledScrape):
        with control.running(item="ps5"):
            raise CancelledScrape("stop")
    assert control.state()["last"]["outcome"] == "cancelled"


def test_a_failed_run_is_recorded_as_failed() -> None:
    with pytest.raises(ValueError):
        with control.running(item="ps5"):
            raise ValueError("boom")
    assert control.state()["last"]["outcome"] == "failed"


# --------------------------------------------------------------------------- #
# Asking for a scrape
# --------------------------------------------------------------------------- #


def test_a_request_is_taken_exactly_once() -> None:
    assert control.request_run()["accepted"] is True
    assert control.run_pending() is True
    assert control.take_run_request() is True
    assert control.take_run_request() is False
    assert control.run_pending() is False


def test_asking_twice_is_one_request() -> None:
    control.request_run()
    again = control.request_run()
    assert again["accepted"] is True
    assert again["status"] == "already_requested"
    assert control.take_run_request() is True
    assert control.take_run_request() is False


def test_a_request_is_refused_while_a_scrape_runs() -> None:
    """Refused rather than queued: a second pass on top of the first is exactly
    the concurrent traffic the monitor is careful not to produce."""
    with control.running(item="ps5"):
        result = control.request_run()
    assert result["accepted"] is False
    assert result["status"] == "already_running"
    assert result["current"]["item"] == "ps5"
    assert control.run_pending() is False


def test_a_forced_pause_drops_a_pending_request() -> None:
    control.request_run()
    control.request_cancel()
    assert control.run_pending() is False


# --------------------------------------------------------------------------- #
# Cancelling
# --------------------------------------------------------------------------- #


def test_the_checkpoint_only_raises_after_a_request() -> None:
    control.raise_if_cancelled()  # no request: nothing happens
    control.request_cancel()
    assert control.cancel_requested() is True
    with pytest.raises(CancelledScrape):
        control.raise_if_cancelled()
    control.clear_cancel()
    control.raise_if_cancelled()


# --------------------------------------------------------------------------- #
# Claiming a listing
# --------------------------------------------------------------------------- #


def test_a_claim_is_exclusive_and_released() -> None:
    with control.claim("facebook", "1") as mine:
        assert mine is True
        assert control.is_claimed("facebook", "1") is True
        with control.claim("facebook", "1") as second:
            assert second is False
    assert control.is_claimed("facebook", "1") is False


def test_a_claim_is_per_listing() -> None:
    with control.claim("facebook", "1"):
        with control.claim("facebook", "2") as other:
            assert other is True
        with control.claim("mercadolibre", "1") as elsewhere:
            assert elsewhere is True


def test_a_claim_survives_an_exception() -> None:
    with pytest.raises(RuntimeError):
        with control.claim("facebook", "1") as mine:
            assert mine is True
            raise RuntimeError("boom")
    assert control.is_claimed("facebook", "1") is False


def test_a_lost_claim_does_not_release_the_winners() -> None:
    """The loser must leave the claim alone; releasing it would hand the listing
    to a third caller while the real holder is still reading it."""
    with control.claim("facebook", "1"):
        with control.claim("facebook", "1") as second:
            assert second is False
        assert control.is_claimed("facebook", "1") is True
