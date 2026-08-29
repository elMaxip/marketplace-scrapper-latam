"""Spacing that keeps its average and loses its regularity.

The scrapers already paced themselves and the pacing was the problem rather
than the fix: two seconds, exactly, forty-eight times in a row is not a slow
visitor, it is a metronome.  Lider refuses product pages arriving in that
pattern while serving the results grid that was asked for once.

The two properties that matter are here, and they pull against each other:
the interval must genuinely vary, and the *mean* must not move -- a pacing
change that quietly costs 20% more wall clock on every pass is a slowdown
wearing a disguise.
"""

from __future__ import annotations

import statistics
from typing import Iterator

import pytest

from ai_marketplace_monitor import utils
from ai_marketplace_monitor.utils import human_interval, human_scroll


@pytest.fixture(autouse=True)
def pacing_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """On regardless of the environment the suite happens to run in."""
    monkeypatch.setattr(utils, "HUMAN_PACING", True)
    yield


def test_the_average_is_unchanged() -> None:
    """The whole reason the change is free.  The bounds are the two-sigma points
    and they are symmetric, so clipping takes as much off the long tail as off
    the short one."""
    draws = [human_interval(2.0) for _ in range(20000)]
    assert statistics.fmean(draws) == pytest.approx(2.0, abs=0.02)


def test_the_interval_actually_varies() -> None:
    draws = [human_interval(2.0) for _ in range(500)]
    assert len(set(draws)) > 400
    assert statistics.stdev(draws) > 0.3


def test_it_never_strays_further_than_the_bounds() -> None:
    """A pause that can be arbitrarily long is a search that can hang."""
    low, high = utils.HUMAN_BOUNDS
    draws = [human_interval(2.0) for _ in range(20000)]
    assert min(draws) >= 2.0 * low
    assert max(draws) <= 2.0 * high


def test_turning_it_off_restores_the_exact_interval() -> None:
    """What somebody comparing timings before and after needs, and what a test
    that must not be flaky needs."""
    utils.HUMAN_PACING = False
    try:
        assert [human_interval(2.0) for _ in range(20)] == [2.0] * 20
    finally:
        utils.HUMAN_PACING = True


def test_no_wait_at_all_stays_no_wait() -> None:
    assert human_interval(0) == 0.0
    assert human_interval(-1) == 0.0


class UnScrollablePage:
    """A page object with no mouse, which is most of the fakes in this suite."""


def test_a_page_that_will_not_scroll_is_not_a_failure() -> None:
    """A search that already has its results must not be lost to a flourish."""
    assert human_scroll(UnScrollablePage()) is False


def test_scrolling_is_off_with_the_pacing() -> None:
    utils.HUMAN_PACING = False
    try:
        assert human_scroll(UnScrollablePage()) is False
    finally:
        utils.HUMAN_PACING = True
