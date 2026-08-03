// Pull the server's observation feed into IndexedDB.
//
// The server hands out a monotonic revision per listing write, so a sync is
// "give me everything above my cursor", repeated until the server says there is
// nothing more.  The first run starts at zero and walks the whole corpus; every
// run after that moves only what actually changed.

import { clear, eachRecord, get, put, putAll } from "./idb.js";
import { buildRow, toProjection } from "./normalize.js";
import { LISTINGS, META, SCHEMA_VERSION, open } from "./schema.js";

const SYNC_META_KEY = "sync";
const PAGE_SIZE = 1000;

/** Rows written per IndexedDB transaction while draining a server page. */
const WRITE_CHUNK = 500;

async function readSyncState(db) {
  const stored = await get(db, META, SYNC_META_KEY);
  return {
    epoch: (stored && stored.epoch) || "",
    cursor: (stored && stored.cursor) || 0,
    schema: (stored && stored.schema) || 0,
    lastSync: (stored && stored.lastSync) || 0,
  };
}

function writeSyncState(db, state) {
  return put(db, META, { k: SYNC_META_KEY, ...state });
}

async function fetchPage(since, limit) {
  const res = await fetch(`/api/listings?since=${since}&limit=${limit}`, {
    credentials: "same-origin",
  });
  if (res.status === 401) {
    const err = new Error("unauthenticated");
    err.unauthenticated = true;
    throw err;
  }
  if (!res.ok) throw new Error(`sync failed: HTTP ${res.status}`);
  return res.json();
}

/** Forget everything local and resync from scratch. */
async function reset(db) {
  await clear(db, LISTINGS);
  await writeSyncState(db, { epoch: "", cursor: 0, schema: SCHEMA_VERSION, lastSync: 0 });
}

/**
 * Bring the local copy up to date.
 *
 * `onProgress({ fetched, total, phase })` is called as pages land so the UI can
 * show progress during the first, long sync.
 *
 * Returns `{ changed, cursor, revision, fetched }`; `changed` says whether
 * anything was written, which is what tells the caller a reload of the
 * in-memory projection is worth doing.
 */
export async function sync(db, { onProgress = () => {} } = {}) {
  let state = await readSyncState(db);

  // A different derivation version means the stored rows were built by an older
  // normalizer.  Rebuilding from the server is cheaper to reason about than
  // migrating rows in place, and cannot lose data.
  if (state.schema !== SCHEMA_VERSION) {
    await reset(db);
    state = await readSyncState(db);
  }

  let { cursor, epoch } = state;
  let fetched = 0;
  let changed = false;
  let revision = 0;

  for (;;) {
    const page = await fetchPage(cursor, PAGE_SIZE);
    revision = page.revision;

    // The server's store was cleared and rebuilt (new epoch), or our cursor is
    // past its revision counter -- either way the local copy describes a store
    // that no longer exists.
    const staleStore = (epoch && page.epoch && epoch !== page.epoch) || cursor > page.revision;
    if (staleStore) {
      await reset(db);
      cursor = 0;
      epoch = page.epoch;
      fetched = 0;
      changed = true;
      continue;
    }
    epoch = page.epoch || epoch;

    if (page.records.length) {
      const rows = page.records.map(buildRow);
      for (let start = 0; start < rows.length; start += WRITE_CHUNK) {
        await putAll(db, LISTINGS, rows.slice(start, start + WRITE_CHUNK));
      }
      fetched += rows.length;
      changed = true;
      onProgress({ fetched, total: page.revision, phase: "download" });
    }

    cursor = page.cursor;
    if (!page.more) break;
  }

  await writeSyncState(db, { epoch, cursor, schema: SCHEMA_VERSION, lastSync: Date.now() });
  return { changed, cursor, revision, fetched };
}

/**
 * Read every stored listing back as the compact in-memory projection.
 *
 * A cursor walk rather than `getAll`, so full records (descriptions, change
 * history) are released as soon as they have been projected.
 */
export async function loadProjection(db, { onProgress = () => {} } = {}) {
  const rows = [];
  await eachRecord(db, LISTINGS, (row, seen) => {
    rows.push(toProjection(row));
    if (seen % 2000 === 0) onProgress({ fetched: seen, phase: "load" });
  });
  return rows;
}

/** Full stored record for one listing, including description and history. */
export function loadDetail(db, key) {
  return get(db, LISTINGS, key);
}

export { open, reset };
