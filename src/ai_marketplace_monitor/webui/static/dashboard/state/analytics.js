// Every derived number the panel shows, as pure functions over the filtered
// projection.  Keeping them here (rather than inside components) means each one
// is memoizable on its inputs and testable without React.

import { boxStats, groupStats, quantile, summarize, values } from "./stats.js";
import { statusOf } from "./filters.js";

const HOUR_MS = 3600000;
const DAY_MS = 86400000;

/**
 * Which currency the selection is in, and whether it is actually one currency.
 *
 * Averaging across currencies produces a number that means nothing, so the
 * panel needs to know when to say so rather than quietly printing it.
 */
export function currencyInfo(rows) {
  const counts = new Map();
  for (const row of rows) {
    if (row.price_value === null) continue;
    const key = row.currency || "";
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  const ranked = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  return {
    primary: ranked.length ? ranked[0][0] : "",
    mixed: ranked.length > 1,
    breakdown: ranked.map(([code, count]) => ({ code: code || "—", count })),
  };
}

/** The headline tiles: volume, price shape, reach and recency. */
export function summary(rows, now = Date.now()) {
  const prices = values(rows);
  const stats = summarize(prices);

  const sellers = new Set();
  const cities = new Set();
  const comunas = new Set();
  let today = 0;
  let lastHour = 0;
  let active = 0;
  let unpriced = 0;

  const dayStart = new Date(now);
  dayStart.setHours(0, 0, 0, 0);
  const dayStartMs = dayStart.getTime();

  for (const row of rows) {
    if (row.seller_key) sellers.add(row.seller_key);
    if (row.city_key) cities.add(row.city_key);
    if (row.comuna_key) comunas.add(row.comuna_key);
    if (row.first_seen_ms >= dayStartMs) today += 1;
    if (now - row.first_seen_ms <= HOUR_MS) lastHour += 1;
    if (statusOf(row, now) === "active") active += 1;
    if (row.price_value === null) unpriced += 1;
  }

  return {
    total: rows.length,
    priced: stats.count,
    unpriced,
    mean: stats.mean,
    median: stats.median,
    min: stats.min,
    max: stats.max,
    p25: stats.p25,
    p75: stats.p75,
    sellers: sellers.size,
    cities: cities.size,
    comunas: comunas.size,
    today,
    lastHour,
    active,
    inactive: rows.length - active,
    currency: currencyInfo(rows),
  };
}

const labelFromRows = (field) => (key, bucket) => bucket[0][field] || key;

/** Price and volume per search item -- the closest thing we have to a product type. */
export const byItem = (rows, options = {}) =>
  groupStats(rows, (row) => row.item, { sortBy: "count", ...options });

export const byCity = (rows, options = {}) =>
  groupStats(rows, (row) => row.city_key, { labelFn: labelFromRows("city"), sortBy: "count", ...options });

export const byComuna = (rows, options = {}) =>
  groupStats(rows, (row) => row.comuna_key, { labelFn: labelFromRows("comuna"), sortBy: "count", ...options });

export const byCondition = (rows, options = {}) =>
  groupStats(rows, (row) => row.condition, { sortBy: "count", ...options });

/**
 * Box statistics per category, for categories with enough listings to have a
 * distribution worth drawing.
 */
export function boxesBy(rows, keyFn, { labelFn = (key) => key, minCount = 5, limit = 12 } = {}) {
  const out = [];
  for (const group of groupStats(rows, keyFn, { labelFn, minCount, sortBy: "count", limit })) {
    const box = boxStats(values(group.rows));
    if (box) out.push({ key: group.key, label: group.label, box, rows: group.rows });
  }
  return out;
}

/** The three most common categories in a comuna, for the geography table. */
function topCategories(bucket, take = 3) {
  const counts = new Map();
  for (const row of bucket) {
    if (!row.item) continue;
    counts.set(row.item, (counts.get(row.item) || 0) + 1);
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, take)
    .map(([label, count]) => ({ label, count }));
}

/** Per-comuna rollup: volume, price level and what sells there. */
export function comunaProfiles(rows, now = Date.now()) {
  return byComuna(rows).map((group) => ({
    ...group,
    active: group.rows.filter((row) => statusOf(row, now) === "active").length,
    sellers: new Set(group.rows.map((row) => row.seller_key).filter(Boolean)).size,
    categories: topCategories(group.rows),
  }));
}

/** Clamp `value` into 0..1 across the [low, high] band. */
function ramp(value, low, high) {
  if (!Number.isFinite(value)) return 0;
  if (value <= low) return 0;
  if (value >= high) return 1;
  return (value - low) / (high - low);
}

/**
 * Seller rollup plus a reseller likelihood.
 *
 * There is no ground truth for "reseller", so the score is an explicit blend of
 * three observable behaviours -- volume, cadence, and how often the same product
 * is relisted -- and every component is reported alongside it so the number can
 * be argued with rather than trusted blindly.
 */
export function sellerProfiles(rows, now = Date.now()) {
  const out = [];
  for (const group of groupStats(rows, (row) => row.seller_key, { labelFn: labelFromRows("seller") })) {
    const first = Math.min(...group.rows.map((row) => row.first_seen_ms || Infinity));
    const last = Math.max(...group.rows.map((row) => row.first_seen_ms || 0));
    const spanDays = Math.max(1, (last - first) / DAY_MS + 1);
    const perDay = group.count / spanDays;
    const distinctProducts = new Set(group.rows.map((row) => row.product_key).filter(Boolean)).size;
    const repeatRatio = group.count > 1 ? 1 - distinctProducts / group.count : 0;

    const score =
      0.4 * ramp(group.count, 3, 25) + 0.3 * ramp(perDay, 0.3, 3) + 0.3 * repeatRatio;

    out.push({
      ...group,
      seller: group.label,
      firstSeen: Number.isFinite(first) ? first : 0,
      lastSeen: last,
      spanDays,
      perDay,
      distinctProducts,
      repeatRatio,
      active: group.rows.filter((row) => statusOf(row, now) === "active").length,
      isNew: now - first <= 7 * DAY_MS,
      resellerScore: score,
      // Volume alone is not enough: a hobbyist clearing out a garage can post
      // five things in a week without being a reseller.
      likelyReseller: group.count >= 4 && score >= 0.5,
    });
  }
  return out;
}

/**
 * Group listings that look like the same product.
 *
 * `product_key` is a sorted bag of title words, so repostings and near-identical
 * titles collapse together without needing fuzzy matching at query time.
 */
export function productGroups(rows, { minCount = 1 } = {}) {
  const groups = groupStats(rows, (row) => row.product_key || row.title, {
    labelFn: (key, bucket) => bucket[0].title,
    minCount,
    sortBy: "count",
  });
  return groups.map((group) => {
    const cheapest = group.rows.reduce(
      (best, row) => (row.price_value !== null && (best === null || row.price_value < best.price_value) ? row : best),
      null,
    );
    return {
      ...group,
      cheapest,
      sellers: new Set(group.rows.map((row) => row.seller_key).filter(Boolean)).size,
      lastSeen: Math.max(...group.rows.map((row) => row.last_seen_ms || 0)),
    };
  });
}

/**
 * Listings priced well below their peers.
 *
 * Comparison is against the *same product* where there are enough comparable
 * listings, and against the search item otherwise -- "cheap for an iPhone 13" is
 * a useful statement, "cheap for a phone" much less so.  Three independent
 * reasons can fire, and a listing keeps all that apply.
 */
export function bargains(rows, { discount = 0.25, minGroup = 4, limit = 100 } = {}) {
  const buckets = new Map();
  for (const row of rows) {
    if (row.price_value === null) continue;
    const key = row.product_key || row.item;
    if (!key) continue;
    const bucket = buckets.get(key);
    if (bucket) bucket.push(row);
    else buckets.set(key, [row]);
  }

  // Fall back to the item-level distribution for products with too few peers.
  const itemStats = new Map();
  for (const group of byItem(rows)) {
    const prices = values(group.rows);
    if (prices.length >= minGroup) {
      itemStats.set(group.key, { median: quantile(prices, 0.5), q1: quantile(prices, 0.25), q3: quantile(prices, 0.75) });
    }
  }

  const found = [];
  for (const [, bucket] of buckets) {
    const prices = values(bucket);
    const usingProduct = prices.length >= minGroup;
    for (const row of bucket) {
      const reference = usingProduct
        ? { median: quantile(prices, 0.5), q1: quantile(prices, 0.25), q3: quantile(prices, 0.75) }
        : itemStats.get(row.item);
      if (!reference || !reference.median) continue;

      const gap = (reference.median - row.price_value) / reference.median;
      const iqr = reference.q3 - reference.q1;
      const reasons = [];

      if (gap >= discount) reasons.push({ code: "below_median", gap });
      if (iqr > 0 && row.price_value < reference.q1 - 1.5 * iqr) reasons.push({ code: "outlier", gap });
      if (
        row.price_min_value !== null &&
        row.price_value !== null &&
        row.price_changes > 0 &&
        row.price_value <= row.price_min_value
      ) {
        reasons.push({ code: "own_low", gap });
      }

      if (reasons.length) {
        found.push({
          row,
          gap,
          reasons,
          reference: reference.median,
          scope: usingProduct ? "product" : "item",
          peers: usingProduct ? prices.length : null,
        });
      }
    }
  }

  found.sort((a, b) => b.gap - a.gap);
  return limit > 0 ? found.slice(0, limit) : found;
}
