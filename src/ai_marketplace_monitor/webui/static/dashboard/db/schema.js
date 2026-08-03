// IndexedDB layout for the dashboard's local copy of the listing corpus.

import { openDB } from "./idb.js";

export const DB_NAME = "aimm-dashboard";

/**
 * Structural version: bump only when stores or indexes change.
 * Triggers an IndexedDB `versionchange` upgrade.
 */
export const DB_VERSION = 1;

/**
 * Derivation version: bump when `normalize.js` changes what it computes.
 * Stored rows are then stale even though the structure is fine, so the sync
 * layer drops them and re-pulls from revision zero -- the server is the source
 * of truth, so a rebuild is always available and never loses anything.
 */
export const SCHEMA_VERSION = 2;

export const LISTINGS = "listings";
export const META = "meta";

/**
 * Indexes exist for the lookups that stay in IndexedDB: opening a listing's
 * detail view, pulling one seller's postings, scoping a rebuild to a date
 * range.  Charts and summary tiles are computed from the in-memory projection
 * instead, because an index cannot answer "median price per comuna".
 */
const LISTING_INDEXES = [
  ["by_last_seen", "last_seen_ms"],
  ["by_first_seen", "first_seen_ms"],
  ["by_seller", "seller_key"],
  ["by_comuna", "comuna_key"],
  ["by_item", "item"],
  ["by_product", "product_key"],
  ["by_price", "price_value"],
];

function upgrade(db) {
  if (!db.objectStoreNames.contains(LISTINGS)) {
    const store = db.createObjectStore(LISTINGS, { keyPath: "key" });
    for (const [name, keyPath] of LISTING_INDEXES) {
      store.createIndex(name, keyPath);
    }
  }
  if (!db.objectStoreNames.contains(META)) {
    db.createObjectStore(META, { keyPath: "k" });
  }
}

export function open() {
  return openDB(DB_NAME, DB_VERSION, upgrade);
}
