// Dashboard shell: sync status, the global filter bar, and the sections.
//
// Sections are tabs rather than one long scroll so that only the active one
// computes its aggregations -- the filter change that repaints the summary tiles
// does not also rebuild the seller profiles for a view nobody is looking at.

import { html, useMemo, useState } from "./html.js";
import { DataProvider, useData } from "./state/DataContext.js";
import { FilterBar } from "./components/FilterBar.js";
import { Overview } from "./components/Overview.js";
import { Charts } from "./components/Charts.js";
import { Geography } from "./components/Geography.js";
import { Sellers } from "./components/Sellers.js";
import { Bargains } from "./components/Bargains.js";
import { Listings } from "./components/Listings.js";
import { ListingDetail } from "./components/ListingDetail.js";

const SECTIONS = [
  { id: "resumen", label: "Resumen" },
  { id: "graficos", label: "Gráficos" },
  { id: "comunas", label: "Comunas" },
  { id: "vendedores", label: "Vendedores" },
  { id: "gangas", label: "Oportunidades" },
  { id: "listado", label: "Listado" },
];

function SyncStatus() {
  const { status, refresh, rows } = useData();
  const label = useMemo(() => {
    switch (status.phase) {
      case "starting":
        return "abriendo almacén local…";
      case "syncing":
        return status.fetched ? `sincronizando ${status.fetched.toLocaleString()}…` : "sincronizando…";
      case "loading":
        return "cargando…";
      case "error":
        return status.error || "error";
      default:
        return `${rows.length.toLocaleString()} publicaciones guardadas`;
    }
  }, [status, rows.length]);

  const busy = status.phase === "syncing" || status.phase === "loading" || status.phase === "starting";

  return html`
    <div class=${`sync-status${status.phase === "error" ? " err" : ""}`}>
      <span class=${busy ? "dot busy" : "dot"} aria-hidden="true"></span>
      <span>${label}</span>
      <button class="ghost small" onClick=${refresh} disabled=${busy}>Actualizar</button>
    </div>
  `;
}

function Sections() {
  const { status, rows } = useData();
  const [section, setSection] = useState("resumen");
  const [detail, setDetail] = useState(null);

  // Hold the previous render while a refresh lands rather than flashing a
  // skeleton; the numbers stay on screen and simply go quiet for a moment.
  const refreshing = status.phase === "syncing" && rows.length > 0;

  if (!rows.length && status.phase !== "ready" && status.phase !== "error") {
    return html`<div class="dash-loading"><p>Preparando el panel…</p></div>`;
  }

  if (!rows.length) {
    return html`
      <div class="dash-loading">
        <p>Todavía no hay publicaciones guardadas.</p>
        <p class="hint">
          El panel se llena solo: cada publicación que el monitor examina queda registrada con su historial.
          Deja corriendo una búsqueda y vuelve aquí.
        </p>
      </div>
    `;
  }

  return html`
    <${FilterBar} />
    <nav class="dash-tabs" role="tablist">
      ${SECTIONS.map(
        (entry) => html`<button
          key=${entry.id}
          role="tab"
          aria-selected=${section === entry.id}
          class=${section === entry.id ? "active" : ""}
          onClick=${() => setSection(entry.id)}
        >
          ${entry.label}
        </button>`,
      )}
    </nav>
    <div class=${`dash-content${refreshing ? " refreshing" : ""}`}>
      ${section === "resumen" ? html`<${Overview} />` : null}
      ${section === "graficos" ? html`<${Charts} />` : null}
      ${section === "comunas" ? html`<${Geography} />` : null}
      ${section === "vendedores" ? html`<${Sellers} />` : null}
      ${section === "gangas" ? html`<${Bargains} onOpen=${setDetail} />` : null}
      ${section === "listado" ? html`<${Listings} onOpen=${setDetail} />` : null}
    </div>
    ${detail ? html`<${ListingDetail} listing=${detail} onClose=${() => setDetail(null)} />` : null}
  `;
}

export function App() {
  return html`
    <${DataProvider}>
      <div class="dashboard">
        <header class="dash-head">
          <h1>Panel de análisis</h1>
          <${SyncStatus} />
        </header>
        <${Sections} />
      </div>
    <//>
  `;
}
