// The global filter model.
//
// One object drives every panel: tiles, charts, tables and the bargain finder
// all read the same filtered row set, which is what makes "los filtros deben
// afectar todo el panel" true by construction rather than by discipline.

import { foldText } from "../db/normalize.js";

/**
 * How long a listing may go unseen before it stops counting as active.
 *
 * The scraper does not revisit listings, so "sold" and "deleted" are not
 * observable -- what is observable is that a listing stopped showing up in
 * search results.  Anything not seen within this window is reported as
 * `inactive`, which is an honest statement about our data rather than a guess
 * about the seller.
 */
export const STALE_AFTER_MS = 3 * 86400000;

export const EMPTY_FILTERS = {
  item: "",
  condition: "",
  city: "",
  comuna: "",
  seller: "",
  status: "",
  minPrice: null,
  maxPrice: null,
  from: "",
  to: "",
  minScore: null,
  matchedOnly: false,
  notifiedOnly: false,
  search: "",
};

export function isEmptyFilters(filters) {
  return Object.keys(EMPTY_FILTERS).every((field) => {
    const value = filters[field];
    const blank = EMPTY_FILTERS[field];
    return value === blank || value === null || value === "" || value === false;
  });
}

export function activeFilterCount(filters) {
  return Object.keys(EMPTY_FILTERS).filter((field) => {
    const value = filters[field];
    return value !== EMPTY_FILTERS[field] && value !== null && value !== "" && value !== false;
  }).length;
}

/** `active` while a listing keeps reappearing in searches, `inactive` after. */
export function statusOf(row, now = Date.now()) {
  return now - row.last_seen_ms <= STALE_AFTER_MS ? "active" : "inactive";
}

/**
 * Apply the filter set to the projection.
 *
 * A single pass with early exits, because this runs on every keystroke of the
 * search box over the whole corpus.  Text matching uses the precomputed folded
 * `search_text` so no per-row normalization happens here.
 */
export function applyFilters(rows, filters, now = Date.now()) {
  const search = foldText(filters.search);
  const fromMs = filters.from ? Date.parse(`${filters.from}T00:00:00`) : null;
  const toMs = filters.to ? Date.parse(`${filters.to}T23:59:59`) : null;

  const out = [];
  for (const row of rows) {
    if (filters.matchedOnly && !row.matched) continue;
    if (filters.notifiedOnly && !row.notified_count) continue;
    if (filters.item && row.item !== filters.item) continue;
    if (filters.condition && row.condition !== filters.condition) continue;
    if (filters.city && row.city_key !== filters.city) continue;
    if (filters.comuna && row.comuna_key !== filters.comuna) continue;
    if (filters.seller && row.seller_key !== filters.seller) continue;
    if (filters.status && statusOf(row, now) !== filters.status) continue;
    if (filters.minScore !== null && (row.score === null || row.score < filters.minScore)) continue;

    if (filters.minPrice !== null || filters.maxPrice !== null) {
      // A listing with no readable price cannot satisfy a price bound; keeping
      // it would quietly inflate every count the price filter is meant to trim.
      if (row.price_value === null) continue;
      if (filters.minPrice !== null && row.price_value < filters.minPrice) continue;
      if (filters.maxPrice !== null && row.price_value > filters.maxPrice) continue;
    }

    if (fromMs !== null && row.first_seen_ms < fromMs) continue;
    if (toMs !== null && row.first_seen_ms > toMs) continue;
    if (search && !row.search_text.includes(search)) continue;

    out.push(row);
  }
  return out;
}

/**
 * The option lists shown in the filter bar.
 *
 * Built from the *unfiltered* corpus so that choosing a comuna does not make
 * every other comuna disappear from its own dropdown.
 */
export function filterOptions(rows) {
  const items = new Set();
  const conditions = new Set();
  const cities = new Map();
  const comunas = new Map();
  const sellers = new Map();

  for (const row of rows) {
    if (row.item) items.add(row.item);
    if (row.condition) conditions.add(row.condition);
    if (row.city_key && !cities.has(row.city_key)) cities.set(row.city_key, row.city);
    if (row.comuna_key && !comunas.has(row.comuna_key)) comunas.set(row.comuna_key, row.comuna);
    if (row.seller_key && !sellers.has(row.seller_key)) sellers.set(row.seller_key, row.seller);
  }

  const pairs = (map) =>
    [...map.entries()]
      .map(([value, label]) => ({ value, label }))
      .sort((a, b) => a.label.localeCompare(b.label));

  return {
    items: [...items].sort((a, b) => a.localeCompare(b)),
    conditions: [...conditions].sort((a, b) => a.localeCompare(b)),
    cities: pairs(cities),
    comunas: pairs(comunas),
    sellers: pairs(sellers),
  };
}
