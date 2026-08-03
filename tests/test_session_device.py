"""Tests for keeping the device identity across a failed login.

A login that never completes used to save nothing at all, so each retry reached
the site as a brand-new browser -- which is what escalates a single challenge
into a loop of them.  Only the stable device cookies are kept; replaying the
half-authenticated session would land the next attempt back in the same
challenge.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from ai_marketplace_monitor import session as session_mod


class FakeContext:
    def __init__(self, cookies: List[Dict[str, Any]]) -> None:
        self.cookies = cookies

    def storage_state(self) -> Dict[str, Any]:
        return {
            "cookies": self.cookies,
            "origins": [{"origin": "https://www.facebook.com", "localStorage": [{"k": "v"}]}],
        }


@pytest.fixture
def session_dir(tmp_path: Path) -> Iterator[Path]:
    original = session_mod.SESSION_DIR
    session_mod.SESSION_DIR = tmp_path / "sessions"
    yield session_mod.SESSION_DIR
    session_mod.SESSION_DIR = original


MIXED = [
    {"name": "datr", "value": "device-id"},
    {"name": "sb", "value": "secure-browser"},
    {"name": "c_user", "value": "42"},
    {"name": "xs", "value": "secret-session"},
    {"name": "checkpoint", "value": "stuck"},
]


def test_only_device_cookies_are_kept(session_dir: Path) -> None:
    assert session_mod.save_device_state("facebook", FakeContext(MIXED)) is True
    saved = session_mod.load_session("facebook")
    assert saved is not None
    names = {cookie["name"] for cookie in saved["cookies"]}
    assert names == {"datr", "sb"}


def test_session_and_checkpoint_cookies_are_dropped(session_dir: Path) -> None:
    """Replaying these would put the next attempt back into the same challenge."""
    session_mod.save_device_state("facebook", FakeContext(MIXED))
    saved = session_mod.load_session("facebook")
    assert saved is not None
    names = {cookie["name"] for cookie in saved["cookies"]}
    assert "c_user" not in names
    assert "xs" not in names
    assert "checkpoint" not in names


def test_local_storage_is_dropped(session_dir: Path) -> None:
    session_mod.save_device_state("facebook", FakeContext(MIXED))
    saved = session_mod.load_session("facebook")
    assert saved is not None
    assert saved["origins"] == []


def test_nothing_written_when_there_is_no_device_cookie(session_dir: Path) -> None:
    """Do not clobber a good saved session with an empty one."""
    context = FakeContext([{"name": "c_user", "value": "42"}])
    assert session_mod.save_device_state("facebook", context) is False
    assert session_mod.load_session("facebook") is None


def test_device_state_does_not_clobber_a_full_session(session_dir: Path) -> None:
    session_mod.save_session("facebook", FakeContext(MIXED))
    session_mod.save_device_state("facebook", FakeContext([{"name": "c_user", "value": "42"}]))
    saved = session_mod.load_session("facebook")
    assert saved is not None
    assert {cookie["name"] for cookie in saved["cookies"]} == {
        cookie["name"] for cookie in MIXED
    }


def test_saved_device_state_reloads(session_dir: Path) -> None:
    """The point of the exercise: the next run starts as the same device."""
    session_mod.save_device_state("facebook", FakeContext(MIXED))
    restored = session_mod.load_session("facebook")
    assert restored is not None
    datr = next(c for c in restored["cookies"] if c["name"] == "datr")
    assert datr["value"] == "device-id"
