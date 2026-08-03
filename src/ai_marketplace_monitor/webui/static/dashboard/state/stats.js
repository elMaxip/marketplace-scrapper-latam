// Numeric helpers shared by the summary tiles, the charts and the bargain
// detector.  Pure functions over plain arrays -- no React, no DOM -- so they
// stay cheap to memoize and easy to reason about.

/** Ascending copy of the finite numbers produced by `pick`. */
export function values(rows, pick = (row) => row.price_value) {
  const out = [];
  for (const row of rows) {
    const value = pick(row);
    if (typeof value === "number" && Number.isFinite(value)) out.push(value);
  }
  return out.sort((a, b) => a - b);
}

/**
 * Quantile of an already-sorted array, interpolating between neighbours.
 * Returns null for an empty array so callers render a dash rather than a zero.
 */
export function quantile(sorted, q) {
  if (!sorted.length) return null;
  if (sorted.length === 1) return sorted[0];
  const position = (sorted.length - 1) * q;
  const lower = Math.floor(position);
  const upper = Math.ceil(position);
  if (lower === upper) return sorted[lower];
  return sorted[lower] + (sorted[upper] - sorted[lower]) * (position - lower);
}

export function mean(sorted) {
  if (!sorted.length) return null;
  let total = 0;
  for (const value of sorted) total += value;
  return total / sorted.length;
}

/** Count, extremes and the usual quantiles, from one pass of sorting. */
export function summarize(sorted) {
  if (!sorted.length) {
    return { count: 0, min: null, max: null, mean: null, median: null, p25: null, p75: null };
  }
  return {
    count: sorted.length,
    min: sorted[0],
    max: sorted[sorted.length - 1],
    mean: mean(sorted),
    median: quantile(sorted, 0.5),
    p25: quantile(sorted, 0.25),
    p75: quantile(sorted, 0.75),
  };
}

/**
 * Bin width by the Freedman-Diaconis rule, which adapts to spread instead of
 * assuming a shape -- marketplace prices are heavily right-skewed and a fixed
 * bin count either flattens the bulk or shatters the tail.  Falls back to
 * Sturges when the IQR collapses (many identical prices).
 */
function binCountFor(sorted) {
  const n = sorted.length;
  if (n < 2) return 1;
  const iqr = quantile(sorted, 0.75) - quantile(sorted, 0.25);
  const span = sorted[n - 1] - sorted[0];
  if (span <= 0) return 1;
  if (iqr > 0) {
    const width = (2 * iqr) / Math.cbrt(n);
    if (width > 0) return Math.max(1, Math.min(60, Math.ceil(span / width)));
  }
  return Math.max(1, Math.min(60, Math.ceil(Math.log2(n) + 1)));
}

/**
 * Histogram over sorted values.
 *
 * `clipTo` drops the extreme upper tail before binning (a single mispriced
 * listing at 100x the median otherwise squeezes every real bin into the first
 * column).  Clipped rows are reported separately rather than silently dropped.
 */
export function histogram(sorted, { bins = null, clipTo = 0.99 } = {}) {
  if (!sorted.length) return { bins: [], clipped: 0, max: 0 };

  const cutoff = clipTo < 1 ? quantile(sorted, clipTo) : sorted[sorted.length - 1];
  const kept = sorted.filter((value) => value <= cutoff);
  const clipped = sorted.length - kept.length;
  if (!kept.length) return { bins: [], clipped, max: 0 };

  const low = kept[0];
  const high = kept[kept.length - 1];
  const count = bins || binCountFor(kept);
  const width = high > low ? (high - low) / count : 1;

  const buckets = Array.from({ length: count }, (_, index) => ({
    from: low + index * width,
    to: low + (index + 1) * width,
    count: 0,
  }));
  for (const value of kept) {
    const index = width > 0 ? Math.min(count - 1, Math.floor((value - low) / width)) : 0;
    buckets[index].count += 1;
  }
  return { bins: buckets, clipped, max: Math.max(...buckets.map((bucket) => bucket.count)) };
}

/**
 * Five-number summary with Tukey whiskers (1.5 x IQR) and the points beyond
 * them, which is what makes a boxplot useful for spotting underpriced listings.
 */
export function boxStats(sorted) {
  if (!sorted.length) return null;
  const q1 = quantile(sorted, 0.25);
  const q3 = quantile(sorted, 0.75);
  const iqr = q3 - q1;
  const lowFence = q1 - 1.5 * iqr;
  const highFence = q3 + 1.5 * iqr;
  const inside = sorted.filter((value) => value >= lowFence && value <= highFence);
  return {
    count: sorted.length,
    min: inside.length ? inside[0] : sorted[0],
    q1,
    median: quantile(sorted, 0.5),
    q3,
    max: inside.length ? inside[inside.length - 1] : sorted[sorted.length - 1],
    outliers: sorted.filter((value) => value < lowFence || value > highFence),
  };
}

/** Group rows into a Map keyed by `keyFn`, preserving first-seen order. */
export function groupBy(rows, keyFn) {
  const groups = new Map();
  for (const row of rows) {
    const key = keyFn(row);
    if (key === null || key === undefined || key === "") continue;
    const bucket = groups.get(key);
    if (bucket) bucket.push(row);
    else groups.set(key, [row]);
  }
  return groups;
}

/**
 * Per-group price statistics, sorted by `sortBy` descending.
 * Groups thinner than `minCount` are dropped: a "average price" over one
 * listing is noise, not a signal.
 */
export function groupStats(rows, keyFn, { labelFn = (key) => key, minCount = 1, sortBy = "count", limit = 0 } = {}) {
  const out = [];
  for (const [key, bucket] of groupBy(rows, keyFn)) {
    if (bucket.length < minCount) continue;
    const prices = values(bucket);
    const stats = summarize(prices);
    out.push({
      key,
      label: labelFn(key, bucket),
      rows: bucket,
      ...stats,
      // After the spread, not before: `summarize` also reports a `count`, but
      // that one counts listings with a readable price. The group's own count is
      // how many listings are in it, which is what every table and bar shows.
      count: bucket.length,
      priced: stats.count,
    });
  }
  out.sort((a, b) => (b[sortBy] ?? -Infinity) - (a[sortBy] ?? -Infinity));
  return limit > 0 ? out.slice(0, limit) : out;
}

const DAY_MS = 86400000;
const HOUR_MS = 3600000;

/** Local-midnight epoch millis for a timestamp. */
export function startOfDay(ms) {
  const date = new Date(ms);
  date.setHours(0, 0, 0, 0);
  return date.getTime();
}

export function startOfHour(ms) {
  return Math.floor(ms / HOUR_MS) * HOUR_MS;
}

/**
 * Bucket rows over time, emitting empty buckets too.
 *
 * Gaps matter here: a day with no listings is information, and skipping it
 * would draw a continuous line across a scraper outage.
 */
export function timeSeries(rows, { pick = (row) => row.first_seen_ms, unit = "day", limit = 0 } = {}) {
  const step = unit === "hour" ? HOUR_MS : DAY_MS;
  const floor = unit === "hour" ? startOfHour : startOfDay;

  const buckets = new Map();
  for (const row of rows) {
    const ms = pick(row);
    if (!ms) continue;
    const bucket = floor(ms);
    const entry = buckets.get(bucket);
    if (entry) entry.push(row);
    else buckets.set(bucket, [row]);
  }
  if (!buckets.size) return [];

  // Step by calendar day rather than by 86_400_000 ms: a DST shift makes a day
  // 23 or 25 hours long, and a fixed increment would drift off local midnight
  // and start missing buckets.
  const shift = (ms, days) => {
    if (unit === "hour") return ms + days * step;
    const date = new Date(ms);
    date.setDate(date.getDate() + days);
    return floor(date.getTime());
  };
  const next = (ms) => shift(ms, 1);

  const stamps = [...buckets.keys()].sort((a, b) => a - b);
  const last = stamps[stamps.length - 1];
  let first = stamps[0];
  if (limit > 0) {
    // Walk back `limit` buckets from the newest, so the window is a bucket
    // count rather than an approximate millisecond span.
    let cursor = last;
    for (let back = 1; back < limit && cursor > first; back += 1) cursor = shift(cursor, -1);
    first = Math.max(first, cursor);
  }

  const series = [];
  for (let ms = first; ms <= last; ms = next(ms)) {
    const bucket = buckets.get(ms) || [];
    const prices = values(bucket);
    series.push({
      ms,
      count: bucket.length,
      mean: mean(prices),
      median: quantile(prices, 0.5),
      rows: bucket,
    });
  }
  return series;
}
