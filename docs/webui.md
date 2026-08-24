# Web UI

AI Marketplace Monitor includes a built-in web interface for editing your configuration and monitoring activity in real time. The web UI starts automatically when you run the monitor — no extra setup needed.

![Web UI Screenshot](webui_screenshot.png)

## Overview

The web UI provides:

- **TOML Config Editor** with syntax highlighting, powered by CodeMirror
- **Add / Edit / Delete** config sections (items, AI backends, users, marketplaces) through guided forms
- **Live Log Streaming** with filtering by level, item, AI score, and text search
- **Pause / resume** button (⏸) in the header — holds back new searches without stopping the monitor
- **Export CSV** button in the header downloads all found (notified) listings — link, price, rating, and details — as a CSV file
- **Auto-validation** of your config as you type
- **Analytics dashboard** (the "Dashboard" tab) — see below
- **Light / dark theme** toggle in the header

## Analytics Dashboard

The **Dashboard** tab analyses every listing the monitor has examined, not just
the ones you were notified about.

Each time a listing turns up in a search, the monitor records a *sighting*:
when it was first and last seen, how many times it reappeared, and a timestamped
diff of anything that changed since last time — price, title, description,
location, seller, condition or image. That log is what makes price history,
"listings from today", lowest-price-ever and bargain detection possible.

Sections:

| Section | What it shows |
|---|---|
| **Resumen** | Totals, mean/median/min/max price, seller and city counts, listings today and in the last hour |
| **Gráficos** | Price histogram, price and volume by product type / city / comuna, spread boxplots, and daily / hourly time series |
| **Comunas** | Volume, price level and top products per comuna; click a comuna to drill into its products, and a product to see its listings |
| **Vendedores** | Per-seller volume, cadence and first/last posting, filterable to new, very active, or likely resellers |
| **Oportunidades** | Listings priced well below comparable ones, with a configurable bargain threshold |
| **Listado** | Every listing, or listings grouped by product with a best-price comparison; click any row for its full detail and change timeline |

One filter bar at the top scopes every section at once — product, condition,
city, comuna, seller, price range, date range, listing status, AI score and free
text. Any table exports to CSV or JSON.

### Deleting listings

Anything the monitor recorded can be thrown away from the panel: tick rows in
**Listado → Todas las publicaciones** and delete them in one go, delete a whole
product group from the grouped view, or delete a single listing from its detail
drawer.

Deletion is permanent on purpose. The scraper has no memory of what you
discarded, so a listing that merely vanished from the panel would walk straight
back in on the next search; instead the monitor records that you removed it and
declines to re-record it. To bring everything back, clear the observation log
(below) and let the panel refill.

### Where the data lives

Sightings are stored in the monitor's own cache under the
`listing-observations` tag. The browser keeps a local copy in **IndexedDB** and
refreshes it incrementally, so the panel stays fast with tens of thousands of
listings and works instantly on reload. Nothing is sent anywhere: the data never
leaves your machine.

To start over, clear the observation log:

```bash
ai-marketplace-monitor --clear-cache listing-observations
```

The browser notices the store was reset and rebuilds its copy on the next visit.

### Signing in

The browser runs on a persistent profile at
`~/.ai-marketplace-monitor/browser-profile/`, so two-factor verification is
normally a one-time thing rather than something you face on every start. That
directory is equivalent to being signed in — treat it like a password.

Persistence matters for more than convenience. A browser Facebook does not
recognize gets challenged, and a throwaway profile is unrecognizable *every*
time — which is how a CAPTCHA turns into a loop you cannot answer your way out
of. A profile makes the second run the same browser coming back.

One consequence: a persistent profile binds its proxy for the whole browser
lifetime, so a rotating `proxy_server` list can no longer be sampled per page.
The monitor warns at startup and uses the first entry.

If an automated sign-in keeps looping — you answer the CAPTCHA correctly and
land back on the login page — sign in by hand once:

```bash
ai-marketplace-monitor --login
```

That opens a browser and waits, with no deadline, for you to finish whatever
Facebook asks for. Normal runs then reuse the saved session.

To start over with a clean login:

```bash
ai-marketplace-monitor --clear-cache sessions
```

### Known limits

- **Listing status.** The monitor does not revisit listings, so "sold" and
  "deleted" cannot be observed. A listing that has not appeared in a search for
  more than three days is reported as *inactiva* rather than guessed at.
- **Comuna and city** are parsed out of the single free-text location line the
  marketplace provides, so they can be imprecise or empty.
- **Mixed currencies.** Averages across different currencies are meaningless;
  the panel says so instead of printing a number, and you can filter down to one.

## Getting Started

Simply run the monitor:

```bash
ai-marketplace-monitor
```

The web UI is available at [http://127.0.0.1:8467](http://127.0.0.1:8467). A startup banner in the terminal shows the URL:

```
╭──────────── Web UI ────────────╮
│ 🌐  http://127.0.0.1:8467      │
│                                │
│ No password required           │
│ (local access only).           │
╰────────────────────────────────╯
```

On localhost, **no password is required**. Open the URL in your browser and start editing.

## Starting with no searches

A fresh install is created with **no searches configured**, and that is a
perfectly ordinary state rather than an error. The monitor reports itself as
`waiting_for_config` and does nothing at all: no browser is launched, no page is
opened and no cycle burns resources. Add a search from the web UI and it is
picked up within a second, without a restart.

The same is true in reverse: you may delete your last search and be left with
zero. Nothing enforces a minimum, and the configuration loader no longer
requires an `[item]` section.

## Knowing what the scraper is running

Saving a change and the scraper *using* it are two different events. The gap is
short — seconds, usually — but it is real, so the file on disk and the
configuration in force can genuinely differ for a moment.

`GET /api/scraper/state` is the monitor's own account of itself, and keeps the
three apart:

| | What it is |
| --- | --- |
| **Persisted** | The configuration files as they are on disk now. |
| **Loaded** | The configuration the scraping thread actually took up, resolved — every default applied and every inherited option folded in — with the version it was loaded under. |
| **Runtime** | The phase, the search under way, when each search last ran and runs next, and how the listing re-checks are getting on. |

The versions are SHA-256 hashes of the configuration files: one taken now, one
taken when the monitor read them. `config.status` is `current` when they match,
`stale` when the scraper has not reloaded yet, and `unknown` before the first
load. `GET /api/status` carries the same comparison under `config_sync`, so the
answer is available on the cheap poll every screen already makes.

Secrets are masked on the way out, including inside lists and nested tables.

## Changing the configuration while it runs

A configuration saved from the web UI (or edited in the file by hand) is taken
up without a restart, and without waiting for the current search to finish
unless waiting is the safe thing to do. The monitor looks at the file from the
checkpoints its scraping code already stops at — between listings, between
search phrases, before a navigation — and what it does depends on who the
change touches:

| The change… | What happens |
| --- | --- |
| **edited the search under way**, or changed its platform's settings | The new settings go into the search already running and it carries on. Lowering a maximum price is not an instruction to abandon a page that has already been loaded. See *What a running search can absorb* below — some settings genuinely cannot take effect until its next run, and those are named rather than glossed over. Set `apply_changes_while_running = false` under `[monitor]` to get the older behaviour, where the search is dropped and the next one starts. |
| **deleted the search under way** | It is stopped at the next checkpoint and the monitor goes on to the next search. `on_delete_running = "finish"` under `[monitor]` lets it run to the end and notify first, then disappear. Either way the scraper keeps going. |
| **switched off the search under way** | It stops. There is nothing worth finishing for a search that is not to run. |
| touched **something else** — another search, a token, the schedule | Adopted immediately, mid-search, without interrupting anything. A search created while another one is running appears in *Búsquedas que el scraper está usando* at once; when it actually runs is still up to its own schedule. |
| added or edited searches | Those are searched straight away. The ones you did not touch keep their places in the schedule rather than all being re-run. |
| left the file unparseable — a half-finished save, a syntax error | Nothing is adopted. The monitor carries on with the configuration it already has, and complains once rather than at every checkpoint. |

What it did about it is reported rather than left to be inferred:
`config.applied` on `GET /api/scraper/state`, and `config_sync.applied` on
`GET /api/status`, carry the change itself — which searches were added, removed,
re-enabled, switched off or edited, which platforms, whether the schedule moved
— along with the search abandoned for it, if there was one:

```json
{
  "seq": 3,
  "at": "2026-08-21T18:04:11+00:00",
  "version": "7851477efbe6…",
  "change": {"removed": [{"item": "bici", "marketplace": "facebook"}], "…": "…"},
  "interrupted": {"item": "bici", "marketplace": "facebook", "reason": "removed"}
}
```

Two equal hashes prove *a* change landed; this says which one, and what it cost.
A search dropped this way is recorded with the outcome `superseded`, which is
not a failure: it is the monitor doing what it was asked.

### What a running search can absorb

"Applied immediately" has a boundary, and pretending otherwise would put a tick
next to a setting that is not in force — which is the one thing this whole
comparison exists to prevent. The boundary is *when the search reads the
setting*, not how much we would like it to be live:

| | Takes effect |
| --- | --- |
| `keywords`, `antikeywords`, `exclude_sellers`, `seller_locations`, `rating`, `notify`, the AI prompts | On the very next listing. The search consults them once per listing. |
| `search_phrases`, `search_city`, `city_name`, `search_region`, `radius`, `min_price`, `max_price`, `condition`, `date_listed`, `delivery_method`, `availability`, `sort_by`, `currency`, `site`, `free_shipping`, `shipping_origin`, `max_pages`, `language` | On that search's **next run**. They were spent building the URL the search is already paging through, and a request that has been made cannot be un-made. |

Which of the two happened is reported, per change, under `applied.live`:

```json
"live": {
  "item": "ps5",
  "marketplace": "facebook",
  "applied": ["keywords"],
  "deferred": ["max_price"]
}
```

The Estado screen shows `deferred` as a standing notice naming the settings and
the search they are waiting on. Nothing is silently claimed.

## Controlling the searches without stopping the scraper

Three requests, all of them about the pass under way and none of them about the
monitor as a whole. Like every cross-thread control here they are flags the
scraping loop reads at its next checkpoint — Playwright's objects belong to the
thread that opened the page, so a request handler cannot close one itself — so
the answer is always "asked for", never "done". Watch `scraping` for it to land.

| Endpoint | What it does |
| --- | --- |
| `POST /api/scraper/search/stop` `{"item": "ps5", "marketplace": "facebook"}` | Ends that platform's search for that product. The same product on other platforms carries on. |
| `POST /api/scraper/search/stop` `{"item": "ps5"}` | Ends the product for this pass, **including the platforms it has not started yet**. A stop that only ended the page currently open would leave the rest of the product running exactly as before. |
| `POST /api/scraper/search/next` `{"item": "bici"}` | Puts a product at the head of the queue. The search under way is deliberately not touched: the promise is the *next* search. `{"item": null}` hands the order back to the schedule. |

A stop lasts for the pass it was made in and no longer — anything more permanent
is what switching the search off is for. A promotion is claimed by the pass that
honours it, so it cannot silently reorder every pass from then on. Both appear
in `scraping.stops` and `scraping.next_search` while they are pending, which is
what lets the interface show a row as stopping during the seconds before it is.

## Starting, pausing and stopping

Four controls, and the difference between them is what is left standing.

| | Interrupts the running search | Closes the browsers | Way back |
| --- | :---: | :---: | --- |
| **Pausar** | yes, at the next checkpoint | no | Reanudar |
| **Detener** | yes, at the next checkpoint | yes | Iniciar |
| **Reanudar** | — | — | — |
| **Iniciar** | — | — | — |

**Iniciar** activates the scraper and lets it run the searches that are *ready*.
It is not "search everything now". The monitor records when each `(search,
platform)` pair actually last ran, in the `search-runs` cache namespace, and
seeds the schedule from that — so a product searched four minutes before the
monitor was stopped is not due again the instant it comes back, and a product
whose interval has elapsed is. A search that has never run is due immediately:
an interval is a gap *between* runs and cannot precede the first one. Overriding
the schedule is a separate control, **Buscar ahora** (`POST /api/monitor/run`).

That memory also fixes a quieter problem: the schedule is rebuilt on every
configuration change, and a rebuilt job used to start its clock from scratch —
so editing one search silently postponed every other one.

**Pausar** cuts the running search off at the next checkpoint, within seconds,
and starts nothing new. It leaves every browser, tab and signed-in session
exactly where it was, so resuming costs one search rather than one sign-in. The
interrupted search was never marked as run, so it is due again on **Reanudar**.

**Detener** does the same interruption and then closes the browsers and releases
the resources. There is nothing to resume into; the way back is Iniciar.

Both are requests the scraping loop picks up at its next checkpoint, which is
usually seconds and can be longer mid-navigation. `scraping.cancelling` says one
is pending and `scraping.cancel_mode` (`"pause"` or `"stop"`) says which.

The switch is persisted to `~/.ai-marketplace-monitor/paused.json`, so a monitor
that was paused or stopped comes back that way after a restart rather than
quietly resuming. Deleting that file also resumes.

## Disabling the Web UI

If you don't need the web UI, disable it with:

```bash
ai-marketplace-monitor --no-webui
```

## Changing the Port

To use a different port:

```bash
ai-marketplace-monitor --webui-port 9090
```

## Advanced: Remote Access

By default, the web UI only listens on `127.0.0.1` (localhost) and requires no password. To access it from another machine on your network, you need to:

1. **Configure credentials** so the web UI is protected by a login screen.
2. **Bind to a network interface** so other machines can connect.
3. **Open a firewall port** if your system has a firewall enabled.

### Step 1: Set up username and password

The web UI uses your marketplace credentials for authentication. Set them in your config file:

```toml
[marketplace.facebook]
username = "you@example.com"
password = "your-password"
```

Or use environment variables:

```toml
[marketplace.facebook]
username = "${FACEBOOK_USERNAME}"
password = "${FACEBOOK_PASSWORD}"
```

Then set the environment variables in your shell before running the monitor:

```bash
export FACEBOOK_USERNAME="you@example.com"
export FACEBOOK_PASSWORD="your-password"
```

### Step 2: Bind to a network interface

Use `--webui-host` to listen on all interfaces:

```bash
ai-marketplace-monitor --webui-host 0.0.0.0
```

The startup banner will show all reachable URLs:

```
╭──────────────── Web UI ────────────────╮
│ 🌐  http://127.0.0.1:8467              │
│ 🌐  http://192.168.1.42:8467           │
│                                        │
│ user:      you@example.com             │
│ password:  (from marketplace config)   │
│                                        │
│ ⚠  Bound to non-loopback interface.    │
│    Consider TLS via a reverse proxy.   │
╰────────────────────────────────────────╯
```

You can also specify a port:

```bash
ai-marketplace-monitor --webui-host 0.0.0.0 --webui-port 9090
```

> **Note:** If no credentials are configured, `--webui-host` will refuse to start and display an error. This prevents accidentally exposing an unprotected editor on the network.

### Step 3: Open a firewall port

If your machine has a firewall, open the web UI port. For example, on Ubuntu with `ufw`:

```bash
sudo ufw allow 8467/tcp
```

On macOS, allow incoming connections through **System Settings > Network > Firewall**.

On Windows, add an inbound rule in **Windows Defender Firewall > Advanced Settings**.

> **Warning:** Exposing the web UI on a network means anyone who can reach the port can attempt to log in. Consider using a reverse proxy (nginx, Caddy, Tailscale) with TLS for encrypted connections, especially over untrusted networks.

## CLI Options Reference

| Option                  | Default     | Description                                         |
| ----------------------- | ----------- | --------------------------------------------------- |
| `--webui / --no-webui`  | `--webui`   | Enable or disable the web UI                        |
| `--webui-host`          | `127.0.0.1` | Bind address (requires credentials if not loopback) |
| `--webui-port`          | `8467`      | Port for the web UI                                 |
| `--webui-open`          | off         | Serve with no password on a non-loopback bind       |
| `--webui-log-retention` | `2000`      | Number of log messages kept in memory               |

## In a container

Inside Docker the server has to bind `0.0.0.0`: a server bound to loopback in a
container is reachable from that container and nothing else, not even from the
container next to it. But the bind address is also what decides whether a
password is required, so binding `0.0.0.0` made the server refuse to start
without credentials -- for a deployment that may be entirely private.

`--webui-open` (or `AIMM_WEBUI_OPEN=1`) says the port is kept private by
something else: a Compose network with the port unpublished, a firewall, or a
reverse proxy that authenticates. It is off by default, has to be asked for by
name, and logs a warning every time it is used.

Do not use it on an address the internet can reach. There, set up credentials as
in *Advanced: Remote Access* above and leave it off.

The `docker-compose.yml` that ships with the web UI does exactly this: the
monitor's own port is **not** published, and the only thing that talks to it is
the UI container, over the private network Compose creates. The browser reaches
the UI, and the UI forwards `/api` and `/ws` to the monitor -- which also keeps
every request same-origin, so there is no CORS to configure.
