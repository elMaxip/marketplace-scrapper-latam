// Section 3: geography, by comuna.
//
// This is not a map.  The scraper stores location as one free-text line, so a
// choropleth would need a gazetteer and boundary data to sit on top of a value
// that is often just "Santiago" -- the geometry would look authoritative while
// the underlying match was a guess.  A ranked table plus bars says exactly what
// the data supports: how much is listed where, at what price, and of what.
//
// Selecting a comuna scopes the entire panel to it, which is the drill-down the
// map was for.

import { html, useMemo } from "../html.js";
import { Bars } from "../charts/Bars.js";
import { ChartCard, compact, money, tipRow } from "../charts/primitives.js";
import { useData } from "../state/DataContext.js";
import { comunaProfiles, currencyInfo } from "../state/analytics.js";
import { downloadCsv } from "../lib/export.js";
import { VirtualTable } from "./VirtualTable.js";
import { SectionHead } from "./ui.js";

const EXPORT_COLUMNS = [
  { label: "comuna", value: (row) => row.label },
  { label: "publicaciones", value: (row) => row.count },
  { label: "activas", value: (row) => row.active },
  { label: "vendedores", value: (row) => row.sellers },
  { label: "precio_promedio", value: (row) => row.mean },
  { label: "precio_mediano", value: (row) => row.median },
  { label: "categorias_top", value: (row) => row.categories.map((entry) => entry.label).join(" | ") },
];

export function Geography() {
  const { filtered, filters, setFilter } = useData();

  const unit = useMemo(() => currencyInfo(filtered).primary, [filtered]);
  const profiles = useMemo(() => comunaProfiles(filtered), [filtered]);
  const topByVolume = useMemo(() => profiles.slice(0, 12), [profiles]);

  const columns = [
    {
      key: "comuna",
      label: "Comuna",
      width: "minmax(140px, 1.4fr)",
      sortValue: (row) => row.label,
      render: (row) => html`<span class="cell-main" title=${row.label}>${row.label}</span>`,
    },
    { key: "count", label: "Publica.", numeric: true, width: "90px", sortValue: (row) => row.count, render: (row) => compact(row.count) },
    { key: "active", label: "Activas", numeric: true, width: "90px", sortValue: (row) => row.active, render: (row) => compact(row.active) },
    { key: "sellers", label: "Vendedores", numeric: true, width: "100px", sortValue: (row) => row.sellers, render: (row) => compact(row.sellers) },
    {
      key: "mean",
      label: "Precio prom.",
      numeric: true,
      width: "120px",
      sortValue: (row) => row.mean,
      render: (row) => money(row.mean, unit),
    },
    {
      key: "median",
      label: "Mediana",
      numeric: true,
      width: "120px",
      sortValue: (row) => row.median,
      render: (row) => money(row.median, unit),
    },
    {
      key: "categories",
      label: "Categorías más frecuentes",
      width: "minmax(180px, 2fr)",
      render: (row) =>
        html`<span class="cell-dim">
          ${row.categories.length ? row.categories.map((entry) => `${entry.label} (${entry.count})`).join(", ") : "—"}
        </span>`,
    },
  ];

  const tooltip = (entry) => html`<div>
    <div class="tip-title">${entry.label}</div>
    ${tipRow("Publicaciones", compact(entry.group.count))}
    ${tipRow("Precio promedio", money(entry.group.mean, unit))}
    ${tipRow("Vendedores", compact(entry.group.sellers))}
  </div>`;

  return html`
    <section class="panel-section">
      <${SectionHead}
        title="Comunas"
        hint="La comuna se deriva del texto libre de ubicación que entrega el marketplace, así que puede quedar vacía o imprecisa en algunas publicaciones."
      >
        <button class="ghost small" onClick=${() => downloadCsv("comunas", EXPORT_COLUMNS, profiles)}>CSV</button>
      <//>

      ${filters.comuna
        ? html`<p class="note info">
            Panel limitado a una comuna.
            <button class="linklike" onClick=${() => setFilter({ comuna: "" })}>Quitar filtro</button>
          </p>`
        : null}

      <div class="chart-grid">
        <${ChartCard}
          title="Publicaciones por comuna"
          subtitle="Las 12 con más volumen · clic para filtrar"
          table=${null}
        >
          <${Bars}
            data=${topByVolume.map((group) => ({ key: group.key, label: group.label, value: group.count, group }))}
            tooltip=${tooltip}
            onSelect=${(entry) => setFilter({ comuna: entry.key })}
            emphasisKey=${filters.comuna || null}
          />
        <//>
        <${ChartCard} title="Precio promedio por comuna" subtitle="Mismas comunas, mismo orden" table=${null}>
          <${Bars}
            data=${topByVolume.map((group) => ({ key: group.key, label: group.label, value: group.mean || 0, group }))}
            format=${(value) => money(value, unit)}
            tooltip=${tooltip}
            onSelect=${(entry) => setFilter({ comuna: entry.key })}
            emphasisKey=${filters.comuna || null}
          />
        <//>
      </div>

      <${VirtualTable}
        columns=${columns}
        rows=${profiles}
        rowKey=${(row) => row.key}
        height=${360}
        initialSort=${{ key: "count", dir: "desc" }}
        onRowClick=${(row) => setFilter({ comuna: row.key })}
        empty="Ninguna publicación de la selección trae ubicación legible"
      />
    </section>
  `;
}
