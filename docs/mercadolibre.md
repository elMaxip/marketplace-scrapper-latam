# Mercado Libre

## Why the pages and not the API

Mercado Libre publishes an API, and it would be the better source — but since
April 2025 it refuses anonymous search:

```
GET https://api.mercadolibre.com/sites/MLC/search?q=playstation%205
{"message":"forbidden","error":"forbidden","status":403}
```

An application-only token is refused too (`you must use an access token linked
to a user`), so using it would mean registering an app, running the
authorization-code flow against a real Mercado Libre account and refreshing a
token every six hours — for data the site hands to any visitor. The scraper
therefore reads the public search pages through the browser the monitor already
drives.

## Requires a visible browser

Mercado Libre answers `403` to a headless Chromium even with the automation
flags off, and serves the real page to the same profile in headed mode. That is
the monitor's default; running with `--headless` effectively disables this
marketplace, and the log says so instead of reporting an empty market.

## Configuration

```toml
[marketplace.mercadolibre]
site = "MLC"            # MLC Chile, MLA Argentina, MLM México, MLU, MCO, MPE, MLB

[item.'playstation 5']
search_phrases = "playstation 5"
min_price = "300000"
max_price = "600000"

[item.'playstation 5'.mercadolibre]
site = "MLA"                # this product in Argentina, whatever the default is
condition = ["used"]        # new / used / refurbished / open_box
free_shipping = true
shipping_origin = "local"   # local / international
max_pages = 1               # 50 listings per page
```

`market_type` can be omitted: a section named after a supported marketplace is
that marketplace.

## Which filters exist

| Option | Sent to Mercado Libre as | Notes |
| --- | --- | --- |
| `min_price` / `max_price` | `_PriceRange_300000CLP-600000CLP` | Currency comes from `site`. |
| `condition` | `_ITEM*CONDITION_<id>` | One condition goes in the URL; several are matched against the results, because the URL takes one. |
| `free_shipping` | `_CostoEnvio_Gratis` | |
| `shipping_origin` | `_SHIPPING*ORIGIN_<id>` | |
| `max_pages` | `_Desde_51`, `_Desde_101`, … | |
| `site` | the host the search runs on | Per item; the `[marketplace.mercadolibre]` value is the default for items that do not name one. |
| `keywords`, `antikeywords`, `exclude_sellers` | — | Applied to the results, as on Facebook. |

## What does not apply

Mercado Libre's search has **no location facet**: results cover a whole site and
its nearest equivalents are about shipping. `search_city`, `city_name`,
`radius`, `search_region` and `seller_locations` are therefore ignored here —
the monitor logs which ones it skipped rather than approximating them. Listings
carry no seller location either, so that field stays empty instead of being
filled with a guess.

## When Mercado Libre asks for an account

After enough page loads from one session, Mercado Libre stops serving the page
asked for and serves a sign-in or "create your account" screen instead. It is
not an error: the request succeeds and the wall parses as a perfectly valid
page, which is why it has to be recognised deliberately rather than left to look
like a listing with no title.

The monitor recognises it three ways — a redirect to a sign-in host, one of the
site's own "ingresa a tu cuenta" panels, or a password field on a page that
should not have one — and then **stops asking**. The marketplace goes on a
cooldown shared by every flow (the search and the listing refresher both read
it, so the second browser tab cannot keep knocking), growing with each
consecutive refusal: 15 minutes, 30, an hour, two, four. A page that comes back
normally clears it.

Nothing is ever deleted because of a wall. A refusal says something about us,
not about the listing, so a re-check that hits one is reported as undecided.

### Reducing how often it happens

- **Do not run headless.** Mercado Libre serves this same wall to a headless
  Chromium from the very first page, so `--headless` disables this marketplace
  in practice and no amount of signing in will help.
- The listing refresher deals its work out one marketplace at a time, so a slice
  never fires all of its page loads at the same site.
- `listing_recheck_interval` in `[monitor]` is the main volume knob: raise it and
  each listing's page is opened less often.

### Signing in

```
ai-marketplace-monitor --login
```

opens a visible browser, waits with no deadline while you sign in by hand, and
keeps the session in the browser profile for later runs. Nothing is typed in for
you: an automated sign-in is the thing most likely to get an account challenged.

The wait is passive — it watches which page you are on rather than reloading to
ask — and it confirms by loading the account page, which only a signed-in
visitor is allowed to see. That check is what makes "signed in" a fact rather
than a guess: an anonymous visitor is sent to the sign-in gateway or bounced
back to the front page, and both change the host.

### Importing a session instead

Mercado Libre does not reliably let a sign-in *complete* inside an automated
browser. The form is accepted and you are quietly returned to the front page
with no session, and no amount of retrying changes that. When that happens,
hand the monitor a session you already have:

**Ajustes → Plataformas → Sesiones del navegador → Importar sesión**, or:

```
POST /api/marketplace/mercadolibre/session   {"cookies": "<pasted>"}
```

Three formats are accepted, because that is what the tools people have actually
produce:

- the `Cookie:` request header, copied from devtools (Network → the first
  request → Request Headers);
- the JSON a cookie-manager extension exports — this one also carries the expiry
  dates, so the interface can tell you when the session runs out;
- a Playwright `storageState` file.

The cookies are stored in `~/.ai-marketplace-monitor/sessions/`, owner-only, and
loaded into the running browser by the monitor between jobs — the web UI runs on
another thread and may not touch Playwright itself. If the monitor is not
running, the file records that the session is waiting and the next browser
launch takes it up; it is applied exactly once, so a session that is live in the
profile is never overwritten by an older copy of itself. Importing also lifts the
marketplace's cooldown: a new session is exactly the reason to try again.

Once loaded, the monitor checks the site actually accepted it and says so in the
log. A set of cookies that does not sign in looks exactly like one that does
until searches quietly start coming back empty.

**Volver a aplicar** loads a stored session into the browser again without
re-pasting it — for when the site logged the profile out but the cookies are
still good.

Cookies for any other site in the same paste are dropped rather than stored, and
nothing can read a stored cookie back out: the interface reports how many there
are, which domains they cover and when they expire, and never a value.

### There is no setting that stops it searching

There used to be a `require_login` option that refused to read Mercado Libre
until a session had been saved. It is gone. Anonymous browsing works until it
doesn't, and a switch that turned the platform off on the user's behalf produced
the worst possible outcome: a monitor that looked perfectly healthy and quietly
searched nowhere.

Mercado Libre is now always searched, session or no session. Without one, the
monitor says so once per pass, and a wall is handled the way any refusal is — a
cooldown that grows with each consecutive one. A configuration that still
carries `require_login` keeps loading; the key is ignored and a warning names it.

Signing in raises the threshold a great deal. It does not remove it: the limit
is on how much one session reads, and an account that reads like a scraper can
still be asked to verify itself.
