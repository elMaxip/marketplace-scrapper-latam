"""The log *files*, as opposed to the log stream.

The console in the interface reads a ring buffer: the last couple of thousand
records, held in memory, fanned out over a socket.  That is the right thing for
watching a scrape happen and the wrong thing for every question that starts with
"what happened last night" -- the buffer holds minutes, and the answer rotated
out of it long before anybody thought to look.

The answer is on disk.  ``cli.py`` has always installed a
``RotatingFileHandler``: one megabyte a file, five rotations kept, so
``ai-marketplace-monitor.log`` plus ``.log.1`` through ``.log.5``.  Nothing ever
exposed them, so the six megabytes of history the monitor was already writing
were reachable only by opening a shell in the container.

Two decisions worth stating, because both could reasonably have gone the other
way:

**The handler is asked, not assumed.**  The path is read off the handler that is
actually installed rather than rebuilt from ``amm_home`` here.  A second copy of
"where the log lives" is a second thing to keep in step with the first, and the
one that matters is the one the process is really writing to -- which is what
makes this work unchanged in the container, where ``HOME`` is somewhere else
entirely.

**Names are matched against the list, never joined to a path.**  Everything a
request can name is checked by exact equality against the files this module
found for itself.  A log viewer that takes a filename from the browser and joins
it to a directory is a file-read primitive for the whole machine, and no amount
of ``..`` filtering is as convincing as never building the path at all.
"""

from __future__ import annotations

import io
import logging
import os
import zipfile
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from ..utils import amm_home, strip_markup

#: What the monitor calls its log, when nothing better can be worked out.
DEFAULT_LOG_NAME = "ai-marketplace-monitor.log"

#: How many lines one request may take away.  The whole point of the file is
#: that it is bigger than the buffer, so this is generous -- but a megabyte of
#: text arriving as one JSON string is a browser tab that stops responding.
DEFAULT_LINES = 2000
MAX_LINES = 20000

#: Re-exported: a file written before the plain formatter landed still carries
#: Rich's tags, so whatever shows it has to be able to take them out.
__all__ = [
    "DEFAULT_LINES",
    "LogFile",
    "archive",
    "base_log_path",
    "find",
    "log_files",
    "strip_markup",
    "tail",
]


@dataclass(frozen=True)
class LogFile:
    """One file on disk, and where it sits in the rotation."""

    #: The file's own name -- also the token a request uses to ask for it.
    name: str
    path: Path
    size: int
    #: Seconds since the epoch, as the filesystem reports them.
    modified: float
    #: Whether this is the file the monitor is writing to right now.
    current: bool
    #: 0 for the live file, 1 for the most recently rotated, and so on.  What
    #: lets the interface say "el anterior" instead of printing ".log.3".
    rotation: int

    def as_dict(self: "LogFile") -> Dict[str, Any]:
        return {
            "name": self.name,
            "size": self.size,
            "modified": self.modified,
            "current": self.current,
            "rotation": self.rotation,
        }


def _handlers() -> List[RotatingFileHandler]:
    """Every rotating file handler installed on the root logger."""
    return [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, RotatingFileHandler) and getattr(handler, "baseFilename", None)
    ]


def base_log_path() -> Path:
    """The file the monitor is writing to.

    From the installed handler when there is one, and from ``amm_home``
    otherwise -- which is the case in a test, and in any process that imports
    this without having set logging up.
    """
    for handler in _handlers():
        return Path(handler.baseFilename)
    return amm_home / DEFAULT_LOG_NAME


def _rotation_of(name: str, base: str) -> int | None:
    """Which rotation ``name`` is, or ``None`` when it is not one of ours.

    ``RotatingFileHandler`` names its backups by appending ``.1``, ``.2`` and so
    on to the base name, so the whole family is decided by the base name plus a
    number -- nothing else in the directory qualifies, whatever it is called.
    """
    if name == base:
        return 0
    if not name.startswith(base + "."):
        return None
    suffix = name[len(base) + 1 :]
    return int(suffix) if suffix.isdigit() else None


def log_files() -> List[LogFile]:
    """The log and its rotations, newest first.

    Ordered by rotation rather than by modification time on purpose: a file's
    mtime moves when it is *rotated into*, so sorting by it puts the files in
    the right order only by coincidence, and a directory copied around loses
    even that.  The rotation number is the sequence, and it says so.
    """
    base_path = base_log_path()
    directory = base_path.parent
    base = base_path.name
    found: List[Tuple[int, LogFile]] = []
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return []
    for name in entries:
        rotation = _rotation_of(name, base)
        if rotation is None:
            continue
        path = directory / name
        try:
            stat = path.stat()
        except OSError:
            continue
        if not path.is_file():
            continue
        found.append(
            (
                rotation,
                LogFile(
                    name=name,
                    path=path,
                    size=stat.st_size,
                    modified=stat.st_mtime,
                    current=rotation == 0,
                    rotation=rotation,
                ),
            )
        )
    found.sort(key=lambda pair: pair[0])
    return [entry for _rotation, entry in found]


def find(name: str) -> LogFile | None:
    """The file called ``name``, or nothing.

    Exact equality against the list this module built, which is the whole of the
    path safety here -- see the module docstring.
    """
    for entry in log_files():
        if entry.name == name:
            return entry
    return None


def tail(entry: LogFile, limit: int = DEFAULT_LINES) -> Tuple[List[str], bool]:
    """The last ``limit`` lines of one file, and whether anything was left out.

    Read whole rather than seeked backwards from the end: the files are capped
    at a megabyte by the handler that writes them, so the simple version costs a
    megabyte of memory for the length of one request, and the seeking version
    costs a second implementation of "where does a line start" that has to be
    right about a multi-byte character split across a chunk boundary.

    ``errors="replace"`` because a log file is not a document: a run killed
    mid-write leaves a partial UTF-8 sequence at the end, and refusing to show
    the other 12,000 lines because of it is the wrong trade.
    """
    limit = max(1, min(int(limit), MAX_LINES))
    try:
        text = entry.path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], False
    lines = text.splitlines()
    if len(lines) <= limit:
        return lines, False
    return lines[-limit:], True


def archive(entries: Sequence[LogFile] | None = None) -> bytes:
    """Every log file in one zip, for handing to somebody else.

    A zip rather than a concatenation because the rotation boundaries are
    information: "this is where the file the monitor was writing began" is
    exactly what you want to know when two runs are being compared.

    Built in memory: the six megabytes the rotation is capped at compress to
    well under one, and a temporary file would have to be cleaned up on a path
    that includes the client disconnecting halfway.
    """
    chosen = list(log_files() if entries is None else entries)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for entry in chosen:
            try:
                bundle.write(entry.path, arcname=entry.name)
            except OSError:
                # One unreadable file must not cost the others; the archive
                # simply does not carry it.
                continue
    return buffer.getvalue()
