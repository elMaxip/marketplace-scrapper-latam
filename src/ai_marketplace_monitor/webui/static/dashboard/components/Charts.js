// Section 2: the charts.
//
// Each one is a single measure over a single series, so they all wear
// categorical slot 1 -- colour here would only restate the bar length.  Every
// card carries a table twin, and the heavier ones only mount once they are near
// the viewport.

import { html, useMemo } from "../html.js";
import { Bars } from "../charts/Bars.js";
import { BoxPlot } from "../charts/BoxPlot.js";
import { Histogram } from "../charts/Histogram.js";
import { TimeSeries } from "../charts/TimeSeries.js";
import {
  ChartCard,
  DataTable,
  Lazy,
  compact,
  formatDay,
  formatHour,
  money,
  tipRow,
} from "../charts/primitives.js";
import { useData } from "../state/DataContext.js";
import { byCity, byComuna, byItem, boxesBy, currencyInfo } from "../state/analytics.js";
import { histogram, timeSeries, values } from "../state/stats.js";
import { statusOf } from "../state/filters.js";

const TOP_N = 12;

/** Shared table twin for the "grouped by something" charts. */
function groupTable(groups, unit) {
  return html`<${DataTable}
    columns=${[
      { key: "label", label: "Grupo", render: (row) => row.label },
      { key: "count", label: "Publicaciones", numeric: true, render: (row) => compact(row.count) },
      { key: "mean", label: "Precio promedio", numeric: true, render: (row) => money(row.mean, unit) },
      { key: "median", label: "Mediana", numeric: true, render: (row) => money(row.median, unit) },
      { key: "min", label: "Mín.", numeric: true, render: (row) => money(row.min, unit) },
      { key: "max", label: "Máx.", numeric: true, render: (row) => money(row.max, unit) },
    ]}
    rows=${groups}
  />`;
}

function seriesTable(series, unit, { valueLabel, pick, labelFor }) {
  return html`<${DataTable}
    columns=${[
      { key: "ms", label: "Periodo", render: (row) => labelFor(row.ms) },
      { key: "count", label: "Publicaciones", numeric: true, render: (row) => compact(row.count) },
      { key: "value", label: valueLabel, numeric: true, render: (row) => money(pick(row), unit) },
    ]}
    rows=${series.map((point) => ({ ...point, key: point.ms }))}
  />`;
}

export function Charts() {
  const { filtered } = useData();

  const unit = useMemo(() => currencyInfo(filtered).primary, [filtered]);
  const prices = useMemo(() => values(filtered), [filtered]);
  const hist = useMemo(() => histogram(prices), [prices]);

  const items = useMemo(() => byItem(filtered, { limit: TOP_N }), [filtered]);
  const cities = useMemo(() => byCity(filtered, { limit: TOP_N, minCount: 2 }), [filtered]);
  const comunas = useMemo(() => byComuna(filtered, { limit: TOP_N, minCount: 2 }), [filtered]);
  const boxes = useMemo(() => boxesBy(filtered, (row) => row.item, { minCount: 5, limit: 10 }), [filtered]);

  const daily = useMemo(() => timeSeries(filtered, { unit: "day", limit: 60 }), [filtered]);
  const hourly = useMemo(() => timeSeries(filtered, { unit: "hour", limit: 48 }), [filtered]);

  // "Active" is defined by recency of last sighting, so it is a property of the
  // corpus now, not of the day a listing appeared -- a per-day line would be a
  // different (and misleading) statement.
  const activeByItem = useMemo(() => {
    const now = Date.now();
    return byItem(filtered, { limit: TOP_N }).map((group) => ({
      ...group,
      active: group.rows.filter((row) => statusOf(row, now) === "active").length,
    }));
  }, [filtered]);

  const priceTip = (entry) => html`<div>
    <div class="tip-title">${entry.label}</div>
    ${tipRow("Publicaciones", compact(entry.group.count))}
    ${tipRow("Promedio", money(entry.group.mean, unit))}
    ${tipRow("Mediana", money(entry.group.median, unit))}
  </div>`;

  const toBars = (groups, pick) =>
    groups.map((group) => ({ key: group.key, label: group.label, value: pick(group), group }));

  return html`
    <section class="panel-section">
      <div class="chart-grid">
        <${ChartCard}
          wide
          title="Distribución de precios"
          subtitle=${`${compact(prices.length)} publicaciones con precio${unit ? ` · ${unit}` : ""}`}
          table=${html`<${DataTable}
            columns=${[
              { key: "range", label: "Rango", render: (row) => `${money(row.from, unit)} – ${money(row.to, unit)}` },
              { key: "count", label: "Publicaciones", numeric: true, render: (row) => compact(row.count) },
            ]}
            rows=${hist.bins.map((bin, index) => ({ ...bin, key: index }))}
          />`}
        >
          <${Histogram} bins=${hist.bins} clipped=${hist.clipped} currency=${unit} />
        <//>

        <${ChartCard}
          title="Publicaciones por tipo de producto"
          subtitle="Los ${TOP_N} más frecuentes"
          table=${groupTable(items, unit)}
        >
          <${Bars} data=${toBars(items, (group) => group.count)} tooltip=${priceTip} />
        <//>

        <${ChartCard}
          title="Precio promedio por tipo de producto"
          subtitle=${unit ? `En ${unit}` : "Promedio por grupo"}
          table=${groupTable(items, unit)}
        >
          <${Bars}
            data=${toBars(items, (group) => group.mean || 0)}
            format=${(value) => money(value, unit)}
            tooltip=${priceTip}
          />
        <//>

        <${ChartCard}
          title="Precio promedio por ciudad"
          subtitle="Ciudades con 2 o más publicaciones"
          table=${groupTable(cities, unit)}
        >
          <${Bars}
            data=${toBars(cities, (group) => group.mean || 0)}
            format=${(value) => money(value, unit)}
            tooltip=${priceTip}
          />
        <//>

        <${ChartCard}
          title="Precio promedio por comuna"
          subtitle="Comunas con 2 o más publicaciones"
          table=${groupTable(comunas, unit)}
        >
          <${Bars}
            data=${toBars(comunas, (group) => group.mean || 0)}
            format=${(value) => money(value, unit)}
            tooltip=${priceTip}
          />
        <//>

        <${ChartCard}
          title="Publicaciones vistas recientemente"
          subtitle="Vistas en las últimas 72 h, por tipo de producto"
          table=${html`<${DataTable}
            columns=${[
              { key: "label", label: "Tipo", render: (row) => row.label },
              { key: "active", label: "Activas", numeric: true, render: (row) => compact(row.active) },
              { key: "count", label: "Total", numeric: true, render: (row) => compact(row.count) },
            ]}
            rows=${activeByItem}
          />`}
        >
          <${Bars}
            data=${toBars(activeByItem, (group) => group.active)}
            tooltip=${(entry) => html`<div>
              <div class="tip-title">${entry.label}</div>
              ${tipRow("Activas", compact(entry.group.active))}
              ${tipRow("Total", compact(entry.group.count))}
            </div>`}
          />
        <//>

        <${ChartCard}
          wide
          title="Dispersión de precios por tipo de producto"
          subtitle="Caja = mitad central del mercado · puntos = atípicos"
          table=${html`<${DataTable}
            columns=${[
              { key: "label", label: "Tipo", render: (row) => row.label },
              { key: "count", label: "n", numeric: true, render: (row) => compact(row.box.count) },
              { key: "q1", label: "P25", numeric: true, render: (row) => money(row.box.q1, unit) },
              { key: "median", label: "Mediana", numeric: true, render: (row) => money(row.box.median, unit) },
              { key: "q3", label: "P75", numeric: true, render: (row) => money(row.box.q3, unit) },
              { key: "out", label: "Atípicos", numeric: true, render: (row) => compact(row.box.outliers.length) },
            ]}
            rows=${boxes}
          />`}
        >
          <${Lazy} height=${280}>${() => html`<${BoxPlot} groups=${boxes} currency=${unit} />`}<//>
        <//>

        <${ChartCard}
          wide
          title="Publicaciones nuevas por día"
          subtitle="Últimos 60 días con actividad"
          table=${seriesTable(daily, "", {
            valueLabel: "Publicaciones",
            pick: (row) => row.count,
            labelFor: formatDay,
          })}
        >
          <${Lazy} height=${240}>
            ${() => html`<${TimeSeries}
              series=${daily}
              pick=${(point) => point.count}
              format=${compact}
              labelFor=${formatDay}
              valueLabel="Publicaciones"
            />`}
          <//>
        <//>

        <${ChartCard}
          wide
          title="Precio promedio por día"
          subtitle=${unit ? `En ${unit}` : "Promedio diario"}
          table=${seriesTable(daily, unit, {
            valueLabel: "Precio promedio",
            pick: (row) => row.mean,
            labelFor: formatDay,
          })}
        >
          <${Lazy} height=${240}>
            ${() => html`<${TimeSeries}
              series=${daily}
              pick=${(point) => point.mean}
              format=${(value) => money(value, unit)}
              labelFor=${formatDay}
              valueLabel="Precio promedio"
            />`}
          <//>
        <//>

        <${ChartCard}
          wide
          title="Publicaciones nuevas por hora"
          subtitle="Últimas 48 horas"
          table=${seriesTable(hourly, "", {
            valueLabel: "Publicaciones",
            pick: (row) => row.count,
            labelFor: formatHour,
          })}
        >
          <${Lazy} height=${240}>
            ${() => html`<${TimeSeries}
              series=${hourly}
              pick=${(point) => point.count}
              format=${compact}
              labelFor=${formatHour}
              valueLabel="Publicaciones"
            />`}
          <//>
        <//>
      </div>
    </section>
  `;
}
