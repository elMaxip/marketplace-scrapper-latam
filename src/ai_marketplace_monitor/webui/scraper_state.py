"""One answer to "what is the scraper doing, and on which configuration?".

Three things are kept apart here on purpose, because conflating them is how an
interface ends up lying:

**Persisted** -- what is on disk, which is what the user last saved.  Its
version is the hash of the configuration files as they are right now.

**Loaded** -- what the scraping thread actually took up, and when.  Its version
is the hash of the same files as they were *when it read them*.  The two being
equal is the only unambiguous answer to "has my change reached the scraper
yet?"; a timestamp written by the browser answers a different question, and a
"saved!" message that appears before the monitor has reloaded is exactly the
kind of confident wrong answer this module exists to prevent.

**Runtime** -- what is happening: the phase, the search under way, when each
search last ran and runs next, how the listing re-checks are getting on.

**Applied** -- the last change the loop took up *while running*, and what it
cost: which searches were added, removed or edited, and whether one had to be
abandoned half way because the user deleted it.  Two equal hashes say a change
landed; only this says *which* change, which is what the user who pressed save
is actually asking.

Nothing here computes state.  Every value is read from
:mod:`ai_marketplace_monitor.control`, which the scraping thread writes, or
from the files themselves.  The only work done here is masking secrets, which
is this layer's business because this is the layer that talks to a browser.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .. import control
from ..pause import pause_state, run_state
from ..utils import calculate_file_hash
from .secrets_redact import redact_tree


def persisted_version(config_files: List[Path]) -> Dict[str, Any]:
    """The version of the configuration as it sits on disk right now.

    A file that has gone missing is reported as such rather than raised: the
    interface asking "are we in sync?" should get an answer, and "the file the
    monitor was told to read is not there" is one.
    """
    try:
        return {
            "version": calculate_file_hash(config_files),
            "files": [str(path) for path in config_files],
            "saved_at": max(path.stat().st_mtime for path in config_files),
            "error": None,
        }
    except FileNotFoundError as e:
        return {"version": None, "files": [str(p) for p in config_files], "saved_at": None,
                "error": str(e)}
    except OSError as e:  # pragma: no cover - unreadable file, same shape of answer
        return {"version": None, "files": [str(p) for p in config_files], "saved_at": None,
                "error": str(e)}


def _sync(persisted: Dict[str, Any], loaded: Dict[str, Any] | None) -> Dict[str, Any]:
    """Whether the scraper is running the configuration that is on disk.

    ``unknown`` is a real answer and is kept distinct from ``stale``: before the
    first load there is nothing to compare, and reporting that as "pending"
    would put a warning on a monitor that has done nothing wrong.
    """
    saved = persisted.get("version")
    running = (loaded or {}).get("version")
    if saved is None or running is None:
        status = "unknown"
    elif saved == running:
        status = "current"
    else:
        status = "stale"
    return {
        "status": status,
        "saved_version": saved,
        "loaded_version": running,
        "saved_at": persisted.get("saved_at"),
        "loaded_at": (loaded or {}).get("loaded_at"),
    }


def config_sync(config_files: List[Path]) -> Dict[str, Any]:
    """Just the saved-versus-loaded comparison, for the cheap status poll.

    Every screen polls ``/api/status``, and "the scraper has not taken up your
    change yet" is worth saying wherever the user happens to be looking -- and
    so is its counterpart, "it has, and here is what it did with it".  Both are
    small; the resolved configuration behind them is not, and stays in
    :func:`scraper_state`.
    """
    return {
        **_sync(persisted_version(config_files), control.loaded_config()),
        "applied": control.config_applied(),
    }


def _searches(loaded: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    """The searches the scraper holds, each with what it has done so far.

    The rows come from the configuration the scraping thread *loaded*, not from
    the file -- so a search saved a moment ago is absent until the reload, which
    is the honest answer, and a search the user deleted disappears only once the
    scraper has stopped running it.

    Runtime history is folded in on top.  A pair that has never run still
    appears, with no last run and whatever next run the scheduler has for it:
    "configured but not yet run" is a state worth being able to see.
    """
    history = {
        (str(entry.get("item")), str(entry.get("marketplace"))): entry
        for entry in control.searches()
    }
    rows: List[Dict[str, Any]] = []
    for search in (loaded or {}).get("searches") or []:
        key = (str(search.get("item")), str(search.get("marketplace")))
        runtime = history.pop(key, {})
        # A pair with no history has no runtime row, and used to arrive with no
        # next run either -- so a search the scheduler holds a slot for read as
        # "sin programar" for the whole first interval after every restart.
        # The scheduler's own answer is the fallback.
        next_run = runtime.get("next_run") or control.next_run_for(*key)
        rows.append(
            {
                **search,
                "options": redact_tree(search.get("options") or {}),
                "running": bool(runtime.get("running")),
                "started_at": runtime.get("started_at"),
                "last_started_at": runtime.get("last_started_at"),
                "last_finished_at": runtime.get("last_finished_at"),
                "last_outcome": runtime.get("last_outcome"),
                "last_found": runtime.get("last_found"),
                "next_run": next_run,
                # Carried with the timestamp, never without it.  `control`
                # computes it and this row used to drop it, which left the
                # interface with a slot in the past and no way to know that
                # "in the past" meant "waiting its turn" -- so it rendered
                # "Próxima ejecución: en cualquier momento", which is the one
                # thing a next run cannot be.  Worked out here rather than
                # taken from the runtime row, because the timestamp above may
                # have come from the scheduler instead of from that row.
                "due_now": bool(
                    control.has_passed(next_run) and not runtime.get("running")
                ),
            }
        )
    # A pair still running that the reload no longer knows about: the search was
    # deleted while it was under way.  It is shown, marked, until it ends --
    # hiding it would claim the scraper had stopped doing something it is still
    # doing.
    for (item, marketplace), runtime in history.items():
        if not runtime.get("running"):
            continue
        rows.append(
            {
                "item": item,
                "marketplace": marketplace,
                "enabled": False,
                "removed": True,
                "search_phrases": [],
                "options": {},
                **{
                    key: runtime.get(key)
                    for key in (
                        "running",
                        "started_at",
                        "last_started_at",
                        "last_finished_at",
                        "last_outcome",
                        "last_found",
                        "next_run",
                        "due_now",
                    )
                },
            }
        )
    return rows


def scraper_state(config_files: List[Path]) -> Dict[str, Any]:
    """Everything the "Estado del scraper" screen shows, in one read."""
    loaded = control.loaded_config()
    persisted = persisted_version(config_files)

    effective: Dict[str, Any] | None = None
    if loaded is not None:
        effective = {
            key: redact_tree(value)
            for key, value in loaded.items()
            # `searches` is served resolved, one row per pair, in its own key
            # below; repeating it here would be the same fact in two places.
            if key not in ("version", "loaded_at", "searches")
        }

    searches = _searches(loaded)
    return {
        "phase": control.phase(),
        "phases": list(control.PHASES),
        "pause": pause_state(),
        # The same three-way answer /api/status carries, so a screen reading
        # this one does not have to reconstruct it from two booleans.
        "run_state": run_state(),
        "scraping": control.state(),
        "config": {
            **_sync(persisted, loaded),
            # What the scraper did about the last change, in its own words.
            # "Current" only says the versions match; this says which change
            # landed, what was in it, and whether a search was dropped for it.
            "applied": control.config_applied(),
            "files": persisted.get("files", []),
            "error": persisted.get("error"),
            # What the scraper is running, resolved: every default applied and
            # every inherited option folded in, which is what makes a
            # disagreement with the saved file something the user can see
            # rather than something they have to take on trust.
            "effective": effective,
        },
        "searches": searches,
        # Counted from the same list the interface is shown, so the headline
        # figure and the rows can never disagree.
        "search_count": len({str(row["item"]) for row in searches if row.get("enabled")}),
        "active_count": sum(1 for row in searches if row.get("running")),
        "updates": control.updates(),
    }
