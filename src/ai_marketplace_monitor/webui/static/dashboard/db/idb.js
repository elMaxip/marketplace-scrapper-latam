// Promise wrapper over the raw IndexedDB API.
//
// Small on purpose: the dashboard needs open/put-many/cursor-scan/get/clear and
// nothing else, and a hand-rolled wrapper keeps the vendored payload down.  All
// callers go through here so transaction and error handling live in one place.

/** Wrap an IDBRequest as a promise. */
export function request(req) {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

/** Resolve when a transaction commits; reject if it aborts or errors. */
export function done(tx) {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onabort = () => reject(tx.error || new Error("transaction aborted"));
    tx.onerror = () => reject(tx.error);
  });
}

/**
 * Open (and if needed create/upgrade) a database.
 *
 * `upgrade(db, oldVersion, tx)` runs inside the versionchange transaction.
 */
export function openDB(name, version, upgrade) {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(name, version);
    req.onupgradeneeded = (event) => {
      try {
        upgrade(req.result, event.oldVersion, req.transaction);
      } catch (err) {
        reject(err);
      }
    };
    req.onsuccess = () => {
      const db = req.result;
      // Another tab asked for a newer schema; step aside so it can upgrade
      // rather than leaving it blocked forever on our open connection.
      db.onversionchange = () => db.close();
      resolve(db);
    };
    req.onerror = () => reject(req.error);
    req.onblocked = () => reject(new Error("IndexedDB upgrade blocked by another tab"));
  });
}

/** Delete a whole database (used when the server's store identity changes). */
export function deleteDB(name) {
  return new Promise((resolve, reject) => {
    const req = indexedDB.deleteDatabase(name);
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
    // A blocked delete still succeeds once other connections close; treat it as
    // done rather than hanging the caller.
    req.onblocked = () => resolve();
  });
}

/** Write many records into one store in a single transaction. */
export async function putAll(db, storeName, rows) {
  if (!rows.length) return;
  const tx = db.transaction(storeName, "readwrite");
  const store = tx.objectStore(storeName);
  for (const row of rows) store.put(row);
  await done(tx);
}

export async function put(db, storeName, row) {
  const tx = db.transaction(storeName, "readwrite");
  tx.objectStore(storeName).put(row);
  await done(tx);
}

export async function get(db, storeName, key) {
  const tx = db.transaction(storeName, "readonly");
  return request(tx.objectStore(storeName).get(key));
}

export async function count(db, storeName) {
  const tx = db.transaction(storeName, "readonly");
  return request(tx.objectStore(storeName).count());
}

export async function clear(db, storeName) {
  const tx = db.transaction(storeName, "readwrite");
  tx.objectStore(storeName).clear();
  await done(tx);
}

/**
 * Walk every record in a store, handing each to `visit`.
 *
 * Preferred over `getAll` for the main listing store: the dashboard only keeps
 * a compact projection of each row in memory, and a cursor lets each full
 * record (description, history, image URLs) be dropped as soon as it has been
 * projected instead of materializing tens of thousands of them at once.
 */
export function eachRecord(db, storeName, visit, { index = null } = {}) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, "readonly");
    const source = index ? tx.objectStore(storeName).index(index) : tx.objectStore(storeName);
    const req = source.openCursor();
    let seen = 0;
    req.onsuccess = () => {
      const cursor = req.result;
      if (!cursor) {
        resolve(seen);
        return;
      }
      visit(cursor.value, seen);
      seen += 1;
      cursor.continue();
    };
    req.onerror = () => reject(req.error);
    tx.onabort = () => reject(tx.error || new Error("cursor transaction aborted"));
  });
}
