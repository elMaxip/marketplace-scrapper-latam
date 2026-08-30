# Configuration Reference

**Table of content:**

- [AI Services](#ai-services)
- [Marketplaces](#marketplaces)
- [Users](#users)
- [Notification](#notification)
- [Email notification](#email-notification)
- [Items to search](#items-to-search)
- [Common item and marketplace options](#common-item-and-marketplace-options)
- [Regions](#regions)
- [Translators](#translators)
- [Monitor Configuration](#monitor-configuration)
- [Additional options](#additional-options)

The AI Marketplace Monitor uses [TOML](https://toml.io/en/) configuration files to control its behavior. The system will always check for a configuration file at `~/.ai-marketplace-monitor/config.toml`. You can specify additional configuration files using the `--config` option.

To avoid including sensitive information directly in the configuration file, all options that accept a string or a list of string can be specified using the `${ENV_VAR}` format. For example

```toml
[marketplace.facebook]
password = '${FACEBOOK_PASSWORD}'

[user.me]
email = ['${EMAIL_1}', '${EMAIL_2}']
pushbullet_token = '${PUSBULLET_TOKEN}'
```

_AI Marketplace Monitor_ will retrieve the value from the corresponding environment variable and raise an error if the environment variable does not exist.

Here is a complete list of options that are acceptable by the program. [`example_config.toml`](example_config.toml) provides an example with many of the options.

### AI Services

One of more sections to list the AI agent that can be used to judge if listings match your selection criteria. The options should have header such as `[ai.openai]`, `[ai.deepseek]`, or `[ai.anthropic]`, and have the following keys:

| Option        | Requirement | DataType | Description                                                |
| ------------- | ----------- | -------- | ---------------------------------------------------------- |
| `provider`    | Optional    | String   | Name of the AI service provider.                           |
| `api_key`     | Optional    | String   | A program token to access the RESTful API.                 |
| `base_url`    | Optional    | String   | URL for the RESTful API                                    |
| `model`       | Optional    | String   | Language model to be used.                                 |
| `max_retries` | Optional    | Integer  | Max retry attempts if connection fails. Default to 10.     |
| `timeout`     | Optional    | Integer  | Timeout (in seconds) waiting for response from AI service. |

Note that:

1. `provider` can be [OpenAI](https://openai.com/),
   [DeepSeek](https://www.deepseek.com/), [Gemini](https://ai.google.dev/), [Anthropic](https://www.anthropic.com/), or [Ollama](https://ollama.com/). The name of the ai service will be used if this option is not specified so `OpenAI` will be used for section `ai.openai`.
2. [OpenAI](https://openai.com/), [DeepSeek](https://www.deepseek.com/), and [Gemini](https://ai.google.dev/) models sets default `base_url` and `model` for these providers.
3. [Anthropic](https://www.anthropic.com/) uses the Anthropic SDK directly (not OpenAI-compatible). The default model is `claude-sonnet-4-20250514`. An `api_key` is required.
4. [Gemini](https://ai.google.dev/) is accessed through Google's OpenAI-compatible endpoint. The default model is `gemini-2.5-flash`. An `api_key` is required and can be obtained from [Google AI Studio](https://aistudio.google.com/apikey).
5. Ollama models require `base_url`. A default model is set to `deepseek-r1:14b`, which seems to be good enough for this application. You can of course try [other models](https://ollama.com/library) by setting the `model` option.
6. Although only five providers are directly supported, you can use any other service provider with `OpenAI`-compatible API using customized `base_url`, `model`, and `api_key`.
7. You can use option `ai` to list the AI services for particular marketplaces or items.

A typical section for OpenAI looks like

```toml
[ai.openai]
api_key = 'sk-proj-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
```

A typical section for Anthropic looks like

```toml
[ai.anthropic]
api_key = 'sk-ant-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
```

A typical section for Gemini looks like

```toml
[ai.gemini]
api_key = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
```

### Marketplaces

Every marketplace the monitor supports — Facebook Marketplace and Mercado Libre —
exists whether or not the configuration mentions it, and a search runs on all of
them unless it says otherwise. There is nothing to add to make a platform
available.

A `marketplace.name` section is therefore optional, and only says how to *sign
in* to one. A section named after something else (`[marketplace.houston]` with
`market_type = "facebook"`) still creates an extra marketplace of its own, which
is how one monitor can search two Facebook accounts or two cities as separate
platforms.

| Option             | Requirement | DataType | Description                                                                                                      |
| ------------------ | ----------- | -------- | ---------------------------------------------------------------------------------------------------------------- |
| `market_type`      | Optional    | String   | The supported marketplace. Currently, only `facebook` is supported.                                              |
| `username`         | Optional    | String   | Username can be entered manually or kept in the config file. Falls back to `FACEBOOK_USERNAME` environment variable if not set. |
| `password`         | Optional    | String   | Password can be entered manually or kept in the config file. Falls back to `FACEBOOK_PASSWORD` environment variable if not set. |
| `login_wait_time`  | Optional    | Integer  | How long (in seconds) to wait for the login to complete — two-factor, CAPTCHA or QR. The wait ends as soon as the session is live, so this is only a ceiling. Defaults to 300. |
| `language`         | Optional    | String   | Language the platform's pages are read in. A fallback only: each search sets its own `language`, and that wins.  |
| **Common options** |             |          | Options listed in the [Common options](#common-options) section below that provide default values for all items. |

1. Multiple marketplaces with different `name`s can be specified for different `item`s (see [Multiple marketplaces](../README.md#multiple-marketplaces)). However, because the default `marketplace` for all items are `facebook`, it is easiest to define a default marketplace called `marketplace.facebook`.
2. `username` and `password` can be provided in three ways (in order of priority): directly in the config file, via the `${ENV_VAR}` syntax (e.g. `password = '${MY_FB_PASS}'`), or automatically from the `FACEBOOK_USERNAME` and `FACEBOOK_PASSWORD` environment variables. If none are set, the monitor runs in anonymous mode.
3. If `language="LAN"` is specified, it must match to one of `translation` sections, defined by yourself or in the system configuration file. The system will try exact match (e.g. `es` to `es` or `zh_CN` to `zh_CN`), then partial match (e.g. `es` to `es_CO` or `es_CO` to `es`).
4. Please see [Support for non-English languages](../README.md#support-for-non-english-languages) on how to set this option and define your own translations.

### Users

One or more `user.username` sections can be defined in the configuration. The `username` one of the usernames listed in the `notify` option of `marketplace` or `item`. Each `user` section accepts the following options

| Option        | Requirement | DataType    | Description                                                                                                                                                  |
| ------------- | ----------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `notify_with` | Optional    | String/List | Specifies one or more notification methods to be used for this user. If left unspecified, all available notification methods will be used.                   |
| `remind`      | Optional    | String      | Enables repeated notifications for the user after a specified duration (e.g., 3 days) if a listing remains active. By default, users are notified only once. |

Note that

1. **Default Notification Behavior**: If the `notify_with` option is not specified, the system will use all available notification methods for the user.
2. **Inline Notification Settings**: Notification settings can be defined directly under the user section. Any settings described in the [Notification](#notification) section can be applied to a user's configuration.
3. **Repeated Notifications**: The `remind` option allows users to receive repeated notifications after a specified time interval. If not set, users will only be notified once about a listing.

### Notification

_AI Marketplace Monitor_ supports various notification methods, allowing you to configure notifications in a flexible way. You can define notification settings directly within the `user` sections or create dedicated `notification.NAME` sections and reference them using the `notify_with` option. This provides flexibility for single-user setups or shared configurations across multiple users.

#### Direct Notification Settings in User Sections

Define notification details directly within the user section. This approach is ideal for single-user configurations.

```toml
[user.me]
pushbullet_token = "xxxxxxxxxxxxxxxx"
email = 'myemail@gmail.com'
smtp_password = 'abcdefghijklmnop'
```

#### Shared Notification Settings in Dedicated Sections

Define notification methods in their own `notification.NAME` sections and reference them using the notify_with option. This approach is better for sharing settings across multiple users.

```toml
[user.me]
email = 'myemail@gmail.com'
notify_with = ['gmail', 'pushbullet']

[user.other]
email = 'other.email@gmail.com'
notify_with = ['gmail']

[notification.gmail]
smtp_password = 'abcdefghijklmnop'

[notification.pushbullet]
pushbullet_token = "xxxxxxxxxxxxxxxx"
```

Note that:

1. Under the hood, _AI Marketplace Monitor_ merges all notification options into the user section. This allows you to share partial settings across users (e.g. `smtp_password`) while customizing specific details (e.g. `email`).
2. If `notify_with` is not specified, the system will automatically include all notification settings for the user, so the `notify_with` option for `user.me` could be ignored.
3. AI Marketplace Monitor does not support multiple notifications of the same type for a single user. For example, the following configuration is not supported:

```toml
[user.me]
notify_with = ['pushbullet1', 'pushbullet2']
```

If you need to send notifications through multiple instances of the same type (e.g., multiple Pushbullet tokens), you must create separate users for each instance. For example:

```toml
[user.me]
notify_with = 'pushbullet1'

[user.other]
notify_with = 'pushbullet2'

[notification.pushbullet1]
pushbullet_token = "xxxxxxxxxxxxxxxx"

[notification.pushbullet2]
pushbullet_token = "yyyyyyyyyyyyyyyy"
```

#### Common Notification settings

| Option                  | Requirement | DataType        | Description                                                                                                                      |
| ----------------------- | ----------- | --------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `max_retries`           | Optional    | Integer         | Number of attempts to retry a notification. Defaults to `5`.                                                                     |
| `retry_delay`           | Optional    | Integer         | Time in seconds to wait between retry attempts. Defaults to `60`.                                                                |
| `with_description`      | Optional    | Boolean/Integer | Whether or not include description of listings. If a number is given, the description will be truncated to the specified length. |
| `rate_limit_enabled`    | Optional    | Boolean         | Enable rate limiting for this notification method. Defaults to `false` (except Telegram which defaults to `true`).              |
| `instance_rate_limit`   | Optional    | Integer         | Minimum seconds between messages for this specific configuration instance. Defaults to `1`.                                      |
| `global_rate_limit`     | Optional    | Integer         | Maximum messages per second across all notification instances (sliding window). Defaults to `10` (`30` for Telegram).            |

Note that

1. These settings are shared across all notification methods. For example, if you are notifying with `notify_with=['gmail', 'pushbullet']`, the same `max_retries` and `retry_delay` will apply to both methods.
2. Support for `with_description` vary across notification methods due to their own limitations and strength. For example, email notification will always include description.
3. Rate limiting prevents API violations by controlling message frequency. When enabled, the system waits for the longer of `instance_rate_limit` or `global_rate_limit` before sending each message. Telegram automatically enables rate limiting with optimized defaults for individual (1.1s) and group chats (3.0s).

#### Telegram notification

| Option             | Requirement | DataType | Description                                    |
| ------------------ | ----------- | -------- | ---------------------------------------------- |
| `telegram_token`   | Required    | String   | Bot token obtained from @BotFather.           |
| `telegram_chat_id` | Required    | String   | Chat ID for receiving notifications.           |

Note that

1. **Automatic Rate Limiting**: Telegram notifications automatically enable rate limiting (`rate_limit_enabled = true`) with intelligent defaults based on chat type.
2. **Smart Chat Detection**: The system automatically detects individual chats (positive chat IDs) vs group chats (negative chat IDs) and applies appropriate rate limits.
3. **Optimized Limits**: Individual chats use 1.1 seconds between messages, group chats use 3.0 seconds, with a global limit of 30 seconds across all Telegram instances.
4. **HTTP 429 Handling**: Built-in retry logic with exponential backoff for Telegram API rate limit responses.
5. **Message Splitting**: Long messages are automatically split while preserving MarkdownV2 formatting.

#### Pushbullet notification

| Option                    | Requirement | DataType | Description                   |
| ------------------------- | ----------- | -------- | ----------------------------- |
| `pushbullet_token`        | Optional    | String   | Token for user.               |
| `pushbullet_proxy_type`   | Optional    | String   | HTTP proxy type, e.g. `https` |
| `pushbullet_proxy_server` | Optional    | String   | HTTP proxy server URL         |

Please refer to [PushBullet documentation](https://github.com/richard-better/pushbullet.py/blob/master/readme-old.md) for details on the use of a proxy server for pushbullet.

#### Pushover notification

| Option               | Requirement | DataType | Description         |
| -------------------- | ----------- | -------- | ------------------- |
| `pushover_user_key`  | Optional    | String   | Pushover user key.  |
| `pushover_api_token` | Optional    | String   | Pushover API Token. |

#### Pushover notification

| Option           | Requirement | DataType | Description                                       |
| ---------------- | ----------- | -------- | ------------------------------------------------- |
| `ntfy_server`    | Optional    | String   | ntfy server, default to `https://ntfy.sh`         |
| `ntfy_topic`     | Optional    | String   | A unique topic to receive your notification.      |
| `message_format` | Optional    | String   | Format notification as `plain_text` or `markdown` |

- According to [ntfy documentation](https://docs.ntfy.sh/publish/#markdown-formatting), markdown format is supported only by web app. Therefore, `message_format` is by default set to `plain_text`.

### Email notification

| Option          | Requirement | DataType    | Description                                             |
| --------------- | ----------- | ----------- | ------------------------------------------------------- |
| `email`         | Optional    | String/List | One or more email addresses for email notifications     |
| `smtp_username` | Optional    | String      | SMTP username.                                          |
| `smtp_password` | Required    | String      | A password or passcode for the SMTP server.             |
| `smtp_server`   | Optional    | String      | SMTP server, usually guessed from sender email address. |
| `smtp_port`     | Optional    | Integer     | SMTP port, default to `587`                             |

Note that

1. We provide default `smtp_server` and `smtp_port` values for popular SMTP service providers.
2. `smtp_username` is assumed to be the first `email`.

See [Setting up email notification](../README.md#setting-up-email-notification) for details on how to set up email notification.

### Items to search

One or more `item.item_name` where `item_name` is the name of the item.

| Option             | Requirement | DataType    | Description                                                                                                                                                                                    |
| ------------------ | ----------- | ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `search_phrases`   | Required    | String/List | One or more strings for searching the item.                                                                                                                                                    |
| `description`      | Optional    | String      | A longer description of the item that better describes your requirements (e.g., manufacture, condition, location, seller reputation, shipping options). Only used if AI assistance is enabled. |
| `keywords`         | Optional    | String/List | Excludes listings whose titles and description do not contain any of the keywords.                                                                                                             |
| `antikeywords`     | Optional    | String/List | Excludes listings whose titles or descriptions contain any of the specified keywords.                                                                                                          |
| `keywords_title`   | Optional    | String/List | The same rule, narrowed to the **title**.                                                                                                                                                       |
| `keywords_description` | Optional | String/List | The same rule, narrowed to the **description**.                                                                                                                                                 |
| `antikeywords_title` | Optional  | String/List | Excludes listings whose **title** contains any of these.                                                                                                                                        |
| `antikeywords_description` | Optional | String/List | Excludes listings whose **description** contains any of these.                                                                                                                                  |
| `marketplace`      | Optional    | String      | Restricts the item to one marketplace. Unset, it is searched on every one of them. Prefer `enabled = false` in the item's per-marketplace block, which is what the web UI writes.               |
| `language`         | Optional    | String      | Facebook only, and per search: the interface language its pages are served in, so the parser can find the labels. Set it in `[item.<name>.facebook]`. Defaults to `es_LA`.                      |
| **Common options** |             |             | Options listed below. These options, if specified in the item section, will override options in the marketplace section.                                                                       |

Marketplaces may return listings that are completely unrelated to search search_phrases, but can also
return related items under different names. To select the right items, you can

1. Use `keywords` to keep only items with certain words in the title. For example, you can set `keywords = ['gopro', 'go pro']` when you search for `search_phrases = 'gopro'`.
2. Use `antikeywords` to narrow down the search. For example, setting `antikeywords=['HERO 4']` will exclude items with `HERO 4` or `hero 4`in the title or description.
3. The `keywords` and `antikeywords` options allows the specification of multiple keywords with a `OR` relationship, but it also allows complex `AND`, `OR` and `NOT` logics. See [Advanced Keyword-based filters](../README.md#advanced-keyword-based-filters) for details.
4. The four scoped variants (`*_title`, `*_description`) exist because the two
   original keys read the title and the description glued together, which is the
   right default and a poor only option. "I do not want cases" is a rule about
   the *title* — every listing of a console mentions a case somewhere in its
   description, so `antikeywords = ['case']` throws away the whole market — and
   "it has to say sealed" is a rule about the *description*, because a title has
   room for four words. `keywords` and `antikeywords` keep their exact meaning,
   so a configuration written before these existed behaves identically.
5. **Where a rule looks decides when it can be answered, and that reaches the
   scrapers.** A shop's results grid carries titles and no descriptions, and
   opening a product page per catalogue entry is the bulk of a search's traffic
   (and, on Lider, the exact requests its bot check refuses). So a search whose
   only word rules are `*_title` ones reads a whole catalogue from the grid
   without opening a single product page, while any rule that can depend on the
   description makes the monitor open them. A rule that cannot be answered yet
   is *undecided*, never "failed" and never "met": a card that does not yet show
   a required word has not broken the rule, and a banned word found in the title
   is settled whatever else is missing.
6. It is usually more effective to write a longer `description` and let the AI know what exactly you want. This will make sure that you will not get a drone when you are looking for a `DJI` camera. It is still a good idea to pre-filter listings using non-AI criteria to reduce the cost of AI services.

### Common item and marketplace options

The following options that can specified for both `marketplace` sections and `item` sections. Values in the `item` section will override value in corresponding marketplace if specified in both places.

| `Parameter`           | Required/Optional | Datatype            | Description                                                                                                                                                 |
| --------------------- | ----------------- | ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `availability`        | Optional          | String/List         | Shows output with `in` (in stock), `out` (out of stock), or `all` (both).                                                                                   |
| `condition`           | Optional          | String/List         | One or more of `new`, `used_like_new`, `used_good`, and `used_fair`.                                                                                        |
| `date_listed`         | Optional          | String/Integer/List | One of `all`, `last 24 hours`, `last 7 days`, `last 30 days`, or `0`, `1`, `7`, and `30`.                                                                   |
| `delivery_method`     | Optional          | String/List         | One of `all`, `local_pick_up`, and `shipping`.                                                                                                              |
| `exclude_sellers`     | Optional          | String/List         | Exclude certain sellers by their names (not username).                                                                                                      |
| `max_price`           | Optional          | Integer/String      | Maximum price, can be followed by a currency name.                                                                                                          |
| `max_search_interval` | Deprecated        | String              | Moved to the [`monitor` section](#monitor-configuration). Still read when `monitor` sets no schedule.                                                        |
| `min_price`           | Optional          | Integer/String      | Minimum price, can be followed by a currency name.                                                                                                          |
| `category`            | Optional          | String              | Category of search.                                                                                                                                         |
| `notify`              | Optional          | String/List         | Users who should be notified.                                                                                                                               |
| `ai`                  | Optional          | String/List         | AI services to use, default to all specified services. `ai=[]` will disable ai.                                                                             |
| `city_name`           | Optional          | String/List         | Corresponding name of `search_city`.                                                                                                                        |
| `radius`              | Optional          | Integer/List        | Radius of search, can be a list if multiple `search_city` are specified.                                                                                    |
| `currency`            | Optional          | Integer/List        | Currency used for the search city, can be a list if multiple `search_city` are specified.                                                                   |
| `prompt`              | Optional          | String              | Prompt to AI service that will replace the default prompt                                                                                                   |
| `extra_prompt`        | Optional          | String              | Additional prompt that will be inserted between regular and rating prompt                                                                                   |
| `ranking_prompt`      | Optional          | String              | Ranking prompt that instruct how AI rates the listings                                                                                                      |
| `rating`              | Optional          | Integer/List        | Notify users with listings with rating at or higher than specified rating.                                                                                  |
| `search_city`         | Required          | String/List         | One or more search cities, obtained from the URL of your search query. Required for marketplace or item if `search_region` is unspecified.                  |
| `search_interval`     | Deprecated        | String              | Moved to the [`monitor` section](#monitor-configuration). Still read when `monitor` sets no schedule.                                                        |
| `search_region`       | Optional          | String/List         | Search over multiple locations to cover an entire region. `regions` should be one or more pre-defined regions or regions defined in the configuration file. |
| `seller_locations`    | Optional          | String/List         | Only allow searched items from these locations.                                                                                                             |
| `sort_by`             | Optional          | String              | Order of search results. One of `suggested`, `new`, `price_ascend`, `price_descend`, and `distance_ascend`.                                                 |
| `start_at`            | Deprecated        | String/List         | Moved to the [`monitor` section](#monitor-configuration). Still read when `monitor` sets no schedule, where it overrides `search_interval`.                  |
| `excluded_price_pattern_sets` | Optional | String/List | Names of `[price_patterns.*]` sections whose patterns this search uses. Resolved into `excluded_price_patterns` before anything runs, so the two add up and duplicates collapse. |
| `target_price`        | Optional          | Integer/String      | What you hope to pay. Never sent to the marketplace and never used as a filter; the web dashboard measures the cheapest listing found against it. Belongs in `[item.<name>.<marketplace>]`, since the same product is worth a different price on each platform. |

Note that

1. `search_city` can be found from the URL that facebook uses to search your region. For example, if the URL for your facebook search is `https://www.facebook.com/marketplace/sanfrancisco/search?query=go%20pro%2011%20deal%20site`, the `search_city` is `sanfrancisco`. This name is not necessarily the name of your city, especially for non-US cities, and you can search multiple cities or an entire region. See [Searching multiple cities and regions](../README.md#searching-multiple-cities-and-regions) for details.
2. If `notify` is not specified for both `item` and `marketplace`, all listed users will be notified.
3. `prompt`, `extra_prompt`, `rating_prompt`, and `rating` are used to adjust how to interact with an AI service. See [Adjust prompt and notification level](../README.md#adjust-prompt-and-notification-level) for details.
4. `start_at` supports one or more of the following values: <br> - `HH:MM:SS` or `HH:MM` for every day at `HH:MM:SS` or `HH:MM:00` <br> - `*:MM:SS` or `*:MM` for every hour at `MM:SS` or `MM:00` <br> - `*:*:SS` for every minute at `SS`.
5. A list of two values can be specified for options `rating`, `availability`, `delivery_method`, and `date_listed`. See [First and subsequent searches](../README.md#first-and-subsequent-searches) for details.
6. `min_price` and `max_price` can be specified as a number (e.g. `min_price=100`) or a number followed by a currency name (e.g. `min_price='100 USD'`). If different currencies are specified for both `min_price/max_price` and `search_city` (or `region`), the `min_price` and `max_price` will be adjusted to use currency for the `search_city`. See [Searching across regions with different currencies](../README.md#searching-across-regions-with-different-currencies) for details. **The conversion needs an exchange rate, and there is not always one.** Rates come from the ECB's daily table via `CurrencyConverter`, which publishes none for CLP, ARS, COP, PEN or UYU — most of the region this monitor is usually pointed at. When there is no rate the bound is sent as the plain number and a warning is logged; it used to raise out of the middle of building a search URL. Naming a currency on a city is still worth doing: it says what that city prices in, and a bound written in the same currency needs no conversion at all. Leaving it unset is the safe default and sends the number exactly as written.
7. `category` can be `vehicles`, `propertyrentals`, `apparel`, `electronics`, `entertainment`, `family`, `freestuff`, `free`, `garden`, `hobbies`, `homegoods`, `homeimprovement`, `homesales`, `musicalinstruments`, `officesupplies`, `petsupplies`, `sportinggoods`, `tickets`, `toys`, and `videogames`. If `catgory=freestuff` or `catgory=free` is set, `min_price` and `max_price` is ignored.
8. `sort_by` controls the order of the search results. `suggested` (the default) uses Facebook's own ranking, `new` lists the newest items first (useful for catching newly listed items), `price_ascend` and `price_descend` sort by price, and `distance_ascend` sorts by distance from the search city.

### Regions

One or more sections of `[region.region_name]`, which defines regions to search. Multiple searches will be performed for multiple cities to cover entire regions.

No regions are shipped with the package any more. Earlier versions merged a dozen of them in from `ai_marketplace_monitor/config.toml` (`usa`, `can`, `mex`, `bra`, `arg`, `aus`, `nzl`, `ind`, `gbr`, `fra`, `spa`, ...), which was only ever useful to somebody living in one of the countries they happened to cover — there was no Chilean one, for instance — while every one of them appeared in the web UI's region picker for everybody. A region is now only what you define here (or save from **Ajustes -> Regiones guardadas** in the web UI, which writes exactly this section). A search naming a region that does not exist is refused by the loader with that region's name in the message, rather than failing obscurely.

| Parameter     | Required/Optional | Data Type    | Description                                                                 |
| ------------- | ----------------- | ------------ | --------------------------------------------------------------------------- |
| `search_city` | Required          | String/List  | One or more cities with names used by Facebook.                             |
| `full_name`   | Optional          | String       | A display name for the region.                                              |
| `radius`      | Optional          | Integer/List | Recommended `805` for regions using kms, and `500` for regions using miles. |
| `currency`    | Optional          | Integer/List | Currency used for the region.                                               |
| `city_name`   | Optional          | String/List  | Corresponding names for `search_city`.                                      |

Note that

1. `radius` has a default value of `500` (miles). You can specify different `radius` for different `search_city`.
2. Options `full_name` and `city_name` are for documentation and logging purposes only.

### Saved price patterns

One or more sections of `[price_patterns.<name>]`: a list of excluded price
patterns written once and referred to by name from as many searches as you like.

A price pattern says "this number is not an asking price" — the run of nines
somebody typed to get past a required field, the keyboard walk, the `0` that
means the listing is really an advert. They are applied *before* `min_price`,
`max_price` and `target_price`, because a junk price is not a cheap listing or
an expensive one: it is a listing whose price is unknown. The syntax is the same
one `excluded_price_patterns` takes and is documented with that option.

The reason for naming them is that the noise belongs to the market rather than
to one search, so the same three or four rules are wanted everywhere — and
retyped rules drift: one search excludes `9*` and its neighbour excludes
`99999`, and the group with the placeholder still in it is the one whose average
nobody can trust.

| Parameter     | Required/Optional | Data Type   | Description                                                     |
| ------------- | ----------------- | ----------- | --------------------------------------------------------------- |
| `patterns`    | Required          | String/List | The patterns themselves.                                        |
| `description` | Optional          | String      | A line for your own benefit. Never read by anything that matches. |
| `enabled`     | Optional          | Boolean     | `false` switches the list off without deleting it, which leaves every reference to it valid. |

```toml
[price_patterns.junk]
description = "Form filler"
patterns = ["9*", "0", "123456"]

[item.ps5.mercadolibre]
excluded_price_pattern_sets = ["junk"]
excluded_price_patterns = ["777"]      # and this one, only here
```

Note that

1. The names are **references, not copies**. Editing a list changes every search
   that uses it, which is the point of having them; the loader refuses a search
   naming a list that does not exist, with the name in the message, rather than
   ignoring it — an ignored reference would leave a search running perfectly and
   silently excluding nothing, which only shows up weeks later as a group whose
   maximum price is 999999. Renaming one from the web UI carries the new name
   through to every search that used it.
2. The resolved list is what everything downstream sees: `excluded_price_patterns`
   in the effective configuration the web UI shows is the real, flat list of
   patterns, deduplicated, not the names.

### Translators

A translator contains a list of word mappings that translate English words to corresponding words in another language. They are used by _AI Marketplace Monitor_ to extract information from webpages in non-English languages.

This section currently accept the following values for Facebook Marketplace.

| Parameter                         | Required/Optional | Data Type | Description                                                |
| --------------------------------- | ----------------- | --------- | ---------------------------------------------------------- |
| `locale`                          | Required          | String    | locale of the translation                                  |
| `Collection of Marketplace items` | Optional          | String    | The "arial-label" for search results.                      |
| `Condition`                       | Optional          | String    | Subtitle "condition" of an listing item.                   |
| `Description`                     | Optional          | String    | Title "description" for a rental item.                     |
| `Details`                         | Optional          | String    | Subtitle "Details" of an listing item.                     |
| `Location is approximate`         | Optional          | String    | The word below listing location.                           |
| `About this vehicle`              | Optional          | String    | The "About this vehicle" section of an automobile listing. |
| `Seller's description`            | Optional          | String    | The "Seller's description" of an automobile listing.       |

Note that not all words needs to be translated (the English version will be used if unspecified), and _AI Marketplace Monitor_ may be able to extract information using language-independent methods.

Please see [Support for non-English languages](../README.md#support-for-non-english-languages)

### Mercado Libre options

There are none at the platform level: `[marketplace.mercadolibre]` does not have
to exist, and nothing in it decides whether the platform is searched. Mercado
Libre is always searched, with or without a signed-in session. What each search
asks it — the site, the condition, the shipping — goes in that search's own
`[item.<name>.mercadolibre]` block.

`require_login` used to live here and no longer exists; a file that still has it
keeps loading, with the key ignored and a warning naming it.

See [docs/mercadolibre.md](mercadolibre.md) for what the site's sign-in wall
looks like, how the monitor backs off from it, and how to sign in once with
`ai-marketplace-monitor --login`.

### Monitor Configuration

The optional `monitor` section allows you to define system configurations for the _AI Marketplace Monitor_: **when it searches**, and how it reaches the network.

| Option                | Requirement | DataType    | Description                                                                                    |
| --------------------- | ----------- | ----------- | ---------------------------------------------------------------------------------------------- |
| `search_interval`     | Optional    | String      | Time between searches (`30m`, `2h`, or a number of seconds). The low end of the range, if both. |
| `max_search_interval` | Optional    | String      | With `search_interval`, a random interval is drawn between the two before each search.          |
| `start_at`            | Optional    | String/List | Times of day to search at, *in addition to* the interval.                                       |
| `proxy_server`        | Optional    | String/List | URL for one or more proxy servers.                                                              |
| `proxy_bypass`        | Optional    | String      | Comma-separated domains to bypass proxy.                                                        |
| `proxy_username`      | Optional    | String      | username for the proxy.                                                                         |
| `proxy_password`      | Optional    | String      | password for the proxy.                                                                         |
| `parallel_marketplaces`    | Optional | Boolean    | Search every marketplace at the same time, each on a browser of its own. Default `true`.        |
| `parallel_listing_updates` | Optional | Boolean    | Give re-checking stored listings a browser and a thread of its own, so it runs alongside searching. Default `true`. |
| `listing_recheck_interval` | Optional | String     | How stale a stored listing has to be before its page is opened again. Default `6h`.             |
| `listing_review_interval`  | Optional | String     | How often a round of re-checks happens, or the low end of the range. Default `60` seconds.      |
| `listing_review_max_interval` | Optional | String  | With `listing_review_interval`, a random interval is drawn between the two before each round.   |
| `listing_review_start_at`  | Optional | String/List | Times of day to review at, *in addition to* the interval.                                      |
| `listing_review_batch`     | Optional | Integer    | Stored listings one round re-checks. Default `10`.                                              |
| `apply_changes_while_running` | Optional | Boolean | Take an edit into the search already under way instead of dropping it. Default `true`.          |
| `on_delete_running`        | Optional | String     | What deleting the running search does: `"stop"` (default) or `"finish"`.                        |
| `notify_immediately`       | Optional | Boolean    | Notify each listing as it passes, rather than once at the end of the search. Default `false`.    |
| `max_description_words`    | Optional | Integer    | Words of the seller's description a notification carries. Default `25`; `0` for no limit.        |

- When to search is a property of the program, not of a product or of a marketplace, so it is asked once here. The same three options on an `item` or a `marketplace` section are **deprecated**: they are still honored when `monitor` sets none of them, so an older configuration file keeps working unchanged, but nothing should be written there any more.
- `start_at` and the interval are not alternatives in the `monitor` section: set both and the monitor searches on its interval *and* at those times. (In the deprecated per-item form, `start_at` still replaces the interval, which is what a file written that way meant.)
- If multiple `proxy_server` URLs are specified as a list, a random one will be chosen each time. However, the proxy will not change while the _AI Marketplace Monitor_ is running.
- The browser does two kinds of work: searching for new listings, and re-reading listings already stored so a price change, a sold item or a dead link is noticed. The second one runs between searches and while waiting for the next one, a few listings at a time.
  - `listing_recheck_interval` is what makes a listing due. A listing read more recently than this is left alone, by the refresher *and* by a search that turns it up again, so the same page is not opened twice in a row.
  - Longest-overdue first, except that a listing whose search is still configured outranks one whose search has been deleted or renamed. Orphans are still re-checked and still removed when they sell — they are still in the dashboard — but they cannot hold up the search actually running, which matters because a dropped search leaves the oldest records in the store precisely because nothing has touched them since.
  - `listing_review_interval`, `listing_review_max_interval` and `listing_review_start_at` say *when* a round happens, with the same three modes the search schedule has: a fixed interval, a random one between two bounds, or fixed times of day. They are not alternatives — set an interval and some times and whichever comes first wins. `listing_review_batch` says how many listings one round re-checks. With none of them set the monitor keeps its old rhythm, a round of ten at most once a minute whenever the browser is free, so an existing configuration behaves exactly as it did.
  - The moment of the next round is drawn once, when a round ends, and published for the web UI to show. Drawing it on request instead would give a random interval a different answer every time the page refreshed.
  - `parallel_listing_updates` decides whether reviewing happens *at the same time* as searching (`true`, the default) or takes turns with it on one browser (`false`). On, it gets a browser and a thread of its own — see the note on parallelism below for why it cannot be a second tab. On by default because taking turns means the review only happens in the gaps between searches, which on a busy schedule is barely at all. The cost is a second Chromium, and it is only paid when there is something to re-check: the lane is not started while nothing in the store is overdue.
  - The two flows are kept off each other's listings by three things that hold however many are running: the queue is built from `last_seen`, which both flows write, so a listing a search has just fetched is not stale and is not in it; freshness is asked again at the moment a listing's turn comes, because the queue is a snapshot and the other flow may have read it since; and a listing being read right now is claimed, with the loser skipping rather than waiting. A marketplace that has refused either flow goes on a cooldown both read.

- The configuration is re-read from the checkpoints the scraping code already stops at, so a change is in use within seconds — mid-search, without waiting for anything to finish. The two options above are the only part the monitor cannot work out for itself: what to do when the thing you just changed is *the search running at that moment*.
  - `apply_changes_while_running` on (the default) takes the new settings into that search and lets it carry on. Lowering a maximum price is a change of mind about what the results should be, not an instruction to throw away a page already loaded and the AI calls already spent on it. Off restores the older behaviour: the search is dropped and the next one starts.
  - What a running search can actually absorb depends on when it reads a setting, and the monitor is explicit about the difference rather than claiming all of it. Filters consulted once per listing — `keywords`, `antikeywords`, `exclude_sellers`, `seller_locations`, `rating`, `notify`, the AI prompts — take effect on the very next listing. Anything that went into the URL it is paging through — `search_phrases`, `search_city`, `city_name`, `search_region`, `radius`, `min_price`, `max_price`, `condition`, `date_listed`, `delivery_method`, `availability`, `sort_by`, `currency`, `site`, `free_shipping`, `shipping_origin`, `max_pages`, `language` — applies from that search's next run, and is reported as waiting rather than counted as applied. See `docs/webui.md`.
  - `on_delete_running` decides what deleting the running search means. `"stop"` ends it at the next checkpoint, which is the natural reading of deleting something. `"finish"` lets it run to the end and notify first, which is worth choosing when a search that is nearly done is worth more than the tidiness. Either way the scraper carries straight on to the next search.
- Searching is scheduled from when each `(search, platform)` pair **actually last ran**, remembered across restarts in the `search-runs` cache namespace. This is what makes the intervals above mean what they say: `schedule` starts a job's clock when the job is built, and the schedule is rebuilt on every start and on every configuration change, so without this a restart searched everything at once and editing one search postponed all the others by a whole interval. A pair that has never run is due immediately — an interval is a gap *between* runs and cannot precede the first one.
- `parallel_marketplaces` decides whether the platforms are searched side by side (`true`, the default) or one after another (`false`). On, each platform gets a browser and a thread of its own and keeps its own cycle: one being slow, being cancelled or failing does not hold up the other. Off, the monitor works through a single queue — which **alternates between platforms** rather than emptying one before starting the next. That ordering is not cosmetic: built platform by platform and worked through in that order, the last platform in the file was not touched until every search on the first had finished, and with a forced stop or a restart sending the pass back to the top of the queue it could be scheduled, reported as configured, and never actually run. On is the default because a Facebook pass over a handful of products is the better part of an hour to wait through; turn it off on a machine where a browser per platform hurts.
- **Why parallelism means a second browser, not a second tab.** Playwright's synchronous API is bound to the thread that created it: touching a page from another thread does not race, it fails. And Chromium takes an exclusive lock on its user-data directory, so two browsers cannot share one profile. Each lane therefore gets `browser-profile-<name>` beside the main one, seeded from the same stored sessions, so the second window opens already signed in. The first marketplace in the file keeps the monitor's own browser and profile, so the one holding your session is never copied.
- A lane that cannot open a browser is not a platform that gets skipped: it is logged and that platform is searched in turn on the main browser instead.
- **Each platform keeps one browser.** Which browser a platform runs on is decided the first time it is searched and does not change afterwards. It used to be decided per pass — "the first platform in this pass gets the monitor's own browser, the rest get lanes" — from whatever happened to be due at that instant, so a platform that had a lane at 14:00 ran on the monitor's browser at 14:20 when it was the only one due. From outside that reads as a search inheriting the browser another platform had been using.
- **A parallel pass is not a barrier.** A lane whose queue empties has its searches marked as run, the schedule republished and anything newly due handed straight back to it, rather than waiting for the slowest platform. Before, two searches ran at once but their *cycles* were locked together: a lane that finished in two minutes sat holding an open browser for the fifty another platform took, with its next search not even chosen and its "next run" still showing a slot that had gone by.
- **The browsers are closed while there is nothing to search.** A gap longer than two minutes before the next search releases every window and Chromium process (the review lane excepted, because it is using its browser right then); the next search opens them again on the same persistent profiles, which costs a browser start and no sign-in. And a browser that goes away on its own — closed by hand, or crashed — is noticed and replaced rather than left as a handle the monitor believes in.

### Notifications: when they go out, and how long they are

```toml
[monitor]
notify_immediately = false
max_description_words = 25
```

- `notify_immediately` decides *when*. Off (the default), one message goes out at the end of each platform's search, covering everything it found. On, a listing is notified the moment it has passed every filter and been scored, without waiting for the rest of the platform.
  - Off is the default because the *message* is better batched: a search that turns up six listings sends one notification about six of them, and switching this on makes it six notifications. It is worth turning on when a platform takes half an hour to search and the good listing is gone in ten — which is a judgement about the market being watched, not one the monitor can make for you.
  - Either way, **nothing is sent on the scraping thread**. A channel blocks for as long as its service wants — Telegram waits out an HTTP 429, SMTP waits for a handshake — and the checkpoints that read the pause and cancel flags live in the scraping code, so a notification sent inline is a page left open and a "stop" button that does not answer. Sends go to a single worker thread behind a queue, in the order the listings were found, and a channel that fails is logged rather than allowed to hold up the ones behind it. Stopping the monitor drains that queue rather than dropping it.
- `max_description_words` decides *how much of the listing* the message carries: how many words of the text the seller wrote. Only the notification's copy is shortened — what the scraper stores is the seller's whole text, and the dashboard, the export and the AI all still read it.
  - 25 by default, because Mercado Libre sellers in particular paste their catalogue, their shipping policy and their opening hours into the description, and that buries the price in the middle of a wall of text. `0` (or `false`) means no limit.
  - **Words rather than lines**, which is what this counted at first and was wrong: a line is not a property of the text, it is a property of the screen showing it. The same "five lines" is five short ones on a desktop and fifteen wrapped ones on a phone — and a seller who writes one unbroken paragraph has a description of *one* line, so a line limit left it entirely untouched while the message stayed enormous. `max_description_lines` is still accepted and ignored, so an older file loads; the web UI drops it from the file the next time the notification settings are saved.
  - Whitespace is not a word: the line breaks inside what is kept are the seller's own, and only the tail is dropped. Over the limit, the text ends in `...`.
  - **The line limit is not what keeps a message deliverable**, and the two should not be confused. Telegram refuses any message over 4096 characters with "Message is too long" and delivers *nothing*; Pushover refuses over 1024; ntfy over 4096. The AI's commentary and a long title can exceed those between them with no description at all. So every channel declares the limit it actually has and the card is *rebuilt shorter* until it fits — description first, then the AI's comment, then the title, never the price or the link. Rebuilt rather than cut, because cutting rendered MarkdownV2 can strand a backslash from the character it escapes, which Telegram rejects just as firmly for a different reason. A batch too big for one message becomes several messages rather than fewer listings.

```toml
[monitor]
# every 5 to 15 minutes, plus a sweep at 09:00 and 18:30
search_interval = '5m'
max_search_interval = '15m'
start_at = ['09:00', '18:30']

# re-read each stored listing at most once every six hours
listing_recheck_interval = '6h'

# a round of 25 listings every 30 to 90 minutes, plus one at 09:00 and 21:00
listing_review_interval = '30m'
listing_review_max_interval = '90m'
listing_review_start_at = ['09:00', '21:00']
listing_review_batch = 25

# both on by default: a browser per platform, plus one for the re-checks
parallel_listing_updates = true
parallel_marketplaces = true

# editing the search that is running takes effect in it, and does not stop it;
# deleting it stops it there and then
apply_changes_while_running = true
on_delete_running = "stop"

# one notification per search, with 25 words of the seller's description
notify_immediately = false
max_description_words = 25
```

### Removing sold and dead listings

While re-checking a Facebook Marketplace listing, the monitor reads the page to
see whether the listing still exists. It is removed from the store — permanently,
with a tombstone, so a later search does not bring it back — in exactly two
cases:

- the listing's own heading starts with **Sold** (`Vendido`, and whatever the
  configured `language` translates it to), which is what Facebook stamps on a
  listing whose item is gone; or
- the page is Facebook's "this content isn't available right now" card, with no
  listing heading of its own.

Everything else leaves the listing exactly where it was and simply tries again
later: a timeout, a dropped connection, a rate limit, a bounce to the login page,
or a layout none of the parsers recognise. These are indistinguishable from a
deleted listing at a glance, and none of them is evidence that one is gone.

Mercado Libre listings are re-read for their price but never removed this way:
it leaves finished listings up under a different label, and the monitor has no
tested reading of those states.

### Additional options

All sections, namely `ai`, `marketplace`, `user`, `smtp`, and `region`, accepts an option `enabled`, which, if set to `false` will disable the corresponding AI service,
marketplace, SMTP server, and stop notifying corresponding user. This option works like a `comment` statement that comments out the entire sections, which allowing the
sections to be referred from elsewhere (e.g. `notify` a disable user is allowed but notification will not be sent.)

| Parameter | Required/Optional | Data Type | Description                                            |
| --------- | ----------------- | --------- | ------------------------------------------------------ |
| `enabled` | Optional          | Boolean   | Disable corresponding configuration if set to `false`. |
