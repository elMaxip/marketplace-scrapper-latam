// Section 6: the corpus itself, either grouped by product or listing by listing.
//
// Grouping uses `product_key` -- a sorted bag of title words -- so repostings of
// the same item collapse together and the "best price found" is a comparison
// between genuinely comparable listings rather than across a whole category.

import { html, useMemo, useState } from "../html.js";
import { compact, formatStamp, money } from "../charts/primitives.js";
import { useData } from "../state/DataContext.js";
import { currencyInfo, productGroups } from "../state/analytics.js";
import { statusOf } from "../state/filters.js";
import { LISTING_COLUMNS, downloadCsv, downloadJson } from "../lib/export.js";
import { VirtualTable } from "./VirtualTable.js";
import { Pill, SectionHead } from "./ui.js";

const GROUP_EXPORT_COLUMNS = [
  { label: "producto", value: (row) => row.label },
  { label: "publicaciones", value: (row) => row.count },
  { label: "vendedores", value: (row) => row.sellers },
  { label: "mejor_precio", value: (row) => (row.cheapest ? row.cheapest.price_value : "") },
  { label: "precio_promedio", value: (row) => row.mean },
  { label: "precio_maximo", value: (row) => row.max },
  { label: "ultima_deteccion", value: (row) => new Date(row.lastSeen).toISOString() },
];

/** Side-by-side comparison of every listing behind one product group. */
function GroupCompare({ group, unit, onOpen, onClose }) {
  const sorted = useMemo(
    () =>
      [...group.rows].sort((a, b) => {
        if (a.price_value === null) return 1;
        if (b.price_value === null) return -1;
        return a.price_value - b.price_value;
      }),
    [group],
  );
  const best = sorted.find((row) => row.price_value !== null) || null;

  return html`
    <div class="compare-panel">
      <header class="compare-head">
        <div>
          <h3>${group.label}</h3>
          <p class="hint">
            ${`${compact(group.count)} publicaciones · ${compact(group.sellers)} vendedores`}
            ${best ? ` · mejor precio ${money(best.price_value, unit)}` : ""}
          </p>
        </div>
        <button class="ghost small" onClick=${onClose}>Cerrar</button>
      </header>
      <div class="compare-grid">
        ${sorted.map(
          (row) => html`<article
            key=${row.key}
            class=${`compare-card${best && row.key === best.key ? " best" : ""}`}
            onClick=${() => onOpen(row)}
          >
            <div class="compare-price">
              ${money(row.price_value, unit)}
              ${best && row.key === best.key ? html`<${Pill} tone="ok">mejor<//>` : null}
            </div>
            <div class="compare-title" title=${row.title}>${row.title}</div>
            <dl class="compare-meta">
              <div><dt>Vendedor</dt><dd>${row.seller || "—"}</dd></div>
              <div><dt>Comuna</dt><dd>${row.comuna || "—"}</dd></div>
              <div><dt>Estado</dt><dd>${row.condition || "—"}</dd></div>
              <div><dt>Detectada</dt><dd>${formatStamp(row.first_seen_ms)}</dd></div>
            </dl>
          </article>`,
        )}
      </div>
    </div>
  `;
}

export function Listings({ onOpen }) {
  const { filtered } = useData();
  const [mode, setMode] = useState("groups");
  const [openGroup, setOpenGroup] = useState(null);

  const unit = useMemo(() => currencyInfo(filtered).primary, [filtered]);
  const groups = useMemo(() => productGroups(filtered), [filtered]);

  // Keep the open comparison in sync when the filters change underneath it.
  const activeGroup = useMemo(
    () => (openGroup ? groups.find((group) => group.key === openGroup) || null : null),
    [groups, openGroup],
  );

  const groupColumns = [
    {
      key: "product",
      label: "Producto",
      width: "minmax(220px, 3fr)",
      sortValue: (row) => row.label,
      render: (row) => html`<span class="cell-main" title=${row.label}>${row.label}</span>`,
    },
    { key: "count", label: "Publica.", numeric: true, width: "90px", sortValue: (row) => row.count, render: (row) => compact(row.count) },
    { key: "sellers", label: "Vendedores", numeric: true, width: "100px", sortValue: (row) => row.sellers, render: (row) => compact(row.sellers) },
    {
      key: "best",
      label: "Mejor precio",
      numeric: true,
      width: "120px",
      sortValue: (row) => (row.cheapest ? row.cheapest.price_value : null),
      render: (row) => html`<b>${row.cheapest ? money(row.cheapest.price_value, unit) : "—"}</b>`,
    },
    { key: "mean", label: "Promedio", numeric: true, width: "120px", sortValue: (row) => row.mean, render: (row) => money(row.mean, unit) },
    { key: "max", label: "Máximo", numeric: true, width: "120px", sortValue: (row) => row.max, render: (row) => money(row.max, unit) },
    {
      key: "last",
      label: "Última vez",
      width: "150px",
      sortValue: (row) => row.lastSeen,
      render: (row) => formatStamp(row.lastSeen),
    },
  ];

  const listingColumns = [
    {
      key: "title",
      label: "Título",
      width: "minmax(220px, 3fr)",
      sortValue: (row) => row.title,
      render: (row) => html`<span class="cell-main" title=${row.title}>${row.title}</span>`,
    },
    {
      key: "price",
      label: "Precio",
      numeric: true,
      width: "120px",
      sortValue: (row) => row.price_value,
      render: (row) => money(row.price_value, unit),
    },
    { key: "item", label: "Tipo", width: "120px", sortValue: (row) => row.item, render: (row) => row.item || "—" },
    { key: "condition", label: "Categoría", width: "110px", sortValue: (row) => row.condition, render: (row) => row.condition || "—" },
    { key: "comuna", label: "Comuna", width: "130px", sortValue: (row) => row.comuna, render: (row) => row.comuna || "—" },
    { key: "city", label: "Ciudad", width: "130px", sortValue: (row) => row.city, render: (row) => row.city || "—" },
    {
      key: "seller",
      label: "Vendedor",
      width: "minmax(120px, 1fr)",
      sortValue: (row) => row.seller,
      render: (row) => html`<span class="cell-dim">${row.seller || "—"}</span>`,
    },
    {
      key: "status",
      label: "Estado",
      width: "110px",
      sortValue: (row) => row.last_seen_ms,
      render: (row) =>
        statusOf(row) === "active"
          ? html`<${Pill} tone="ok">activa<//>`
          : html`<${Pill} tone="dim" title="Dejó de aparecer en las búsquedas">sin ver<//>`,
    },
    {
      key: "first",
      label: "Detectada",
      width: "150px",
      sortValue: (row) => row.first_seen_ms,
      render: (row) => formatStamp(row.first_seen_ms),
    },
    {
      key: "changes",
      label: "Cambios",
      numeric: true,
      width: "90px",
      sortValue: (row) => row.price_changes,
      render: (row) => (row.price_changes ? compact(row.price_changes) : "—"),
    },
    {
      key: "score",
      label: "IA",
      numeric: true,
      width: "70px",
      sortValue: (row) => row.score,
      render: (row) => (row.score === null ? "—" : row.score),
    },
  ];

  return html`
    <section class="panel-section">
      <${SectionHead} title="Listado de productos" hint="Clic en una fila para ver el detalle y el historial.">
        <button
          class="ghost small"
          onClick=${() =>
            mode === "groups"
              ? downloadCsv("productos", GROUP_EXPORT_COLUMNS, groups)
              : downloadCsv("publicaciones", LISTING_COLUMNS, filtered)}
        >
          CSV
        </button>
        <button
          class="ghost small"
          onClick=${() =>
            mode === "groups"
              ? downloadJson("productos", groups.map(({ rows: _rows, cheapest, ...rest }) => ({ ...rest, mejor_precio: cheapest ? cheapest.price_value : null })))
              : downloadJson("publicaciones", filtered)}
        >
          JSON
        </button>
      <//>

      <div class="seg wide-seg" role="group" aria-label="Agrupación">
        <button class=${mode === "groups" ? "active" : ""} aria-pressed=${mode === "groups"} onClick=${() => setMode("groups")}>
          ${`Productos agrupados (${compact(groups.length)})`}
        </button>
        <button class=${mode === "flat" ? "active" : ""} aria-pressed=${mode === "flat"} onClick=${() => setMode("flat")}>
          ${`Todas las publicaciones (${compact(filtered.length)})`}
        </button>
      </div>

      ${mode === "groups"
        ? html`<${VirtualTable}
            columns=${groupColumns}
            rows=${groups}
            rowKey=${(row) => row.key}
            height=${420}
            initialSort=${{ key: "count", dir: "desc" }}
            onRowClick=${(row) => setOpenGroup(row.key)}
            empty="Sin productos en esta selección"
          />`
        : html`<${VirtualTable}
            columns=${listingColumns}
            rows=${filtered}
            rowKey=${(row) => row.key}
            height=${520}
            initialSort=${{ key: "first", dir: "desc" }}
            onRowClick=${onOpen}
            empty="Sin publicaciones en esta selección"
          />`}

      ${activeGroup
        ? html`<${GroupCompare} group=${activeGroup} unit=${unit} onOpen=${onOpen} onClose=${() => setOpenGroup(null)} />`
        : null}
    </section>
  `;
}
