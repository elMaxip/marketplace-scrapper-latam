// Section 5: listings that look underpriced.
//
// "Cheap" only means something against a reference, so each hit says what it was
// compared with (the same product where there are enough peers, the search item
// otherwise) and why it fired.  The threshold is a control, not a constant --
// what counts as a bargain depends on the category.

import { html, useMemo, useState } from "../html.js";
import { compact, formatStamp, money } from "../charts/primitives.js";
import { useData } from "../state/DataContext.js";
import { bargains, currencyInfo } from "../state/analytics.js";
import { downloadCsv } from "../lib/export.js";
import { VirtualTable } from "./VirtualTable.js";
import { Pill, SectionHead } from "./ui.js";

const REASON_LABELS = {
  below_median: { text: "bajo la mediana", tone: "ok" },
  outlier: { text: "atípico bajo", tone: "warn" },
  own_low: { text: "mínimo histórico", tone: "info" },
};

const EXPORT_COLUMNS = [
  { label: "titulo", value: (hit) => hit.row.title },
  { label: "precio", value: (hit) => hit.row.price_value },
  { label: "referencia", value: (hit) => Math.round(hit.reference) },
  { label: "descuento_pct", value: (hit) => Math.round(hit.gap * 100) },
  { label: "motivos", value: (hit) => hit.reasons.map((reason) => reason.code).join(" | ") },
  { label: "comparado_con", value: (hit) => (hit.scope === "product" ? "mismo producto" : "mismo tipo") },
  { label: "vendedor", value: (hit) => hit.row.seller },
  { label: "comuna", value: (hit) => hit.row.comuna },
  { label: "url", value: (hit) => hit.row.post_url },
];

export function Bargains({ onOpen }) {
  const { filtered } = useData();
  const [threshold, setThreshold] = useState(25);
  const [minGroup, setMinGroup] = useState(4);

  const unit = useMemo(() => currencyInfo(filtered).primary, [filtered]);
  const hits = useMemo(
    () => bargains(filtered, { discount: threshold / 100, minGroup, limit: 200 }),
    [filtered, threshold, minGroup],
  );

  const columns = [
    {
      key: "title",
      label: "Publicación",
      width: "minmax(200px, 2.5fr)",
      sortValue: (hit) => hit.row.title,
      render: (hit) => html`<span class="cell-main" title=${hit.row.title}>${hit.row.title}</span>`,
    },
    {
      key: "price",
      label: "Precio",
      numeric: true,
      width: "120px",
      sortValue: (hit) => hit.row.price_value,
      render: (hit) => money(hit.row.price_value, unit),
    },
    {
      key: "reference",
      label: "Referencia",
      numeric: true,
      width: "120px",
      sortValue: (hit) => hit.reference,
      render: (hit) => money(hit.reference, unit),
    },
    {
      key: "gap",
      label: "Diferencia",
      numeric: true,
      width: "100px",
      sortValue: (hit) => hit.gap,
      render: (hit) => html`<b class="good">${`-${Math.round(hit.gap * 100)}%`}</b>`,
    },
    {
      key: "reasons",
      label: "Motivo",
      width: "minmax(180px, 1.4fr)",
      render: (hit) =>
        html`<span class="cell-pills">
          ${hit.reasons.map((reason) => {
            const meta = REASON_LABELS[reason.code];
            return html`<${Pill} key=${reason.code} tone=${meta.tone}>${meta.text}<//>`;
          })}
        </span>`,
    },
    {
      key: "scope",
      label: "Comparado con",
      width: "150px",
      render: (hit) =>
        html`<span class="cell-dim">
          ${hit.scope === "product" ? `mismo producto (${hit.peers})` : "mismo tipo de producto"}
        </span>`,
    },
    {
      key: "seller",
      label: "Vendedor",
      width: "minmax(120px, 1fr)",
      sortValue: (hit) => hit.row.seller,
      render: (hit) => html`<span class="cell-dim">${hit.row.seller || "—"}</span>`,
    },
    {
      key: "seen",
      label: "Detectada",
      width: "150px",
      sortValue: (hit) => hit.row.first_seen_ms,
      render: (hit) => formatStamp(hit.row.first_seen_ms),
    },
  ];

  return html`
    <section class="panel-section">
      <${SectionHead}
        title="Productos interesantes"
        hint="Se compara contra publicaciones del mismo producto cuando hay suficientes; si no, contra el mismo tipo de producto."
      >
        <button class="ghost small" onClick=${() => downloadCsv("gangas", EXPORT_COLUMNS, hits)}>CSV</button>
      <//>

      <div class="control-row">
        <label class="fld narrow">
          <span>Ganga desde</span>
          <div class="range-field">
            <input
              type="range"
              min="5"
              max="80"
              step="5"
              value=${threshold}
              onInput=${(event) => setThreshold(Number(event.target.value))}
            />
            <b>${`${threshold}%`}</b>
          </div>
        </label>
        <label class="fld narrow">
          <span>Mínimo de comparables</span>
          <input
            type="number"
            min="2"
            max="50"
            value=${minGroup}
            onChange=${(event) => setMinGroup(Math.max(2, Number(event.target.value) || 2))}
          />
        </label>
        <span class="control-summary">${`${compact(hits.length)} oportunidades`}</span>
      </div>

      <${VirtualTable}
        columns=${columns}
        rows=${hits}
        rowKey=${(hit) => hit.row.key}
        height=${420}
        initialSort=${{ key: "gap", dir: "desc" }}
        onRowClick=${(hit) => onOpen(hit.row)}
        empty="Nada por debajo del umbral en esta selección"
      />
    </section>
  `;
}
