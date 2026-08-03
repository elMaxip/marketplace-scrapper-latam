// Single source of truth for the panel: the corpus, the global filters, and
// the filtered view every section renders from.

import { createContext, html, useCallback, useContext, useEffect, useMemo, useRef, useState } from "../html.js";
import { loadProjection, open, sync } from "../db/sync.js";
import { EMPTY_FILTERS, applyFilters, filterOptions } from "./filters.js";

const DataContext = createContext(null);

/** How often to ask the server for new observations while the panel is open. */
const POLL_MS = 20000;

export function DataProvider({ children }) {
  const [db, setDb] = useState(null);
  const [rows, setRows] = useState([]);
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [status, setStatus] = useState({ phase: "starting", fetched: 0, error: null, lastSync: 0 });

  // Guards against a poll firing while the previous one is still draining a
  // first sync of tens of thousands of records.
  const busy = useRef(false);

  const refresh = useCallback(
    async (handle, { silent = false } = {}) => {
      if (!handle || busy.current) return;
      busy.current = true;
      try {
        if (!silent) setStatus((prev) => ({ ...prev, phase: "syncing", error: null }));
        const result = await sync(handle, {
          onProgress: ({ fetched }) => setStatus((prev) => ({ ...prev, phase: "syncing", fetched })),
        });
        if (result.changed || !rows.length) {
          setStatus((prev) => ({ ...prev, phase: "loading" }));
          setRows(await loadProjection(handle));
        }
        setStatus((prev) => ({ ...prev, phase: "ready", error: null, lastSync: Date.now() }));
      } catch (err) {
        if (err && err.unauthenticated) {
          // app.js owns the login screen; it will re-show it on its own poll.
          setStatus((prev) => ({ ...prev, phase: "error", error: "Sesión expirada" }));
        } else {
          setStatus((prev) => ({ ...prev, phase: "error", error: String((err && err.message) || err) }));
        }
      } finally {
        busy.current = false;
      }
    },
    [rows.length],
  );

  // Open the database once, then do the initial sync.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const handle = await open();
        if (cancelled) return;
        setDb(handle);
        // Show whatever was already stored before waiting on the network, so a
        // reload paints instantly instead of blocking on a sync.
        const cached = await loadProjection(handle);
        if (cancelled) return;
        if (cached.length) {
          setRows(cached);
          setStatus((prev) => ({ ...prev, phase: "ready" }));
        }
        await refresh(handle, { silent: cached.length > 0 });
      } catch (err) {
        if (!cancelled) {
          setStatus({ phase: "error", fetched: 0, error: String((err && err.message) || err), lastSync: 0 });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // Deliberately once: `refresh` changes identity as rows load, and re-running
    // this would reopen the database on every sync.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Poll for new observations, but only while the tab is actually visible.
  useEffect(() => {
    if (!db) return undefined;
    const tick = () => {
      if (document.visibilityState === "visible") refresh(db, { silent: true });
    };
    const timer = setInterval(tick, POLL_MS);
    document.addEventListener("visibilitychange", tick);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [db, refresh]);

  const setFilter = useCallback((patch) => {
    setFilters((prev) => ({ ...prev, ...patch }));
  }, []);

  const resetFilters = useCallback(() => setFilters(EMPTY_FILTERS), []);

  // Options come from the whole corpus; picking one comuna must not empty the
  // comuna dropdown.
  const options = useMemo(() => filterOptions(rows), [rows]);
  const filtered = useMemo(() => applyFilters(rows, filters), [rows, filters]);

  const value = useMemo(
    () => ({
      db,
      rows,
      filtered,
      filters,
      setFilter,
      resetFilters,
      options,
      status,
      refresh: () => refresh(db),
    }),
    [db, rows, filtered, filters, setFilter, resetFilters, options, status, refresh],
  );

  return html`<${DataContext.Provider} value=${value}>${children}<//>`;
}

export function useData() {
  const value = useContext(DataContext);
  if (!value) throw new Error("useData must be used inside <DataProvider>");
  return value;
}
