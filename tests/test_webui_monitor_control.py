"""Tests for the endpoints that drive the scraping loop from the web UI."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from diskcache import Cache  # type: ignore
from fastapi.testclient import TestClient

from ai_marketplace_monitor import control, pause
from ai_marketplace_monitor.webui import server as webui_server
from ai_marketplace_monitor.webui.config_api import ConfigFileService
from ai_marketplace_monitor.webui.log_handler import LogBroadcastHandler
from ai_marketplace_monitor.webui.server import AuthState, WebUIConfig, create_app


@pytest.fixture(autouse=True)
def clean_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(pause, "STATE_FILE", tmp_path / "paused.json")
    pause.reset_for_tests()
    control.reset_for_tests()
    yield
    pause.reset_for_tests()
    control.reset_for_tests()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    cache = Cache(str(tmp_path / "cache"))
    monkeypatch.setattr(webui_server, "cache", cache)
    config_file = tmp_path / "config.toml"
    config_file.write_text("[marketplace.facebook]\nsearch_city = 'dallas'\n", encoding="utf-8")
    handler = LogBroadcastHandler()
    app = create_app(
        WebUIConfig(config_files=[config_file], log_handler=handler),
        AuthState(),
        ConfigFileService([config_file]),
        handler,
    )
    yield TestClient(app)
    cache.close()


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #


def test_status_reports_the_pause_and_what_is_running(client: TestClient) -> None:
    body = client.get("/api/status").json()
    assert body["paused"] is False
    assert body["pause"] == {"paused": False, "since": None, "force": False}
    assert body["scraping"]["running"] is False
    assert body["scraping"]["cancelling"] is False


def test_status_shows_the_running_search(client: TestClient) -> None:
    with control.running(item="ps5", marketplace="facebook"):
        body = client.get("/api/status").json()
    assert body["scraping"]["running"] is True
    assert body["scraping"]["current"]["item"] == "ps5"


# --------------------------------------------------------------------------- #
# Forcing a scrape
# --------------------------------------------------------------------------- #


def test_forcing_a_scrape_raises_the_request(client: TestClient) -> None:
    body = client.post("/api/monitor/run").json()
    assert body["accepted"] is True
    assert control.run_pending() is True


def test_forcing_a_scrape_is_refused_while_one_runs(client: TestClient) -> None:
    """Refused rather than queued: a second full pass on top of the first is
    the concurrent traffic this is meant to avoid."""
    with control.running(item="ps5", marketplace="facebook"):
        body = client.post("/api/monitor/run").json()
    assert body["accepted"] is False
    assert body["status"] == "already_running"
    assert control.run_pending() is False


def test_forcing_a_scrape_does_not_release_the_pause(client: TestClient) -> None:
    """Searching while paused would be two contradictory orders; the caller
    decides which it meant."""
    pause.set_paused(True)
    body = client.post("/api/monitor/run").json()
    assert body["paused"] is True


# --------------------------------------------------------------------------- #
# Pausing, both ways
# --------------------------------------------------------------------------- #


def test_a_plain_pause_stops_the_running_search_but_keeps_the_browsers(
    client: TestClient,
) -> None:
    """Pause means now.

    It used to mean "when the current search finishes", which on a Facebook
    pass is twenty minutes of a button that visibly did nothing.  The search is
    cut off at the next checkpoint like a stop is -- what makes it a pause and
    not a stop is that the browsers, tabs and signed-in sessions are left
    standing, so resuming costs one search rather than one sign-in.
    """
    body = client.post("/api/monitor/pause", json={"paused": True}).json()
    assert body["paused"] is True
    assert body["force"] is False
    assert control.cancel_requested() is True
    assert control.cancel_mode() == "pause"
    assert body["scraping"]["cancel_mode"] == "pause"


def test_a_forced_pause_asks_the_running_search_to_stop(client: TestClient) -> None:
    body = client.post("/api/monitor/pause", json={"paused": True, "force": True}).json()
    assert body["paused"] is True
    assert body["force"] is True
    # A request, not an act: Playwright belongs to the scraping thread, so the
    # handler raises a flag that the loop reads at its next checkpoint.
    assert control.cancel_requested() is True
    assert body["scraping"]["cancelling"] is True
    # And this one takes the browsers with it, which is the whole difference.
    assert control.cancel_mode() == "stop"


# --------------------------------------------------------------------------- #
# Ending one search without stopping the scraper
# --------------------------------------------------------------------------- #


def test_stopping_a_platform_names_only_that_platform(client: TestClient) -> None:
    body = client.post(
        "/api/scraper/search/stop", json={"item": "ps5", "marketplace": "facebook"}
    ).json()
    assert body["ok"] is True
    assert body["stop"]["scope"] == "platform"
    assert control.stop_requested("ps5", "facebook") is not None
    # The same product on the other platform carries on, which is the promise.
    assert control.stop_requested("ps5", "mercadolibre") is None
    # And the scraper is not being stopped.
    assert control.cancel_requested() is False


def test_stopping_a_search_covers_every_platform_of_it(client: TestClient) -> None:
    body = client.post("/api/scraper/search/stop", json={"item": "ps5"}).json()
    assert body["stop"]["scope"] == "search"
    assert control.stop_requested("ps5", "facebook") is not None
    assert control.stop_requested("ps5", "mercadolibre") is not None
    assert control.stop_requested("bici", "facebook") is None


def test_stopping_needs_a_search_to_stop(client: TestClient) -> None:
    assert client.post("/api/scraper/search/stop", json={}).status_code == 400


def test_choosing_the_next_search_leaves_the_current_one_alone(client: TestClient) -> None:
    body = client.post("/api/scraper/search/next", json={"item": "bici"}).json()
    assert body["next_search"]["item"] == "bici"
    assert body["scraping"]["next_search"]["item"] == "bici"
    # Nothing was cancelled: the button says *next*, not *instead of this one*.
    assert control.cancel_requested() is False


def test_choosing_a_second_search_moves_the_choice(client: TestClient) -> None:
    client.post("/api/scraper/search/next", json={"item": "bici"})
    client.post("/api/scraper/search/next", json={"item": "ps5"})
    assert control.next_search()["item"] == "ps5"


def test_the_choice_can_be_withdrawn(client: TestClient) -> None:
    client.post("/api/scraper/search/next", json={"item": "bici"})
    body = client.post("/api/scraper/search/next", json={"item": None}).json()
    assert body["next_search"] is None
    assert control.next_search() is None


def test_resuming_withdraws_a_stop_that_never_landed(client: TestClient) -> None:
    client.post("/api/monitor/pause", json={"paused": True, "force": True})
    body = client.post("/api/monitor/pause", json={"paused": False}).json()
    assert body["paused"] is False
    assert body["force"] is False
    assert control.cancel_requested() is False


def test_force_is_ignored_when_resuming(client: TestClient) -> None:
    """"Resume, forcefully" is not a thing; it must not leave a stop pending."""
    body = client.post("/api/monitor/pause", json={"paused": False, "force": True}).json()
    assert body["paused"] is False
    assert control.cancel_requested() is False


def test_pause_still_rejects_a_missing_flag(client: TestClient) -> None:
    assert client.post("/api/monitor/pause", json={}).status_code == 400


def test_get_pause_reports_the_scraping_state_too(client: TestClient) -> None:
    body = client.get("/api/monitor/pause").json()
    assert body["paused"] is False
    assert "scraping" in body


# --------------------------------------------------------------------------- #
# Which of the three states the monitor is in
# --------------------------------------------------------------------------- #
#
# The interface offers "Iniciar" from a stop and "Reanudar" from a pause, and
# never both.  It can only do that if the two stops are distinguishable, which
# they were not: the poll carried `paused` and `pause.force` and left the
# browser to work out what that meant.


def test_status_says_running_when_nothing_is_held_back(client: TestClient) -> None:
    assert client.get("/api/status").json()["run_state"] == "running"


def test_a_pause_reads_as_paused_everywhere_it_is_reported(client: TestClient) -> None:
    body = client.post("/api/monitor/pause", json={"paused": True}).json()
    assert body["run_state"] == "paused"
    assert client.get("/api/status").json()["run_state"] == "paused"
    assert client.get("/api/monitor/pause").json()["run_state"] == "paused"
    assert client.get("/api/scraper/state").json()["run_state"] == "paused"


def test_a_forced_pause_reads_as_stopped(client: TestClient) -> None:
    body = client.post("/api/monitor/pause", json={"paused": True, "force": True}).json()
    assert body["run_state"] == "stopped"
    assert client.get("/api/status").json()["run_state"] == "stopped"


def test_resuming_from_a_stop_reads_as_running(client: TestClient) -> None:
    client.post("/api/monitor/pause", json={"paused": True, "force": True})
    body = client.post("/api/monitor/pause", json={"paused": False}).json()
    assert body["run_state"] == "running"
