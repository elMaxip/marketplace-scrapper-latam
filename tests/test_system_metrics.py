"""Resource metrics: real numbers, or an explicit "not available".

The two properties worth pinning are the ones a metrics panel gets wrong: that
nothing is invented when a reading cannot be taken, and that reading the numbers
costs nothing -- the sampler takes them on its own thread and the API hands out
the last one, so twenty open status screens are twenty dictionary copies.

The browser and tab counts are tested here too, because they are the half of
this that is *not* about the machine: only the browsers the scraper opened are
counted, which is what makes the figure mean something on a desktop where the
user has Chrome open beside it.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterator

import pytest

from ai_marketplace_monitor import control, system_metrics


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    control.reset_for_tests()
    yield
    system_metrics.stop()
    control.reset_for_tests()


def sampled() -> Dict[str, Any]:
    """A snapshot with a real sample in it, not the pending placeholder."""
    system_metrics.start()
    deadline = time.time() + 15
    while time.time() < deadline:
        snapshot = system_metrics.snapshot()
        if not snapshot.get("pending"):
            return snapshot
        time.sleep(0.2)
    raise AssertionError("the sampler produced nothing within 15 seconds")


# --------------------------------------------------------------------------- #
# Never invented
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("metric", ["cpu", "memory", "disk", "gpu", "process"])
def test_every_metric_says_whether_it_is_available(metric: str) -> None:
    """`available` is on every one of them, true or false.

    The alternative -- a missing key, or a zero -- is how a panel ends up
    reporting an idle GPU on a machine that has none.
    """
    reading = sampled()[metric]
    assert "available" in reading
    if not reading["available"]:
        assert reading["reason"], f"{metric} is unavailable without saying why"


def test_a_gpu_that_cannot_be_read_is_not_reported_as_idle() -> None:
    gpu = sampled()["gpu"]
    if gpu["available"]:
        for card in gpu["cards"]:
            assert card["name"]
            # A figure the card does not expose is None, not 0.
            assert card["percent"] is None or 0 <= card["percent"] <= 100
    else:
        assert "percent" not in gpu
        assert gpu["reason"]


def test_the_disk_reading_names_the_volume_it_measured() -> None:
    """Which volume is half the answer: it is the one holding the cache."""
    disk = sampled()["disk"]
    assert disk["available"] is True
    assert disk["path"]
    assert disk["total"] > 0
    assert disk["used"] + disk["free"] <= disk["total"] * 1.01
    assert 0 <= disk["percent"] <= 100


def test_the_memory_reading_is_the_whole_machine() -> None:
    """The browsers are separate processes and are most of the cost."""
    memory = sampled()["memory"]
    if memory["available"]:
        assert memory["total"] > 0
        assert 0 <= memory["percent"] <= 100
        assert memory["used"] <= memory["total"]


# --------------------------------------------------------------------------- #
# Cheap to read
# --------------------------------------------------------------------------- #


def test_reading_the_snapshot_does_not_measure_anything() -> None:
    """Twenty polls must cost twenty dictionary copies, not twenty readings.

    A generous ceiling on purpose -- this is a smoke test against somebody
    moving a ``cpu_percent(interval=1)`` or an ``nvidia-smi`` call onto the
    request path, which would take seconds rather than milliseconds.
    """
    sampled()
    started = time.monotonic()
    for _ in range(20):
        system_metrics.snapshot()
    assert time.monotonic() - started < 1.0


def test_the_sample_keeps_moving() -> None:
    """A frozen panel is the failure mode; the timestamp is how it is seen."""
    first = sampled()["at"]
    deadline = time.time() + system_metrics.INTERVAL * 3
    while time.time() < deadline:
        if system_metrics.snapshot()["at"] != first:
            return
        time.sleep(0.2)
    raise AssertionError("the sampler stopped after its first reading")


def test_a_snapshot_before_the_first_sample_says_so() -> None:
    """"Starting" is a different thing to show than "unavailable"."""
    system_metrics.stop()
    system_metrics._sample.clear()
    snapshot = system_metrics.snapshot()
    if snapshot.get("pending"):
        assert "cpu" not in snapshot
        assert snapshot["browsers"] == {"count": 0, "tabs": 0, "detail": []}


# --------------------------------------------------------------------------- #
# The scraper's own browsers
# --------------------------------------------------------------------------- #


def test_only_the_scrapers_browsers_are_counted() -> None:
    """Nothing here can see a browser the monitor did not open.

    The count is built from what the owning threads publish, so a Chrome the
    user has open on the same machine contributes nothing -- which is the
    difference between a useful figure and a random one.
    """
    assert system_metrics.snapshot()["browsers"]["count"] == 0

    control.report_browser(control.MAIN_LANE, 2, "browser-profile-main")
    control.report_browser("mercadolibre", 1)
    browsers = system_metrics.snapshot()["browsers"]
    assert browsers["count"] == 2
    assert browsers["tabs"] == 3
    assert {row["lane"] for row in browsers["detail"]} == {"main", "mercadolibre"}


def test_a_closed_browser_stops_being_counted() -> None:
    control.report_browser(control.MAIN_LANE, 2)
    control.forget_browser(control.MAIN_LANE)
    assert system_metrics.snapshot()["browsers"] == {"count": 0, "tabs": 0, "detail": []}


def test_forgetting_a_browser_twice_is_harmless() -> None:
    """Teardown paths overlap; a close must not depend on being the first."""
    control.report_browser("updates", 1)
    control.forget_browser("updates")
    control.forget_browser("updates")
    assert system_metrics.snapshot()["browsers"]["count"] == 0


def test_reporting_the_same_browser_replaces_its_row() -> None:
    """Each report is the current truth for that browser, not another one."""
    control.report_browser(control.MAIN_LANE, 1)
    control.report_browser(control.MAIN_LANE, 4)
    browsers = system_metrics.snapshot()["browsers"]
    assert browsers["count"] == 1
    assert browsers["tabs"] == 4
