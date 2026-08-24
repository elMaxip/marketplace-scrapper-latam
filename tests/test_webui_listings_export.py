"""Tests for the per-group CSV export."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Dict, Iterator, List

import pytest
from diskcache import Cache  # type: ignore

from ai_marketplace_monitor import observations as obs
from ai_marketplace_monitor.listing import Listing
from ai_marketplace_monitor.webui.listings_export import (
    BOM,
    CSV_COLUMNS,
    belongs_to,
    group_csv,
    iter_group_rows,
)


def _listing(
    listing_id: str = "1",
    price: str = "$100.000",
    title: str = "PlayStation 5",
    seller: str = "Ana",
) -> Listing:
    return Listing(
        marketplace="facebook",
        name="ps5",
        id=listing_id,
        title=title,
        image="",
        price=price,
        post_url=f"https://www.facebook.com/marketplace/item/{listing_id}/",
        location="Ñuñoa, Región Metropolitana",
        seller=seller,
        condition="used_good",
        description="anda perfecta",
    )


@pytest.fixture
def temp_cache(tmp_path: Path) -> Iterator[Cache]:
    cache = Cache(str(tmp_path / "cache"))
    obs.reset_index_cache()
    yield cache
    obs.reset_index_cache()
    cache.close()


def _parse(text: str) -> List[Dict[str, str]]:
    assert text.startswith(BOM)
    return list(csv.DictReader(io.StringIO(text[len(BOM) :], newline="")))


# --------------------------------------------------------------------------- #
# Which listings are in the group
# --------------------------------------------------------------------------- #


def test_belongs_to_reads_both_places_the_item_is_recorded() -> None:
    assert belongs_to({"items": ["ps5"], "listing": {}}, "ps5") is True
    assert belongs_to({"items": [], "listing": {"name": "ps5"}}, "ps5") is True
    assert belongs_to({"items": ["xbox"], "listing": {"name": "xbox"}}, "ps5") is False


def test_belongs_to_ignores_case_and_padding() -> None:
    assert belongs_to({"items": ["PlayStation 5"], "listing": {}}, "playstation 5") is True


def test_only_the_named_group_is_exported(temp_cache: Cache) -> None:
    obs.record_observation(_listing("1"), item_name="ps5", local_cache=temp_cache)
    other = _listing("2", title="Xbox Series X")
    other.name = "xbox"
    obs.record_observation(other, item_name="xbox", local_cache=temp_cache)

    rows = list(iter_group_rows(temp_cache, "ps5"))
    assert [row["titulo"] for row in rows] == ["PlayStation 5"]


def test_a_deleted_listing_is_not_exported(temp_cache: Cache) -> None:
    obs.record_observation(_listing("1"), item_name="ps5", local_cache=temp_cache)
    obs.record_observation(_listing("2"), item_name="ps5", local_cache=temp_cache)
    obs.delete_observations([("facebook", "1")], local_cache=temp_cache)

    assert [row["url"].split("/")[-2] for row in iter_group_rows(temp_cache, "ps5")] == ["2"]


def test_the_whole_group_is_exported_cheapest_first(temp_cache: Cache) -> None:
    """Not the page being looked at: the rows come from the store."""
    for listing_id, price in (("1", "$300.000"), ("2", "$100.000"), ("3", "$200.000")):
        obs.record_observation(
            _listing(listing_id, price=price), item_name="ps5", local_cache=temp_cache
        )
    rows = list(iter_group_rows(temp_cache, "ps5"))
    assert [row["precio_actual_valor"] for row in rows] == ["100000", "200000", "300000"]


def test_an_unpriced_listing_sinks_to_the_bottom(temp_cache: Cache) -> None:
    obs.record_observation(
        _listing("1", price="**unspecified**"), item_name="ps5", local_cache=temp_cache
    )
    obs.record_observation(_listing("2", price="$100.000"), item_name="ps5", local_cache=temp_cache)
    rows = list(iter_group_rows(temp_cache, "ps5"))
    assert [row["precio_actual_valor"] for row in rows] == ["100000", ""]


# --------------------------------------------------------------------------- #
# What the rows say
# --------------------------------------------------------------------------- #


def test_a_price_change_is_reported_as_before_now_and_difference(temp_cache: Cache) -> None:
    obs.record_observation(_listing(price="$100.000"), item_name="ps5", local_cache=temp_cache)
    obs.record_observation(_listing(price="$80.000"), item_name="ps5", local_cache=temp_cache)

    row = _parse(group_csv(temp_cache, "ps5"))[0]
    assert row["precio_actual"] == "$80.000"
    assert row["precio_actual_valor"] == "80000"
    assert row["precio_anterior_valor"] == "100000"
    assert row["variacion_precio"] == "-20000"
    assert row["precio_minimo_historico"] == "80000"


def test_a_listing_that_never_moved_has_no_previous_price(temp_cache: Cache) -> None:
    obs.record_observation(_listing(price="$100.000"), item_name="ps5", local_cache=temp_cache)
    row = _parse(group_csv(temp_cache, "ps5"))[0]
    assert row["precio_actual_valor"] == "100000"
    assert row["precio_anterior_valor"] == ""
    assert row["variacion_precio"] == ""


def test_the_filters_verdict_is_the_state_column(temp_cache: Cache) -> None:
    obs.record_observation(_listing("1"), item_name="ps5", local_cache=temp_cache)
    obs.record_observation(_listing("2"), matched=False, item_name="ps5", local_cache=temp_cache)
    states = {row["url"].split("/")[-2]: row["estado"] for row in iter_group_rows(temp_cache, "ps5")}
    assert states == {"1": "activa", "2": "descartada"}


def test_the_ai_verdict_is_carried(temp_cache: Cache) -> None:
    listing = _listing()
    obs.record_observation(listing, item_name="ps5", local_cache=temp_cache)
    obs.record_rating(listing, score=4, comment="buen precio", local_cache=temp_cache)
    row = _parse(group_csv(temp_cache, "ps5"))[0]
    assert row["puntaje_ia"] == "4"
    assert row["comentario_ia"] == "buen precio"


# --------------------------------------------------------------------------- #
# The file itself
# --------------------------------------------------------------------------- #


def test_the_header_names_every_column(temp_cache: Cache) -> None:
    text = group_csv(temp_cache, "ps5")
    assert text.startswith(BOM)
    assert text[len(BOM) :].splitlines()[0].split(",") == CSV_COLUMNS


def test_accents_survive(temp_cache: Cache) -> None:
    """The BOM is what makes Excel read the file as UTF-8 rather than mangling
    "Ñuñoa"."""
    obs.record_observation(_listing(), item_name="ps5", local_cache=temp_cache)
    text = group_csv(temp_cache, "ps5")
    assert text.startswith(BOM)
    assert "Ñuñoa, Región Metropolitana" in text
    assert text.encode("utf-8").startswith(b"\xef\xbb\xbf")


def test_commas_quotes_and_newlines_are_escaped(temp_cache: Cache) -> None:
    obs.record_observation(
        _listing(title='PS5 "slim", con 2 controles\ny caja'),
        item_name="ps5",
        local_cache=temp_cache,
    )
    row = _parse(group_csv(temp_cache, "ps5"))[0]
    assert row["titulo"] == 'PS5 "slim", con 2 controles\ny caja'


def test_a_formula_is_neutralized(temp_cache: Cache) -> None:
    """Scraped text opened in a spreadsheet must not execute."""
    obs.record_observation(
        _listing(seller="=1+1"), item_name="ps5", local_cache=temp_cache
    )
    row = _parse(group_csv(temp_cache, "ps5"))[0]
    assert row["vendedor"] == "'=1+1"


def test_an_empty_group_is_a_header_and_nothing_else(temp_cache: Cache) -> None:
    text = group_csv(temp_cache, "ps5")
    assert len(text[len(BOM) :].strip().splitlines()) == 1


# --------------------------------------------------------------------------- #
# The endpoint
# --------------------------------------------------------------------------- #

from fastapi.testclient import TestClient  # noqa: E402

from ai_marketplace_monitor.webui import server as webui_server  # noqa: E402
from ai_marketplace_monitor.webui.config_api import ConfigFileService  # noqa: E402
from ai_marketplace_monitor.webui.log_handler import LogBroadcastHandler  # noqa: E402
from ai_marketplace_monitor.webui.server import AuthState, WebUIConfig, create_app  # noqa: E402


def _client(
    tmp_path: Path, temp_cache: Cache, monkeypatch: pytest.MonkeyPatch, exposed: bool = False
) -> TestClient:
    monkeypatch.setattr(webui_server, "cache", temp_cache)
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "[marketplace.facebook]\nsearch_city = 'dallas'\n",
        encoding="utf-8",
    )
    handler = LogBroadcastHandler()
    state = AuthState()
    state.exposed = exposed
    app = create_app(
        WebUIConfig(config_files=[config_file], log_handler=handler),
        state,
        ConfigFileService([config_file]),
        handler,
    )
    return TestClient(app)


def test_the_endpoint_serves_the_group_as_an_attachment(
    tmp_path: Path, temp_cache: Cache, monkeypatch: pytest.MonkeyPatch
) -> None:
    obs.record_observation(_listing(), item_name="ps5", local_cache=temp_cache)
    response = _client(tmp_path, temp_cache, monkeypatch).get("/api/listings/export.csv?item=ps5")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.text.startswith(BOM)
    assert "PlayStation 5" in response.text


def test_the_filename_survives_an_awkward_group_name(
    tmp_path: Path, temp_cache: Cache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A search is named by the user and travels into a header; only the safe
    part of it may end up there."""
    response = _client(tmp_path, temp_cache, monkeypatch).get(
        "/api/listings/export.csv?item=playstation%205%20%22pro%22"
    )
    disposition = response.headers["content-disposition"]
    assert response.status_code == 200
    assert '"' not in disposition.split("filename=")[1][1:-1]
    assert "playstation-5-pro" in disposition


def test_the_endpoint_needs_an_item(
    tmp_path: Path, temp_cache: Cache, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, temp_cache, monkeypatch)
    assert client.get("/api/listings/export.csv").status_code == 422
    assert client.get("/api/listings/export.csv?item=%20").status_code == 400


def test_the_endpoint_requires_a_session_when_exposed(
    tmp_path: Path, temp_cache: Cache, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, temp_cache, monkeypatch, exposed=True)
    assert client.get("/api/listings/export.csv?item=ps5").status_code == 401
