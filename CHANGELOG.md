# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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

### Fixed
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
