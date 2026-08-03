// Section 7: one listing in full, with its change timeline.
//
// The projection kept in memory omits the heavy fields, so the drawer reads the
// complete record back from IndexedDB by key when it opens -- descriptions and
// history are paid for only when someone actually looks at them.

import { html, useEffect, useState } from "../html.js";
import { formatStamp, money } from "../charts/primitives.js";
import { loadDetail } from "../db/sync.js";
import { useData } from "../state/DataContext.js";
import { statusOf } from "../state/filters.js";
import { Pill } from "./ui.js";

const FIELD_LABELS = {
  price: "Precio",
  title: "Título",
  description: "Descripción",
  location: "Ubicación",
  seller: "Vendedor",
  condition: "Categoría",
  image: "Imagen",
};

/** Condense long values so a description rewrite does not flood the timeline. */
function short(value, limit = 90) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  if (!text) return "—";
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

function Change({ field, change }) {
  const label = FIELD_LABELS[field] || field;
  if (field === "image") {
    return html`<li><b>${label}</b> <span class="cell-dim">actualizada</span></li>`;
  }
  return html`<li>
    <b>${label}</b>
    <span class="from">${short(change.from)}</span>
    <span class="arrow">→</span>
    <span class="to">${short(change.to)}</span>
  </li>`;
}

/** Newest first: the most recent change is what someone opening this wants. */
function Timeline({ record }) {
  const entries = [...(record.history || [])].reverse();
  return html`
    <ol class="timeline">
      ${entries.map(
        (entry, index) => html`<li key=${index} class="timeline-entry">
          <time>${formatStamp(Date.parse(entry.ts))}</time>
          <ul class="timeline-changes">
            ${Object.entries(entry.changes).map(
              ([field, change]) => html`<${Change} key=${field} field=${field} change=${change} />`,
            )}
          </ul>
        </li>`,
      )}
      <li class="timeline-entry origin">
        <time>${formatStamp(Date.parse(record.first_seen))}</time>
        <p>Primera vez detectada${record.price ? ` a ${record.price}` : ""}.</p>
      </li>
    </ol>
  `;
}

function PriceSeries({ points }) {
  if (!points || points.length < 2) return null;
  return html`
    <ul class="price-series">
      ${points.map(
        (point, index) => html`<li key=${index}>
          <time>${formatStamp(Date.parse(point.ts))}</time>
          <b>${point.price}</b>
        </li>`,
      )}
    </ul>
  `;
}

export function ListingDetail({ listing, onClose }) {
  const { db, setFilter } = useData();
  const [record, setRecord] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setRecord(null);
    setError(null);
    if (!db || !listing) return undefined;
    loadDetail(db, listing.key)
      .then((value) => {
        if (!cancelled) setRecord(value || null);
      })
      .catch((err) => {
        if (!cancelled) setError(String((err && err.message) || err));
      });
    return () => {
      cancelled = true;
    };
  }, [db, listing]);

  // Escape closes the drawer, matching the modals the rest of the UI uses.
  useEffect(() => {
    const onKey = (event) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (!listing) return null;
  const unit = listing.currency;

  return html`
    <div class="drawer-backdrop" onClick=${onClose}>
      <aside
        class="drawer"
        role="dialog"
        aria-modal="true"
        aria-label=${listing.title}
        onClick=${(event) => event.stopPropagation()}
      >
        <header class="drawer-head">
          <div>
            <h3>${listing.title}</h3>
            <p class="hint">
              ${money(listing.price_value, unit)}
              ${listing.price_min_value !== null && listing.price_min_value < listing.price_value
                ? html` · mínimo histórico ${money(listing.price_min_value, unit)}`
                : null}
            </p>
          </div>
          <button class="ghost small" onClick=${onClose} aria-label="Cerrar">×</button>
        </header>

        <div class="drawer-body">
          <div class="drawer-badges">
            ${statusOf(listing) === "active" ? html`<${Pill} tone="ok">activa<//>` : html`<${Pill} tone="dim">sin ver<//>`}
            ${listing.matched ? null : html`<${Pill} tone="dim" title="Descartada por los filtros del monitor">descartada<//>`}
            ${listing.notified_count ? html`<${Pill} tone="info">notificada<//>` : null}
            ${listing.score !== null ? html`<${Pill} tone="info">${`IA ${listing.score}`}<//>` : null}
          </div>

          <dl class="detail-grid">
            <div><dt>Vendedor</dt><dd>
              <button class="linklike" onClick=${() => { setFilter({ seller: listing.seller_key }); onClose(); }}>
                ${listing.seller || "—"}
              </button>
            </dd></div>
            <div><dt>Comuna</dt><dd>${listing.comuna || "—"}</dd></div>
            <div><dt>Ciudad</dt><dd>${listing.city || "—"}</dd></div>
            <div><dt>Categoría</dt><dd>${listing.condition || "—"}</dd></div>
            <div><dt>Tipo de producto</dt><dd>${listing.item || "—"}</dd></div>
            <div><dt>Primera detección</dt><dd>${formatStamp(listing.first_seen_ms)}</dd></div>
            <div><dt>Última detección</dt><dd>${formatStamp(listing.last_seen_ms)}</dd></div>
            <div><dt>Veces vista</dt><dd>${listing.seen_count}</dd></div>
          </dl>

          ${listing.post_url
            ? html`<p><a class="linklike" href=${listing.post_url} target="_blank" rel="noopener noreferrer">Abrir en el marketplace ↗</a></p>`
            : null}

          ${error ? html`<p class="note warn">${`No se pudo leer el detalle local: ${error}`}</p>` : null}
          ${!record && !error ? html`<p class="chart-empty">Cargando detalle…</p>` : null}

          ${record
            ? html`
                ${record.rating
                  ? html`<section class="drawer-section">
                      <h4>Evaluación IA</h4>
                      <p class="ai-comment">
                        <b>${`${record.rating.conclusion || "—"} (${record.rating.score})`}</b>
                        ${record.rating.comment ? html` — ${record.rating.comment}` : null}
                      </p>
                    </section>`
                  : null}

                ${record.description
                  ? html`<section class="drawer-section">
                      <h4>Descripción</h4>
                      <p class="description">${record.description}</p>
                    </section>`
                  : null}

                <section class="drawer-section">
                  <h4>Historial de precio</h4>
                  ${record.price_points && record.price_points.length > 1
                    ? html`<${PriceSeries} points=${record.price_points} />`
                    : html`<p class="chart-empty">Sin cambios de precio registrados.</p>`}
                </section>

                <section class="drawer-section">
                  <h4>Línea de tiempo</h4>
                  <${Timeline} record=${record} />
                </section>

                ${Object.keys(record.notified || {}).length
                  ? html`<section class="drawer-section">
                      <h4>Notificaciones</h4>
                      <ul class="plain-list">
                        ${Object.entries(record.notified).map(
                          ([user, ts]) => html`<li key=${user}>${user} · ${formatStamp(Date.parse(ts))}</li>`,
                        )}
                      </ul>
                    </section>`
                  : null}
              `
            : null}
        </div>
      </aside>
    </div>
  `;
}
