"""The pause switch shared by the web UI and the monitor loop.

The web UI runs on a thread of the monitor process, so an in-memory flag would
be enough to make the button work.  It is backed by a file anyway, for two
reasons: a pause survives a restart (a monitor that quietly resumed scraping
after a crash would be a surprise, and the reason people pause is usually that
the marketplace is unhappy with them), and the state is inspectable and
recoverable without the UI.

Reads go through the in-memory value, because the monitor asks "am I paused?"
once a second while it sleeps and that must not become a stat() per tick.  The
file is only read once, on first use, and written whenever the switch moves.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .utils import amm_home

logger = logging.getLogger(__name__)

#: Where the switch is persisted.  Deleting this file resumes scraping.
STATE_FILE: Path = amm_home / "paused.json"

_lock = threading.Lock()
#: ``None`` until the file has been consulted; ``(paused, since)`` after.
_state: Optional[Dict[str, Any]] = None


def _blank() -> Dict[str, Any]:
    return {"paused": False, "since": None, "force": False}


def _load() -> Dict[str, Any]:
    """Read the persisted switch.  Any problem reads as "not paused"."""
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _blank()
    except KeyboardInterrupt:
        raise
    except Exception:
        logger.debug("Unreadable pause state at %s; treating as running", STATE_FILE)
        return _blank()
    if not isinstance(raw, dict) or not raw.get("paused"):
        return _blank()
    since = raw.get("since")
    return {
        "paused": True,
        "since": since if isinstance(since, str) else None,
        "force": bool(raw.get("force")),
    }


def _store(state: Dict[str, Any]) -> None:
    """Persist the switch.  A failure here must not break the toggle."""
    try:
        if state["paused"]:
            STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
        else:
            STATE_FILE.unlink(missing_ok=True)
    except KeyboardInterrupt:
        raise
    except OSError:
        logger.warning("Could not persist the pause switch to %s", STATE_FILE, exc_info=True)


def pause_state() -> Dict[str, Any]:
    """``{"paused": bool, "since": <iso timestamp> | None}``."""
    global _state
    with _lock:
        if _state is None:
            _state = _load()
        return dict(_state)


def is_paused() -> bool:
    """Whether new searches should be held back."""
    return bool(pause_state()["paused"])


#: The three states the switch can be in, named for what the user did.
#:
#: ``running``  scraping on its schedule.
#: ``paused``   "Pausar": no new searches, the one under way was left to finish.
#: ``stopped``  "Detener": the search under way was cut off and the browsers closed.
#:
#: Both stops are the same switch underneath, which is why they used to be
#: indistinguishable to the interface -- and why the interface offered "Iniciar"
#: and "Reanudar" side by side after either one, with no way to tell which of
#: them was the answer.  They are not the same act and they do not have the same
#: way back: a pause is resumed, a stop is started again.
RunState = str


def run_state() -> RunState:
    """Which of the three states the monitor is in, in one word."""
    state = pause_state()
    if not state["paused"]:
        return "running"
    return "stopped" if state.get("force") else "paused"


def is_force_paused() -> bool:
    """Whether the pause also asked the running search to stop right now.

    The ordinary pause only holds back what has not started yet.  A forced one
    additionally means "abandon what is running": :mod:`ai_marketplace_monitor.control`
    carries that request to the scraping thread, and this flag is what makes it
    survive a restart -- a monitor that came back mid-search after being force
    paused would defeat the point of the button.
    """
    state = pause_state()
    return bool(state["paused"]) and bool(state.get("force"))


def set_paused(paused: bool, force: bool = False) -> Dict[str, Any]:
    """Move the switch, returning the resulting state.

    Setting a state that is already in force keeps the original ``since``, so
    repeatedly pressing pause does not reset how long the pause has lasted.

    ``force`` only ever escalates: pressing the forced pause on a monitor that
    was already softly paused upgrades it (and keeps ``since``), while pressing
    the ordinary pause on a forced one changes nothing -- there is no way to
    "un-abandon" a search that has already been told to stop.  Resuming clears
    both.
    """
    global _state
    with _lock:
        current = _state if _state is not None else _load()
        if bool(paused) == bool(current["paused"]):
            escalating = bool(paused) and bool(force) and not current.get("force")
            _state = dict(current)
            if escalating:
                _state["force"] = True
                _store(_state)
            return dict(_state)
        _state = (
            {
                "paused": True,
                "since": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "force": bool(force),
            }
            if paused
            else _blank()
        )
        _store(_state)
        return dict(_state)


def reset_for_tests() -> None:
    """Drop the cached value so the next read consults the file again."""
    global _state
    with _lock:
        _state = None
