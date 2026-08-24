"""What ``/api/scraper/state`` reports, and why each answer is the one it is.

The endpoint exists so the interface never has to guess.  The two questions it
must answer without lying:

* **Which searches is the scraper running?**  The ones it *loaded*, not the ones
  in the file.  A search saved a second ago is absent until the reload, and a
  search deleted mid-run stays visible, marked, until it ends.
* **Has the scraper taken up my change?**  A hash of the files as they are now
  against a hash of the files as the monitor read them.  Equal, or not.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest
from diskcache import Cache  # type: ignore
from fastapi.testclient import TestClient

from ai_marketplace_monitor import control, pause
from ai_marketplace_monitor.config import Config
from ai_marketplace_monitor.utils import calculate_file_hash
from ai_marketplace_monitor.webui import server as webui_server
from ai_marketplace_monitor.webui.config_api import ConfigFileService
from ai_marketplace_monitor.webui.log_handler import LogBroadcastHandler
from ai_marketplace_monitor.webui.server import AuthState, WebUIConfig, create_app

CONFIG = """
[marketplace.facebook]
username = "user@example.com"
password = "hunter2"
search_city = "houston"

[item.ps5]
search_phrases = "playstation 5"
max_price = 500000

[user.me]
pushbullet_token = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
"""

NO_ITEMS = """
[marketplace.facebook]
username = "user@example.com"
password = "hunter2"
search_city = "houston"

[user.me]
pushbullet_token = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
"""


@pytest.fixture(autouse=True)
def clean_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(pause, "STATE_FILE", tmp_path / "paused.json")
    pause.reset_for_tests()
    control.reset_for_tests()
    yield
    pause.reset_for_tests()
    control.reset_for_tests()


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


@pytest.fixture
def client(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    cache = Cache(str(tmp_path / "cache"))
    monkeypatch.setattr(webui_server, "cache", cache)
    handler = LogBroadcastHandler()
    app = create_app(
        WebUIConfig(config_files=[config_path], log_handler=handler),
        AuthState(),
        ConfigFileService([config_path]),
        handler,
    )
    yield TestClient(app)
    cache.close()


def load(config_path: Path) -> None:
    """Do what the monitor does after a successful config load."""
    control.set_loaded_config(
        calculate_file_hash([config_path]), Config([config_path]).describe()
    )


# --------------------------------------------------------------------------- #
# Before the scraper has loaded anything
# --------------------------------------------------------------------------- #


def test_nothing_loaded_yet_is_unknown_not_stale(client: TestClient) -> None:
    """"Not compared yet" and "behind" are different, and only one is a warning."""
    body = client.get("/api/scraper/state").json()
    assert body["config"]["status"] == "unknown"
    assert body["config"]["loaded_version"] is None
    assert body["config"]["saved_version"]
    assert body["config"]["effective"] is None
    assert body["searches"] == []
    assert body["phase"]["name"] == "starting"


# --------------------------------------------------------------------------- #
# Saved versus loaded
# --------------------------------------------------------------------------- #


def test_a_freshly_loaded_config_is_current(client: TestClient, config_path: Path) -> None:
    load(config_path)
    body = client.get("/api/scraper/state").json()
    assert body["config"]["status"] == "current"
    assert body["config"]["saved_version"] == body["config"]["loaded_version"]


def test_editing_the_file_makes_the_loaded_config_stale(
    client: TestClient, config_path: Path
) -> None:
    load(config_path)
    config_path.write_text(CONFIG.replace("500000", "400000"), encoding="utf-8")

    body = client.get("/api/scraper/state").json()

    assert body["config"]["status"] == "stale"
    assert body["config"]["saved_version"] != body["config"]["loaded_version"]
    # The scraper is still running the old number, and says so.  A price is
    # normalized to a string by the loader, because it may carry a currency
    # ("300 USD"); the point here is which value, not which type.
    search = body["searches"][0]
    assert search["options"]["max_price"] == "500000"


def test_reloading_makes_it_current_again(client: TestClient, config_path: Path) -> None:
    load(config_path)
    config_path.write_text(CONFIG.replace("500000", "400000"), encoding="utf-8")
    assert client.get("/api/scraper/state").json()["config"]["status"] == "stale"

    load(config_path)

    body = client.get("/api/scraper/state").json()
    assert body["config"]["status"] == "current"
    assert body["searches"][0]["options"]["max_price"] == "400000"


def test_the_cheap_status_poll_carries_the_same_answer(
    client: TestClient, config_path: Path
) -> None:
    """Every screen polls /api/status, so the pending warning can appear anywhere."""
    load(config_path)
    assert client.get("/api/status").json()["config_sync"]["status"] == "current"
    config_path.write_text(CONFIG.replace("500000", "400000"), encoding="utf-8")
    assert client.get("/api/status").json()["config_sync"]["status"] == "stale"


# --------------------------------------------------------------------------- #
# Which searches the scraper holds
# --------------------------------------------------------------------------- #


def test_a_configured_search_appears_before_it_has_ever_run(
    client: TestClient, config_path: Path
) -> None:
    load(config_path)
    search = client.get("/api/scraper/state").json()["searches"][0]
    assert search["item"] == "ps5"
    assert search["marketplace"] == "facebook"
    assert search["enabled"] is True
    assert search["running"] is False
    assert search["last_finished_at"] is None


def test_a_running_search_is_reported_with_its_platform(
    client: TestClient, config_path: Path
) -> None:
    load(config_path)
    with control.search("ps5", "facebook"):
        search = client.get("/api/scraper/state").json()["searches"][0]
        assert search["running"] is True
        assert search["started_at"]
    after = client.get("/api/scraper/state").json()["searches"][0]
    assert after["running"] is False
    assert after["last_outcome"] == "finished"


def test_a_slot_that_has_passed_is_sent_as_due_rather_than_as_a_timestamp(
    client: TestClient, config_path: Path
) -> None:
    """The other half of "Próxima ejecución: en cualquier momento".

    `control` works out that a waiting search's slot has gone by, and this row
    used to carry the timestamp and drop that answer.  The screen was then left
    with a moment in the past under a label reading "próxima", and rendered the
    only thing it could -- which is a sentence that cannot be true.
    """
    load(config_path)
    past = (datetime.now(timezone.utc) - timedelta(minutes=8)).isoformat(timespec="seconds")
    with control.search("ps5", "facebook"):
        pass
    control.set_next_runs({"ps5": past}, {("ps5", "facebook"): past})

    row = client.get("/api/scraper/state").json()["searches"][0]

    assert row["next_run"] == past
    assert row["due_now"] is True


def test_a_slot_still_to_come_is_not_reported_as_due(
    client: TestClient, config_path: Path
) -> None:
    load(config_path)
    soon = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(timespec="seconds")
    with control.search("ps5", "facebook"):
        pass
    control.set_next_runs({"ps5": soon}, {("ps5", "facebook"): soon})

    row = client.get("/api/scraper/state").json()["searches"][0]

    assert row["due_now"] is False


def test_a_search_that_has_never_run_still_shows_when_it_next_will(
    client: TestClient, config_path: Path
) -> None:
    """After a restart nothing has run yet, and the slots still exist.

    Runtime history is per process; the scheduler's slots are seeded from what
    each pair last did and therefore survive. Reading the next run only from
    the history meant a search genuinely scheduled for 18:20 was shown as "sin
    programar" for the whole first interval after every restart -- the screen
    contradicting the phase line right above it, which named the same slot.
    """
    load(config_path)
    soon = (datetime.now(timezone.utc) + timedelta(minutes=36)).isoformat(timespec="seconds")
    control.set_next_runs({"ps5": soon}, {("ps5", "facebook"): soon})

    row = client.get("/api/scraper/state").json()["searches"][0]

    assert row["last_outcome"] is None, "nothing has run in this process"
    assert row["next_run"] == soon
    assert row["due_now"] is False


def test_zero_searches_is_reported_as_a_state_not_an_error(
    client: TestClient, config_path: Path
) -> None:
    config_path.write_text(NO_ITEMS, encoding="utf-8")
    load(config_path)

    body = client.get("/api/scraper/state").json()

    assert body["searches"] == []
    assert body["search_count"] == 0
    assert body["config"]["status"] == "current"
    assert body["config"]["effective"] is not None


def test_a_search_deleted_while_running_stays_visible_until_it_ends(
    client: TestClient, config_path: Path
) -> None:
    """Hiding it would claim the scraper had stopped doing what it is doing."""
    load(config_path)
    with control.search("ps5", "facebook"):
        config_path.write_text(NO_ITEMS, encoding="utf-8")
        load(config_path)

        rows = client.get("/api/scraper/state").json()["searches"]
        assert len(rows) == 1
        assert rows[0]["item"] == "ps5"
        assert rows[0]["running"] is True
        assert rows[0]["removed"] is True

    assert client.get("/api/scraper/state").json()["searches"] == []


def test_a_deleted_search_leaves_no_history_behind(
    client: TestClient, config_path: Path
) -> None:
    load(config_path)
    with control.search("ps5", "facebook"):
        pass
    assert client.get("/api/scraper/state").json()["searches"][0]["last_outcome"]

    config_path.write_text(NO_ITEMS, encoding="utf-8")
    control.forget_searches(set())
    load(config_path)

    assert client.get("/api/scraper/state").json()["searches"] == []


# --------------------------------------------------------------------------- #
# The rest of the picture
# --------------------------------------------------------------------------- #


def test_secrets_never_reach_the_browser(client: TestClient, config_path: Path) -> None:
    load(config_path)
    body = client.get("/api/scraper/state").text
    assert "hunter2" not in body
    assert "user@example.com" not in body
    assert "<REDACTED>" in body


def test_the_phase_and_when_it_started_are_reported(client: TestClient) -> None:
    control.set_phase("waiting_for_config", "No searches are configured.")
    phase = client.get("/api/scraper/state").json()["phase"]
    assert phase["name"] == "waiting_for_config"
    assert phase["detail"] == "No searches are configured."
    assert phase["since"]


def test_re_declaring_a_phase_does_not_reset_its_clock(client: TestClient) -> None:
    """A loop that says "idle" every pass must not look like it just got there."""
    first = control.set_phase("idle", "waiting")
    again = control.set_phase("idle", "waiting")
    assert again["since"] == first["since"]


def test_the_listing_updates_report_their_progress(client: TestClient) -> None:
    control.set_updates_config(
        enabled=True, parallel=False, interval=3600, marketplaces=["facebook"]
    )
    with control.updating(["facebook"]):
        control.updates_pending(128)
        control.updates_current("facebook", "12345")
        updates = client.get("/api/scraper/state").json()["updates"]
        assert updates["running"] is True
        assert updates["pending"] == 128
        assert updates["current"]["listing_id"] == "12345"

    after = client.get("/api/scraper/state").json()["updates"]
    assert after["running"] is False
    assert after["current"] is None
    assert after["marketplaces"] == ["facebook"]


def test_updates_with_no_usable_platform_are_reported_disabled(client: TestClient) -> None:
    control.set_updates_config(enabled=False, parallel=False, interval=None, marketplaces=[])
    assert client.get("/api/scraper/state").json()["updates"]["enabled"] is False


# --------------------------------------------------------------------------- #
# "Your change is in use now"
# --------------------------------------------------------------------------- #
#
# Two equal hashes prove a change landed.  They say nothing about *which*
# change, or about what it cost -- and those are what the user who just pressed
# save is asking.  The loop reports both, and both travel on the cheap poll
# every screen already makes, because the answer is worth having wherever the
# user happens to be looking.


def test_nothing_applied_yet_is_reported_as_nothing(client: TestClient) -> None:
    """The first load is not a change; a notice about it would be noise."""
    assert client.get("/api/scraper/state").json()["config"]["applied"] is None
    assert client.get("/api/status").json()["config_sync"]["applied"] is None


def test_an_applied_change_is_reported_with_what_was_in_it(
    client: TestClient, config_path: Path
) -> None:
    load(config_path)
    control.set_config_applied(
        version=calculate_file_hash([config_path]),
        change={
            "added": [{"item": "bici", "marketplace": "facebook"}],
            "removed": [],
            "enabled": [],
            "disabled": [],
            "modified": [],
            "marketplaces": [],
            "schedule": False,
            "general": False,
        },
    )
    applied = client.get("/api/scraper/state").json()["config"]["applied"]
    assert applied["change"]["added"] == [{"item": "bici", "marketplace": "facebook"}]
    assert applied["interrupted"] is None
    assert applied["seq"] == 1
    # And on the cheap poll too, at the same version the state reports loaded.
    status = client.get("/api/status").json()
    assert status["config_sync"]["applied"]["seq"] == 1
    assert status["config_sync"]["status"] == "current"


def test_a_search_dropped_for_the_change_is_named(
    client: TestClient, config_path: Path
) -> None:
    """"Applied" is not the whole story when applying it cost a search.

    The user deleted the thing that was running; being told it stopped is the
    difference between a monitor that obeyed and one that looks like it hung.
    """
    control.set_config_applied(
        version="abc",
        change={"removed": [{"item": "ps5", "marketplace": "facebook"}]},
        interrupted={"item": "ps5", "marketplace": "facebook", "reason": "removed"},
    )
    applied = client.get("/api/scraper/state").json()["config"]["applied"]
    assert applied["interrupted"]["item"] == "ps5"
    assert applied["interrupted"]["reason"] == "removed"
