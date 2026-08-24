"""Tests for the pause switch shared by the web UI and the monitor loop."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from ai_marketplace_monitor import pause


@pytest.fixture(autouse=True)
def temp_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the switch at a throwaway file and forget any cached value."""
    state_file = tmp_path / "paused.json"
    monkeypatch.setattr(pause, "STATE_FILE", state_file)
    pause.reset_for_tests()
    yield state_file
    pause.reset_for_tests()


def test_defaults_to_running() -> None:
    assert pause.is_paused() is False
    assert pause.pause_state() == {"paused": False, "since": None, "force": False}
    assert pause.is_force_paused() is False


def test_pause_and_resume_roundtrip() -> None:
    state = pause.set_paused(True)
    assert state["paused"] is True
    assert state["since"]
    assert pause.is_paused() is True

    assert pause.set_paused(False)["paused"] is False
    assert pause.is_paused() is False


def test_pause_survives_a_restart(temp_state: Path) -> None:
    """A paused monitor that is restarted must come back paused."""
    pause.set_paused(True)
    # Dropping the cached value is what a fresh process sees.
    pause.reset_for_tests()
    assert pause.is_paused() is True


def test_resuming_removes_the_file(temp_state: Path) -> None:
    pause.set_paused(True)
    assert temp_state.exists()
    pause.set_paused(False)
    assert not temp_state.exists()


def test_pausing_twice_keeps_the_original_timestamp() -> None:
    first = pause.set_paused(True)
    again = pause.set_paused(True)
    assert again["since"] == first["since"]


def test_unreadable_state_reads_as_running(temp_state: Path) -> None:
    """A corrupt file must not wedge the monitor into a permanent pause."""
    temp_state.write_text("this is not json", encoding="utf-8")
    pause.reset_for_tests()
    assert pause.is_paused() is False


def test_reported_state_is_a_copy() -> None:
    """Callers get a snapshot; mutating it must not move the switch."""
    pause.set_paused(True)
    snapshot = pause.pause_state()
    snapshot["paused"] = False
    assert pause.is_paused() is True


# --------------------------------------------------------------------------- #
# The forced pause
# --------------------------------------------------------------------------- #


def test_a_plain_pause_is_not_a_forced_one() -> None:
    """The difference is the whole point of having two buttons: one holds back
    what has not started, the other abandons what is running."""
    pause.set_paused(True)
    assert pause.is_paused() is True
    assert pause.is_force_paused() is False


def test_a_forced_pause_is_both() -> None:
    state = pause.set_paused(True, force=True)
    assert state["paused"] is True
    assert state["force"] is True
    assert pause.is_force_paused() is True


def test_forcing_an_existing_pause_upgrades_it_without_moving_since() -> None:
    first = pause.set_paused(True)
    upgraded = pause.set_paused(True, force=True)
    assert upgraded["force"] is True
    assert upgraded["since"] == first["since"]
    assert pause.is_force_paused() is True


def test_a_plain_pause_cannot_undo_a_forced_one() -> None:
    """There is no un-abandoning a search that has already been told to stop."""
    pause.set_paused(True, force=True)
    assert pause.set_paused(True)["force"] is True
    assert pause.is_force_paused() is True


def test_a_forced_pause_survives_a_restart(temp_state: Path) -> None:
    pause.set_paused(True, force=True)
    pause.reset_for_tests()
    assert pause.is_force_paused() is True


def test_resuming_clears_the_force() -> None:
    pause.set_paused(True, force=True)
    pause.set_paused(False)
    assert pause.is_paused() is False
    assert pause.is_force_paused() is False


# --------------------------------------------------------------------------- #
# The three states, named
# --------------------------------------------------------------------------- #
#
# "Pausar" and "Detener" are the same switch underneath, which is exactly why
# the interface could not tell them apart: it saw `paused` and `force` and had
# to decide for itself what they meant, so both stops produced the same pair of
# buttons -- "Iniciar" *and* "Reanudar", two answers to a question with one.
# `run_state` is that decision made once, here, by the side that knows.


def test_running_is_the_state_with_nothing_held_back() -> None:
    assert pause.run_state() == "running"


def test_a_plain_pause_reads_as_paused() -> None:
    pause.set_paused(True)
    assert pause.run_state() == "paused"


def test_a_forced_pause_reads_as_stopped() -> None:
    pause.set_paused(True, force=True)
    assert pause.run_state() == "stopped"


def test_resuming_from_either_reads_as_running() -> None:
    for force in (False, True):
        pause.set_paused(True, force=force)
        pause.set_paused(False)
        assert pause.run_state() == "running"


def test_the_state_survives_a_restart(temp_state: Path) -> None:
    """Which stop it was outlives the process, because the way back differs."""
    pause.set_paused(True, force=True)
    pause.reset_for_tests()
    assert pause.run_state() == "stopped"
