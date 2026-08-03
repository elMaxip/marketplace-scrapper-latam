// Turn a server record into the row the dashboard stores and queries.
//
// The scraper keeps prices as display strings ("$100.000", "MX$1,200") and
// location as one free-text line ("Nunoa, Region Metropolitana"), so the fields
// the panel actually filters and charts on -- numeric price, currency, comuna,
// city, timestamps in millis -- are derived here.
//
// Deriving on the client rather than the server is deliberate: these are
// heuristics that will get tuned against real data, and a tweak only needs a
// SCHEMA_VERSION bump to re-derive from the local copy, with no change to what
// the monitor persists.

/** Price strings the marketplace uses for "no price given". */
const UNPRICED = new Set(["", "**unspecified**", "-", "--"]);

/** Words that mean zero, across the languages the scraper supports. */
const FREE_WORDS = /^(free|gratis|gratuito|grátis|kostenlos|gratuit|regalo)$/i;

/** Dropped from the tail of a location so "Nunoa, Chile" groups as "Nunoa". */
const COUNTRY_SUFFIXES = new Set([
  "chile",
  "argentina",
  "mexico",
  "méxico",
  "peru",
  "perú",
  "colombia",
  "espana",
  "españa",
  "spain",
  "united states",
  "usa",
  "canada",
  "canadá",
]);

/** Symbol -> ISO code, for the unambiguous ones only. */
const CURRENCY_CODES = {
  "€": "EUR",
  "£": "GBP",
  "¥": "JPY",
  "₹": "INR",
  "₩": "KRW",
  "₪": "ILS",
  "₺": "TRY",
  "R$": "BRL",
  "MX$": "MXN",
  "CA$": "CAD",
  "A$": "AUD",
  "CLP": "CLP",
  "US$": "USD",
};

/** Lowercase, strip accents and collapse whitespace -- for grouping keys. */
export function foldText(value) {
  return String(value || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * A single price that the scraper split at its thousands separator: Facebook
 * renders CLP as "100 000", and an older `extract_price` stored that as
 * "100 | 000".
 *
 * " | " normally separates two real prices (current and struck-through
 * original), so this has to be narrow: every piece after the first must be
 * exactly three bare digits, and none may carry its own currency.  A genuine
 * pair looks like "$80.000 | $90.000" and does not match.
 *
 * Repaired on read because those strings are already in the cache, and the
 * listings they came from will not be fetched again.
 */
const SPLIT_THOUSANDS = /^([^\d|]*?)\s*(\d{1,3})((?:\s*\|\s*\d{3})+)$/;

function healSplitThousands(text) {
  const match = SPLIT_THOUSANDS.exec(text);
  if (!match) return text;
  const groups = match[3].split("|").map((part) => part.trim()).filter(Boolean);
  return `${match[1]}${match[2]}.${groups.join(".")}`;
}

/**
 * Split a price string into a number and its currency label.
 *
 * The hard part is the separator: "$100.000" is a hundred thousand Chilean
 * pesos while "$100.00" is a hundred dollars.  When both separators appear the
 * last one is the decimal point; when only one appears it is a decimal point
 * only if exactly one or two digits follow it, and a thousands separator
 * otherwise.
 */
export function parsePrice(raw) {
  const text = healSplitThousands(String(raw || "").trim());
  if (UNPRICED.has(text.toLowerCase())) return { value: null, currency: "" };
  if (FREE_WORDS.test(text)) return { value: 0, currency: "" };

  const match = text.match(/\d[\d., \s]*/);
  if (!match) return { value: null, currency: "" };

  // A leading symbol, or a trailing one on a bare number -- but never anything
  // holding digits. On a two-price string the tail is the *other* price, and
  // pasting that in front of the amount is how "$150.000" rendered "| 000150".
  const before = text.slice(0, match.index).trim();
  const after = text.slice(match.index + match[0].length).trim();
  const candidate = before || after;
  const currency = /\d/.test(candidate) ? "" : candidate;

  let digits = match[0].replace(/[ \s]/g, "");
  const lastDot = digits.lastIndexOf(".");
  const lastComma = digits.lastIndexOf(",");
  let decimalAt = -1;
  if (lastDot >= 0 && lastComma >= 0) {
    decimalAt = Math.max(lastDot, lastComma);
  } else if (lastDot >= 0 || lastComma >= 0) {
    const only = Math.max(lastDot, lastComma);
    const trailing = digits.length - only - 1;
    // A single separator with 1-2 trailing digits is a decimal point; with 3
    // (or an odd count) it is grouping.
    if (trailing === 1 || trailing === 2) decimalAt = only;
  }

  if (decimalAt >= 0) {
    digits = digits.slice(0, decimalAt).replace(/[.,]/g, "") + "." + digits.slice(decimalAt + 1);
  } else {
    digits = digits.replace(/[.,]/g, "");
  }

  const value = Number.parseFloat(digits);
  return {
    value: Number.isFinite(value) ? value : null,
    currency: CURRENCY_CODES[currency] || currency,
  };
}

/**
 * Split the free-text location into comuna and city.
 *
 * Facebook renders locality first ("Nunoa, Region Metropolitana"), so the head
 * is the comuna and the tail, once any country name is dropped, is the wider
 * city or region.  A single-part location is treated as both.
 */
export function parseLocation(raw) {
  const parts = String(raw || "")
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean)
    .filter((part) => !COUNTRY_SUFFIXES.has(foldText(part)));

  if (!parts.length) return { comuna: "", city: "" };
  if (parts.length === 1) return { comuna: parts[0], city: parts[0] };
  return { comuna: parts[0], city: parts[parts.length - 1] };
}

/** Epoch millis from an ISO timestamp, or 0 when unparseable. */
export function toMillis(iso) {
  if (!iso) return 0;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms : 0;
}

/**
 * Collapse a title to a comparison key.
 *
 * Used to group repostings of the same product and to spot sellers listing the
 * same thing over and over.  Prices, sizes and punctuation are stripped so
 * "iPhone 13 128GB - $400.000" and "iphone 13 128 gb" land on the same key.
 */
export function productKey(title) {
  return foldText(title)
    .replace(/[^\p{L}\p{N}\s]/gu, " ")
    .split(/\s+/)
    .filter((word) => word && !/^\d{4,}$/.test(word))
    .sort()
    .join(" ")
    .trim();
}

/** Lowest price this listing was ever seen at, from its recorded price series. */
function historicalLow(pricePoints, current) {
  let low = current;
  for (const point of pricePoints || []) {
    const { value } = parsePrice(point.price);
    if (value !== null && (low === null || value < low)) low = value;
  }
  return low;
}

/**
 * Build the stored row for one server record.
 *
 * Everything the panel filters, sorts or charts on is precomputed here so that
 * neither the render path nor an IndexedDB index has to parse strings.
 */
export function buildRow(record) {
  const { value: priceValue, currency } = parsePrice(record.price);
  const { comuna, city } = parseLocation(record.location);
  const rating = record.rating || null;

  return {
    ...record,
    price_value: priceValue,
    price_min_value: historicalLow(record.price_points, priceValue),
    currency,
    comuna,
    city,
    comuna_key: foldText(comuna),
    city_key: foldText(city),
    seller_key: foldText(record.seller),
    product_key: productKey(record.title),
    // The search item that turned this listing up is the closest thing the
    // scraper records to a product type.
    item: (record.items && record.items[0]) || record.name || "",
    first_seen_ms: toMillis(record.first_seen),
    last_seen_ms: toMillis(record.last_seen),
    price_changes: (record.history || []).filter((entry) => entry.changes && entry.changes.price).length,
    score: rating && Number.isFinite(rating.score) ? rating.score : null,
    notified_count: Object.keys(record.notified || {}).length,
    search_text: foldText(`${record.title} ${record.seller} ${record.location}`),
  };
}

/**
 * The subset of a row kept in memory for aggregation.
 *
 * Descriptions, change history and image URLs stay in IndexedDB and are read
 * back by key only when a detail view opens, which is what keeps a dataset of
 * tens of thousands of listings comfortable in memory.
 */
export function toProjection(row) {
  return {
    key: row.key,
    id: row.id,
    marketplace: row.marketplace,
    title: row.title,
    price: row.price,
    price_value: row.price_value,
    price_min_value: row.price_min_value,
    currency: row.currency,
    location: row.location,
    comuna: row.comuna,
    comuna_key: row.comuna_key,
    city: row.city,
    city_key: row.city_key,
    seller: row.seller,
    seller_key: row.seller_key,
    condition: row.condition,
    item: row.item,
    product_key: row.product_key,
    post_url: row.post_url,
    image: row.image,
    matched: row.matched,
    seen_count: row.seen_count,
    price_changes: row.price_changes,
    score: row.score,
    notified_count: row.notified_count,
    first_seen_ms: row.first_seen_ms,
    last_seen_ms: row.last_seen_ms,
    search_text: row.search_text,
  };
}
