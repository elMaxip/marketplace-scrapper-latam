"""Tests for importing a session from the user's own browser.

The point of the feature is that some sites will not complete a sign-in inside
an automated browser at all, so the cookies have to come from outside.  The
paste therefore arrives in whatever shape the user's tools produce, and the two
things that must never happen are loading somebody's unrelated session into the
scraping profile and handing a cookie value back out again.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest

import logging

from ai_marketplace_monitor import session
from ai_marketplace_monitor.facebook import FacebookMarketplace
from ai_marketplace_monitor.lider import LiderMarketplace
from ai_marketplace_monitor.mercadolibre import MercadoLibreMarketplace
from ai_marketplace_monitor.monitor import MarketplaceMonitor


@pytest.fixture(autouse=True)
def temp_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    directory = tmp_path / "sessions"
    monkeypatch.setattr(session, "SESSION_DIR", directory)
    yield directory


ML_DOMAINS = MercadoLibreMarketplace.session_domains()


# --------------------------------------------------------------------------- #
# Reading whatever was pasted
# --------------------------------------------------------------------------- #


def test_a_cookie_manager_export_is_read() -> None:
    blob = json.dumps(
        [
            {
                "name": "ssid",
                "value": "abc",
                "domain": ".mercadolibre.cl",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "no_restriction",
                "expirationDate": 1900000000.5,
            }
        ]
    )
    cookie = session.parse_cookies(blob)[0]
    assert cookie["name"] == "ssid"
    assert cookie["domain"] == ".mercadolibre.cl"
    assert cookie["sameSite"] == "None"
    assert cookie["expires"] == 1900000000.5


def test_a_playwright_storage_state_is_read() -> None:
    blob = json.dumps(
        {"cookies": [{"name": "a", "value": "1", "domain": ".mercadolibre.cl"}], "origins": []}
    )
    assert [cookie["name"] for cookie in session.parse_cookies(blob)] == ["a"]


def test_a_header_line_is_read() -> None:
    """What devtools copies. It carries no domain, so one is supplied."""
    cookies = session.parse_cookies("ssid=abc; _d2id=xyz", default_domain=".mercadolibre.cl")
    assert [(c["name"], c["value"]) for c in cookies] == [("ssid", "abc"), ("_d2id", "xyz")]
    assert {c["domain"] for c in cookies} == {".mercadolibre.cl"}


def test_the_cookie_prefix_is_tolerated() -> None:
    cookies = session.parse_cookies("Cookie: ssid=abc", default_domain=".mercadolibre.cl")
    assert cookies[0]["name"] == "ssid"


def test_a_value_containing_an_equals_sign_survives() -> None:
    cookies = session.parse_cookies("t=abc=def==", default_domain=".mercadolibre.cl")
    assert cookies[0]["value"] == "abc=def=="


def test_a_session_cookie_gets_playwrights_sentinel() -> None:
    cookies = session.parse_cookies("a=1", default_domain=".mercadolibre.cl")
    assert cookies[0]["expires"] == -1


def test_same_site_none_forces_secure() -> None:
    """Chromium refuses SameSite=None on a non-secure cookie, and a refused
    cookie is a session that silently does not work."""
    blob = json.dumps(
        [{"name": "a", "value": "1", "domain": ".mercadolibre.cl", "sameSite": "None"}]
    )
    assert session.parse_cookies(blob)[0]["secure"] is True


@pytest.mark.parametrize(
    "blob", ["", "   ", "{not json", "esto no es nada"], ids=["empty", "blank", "broken", "prose"]
)
def test_a_paste_that_is_not_cookies_is_refused(blob: str) -> None:
    with pytest.raises(ValueError):
        session.parse_cookies(blob, default_domain=".mercadolibre.cl")


def test_a_cookie_with_no_name_is_dropped() -> None:
    blob = json.dumps(
        [{"value": "1", "domain": ".mercadolibre.cl"}, {"name": "a", "value": "1", "domain": "x.cl"}]
    )
    assert [cookie["name"] for cookie in session.parse_cookies(blob)] == ["a"]


# --------------------------------------------------------------------------- #
# Whose cookies these are
# --------------------------------------------------------------------------- #


def test_domain_matching_is_on_label_boundaries() -> None:
    assert session.domain_allowed(".mercadolibre.cl", ML_DOMAINS) is True
    assert session.domain_allowed("www.mercadolibre.cl", ML_DOMAINS) is True
    assert session.domain_allowed("mercadolibre.com.ar", ML_DOMAINS) is True
    assert session.domain_allowed("notmercadolibre.cl", ML_DOMAINS) is False
    assert session.domain_allowed("facebook.com", ML_DOMAINS) is False


def test_the_marketplaces_claim_their_own_domains() -> None:
    assert "facebook.com" in FacebookMarketplace.session_domains()
    assert "mercadolibre.cl" in ML_DOMAINS
    assert "mercadolivre.com.br" in ML_DOMAINS


def test_cookies_for_somewhere_else_are_dropped() -> None:
    """Pasting the wrong export is a normal mistake; loading an unrelated
    session into the scraping profile would be a bad way to find out."""
    cookies = session.parse_cookies(
        json.dumps(
            [
                {"name": "a", "value": "1", "domain": ".mercadolibre.cl"},
                {"name": "SID", "value": "2", "domain": ".google.com"},
            ]
        )
    )
    result = session.import_session("mercadolibre", cookies, ML_DOMAINS)
    assert (result["imported"], result["ignored"]) == (1, 1)
    stored = session.load_session("mercadolibre")
    assert [cookie["name"] for cookie in stored["cookies"]] == ["a"]


def test_an_import_with_nothing_of_ours_is_refused() -> None:
    cookies = session.parse_cookies(
        json.dumps([{"name": "SID", "value": "2", "domain": ".google.com"}])
    )
    with pytest.raises(ValueError):
        session.import_session("mercadolibre", cookies, ML_DOMAINS)
    assert session.load_session("mercadolibre") is None


# --------------------------------------------------------------------------- #
# What can be read back
# --------------------------------------------------------------------------- #


def test_nothing_is_stored_until_something_is_imported() -> None:
    info = session.session_info("mercadolibre")
    assert info["saved"] is False
    assert info["cookies"] == 0


def test_the_summary_never_carries_a_cookie_value() -> None:
    """A cookie value is the session itself; an interface that could read one
    back would be a way to lift it."""
    cookies = session.parse_cookies("ssid=super-secret", default_domain=".mercadolibre.cl")
    session.import_session("mercadolibre", cookies, ML_DOMAINS)
    info = session.session_info("mercadolibre")
    assert "super-secret" not in json.dumps(info)
    assert info["saved"] is True
    assert info["cookies"] == 1
    assert info["domains"] == ["mercadolibre.cl"]
    assert info["saved_at"]


def test_the_summary_reports_the_furthest_expiry() -> None:
    blob = json.dumps(
        [
            {"name": "a", "value": "1", "domain": ".mercadolibre.cl", "expirationDate": 1800000000},
            {"name": "b", "value": "2", "domain": ".mercadolibre.cl", "expirationDate": 1900000000},
            {"name": "c", "value": "3", "domain": ".mercadolibre.cl"},
        ]
    )
    session.import_session("mercadolibre", session.parse_cookies(blob), ML_DOMAINS)
    info = session.session_info("mercadolibre")
    assert info["expires_at"].startswith("2030-")


def test_clearing_forgets_it() -> None:
    session.import_session(
        "mercadolibre",
        session.parse_cookies("a=1", default_domain=".mercadolibre.cl"),
        ML_DOMAINS,
    )
    session.clear_session("mercadolibre")
    assert session.session_info("mercadolibre")["saved"] is False


# --------------------------------------------------------------------------- #
# Getting it into the browser
# --------------------------------------------------------------------------- #


def _import(name: str = "mercadolibre") -> None:
    session.import_session(
        name,
        session.parse_cookies("ssid=abc", default_domain=".mercadolibre.cl"),
        ML_DOMAINS,
    )


def test_a_fresh_import_is_waiting_to_be_applied() -> None:
    """Stored is not the same as in use: the browser belongs to another thread
    and takes it between jobs, or on its next start."""
    _import()
    assert session.import_is_pending("mercadolibre") is True
    assert session.session_info("mercadolibre")["pending"] is True


def test_an_import_survives_a_restart_unapplied() -> None:
    """The regression this exists for: the request used to live only in memory,
    so a monitor restarted before it was taken simply lost the session, and an
    established browser profile is never re-seeded from disk."""
    _import()
    # Nothing in memory; a new process sees only the file.
    assert session.import_is_pending("mercadolibre") is True


def test_applying_it_once_is_enough() -> None:
    """Replaying it on every launch would overwrite a good live session with an
    older copy of itself."""
    _import()
    session.mark_import_applied("mercadolibre")
    assert session.import_is_pending("mercadolibre") is False
    assert session.session_info("mercadolibre")["pending"] is False


def test_a_session_the_monitor_saved_is_not_replayed() -> None:
    """It is already in the profile it came from."""
    session._write("facebook", {"cookies": [{"name": "c_user", "value": "1"}], "origins": []})
    assert session.import_is_pending("facebook") is False


def test_re_arming_asks_for_it_again() -> None:
    _import()
    session.mark_import_applied("mercadolibre")
    assert session.rearm_import("mercadolibre") is True
    assert session.import_is_pending("mercadolibre") is True


def test_re_arming_works_on_a_file_with_no_bookkeeping() -> None:
    """A session imported before this bookkeeping existed carries no note saying
    it was ever meant to be applied."""
    session._write("mercadolibre", {"cookies": [{"name": "ssid", "value": "1"}], "origins": []})
    assert session.import_is_pending("mercadolibre") is False
    assert session.rearm_import("mercadolibre") is True
    assert session.import_is_pending("mercadolibre") is True


def test_re_arming_nothing_says_so() -> None:
    assert session.rearm_import("mercadolibre") is False


def test_the_cookies_survive_the_bookkeeping() -> None:
    _import()
    session.mark_import_applied("mercadolibre")
    session.rearm_import("mercadolibre")
    stored = session.load_session("mercadolibre")
    assert [cookie["name"] for cookie in stored["cookies"]] == ["ssid"]


# --------------------------------------------------------------------------- #
# Reaching every browser, not just the first one
# --------------------------------------------------------------------------- #
#
# A lane is a second browser on a profile of its own, and it is the browser that
# actually searches whichever platform runs in parallel.  An import that reached
# only the main context therefore did nothing at all to it -- which is what
# "the Sodimac cookies do not work" turned out to be: Sodimac runs on
# `browser-profile-sodimac`.


class FakeLane:
    """A lane that runs what it is given, the way a live one eventually does."""

    def __init__(self, name: str, alive: bool = True) -> None:
        self.name = name
        self.alive = alive
        self.context = FakeCookieJar()

    def submit(self, call):
        call(self.context)
        return None


class FakeCookieJar:
    def __init__(self) -> None:
        self.added = []

    def add_cookies(self, cookies):
        self.added.extend(cookies)


def _monitor(lanes):
    instance = MarketplaceMonitor.__new__(MarketplaceMonitor)
    instance.logger = logging.getLogger("test-session-import")
    instance.lanes = lanes
    return instance


COOKIES = [{"name": "cf_clearance", "value": "x", "domain": ".sodimac.cl"}]


def test_an_import_reaches_the_lanes_too() -> None:
    lane = FakeLane("sodimac")
    _monitor({"sodimac": lane})._seed_lanes_with_session("sodimac", COOKIES)
    assert lane.context.added == COOKIES


def test_every_live_lane_gets_it() -> None:
    # Not only the lane named after the platform: one profile visits every site,
    # and a cookie the review lane is missing is a review that gets refused.
    lanes = {"sodimac": FakeLane("sodimac"), "updates": FakeLane("updates")}
    _monitor(lanes)._seed_lanes_with_session("sodimac", COOKIES)
    assert all(lane.context.added == COOKIES for lane in lanes.values())


def test_a_lane_that_has_not_started_is_left_alone() -> None:
    # It seeds itself from the same stored file when its browser opens, so
    # there is nothing to do and no thread to do it on.
    lane = FakeLane("sodimac", alive=False)
    _monitor({"sodimac": lane})._seed_lanes_with_session("sodimac", COOKIES)
    assert lane.context.added == []


def test_a_lane_that_refuses_the_work_does_not_stop_the_others() -> None:
    class Broken(FakeLane):
        def submit(self, call):
            raise RuntimeError("lane is going away")

    good = FakeLane("updates")
    _monitor({"a": Broken("a"), "b": good})._seed_lanes_with_session("sodimac", COOKIES)
    assert good.context.added == COOKIES


# --------------------------------------------------------------------------- #
# One import, several browser profiles
# --------------------------------------------------------------------------- #
#
# The bug these pin down is the whole of "importar la sesión de Lider/Sodimac no
# hace nada".  A lane is a second browser on a profile of its own and it is the
# browser that searches whichever platform runs in parallel.  "Applied" used to
# be one fact about the import, so the monitor's own browser taking it settled
# the question for every profile -- and the lane, whose profile already existed
# and so was never re-seeded from disk, searched signed out for ever.


def test_applied_is_recorded_per_profile() -> None:
    _import()
    session.mark_import_applied("mercadolibre", None)
    assert session.import_is_pending("mercadolibre", None) is False
    assert session.import_is_pending("mercadolibre", "sodimac") is True


def test_each_profile_is_settled_on_its_own() -> None:
    _import()
    session.mark_import_applied("mercadolibre", "sodimac")
    assert session.import_is_pending("mercadolibre", "sodimac") is False
    assert session.import_is_pending("mercadolibre", None) is True


def test_a_file_from_the_old_bookkeeping_still_owes_every_lane() -> None:
    """`applied: True` meant "some browser took it", and in practice only ever
    meant the monitor's own.  Read that way, so upgrading repairs the lanes."""
    _import()
    state = session.load_session("mercadolibre")
    state["aimm"] = {"source": "imported", "applied": True}
    session._write_now("mercadolibre", state)
    assert session.import_is_pending("mercadolibre", None) is False
    assert session.import_is_pending("mercadolibre", "sodimac") is True


def test_re_arming_asks_every_profile_again() -> None:
    _import()
    session.mark_import_applied("mercadolibre", None)
    session.mark_import_applied("mercadolibre", "sodimac")
    session.rearm_import("mercadolibre")
    assert session.import_is_pending("mercadolibre", None) is True
    assert session.import_is_pending("mercadolibre", "sodimac") is True


# --------------------------------------------------------------------------- #
# A shop's own save must not eat the paste
# --------------------------------------------------------------------------- #


class FakeContext:
    """Just enough of a Playwright context for `save_site_session`."""

    def __init__(self, cookies) -> None:
        self._cookies = cookies

    def storage_state(self):
        return {"cookies": self._cookies, "origins": []}


ANONYMOUS = [{"name": "_px3", "value": "x", "domain": ".mercadolibre.cl"}]


def test_a_shop_save_keeps_the_import_bookkeeping() -> None:
    """`save_site_session` writes cookies and no `aimm` block.  Dropping the
    stored one erased what the user had imported and which profiles had taken
    it, so "importada" turned back into "guardada por el navegador"."""
    _import()
    session.mark_import_applied("mercadolibre", None)
    session.save_site_session("mercadolibre", FakeContext(ANONYMOUS), ML_DOMAINS)
    info = session.session_info("mercadolibre")
    assert info["source"] == "imported"
    assert session.import_is_pending("mercadolibre", "sodimac") is True


def test_a_shop_save_will_not_overwrite_a_paste_nobody_has_loaded() -> None:
    """The destructive case: the import never reached this browser, this browser
    is signed out, and its jar would be the only copy left."""
    _import()
    assert session.save_site_session("mercadolibre", FakeContext(ANONYMOUS), ML_DOMAINS) is False
    stored = session.load_session("mercadolibre")
    assert [cookie["name"] for cookie in stored["cookies"]] == ["ssid"]


# --------------------------------------------------------------------------- #
# The bot check's opinion is not the user's login
# --------------------------------------------------------------------------- #
#
# Both live on the shop's own domain, and `save_site_session` filters by domain,
# so a device id PerimeterX had already decided against was stored beside the
# pasted login and seeded back into every new profile.  Deleting every profile
# to start clean produced a fresh browser that was walled one second after it
# opened, wearing the flagged id we had just handed it.


def _lider_session() -> None:
    session._write(
        "lider",
        {
            "cookies": [
                {"name": "customer", "value": "u", "domain": ".lider.cl"},
                {"name": "auth", "value": "t", "domain": ".lider.cl"},
                {"name": "_pxvid", "value": "burned", "domain": ".lider.cl"},
                {"name": "_px3", "value": "stale", "domain": ".lider.cl"},
                {"name": "pxcts", "value": "x", "domain": ".lider.cl"},
            ],
            "origins": [],
        },
    )


def test_dropping_the_device_id_keeps_the_login() -> None:
    """The half that must survive: those cookies were pasted by hand and there
    is no other copy of them."""
    _lider_session()
    removed = session.drop_cookies("lider", LiderMarketplace.challenge_cookies)
    assert removed == 3
    stored = session.load_session("lider")
    assert sorted(cookie["name"] for cookie in stored["cookies"]) == ["auth", "customer"]


def test_dropping_nothing_leaves_the_file_alone() -> None:
    _lider_session()
    assert session.drop_cookies("lider", ()) == 0
    assert session.drop_cookies("lider", ("not-a-cookie",)) == 0
    assert len(session.load_session("lider")["cookies"]) == 5


def test_the_import_bookkeeping_survives_the_drop() -> None:
    """Otherwise clearing a burned device id would quietly turn a session the
    user imported back into one "saved by the browser", and no profile could be
    told to take it again."""
    session.import_session(
        "lider",
        session.parse_cookies("customer=u; _pxvid=burned", default_domain=".lider.cl"),
        ("lider.cl",),
    )
    session.mark_import_applied("lider", None)
    session.drop_cookies("lider", LiderMarketplace.challenge_cookies)
    info = session.session_info("lider")
    assert info["source"] == "imported"
    assert info["applied_to"] == [""]


def test_a_shop_with_nothing_to_drop_is_not_a_special_case() -> None:
    assert session.drop_cookies("nothing-stored-here", ("_pxvid",)) == 0


# --------------------------------------------------------------------------- #
# What an export from the user's own Chrome carries that should not travel
# --------------------------------------------------------------------------- #
#
# The bot-check cookies do belong to the shop, so the domain filter keeps them --
# they are simply not *ours*.  An export from an ordinary browser carries that
# browser's device id, and replaying it in the monitor's browser makes the id and
# the fingerprint disagree.  Dropped here rather than asked of the user, who
# would otherwise be editing a JSON export by hand to remove four names they have
# no way of recognising, on every new machine.


LIDER_DOMAINS = LiderMarketplace.session_domains()


def test_the_login_travels_and_the_device_id_does_not() -> None:
    result = session.import_session(
        "lider",
        session.parse_cookies(
            "customer=u; auth=t; _pxvid=theirs; _px3=theirs", default_domain=".lider.cl"
        ),
        LIDER_DOMAINS,
        drop_names=LiderMarketplace.challenge_cookies,
    )
    assert result["imported"] == 2
    assert result["dropped"] == 2
    stored = session.load_session("lider")
    assert sorted(cookie["name"] for cookie in stored["cookies"]) == ["auth", "customer"]


def test_other_sites_and_the_device_id_are_counted_apart() -> None:
    """Two different reasons to drop a cookie, and the interface says which."""
    result = session.import_session(
        "lider",
        session.parse_cookies(
            '[{"name":"customer","value":"u","domain":".lider.cl"},'
            '{"name":"_pxvid","value":"x","domain":".lider.cl"},'
            '{"name":"SID","value":"g","domain":".google.com"}]'
        ),
        LIDER_DOMAINS,
        drop_names=LiderMarketplace.challenge_cookies,
    )
    assert result["imported"] == 1
    assert result["ignored"] == 1
    assert result["dropped"] == 1


def test_a_paste_of_nothing_but_bot_check_cookies_says_so() -> None:
    """Kept apart from "no pertenecen a este marketplace": these did come from
    the right site, so that message would send the reader looking for a mistake
    they did not make."""
    with pytest.raises(ValueError, match="control antibots"):
        session.import_session(
            "lider",
            session.parse_cookies("_pxvid=x; _px3=y", default_domain=".lider.cl"),
            LIDER_DOMAINS,
            drop_names=LiderMarketplace.challenge_cookies,
        )


def test_a_marketplace_with_no_bot_check_cookies_is_unaffected() -> None:
    """Facebook and Mercado Libre declare none, and the import must behave
    exactly as it did before this existed."""
    result = session.import_session(
        "mercadolibre",
        session.parse_cookies("ssid=abc", default_domain=".mercadolibre.cl"),
        ML_DOMAINS,
    )
    assert result["imported"] == 1
    assert result["dropped"] == 0
