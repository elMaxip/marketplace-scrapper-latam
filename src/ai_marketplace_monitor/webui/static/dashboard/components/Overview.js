// Section 1: the headline numbers for the current selection.
//
// These are stat tiles rather than charts on purpose -- a single number is the
// right form for "how many listings" and "what is the median price", and a
// one-bar chart would say less in more space.

import { html, useMemo } from "../html.js";
import { compact, money, number } from "../charts/primitives.js";
import { useData } from "../state/DataContext.js";
import { summary } from "../state/analytics.js";
import { Note } from "./ui.js";

function Tile({ label, value, sub = null, hero = false }) {
  return html`
    <div class=${`stat-tile${hero ? " hero" : ""}`}>
      <span class="stat-label">${label}</span>
      <span class="stat-value">${value}</span>
      ${sub ? html`<span class="stat-sub">${sub}</span>` : null}
    </div>
  `;
}

export function Overview() {
  const { filtered } = useData();
  const stats = useMemo(() => summary(filtered), [filtered]);
  const unit = stats.currency.primary;

  return html`
    <section class="panel-section">
      <div class="stat-grid">
        <${Tile}
          hero
          label="Publicaciones"
          value=${stats.total.toLocaleString()}
          sub=${`${stats.active.toLocaleString()} vistas recientemente`}
        />
        <${Tile} label="Precio promedio" value=${money(stats.mean, unit)} sub=${`${compact(stats.priced)} con precio`} />
        <${Tile} label="Precio mediano" value=${money(stats.median, unit)} sub=${`P25 ${money(stats.p25, unit)} · P75 ${money(stats.p75, unit)}`} />
        <${Tile} label="Precio mínimo" value=${money(stats.min, unit)} />
        <${Tile} label="Precio máximo" value=${money(stats.max, unit)} />
        <${Tile} label="Vendedores" value=${number(stats.sellers)} />
        <${Tile} label="Ciudades" value=${number(stats.cities)} sub=${`${number(stats.comunas)} comunas`} />
        <${Tile} label="Hoy" value=${number(stats.today)} sub=${`${number(stats.lastHour)} en la última hora`} />
      </div>

      ${stats.currency.mixed
        ? html`<${Note} tone="warn">
            La selección mezcla ${stats.currency.breakdown.length} monedas
            (${stats.currency.breakdown.map((entry) => `${entry.code}: ${compact(entry.count)}`).join(" · ")}).
            Los promedios y las comparaciones de precio no son válidos hasta filtrar por una sola.
          <//>`
        : null}
      ${stats.unpriced
        ? html`<${Note} tone="info">
            ${`${compact(stats.unpriced)} publicaciones sin precio legible quedan fuera de las estadísticas de precio.`}
          <//>`
        : null}
    </section>
  `;
}
