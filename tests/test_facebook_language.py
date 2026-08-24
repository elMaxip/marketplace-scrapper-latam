"""The Facebook interface language has a default, and an empty one is not a value.

Facebook serves Marketplace in the account's own language and the scraper reads
the page by its labels.  With no language configured the scraper matched
English, which on a Spanish account does not fail loudly: the listing parses
with no seller and no condition, and the search quietly returns nothing useful.

So an unset -- or empty -- ``language`` resolves to ``es_LA`` rather than to
"English by accident".  An explicitly configured language is never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_marketplace_monitor.config import Config
from ai_marketplace_monitor.facebook import FacebookMarketplaceConfig

BASE = """
[marketplace.facebook]
username = "user@example.com"
password = "secret"
search_city = "houston"
{language}

[user.me]
pushbullet_token = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
"""


def _language_of(tmp_path: Path, line: str) -> str | None:
    path = tmp_path / "config.toml"
    path.write_text(BASE.format(language=line), encoding="utf-8")
    return Config([path]).marketplace["facebook"].language


def test_an_unset_language_falls_back(tmp_path: Path) -> None:
    assert _language_of(tmp_path, "") == "es_LA"


def test_an_empty_language_falls_back(tmp_path: Path) -> None:
    """What an older web UI left behind when the field was cleared."""
    assert _language_of(tmp_path, 'language = ""') == "es_LA"


def test_a_configured_language_is_kept(tmp_path: Path) -> None:
    assert _language_of(tmp_path, 'language = "zh_CN"') == "zh_CN"


def test_the_default_resolves_to_a_translation(tmp_path: Path) -> None:
    """The fallback has to be a language the monitor can actually translate.

    ``Config`` refuses a language with no translation section, so a default
    nobody had a dictionary for would make every configuration invalid --
    which is a far worse failure than the one it set out to fix.
    """
    path = tmp_path / "config.toml"
    path.write_text(BASE.format(language=""), encoding="utf-8")
    config = Config([path])  # would raise if "es" had no [translation.*]
    assert config.marketplace["facebook"].language == "es_LA"


def test_the_fallback_is_applied_by_the_config_object() -> None:
    """Straight from the dataclass, with no file involved."""
    assert FacebookMarketplaceConfig(name="facebook").language == "es_LA"
    assert FacebookMarketplaceConfig(name="facebook", language="").language == "es_LA"
    assert FacebookMarketplaceConfig(name="facebook", language="sv_SE").language == "sv_SE"


def test_a_non_string_language_is_still_refused() -> None:
    with pytest.raises(ValueError, match="must be a string"):
        FacebookMarketplaceConfig(name="facebook", language=7)  # type: ignore[arg-type]
