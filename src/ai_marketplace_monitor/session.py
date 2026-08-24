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
import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

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


def _lane_suffix(lane: str | None) -> str:
    """The directory suffix for one lane, kept to filename-safe characters.

    Lane names come from marketplace section headers, so they are user text and
    must not be able to walk out of the home directory.
    """
    if not lane:
        return ""
    safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in lane)
    return f"-{safe}" if safe else ""


def profile_path(lane: str | None = None) -> Path:
    """Where one lane's profile lives, without creating it.

    ``None`` is the profile the monitor has always used.  A named lane gets a
    directory of its own because a Chromium profile is held exclusively by the
    process that opens it: two browsers running at the same time -- which is
    what parallel searching means -- cannot share one.
    """
    return PROFILE_DIR if lane is None else PROFILE_DIR.parent / (PROFILE_DIR.name + _lane_suffix(lane))


def profile_dir(lane: str | None = None) -> Path:
    """The browser profile directory, created on first use."""
    path = profile_path(lane)
    path.mkdir(parents=True, exist_ok=True)
    return path


def profile_is_new(lane: str | None = None) -> bool:
    """Whether the profile has yet to be written by a real browser run."""
    # Chromium drops a "Default" subdirectory as soon as it owns the profile.
    return not (profile_path(lane) / "Default").exists()


#: The files Chromium uses to claim a profile directory for one running browser.
#: ``SingletonLock`` is a symbolic link whose target is ``<hostname>-<pid>``; the
#: other two go with it and are dropped together.
_SINGLETON_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def _singleton_owner(lane: str | None = None) -> Optional[tuple[str, int]]:
    """The host and process id written into this profile's lock, if any."""
    try:
        target = os.readlink(profile_path(lane) / "SingletonLock")
    except OSError:
        # No lock, or a platform where it is not a symlink (Windows), or a
        # directory we may not read.  Nothing to say either way.
        return None
    host, _, pid = target.rpartition("-")
    try:
        return host, int(pid)
    except ValueError:
        return None


def stale_profile_lock(lane: str | None = None) -> Optional[str]:
    """Why this profile's lock cannot belong to a live browser, or None.

    Chromium refuses to open a profile another Chromium holds, and says so on
    stderr before exiting -- which Playwright reports as "Target page, context or
    browser has been closed" and this monitor, one level up again, as "no browser
    could be launched".  The profile is then unusable until somebody deletes a
    file, and the monitor crashes on every start; under supervisor in a container
    that is a restart loop with a web UI that never comes up.

    Two ways a lock outlives its browser, and the container hits both:

    * the browser was killed rather than closed (``docker kill``, an OOM, a
      power cut), so nothing removed the lock;
    * the container was *replaced*, which is the ordinary way to restart one.
      The profile lives in a volume and the volume outlives the container, but
      the hostname does not -- so the lock names a machine that no longer exists,
      and Chromium's own check is "different host, assume it is still in use".

    Deliberately conservative: an unreadable lock, an unparsable one, or a live
    process of our own name says nothing and nothing is removed.  The only two
    answers are "written by a host that is not this one" and "written by a
    process that is gone".
    """
    owner = _singleton_owner(lane)
    if owner is None:
        return None
    host, pid = owner
    if host != socket.gethostname():
        # A profile is held by one browser on one machine.  Another machine's
        # name in the lock means that browser is not reachable from here, and in
        # a container it means the container it ran in has been replaced.
        return f"a browser on {host}, which is not this machine"
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return f"process {pid}, which is no longer running"
    except OSError:
        # Alive but not ours to signal, or a platform without `kill`.  Either
        # way, not evidence that it is gone.
        return None
    return None


def release_stale_profile_lock(lane: str | None = None) -> Optional[str]:
    """Drop a profile lock left behind by a browser that is gone.  Reason, or None.

    Returns what was wrong so the caller can say it: a lock silently removed is
    a lock that will be silently removed again next time something else goes
    wrong with the profile.
    """
    reason = stale_profile_lock(lane)
    if reason is None:
        return None
    for name in _SINGLETON_FILES:
        path = profile_path(lane) / name
        try:
            path.unlink()
        except OSError:
            # A lock we cannot remove is not a lock we can pretend to have
            # removed; the launch will fail and say why.
            continue
    return reason


def profile_lanes() -> List[str]:
    """Every lane profile that exists on disk, the unnamed one excluded."""
    prefix = PROFILE_DIR.name + "-"
    try:
        return sorted(
            path.name[len(prefix) :]
            for path in PROFILE_DIR.parent.iterdir()
            if path.is_dir() and path.name.startswith(prefix)
        )
    except OSError:
        return []


def clear_profile() -> bool:
    """Delete every browser profile, so the next run starts from a clean install.

    Lane profiles go with the main one: they are copies of the same browser
    identity handed to a second window, and leaving one behind would mean a
    "clean install" that still remembers.
    """
    paths = [PROFILE_DIR, *(profile_path(lane) for lane in profile_lanes())]
    existing = [path for path in paths if path.exists()]
    if not existing:
        return False
    for path in existing:
        shutil.rmtree(path, ignore_errors=True)
    return not any(path.exists() for path in existing)


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


# --------------------------------------------------------------------------- #
# Importing a session from the user's own browser
# --------------------------------------------------------------------------- #
#
# Some sites will not complete a sign-in inside an automated browser at all --
# the form is accepted and the visitor is quietly returned to the front page.
# Arguing with that is a losing game, so the way out is to hand the monitor a
# session the user already established in their normal browser: their account,
# their machine, their cookies, copied across.
#
# Three shapes are accepted because that is what the tools people actually have
# produce: Playwright's own ``storageState`` JSON, the array a cookie-manager
# extension exports, and the bare ``Cookie:`` header line from devtools.

#: How each source spells "when this expires".  A cookie with none is a session
#: cookie, which Playwright wants as ``-1`` rather than as an absent key.
_EXPIRY_KEYS = ("expires", "expirationDate", "expiration_date", "expiry")

#: Cookie managers spell SameSite in their own vocabularies; Playwright accepts
#: exactly three values.
_SAME_SITE = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
    "no_restriction": "None",
    "unspecified": "Lax",
}


def _strip_dot(domain: str) -> str:
    return domain[1:] if domain.startswith(".") else domain


def domain_allowed(domain: str, allowed: Iterable[str]) -> bool:
    """Whether a cookie's domain belongs to one of ``allowed``.

    Suffix matching on label boundaries, so ``.mercadolibre.cl`` matches
    ``mercadolibre.cl`` and ``notmercadolibre.cl`` does not.  Pasting the wrong
    export is a normal mistake, and silently loading somebody's Google session
    into the scraping profile would be a bad way to find out.
    """
    host = _strip_dot(str(domain or "")).lower()
    if not host:
        return False
    return any(
        host == candidate.lower() or host.endswith("." + candidate.lower())
        for candidate in allowed
    )


def normalize_cookie(raw: Any, default_domain: str = "") -> Optional[Dict[str, Any]]:
    """One cookie in the shape Playwright's ``add_cookies`` takes, or None.

    Everything optional is defaulted rather than rejected: an export that omits
    ``path`` or ``sameSite`` is still a perfectly good cookie, and refusing it
    would mean refusing the whole session over a field the browser fills in.
    """
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    value = raw.get("value")
    if value is None:
        return None

    domain = str(raw.get("domain") or default_domain or "").strip()
    if not domain:
        return None

    expires = -1.0
    for key in _EXPIRY_KEYS:
        candidate = raw.get(key)
        if isinstance(candidate, (int, float)) and candidate > 0:
            expires = float(candidate)
            break

    same_site = _SAME_SITE.get(str(raw.get("sameSite") or "").strip().lower(), "Lax")
    secure = bool(raw.get("secure", False))
    # Chromium refuses SameSite=None on a non-secure cookie, and a refused
    # cookie is a session that silently does not work.
    if same_site == "None":
        secure = True

    return {
        "name": name,
        "value": str(value),
        "domain": domain,
        "path": str(raw.get("path") or "/"),
        "expires": expires,
        "httpOnly": bool(raw.get("httpOnly", raw.get("http_only", False))),
        "secure": secure,
        "sameSite": same_site,
    }


def parse_cookies(text: str, default_domain: str = "") -> List[Dict[str, Any]]:
    """Read cookies out of whatever the user pasted.

    Raises ``ValueError`` when the text is not any of the three understood
    shapes, so the interface can say so rather than reporting "0 cookies" for a
    paste that was simply the wrong thing.
    """
    blob = (text or "").strip()
    if not blob:
        raise ValueError("No hay nada que importar.")

    raw: Any = None
    if blob[0] in "[{":
        try:
            raw = json.loads(blob)
        except json.JSONDecodeError as error:
            raise ValueError(f"El JSON no se puede leer: {error}") from error

    if isinstance(raw, dict):
        # Playwright's storageState, or a single cookie object.
        raw = raw.get("cookies", raw if "name" in raw else None)
    if isinstance(raw, dict):
        raw = [raw]

    if raw is None:
        # A bare "Cookie:" header line, which is what devtools copies.
        header = blob[len("cookie:") :].strip() if blob.lower().startswith("cookie:") else blob
        raw = []
        for pair in header.split(";"):
            name, separator, value = pair.strip().partition("=")
            if separator and name.strip():
                raw.append({"name": name.strip(), "value": value.strip()})
        if not raw:
            raise ValueError(
                "No se reconoce el formato. Pega el JSON de una extensión de cookies, "
                "un storageState de Playwright, o la línea 'Cookie: a=1; b=2'."
            )

    if not isinstance(raw, list):
        raise ValueError("Se esperaba una lista de cookies.")

    cookies = [normalize_cookie(entry, default_domain) for entry in raw]
    usable = [cookie for cookie in cookies if cookie is not None]
    if not usable:
        raise ValueError("Ninguna de las cookies pegadas tiene nombre, valor y dominio.")
    return usable


def import_session(
    marketplace_name: str, cookies: List[Dict[str, Any]], allowed_domains: Iterable[str]
) -> Dict[str, Any]:
    """Store a set of cookies as this marketplace's session.

    Cookies for anywhere else are dropped rather than stored: they would be no
    use to this marketplace and every one of them is credential-equivalent for
    whatever site it did come from.
    """
    allowed = list(allowed_domains)
    kept = [cookie for cookie in cookies if not allowed or domain_allowed(cookie["domain"], allowed)]
    ignored = len(cookies) - len(kept)
    if not kept:
        raise ValueError(
            "Ninguna cookie pertenece a este marketplace"
            + (f" (se esperaban dominios como {allowed[0]})." if allowed else ".")
        )
    written = _write(
        marketplace_name,
        {
            "cookies": kept,
            "origins": [],
            # An imported session is a *seed*, and it has to survive until the
            # browser has actually taken it.  Without this the import lived only
            # in memory: a monitor restarted before it was applied simply lost
            # it, and an established profile is never re-seeded from disk.
            "aimm": {
                "source": "imported",
                "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "applied": False,
            },
        },
    )
    return {
        "ok": written,
        "imported": len(kept),
        "ignored": ignored,
        "domains": sorted({_strip_dot(cookie["domain"]) for cookie in kept}),
    }


def import_is_pending(marketplace_name: str) -> bool:
    """Whether a stored session is waiting to be loaded into a browser.

    True only for a session the user imported and that no browser has taken yet.
    A session written by the monitor itself is already in the profile it came
    from, and replaying it over a live one would be a way to overwrite a good
    session with an older copy of itself.
    """
    state = load_session(marketplace_name) or {}
    meta = state.get("aimm")
    if not isinstance(meta, dict):
        return False
    return meta.get("source") == "imported" and not meta.get("applied")


def rearm_import(marketplace_name: str) -> bool:
    """Mark a stored session as waiting to be loaded again.

    For the two cases where a stored session needs to reach the browser a second
    time: the site logged us out and the cookies are still good, and a file
    imported before this bookkeeping existed, which carries no note saying it
    was ever meant to be applied.  Returns whether there was anything to re-arm.
    """
    state = load_session(marketplace_name)
    if state is None or not state.get("cookies"):
        return False
    meta = state.get("aimm") if isinstance(state.get("aimm"), dict) else {}
    state["aimm"] = {
        **meta,
        "source": "imported",
        "imported_at": meta.get("imported_at")
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "applied": False,
    }
    return _write(marketplace_name, state)


def mark_import_applied(marketplace_name: str) -> None:
    """Note that a browser has taken the imported session, so it is not re-applied."""
    state = load_session(marketplace_name)
    if state is None:
        return
    meta = state.get("aimm")
    if not isinstance(meta, dict):
        return
    meta["applied"] = True
    meta["applied_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state["aimm"] = meta
    _write(marketplace_name, state)


def session_info(marketplace_name: str) -> Dict[str, Any]:
    """What is stored for a marketplace -- never *what* is stored.

    Counts, domains and dates only.  A cookie value is the session itself, and
    an interface that could read one back would be a way to lift it.
    """
    path = session_path(marketplace_name)
    state = load_session(marketplace_name)
    if state is None:
        return {
            "saved": False,
            "cookies": 0,
            "domains": [],
            "saved_at": None,
            "expires_at": None,
            "source": None,
            "pending": False,
        }

    cookies = [cookie for cookie in state.get("cookies") or [] if isinstance(cookie, dict)]
    expiries = [
        float(cookie["expires"])
        for cookie in cookies
        if isinstance(cookie.get("expires"), (int, float)) and float(cookie["expires"]) > 0
    ]
    meta = state.get("aimm") if isinstance(state.get("aimm"), dict) else {}
    return {
        "saved": True,
        "cookies": len(cookies),
        # Where it came from and whether a browser has taken it yet: an import
        # that is still pending is the difference between "nothing happened"
        # and "it will happen when the browser next starts".
        "source": str(meta.get("source") or "browser"),
        "pending": bool(meta.get("source") == "imported" and not meta.get("applied")),
        "domains": sorted({_strip_dot(str(cookie.get("domain") or "")) for cookie in cookies} - {""}),
        "saved_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(
            timespec="seconds"
        )
        if path.exists()
        else None,
        # The furthest-out expiry: the session lasts at most until then, and a
        # date in the past explains a marketplace that started asking again.
        "expires_at": datetime.fromtimestamp(max(expiries), timezone.utc).isoformat(
            timespec="seconds"
        )
        if expiries
        else None,
    }


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
