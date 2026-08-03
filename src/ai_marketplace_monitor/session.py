"""Persist a marketplace's browser session between runs.

Without this every run starts from a blank browser profile, so the marketplace
sees a brand-new device each time and challenges the login with two-factor
verification on every single start.  Saving the storage state (cookies plus
per-origin localStorage) lets the next run resume an existing session instead of
logging in again.

Storage state rather than a persistent profile directory
(``launch_persistent_context``) is deliberate: the proxy is chosen per *context*
in :meth:`Marketplace.create_page`, and a persistent profile fixes the proxy at
launch, which would quietly break proxy rotation.  Storage state keeps that
working -- the same session can be replayed through a different proxy -- and
carries the cookies that actually matter for staying signed in.

The saved file is credential-equivalent: anyone holding it is signed in as the
user.  It is written to the private ``~/.ai-marketplace-monitor`` directory and
locked down to the owner where the platform supports it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from .utils import amm_home

logger = logging.getLogger(__name__)

SESSION_DIR = amm_home / "sessions"

#: On-disk Chromium profile shared by every marketplace.
#:
#: A profile directory carries far more than cookies -- the device identifier a
#: site issues, its local databases, the whole browser identity -- which is what
#: lets a second run look like the same browser coming back rather than a fresh
#: install.  Cookies alone are not enough for sites that re-challenge a login
#: whose browser they do not recognize.
PROFILE_DIR = amm_home / "browser-profile"


def profile_dir() -> Path:
    """The browser profile directory, created on first use."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return PROFILE_DIR


def profile_is_new() -> bool:
    """Whether the profile has yet to be written by a real browser run."""
    # Chromium drops a "Default" subdirectory as soon as it owns the profile.
    return not (PROFILE_DIR / "Default").exists()


def clear_profile() -> bool:
    """Delete the browser profile, so the next run starts from a clean install."""
    if not PROFILE_DIR.exists():
        return False
    shutil.rmtree(PROFILE_DIR, ignore_errors=True)
    return not PROFILE_DIR.exists()


def session_path(marketplace_name: str) -> Path:
    """Where one marketplace's saved session lives."""
    # Marketplace names come from config section headers, so keep the filename
    # to characters that cannot escape the directory.
    safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in marketplace_name)
    return SESSION_DIR / f"{safe or 'default'}.json"


def _restrict(path: Path) -> None:
    """Make the session file owner-only where the platform supports it."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows ignores POSIX modes; the file still sits in the user's private
        # home directory, which is the practical protection there.
        logger.debug("Could not restrict permissions on %s", path, exc_info=True)


def load_session(marketplace_name: str) -> Optional[Dict[str, Any]]:
    """Return a saved storage state, or None when there is nothing usable.

    Returns the parsed object rather than the path so a missing or corrupt file
    is a None here instead of an exception out of Playwright.
    """
    path = session_path(marketplace_name)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
    except KeyboardInterrupt:
        raise
    except Exception:
        logger.debug("Ignoring unreadable session file %s", path, exc_info=True)
        return None
    if not isinstance(state, dict) or "cookies" not in state:
        logger.debug("Ignoring session file %s: unexpected shape", path)
        return None
    return state


def save_session(marketplace_name: str, context: Any) -> bool:
    """Write ``context``'s storage state for the next run.

    Best effort: failing to save a session must never take down a scrape, so
    every error is swallowed and reported through the return value.
    """
    try:
        return _write(marketplace_name, context.storage_state())
    except KeyboardInterrupt:
        raise
    except Exception:
        logger.debug("Could not save session for %r", marketplace_name, exc_info=True)
        return False


def _write(marketplace_name: str, state: Dict[str, Any]) -> bool:
    """Atomically write a storage state to this marketplace's session file."""
    path = session_path(marketplace_name)
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the same directory and replace, so an
        # interrupted save cannot leave a half-written session behind.
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(SESSION_DIR), delete=False, suffix=".tmp"
        )
        try:
            with handle:
                json.dump(state, handle)
            _restrict(Path(handle.name))
            os.replace(handle.name, path)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise
        _restrict(path)
        return True
    except KeyboardInterrupt:
        raise
    except Exception:
        logger.debug("Could not save session for %r", marketplace_name, exc_info=True)
        return False


#: Cookies that identify the *browser*, not the signed-in user.  ``datr`` is the
#: important one: Facebook uses it to recognize a device across visits.
DEVICE_COOKIES = frozenset({"datr", "sb", "wd", "dpr", "locale"})


def save_device_state(marketplace_name: str, context: Any) -> bool:
    """Persist only the device-identity cookies, discarding session state.

    Called when a login *fails*.  Without this, every failed attempt throws away
    the device identity and the next one arrives as a brand-new browser -- which
    is exactly what escalates a site into challenging the login again, so a
    CAPTCHA loop can never work itself out.  Keeping ``datr`` across attempts
    means the site sees one device retrying rather than a stream of new ones.

    Session and checkpoint cookies are deliberately dropped: replaying those
    would restore the half-authenticated state that failed, putting the next run
    straight back into the same challenge.
    """
    try:
        state = context.storage_state()
        kept = [
            cookie
            for cookie in state.get("cookies", [])
            if cookie.get("name") in DEVICE_COOKIES
        ]
        if not kept:
            return False
        return _write(marketplace_name, {"cookies": kept, "origins": []})
    except KeyboardInterrupt:
        raise
    except Exception:
        logger.debug("Could not save device state for %r", marketplace_name, exc_info=True)
        return False


def clear_session(marketplace_name: str) -> None:
    """Drop a saved session, so the next run logs in from scratch."""
    try:
        session_path(marketplace_name).unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not remove session for %r", marketplace_name, exc_info=True)


def clear_all_sessions() -> int:
    """Drop every saved session.  Returns how many were removed."""
    if not SESSION_DIR.exists():
        return 0
    removed = 0
    for path in SESSION_DIR.glob("*.json"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            logger.debug("Could not remove session file %s", path, exc_info=True)
    return removed
