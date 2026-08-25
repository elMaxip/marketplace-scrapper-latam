"""Tests for `ai_marketplace_monitor`.cli module."""

from dataclasses import asdict
from typing import Callable, List, Tuple, Type, Union

import pytest
from pytest import TempPathFactory
from typer.testing import CliRunner

import ai_marketplace_monitor
from ai_marketplace_monitor import cli
from ai_marketplace_monitor.config import Config

runner = CliRunner()


@pytest.mark.parametrize(
    "options,expected",
    [
        # ([], "ai_marketplace_monitor.cli.main"),
        (["--help"], "Usage: "),
        (
            ["--version"],
            f"AI Marketplace Monitor, paquete {ai_marketplace_monitor.__version__} "
            "(sin tag de imagen)\n",
        ),
    ],
)
def test_command_line_interface(
    options: List[str], expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test the CLI."""
    # Outside a container there is no tag, and that is the case being asserted:
    # a stray AIMM_VERSION in the developer's shell would otherwise silently
    # switch the branch under the test.
    monkeypatch.delenv(ai_marketplace_monitor.VERSION_ENV, raising=False)
    result = runner.invoke(cli.app, options)
    assert result.exit_code == 0
    assert expected in result.stdout


def test_version_prefers_image_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tag the image was built from wins over pyproject's number.

    The two disagree on purpose in this fork — releases are tagged v1.x while
    pyproject still tracks upstream's 0.10.x — so reporting the package number
    made an updated container look like it had not updated.
    """
    monkeypatch.setenv(ai_marketplace_monitor.VERSION_ENV, "1.0.3")
    assert ai_marketplace_monitor.app_version() == ("1.0.3", "tag")
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert "AI Marketplace Monitor 1.0.3" in result.stdout


def test_version_falls_back_to_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty build arg is not a version: a local build must not invent one."""
    monkeypatch.setenv(ai_marketplace_monitor.VERSION_ENV, "  ")
    value, source = ai_marketplace_monitor.app_version()
    assert source == "package"
    assert value == ai_marketplace_monitor.__version__


@pytest.fixture(scope="session")
def config_file(tmp_path_factory: TempPathFactory) -> Callable:
    def generate_config_file(content: str) -> str:
        fn = tmp_path_factory.mktemp("config") / "test.toml"
        with open(fn, "w") as f:
            f.write(content)
        return str(fn)

    return generate_config_file


base_marketplace_cfg = """
[marketplace.facebook]
search_city = 'dallas'
"""

# A saved region, defined here because nothing ships one any more: the packaged
# config.toml used to carry a dozen `[region.*]` blocks and no longer does. A
# config that names a region has to define it, which is exactly what the web UI
# writes when the user saves one.
region_cfg = """
[region.usa]
full_name = "USA (test)"
radius = 500
city_name = ["Houston", "Dallas"]
search_city = ["houston", "dallas"]
currency = "USD"
"""

full_marketplace_cfg = """
[marketplace.facebook]
login_wait_time = 50
password = "password"
search_city = ['houston']
username = "username"
# the following are common options
seller_locations = "city"
condition = ['new', 'used_good']
date_listed = 7
delivery_method = 'local_pick_up'
exclude_sellers = "seller"
ai = []
currency = 'USD'
max_price = '300 EUR'
min_price = 200
rating = 4
max_search_interval = 40
notify = 'user1'
radius = 100
search_interval = 10
search_region = 'usa'
"""

base_item_cfg = """
[item.name]
search_phrases = 'search word one'
"""

full_item_cfg = """
[item.name]
antikeywords = ['exclude1', 'exclude2']
description = 'long description'
enabled = true
keywords = ['exclude1', 'exclude2']
search_phrases = 'search word one'
marketplace = 'facebook'
search_city = 'houston'
# the following are common options
seller_locations = "city"
condition = ['new', 'used_good']
date_listed = 7
ai = 'openai'
availability = ['out', 'all']
delivery_method = 'local_pick_up'
exclude_sellers = "seller"
max_price = 300
rating = 4
max_search_interval = '1d'
search_interval = '12h'
min_price = 200
notify = 'user1'
radius = 100
search_region = 'usa'
"""

base_user_cfg = """
[user.user1]
pushbullet_token = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
"""

full_user_cfg = """
[user.user1]
pushbullet_token = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
remind = '1 day'

[user.user2]
pushbullet_token = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
"""


base_ai_cfg = """
[ai.openai]
api_key = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
"""

full_ai_cfg = """
[ai.openai]
api_key = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
model = 'gpt'
"""

base_pushbullet_cfg = """
[notification.pushbullet1]
pushbullet_token = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
"""

base_pushover_cfg = """
[notification.pushover]
pushover_user_key = "xxxxxx"
pushover_api_token = "dfafdafd"
"""


base_ntfy_cfg = """
[notification.ntfy]
ntfy_server = "https://xxxxxx"
ntfy_topic = "dfafdafd"
"""

base_email_cfg = """
[notification.gmail]
smtp_password = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
"""

notify_user_cfg = """
[user.user1]
notify_with = ['pushbullet1', 'gmail']
"""

notify_user_pushover_cfg = """
[user.user1]
notify_with = 'pushover'
"""

notify_user_ntfy_cfg = """
[user.user1]
notify_with = 'ntfy'
"""

monitor_cfg = """
[monitor]
proxy_server = 'https://fdaf.fadfd.com'
proxy_username = 'adfa'
proxy_password = 'fadfadf'
"""

licensed_monitor_cfg = """
[monitor]
proxy_server = 'https://fdaf.fadfd.com'
proxy_username = 'adfa'
proxy_password = 'fadfadf'
"""


@pytest.mark.parametrize(
    "config_content,acceptable",
    [
        # No section is required any more.  The platforms are built in, a
        # monitor with no searches is idle rather than broken, and one with
        # nobody to notify still searches and stores what it finds -- so each
        # of these on its own is a configuration, just not an interesting one.
        (base_marketplace_cfg, True),
        # Still refused, and not for a missing section: a Facebook search has
        # to say where to search from, and this one names no city and no region.
        (base_item_cfg, False),
        (base_user_cfg, True),
        (base_marketplace_cfg + base_item_cfg + base_user_cfg, True),
        (base_marketplace_cfg + base_item_cfg + base_user_cfg + base_ai_cfg, True),
        (
            region_cfg + full_marketplace_cfg + full_item_cfg + full_user_cfg + full_ai_cfg,
            True,
        ),
        (region_cfg + base_marketplace_cfg + full_item_cfg + base_user_cfg + base_ai_cfg, True),
        # notification should match
        (
            region_cfg + base_marketplace_cfg + full_item_cfg + notify_user_cfg,
            False,
        ),
        # pushbullet
        (
            base_marketplace_cfg
            + base_item_cfg
            + notify_user_cfg
            + base_pushbullet_cfg
            + base_email_cfg,
            True,
        ),
        (
            base_marketplace_cfg
            + base_item_cfg
            + notify_user_cfg
            + base_pushbullet_cfg.replace("pushbullet1", "somethingelse")
            + base_email_cfg,
            False,
        ),
        # pushover
        (
            base_marketplace_cfg + base_item_cfg + notify_user_pushover_cfg + base_pushover_cfg,
            True,
        ),
        (
            base_marketplace_cfg
            + base_item_cfg
            + notify_user_pushover_cfg
            + base_pushover_cfg.replace("pushover", "somethingelse"),
            False,
        ),
        # ntfy
        (
            base_marketplace_cfg + base_item_cfg + notify_user_ntfy_cfg + base_ntfy_cfg,
            True,
        ),
        (
            base_marketplace_cfg
            + base_item_cfg
            + notify_user_ntfy_cfg
            + base_ntfy_cfg.replace("ntfy", "somethingelse"),
            False,
        ),
        # user should match
        (
            region_cfg
            + base_marketplace_cfg
            + full_item_cfg.replace("user1", "unknown_user")
            + base_user_cfg,
            False,
        ),
        # no additional keys
        (base_marketplace_cfg + "\na=1\n" + base_item_cfg + base_user_cfg, False),
        (base_marketplace_cfg + base_item_cfg + "\na=1\n" + base_user_cfg, False),
        (base_marketplace_cfg + base_item_cfg + base_user_cfg + "\na=1\n", False),
        (base_marketplace_cfg + base_item_cfg + base_user_cfg + monitor_cfg, True),
        (base_marketplace_cfg + base_item_cfg + base_user_cfg + licensed_monitor_cfg, True),
    ],
)
def test_config(config_file: Callable, config_content: str, acceptable: bool) -> None:
    """Test the config command."""
    cfg = config_file(config_content)
    key_types: dict[str, Union[Type, Tuple[Type, ...]]] = {
        "seller_locations": (list, type(None)),
        "ai": (list, type(None)),
        "availability": (list, type(None)),
        "api_key": str,
        "category": (str, type(None)),
        "city_name": (list, type(None)),
        "condition": (list, type(None)),
        "currency": (list, type(None)),
        "date_listed": (list, type(None)),
        "delivery_method": (list, type(None)),
        "description": (str, type(None)),
        "enabled": (bool, type(None)),
        "antikeywords": (list, type(None)),
        "exclude_sellers": (list, type(None)),
        "keywords": (list, type(None)),
        "language": (str, type(None)),
        "login_wait_time": (int, type(None)),
        "marketplace": (str, type(None)),
        "max_price": (str, type(None)),
        "max_search_interval": (int, type(None)),
        "market_type": (str, type(None)),
        "min_price": (str, type(None)),
        "model": (str, type(None)),
        "monitor_config": dict,
        "name": (str, type(None)),
        "notify": (list, type(None)),
        "password": (str, type(None)),
        "prompt": (str, type(None)),
        "extra_prompt": (str, type(None)),
        "rating_prompt": (str, type(None)),
        "pushbullet_token": str,
        "radius": (list, type(None)),
        "rating": (list, type(None)),
        "remind": (int, type(None)),
        "search_city": (list, type(None)),
        "search_interval": (int, type(None)),
        "search_phrases": list,
        "search_region": (list, type(None)),
        "searched_count": int,
        "sort_by": (str, type(None)),
        "start_at": (list, type(None)),
        "target_price": (str, type(None)),
        "username": (str, type(None)),
    }
    if acceptable:
        # print(config_content)
        config = Config([cfg])
        # assert the types
        for key, value in asdict(config.marketplace["facebook"]).items():
            assert isinstance(value, key_types[key]), f"{key} must be of type {key_types[key]}"

        for item_cfg in config.item.values():
            for key, value in asdict(item_cfg).items():
                assert isinstance(value, key_types[key]), f"{key} must be of type {key_types[key]}"
        # test if all elements can be frozen
        for attr in ("item", "ai", "user", "marketplae"):
            for item_cfg in getattr(config, attr, {}).values():
                assert item_cfg.hash
    else:
        with pytest.raises(Exception):
            Config([cfg])


alt_marketplace_cfg = """
[marketplace.houston]
search_city = 'houston'
"""

alt_item_cfg = """
[item.whatever]
marketplace = "houston"
search_phrases = "search word two"
"""


def test_support_multiple_marketplaces(config_file: Callable) -> None:
    """Test the config command."""
    cfg = config_file(
        base_marketplace_cfg + alt_marketplace_cfg + alt_item_cfg + base_item_cfg + base_user_cfg
    )
    config = Config([cfg])

    # The two built-in platforms, plus the extra section this file declares.
    assert sorted(config.marketplace) == ["facebook", "houston", "mercadolibre"]
    assert len(config.item) == 2
    assert len(config.user) == 1

    assert config.item["name"].marketplace == "facebook"
    assert config.item["whatever"].marketplace == "houston"
    assert config.marketplace["facebook"].search_city == ["dallas"]
    assert config.marketplace["houston"].search_city == ["houston"]


alt_ai_cfg = """
[ai.some_ai]
provider = 'OpenAI'
api_key = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
model = 'gpt_none'
base_url = 'http://someother.com'
"""


def test_multiplace_ai_agent(config_file: Callable) -> None:
    """Test the config command."""
    cfg = config_file(
        base_marketplace_cfg + base_ai_cfg + base_item_cfg + alt_ai_cfg + base_user_cfg
    )
    config = Config([cfg])

    # Every platform the monitor supports, whether or not the file names it.
    assert sorted(config.marketplace) == ["facebook", "mercadolibre"]
    assert len(config.ai) == 2

    assert config.ai["openai"].api_key == "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    assert config.ai["some_ai"].model == "gpt_none"


currency_item_cfg = """
[item.name]
search_phrases = 'search word one'
max_price = '300 USD'
search_city = 'paris'
currency = 'EUR'
"""


def test_price_conversion(config_file: Callable) -> None:
    """Test the config command."""
    cfg = config_file(base_marketplace_cfg + base_ai_cfg + currency_item_cfg + base_user_cfg)
    config = Config([cfg])

    assert config.item["name"].max_price == "300 USD"
    assert config.item["name"].currency == ["EUR"]


def test_the_telegram_library_cannot_log_the_bot_token() -> None:
    """The token is in the URL the library announces at DEBUG.

    `python-telegram-bot` logs "Set Bot API URL: https://api.telegram.org/
    bot<token>" and then every call's parameters, chat id included.  The root
    logger is at DEBUG whether or not `--verbose` was passed, so that went into
    the log file in clear text -- and out over the web UI's log websocket to
    anyone with the interface open.
    """
    import logging

    from ai_marketplace_monitor.cli import _silence_noisy_loggers

    _silence_noisy_loggers()
    for name in ("telegram", "telegram.ext", "telegram.request"):
        assert logging.getLogger(name).level == logging.ERROR, name
    # A record the library would have emitted is now below the bar.
    assert not logging.getLogger("telegram.request").isEnabledFor(logging.DEBUG)
