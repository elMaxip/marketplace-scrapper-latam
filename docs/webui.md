# Web UI

AI Marketplace Monitor includes a built-in web interface for editing your configuration and monitoring activity in real time. The web UI starts automatically when you run the monitor — no extra setup needed.

![Web UI Screenshot](webui_screenshot.png)

## Overview

The web UI provides:

- **TOML Config Editor** with syntax highlighting, powered by CodeMirror
- **Add / Edit / Delete** config sections (items, AI backends, users, marketplaces) through guided forms
- **Live Log Streaming** with filtering by level, item, AI score, and text search
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
| **Comunas** | Volume, price level and top categories per comuna, with click-to-filter |
| **Vendedores** | Per-seller volume, cadence and first/last posting, filterable to new, very active, or likely resellers |
| **Oportunidades** | Listings priced well below comparable ones, with a configurable bargain threshold |
| **Listado** | Every listing, or listings grouped into products with a best-price comparison; click any row for its full detail and change timeline |

One filter bar at the top scopes every section at once — product type, category,
city, comuna, seller, price range, date range, status, AI score and free text.
Any table exports to CSV or JSON.

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

- **Status.** The monitor does not revisit listings, so "sold" and "deleted"
  cannot be observed. A listing that stops appearing in searches is reported as
  *sin ver* (not seen recently) rather than guessed at.
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
| `--webui-log-retention` | `2000`      | Number of log messages kept in memory               |
