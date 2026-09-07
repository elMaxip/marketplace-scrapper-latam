"""A session file belongs to one platform, and the browser is asked what stuck.

Reported as "las sesiones son impredecibles: a veces funciona una y a veces la
otra, y cuando quiera".  It was two faults wearing one symptom, and both are
measurable rather than theoretical -- the numbers below come from a running
container.

**One profile, several platforms, one jar.**  ``Marketplace.save_session`` wrote
``context.storage_state()``, the *whole* jar, into ``sessions/<name>.json``.  One
Chromium holds Facebook, Mercado Libre and both shops, so
``sessions/facebook.json`` came to hold thirty Mercado Libre cookies -- a
snapshot of whatever state Mercado Libre was in when Facebook last signed in,
which for a logged-out moment is a *logged-out* Mercado Libre session filed
under Facebook's name.  Fifteen of them share a name with one in a freshly
imported ``sessions/mercadolibre.json``; seeding a profile replays both files,
and whichever went in last won.  Which one that was is the order of the sections
in the config file.  That is the whole of "cuando quiera".

**"Loaded 17 cookies" was not a fact about the browser.**  It counted what was
read off disk.  In the case that prompted this, two of the seventeen were
already expired (a browser silently drops those on the way in) and the site
deleted a third -- ``ssid``, the session -- on the very first request.  The
monitor reported a successful load, then reported "does not recognise the
imported session … they were probably copied from a different country's site",
and the user re-exported cookies that were fine.

So the browser is asked.  What is *missing* after the cookies were just put back
is what the site refused to keep, and the two failures stop looking alike:
missing cookies are worth re-injecting, present ones are worth reporting.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Tuple

from ai_marketplace_monitor.marketplace import Marketplace
from ai_marketplace_monitor.session import own_cookies


def cookie(name: str, domain: str, expires: float | None = None) -> Dict[str, Any]:
    return {
        "name": name,
        "value": "x",
        "domain": domain,
        "path": "/",
        "expires": -1 if expires is None else expires,
    }


FUTURE = time.time() + 86_400
PAST = time.time() - 86_400


# --------------------------------------------------------------------------- #
# Which cookies are a platform's own
# --------------------------------------------------------------------------- #


def test_a_platforms_file_keeps_only_its_own() -> None:
    jar = [
        cookie("c_user", ".facebook.com"),
        cookie("xs", ".facebook.com"),
        cookie("ssid", ".mercadolibre.cl"),
        cookie("orguserid", ".mercadolibre.cl"),
    ]
    kept = own_cookies(jar, ("facebook.com", "messenger.com"))
    assert [c["name"] for c in kept] == ["c_user", "xs"]


def test_subdomains_count_as_the_platforms_own() -> None:
    """`www.`, `listado.`, `myaccount.` -- all one site's."""
    jar = [
        cookie("_csrf", "www.mercadolibre.cl"),
        cookie("c_57eld", ".www.mercadolibre.cl"),
        cookie("ssid", ".mercadolibre.cl"),
    ]
    assert len(own_cookies(jar, ("mercadolibre.cl",))) == 3


def test_a_platform_that_cannot_name_its_domains_keeps_everything() -> None:
    """No domains is "cannot say", and the old behaviour is the safe answer:
    filtering on a guess would throw away the session it meant to protect."""
    jar = [cookie("a", ".example.com"), cookie("b", ".other.test")]
    assert len(own_cookies(jar, ())) == 2


def test_the_facebook_file_stops_carrying_mercado_libre() -> None:
    """The measured case: 50 cookies in `sessions/facebook.json`, 30 of them
    Mercado Libre's, and 15 of those sharing a name with the imported ones."""
    polluted = [cookie(f"fb{i}", ".facebook.com") for i in range(20)] + [
        cookie(name, ".mercadolibre.cl")
        for name in ("orgnickp", "orguserid", "orguseridp", "ftid", "cp")
    ]
    kept = own_cookies(polluted, ("facebook.com", "messenger.com"))
    assert len(kept) == 20
    assert not any("mercadolibre" in c["domain"] for c in kept)


# --------------------------------------------------------------------------- #
# What the browser actually has
# --------------------------------------------------------------------------- #


class FakeContext:
    """A browser jar that behaves like Chromium about the one thing that
    matters here: an already-expired cookie is dropped on the way in."""

    def __init__(self: "FakeContext", jar: Iterable[Dict[str, Any]] = ()) -> None:
        self.jar: List[Dict[str, Any]] = list(jar)
        #: Names the "site" deletes as soon as they are added -- what Mercado
        #: Libre does with a session it has invalidated.
        self.refuses: Tuple[str, ...] = ()

    def add_cookies(self: "FakeContext", cookies: Iterable[Dict[str, Any]]) -> None:
        now = time.time()
        for entry in cookies:
            expires = entry.get("expires")
            if isinstance(expires, (int, float)) and 0 < expires <= now:
                continue
            if entry.get("name") in self.refuses:
                continue
            self.jar = [
                c
                for c in self.jar
                if not (c["name"] == entry["name"] and c["domain"] == entry["domain"])
            ]
            self.jar.append(dict(entry))

    def cookies(self: "FakeContext") -> List[Dict[str, Any]]:
        return list(self.jar)


def market(context: FakeContext, stored: List[Dict[str, Any]]) -> Marketplace:
    instance = Marketplace("mercadolibre", context)  # type: ignore[arg-type]
    instance.stored_cookies = lambda: list(stored)  # type: ignore[method-assign]
    # A plain callable, not a classmethod: bound on the instance is what the
    # code calls, and a classmethod object assigned to an attribute is not.
    instance.session_domains = lambda: ("mercadolibre.cl",)  # type: ignore[method-assign]
    return instance


def test_the_browser_is_asked_which_cookies_it_has() -> None:
    context = FakeContext([cookie("ssid", ".mercadolibre.cl", FUTURE)])
    instance = market(context, [])
    assert instance.live_cookie_names() == ("ssid",)


def test_a_cookie_the_site_deleted_is_reported_missing() -> None:
    """The measured case: `ssid` and `ftid` have identical attributes, `ftid`
    survives and `ssid` does not, because the site cleared it."""
    stored = [
        cookie("ssid", ".mercadolibre.cl", FUTURE),
        cookie("ftid", ".mercadolibre.cl", FUTURE),
    ]
    context = FakeContext([cookie("ftid", ".mercadolibre.cl", FUTURE)])
    assert market(context, stored).missing_session_cookies() == ("ssid",)


def test_an_already_expired_cookie_is_not_reported_missing() -> None:
    """A browser drops it on the way in, so counting it as missing would report
    a fault that will never clear -- and would make the re-injection a loop."""
    stored = [
        cookie("ssid", ".mercadolibre.cl", FUTURE),
        cookie("cookiesPreferencesLogged", ".mercadolibre.cl", PAST),
    ]
    context = FakeContext([cookie("ssid", ".mercadolibre.cl", FUTURE)])
    assert market(context, stored).missing_session_cookies() == ()


def test_nothing_stored_means_nothing_missing() -> None:
    assert market(FakeContext(), []).missing_session_cookies() == ()


# --------------------------------------------------------------------------- #
# Putting them back
# --------------------------------------------------------------------------- #


def test_restoring_puts_the_stored_cookies_into_the_browser() -> None:
    """The "force the injection" case: the cookies are on disk and not in the
    profile, because an import is marked applied once and never replayed."""
    stored = [
        cookie("ssid", ".mercadolibre.cl", FUTURE),
        cookie("orguserid", ".mercadolibre.cl", FUTURE),
    ]
    context = FakeContext()
    instance = market(context, stored)
    assert instance.missing_session_cookies() == ("orguserid", "ssid")

    added, missing = instance.restore_session()

    assert added == 2
    assert missing == ()
    assert set(instance.live_cookie_names()) == {"ssid", "orguserid"}


def test_restoring_reports_what_the_site_refused_to_keep() -> None:
    """The half that stops this being a loop.  Re-injecting a session the site
    has invalidated changes nothing, so it must be reported, not retried."""
    stored = [
        cookie("ssid", ".mercadolibre.cl", FUTURE),
        cookie("ftid", ".mercadolibre.cl", FUTURE),
    ]
    context = FakeContext()
    context.refuses = ("ssid",)
    added, missing = market(context, stored).restore_session()

    assert added == 2  # they were handed over
    assert missing == ("ssid",)  # and one did not stick


def test_restoring_without_a_browser_does_nothing() -> None:
    instance = Marketplace("mercadolibre", None)  # type: ignore[arg-type]
    assert instance.restore_session() == (0, ())


def test_restoring_survives_a_browser_that_refuses_the_cookies() -> None:
    class Angry(FakeContext):
        def add_cookies(self, cookies: Iterable[Dict[str, Any]]) -> None:
            raise RuntimeError("the driver is unhappy")

    stored = [cookie("ssid", ".mercadolibre.cl", FUTURE)]
    added, missing = market(Angry(), stored).restore_session()
    assert added == 0
    assert missing == ("ssid",)


# --------------------------------------------------------------------------- #
# The decision the monitor makes with all that
# --------------------------------------------------------------------------- #


class Probe:
    """A platform that says no until its session cookie is in the browser."""

    def __init__(self: "Probe", context: FakeContext) -> None:
        self.name = "mercadolibre"
        self.context = context
        self.asked = 0
        self.restored = 0

    def is_signed_in(self: "Probe") -> bool:
        self.asked += 1
        return any(c["name"] == "ssid" for c in self.context.cookies())

    def missing_session_cookies(self: "Probe") -> Tuple[str, ...]:
        have = {c["name"] for c in self.context.cookies()}
        return () if "ssid" in have else ("ssid",)

    def restore_session(self: "Probe") -> Tuple[int, Tuple[str, ...]]:
        self.restored += 1
        self.context.add_cookies([cookie("ssid", ".mercadolibre.cl", FUTURE)])
        return 1, ()


def monitor_with_logger() -> Any:
    import logging

    from ai_marketplace_monitor.monitor import MarketplaceMonitor

    instance = MarketplaceMonitor.__new__(MarketplaceMonitor)
    instance.logger = logging.getLogger("test-session")
    return instance


def test_a_missing_session_is_put_back_and_the_answer_asked_again() -> None:
    context = FakeContext()
    probe = Probe(context)
    assert monitor_with_logger()._ask_signed_in(probe) is True
    assert probe.restored == 1
    # Asked before and after: the second answer is the one reported.
    assert probe.asked >= 2


def test_a_session_the_site_refuses_is_not_re_injected_for_ever() -> None:
    """Every stored cookie is already in the browser and the site still says no.
    Putting them back again would change nothing, so it is not attempted."""

    class Refused(Probe):
        def is_signed_in(self) -> bool:
            self.asked += 1
            return False

        def missing_session_cookies(self) -> Tuple[str, ...]:
            return ()

    probe = Refused(FakeContext())
    assert monitor_with_logger()._ask_signed_in(probe) is False
    assert probe.restored == 0
    assert probe.asked == 1


def test_a_platform_that_cannot_answer_is_not_asked() -> None:
    class Silent:
        name = "lider"

    assert monitor_with_logger()._ask_signed_in(Silent()) is False


def test_a_probe_that_raises_is_not_signed_in() -> None:
    class Broken(Probe):
        def is_signed_in(self) -> bool:
            raise RuntimeError("the site timed out")

    assert monitor_with_logger()._ask_signed_in(Broken(FakeContext())) is False


# --------------------------------------------------------------------------- #
# Keeping the stored file in step with the live session
# --------------------------------------------------------------------------- #
#
# Sites rotate a session while it is in use: Mercado Libre issues a new `ssid`
# as you browse and the profile takes it, while `sessions/mercadolibre.json`
# went on holding the token imported weeks earlier -- the file was only ever
# written by an interactive sign-in.  Nothing breaks while the profile lives.
# The day it is rebuilt (a recovery after a refusal, a new container, a
# `reset_profile`) the monitor reseeds a token the site retired long ago.
#
# The guard is the whole design: refreshing a file with a *signed-out* jar
# would destroy the one good copy there is, which is worse than the staleness
# it set out to fix.


def signed_in_market(context: FakeContext, cookies: Tuple[str, ...]) -> Marketplace:
    instance = Marketplace("mercadolibre", context)  # type: ignore[arg-type]
    instance.session_domains = lambda: ("mercadolibre.cl",)  # type: ignore[method-assign]
    instance.session_cookies = cookies  # type: ignore[misc]
    instance.saved = 0  # type: ignore[attr-defined]

    def save() -> bool:
        instance.saved += 1  # type: ignore[attr-defined]
        return True

    instance.save_session = save  # type: ignore[method-assign]
    return instance


def test_a_signed_in_browser_refreshes_the_file() -> None:
    context = FakeContext(
        [
            cookie("ssid", ".mercadolibre.cl", FUTURE),
            cookie("orguserid", ".mercadolibre.cl", FUTURE),
        ]
    )
    instance = signed_in_market(context, ("ssid",))
    assert instance.refresh_stored_session() is True
    assert instance.saved == 1  # type: ignore[attr-defined]


def test_a_signed_out_browser_never_overwrites_the_file() -> None:
    """The measured shape of a signed-out Mercado Libre profile: the account
    cookies are all still there and only `ssid` is gone.  Anything that read
    those as "signed in" would write a session-less jar over a good import."""
    context = FakeContext(
        [
            cookie("orgnickp", ".mercadolibre.cl", FUTURE),
            cookie("orguserid", ".mercadolibre.cl", FUTURE),
            cookie("orguseridp", ".mercadolibre.cl", FUTURE),
        ]
    )
    instance = signed_in_market(context, ("ssid",))
    assert instance.refresh_stored_session() is False
    assert instance.saved == 0  # type: ignore[attr-defined]


def test_every_named_cookie_is_required_not_just_one() -> None:
    """`any` is the right reading for a sentence in the log and the wrong one
    for a guard on overwriting a file."""
    context = FakeContext([cookie("c_user", ".facebook.com", FUTURE)])
    instance = signed_in_market(context, ("c_user", "xs"))
    instance.session_domains = lambda: ("facebook.com",)  # type: ignore[method-assign]
    assert instance.looks_signed_in() is False

    context.add_cookies([cookie("xs", ".facebook.com", FUTURE)])
    assert instance.looks_signed_in() is True


def test_a_platform_that_cannot_say_refreshes_nothing() -> None:
    """Empty means "cannot say", and a guess would be a way to lose a session."""
    instance = signed_in_market(FakeContext(), ())
    assert instance.looks_signed_in() is False
    assert instance.refresh_stored_session() is False
    assert instance.saved == 0  # type: ignore[attr-defined]


def test_a_foreign_cookie_of_the_same_name_does_not_count() -> None:
    """`live_cookie_names` is filtered to our own domains, so another site's
    cookie called `ssid` cannot make this platform look signed in."""
    context = FakeContext([cookie("ssid", ".example.test", FUTURE)])
    instance = signed_in_market(context, ("ssid",))
    assert instance.looks_signed_in() is False


def test_the_real_platforms_name_their_session_cookies() -> None:
    """So adding a platform is "name the cookies", never "find the mechanism"."""
    from ai_marketplace_monitor.facebook import FacebookMarketplace
    from ai_marketplace_monitor.mercadolibre import MercadoLibreMarketplace

    assert "ssid" in MercadoLibreMarketplace.session_cookies
    # Deliberately absent: Mercado Libre leaves these behind on a sign-out.
    for name in ("orgnickp", "orguserid", "orguseridp"):
        assert name not in MercadoLibreMarketplace.session_cookies
    assert "c_user" in FacebookMarketplace.session_cookies


# --------------------------------------------------------------------------- #
# The same audit, applied to every platform
# --------------------------------------------------------------------------- #
#
# The Mercado Libre bug was one instance of a shape: a *save* that keeps less
# than the *import* accepted, so writing the file deletes part of the stored
# session.  Two more turned up when the other platforms were read with that
# shape in mind, and the invariant below is what makes the next one fail a test
# instead of being found by hand months later.


def test_every_platform_keeps_what_its_import_accepts() -> None:
    """The invariant, once, for all of them.

    ``import_session`` filters a paste by ``session_domains()``; ``save_session``
    keeps ``session_domains()`` too.  Sodimac is the one that was wrong: its
    import accepts ``falabella.com`` -- its login *is* Falabella's -- while the
    save kept only ``hosts``, i.e. ``sodimac.cl``.  So the first page Sodimac
    served rewrote the file without the login half, and a profile rebuilt after
    that had no copy of it anywhere.
    """
    from ai_marketplace_monitor.config import supported_marketplaces

    for name, cls in supported_marketplaces.items():
        hosts = getattr(cls, "hosts", ())
        for host in hosts:
            assert any(
                host == domain or host.endswith("." + domain)
                for domain in cls.session_domains()
            ), f"{name}: {host} is not covered by session_domains()"


def test_a_shop_saves_its_login_domain_and_not_only_its_own_host() -> None:
    """Sodimac, specifically, because it is the one where the two differ."""
    from ai_marketplace_monitor.sodimac import SodimacMarketplace

    domains = SodimacMarketplace.session_domains()
    assert "falabella.com" in domains
    kept = own_cookies(
        [
            cookie("cart", ".sodimac.cl", FUTURE),
            cookie("JSESSIONID", ".falabella.com", FUTURE),
        ],
        domains,
    )
    assert {c["name"] for c in kept} == {"cart", "JSESSIONID"}


def test_a_platforms_domains_do_not_reach_another_platforms() -> None:
    """Nothing may claim a cookie that belongs to somebody else, or the files
    start swallowing each other again from the other end."""
    from ai_marketplace_monitor.config import supported_marketplaces

    entries = sorted(supported_marketplaces.items())
    for name, cls in entries:
        for other_name, other in entries:
            if name == other_name:
                continue
            for domain in other.session_domains():
                assert not own_cookies(
                    [cookie("x", "." + domain, FUTURE)], cls.session_domains()
                ), f"{name} would claim {other_name}'s {domain}"


def test_a_lookalike_domain_is_not_ours() -> None:
    """Suffix matching on label boundaries, not a bare `endswith`: the shops'
    own cookie reader used the loose version."""
    for evil in ("notfacebook.com", "facebook.com.evil.test", "xlider.cl"):
        assert not own_cookies(
            [cookie("x", evil, FUTURE)], ("facebook.com", "lider.cl")
        ), evil


# --------------------------------------------------------------------------- #
# A failed login must not destroy a pasted session
# --------------------------------------------------------------------------- #
#
# `save_device_state` runs when a Facebook login fails, to keep `datr` so the
# next attempt arrives as the same device rather than a new one.  It replaced
# the whole file with five device cookies -- which is right when the file is a
# session this monitor wrote and has just watched fail, and destruction when it
# is the user's own paste.  A login fails for reasons that say nothing about the
# cookies (a challenge, a two-factor prompt that timed out, the site being
# slow), so one bad attempt threw away a hand-pasted session with no copy left.


class StateContext:
    def __init__(self: "StateContext", cookies: List[Dict[str, Any]]) -> None:
        self._cookies = cookies

    def storage_state(self: "StateContext") -> Dict[str, Any]:
        return {"cookies": list(self._cookies), "origins": []}


def session_dir(tmp_path: Any) -> Any:
    import ai_marketplace_monitor.session as sess

    sess.SESSION_DIR = tmp_path
    return sess


def test_a_failed_login_keeps_a_pasted_session(tmp_path: Any) -> None:
    sess = session_dir(tmp_path)
    sess._write_now(
        "facebook",
        {
            "cookies": [cookie("c_user", ".facebook.com", FUTURE), cookie("xs", ".facebook.com", FUTURE)],
            "origins": [],
            "aimm": {"source": "imported", "applied_to": [""]},
        },
    )
    context = StateContext([cookie("datr", ".facebook.com", FUTURE)])

    assert sess.save_device_state("facebook", context, ("facebook.com",)) is True

    kept = {c["name"] for c in (sess.load_session("facebook") or {})["cookies"]}
    assert kept == {"c_user", "xs", "datr"}, "the paste must survive a failed attempt"


def test_a_failed_login_still_drops_a_session_the_monitor_wrote(tmp_path: Any) -> None:
    """The case the function was written for is unchanged: replaying a
    half-authenticated state puts the next run back into the same challenge."""
    sess = session_dir(tmp_path)
    sess._write_now(
        "facebook",
        {"cookies": [cookie("c_user", ".facebook.com", FUTURE)], "origins": []},
    )
    context = StateContext([cookie("datr", ".facebook.com", FUTURE)])

    sess.save_device_state("facebook", context, ("facebook.com",))

    kept = {c["name"] for c in (sess.load_session("facebook") or {})["cookies"]}
    assert kept == {"datr"}


def test_device_cookies_are_taken_from_our_own_domains_only(tmp_path: Any) -> None:
    """`locale`, `dpr`, `wd` and `sb` are names any site may use, and one
    profile holds several sites at once."""
    sess = session_dir(tmp_path)
    context = StateContext(
        [
            cookie("datr", ".facebook.com", FUTURE),
            cookie("locale", ".facebook.com", FUTURE),
            cookie("locale", ".mercadolibre.cl", FUTURE),
            cookie("wd", ".mercadolibre.cl", FUTURE),
        ]
    )

    sess.save_device_state("facebook", context, ("facebook.com",))

    stored = (sess.load_session("facebook") or {})["cookies"]
    assert all("facebook" in c["domain"] for c in stored)
    assert {c["name"] for c in stored} == {"datr", "locale"}


def test_a_refreshed_device_cookie_replaces_the_stored_one(tmp_path: Any) -> None:
    """Merging must not leave two `datr`s, or the file grows a duplicate on
    every failed attempt."""
    sess = session_dir(tmp_path)
    old = cookie("datr", ".facebook.com", FUTURE)
    old["value"] = "old"
    sess._write_now(
        "facebook",
        {"cookies": [old], "origins": [], "aimm": {"source": "imported", "applied_to": [""]}},
    )
    fresh = cookie("datr", ".facebook.com", FUTURE)
    fresh["value"] = "new"

    sess.save_device_state("facebook", StateContext([fresh]), ("facebook.com",))

    stored = (sess.load_session("facebook") or {})["cookies"]
    assert len(stored) == 1
    assert stored[0]["value"] == "new"


# --------------------------------------------------------------------------- #
# What a shop sets to a visitor with no account
# --------------------------------------------------------------------------- #

#: Every cookie Lider sets on `lider.cl` for an anonymous visitor, measured
#: against the live site: `https://www.lider.cl/` → `/inicio`, page fully
#: loaded, 23 cookies.  Recorded because the interesting fact is a *negative*
#: one and a negative cannot be re-derived from the code.
LIDER_ANONYMOUS = (
    "ACID", "TS017e8d10", "TS01cc7ea9", "TS01fffdff", "TSe3289311027",
    "__pxvid", "_astc", "_px3", "_pxvid", "adblocked", "b30msc", "bsc",
    "bstc", "btc", "cartId", "dimensionData", "exp-ck", "pxcts",
    "userAppVersion", "vtc", "xpa", "xpm", "xpth",
)


def test_a_shops_signed_in_cookies_are_not_all_the_same_age() -> None:
    """Why the list is one name and not the three a signed-in jar contains.

    Measured on a real imported Lider session: ``customer`` and ``CID`` last 169
    days, ``auth`` lasts 29.  ``looks_signed_in`` requires every name, so a list
    holding ``auth`` would have stopped answering a month after any sign-in --
    the session would quietly stop being kept up to date and nothing would say
    why.  Encoded as a test because the next reader will otherwise "complete"
    the list from a jar dump, which is exactly how it got there.
    """
    from ai_marketplace_monitor.lider import LiderMarketplace

    assert LiderMarketplace.session_cookies == ("customer",)


def test_no_cookie_a_shop_gives_everyone_counts_as_being_signed_in() -> None:
    """The trap this measurement exists to close.

    Lider sets ``ACID`` to every anonymous visitor, and the declaration used to
    say ``CID`` -- one character away.  "Correcting" it would make a signed-out
    browser pass ``looks_signed_in``, and the next save would write that
    session-less jar over a session the user pasted by hand.
    """
    from ai_marketplace_monitor.lider import LiderMarketplace

    for name in LIDER_ANONYMOUS:
        assert name not in LiderMarketplace.session_cookies, name


def test_an_anonymous_shop_jar_does_not_look_signed_in() -> None:
    """End to end over the real names, without the browser: the 23 cookies Lider
    hands a stranger must not add up to an account."""
    from ai_marketplace_monitor.lider import LiderMarketplace

    context = FakeContext([cookie(name, ".lider.cl", FUTURE) for name in LIDER_ANONYMOUS])
    shop = LiderMarketplace("lider", context)  # type: ignore[arg-type]
    assert shop.live_cookie_names() == tuple(sorted(LIDER_ANONYMOUS))
    assert shop.looks_signed_in() is False
    # And the shops' own, looser reading must not false-positive either.
    names = shop.live_cookie_names()
    assert not any(name in names for name in shop.session_cookies)


def test_the_shop_looks_signed_in_once_its_own_cookie_is_there() -> None:
    from ai_marketplace_monitor.lider import LiderMarketplace

    context = FakeContext([cookie(name, ".lider.cl", FUTURE) for name in LIDER_ANONYMOUS])
    shop = LiderMarketplace("lider", context)  # type: ignore[arg-type]
    context.add_cookies([cookie("customer", ".lider.cl", FUTURE)])
    assert shop.looks_signed_in() is True


def test_every_platform_names_at_least_one_cookie_or_none_at_all() -> None:
    """A single name is a measurement; a list is a bet that all of them are
    always set together.  Sodimac declares none on purpose -- its stored session
    is a Cloudflare clearance, not a login, so "signed out" would be an alarm
    about a state that is entirely normal."""
    from ai_marketplace_monitor.config import supported_marketplaces

    for name, cls in supported_marketplaces.items():
        assert isinstance(cls.session_cookies, tuple), name
        assert all(isinstance(entry, str) and entry for entry in cls.session_cookies), name


# --------------------------------------------------------------------------- #
# What the panel says it has
# --------------------------------------------------------------------------- #


def test_the_panel_counts_the_session_and_not_the_file(tmp_path: Any) -> None:
    """Measured on a real installation: `sessions/facebook.json` written before
    the save learned to filter holds 52 cookies, 43 of them Mercado Libre's.
    The panel read that as "52 cookies guardadas de facebook.com,
    listado.mercadolibre.cl, mercadoclics.com..." -- true about a file, false
    about a Facebook session, and shown to somebody already trying to work out
    why their sessions behave oddly."""
    sess = session_dir(tmp_path)
    sess._write_now(
        "facebook",
        {
            "cookies": [
                cookie("c_user", ".facebook.com", FUTURE),
                cookie("xs", ".facebook.com", FUTURE),
                cookie("ssid", ".mercadolibre.cl", FUTURE),
                cookie("_d2id", ".mercadoclics.com", FUTURE),
            ],
            "origins": [],
        },
    )

    info = sess.session_info("facebook", ("facebook.com", "messenger.com"))

    assert info["cookies"] == 2
    assert info["foreign"] == 2
    assert info["domains"] == ["facebook.com"]


def test_the_panel_is_unchanged_for_a_file_that_is_all_its_own() -> None:
    """The normal case, which is every file written since the fix."""
    import ai_marketplace_monitor.session as sess

    info = sess.session_info("nothing-stored-here", ("facebook.com",))
    assert info["saved"] is False
    assert info["cookies"] == 0 and info["foreign"] == 0


def test_the_panel_still_describes_a_platform_that_names_no_domains(tmp_path: Any) -> None:
    """No domains is "cannot say", and everything counts -- the behaviour before
    any of this existed."""
    sess = session_dir(tmp_path)
    sess._write_now(
        "whatever",
        {"cookies": [cookie("a", ".one.test", FUTURE), cookie("b", ".two.test", FUTURE)], "origins": []},
    )
    info = sess.session_info("whatever")
    assert info["cookies"] == 2 and info["foreign"] == 0
