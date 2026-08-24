"""Tests for the session-import endpoints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from ai_marketplace_monitor import control, session
from ai_marketplace_monitor.webui.config_api import ConfigFileService
from ai_marketplace_monitor.webui.log_handler import LogBroadcastHandler
from ai_marketplace_monitor.webui.server import AuthState, WebUIConfig, create_app

CONFIG = "[marketplace.facebook]\nsearch_city = 'dallas'\n\n[marketplace.mercadolibre]\n"


@pytest.fixture(autouse=True)
def temp_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(session, "SESSION_DIR", tmp_path / "sessions")
    control.reset_for_tests()
    yield
    control.reset_for_tests()


def _client(tmp_path: Path, exposed: bool = False) -> TestClient:
    config_file = tmp_path / "config.toml"
    config_file.write_text(CONFIG, encoding="utf-8")
    handler = LogBroadcastHandler()
    state = AuthState()
    state.exposed = exposed
    app = create_app(
        WebUIConfig(config_files=[config_file], log_handler=handler),
        state,
        ConfigFileService([config_file]),
        handler,
    )
    return TestClient(app)


COOKIES = json.dumps(
    [{"name": "ssid", "value": "super-secret", "domain": ".mercadolibre.cl", "path": "/"}]
)


def test_nothing_is_stored_to_begin_with(tmp_path: Path) -> None:
    body = _client(tmp_path).get("/api/marketplace/sessions").json()
    assert body["sessions"]["mercadolibre"]["saved"] is False


def test_importing_stores_it_and_asks_the_monitor_to_load_it(tmp_path: Path) -> None:
    client = _client(tmp_path)
    body = client.post("/api/marketplace/mercadolibre/session", json={"cookies": COOKIES}).json()

    assert body["imported"] == 1
    assert body["session"]["saved"] is True
    # The browser belongs to the scraping thread; it picks this up between jobs.
    assert "mercadolibre" in control.pending_session_imports()


def test_the_listing_never_returns_a_cookie_value(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.post("/api/marketplace/mercadolibre/session", json={"cookies": COOKIES})
    listing = client.get("/api/marketplace/sessions")
    assert "super-secret" not in listing.text
    assert listing.json()["sessions"]["mercadolibre"]["cookies"] == 1


def test_a_header_line_is_accepted(tmp_path: Path) -> None:
    """What devtools copies, with no domain of its own."""
    client = _client(tmp_path)
    body = client.post(
        "/api/marketplace/mercadolibre/session", json={"cookies": "ssid=abc; _d2id=xyz"}
    ).json()
    assert body["imported"] == 2


def test_cookies_from_somewhere_else_are_refused(tmp_path: Path) -> None:
    blob = json.dumps([{"name": "SID", "value": "1", "domain": ".google.com"}])
    response = _client(tmp_path).post(
        "/api/marketplace/mercadolibre/session", json={"cookies": blob}
    )
    assert response.status_code == 400
    assert "marketplace" in response.json()["detail"]


def test_an_unreadable_paste_says_so(tmp_path: Path) -> None:
    response = _client(tmp_path).post(
        "/api/marketplace/mercadolibre/session", json={"cookies": "{not json"}
    )
    assert response.status_code == 400


def test_a_missing_field_is_rejected(tmp_path: Path) -> None:
    assert _client(tmp_path).post("/api/marketplace/mercadolibre/session", json={}).status_code == 400


def test_an_unknown_marketplace_is_a_404(tmp_path: Path) -> None:
    response = _client(tmp_path).post(
        "/api/marketplace/tiendanube/session", json={"cookies": COOKIES}
    )
    assert response.status_code == 404


def test_deleting_forgets_the_session(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.post("/api/marketplace/mercadolibre/session", json={"cookies": COOKIES})
    body = client.request("DELETE", "/api/marketplace/mercadolibre/session").json()
    assert body["session"]["saved"] is False


def test_the_endpoints_need_a_session_when_exposed(tmp_path: Path) -> None:
    client = _client(tmp_path, exposed=True)
    assert client.get("/api/marketplace/sessions").status_code == 401
    assert (
        client.post("/api/marketplace/mercadolibre/session", json={"cookies": COOKIES}).status_code
        == 401
    )


# --------------------------------------------------------------------------- #
# Which platforms the panel lists
# --------------------------------------------------------------------------- #


def test_every_supported_platform_is_listed_without_being_declared(
    tmp_path: Path,
) -> None:
    """The session panel is most needed on a config that declares nothing.

    Listing only the platforms someone had already written a section for meant
    the one screen that explains how to sign in was empty on a fresh install.
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text("# nothing at all\n", encoding="utf-8")
    handler = LogBroadcastHandler()
    app = create_app(
        WebUIConfig(config_files=[config_file], log_handler=handler),
        AuthState(),
        ConfigFileService([config_file]),
        handler,
    )
    body = TestClient(app).get("/api/marketplace/sessions").json()
    assert sorted(body["sessions"]) == ["facebook", "mercadolibre"]


def test_configured_credentials_are_reported_as_another_way_in(tmp_path: Path) -> None:
    """A platform with a username and password is not "without a session".

    The interface warns before a run about platforms it would search
    anonymously; warning about one that can sign itself in would be noise.
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[marketplace.facebook]\nusername = "me@example.com"\npassword = "secret"\n',
        encoding="utf-8",
    )
    handler = LogBroadcastHandler()
    app = create_app(
        WebUIConfig(config_files=[config_file], log_handler=handler),
        AuthState(),
        ConfigFileService([config_file]),
        handler,
    )
    sessions = TestClient(app).get("/api/marketplace/sessions").json()["sessions"]
    assert sessions["facebook"]["credentials"] is True
    assert sessions["mercadolibre"]["credentials"] is False
