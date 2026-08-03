// The one filter row that scopes the whole panel.
//
// Per-section filters are deliberately absent: every tile, chart and table
// downstream reads the same filtered slice, so what you see is always one
// consistent selection rather than several that disagree.

import { html } from "../html.js";
import { useData } from "../state/DataContext.js";
import { activeFilterCount } from "../state/filters.js";
import { DateField, NumberField, SearchField, Select, Toggle } from "./ui.js";

export function FilterBar() {
  const { filters, setFilter, resetFilters, options, filtered, rows } = useData();
  const active = activeFilterCount(filters);

  return html`
    <div class="filter-bar">
      <div class="filter-row">
        <${SearchField}
          label="Buscar"
          value=${filters.search}
          placeholder="título, vendedor o ubicación…"
          onChange=${(value) => setFilter({ search: value })}
        />
        <${Select}
          label="Tipo de producto"
          value=${filters.item}
          options=${options.items}
          onChange=${(value) => setFilter({ item: value })}
        />
        <${Select}
          label="Categoría"
          value=${filters.condition}
          options=${options.conditions}
          onChange=${(value) => setFilter({ condition: value })}
        />
        <${Select}
          label="Ciudad"
          value=${filters.city}
          options=${options.cities}
          onChange=${(value) => setFilter({ city: value })}
        />
        <${Select}
          label="Comuna"
          value=${filters.comuna}
          options=${options.comunas}
          onChange=${(value) => setFilter({ comuna: value })}
        />
        <${Select}
          label="Vendedor"
          value=${filters.seller}
          options=${options.sellers}
          onChange=${(value) => setFilter({ seller: value })}
        />
      </div>
      <div class="filter-row">
        <${NumberField}
          label="Precio mín."
          value=${filters.minPrice}
          onChange=${(value) => setFilter({ minPrice: value })}
        />
        <${NumberField}
          label="Precio máx."
          value=${filters.maxPrice}
          onChange=${(value) => setFilter({ maxPrice: value })}
        />
        <${DateField} label="Desde" value=${filters.from} onChange=${(value) => setFilter({ from: value })} />
        <${DateField} label="Hasta" value=${filters.to} onChange=${(value) => setFilter({ to: value })} />
        <${Select}
          label="Estado"
          value=${filters.status}
          options=${[
            { value: "active", label: "Activa" },
            { value: "inactive", label: "Sin ver recientemente" },
          ]}
          onChange=${(value) => setFilter({ status: value })}
        />
        <${Select}
          label="Puntaje IA"
          value=${filters.minScore === null ? "" : String(filters.minScore)}
          options=${[
            { value: "3", label: "≥ 3" },
            { value: "4", label: "≥ 4" },
            { value: "5", label: "= 5" },
          ]}
          onChange=${(value) => setFilter({ minScore: value === "" ? null : Number(value) })}
        />
        <div class="filter-toggles">
          <${Toggle}
            label="Sólo coincidencias"
            checked=${filters.matchedOnly}
            title="Ocultar publicaciones descartadas por los filtros del monitor"
            onChange=${(value) => setFilter({ matchedOnly: value })}
          />
          <${Toggle}
            label="Sólo notificadas"
            checked=${filters.notifiedOnly}
            onChange=${(value) => setFilter({ notifiedOnly: value })}
          />
        </div>
        <div class="filter-summary">
          <span>
            <b>${filtered.length.toLocaleString()}</b> de ${rows.length.toLocaleString()}
          </span>
          <button class="ghost small" disabled=${active === 0} onClick=${resetFilters}>
            ${active ? `Limpiar (${active})` : "Limpiar"}
          </button>
        </div>
      </div>
    </div>
  `;
}
