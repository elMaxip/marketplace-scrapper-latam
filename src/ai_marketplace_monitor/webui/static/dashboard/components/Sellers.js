// Section 4: who is selling.
//
// The reseller flag is a heuristic, so the table shows the evidence next to the
// verdict -- volume, cadence and how often the same product is relisted -- and
// clicking a seller scopes the whole panel to them rather than opening a
// separate, disconnected view.

import { html, useMemo, useState } from "../html.js";
import { compact, formatStamp, money, number } from "../charts/primitives.js";
import { useData } from "../state/DataContext.js";
import { currencyInfo, sellerProfiles } from "../state/analytics.js";
import { downloadCsv, downloadJson } from "../lib/export.js";
import { VirtualTable } from "./VirtualTable.js";
import { Pill, SectionHead } from "./ui.js";

const VIEWS = [
  { id: "volume", label: "Más publicaciones" },
  { id: "new", label: "Vendedores nuevos" },
  { id: "active", label: "Muy activos" },
  { id: "reseller", label: "Posibles revendedores" },
];

const EXPORT_COLUMNS = [
  { label: "vendedor", value: (row) => row.seller },
  { label: "publicaciones", value: (row) => row.count },
  { label: "activas", value: (row) => row.active },
  { label: "primera_publicacion", value: (row) => new Date(row.firstSeen).toISOString() },
  { label: "ultima_publicacion", value: (row) => new Date(row.lastSeen).toISOString() },
  { label: "precio_promedio", value: (row) => row.mean },
  { label: "publicaciones_por_dia", value: (row) => row.perDay.toFixed(2) },
  { label: "productos_distintos", value: (row) => row.distinctProducts },
  { label: "indice_reventa", value: (row) => row.resellerScore.toFixed(2) },
];

export function Sellers() {
  const { filtered, setFilter, filters } = useData();
  const [view, setView] = useState("volume");

  const unit = useMemo(() => currencyInfo(filtered).primary, [filtered]);
  const profiles = useMemo(() => sellerProfiles(filtered), [filtered]);

  const rows = useMemo(() => {
    switch (view) {
      case "new":
        return profiles.filter((profile) => profile.isNew).sort((a, b) => b.firstSeen - a.firstSeen);
      case "active":
        return [...profiles].sort((a, b) => b.perDay - a.perDay);
      case "reseller":
        return profiles.filter((profile) => profile.likelyReseller).sort((a, b) => b.resellerScore - a.resellerScore);
      default:
        return [...profiles].sort((a, b) => b.count - a.count);
    }
  }, [profiles, view]);

  const columns = [
    {
      key: "seller",
      label: "Vendedor",
      width: "minmax(160px, 2fr)",
      sortValue: (row) => row.seller,
      render: (row) => html`<span class="cell-main" title=${row.seller}>
        ${row.seller}
        ${row.likelyReseller ? html`<${Pill} tone="warn" title=${`Índice de reventa ${row.resellerScore.toFixed(2)}`}>rev<//>` : null}
        ${row.isNew ? html`<${Pill} tone="ok">nuevo<//>` : null}
      </span>`,
    },
    { key: "count", label: "Publica.", numeric: true, width: "90px", sortValue: (row) => row.count, render: (row) => compact(row.count) },
    { key: "active", label: "Activas", numeric: true, width: "90px", sortValue: (row) => row.active, render: (row) => compact(row.active) },
    {
      key: "first",
      label: "Primera",
      width: "150px",
      sortValue: (row) => row.firstSeen,
      render: (row) => formatStamp(row.firstSeen),
    },
    {
      key: "last",
      label: "Última",
      width: "150px",
      sortValue: (row) => row.lastSeen,
      render: (row) => formatStamp(row.lastSeen),
    },
    {
      key: "mean",
      label: "Precio prom.",
      numeric: true,
      width: "120px",
      sortValue: (row) => row.mean,
      render: (row) => money(row.mean, unit),
    },
    {
      key: "perDay",
      label: "Pub./día",
      numeric: true,
      width: "90px",
      sortValue: (row) => row.perDay,
      render: (row) => number(row.perDay),
    },
    {
      key: "repeat",
      label: "Repetición",
      numeric: true,
      width: "110px",
      sortValue: (row) => row.repeatRatio,
      render: (row) => `${Math.round(row.repeatRatio * 100)}%`,
    },
  ];

  return html`
    <section class="panel-section">
      <${SectionHead}
        title="Vendedores"
        hint="El índice de reventa combina volumen, frecuencia y cuánto se repite el mismo producto. Es una heurística, no un hecho."
      >
        <button class="ghost small" onClick=${() => downloadCsv("vendedores", EXPORT_COLUMNS, rows)}>CSV</button>
        <button class="ghost small" onClick=${() => downloadJson("vendedores", rows.map(({ rows: _rows, ...rest }) => rest))}>
          JSON
        </button>
      <//>

      <div class="seg wide-seg" role="group" aria-label="Filtro de vendedores">
        ${VIEWS.map(
          (entry) => html`<button
            key=${entry.id}
            class=${view === entry.id ? "active" : ""}
            aria-pressed=${view === entry.id}
            onClick=${() => setView(entry.id)}
          >
            ${entry.label}
          </button>`,
        )}
      </div>

      ${filters.seller
        ? html`<p class="note info">
            Panel limitado al vendedor seleccionado.
            <button class="linklike" onClick=${() => setFilter({ seller: "" })}>Quitar filtro</button>
          </p>`
        : null}

      <${VirtualTable}
        columns=${columns}
        rows=${rows}
        rowKey=${(row) => row.key}
        height=${420}
        initialSort=${{ key: "count", dir: "desc" }}
        onRowClick=${(row) => setFilter({ seller: row.key })}
        empty="Ningún vendedor cumple este criterio en la selección actual"
      />
    </section>
  `;
}
