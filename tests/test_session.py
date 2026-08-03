"""Tests for saved marketplace sessions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator

import pytest

from ai_marketplace_monitor import session as session_mod

STATE: Dict[str, Any] = {
    "cookies": [{"name": "c_user", "value": "42", "domain": ".facebook.com"}],
    "origins": [],
}


class FakeContext:
    """Stands in for a Playwright BrowserContext."""

    def __init__(self, state: Dict[str, Any] | None = None, fail: bool = False) -> None:
        self.state = STATE if state is None else state
        self.fail = fail

    def storage_state(self) -> Dict[str, Any]:
        if self.fail:
            raise RuntimeError("browser is gone")
        return self.state


@pytest.fixture
def session_dir(tmp_path: Path) -> Iterator[Path]:
    """Redirect session storage into a throwaway directory."""
    original = session_mod.SESSION_DIR
    session_mod.SESSION_DIR = tmp_path / "sessions"
    yield session_mod.SESSION_DIR
    session_mod.SESSION_DIR = original


def test_missing_session_reads_as_none(session_dir: Path) -> None:
    assert session_mod.load_session("facebook") is None


def test_save_then_load_round_trip(session_dir: Path) -> None:
    assert session_mod.save_session("facebook", FakeContext()) is True
    assert session_mod.load_session("facebook") == STATE


def test_corrupt_session_is_ignored(session_dir: Path) -> None:
    """A half-written file must degrade to "log in again", not raise."""
    session_mod.save_session("facebook", FakeContext())
    session_mod.session_path("facebook").write_text("{not json", encoding="utf-8")
    assert session_mod.load_session("facebook") is None


def test_session_of_unexpected_shape_is_ignored(session_dir: Path) -> None:
    session_mod.SESSION_DIR.mkdir(parents=True, exist_ok=True)
    session_mod.session_path("facebook").write_text('{"nope": 1}', encoding="utf-8")
    assert session_mod.load_session("facebook") is None


def test_save_failure_is_reported_not_raised(session_dir: Path) -> None:
    """Losing a session must never take down a scrape."""
    assert session_mod.save_session("facebook", FakeContext(fail=True)) is False
    assert session_mod.load_session("facebook") is None


def test_save_leaves_no_temp_file_behind(session_dir: Path) -> None:
    session_mod.save_session("facebook", FakeContext())
    assert list(session_dir.glob("*.tmp")) == []


def test_failed_save_leaves_the_previous_session_intact(session_dir: Path) -> None:
    session_mod.save_session("facebook", FakeContext())
    session_mod.save_session("facebook", FakeContext(fail=True))
    assert session_mod.load_session("facebook") == STATE


def test_filename_cannot_escape_the_session_directory() -> None:
    """Marketplace names come from config section headers, so treat as input."""
    path = session_mod.session_path("../../etc/passwd")
    assert path.parent == session_mod.SESSION_DIR
    assert ".." not in path.name


def test_clear_session_removes_only_that_marketplace(session_dir: Path) -> None:
    session_mod.save_session("facebook", FakeContext())
    session_mod.save_session("other", FakeContext())
    session_mod.clear_session("facebook")
    assert session_mod.load_session("facebook") is None
    assert session_mod.load_session("other") == STATE


def test_clear_all_sessions(session_dir: Path) -> None:
    session_mod.save_session("facebook", FakeContext())
    session_mod.save_session("other", FakeContext())
    assert session_mod.clear_all_sessions() == 2
    assert session_mod.load_session("facebook") is None


def test_clear_all_sessions_on_missing_directory(session_dir: Path) -> None:
    assert session_mod.clear_all_sessions() == 0


@pytest.fixture
def profile_dir(tmp_path: Path) -> Iterator[Path]:
    """Redirect the browser profile into a throwaway directory."""
    original = session_mod.PROFILE_DIR
    session_mod.PROFILE_DIR = tmp_path / "browser-profile"
    yield session_mod.PROFILE_DIR
    session_mod.PROFILE_DIR = original


def test_profile_dir_is_created_on_demand(profile_dir: Path) -> None:
    assert not profile_dir.exists()
    assert session_mod.profile_dir() == profile_dir
    assert profile_dir.exists()


def test_profile_is_new_until_the_browser_owns_it(profile_dir: Path) -> None:
    """Creating the directory is not the same as a browser having written it."""
    assert session_mod.profile_is_new() is True
    session_mod.profile_dir()
    assert session_mod.profile_is_new() is True
    # Chromium drops a "Default" subdirectory once it takes the profile over.
    (profile_dir / "Default").mkdir(parents=True)
    assert session_mod.profile_is_new() is False


def test_clear_profile_removes_everything(profile_dir: Path) -> None:
    (profile_dir / "Default").mkdir(parents=True)
    (profile_dir / "Default" / "Cookies").write_text("data", encoding="utf-8")
    assert session_mod.clear_profile() is True
    assert not profile_dir.exists()
    assert session_mod.profile_is_new() is True


def test_clear_profile_on_missing_directory(profile_dir: Path) -> None:
    assert session_mod.clear_profile() is False
