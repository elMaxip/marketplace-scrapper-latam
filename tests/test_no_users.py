"""Cero usuarios es un estado válido.

A recipient is someone to *tell*.  Having none means nobody is told; it does not
mean the monitor stops looking, and it certainly does not mean what it finds is
thrown away.  That distinction had been lost in one line: the check for "has
everyone already been notified about this listing?" was an ``all()`` over the
recipients, and ``all()`` of nothing is true -- so a monitor with no users
decided every listing it found was old news and dropped the lot.

The first-run template also used to invent a ``[user.me]`` with no notification
channel in it, purely so the loader's "there must be a user section" rule would
pass.  Both are gone: the section is optional and the template creates nothing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterator, List

import pytest

from ai_marketplace_monitor import control
from ai_marketplace_monitor.ai import AIResponse
from ai_marketplace_monitor.config import Config
from ai_marketplace_monitor.listing import Listing
from ai_marketplace_monitor.monitor import MarketplaceMonitor

NO_USERS = """
[item.ps5]
search_phrases = "playstation 5"

[item.ps5.facebook]
search_city = "santiago"
"""


@pytest.fixture(autouse=True)
def clean_control() -> Iterator[None]:
    control.reset_for_tests()
    yield
    control.reset_for_tests()


def listing(number: str = "1") -> Listing:
    return Listing(
        marketplace="facebook",
        name="ps5",
        id=number,
        title=f"PlayStation 5 ({number})",
        image="",
        price="$300",
        post_url=f"https://www.facebook.com/marketplace/item/{number}",
        location="santiago",
        seller="alguien",
        condition="Used",
        description="una consola",
    )


def test_a_config_with_no_users_loads(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(NO_USERS, encoding="utf-8")
    config = Config([path])
    assert config.user == {}
    assert ("facebook", "ps5") in config.items


def test_found_listings_are_kept_when_there_is_nobody_to_notify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug this pins: ``all([])`` is ``True``.

    Read as "everyone has already been notified", it made a monitor with no
    recipients skip every listing it found -- the search ran, the page was
    fetched, and nothing was ever recorded.
    """
    path = tmp_path / "config.toml"
    path.write_text(NO_USERS, encoding="utf-8")

    monitor = MarketplaceMonitor.__new__(MarketplaceMonitor)
    monitor.config = Config([path])
    monitor.logger = logging.getLogger("test-no-users")
    monitor.ai_agents = []

    marketplace_config = monitor.config.marketplace["facebook"]
    item_config = monitor.config.items[("facebook", "ps5")]

    found = [listing("1"), listing("2")]

    class FakeMarketplace:
        def search(self, _item: Any) -> Iterator[Listing]:
            yield from found

    rated: List[Listing] = []

    monkeypatch.setattr(
        monitor, "evaluate_by_ai", lambda *args, **kwargs: AIResponse(score=5, comment="ok")
    )
    monkeypatch.setattr(
        "ai_marketplace_monitor.monitor.record_rating",
        lambda item, **kwargs: rated.append(item),
    )
    monkeypatch.setattr("ai_marketplace_monitor.monitor.time.sleep", lambda _seconds: None)

    monitor._search_item(marketplace_config, FakeMarketplace(), item_config)

    # Every listing reached the point where it is judged and recorded, rather
    # than being dropped as "already notified" before it got there.
    assert [item.id for item in rated] == ["1", "2"]
    assert control.searches()[0]["last_found"] == 2


def test_the_web_ui_can_delete_the_last_user(tmp_path: Path) -> None:
    """What the interface's delete produces: a file with no user section."""
    from ai_marketplace_monitor.webui.config_api import ConfigFileService

    path = tmp_path / "config.toml"
    path.write_text(NO_USERS + '\n[user.me]\npushbullet_token = "x"\n', encoding="utf-8")

    service = ConfigFileService([path])
    content, mtime = service.read("primary")
    _mtime, ok, error = service.write("primary", content.split("[user.me]")[0], base_mtime=mtime)

    assert ok, error
    assert "[user." not in path.read_text(encoding="utf-8")
