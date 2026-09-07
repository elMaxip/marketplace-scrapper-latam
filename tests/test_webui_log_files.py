"""Listing, reading and downloading the log files the monitor already wrote.

The console in the interface reads a ring buffer -- the last couple of thousand
records, in memory.  The monitor has also been writing a rotating file all
along: a megabyte each, five backups, roughly six megabytes of history that
until now was reachable only from a shell inside the container.

What these tests hold to:

* the *rotation* is unchanged.  The handler is the same handler with the same
  size and the same backup count; only the formatting of a line changed, and
  that is checked below because a log file with no timestamp cannot answer the
  question anybody opens an old log to ask.
* the order is the *rotation* order, not the modification time.  A file's mtime
  moves when it is rotated into, so ordering by it is right by coincidence.
* a name from the browser is matched against the files that were found, never
  joined to a directory.  The traversal tests are the point of that rule.
"""

from __future__ import annotations

import io
import logging
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterator, List

import pytest

from ai_marketplace_monitor.cli import PlainFileFormatter, _log_file_handler
from ai_marketplace_monitor.utils import hilight, strip_markup
from ai_marketplace_monitor.webui import log_files as module


@pytest.fixture
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A directory with a log and four rotations in it, and no logging set up.

    `base_log_path` falls back to `amm_home` when no handler is installed, which
    is exactly the shape a test wants: the files are written by hand, so what is
    under test is the reading rather than the writing.
    """
    monkeypatch.setattr(module, "amm_home", tmp_path)
    yield tmp_path


def write(directory: Path, name: str, lines: List[str]) -> Path:
    path = directory / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Which files there are
# --------------------------------------------------------------------------- #


def test_only_the_monitors_own_files_are_listed(log_dir: Path) -> None:
    """A directory that also holds the cache, the config and the sessions."""
    write(log_dir, "ai-marketplace-monitor.log", ["now"])
    write(log_dir, "ai-marketplace-monitor.log.1", ["before"])
    write(log_dir, "config.toml", ["[monitor]"])
    write(log_dir, "ai-marketplace-monitor.log.bak", ["not a rotation"])
    write(log_dir, "other.log", ["somebody else's"])

    assert [entry.name for entry in module.log_files()] == [
        "ai-marketplace-monitor.log",
        "ai-marketplace-monitor.log.1",
    ]


def test_the_order_is_the_rotation_not_the_clock(log_dir: Path) -> None:
    """The one that would pass by coincidence if it were sorted by mtime."""
    for rotation in (3, 1, 2):
        write(log_dir, f"ai-marketplace-monitor.log.{rotation}", [str(rotation)])
    write(log_dir, "ai-marketplace-monitor.log", ["current"])

    assert [entry.rotation for entry in module.log_files()] == [0, 1, 2, 3]


def test_exactly_one_file_is_the_current_one(log_dir: Path) -> None:
    write(log_dir, "ai-marketplace-monitor.log", ["now"])
    write(log_dir, "ai-marketplace-monitor.log.1", ["before"])
    files = module.log_files()
    assert [entry.current for entry in files] == [True, False]


def test_a_double_digit_rotation_sorts_as_a_number(log_dir: Path) -> None:
    """Sorted as text, `.log.10` comes before `.log.2`."""
    for rotation in (1, 2, 10):
        write(log_dir, f"ai-marketplace-monitor.log.{rotation}", ["x"])
    assert [entry.rotation for entry in module.log_files()] == [1, 2, 10]


def test_no_directory_is_not_a_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "amm_home", tmp_path / "nowhere")
    assert module.log_files() == []


# --------------------------------------------------------------------------- #
# Asking for one by name
# --------------------------------------------------------------------------- #


def test_a_file_is_found_by_its_own_name(log_dir: Path) -> None:
    write(log_dir, "ai-marketplace-monitor.log.2", ["older"])
    entry = module.find("ai-marketplace-monitor.log.2")
    assert entry is not None and entry.rotation == 2


@pytest.mark.parametrize(
    "name",
    [
        "../config.toml",
        "..\\config.toml",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "ai-marketplace-monitor.log/../../secrets",
        "",
        "config.toml",
    ],
)
def test_nothing_but_a_listed_file_is_reachable(log_dir: Path, name: str) -> None:
    """The whole of the path safety: the name is compared, never joined.

    Worth a parametrised test rather than a comment, because the failure mode is
    a log viewer that reads any file on the machine.
    """
    write(log_dir, "ai-marketplace-monitor.log", ["now"])
    write(log_dir, "config.toml", ["[monitor]\npassword = 'hunter2'"])
    assert module.find(name) is None


# --------------------------------------------------------------------------- #
# Reading one
# --------------------------------------------------------------------------- #


def test_the_tail_is_the_end_of_the_file(log_dir: Path) -> None:
    write(log_dir, "ai-marketplace-monitor.log", [f"line {index}" for index in range(100)])
    entry = module.find("ai-marketplace-monitor.log")
    assert entry is not None
    lines, truncated = module.tail(entry, limit=10)
    assert lines == [f"line {index}" for index in range(90, 100)]
    assert truncated is True


def test_a_short_file_is_not_truncated(log_dir: Path) -> None:
    write(log_dir, "ai-marketplace-monitor.log", ["one", "two"])
    entry = module.find("ai-marketplace-monitor.log")
    assert entry is not None
    assert module.tail(entry, limit=100) == (["one", "two"], False)


def test_a_broken_byte_does_not_hide_the_rest(log_dir: Path) -> None:
    """A run killed mid-write leaves a partial UTF-8 sequence.  Refusing to show
    the other twelve thousand lines because of it is the wrong trade."""
    path = log_dir / "ai-marketplace-monitor.log"
    path.write_bytes("readable line\n".encode("utf-8") + b"\xff\xfe broken\n")
    entry = module.find("ai-marketplace-monitor.log")
    assert entry is not None
    lines, _ = module.tail(entry)
    assert lines[0] == "readable line"
    assert len(lines) == 2


def test_the_limit_is_capped(log_dir: Path) -> None:
    """A megabyte of text as one JSON string is a tab that stops responding."""
    write(log_dir, "ai-marketplace-monitor.log", ["x"])
    entry = module.find("ai-marketplace-monitor.log")
    assert entry is not None
    module.tail(entry, limit=10**9)  # must not raise, must not read for ever


# --------------------------------------------------------------------------- #
# Downloading all of them
# --------------------------------------------------------------------------- #


def test_the_archive_holds_every_file_under_its_own_name(log_dir: Path) -> None:
    write(log_dir, "ai-marketplace-monitor.log", ["now"])
    write(log_dir, "ai-marketplace-monitor.log.1", ["before"])
    write(log_dir, "config.toml", ["not a log"])

    bundle = zipfile.ZipFile(io.BytesIO(module.archive()))
    assert sorted(bundle.namelist()) == [
        "ai-marketplace-monitor.log",
        "ai-marketplace-monitor.log.1",
    ]
    # `splitlines`, not an exact compare: the fixture writes with the platform's
    # own newline, and what is under test is that the archive carries the file's
    # bytes rather than a re-encoding of them.
    assert bundle.read("ai-marketplace-monitor.log.1").decode().splitlines() == ["before"]


def test_an_empty_directory_still_makes_a_valid_zip(log_dir: Path) -> None:
    """The button is offered before the monitor has written anything."""
    bundle = zipfile.ZipFile(io.BytesIO(module.archive()))
    assert bundle.namelist() == []


# --------------------------------------------------------------------------- #
# What goes into a line, and the rotation that was already there
# --------------------------------------------------------------------------- #


def test_the_handler_still_rotates_the_way_it_did(tmp_path: Path) -> None:
    """The rotation is not what changed, and this is where that is pinned."""
    handler = _log_file_handler()
    try:
        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == 1024 * 1024
        assert handler.backupCount == 5
    finally:
        handler.close()


def test_a_written_line_carries_a_time_and_a_level(tmp_path: Path) -> None:
    """The file used to be `%(message)s` and nothing else: no time, no level.

    Serving that file to a reader would have been serving them a list of
    sentences with no way to tell when any of them happened.
    """
    record = logging.LogRecord(
        name="monitor",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg=f"{hilight('[Search]', 'succ')} looking for a ps5",
        args=(),
        exc_info=None,
    )
    line = PlainFileFormatter().format(record)
    assert " WARNING " in line
    assert line.endswith("[Search] looking for a ps5")
    assert "[green]" not in line


def test_the_rotation_and_the_reading_meet(tmp_path: Path) -> None:
    """End to end, through the real handler: write enough to rotate, then list,
    read and archive what came out."""
    path = tmp_path / "ai-marketplace-monitor.log"
    handler = RotatingFileHandler(path, encoding="utf-8", maxBytes=400, backupCount=5)
    handler.setFormatter(PlainFileFormatter())
    logger = logging.getLogger("test-log-files")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.handlers = [handler]
    try:
        for index in range(40):
            logger.warning(f"{hilight('[Search]', 'info')} line {index}")
        handler.flush()

        # `base_log_path` finds the handler on the *root* logger, so point it
        # at the directory instead -- which is the fallback path a test uses.
        original = module.amm_home
        module.amm_home = tmp_path
        try:
            files = module.log_files()
            assert len(files) > 1, "the handler should have rotated"
            assert files[0].current and files[0].rotation == 0
            lines, _ = module.tail(files[0])
            assert lines[-1].endswith("[Search] line 39")
            assert all("[blue]" not in line for line in lines)
            bundle = zipfile.ZipFile(io.BytesIO(module.archive()))
            assert len(bundle.namelist()) == len(files)
        finally:
            module.amm_home = original
    finally:
        logger.handlers = []
        handler.close()


def test_old_files_can_still_be_read(log_dir: Path) -> None:
    """Five rotations survive a deploy, so on upgrade day half the history still
    has Rich's tags in it.  Stripping is applied on the way out, not only on the
    way in."""
    write(log_dir, "ai-marketplace-monitor.log.1", ["[blue][Pause][/blue] paused"])
    entry = module.find("ai-marketplace-monitor.log.1")
    assert entry is not None
    lines, _ = module.tail(entry)
    assert strip_markup(lines[0]) == "[Pause] paused"


# --------------------------------------------------------------------------- #
# Through the API
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(log_dir: Path):
    """The real app, with the log directory pointed at a temporary one.

    Open mode (no exposure), like the other web UI tests: what is under test is
    the log endpoints, and the session machinery has its own suite.
    """
    from fastapi.testclient import TestClient

    from ai_marketplace_monitor.webui.config_api import ConfigFileService
    from ai_marketplace_monitor.webui.log_handler import LogBroadcastHandler
    from ai_marketplace_monitor.webui.server import AuthState, WebUIConfig, create_app

    write(log_dir, "ai-marketplace-monitor.log", ["current line"])
    write(log_dir, "ai-marketplace-monitor.log.1", ["older line"])
    config_file = log_dir / "config.toml"
    config_file.write_text(
        "[marketplace.facebook]\nsearch_city = 'dallas'\n", encoding="utf-8"
    )
    handler = LogBroadcastHandler()
    state = AuthState()
    state.exposed = False
    app = create_app(
        WebUIConfig(config_files=[config_file], log_handler=handler),
        state,
        ConfigFileService([config_file]),
        handler,
    )
    return TestClient(app)


def test_the_api_lists_the_files(client) -> None:
    answer = client.get("/api/logs/files").json()
    assert [entry["name"] for entry in answer["files"]] == [
        "ai-marketplace-monitor.log",
        "ai-marketplace-monitor.log.1",
    ]
    assert answer["current"] == "ai-marketplace-monitor.log"


def test_the_api_reads_one_file(client) -> None:
    answer = client.get("/api/logs/file?name=ai-marketplace-monitor.log.1").json()
    assert answer["lines"] == ["older line"]
    assert answer["file"]["rotation"] == 1


def test_the_api_refuses_a_name_it_did_not_list(client) -> None:
    assert client.get("/api/logs/file?name=../config.toml").status_code == 404
    assert client.get("/api/logs/file?name=config.toml").status_code == 404


def test_the_api_hands_over_a_zip(client) -> None:
    answer = client.get("/api/logs/download")
    assert answer.status_code == 200
    assert answer.headers["content-type"] == "application/zip"
    assert "attachment" in answer.headers["content-disposition"]
    bundle = zipfile.ZipFile(io.BytesIO(answer.content))
    assert sorted(bundle.namelist()) == [
        "ai-marketplace-monitor.log",
        "ai-marketplace-monitor.log.1",
    ]
