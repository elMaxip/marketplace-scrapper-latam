"""FastAPI app factory and uvicorn-in-a-thread runner.

The monitor process stays fully synchronous. Uvicorn runs on its own
asyncio loop in a daemon thread; the LogBroadcastHandler bridges records
from the main thread to that loop via ``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import uvicorn
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import app_version, control
from ..config import supported_marketplaces
from ..pause import is_paused, pause_state, run_state, set_paused
from ..tracking import (
    is_watchable as track_is_watchable,
    preview as track_preview,
    reader_for as track_reader_for,
)
from ..session import (
    clear_session,
    import_session,
    parse_cookies,
    rearm_import,
    session_info,
)
from ..utils import cache
from .auth import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    SESSION_TTL,
    AuthConfig,
    RateLimiter,
    SessionManager,
    hash_password,
    verify_password,
)
from .config_api import ConfigFileService
from .config_auth import extract_credentials
from .found_export import iter_found_csv, iter_found_rows
from .listings_api import MAX_DELETE_KEYS, build_sync_response, delete_listings
from .listings_export import iter_group_csv, iter_group_rows
from .log_handler import LogBroadcastHandler
from .scraper_state import config_sync, scraper_state

# Ensure the vendored toml-edit-js WASM bundle is served with the right
# Content-Type. Python's mimetypes module learned .wasm in 3.10 but
# explicit registration is safer across patch versions.
mimetypes.add_type("application/wasm", ".wasm")

STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class WebUIConfig:
    host: str = "127.0.0.1"
    port: int = 8467
    config_files: List[Path] = field(default_factory=list)
    log_handler: LogBroadcastHandler | None = None
    #: Serve without a password even though the bind address is not loopback.
    #:
    #: There is exactly one situation this is for, and it is the reason it
    #: exists: a container.  Inside Docker the server *must* bind 0.0.0.0 or
    #: nothing outside the container can reach it -- including the web UI's own
    #: container, one hop away on a private network -- and the bind address is
    #: what the loopback rule reads.  So a perfectly private deployment looked
    #: identical to one on the open internet, and the server refused to start.
    #:
    #: It is off by default and has to be asked for by name (``--webui-open``,
    #: or ``AIMM_WEBUI_OPEN=1``), because saying "no password" is a decision
    #: about who can reach the port, and only the person running it knows.
    open_access: bool = False


@dataclass
class StartupInfo:
    """Information about the running server, shown in the startup banner."""

    urls: List[str]
    username: str | None  # None in open mode
    host: str
    port: int
    exposed: bool


class AuthState:
    """Mutable auth state.

    On loopback (default) the web UI is always open — no password
    required.  When ``--webui-host`` exposes the server on a
    non-loopback interface, ``auth`` must be set (credentials from
    a marketplace config section or environment variables).
    """

    def __init__(self) -> None:
        self.auth: AuthConfig | None = None
        self.exposed: bool = False


def _resolve_auth(config: WebUIConfig) -> tuple[AuthState, StartupInfo]:
    """Build initial AuthState from config files and environment.

    On loopback the UI is always open.  When exposed (--webui-host),
    credentials are required — checked from ``[marketplace.*]`` config
    sections, then ``FACEBOOK_USERNAME`` / ``FACEBOOK_PASSWORD`` env
    vars.
    """
    # "Exposed" means "a password is required", and it is not quite the same
    # question as "is the bind address loopback".  A container has to bind
    # 0.0.0.0 to be reachable from the container next to it, which is not the
    # same as being reachable from the internet -- so the operator can say so.
    exposed = config.host not in ("127.0.0.1", "localhost", "::1") and not config.open_access
    state = AuthState()
    state.exposed = exposed

    if exposed:
        extracted = extract_credentials(config.config_files)
        if extracted.username and extracted.password:
            state.auth = AuthConfig(
                username=extracted.username,
                password_hash=hash_password(extracted.password),
                secret_key=secrets.token_urlsafe(32),
            )
        # If exposed with no credentials, start_webui() will reject this.

    info = StartupInfo(
        urls=_enumerate_urls(config.host, config.port),
        username=state.auth.username if state.auth else None,
        host=config.host,
        port=config.port,
        exposed=exposed,
    )
    return state, info


def _set_session_cookies(response: Response, token: str, csrf: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=SESSION_TTL,
        httponly=False,  # JS reads this to echo via header
        samesite="strict",
    )


def _enumerate_urls(host: str, port: int) -> List[str]:
    if host in ("127.0.0.1", "localhost", "::1"):
        return [f"http://127.0.0.1:{port}"]
    if host in ("0.0.0.0", "::"):  # noqa: S104 — intentional bind-all
        # Enumerate local interface addresses so the user sees every reachable URL.
        urls = [f"http://127.0.0.1:{port}"]
        try:
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None):
                addr = str(info[4][0])
                if addr and addr not in ("127.0.0.1", "::1"):
                    if ":" in addr:
                        urls.append(f"http://[{addr}]:{port}")
                    else:
                        urls.append(f"http://{addr}:{port}")
        except socket.gaierror:
            pass
        # De-duplicate preserving order.
        seen: set[str] = set()
        unique: List[str] = []
        for url in urls:
            if url not in seen:
                seen.add(url)
                unique.append(url)
        return unique
    return [f"http://{host}:{port}"]


def create_app(
    config: WebUIConfig,
    state: AuthState,
    config_service: ConfigFileService,
    log_handler: LogBroadcastHandler,
) -> FastAPI:
    app = FastAPI(
        title="AI Marketplace Monitor",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    process_secret = secrets.token_urlsafe(32)
    sessions = SessionManager(process_secret)
    rate_limiter = RateLimiter()

    def is_open() -> bool:
        """True when running on loopback — no password required."""
        return not state.exposed

    def require_session(
        request: Request,
        session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    ) -> str:
        if is_open():
            return "anonymous"
        if session is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        username = sessions.validate(session)
        if username is None:
            raise HTTPException(status_code=401, detail="Session expired")
        return username

    def require_csrf(
        request: Request,
        csrf_cookie: str | None = Cookie(default=None, alias=CSRF_COOKIE),
    ) -> None:
        if is_open():
            return  # open mode skips CSRF (nothing to protect)
        header = request.headers.get(CSRF_HEADER)
        if not header or not csrf_cookie or not secrets.compare_digest(header, csrf_cookie):
            raise HTTPException(status_code=403, detail="CSRF token mismatch")

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.get("/api/auth/info")
    async def auth_info() -> Dict[str, Any]:
        """Return auth mode info for the frontend login screen."""
        return {
            "open": is_open(),
            "username_hint": state.auth.username if state.auth else None,
        }

    @app.post("/api/login")
    async def login(
        request: Request,
        response: Response,
        username: str = Form(""),
        password: str = Form(""),
    ) -> Dict[str, Any]:
        # Loopback — always open, no password needed.
        if is_open():
            token, csrf = sessions.issue("anonymous")
            _set_session_cookies(response, token, csrf)
            return {"username": "anonymous", "csrf": csrf}

        # Exposed — credentials required.
        client_ip = request.client.host if request.client else "unknown"
        if rate_limiter.is_locked(client_ip):
            raise HTTPException(status_code=429, detail="Too many failed attempts")

        assert state.auth is not None  # enforced by start_webui()
        if username != state.auth.username or not verify_password(
            password, state.auth.password_hash
        ):
            rate_limiter.record_failure(client_ip)
            raise HTTPException(status_code=401, detail="Invalid credentials")

        rate_limiter.reset(client_ip)
        token, csrf = sessions.issue(username)
        _set_session_cookies(response, token, csrf)
        return {"username": username, "csrf": csrf}

    @app.post("/api/logout")
    async def logout(response: Response) -> Dict[str, Any]:
        response.delete_cookie(SESSION_COOKIE)
        response.delete_cookie(CSRF_COOKIE)
        return {"ok": True}

    @app.get("/api/status")
    async def status(_: str = Depends(require_session)) -> Dict[str, Any]:
        files = config_service.list_files()
        return {
            "config_files": [f.__dict__ for f in files],
            "urls": _enumerate_urls(config.host, config.port),
            "auth_mode": "open" if is_open() else "authenticated",
            "open": is_open(),
            "paused": is_paused(),
            # Which of the three states the monitor is in, said by the monitor
            # rather than pieced together by the interface from two booleans.
            # "Pausar" and "Detener" do not have the same way back -- one is
            # resumed, the other started again -- so the controls need to know
            # which one happened, not merely that something is held back.
            "run_state": run_state(),
            # Everything the controls need in one poll: whether the pause was the
            # forced kind, and whether a search is actually under way right now.
            "pause": pause_state(),
            "scraping": control.state(),
            # What the loop is doing at the coarsest grain, and whether it has
            # taken up the configuration that is on disk.  Both belong in the
            # poll every screen already makes: a saved change the scraper has
            # not read yet is worth saying wherever the user is looking.
            "phase": control.phase(),
            "config_sync": config_sync(list(config.config_files)),
            # Which build is answering.  Carried on the poll every screen
            # already makes because the question it answers -- "did the update
            # actually land?" -- is asked from the outside, by someone who has
            # just pushed a tag and cannot see this container's logs.
            # `source` travels with it so the interface never presents the
            # package number as if it were the released tag: see `app_version`.
            "version": app_version()._asdict(),
            "vnc_enabled": os.environ.get("AIMM_ENABLE_VNC") == "1"
            and Path(os.environ.get("AIMM_NOVNC_DIR", "/usr/share/novnc")).is_dir(),
        }

    @app.get("/api/scraper/state")
    async def get_scraper_state(_: str = Depends(require_session)) -> Dict[str, Any]:
        """What the scraper is doing, and on which configuration.

        Deliberately separate from ``/api/status``, which answers "is the
        monitor up and is it paused" and is polled by every screen.  This one
        is the detailed picture, and only the screen showing it asks for it.

        The two questions it exists to answer honestly are "which searches is
        the scraper actually running" and "has it taken up the change I just
        saved" -- both read from what the scraping thread reported, never
        inferred from the file on disk.
        """
        return scraper_state(list(config.config_files))

    @app.get("/api/config/files")
    async def list_config_files(_: str = Depends(require_session)) -> Dict[str, Any]:
        return {"files": [f.__dict__ for f in config_service.list_files()]}

    @app.get("/api/config/file/{file_id}")
    async def get_config_file(file_id: str, _: str = Depends(require_session)) -> Dict[str, Any]:
        try:
            content, mtime = config_service.read(file_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e)) from None
        from .config_api import scan_sections
        from .secrets_redact import MASK, has_mask

        sections = [
            {
                "name": s.name,
                "prefix": s.prefix,
                "suffix": s.suffix,
                "line_start": s.line_start,
                "line_end": s.line_end,
                "fields": s.fields,
            }
            for s in scan_sections(content)
        ]
        return {
            "content": content,
            "mtime": mtime,
            "has_masked_secrets": has_mask(content),
            "mask_token": MASK,
            "sections": sections,
        }

    @app.put("/api/config/file/{file_id}", response_model=None)
    async def put_config_file(
        file_id: str,
        body: Dict[str, Any],
        _: str = Depends(require_session),
        __: None = Depends(require_csrf),
    ) -> Dict[str, Any]:
        content = body.get("content")
        if not isinstance(content, str):
            raise HTTPException(status_code=400, detail="Missing 'content' field")
        base_mtime = body.get("base_mtime")
        try:
            new_mtime, ok, error = config_service.write(
                file_id, content, base_mtime if isinstance(base_mtime, (int, float)) else None
            )
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e)) from None
        if not ok:
            status_code = 409 if error and "conflict" in error else 400
            return JSONResponse(  # type: ignore[return-value]
                status_code=status_code,
                content={"ok": False, "error": error, "mtime": new_mtime},
            )
        return {"ok": True, "mtime": new_mtime}

    @app.post("/api/config/validate")
    async def validate_config(
        body: Dict[str, Any],
        _: str = Depends(require_session),
        __: None = Depends(require_csrf),
    ) -> Dict[str, Any]:
        content = body.get("content")
        if not isinstance(content, str):
            raise HTTPException(status_code=400, detail="Missing 'content' field")
        ok, error = config_service.validate(content)
        return {"valid": ok, "error": error}

    @app.post("/api/monitor/restart")
    async def restart_monitor(
        _: str = Depends(require_session),
        __: None = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """Wake the monitor by touching the config file.

        The file watcher interrupts the monitor's doze() sleep, causing
        it to reload the config and run all scheduled searches immediately.
        """
        try:
            path = config_service.editable_path
            path.touch()
            return {"ok": True, "message": "Monitor woken — searching all items now."}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to touch config: {e}") from e

    @app.get("/api/monitor/pause")
    async def get_pause(_: str = Depends(require_session)) -> Dict[str, Any]:
        return {**pause_state(), "run_state": run_state(), "scraping": control.state()}

    @app.post("/api/monitor/pause")
    async def post_pause(
        body: Dict[str, Any],
        _: str = Depends(require_session),
        __: None = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """Hold back searching, or release it.

        Two kinds of stop, and both of them stop the search under way at its
        next checkpoint -- within seconds, not whenever it happens to finish.
        What separates them is what is left standing afterwards:

        * plain (``force`` absent or false): the browsers, tabs and signed-in
          sessions stay exactly as they are, so resuming costs one search
          rather than one sign-in.  This is "Pausa".
        * forced (``force: true``): every browser is closed and the resources
          released.  This is "Detener", and there is nothing to resume into.

        The stop is a request, not an act.  Playwright's objects belong to the
        thread that made them, so this handler cannot close a page itself; it
        raises a flag that the scraping loop reads within seconds.  ``scraping``
        in the response is how the caller sees whether it has landed yet.
        """
        paused = body.get("paused")
        if not isinstance(paused, bool):
            raise HTTPException(status_code=400, detail="Missing boolean 'paused' field")
        force = bool(body.get("force")) and paused
        state = set_paused(paused, force=force)
        if paused:
            # Both kinds interrupt.  A pause that waited for the current search
            # to finish was a pause that did nothing for the twenty minutes a
            # Facebook pass takes, which is not what the word promises.
            control.request_cancel(mode="stop" if force else "pause")
        else:
            # Resuming withdraws any stop that had not been acted on yet.
            control.clear_cancel()
        return {**state, "run_state": run_state(), "scraping": control.state()}

    # ------------------------------------------------------------------
    # Controlling the searches themselves
    # ------------------------------------------------------------------
    #
    # Three buttons that all mean "not this, now" without meaning "stop the
    # scraper".  Like every other cross-thread request here they are flags the
    # scraping loop reads at its own checkpoints, so the answer is always "asked
    # for", never "done" -- the caller watches `scraping` to see it land.

    @app.post("/api/scraper/search/stop")
    async def stop_search(
        body: Dict[str, Any],
        _: str = Depends(require_session),
        __: None = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """End one search early and move on to the next.

        With ``marketplace``, only that platform of that product stops and the
        rest of the product carries on.  Without it, the product is done for
        this pass -- including the platforms it has not started yet, which is
        the difference between stopping a search and stopping a page.

        The scraper itself is untouched either way: no browser is closed, and
        the next search starts as soon as this one unwinds.
        """
        item = body.get("item")
        if not isinstance(item, str) or not item:
            raise HTTPException(status_code=400, detail="Missing 'item' field")
        marketplace = body.get("marketplace")
        if marketplace is not None and not isinstance(marketplace, str):
            raise HTTPException(status_code=400, detail="'marketplace' must be a string")
        requested = control.request_search_stop(item, marketplace or None)
        return {"ok": True, "stop": requested, "scraping": control.state()}

    @app.post("/api/scraper/search/next")
    async def choose_next_search(
        body: Dict[str, Any],
        _: str = Depends(require_session),
        __: None = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """Put one product at the head of the queue.  ``item: null`` clears it.

        Deliberately does not touch the search under way: the promise is the
        *next* one, and a button that cut off the current search would be the
        one above.
        """
        item = body.get("item")
        if item is not None and (not isinstance(item, str) or not item):
            raise HTTPException(status_code=400, detail="'item' must be a name or null")
        chosen = control.set_next_search(item)
        return {"ok": True, "next_search": chosen, "scraping": control.state()}

    @app.post("/api/scraper/search/run")
    async def run_search_now(
        body: Dict[str, Any],
        _: str = Depends(require_session),
        __: None = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """Search one product now, ending whatever is running to get to it.

        The two requests above, sent together, because that is what "ahora"
        means: the promotion says what runs next, and the stop is what makes
        "next" arrive in seconds rather than at the end of a pass.  The searches
        it ends finish the way the button beside them ends one -- as if they had
        run out, browser kept, queue continued -- so the scraper stays in
        exactly the state it was in.

        Still a request, like everything else here: the answer says what was
        asked for, and the caller watches `scraping` to see it land.  It does
        not release the pause switch, for the same reason ``/api/monitor/run``
        does not: a paused monitor told to search would be obeying two
        contradictory instructions.  `paused` is in the answer so the caller can
        say so instead of promising something that will not happen yet.
        """
        item = body.get("item")
        if not isinstance(item, str) or not item:
            raise HTTPException(status_code=400, detail="Missing 'item' field")
        result = control.request_search_now(item)
        return {
            "ok": True,
            **result,
            "paused": is_paused(),
            "run_state": run_state(),
            "scraping": control.state(),
        }

    @app.post("/api/monitor/run")
    async def force_run(
        _: str = Depends(require_session),
        __: None = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """Search everything now, whatever the schedule says.

        Refused rather than queued while a search is already running: stacking a
        second full pass on top of the first is the concurrent hammering of the
        marketplace this is meant to avoid, and the caller can simply watch the
        run that is already going.

        This does not release the pause switch.  A paused monitor that was told
        to search would be obeying two contradictory instructions; the caller
        resumes first if that is what it means.
        """
        result = control.request_run(reason="web UI")
        return {
            "ok": True,
            **result,
            "paused": is_paused(),
            "run_state": run_state(),
            "scraping": control.state(),
        }

    # ------------------------------------------------------------------
    # Marketplace sessions
    # ------------------------------------------------------------------
    #
    # Some sites will not complete a sign-in inside an automated browser: the
    # form is accepted and the visitor is quietly returned to the front page.
    # Rather than fight that, the user can hand over a session established in
    # their own browser.  Cookies are credential-equivalent, so they only ever
    # travel *in*: nothing here can read one back out.

    def _marketplace_sections() -> Dict[str, Dict[str, Any]]:
        """Every ``[marketplace.*]`` section in the file, by name."""
        from .config_api import scan_sections

        try:
            content, _mtime = config_service.read("primary")
        except KeyError:
            return {}
        found: Dict[str, Dict[str, Any]] = {}
        for section in scan_sections(content):
            if section.prefix != "marketplace" or not section.suffix:
                continue
            found[section.suffix.strip("\"'")] = dict(section.fields or {})
        return found

    def _configured_marketplaces() -> Dict[str, str]:
        """Marketplace name -> the implementation behind it.

        Every platform this monitor supports is here whether or not the file
        mentions it: a platform is a capability, not something the user adds,
        and a session panel that listed only the platforms someone had already
        declared was empty exactly when it was needed.

        Sections are still read, because one may be named something other than
        its platform (``[marketplace.houston]`` with ``market_type =
        "facebook"``) and the session belongs to the name every other part of
        the monitor stores under.
        """
        found: Dict[str, str] = {name: name for name in supported_marketplaces}
        for name, fields in _marketplace_sections().items():
            declared = str(fields.get("market_type") or "") or name
            if declared.lower() in supported_marketplaces:
                found[name] = declared.lower()
        return found

    def _marketplace_class(name: str) -> Any:
        """The implementation behind a configured marketplace section."""
        kind = _configured_marketplaces().get(name)
        return supported_marketplaces.get(kind) if kind else None

    def _ai_reader() -> Any:
        """The AI service to fall back on when a page publishes nothing.

        Loaded from the file here rather than borrowed from the monitor: the
        monitor keeps its `Config` on the scraping thread and publishes only a
        summary of it, and the backends are objects rather than summary.
        Building one costs a parse of a file that is already in the page cache
        and happens only when a preview is asked for.

        None whenever the configuration cannot be read, which is the honest
        answer: a preview that failed because the file was mid-save would be a
        spinner failing for a reason having nothing to do with the page.
        """
        try:
            from ..config import Config

            return track_reader_for(Config([config_service.editable_path]))
        except KeyboardInterrupt:
            raise
        except Exception:
            return None

    @app.post("/api/track/analyze")
    def analyze_tracked_page(
        body: Dict[str, Any],
        _: str = Depends(require_session),
        __: None = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """Read a product page and say what could be found on it.

        The "analizar página" step, before anything is written to the config.
        The answer names the strategy behind every field -- see
        :func:`ai_marketplace_monitor.tracking.preview` -- because a title from
        JSON-LD and a title guessed from an ``<h1>`` are worth different amounts
        of confidence, and this is the only moment somebody can be told which
        they are looking at.

        Sync def: it fetches a page, which is blocking work, so it runs on the
        thread pool rather than on the event loop.

        ``skip`` is what "reintentar extracción" sends: the strategies that
        produced the field the user says is wrong.  Dropping them makes the next
        one down speak, instead of asking again and getting the same answer.
        """
        url = str(body.get("url") or "").strip()
        if not track_is_watchable(url):
            raise HTTPException(
                status_code=400,
                detail="Pega la dirección completa de la página del producto, "
                "empezando con http:// o https://, o un archivo .html guardado "
                "(file:///…).",
            )
        raw_skip = body.get("skip")
        skip = tuple(str(name) for name in raw_skip) if isinstance(raw_skip, list) else ()
        # The AI is the last strategy and is only reached when the page
        # published nothing the other five could read -- see `extract`.  Built
        # per request rather than held, because the configured services change
        # whenever the file does.
        return track_preview(url, skip=skip, ai=_ai_reader())

    @app.get("/api/marketplace/sessions")
    async def list_sessions(_: str = Depends(require_session)) -> Dict[str, Any]:
        """What is stored for each marketplace, and never what it contains.

        ``credentials`` is the other way a platform can be signed in: a
        username and a password in the config, which Facebook can still use.
        Reported alongside the stored session so the interface can say "this
        platform will be searched anonymously" without having to read the
        config file and guess.
        """
        sections = _marketplace_sections()
        result: Dict[str, Any] = {}
        for name in sorted(_configured_marketplaces()):
            fields = sections.get(name, {})
            result[name] = {
                **session_info(name),
                "credentials": bool(fields.get("username")) and bool(fields.get("password")),
            }
        return {"sessions": result}

    @app.post("/api/marketplace/{name}/session")
    def put_marketplace_session(
        name: str,
        body: Dict[str, Any],
        _: str = Depends(require_session),
        __: None = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """Import a session for one marketplace from pasted cookies.

        Sync def: it writes a file and reads the config, which is blocking work.
        Accepts a cookie-manager export, a Playwright ``storageState``, or the
        bare ``Cookie:`` header line -- whichever the user's tools produce.
        """
        marketplace = _marketplace_class(name)
        if marketplace is None:
            raise HTTPException(status_code=404, detail=f"Unknown marketplace {name!r}")
        raw = body.get("cookies")
        if not isinstance(raw, str):
            raise HTTPException(status_code=400, detail="Missing 'cookies' text")

        domains = marketplace.session_domains()
        # A shop's bot-check cookies belong to the browser they were exported
        # from, not to the user, so they are not carried across.  Empty for the
        # marketplaces, which have none.
        challenge = getattr(marketplace, "challenge_cookies", ())
        try:
            cookies = parse_cookies(raw, default_domain=domains[0] if domains else "")
            result = import_session(name, cookies, domains, drop_names=challenge)
        except ValueError as error:
            # The user's paste was the wrong thing, which is a thing to say
            # rather than a server fault.
            raise HTTPException(status_code=400, detail=str(error)) from None
        if not result["ok"]:
            raise HTTPException(status_code=500, detail="Could not write the session file")

        # The browser belongs to the scraping thread; it loads these itself.
        control.request_session_import(name)
        # Stored now, live shortly: the monitor loads it between jobs, so a
        # search under way finishes on the session it started with.
        return {**result, "session": session_info(name), "pending": True}

    @app.post("/api/marketplace/{name}/session/apply")
    def reapply_marketplace_session(
        name: str,
        _: str = Depends(require_session),
        __: None = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """Load the stored session into the browser again.

        For a session that is still good but is no longer in the profile -- the
        site logged us out, or the monitor was restarted with a profile that
        never took the import.  Nothing is re-pasted: the cookies are already
        here.
        """
        if _marketplace_class(name) is None:
            raise HTTPException(status_code=404, detail=f"Unknown marketplace {name!r}")
        if not rearm_import(name):
            raise HTTPException(status_code=404, detail="No hay una sesión guardada que aplicar.")
        control.request_session_import(name)
        return {"ok": True, "pending": True, "session": session_info(name)}

    @app.delete("/api/marketplace/{name}/session")
    def delete_marketplace_session(
        name: str,
        _: str = Depends(require_session),
        __: None = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """Forget a stored session.

        The cookies already loaded into the running browser stay there until it
        is restarted -- this removes what would be replayed, not what is live.
        """
        if _marketplace_class(name) is None:
            raise HTTPException(status_code=404, detail=f"Unknown marketplace {name!r}")
        clear_session(name)
        return {"ok": True, "session": session_info(name)}

    @app.get("/api/logs")
    async def get_logs(
        limit: int = 500,
        level: str = "DEBUG",
        kind: str | None = None,
        item: str | None = None,
        min_score: int | None = None,
        _: str = Depends(require_session),
    ) -> Dict[str, Any]:
        level_value = logging.getLevelName(level.upper())
        if not isinstance(level_value, int):
            level_value = 0
        return {
            "records": log_handler.snapshot(
                limit=limit,
                min_level=level_value,
                kind=kind,
                item=item,
                min_score=min_score,
            ),
            "capacity": log_handler._buffer.maxlen,
        }

    @app.websocket("/ws/stream")
    async def ws_stream(websocket: WebSocket) -> None:
        # In open mode (loopback) skip cookie check; otherwise require
        # a valid session cookie on the WebSocket handshake.
        if not is_open():
            session = websocket.cookies.get(SESSION_COOKIE)
            if not session or sessions.validate(session) is None:
                await websocket.close(code=4401)
                return

        await websocket.accept()
        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=1000)
        log_handler.subscribe(queue)
        try:
            # Send a brief hello so clients know the stream is live.
            await websocket.send_json({"type": "hello", "time": time.time()})
            while True:
                payload = await queue.get()
                await websocket.send_json({"type": "log", "record": payload})
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: S110 — client disconnected; nothing to handle
            pass
        finally:
            log_handler.unsubscribe(queue)

    # ------------------------------------------------------------------
    # Optional noVNC bridge (Docker deployments)
    # ------------------------------------------------------------------
    novnc_dir = os.environ.get("AIMM_NOVNC_DIR", "/usr/share/novnc")
    vnc_host = os.environ.get("AIMM_VNC_HOST", "127.0.0.1")
    vnc_port = int(os.environ.get("AIMM_VNC_PORT", "5900"))
    if os.environ.get("AIMM_ENABLE_VNC") == "1" and Path(novnc_dir).is_dir():
        app.mount("/vnc", StaticFiles(directory=novnc_dir, html=True), name="vnc")

        @app.websocket("/ws/vnc")
        async def ws_vnc(websocket: WebSocket) -> None:
            if not is_open():
                session = websocket.cookies.get(SESSION_COOKIE)
                if not session or sessions.validate(session) is None:
                    await websocket.close(code=4401)
                    return
            # Echoed, never asserted.  RFC 6455 requires a client to fail the
            # connection when the server names a subprotocol it did not offer,
            # and noVNC stopped offering "binary" at 1.3 -- so answering every
            # client with it meant the browser closed the socket itself and
            # noVNC showed "Failed to connect to server", with nothing wrong at
            # either end of the VNC connection underneath.
            offered = websocket.scope.get("subprotocols") or []
            await websocket.accept(subprotocol="binary" if "binary" in offered else None)
            try:
                reader, writer = await asyncio.open_connection(vnc_host, vnc_port)
            except OSError:
                await websocket.close(code=1011)
                return

            async def ws_to_tcp() -> None:
                try:
                    while True:
                        data = await websocket.receive_bytes()
                        writer.write(data)
                        await writer.drain()
                except WebSocketDisconnect:
                    pass
                finally:
                    writer.close()

            async def tcp_to_ws() -> None:
                try:
                    while True:
                        chunk = await reader.read(65536)
                        if not chunk:
                            break
                        await websocket.send_bytes(chunk)
                finally:
                    try:
                        await websocket.close()
                    except Exception:  # noqa: S110 — already closed
                        pass

            await asyncio.gather(ws_to_tcp(), tcp_to_ws(), return_exceptions=True)

    # ------------------------------------------------------------------
    # Static UI
    # ------------------------------------------------------------------
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    # Sync def (not async): FastAPI runs it in a threadpool and Starlette
    # iterates the sync generator there too, so the blocking cache scan never
    # runs on the event loop. The body streams row-by-row rather than buffering
    # the whole CSV, keeping memory bounded for large exports.
    @app.get("/api/found.csv")
    def export_found_csv(_: str = Depends(require_session)) -> StreamingResponse:
        filename = f"found-items-{time.strftime('%Y%m%d-%H%M%S')}.csv"
        return StreamingResponse(
            iter_found_csv(iter_found_rows(cache)),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # Also sync def: the dashboard polls this, and reading observations out of
    # the cache is blocking SQLite work that has no business on the event loop.
    @app.get("/api/listings")
    def listings_sync(
        since: int = 0,
        limit: int = 0,
        _: str = Depends(require_session),
    ) -> Dict[str, Any]:
        """Incremental feed of observed listings for the dashboard's IndexedDB."""
        return build_sync_response(cache, since=since, limit=limit)

    # Sync def like the sync feed above: walking the observation store is
    # blocking SQLite work, and the body streams row by row so exporting a large
    # group never holds the whole CSV in memory.
    @app.get("/api/listings/export.csv")
    def export_group_csv(
        item: str,
        _: str = Depends(require_session),
    ) -> StreamingResponse:
        """Every listing of one search item, as a spreadsheet.

        ``item`` is the search item's name -- the thing the dashboard groups by.
        The whole group is exported, not the page being looked at, because the
        rows come from the store rather than from the client.
        """
        if not item.strip():
            raise HTTPException(status_code=400, detail="Missing 'item' query parameter")
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", item.strip()).strip("-") or "grupo"
        filename = f"{safe}-{time.strftime('%Y%m%d-%H%M%S')}.csv"
        return StreamingResponse(
            iter_group_csv(iter_group_rows(cache, item)),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # Sync def for the same reason as the two above: deleting walks the cache
    # under a transaction per key, which is blocking work.
    @app.post("/api/listings/delete")
    def listings_delete(
        body: Dict[str, Any],
        _: str = Depends(require_session),
        __: None = Depends(require_csrf),
    ) -> Dict[str, Any]:
        """Delete listings from the dashboard by their ``marketplace:id`` keys.

        Permanent: the monitor will not re-record a deleted listing even when a
        later search turns it up again.
        """
        keys = body.get("keys")
        if not isinstance(keys, list):
            raise HTTPException(status_code=400, detail="Missing 'keys' array")
        if len(keys) > MAX_DELETE_KEYS:
            raise HTTPException(
                status_code=413,
                detail=f"Too many keys in one request (max {MAX_DELETE_KEYS})",
            )
        return delete_listings(cache, keys)

    return app


# ----------------------------------------------------------------------
# Thread runner
# ----------------------------------------------------------------------


class WebUIServer:
    """Runs uvicorn in a background thread."""

    def __init__(
        self,
        config: WebUIConfig,
        state: AuthState,
        config_service: ConfigFileService,
    ) -> None:
        if config.log_handler is None:
            raise ValueError("WebUIConfig.log_handler is required")
        self._config = config
        self._state = state
        self._config_service = config_service
        self._app = create_app(config, state, config_service, config.log_handler)
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        uv_config = uvicorn.Config(
            self._app,
            host=self._config.host,
            port=self._config.port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        self._server = uvicorn.Server(uv_config)

        def runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            assert self._config.log_handler is not None
            self._config.log_handler.attach_loop(loop)
            self._ready.set()
            try:
                loop.run_until_complete(self._server.serve())  # type: ignore[union-attr]
            finally:
                loop.close()

        self._thread = threading.Thread(target=runner, name="aimm-webui", daemon=True)
        self._thread.start()
        # Give the loop a moment to bind so attach_loop completes before
        # any log records are emitted.
        self._ready.wait(timeout=5)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True


def start_webui(
    config: WebUIConfig, logger: logging.Logger | None = None
) -> tuple[WebUIServer, StartupInfo]:
    """Resolve auth, build the service, and start the server thread."""
    if config.log_handler is None:
        raise ValueError("WebUIConfig.log_handler is required")
    state, info = _resolve_auth(config)

    if config.open_access and config.host not in ("127.0.0.1", "localhost", "::1"):
        # Said out loud every time, because it is the one setting whose whole
        # effect is invisible until somebody else finds the port.
        (logger or logging.getLogger("monitor")).warning(
            f"The web UI is listening on {config.host}:{config.port} with no password, "
            "because --webui-open (or AIMM_WEBUI_OPEN) was set. Only do this when "
            "something else keeps the port private -- a container network, a firewall, "
            "or a reverse proxy that authenticates."
        )

    # --webui-host requires credentials. Refuse to expose without auth.
    if state.exposed and state.auth is None:
        raise RuntimeError(
            f"--webui-host {config.host} requires authentication. "
            "Set username/password in a [marketplace.*] config section "
            "or set FACEBOOK_USERNAME and FACEBOOK_PASSWORD environment "
            "variables. Omit --webui-host to run on 127.0.0.1 without "
            "a password."
        )

    config_service = ConfigFileService(config.config_files, logger=logger)
    server = WebUIServer(config, state, config_service)
    server.start()
    return server, info
