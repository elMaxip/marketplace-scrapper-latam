# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **The container looks like the machine it is standing in for, and this is
  measured rather than argued.** The same fingerprint probe was run on Windows
  with real Chrome — which has never once been walled by Lider, across a run
  that mapped 800 listings — and in the container, which was refused within ten
  seconds. The browser binary was not the difference. The clock was `UTC` and
  the language `en-US` while the request left from a Chilean address: the
  signature of a rented server, not of somebody shopping for a television. The
  image already carries `tzdata`, and Chromium reads `LANGUAGE` on its own, so
  `TZ` and `LANGUAGE` in the compose file now make the container report
  `America/Santiago` and `es-419` — character for character what Windows
  reports. Set them to your own if you run this from somewhere else.
- **`patchright` is installed in the image.** The comment in `pyproject.toml`
  said the container did not need it, and that was true while the container was
  only ever asked for Facebook and Mercado Libre. It is the one change here
  with a mechanism rather than a correlation behind it: the tell that survives
  every launch flag is the driver, and Playwright's leaves CDP traces a page can
  read. Still optional, still a silent fallback — uninstalling it changes
  nothing else.

### Fixed
- **A browser with no WebGL at all, which is worse than one rendering in
  software.** patchright's Chromium returns `null` from
  `canvas.getContext('webgl')` where Playwright's build quietly fell back to
  SwiftShader, so installing it for stealth would have traded a CDP leak for a
  browser unlike any that exists on a desktop. `--enable-unsafe-swiftshader`
  brings the context back with the same renderer string and the same
  thirty-five extensions as before. It permits the fallback rather than forcing
  it, so a machine with a GPU is untouched — and it is the one flag here that
  is *added* rather than dropped, because what a page reads is not the command
  line but what the flag does.

### Changed
- **A shop's refusal is answered with a new browser, not with a wait.** Being
  walled used to cost fifteen minutes of doing nothing and abandon the search
  half way through a catalogue. Now the search drops the identity the shop just
  refused, throws the browser and its profile away, opens a new one — reseeded
  from the stored session, so it arrives with the account and nothing the wall
  recognises — and asks for the same page again, all inside the search that hit
  the wall. Once per search: a shop that refuses everything must not become a
  loop of opening and closing browsers. The cooldown still exists and is now
  what it should always have been, the last resort.
- **A search walks a shop's catalogue to its end.** `max_pages` used to default
  to one page, so nine tenths of a shop did not exist for the monitor. Left
  unset it now pages until the shop runs out; set, it still means exactly that
  many. It stops on an empty page, on a page holding nothing the earlier ones
  did not, on the shop's own page count where it publishes one (Lider's
  `paginationV2.maxPage`), and at a ceiling of 25 pages for the next site to
  invent a way of never saying "no more".
- **Results pages are spaced like pages somebody reads**, four seconds apart and
  unevenly, where before the next one was requested the instant the last was
  parsed. With a search that now walks to the end of a catalogue, that would
  have been a burst.

### Fixed
- **Sodimac searches read one page of the catalogue and called it the end.**
  Any phrase the shop can map to a category — "taladro", most single words —
  redirects from `?Ntt=` to `/lista/...`, and the redirect keeps none of the
  query string, so the `?currentpage` the monitor appended never arrived. That
  route ignores `currentpage` anyway: its parameter is `?page`. Both halves
  were verified live, and together they meant the duplicate-page stop fired on
  page two every time — 558 taladros seen as 48. Page two onwards is now asked
  for at the address page one actually came back from
  (`pageProps.canonicalUrl`) with the parameter that route understands, through
  a new `next_page_url` hook whose default leaves every other shop alone.
- **Sodimac now says how many pages it has.** `total_pages` returned `None` on
  the grounds that the page size was not published. It is: `pagination.perPage`,
  48 on the category route and 28 on the search one, and the arithmetic closes
  exactly — 558 products at 48 is 12 pages, the twelfth came back with 30
  entries and the thirteenth empty. Not `totalPerPage` (56), which counts the
  sponsored cards padding each page and would end the search two pages early.
- **A shop's bot check no longer gets to keep its verdict for ever.** Sessions
  are filtered by domain, so PerimeterX's device id was stored in
  `sessions/lider.json` beside the login and seeded back into every new browser
  profile: deleting every profile to start clean produced a fresh browser that
  was refused one second after it opened, wearing the identity it had just been
  handed. Each platform now names those cookies (`challenge_cookies`); a refusal
  drops them, so the next profile starts with the account and an identity the
  wall has no history for, and an import drops them too, since the ones in an
  export belong to the browser they were copied from. Login cookies are never
  touched, and platforms whose device cookies are an asset rather than a
  liability — Facebook's `datr` — declare none.
- **Being refused a product page no longer stops a shop being searched.** Both
  shops serve their results grid far more willingly than their product pages,
  and a page opened only for a description was putting the whole platform on a
  fifteen-minute cooldown — skipping the next search, which was the part that
  still worked. Only a refusal on the page the search actually came for does
  that now; a walled product page keeps the listings already found, reads the
  rest from the search cards, and stops opening pages until one comes back.
- **A results page carrying a payload with no products in it is recognised as
  the wall.** Both shops' block pages are Next.js applications too, so a refusal
  could arrive with a perfectly good `__NEXT_DATA__` and be reported as "0
  results" — indistinguishable from a shop that sells none of it, and with the
  cooldown left switched off.
- **An imported session reaches every browser, not just the first one.** The
  monitor runs a browser profile per platform searched in parallel, plus one for
  the review; "applied" was one fact about the import, so the first profile to
  take it settled the question for all of them and the browser that actually
  searched the shop never got it.
- **"Ejecutar ahora" ignores a cooldown.** A back-off the monitor set itself is
  a guess, and a person asking for a search by name outranks it.

### Changed
- **The spacing between page loads varies instead of being exact.** Two seconds,
  precisely, forty-eight times in a row is a metronome rather than a slow
  visitor. `utils.human_delay` keeps the average — the bounds are the two-sigma
  points and symmetric, so a pass costs the same wall clock — and loses the
  regularity. `AIMM_HUMAN_PACING=0` turns it off.
- **A product page is opened only when its description decides something** — a
  keyword filter, an AI rating, or `in_stock_only`, which needs a stock level no
  grid publishes. Otherwise the card already carries the title, price, image,
  seller and condition, and the review reads the page later anyway.
- **Real Google Chrome is used where the machine has it**, and Playwright's
  bundled Chromium where it does not. Where `patchright` is installed it drives
  the browsers instead of Playwright; both are optional and absence changes
  nothing.
- **Searching the platforms in parallel, and reviewing stored listings in
  parallel, are both on by default.** Off, the monitor works through one queue
  and one browser: every Facebook search finishes before the first Mercado
  Libre one starts, and the re-checks only happen in the gaps between searches
  — which on a busy schedule is barely at all. Both remain switchable; an
  explicit `false` is still honoured.

  Turning them on by default meant fixing two things that had been broken in
  parallel mode for as long as it existed, and were survivable only because
  hardly anyone was in it: **"Establecer como siguiente búsqueda" did nothing**,
  and **a stop was never spent**, so the same search stayed silently skipped on
  every later pass. Both had the same cause — the parallel pass ran its own
  share through the sequential one narrowed to a single platform, which is
  exactly the case that declines to honour a promotion or clear a stop, and
  correctly so, since it cannot see the other platforms. The whole pass now
  claims both, and hands the promotion to the lanes as well.

- **The description limit counts words, not lines** — `[monitor]
  max_description_words`, 25 by default, `0` for no limit. Reported as
  "messages that were limited and are still far too long", and the reason is
  that a line is not a property of the text at all: it is a property of the
  screen showing it. The same five lines is five short ones on a desktop and
  fifteen wrapped ones on a phone — and a seller who types one unbroken
  paragraph has a description of *one* line, so the limit never touched it.
  Words measure the text itself, so the answer is the same everywhere. Over the
  limit the text ends in `...`.

  `max_description_lines` is accepted and ignored rather than rejected, so a
  file written while it existed still loads; the web UI drops it from the file
  the next time the notification settings are saved. It is deliberately not
  re-read as a word count: "5 lines" and "5 words" are not the same request.

### Fixed
- **A search that was stopped threw away every listing it had found.** With
  `notify_immediately` off — the default — the one notification a search sends
  is built after its loop over the listings, and every way of ending a search
  early raises through that point: "detener esta búsqueda", "detener esta
  plataforma", "Pausa" and "Detener". So stopping a search that had already
  found and scored eight listings sent nothing about any of them, which is why
  it read as "I stop a search and Telegram goes quiet". Nothing was lost
  permanently — the listings were never marked as notified — but the next
  message about them waited a whole interval, which on a marketplace where a
  well-priced console is gone in ten minutes is the same as never. The search
  now says what it found and *then* stops. It costs the stop nothing: the send
  is queued on the dispatcher's thread, as every other notification is.

- **Mercado Libre reported a perfectly good imported session as unrecognised,**
  and left a stray tab titled "Resumen" open in the browser Facebook was
  searching in. One cause, two symptoms: `is_signed_in()` opens the account
  page and then asked two questions about where it landed, and both were wrong.
  `myaccount.mercadoli` is one of the sign-in-wall URL markers — it means *sent*
  to the account area — so the page the check deliberately opened was read as a
  redirect away from itself; and Mercado Libre has since moved the account area
  behind that host, so a signed-in visitor now ends on `www.<site>/resumen` (the
  page titled "Resumen"), which the "are we still on `myaccount`?" half read as
  being bounced to the front page. The check could not return true for anybody:
  not for an imported session, not for a saved one, not for a sign-in just
  completed under `--login`. The markers are now told which URL was asked for, a
  marker matching it is not an answer, and the account area is recognised
  wherever the site currently keeps it. The probe also borrows a tab of its own
  and gives it back, so it no longer leaves one open.

- **A stopped search could stay on "deteniéndose…" for ever.** The stop register
  was cleared only at the end of a pass over the *whole* queue. Saving a search
  starts a pass narrowed to the searches it touched, and a stop made during one
  of those was cleared by nobody: the interface went on reporting the search as
  stopping — accurately; the request really was still standing — while its next
  run counted down beside it, and the next full pass silently skipped it once
  for a button pressed an hour earlier. A narrowed pass now spends the stops of
  the searches it held, and the loop drops any that are left when it goes idle,
  because a stop lasts for the pass it was made in and there is no pass.

- **In a container the monitor could not start at all, for ever.** Chromium
  claims a profile with a `SingletonLock` naming `<hostname>-<pid>` and refuses
  to open one whose lock names anything else. The profile lives in a volume and
  the volume outlives the container, but the hostname does not — so an ordinary
  `docker compose up` after the container was replaced found a lock naming a
  machine that no longer existed. Chromium declined, Playwright reported "Target
  page, context or browser has been closed", and the monitor turned that into
  "No browser could be launched. Please ensure Chromium … is installed", which
  sends the reader looking for a missing package. Under supervisor it was a
  restart loop with a web UI that never came up. A lock written by another host,
  or by a process that is gone, is now released before the launch and said so in
  the log; a lock held by a live process here is never touched. Launch failures
  also carry the reason they failed instead of hiding it at debug level.

- **noVNC answered "Failed to connect to server" with nothing wrong behind it.**
  The websocket bridge accepted every client with the `binary` subprotocol,
  which noVNC stopped offering at 1.3 — and RFC 6455 requires a client to fail
  the connection when the server names a subprotocol it did not offer. The
  subprotocol is now echoed when it is offered and omitted when it is not, so
  both old and current noVNC connect.

- **The log filled with filesystem events and nothing else.** `watchdog` logs one
  `in-event <InotifyEvent …>` line per event under the data directory at DEBUG,
  which includes every write to the log file it is writing; the root logger is at
  DEBUG whether or not `--verbose` was passed. A container filled all five 1 MB
  rotated logs with them inside a minute — taking the record of what the monitor
  had actually done with it — and broadcast the same flood over the web UI's log
  socket. It joins the other libraries that are turned down.

- **The Telegram bot token was written to the log in clear text.**
  `python-telegram-bot` announces `Set Bot API URL:
  https://api.telegram.org/bot<token>` at DEBUG and then logs every call's
  parameters, chat id included. The root logger is at DEBUG whether or not
  `--verbose` was passed, so a single run put the token in
  `ai-marketplace-monitor.log` dozens of times — and the web UI's own handler is
  at DEBUG too, so it went out over the log websocket to anyone with the
  interface open. The `telegram` loggers are now silenced like the other
  chatty libraries.

  **If you have run a version before this one with Telegram configured, treat
  the token as exposed: revoke it with @BotFather and delete the old log file.**

- **The sign-in wait was the one place the monitor stopped listening.** It polls
  for up to five minutes by default and read no flags at all, so for the whole
  of it "Detener búsqueda de esta plataforma" sat on "deteniéndose…", a pause
  did nothing, and a configuration change that deleted that very search went
  unnoticed — all three are read at checkpoints, and this loop had none. It is
  now a checkpoint like every other long wait. `--login` is unaffected: with no
  cancellation asked for and no guard installed, the check is a no-op, which is
  what keeps the manual sign-in untimed.

- **An extra browser sitting at about:blank.** Not a race at launch — the
  profile's first tab is always there by the time `launch_persistent_context`
  returns — but a browser opened before anything had checked whether there was
  work for it. Three places, now all guarded: the review lane started even with
  nothing in the store overdue (most visible on a fresh install, where it had
  nothing to do for its whole life); a search lane started before finding out
  that every one of its searches was going to be skipped, for a platform on
  cooldown or already told to stop; and a leftover blank tab beside a real one
  is now closed, never the last one, since closing every tab of a persistent
  context takes the browser down with it.

### Added
- **Notificaciones inmediatas** (`[monitor] notify_immediately`, off by default).
  On, a listing is notified the moment it has passed every filter and been
  scored, rather than at the end of the platform's search. A Facebook pass over
  a handful of products is the better part of an hour, and the listing that made
  the search worth running was found in the first two minutes of it; on a
  marketplace where a well-priced console is gone within ten, that silence is
  the difference between a monitor and a diary.

  Off by default because the *message* is better batched: a search that turns up
  six listings sends one notification about six of them, and this makes it six
  notifications. Whether that trade is worth it is a judgement about the market
  being watched.

  Either way, **nothing is sent on the scraping thread any more**. Notification
  channels block for as long as their service wants — Telegram waits out an HTTP
  429, SMTP waits for a TLS handshake, Pushbullet loads libmagic — and the
  checkpoints that read the pause and cancel flags live in the scraping code, so
  a notification sent inline was a page left open and a "detener" button that
  did not answer until it finished. Sends now go to one worker thread behind a
  queue (`dispatch.py`): one worker, so listings are told in the order they were
  found and two threads never race to write the same "already notified" entry;
  a channel that fails is logged rather than allowed to hold up what is behind
  it; and stopping the monitor drains the queue rather than dropping it.

- **Máximo de palabras de descripción** (`[monitor] max_description_words`,
  25 by default, `0` for no limit). How many words of the seller's own text a
  notification carries. Only the notification's copy is shortened: what the
  scraper stores is the seller's whole text, and the dashboard, the export and
  the AI all go on reading it.

### Fixed
- **Telegram: "Max retries (3) reached for Telegram errors: Message is too
  long."** A card was rendered and sent with no length check at all, and a
  Mercado Libre seller's twelve-screen description made it six thousand
  characters. Telegram does not truncate such a message, it refuses it — three
  retries later the listing had never been delivered.

  Fixed in the one place that decides what a message says rather than per
  channel. Every channel now declares the limit it actually has (Telegram 4096,
  Pushover 1024, ntfy 4096) and the card is *rebuilt shorter* until it fits: the
  seller's description first, then the AI's comment, then the title — never the
  price, the platform or the link, which are the message. Rebuilt and re-rendered
  rather than cut, because cutting rendered MarkdownV2 can strand a backslash
  from the character it escapes and Telegram rejects that just as firmly, for a
  different reason. A batch too big for one message becomes several messages
  rather than fewer listings.

- **A platform could end up searching on another platform's browser.** Which
  browser a platform ran on was decided per pass — "the first platform in this
  pass keeps the monitor's own browser, the rest get lanes" — and a pass was
  built from whatever happened to be *due at that instant*. So a platform that
  ran on its own lane at 14:00, because something else was due alongside it, ran
  on the monitor's browser at 14:20 when it was the only one due. From outside
  that is a search inheriting the browser another platform had been using. Each
  platform is now bound to one browser the first time it is searched, and keeps
  it.

- **Parallel searching was a barrier, so one platform's queue delayed the
  other's.** Work was handed out and then every lane was joined before the
  monitor thread touched the schedule again: a lane that emptied its queue in
  two minutes sat holding an open browser for the fifty the slowest participant
  took, with its next search not even chosen. Two searches ran at once and their
  *cycles* were locked together, which is the one thing parallel searching was
  for. Lanes are now reaped as they finish — searches marked as run, schedule
  republished, anything newly due handed straight back — both between the
  monitor thread's own searches and while it waits.

- **"Próxima ejecución" showed a slot that had already gone by** after
  cancelling a platform, sometimes for minutes. Nothing was wrong with the
  interface: the job really did still hold the old slot when the state was last
  published, because `_run_job` publishes in its `finally`, which runs *before*
  the caller advances the job, and nothing published again until the next search
  ended. The schedule is now published where the change actually happens.

- **The Estado screen never received `due_now` at all.** `control` works out
  that a waiting search's slot has gone by, `/api/status` carries the answer,
  and the row `/api/scraper/state` builds simply dropped it. The screen was
  left with a timestamp in the past under a label reading "próxima" and
  rendered the only thing it could: **"Próxima ejecución: en cualquier
  momento"** — the exact words in the report. Found by watching the real
  screen; every unit test around it passed, because they all tested the half
  that worked.

- **A search that had not run yet showed as "sin programar"** for the whole
  first interval after each restart, while the phase line directly above it
  named the very slot it was denying. Runtime history is per process and the
  scheduler's slots survive (they are seeded from what each pair last did), and
  the row was reading the next run only from the history. It now falls back to
  the scheduler's own answer (`control.next_run_for`).

- **`due_now` compared timestamps from two different time zones as text.** The
  scheduler publishes a slot as local time with its own offset
  (`2026-08-23T13:37:55-04:00`) and `control` stamps in UTC; compared as
  strings, that is two wall clocks in two zones. In Chile every waiting search
  read as permanently overdue; four hours the other way, an overdue one would
  read as not yet due. Both timestamps are now parsed.

- **The browsers were never closed.** A search every half hour held a Chromium —
  two, with parallel platforms — and a visible window for twenty-nine minutes
  out of every thirty. A gap longer than two minutes now releases them (the
  review lane excepted: when reviews have a browser of their own they are using
  it in exactly that gap), and the next search opens them again on the same
  persistent profiles, which costs a browser start and no sign-in.

- **Closing the browser by hand left the monitor believing in it.** The
  `BrowserContext` stays perfectly valid Python after the process behind it
  dies, so the monitor went on reporting that it was searching while there was
  no browser at all, and a lane kept its dead context and failed every later
  search in the same way. The browser is now *asked* before every use, and a
  lane opens a replacement on its own thread when its own has gone.

- **`SIGTERM` skipped the cleanup entirely.** Python's default handler ends the
  process where it stands: no exception, so no `finally`, so `stop_monitor`
  never ran — the browsers were killed with the process, the persistent profile
  was not flushed (which can lose the session the profile exists to keep), and a
  queued notification was dropped. That is exactly what `docker stop` sends, so
  the container's every shutdown was the ungraceful one. It now takes the same
  path as Ctrl-C.

### Changed
- **The Dockerfile is a multi-stage build**, and the process inside it is no
  longer root. The build stage installs into a virtualenv and is thrown away, so
  pip's cache, the source tree and the build backend do not ship. Chromium and
  the X and font libraries it needs are deliberately *not* slimmed: without them
  the browser starts and dies on the first page, which looks like a scraping bug
  rather than a missing package.

  **Upgrading:** the data directory moved with the user, from
  `/root/.ai-marketplace-monitor` to `/home/aimm/.ai-marketplace-monitor`. That
  is where the config, the listing store and the browser profiles live, so an
  existing mount has to be repointed.

- **`--webui-open` / `AIMM_WEBUI_OPEN`** serves the web UI without a password
  even when the bind address is not loopback. There is one situation it is for:
  a container has to bind `0.0.0.0` to be reachable from the container next to
  it, and the bind address was what the loopback rule read — so a perfectly
  private deployment looked identical to one on the open internet and the server
  refused to start. Off by default, has to be asked for by name, and says so in
  the log every time it is used.

### Added
- **Notifications are written to be read in a second.** The message a channel
  sends is now built once as a *card* — title, price, what the price used to be,
  how far it moved, rating, place, platform, link — and each channel renders
  that card in whatever it can display. What this replaces opened with a
  paragraph of AI commentary and buried the number underneath it.

  A missing fact drops its line rather than rendering an empty one, which
  matters because half of what a marketplace prints is missing half of the time.
  Prices are still never re-formatted: a Chilean Facebook listing prints
  "450 000" with no symbol at all, and inventing a "$" for it would be inventing
  a fact. Only the *difference* between two prices is computed here, and it
  carries whatever symbol the marketplace itself used.

- **Telegram sends the listing's photo.** A picture of the thing being sold is
  worth more than any description the seller wrote, and Telegram will carry it —
  but only attached to a message about *one* listing, which the pre-joined block
  of text this replaces could never be. Each card goes as its own message with
  the photo above it and the link as a button. A listing with no image, an image
  Telegram will not fetch (Facebook's URLs expire), or a card longer than a
  caption may be all fall back to text; none of them blocks the notification.

- **Controls for the pass under way**, in *Estado → Búsquedas que el scraper
  está usando*, none of which stop the scraper:
  - *Detener esta búsqueda y pasar a la siguiente* — on the search actually
    running. It ends as if it had finished, including on the platforms it has
    not started yet, and the next search begins. No browser is closed.
  - *Detener búsqueda de esta plataforma* — on each platform of that search.
    The other platforms carry on; when the last one goes, the search is over and
    the monitor moves on immediately.
  - *Establecer como siguiente búsqueda* — on the searches that are not running.
    It jumps the queue without interrupting anything, and is marked with a blue
    border that stays put whether or not the pointer is near it.

- **Two settings for what a save means to work in progress**, under `[monitor]`:
  `apply_changes_while_running` (default on) and `on_delete_running` (default
  `"stop"`). See `docs/webui.md`.

### Changed
- **"Iniciar" no longer means "search everything now".** It activates the
  scraper and lets it run the searches that are *ready*, respecting the
  intervals that were configured. Overriding the schedule is a separate control,
  *Buscar ahora*.

  This needed the monitor to remember something it never had: when each
  `(search, platform)` pair actually last ran, persisted in the new `search-runs`
  cache namespace. `schedule` starts a job's clock when the job is built, and the
  schedule is rebuilt on every start *and every configuration change* — so every
  start was a full pass over everything, and editing one search quietly
  postponed all the others by a whole interval.

- **"Pausa" now stops the running search instead of waiting for it.** It used to
  hold back only what had not started, which on a Facebook pass is twenty
  minutes of a button that visibly did nothing. It cuts the search off at the
  next checkpoint and leaves every browser, tab and signed-in session open, so
  resuming costs one search rather than one sign-in. The interrupted search is
  never marked as run, so it is due again on resume.

  "Detener" is unchanged in what it means and is now the *only* difference that
  matters: same interruption, and then the browsers are closed.

- **Editing the search that is running no longer abandons it.** The new settings
  go into the search already under way and it carries on — lowering a maximum
  price is not an instruction to throw away a page that has been loaded and the
  AI calls already spent on it. What a running search genuinely cannot absorb
  (anything that went into the URL it is paging through: the city, the price
  band, the phrases) is *named* and reported as waiting for that search's next
  run, rather than being counted as applied. Set
  `apply_changes_while_running = false` for the old behaviour.

- **A change that does not touch the running search is adopted at once**, rather
  than at the end of whatever is running. A search created from the web UI now
  appears in *Búsquedas que el scraper está usando* immediately; when it runs is
  still up to its own schedule.

### Fixed
- **"Última ejecución: en 8 minutos" and "Próxima ejecución: hace 8 minutos".**
  Both were reachable, and both are sentences that cannot be true. Three causes,
  all fixed at the source rather than papered over in the display:
  - the scheduler's timestamps went out as naive local times, so a browser in
    another zone resolved them against its own clock;
  - a search whose slot has passed while something else runs is *waiting its
    turn*, not late — the monitor now says `due_now` and the screen says "en
    cuanto le toque";
  - a review round skipped because the monitor was paused left its slot behind
    in the past instead of moving it.

  On top of that, a label that has committed to a direction is no longer
  contradicted by the value under it: "última" never renders a future and
  "próxima" never renders a past, whatever the clocks say.

- **Next-run times are now published per platform, not per product.** One
  product can be due on one platform and not on the other — a single figure had
  to be wrong about one of them.

- **A saved edit could be rejected because of a secret the user never touched.**
  Editing the config from a browser tab that had outlived a monitor restart
  failed with `telegram_token must contain a colon` — a complaint about a field
  the form had not even opened. The tab PUTs back what it fetched, masks and
  all, and the secret map that turns `<REDACTED>` into the real value is filled
  in by a *read*: a fresh process had never read the file, so every mask passed
  through untouched and the loader refused the result. The masks are now
  restored from the file as it is at that moment rather than from whatever the
  last read left behind.

- **A search stopped from the web UI was recorded as having failed.** The two
  context managers that classify how a job ended were written before there was
  a way to stop one search on purpose, so it fell through to their catch-all —
  and a control that worked exactly as designed reported itself to the user as
  a fault.

- **Mercado Libre's discounted prices were printed raw in notifications.** The
  scraper stores them as the page shows them, `"$74.990 | $99.990"` — asking
  price, then the price struck through. Next to a previous price that came out
  as `$89.990 → $74.990 | $99.990`: three numbers and no answer. The two halves
  are two different facts and the message now has a place for each.

- **A review round that could not run left its slot in the past.** While the
  monitor was paused or stopped, "próxima revisión" went on counting backwards
  from a moment that had already gone by. Nothing is scheduled while the monitor
  is held back, and that is now what it says.

- **The second marketplace was scheduled, reported as configured, and never
  actually searched.** With Facebook and Mercado Libre both set up, the queue
  was built one platform at a time — every Facebook search, then every Mercado
  Libre one, because that is the order the configuration is read in — and worked
  through in that order. A Facebook pass over a handful of products is the
  better part of an hour, and a forced stop or a restart sends the pass back to
  the top of the queue, so in practice the platform at the end of the queue was
  never reached at all. It looked exactly like a marketplace that had quietly
  stopped working.

  The queue now **alternates between platforms**, and keeps alternating across
  the re-derivation the pass does after every single search (which exists so
  that a search deleted mid-pass is not run). Order *within* a platform is
  untouched, so nothing else about the queue changes.

- **Two flows could re-read the same listing minutes apart.** A listing being
  read right now was already claimed, so nothing was ever read *twice at once*;
  but the review queue is a snapshot, and a listing read by the other flow
  between the snapshot and its turn was read again anyway. Freshness is now
  asked again at the moment a listing's turn comes, not only when the queue was
  built.

### Added
- **The platforms can search at the same time**, one browser each, behind
  `parallel_marketplaces` in `[monitor]` (off by default). Each platform gets a
  thread and a browser of its own and they run side by side; a failure or an
  early finish on one does not touch the other, and a platform whose browser
  will not open is searched in turn on the main one rather than skipped.

- **Re-checking stored listings can run *alongside* searching** rather than in
  the gaps between searches. `parallel_listing_updates` used to mean "a second
  tab on the same browser", which still meant the two took turns; it now gives
  the review a browser and a thread of its own, so a round of re-checks and a
  search genuinely happen at the same time. Still off by default.

  Both of these need a browser and not a tab for the same two reasons:
  Playwright's synchronous API is bound to the thread that created it — touching
  a page from another thread does not race, it fails — and Chromium takes an
  exclusive lock on its profile directory, so two browsers cannot share one.
  Each lane gets `browser-profile-<name>` beside the main one, seeded from the
  stored sessions so it opens already signed in. The first marketplace in the
  file keeps the monitor's own browser and profile, so the one holding the
  session is never copied.

  The two flows are kept off each other's work by three things that hold however
  many are running: the review queue is ordered by `last_seen`, which both flows
  write, so a listing a search has just fetched is not stale and is not in it;
  freshness is re-checked when a listing's turn comes; and a listing being read
  is claimed, with the loser skipping rather than waiting. A marketplace that
  refused either flow goes on a cooldown both read.

- **Reviewing stored listings has a schedule of its own**, with the same three
  modes searching already had, plus the one that only makes sense here:

  - `listing_review_interval` — a fixed interval, or the floor of a range;
  - `listing_review_max_interval` — the ceiling, making it random;
  - `listing_review_start_at` — fixed times of day, combinable with either;
  - `listing_review_batch` — how many listings one round re-checks.

  With none of them set the monitor keeps its old rhythm exactly, so an existing
  configuration behaves as it did. The next round's moment is drawn once, when a
  round ends, and published: asking for it on demand would give a random
  interval a different answer every time the interface refreshed. `/api/scraper/
  state` now reports when the next review is due, when the last one ran, whether
  one is running, and the schedule deciding it.

### Changed
- **The regions shipped inside the package are gone.** `ai_marketplace_monitor/
  config.toml` used to merge in a dozen `[region.*]` blocks (`usa`, `can`,
  `mex`, `bra`, `arg`, `aus`, `nzl`, `ind`, `gbr`, `fra`, `spa`, ...) that were
  only useful to somebody living in a country whoever wrote them had thought of
  — there was never a Chilean one — while every one of them filled the web UI's
  region picker for everybody. Regions are now only what the user defines in
  their own configuration, which is what `[region.*]` always was; the web UI
  grows a panel for saving them, so a Facebook city id is entered once instead
  of being repeated in every search.

  A search naming a region that does not exist is now refused with that
  region's name in the message. It previously raised a bare `KeyError`: the
  lookup happened before the check meant to catch it.

- `--clear-cache` clears every lane's browser profile, not only the main one.
  Leaving one behind would be a "clean install" that still remembered.

### Added
- **A configuration saved while the monitor is running is taken up at once**,
  and the interface says so. The monitor used to notice a changed file only
  between searches and while asleep, and to answer it by throwing the whole
  schedule away and searching everything again from the top. Both were wrong for
  the way the web UI is used: a search deleted while it was running went on
  running for however long it took to finish, and adding one search re-scraped
  every other one as a side effect.

  The file is now looked at from the checkpoints the scraping code already stops
  at — between listings, between search phrases, before a navigation — and what
  happens next depends on *who the change touches*:

  - **The search under way was deleted, switched off, edited, or its platform's
    settings changed.** It is dropped where it stands and the monitor moves on
    to the next search. Finishing would spend a page load and a round of AI
    calls producing results judged against settings the user has already
    replaced. The browser stays open: nothing is wrong.
  - **The change is about something else** — another search, a notification
    token, the schedule. The search under way finishes untouched and the change
    lands the moment it ends, which is as immediate as it can safely be for a
    loop that searches one thing at a time. The pending change is logged once,
    so a save is never met with silence.
  - **The searches the change added or edited are searched straight away**; the
    ones it did not touch keep their places in the schedule.
  - **A pass already half done keeps its place.** A search that survived the
    reload unchanged is not searched twice for somebody else's edit; one that
    was edited is searched again, under its new settings.
  - **A file caught halfway through being saved is never an outage.** It cannot
    be parsed, so it is not adopted, and the monitor carries on with the
    configuration it already has. It is complained about once, not at every
    checkpoint.

  What the monitor did about it is reported rather than left to be inferred:
  `config_sync.applied` on `GET /api/status` and `config.applied` on
  `GET /api/scraper/state` carry the change itself — which searches were added,
  removed, re-enabled, switched off or edited, which platforms and whether the
  schedule moved — along with the search that had to be abandoned for it, if
  there was one. The web UI turns that into a message on whatever screen the
  user is on: *"Configuración aplicada: se eliminó la búsqueda X. El scraper ya
  la está usando."* Two equal hashes prove *a* change landed; this says *which*.
- `superseded` joins `finished`, `cancelled` and `failed` as an outcome a search
  can have. It is not a failure: the search was dropped part-way because the
  user replaced the configuration under it.
- `GET /api/scraper/state`: the monitor's own account of what it is doing and on
  which configuration. It keeps three things apart that the web UI used to have
  to conflate — the configuration **persisted** on disk, the one the scraping
  thread has actually **loaded** (resolved: every default applied, every
  inherited option folded in), and the **runtime** state: the phase and since
  when, the searches it holds with their last and next run, and how the listing
  re-checks are getting on.

  Saving a change and the scraper using it are two different events, and the
  gap between them is real — an execution already under way finishes on the
  configuration it started with. The two are compared by hashing the
  configuration files twice: once as they are now, once as they were when the
  monitor read them. `GET /api/status` carries the same comparison under
  `config_sync` so the answer is available on the poll every screen already
  makes. Secrets are masked on the way out, lists and nested tables included.
- The web UI gained an **Estado** screen built on that endpoint: the phase, the
  searches the scraper is actually running (per product and platform, with the
  query, the last run and the next), the progress of the listing re-checks, the
  monitor's log live over the existing `/ws/stream` socket, and a read-only view
  of the saved file next to the configuration in force.

- A session can be imported from your own browser, from the web UI (Ajustes →
  Plataformas → Sesiones del navegador) or `POST
  /api/marketplace/<name>/session`. Mercado Libre does not reliably let a
  sign-in complete inside an automated browser — the form is accepted and you
  are returned to the front page with no session — so the way through is to copy
  across one you already have. The paste can be the `Cookie:` header from
  devtools, a cookie-manager JSON export, or a Playwright `storageState`.

  Cookies for other sites in the same paste are dropped, the file is owner-only,
  and nothing reads a value back out: the interface reports counts, domains and
  expiry dates only. The monitor loads the session into the running browser
  between jobs and lifts that marketplace's cooldown.
- Mercado Libre's sign-in wall is recognised and respected. After enough page
  loads the site stops serving listings and serves a "create your account"
  screen instead — which parses as a perfectly good page, so the monitor used to
  read it as a listing with no title and keep going. It is now detected (a
  redirect to a sign-in host, one of the site's own panels, or a password field)
  and the marketplace goes on a **shared, escalating cooldown**: 15 minutes,
  then 30, an hour, two, four, cleared by the first page that comes back
  normally. Both the search and the listing refresher read the same cooldown, so
  a second browser tab cannot keep knocking. Nothing is ever deleted because of
  a wall — a refusal says something about us, not about the listing.
- `ai-marketplace-monitor --login` now signs in to Mercado Libre too: it opens
  the site, waits with no deadline while you sign in by hand, and keeps the
  session in the browser profile. Nothing is typed in for you. The command also
  no longer aborts when a configured marketplace has no sign-in of its own.
- `require_login` for `[marketplace.mercadolibre]` (default `false`): refuse to
  read the site at all until a session has been signed in, and treat a wall as
  "the session is gone" rather than as something to retry shortly.
- The listing refresher deals its work out one marketplace at a time, so a slice
  never fires all of its page loads at the same site.
- The browser now does a second kind of work: re-reading listings that are
  already stored, so a price change, a sold item or a dead link is noticed
  without waiting for the listing to turn up in a search again. It runs between
  searches and while waiting for the next one, a few listings at a time, and is
  configured in `[monitor]`:

  ```toml
  [monitor]
  # how stale a listing has to be before its page is opened again
  listing_recheck_interval = '6h'
  # give that work a browser tab of its own, instead of sharing the search's
  parallel_listing_updates = false
  ```

  `parallel_listing_updates` is off by default on purpose: reading listing pages
  at the same time as a search is more traffic from one session than a
  marketplace expects, so it is meant to be turned on once and watched.
- Facebook Marketplace listings that are **sold** or whose page no longer exists
  are removed from the store during a re-check, permanently (a tombstone, so the
  next search does not put them back). Only positive evidence counts — a `Sold`
  badge on the listing's own heading, or Facebook's "this content isn't
  available right now" card. A timeout, a network error, a rate limit, a login
  wall or an unfamiliar layout all leave the listing untouched, because none of
  them says a listing is gone.
- A listing read within `listing_recheck_interval` is not opened again, by the
  refresher or by a search that turns it up; and the two flows take an exclusive
  claim on a listing, so they can never fetch (or judge) the same one at once.
- `target_price`, per platform: what you hope to pay for a product on that
  marketplace. It is never sent anywhere and filters nothing — the web dashboard
  measures the cheapest valid listing found against it. It lives in
  `[item.<name>.<marketplace>]` because the same product is worth a different
  price on Facebook and on Mercado Libre.
- Web UI endpoints for driving the scraping loop directly:
  - `POST /api/monitor/run` searches everything now. Refused rather than queued
    while a search is already running, so a second full pass is never stacked on
    the first. It replaces the old trick of touching the config file to wake the
    monitor, which still works.
  - `POST /api/monitor/pause` takes `force: true`, which additionally asks the
    running search to stop at its next checkpoint and closes the browser. The
    plain pause still lets the search under way finish. The forced state is
    persisted, so a monitor restarted mid-stop does not resume scraping.
  - `GET /api/status` reports `pause` and `scraping`: whether a search is
    running, what it is working on, and whether a stop has been asked for and
    not yet acted on.
  - `GET /api/listings/export.csv?item=<name>` exports every listing of one
    search item as a spreadsheet — the whole group, from the store, with the
    price now, the price before, the change, and a UTF-8 BOM so Excel shows
    accents correctly.

### Changed
- **Zero searches is a supported state.** A fresh install starts with none, and
  the last search can be deleted. Previously the configuration loader required
  an `[item]` section, so deleting the last search came back rejected by the
  validator with no explanation the user could act on; and the first-run
  template shipped an example GoPro search purely to satisfy that rule. The
  template no longer invents a search, and the loader no longer insists on one.
- A monitor with nothing to search now waits, quietly and cheaply: no browser is
  launched, no page is opened, and it wakes on the configuration file changing
  rather than cycling every sixty seconds logging an error about a state the
  user may well have chosen. It reports itself as `waiting_for_config` and picks
  up the first search added without a restart.
- `[marketplace.facebook]` defaults `language` to `es_LA`, and an empty
  `language = ""` is treated as unset rather than as a value. Facebook serves
  Marketplace in the account's own language and the parser reads the page by its
  labels; with nothing configured it matched English, which on a Spanish account
  does not fail loudly — the listing parses with no seller and no condition, and
  the search quietly returns nothing useful.

### Fixed
- The AI services are rebuilt on each reload rather than added to. They were
  appended to a list that was never cleared, so every reschedule left another
  copy of every service behind — and now that the schedule is rebuilt whenever
  the configuration changes, a service the user had just removed would have gone
  on being asked, once more per save.
- Scheduled searches are run one at a time by the monitor itself instead of
  through `schedule.run_pending()`. That call ran them in a batch of its own:
  the phase went unreported while it worked, a pause was not honoured between
  two of them, and an abandoned search escaped the loop as an exception.
- Deleting every search while the monitor was idle no longer stops it. Having
  nothing to search is a state to wait in — the monitor already waits for the
  first search on a fresh install — and it is now waited in here too, rather
  than taken as "no more active job" and exited on.

- An imported session now actually reaches the browser. The request to load it
  lived only in memory, so a monitor restarted before it was taken lost the
  session entirely — and an established browser profile is never re-seeded from
  disk, so it was never picked up again. The stored file now records that it is
  waiting, every browser launch takes up whatever is pending, and it is applied
  exactly once so a live session is never overwritten by an older copy of
  itself. `POST /api/marketplace/<name>/session/apply` re-arms a stored session
  without re-pasting it.
- After loading an imported session the monitor says whether the site actually
  accepted it. A set of cookies that does not sign in is otherwise
  indistinguishable from one that does, until searches quietly come back empty.
- `--login` for Mercado Libre no longer reports success without a sign-in. It
  waited for a page with no sign-in wall on it, and the site's own front page is
  exactly that, so it saved an anonymous session after three seconds. It now
  confirms by loading the account page, which only a signed-in visitor is
  allowed to see, and it waits passively instead of reloading under the user.
- The listing refresher no longer starves. Listings whose search had been
  deleted or renamed were skipped, but they are also the oldest records in the
  store — nothing had touched them since — so every slice filled up with them,
  skipped them all and never reached the listings of a search that was actually
  running. Orphaned listings are now re-checked too (their verdict is kept
  rather than recomputed), configured searches are re-checked first, and a skip
  no longer consumes a slot in the slice.
- The refresher's log line now says how many listings it skipped and why. It
  read `Re-checked 0 stored listing(s)` whether the queue was empty or every
  candidate had been silently discarded, which are opposite situations.
- The CSV formula-injection guard no longer quotes plain negative numbers, which
  turned a column a spreadsheet could add up into a column of text.

- The schedule is a property of the monitor, not of a product or of a platform:
  `search_interval`, `max_search_interval` and `start_at` can now be set in the
  `[monitor]` section, once, for everything it searches. Fixed times are no
  longer an alternative to the interval there — set both and it searches on its
  interval *and* at those times:

  ```toml
  [monitor]
  search_interval = '5m'
  max_search_interval = '15m'
  start_at = ['09:00', '18:30']
  ```

  The same three options on an `[item.*]` or `[marketplace.*]` section are
  deprecated. They are still read when `[monitor]` sets no schedule, so an
  existing configuration keeps its behaviour exactly (including `start_at`
  replacing the interval there), and every `start_at` value now gets a job —
  before, only the last one in the list was actually scheduled.
- Mercado Libre's `site` can be set per item, in
  `[item.<name>.mercadolibre]`, so two products can be searched in two
  countries. The `[marketplace.mercadolibre]` value becomes the default for
  items that do not name one.
- Mercado Libre as a second marketplace. A search now describes a *product*,
  not a platform: an item with no `marketplace` of its own runs on every
  configured marketplace, and each platform applies the filters it actually has.
  Options that belong to one platform go in `[item.<name>.<marketplace>]`, so a
  search is written once:

  ```toml
  [item.'playstation 5']
  search_phrases = "playstation 5"
  min_price = "300000"
  max_price = "600000"

  [item.'playstation 5'.facebook]
  search_city = "106647439372422"
  radius = 60

  [item.'playstation 5'.mercadolibre]
  condition = ["used"]
  free_shipping = true
  ```

  Listings from both arrive in the same shape (`Listing`, with `marketplace` as
  the source) and group together under the search item, so nothing downstream
  has to know where a listing came from. See `docs/mercadolibre.md`.
- The config loader builds one item configuration per (marketplace, item) pair
  instead of binding an item to the first marketplace, and each marketplace
  class now answers whether it can run a given item — Facebook still requires a
  `search_city`, Mercado Libre searches a whole site and does not.
- `--check <url>` accepts a Mercado Libre listing URL; each marketplace declares
  the URLs it can read.
- Pause switch in the web UI header (⏸ / ▶): holds back new searches while the
  monitor, the web UI and the log stream keep running. The switch is read
  between searches, so a search already under way finishes rather than being
  torn in half, and it is persisted to `~/.ai-marketplace-monitor/paused.json`
  so a paused monitor comes back paused. New `GET`/`POST /api/monitor/pause`.
- Delete listings from the dashboard: tick several rows and remove them at once,
  delete a whole product group, or delete one listing from its detail drawer.
  Deletion is permanent — the monitor records that the listing was discarded and
  declines to re-record it, so it does not walk back in on the next search.
  New `POST /api/listings/delete`.
- Analytics dashboard in the web UI (new "Dashboard" tab): summary tiles, price
  distribution and time-series charts, per-comuna and per-seller rollups,
  automatic bargain detection, product grouping and a per-listing change
  timeline. Every section is scoped by one global filter bar, and any table can
  be exported to CSV or JSON.
- Observation log: every listing the monitor examines is now recorded with
  first/last seen, a sighting count, and a timestamped diff of what changed
  between sightings (price, title, description, location, seller, condition,
  image), plus its AI rating and notification outcome. Stored under the new
  `listing-observations` cache tag, clearable with
  `--clear-cache listing-observations`.
- `GET /api/listings` — incremental feed of observations, cursored on a
  monotonic revision, that keeps the dashboard's local IndexedDB copy in sync.
- Light/dark theme toggle in the web UI header.
- The browser now runs on a persistent on-disk profile
  (`~/.ai-marketplace-monitor/browser-profile/`) instead of a throwaway one, so
  a second run is the same browser coming back rather than a fresh install. This
  is what stops a site re-challenging every login. A session saved by the
  previous cookie-only mechanism is imported once into a new profile, so
  upgrading does not force a re-login. Clear it with `--clear-cache sessions`
  (or `--clear-cache all`).
- `--login`: opens a browser, lets you sign in by hand with no time limit, saves
  the session and exits. For when an automated sign-in keeps looping on a
  CAPTCHA or two-factor challenge.

### Changed
- The web UI is now in Spanish, and price examples use CLP rather than USD.
- Dashboard wording follows what the fields actually mean: "tipo de producto" is
  now **producto**, the listing's own condition is **estado del producto**, and
  the activity flag is **estado de la publicación** with its window spelled out
  ("activa (vista hace menos de 3 días)" / "inactiva (sin aparecer hace 3+
  días)") instead of the unexplained "sin ver recientemente".
- **Listado** groups by product — the search item a listing was found under —
  rather than by a bag of title words, so a group is a comparable set of offers
  for something you actually configured. Listings the monitor never tied to an
  item get their own group instead of vanishing from the grouped view.
- Clicking a comuna now drills into it — its products, then one product's
  listings — instead of scoping the whole panel to it. Filtering everything down
  to a comuna is still available from the filter bar.

### Fixed
- The "Navegador" button was shown outside Docker, where the noVNC bridge is not
  mounted, so pressing it landed on `{"detail":"Not Found"}`. The header gives
  its links an explicit `display`, which outranked the `hidden` attribute the
  code was already setting; `[hidden]` is now enforced.
- Prices grouped with a space were mangled. Facebook renders CLP as
  `100 000`, and `extract_price` only understood comma grouping, so it either
  split one price into two (`100 | 000`) or — with a currency prefix — kept just
  the leading group and recorded **`$100 000` as `$100`**. That wrong number fed
  the AI evaluation, the price filters and the notifications, not only the
  dashboard. Space, non-breaking space, narrow non-breaking space, dot and comma
  are now all understood, and a trailing currency code (`150.000 CLP`) is kept.
- The dashboard repairs already-cached `100 | 000` strings on read, so listings
  scraped before the fix show the right price without being re-fetched, and a
  currency label containing digits is never rendered (the source of `| 000150`).
- Group counts (the "Publicaciones" column and every bar chart) counted only
  listings with a readable price, because `summarize`'s own `count` overwrote the
  group's. A category with 150 listings of which 12 had no price reported 138.
- Prices rendered with a space after the symbol (`$ 150.000`). A symbol now sits
  flush against the number (`$150.000`); an alphabetic code keeps its space
  (`CLP 150.000`). Grouping follows the viewer's locale, so a Chilean browser
  gets `150.000` rather than `150,000`.
- Price drops went unnoticed for space-grouped currencies. `_is_discounted`
  parsed the two prices itself and stripped plain spaces only, so a Chilean
  price (`450 000`, with a non-breaking space) never converted and both sides
  of the comparison fell back to the same "very expensive" constant: the
  listing was reported as not cheaper no matter how far the price fell, and a
  discount was never notified. Parsing now goes through the new
  `utils.price_value`, which understands every separator `extract_price` does,
  takes the current half of a `current | original` pair, reads *gratis* / *free*
  as zero, and treats an unreadable price as infinity rather than `999999999` —
  a figure an ordinary listing in CLP or COP can exceed.
- The monitor no longer searches while signed out. It now waits for the login to
  actually complete — polling for a live session rather than sleeping a fixed
  60 seconds — and skips the cycle with a clear error if it never does. Searching
  unauthenticated silently returned results for the marketplace's own default
  city instead of the configured one. The `login_wait_time` default is now 5
  minutes, which costs nothing on a normal sign-in because the wait ends as soon
  as the session goes live.
- An expired session mid-run is now detected and re-authenticated instead of
  degrading to signed-out searches.
- Chromium no longer announces itself as automated (`--enable-automation` /
  `navigator.webdriver`), which could bounce an ordinary interactive sign-in
  into an endless CAPTCHA loop — the challenge is answered correctly and the
  login page simply returns, because it is the browser being rejected, not the
  answer.
- A failed login now keeps the browser's device cookies (`datr`, `sb`) instead
  of discarding everything. Each retry used to arrive as a brand-new browser,
  which is what escalates a single challenge into a loop of them. Session and
  checkpoint cookies are still dropped, so a failed attempt is never replayed.
- Sign-in now starts on Facebook Marketplace rather than the legacy
  `/login/device-based/regular/login/` endpoint, which was observed looping. The
  current page also offers the "log in with your phone" QR flow. The old
  endpoint is still tried as a fallback when no login form appears.
- Under `--headless`, Chromium no longer advertises `HeadlessChrome` in its user
  agent (both the JS value and the HTTP header). A profile that signed in fine
  with a visible window would otherwise start failing once switched to headless.
- The web UI's "search now" (▶) button had no effect. It touches the config file
  to wake the monitor, but the monitor compared file *contents* and went straight
  back to sleep on finding them unchanged.
- `[Search] Failed to get search results` was logged after every search, whether
  or not it succeeded. It now reports the result count, and says so explicitly
  when an empty page is caused by a lost session.

## [0.10.2] - 2026-07-17

### Added
- Option `sort_by` to order Facebook search results by `suggested`, `new` (newest first), `price_ascend`, `price_descend`, or `distance_ascend` ([#323](https://github.com/BoPeng/ai-marketplace-monitor/issues/323))
- Web UI "Export CSV" button that downloads all found (notified) listings with link, price, rating, and details ([#334](https://github.com/BoPeng/ai-marketplace-monitor/issues/334))
- Docker image bundling Xvfb + Playwright Chromium + noVNC, with a "Browser" button in the web UI that exposes the live Chromium session for solving Facebook CAPTCHA / interactive logins ([#310](https://github.com/BoPeng/ai-marketplace-monitor/issues/310))
- GitHub Actions workflow publishing multi-arch (amd64/arm64) images to `ghcr.io/bopeng/ai-marketplace-monitor`

### Fixed
- WebUI startup failure on older FastAPI versions ([#315](https://github.com/BoPeng/ai-marketplace-monitor/pull/315))
- Stale runtime version reporting ([#314](https://github.com/BoPeng/ai-marketplace-monitor/pull/314))

### Documentation
- Note Python 3.10+ requirement in Quick Start ([#311](https://github.com/BoPeng/ai-marketplace-monitor/pull/311))
- Fix broken WEBUI.md link in README

## [0.10.1]

### Added
- Built-in web UI for config editing and live monitoring (FastAPI + CodeMirror)
- TOML syntax highlighting in config editor
- Live log streaming with filtering by level, item, AI score, and text
- Guided forms for adding/editing AI backends, items, users, and marketplaces
- `--webui-host` and `--webui-port` CLI options for remote access
- No password required on localhost; credentials required for remote access
- `FACEBOOK_USERNAME` / `FACEBOOK_PASSWORD` environment variable fallback for credentials
- Graceful handling of missing `${ENV_VAR}` references (warning instead of error)

## [0.10.0]

### Added
- Anthropic/Claude as an AI backend provider with support for Claude models (default: `claude-sonnet-4-20250514`)
- [issue 235](https://github.com/BoPeng/ai-marketplace-monitor/issues/235) Configurable rate limiting framework for all notification types
  - Rate limiting infrastructure moved from Telegram-specific to base notification class
  - Automatic rate limiting for Telegram with intelligent chat type detection (1.1s individual, 3.0s group)
  - Configurable instance-level and global rate limiting for all notification methods
  - Opt-in rate limiting for email, PushBullet, PushOver, and other notification types
  - Comprehensive test coverage for rate limiting behavior
- Support for `FACEBOOK_USERNAME` and `FACEBOOK_PASSWORD` environment variables as fallback credentials
- PyPI trusted publisher (OIDC) for release workflow

## [0.9.12]

- [Issue 289](https://github.com/BoPeng/ai-marketplace-monitor/issues/289). Fix 30s timeout delay in get_seller for anonymous mode.
- Change release workflow trigger from tag push to release creation.

## [0.9.11]

- [Issue 264](https://github.com/BoPeng/ai-marketplace-monitor/pull/264). Support different browsers.

## [0.9.10]

- [Issue 264](https://github.com/BoPeng/ai-marketplace-monitor/pull/264). Validate `search_city`.

## [0.9.9]

- [Issue 259](https://github.com/BoPeng/ai-marketplace-monitor/pull/259). Disallow keyboard monitoring by default.

## [0.9.8]

- [Issue 248](https://github.com/BoPeng/ai-marketplace-monitor/pull/248). Fix an issue with premature keyword filtering. Thanks to @adawalli

## [0.9.7]

- Add support for telegram [PR 231](https://github.com/BoPeng/ai-marketplace-monitor/pull/231). thanks to @adawalli

## [0.9.6]

- Fix searching across regions.
- Switch from `poetry` to `uv` for development.

## [0.9.5]

- [issue 155](https://github.com/BoPeng/ai-marketplace-monitor/issues/155) Fix output of pushbullet
- [issue 150](https://github.com/BoPeng/ai-marketplace-monitor/issues/150) Support option `category`

## [0.9.4] - 2025-04-15

- [issue 132](https://github.com/BoPeng/ai-marketplace-monitor/issues/132) Improve PushOver notification

## [0.9.3] - 2025-04-15

- [issue 102](https://github.com/BoPeng/ai-marketplace-monitor/issues/102) Fix pushover support and add more documentation

## [0.9.2] - 2025-04-07

- [issue 122](https://github.com/BoPeng/ai-marketplace-monitor/issues/122) Support searching across regions with different currencies

## [0.9.1] - 2025-03-13

- Re-release AI Marketplace Monitor under a AGPL license

## [0.8.8] - 2025-03-12

- Allow option date_listed to accept numeric value #96
- Fix importing pushover #91

## [0.8.6] - 2025-03-03

- Allow support for multiple languages.

## [0.8.5] - 2025-03-03

- Allow [pushover](https://pushover.net/) notification

## [0.8.2] - 2025-03-02

- Reorganize notification settings
- Support the use of environment variables for passwords
- Support browser proxy

**BREAKING CHANGES**

- Rename `smtp` sections to `notification`
- Rename parameter `smtp` to `notify_with`

## [0.7.11] - 2025-03-01

- Fix a bug on the handling of logical expressions for `keywords` and `antikeywords`.
- Add support for another auto layout page

## [0.8.9] - 2025-02-21

- Add options `prompt`, `extra_prompt` and `rating_prompt`

## [0.7.7] - 2025-02-17

- Expand the use of `enabled=False` to all sections
- Allow complex `AND` `OR` and `NOT` operations for `keywords` and `antikeywords`.

## [0.7.4] - 2025-02-10

- Rename `keywords` to `search_phrases`, `include_keywords` to `keywords` and `exclude_keywords` to `antikeywords` [#45]
- Separate statistics by item name [#46]

## [0.7.3] - 2025-02-07

- Allow email notification

## [0.7.0] - 2025-02-06

- Re-retrieve details of listings if there are title or price change
- Allow sending reminders for available items after specified time. (#41)
- Display counters

## [0.6.5] - 2025-02-05

- Allow checking URLs during monitoring (#34)
- Add option `ai` that allows the specification of AI models to use for certain marketplaces or items.
- Support locally hosted Ollama models
- Support DeepSeek-r1 model with `<think>` tags.
- Add option `timeout` to AI request.
- Expand command line option `--clear-cache`

## [0.6.2] - 2025-02-03

- Support extracting details from automobile listings.

## [0.6.1] - 2025-02-02

- Allow multiple `start_at`

## [0.6.0] - 2025-02-01

- Allow some parameters to different from initial and subsequent searches.
- Allow the AI to return a rating and some comments, and use the rating to determine if the user should be notified.

## [0.5.3] - 2025-01-31

- Add command line option `--diable-javascript` which can be helpful in some cases.
- Add option `include_keywords` to fine-tune the behavior of `keywords`.
- Add option `provider` to allow the specfication of more AI service providers.
- Allow `market_type` to marketplaces and allow multiple marketplaces.

## [0.5.1] - 2025-01-30

- Change the unit of `search-interval` to seconds to allow for more frequent search, although that is not recommended.
- Rename option `acceptable_locations` to `seller_locations`

## [0.5.0] - 2025-01-29

- Allow each time to add its own `search_interval`
- Add options such as `delivery_method`, `radius`, and `condition`
- Add options to define and use regions for searching large regions

## [0.4.5] - 2025-01-27

- Add option `--check` and `--for` to check particular listings

## [0.4.3] - 2025-01-26

- Add support for DeepSeek

## [0.4.0] - 2025-01-25

- Allow section `[ai.openai]`
- Use openAI to confirm if the item matches what user requests
- Slightly better logging

## [0.3.3] - 2025-01-21

- Allow option `enabled` for items
- Notify all users if no `notify` is specified for item or marketplace
- Compare string after normalization (#8)
- Stop sleeping if config files are changed. Allowing more interactive modification of search terms.
- Give more time after logging in, allow option `login_wait_time`.
- Allow entering username and password manually

## [0.2.0] - 2025-01-21

- Allow the definition of a reusable config file from `~/.ai-marketplace-monitor/config.toml`
- Allow options `exclude_sellers` and `exclude_by_description`
- Fix a bug that prevents the sending of phone notification

## [0.1.0] - 2025-01-20

### Added

- First release on PyPI.

[Unreleased]: https://github.com/BoPeng/ai-marketplace-monitor/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/BoPeng/ai-marketplace-monitor/compare/releases/tag/v0.1.0
