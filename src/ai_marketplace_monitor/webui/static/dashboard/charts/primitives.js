// Shared chart machinery: scales, ticks, formatting, the SVG frame and the
// hover layer.  Every chart in the panel is built from these, so mark specs and
// chrome stay identical across the dashboard instead of drifting per chart.

import { html, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "../html.js";

/** Plot padding.  The bottom band is sized to hold rotated x labels. */
export const MARGIN = { top: 12, right: 16, bottom: 34, left: 56 };

/** Track an element's width so the SVG can be responsive without a viewBox hack. */
export function useElementWidth(ref, fallback = 640) {
  const [width, setWidth] = useState(fallback);
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return undefined;
    const observer = new ResizeObserver((entries) => {
      const next = Math.floor(entries[0].contentRect.width);
      if (next > 0) setWidth(next);
    });
    observer.observe(node);
    setWidth(Math.floor(node.getBoundingClientRect().width) || fallback);
    return () => observer.disconnect();
  }, [ref, fallback]);
  return width;
}

/** Linear scale from a data domain onto a pixel range. */
export function linear([d0, d1], [r0, r1]) {
  const span = d1 - d0 || 1;
  const scale = (value) => r0 + ((value - d0) / span) * (r1 - r0);
  scale.invert = (pixel) => d0 + ((pixel - r0) / (r1 - r0 || 1)) * span;
  scale.domain = [d0, d1];
  scale.range = [r0, r1];
  return scale;
}

/**
 * Evenly spaced band positions with a gap between neighbours.
 *
 * The gap is the surface doing the separating -- 2px minimum, growing with the
 * band so wide bars stay visually distinct without a stroke around them.
 */
export function band(count, [r0, r1], { maxThickness = 24, gap = 2 } = {}) {
  const step = count > 0 ? (r1 - r0) / count : r1 - r0;
  const thickness = Math.max(1, Math.min(maxThickness, step - gap));
  return {
    step,
    thickness,
    center: (index) => r0 + step * (index + 0.5),
    start: (index) => r0 + step * (index + 0.5) - thickness / 2,
  };
}

/** Round tick values that bracket the domain (0 / 1,000 / 2,000 rather than 1,037). */
export function niceTicks(min, max, count = 5) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return { ticks: [0], domain: [0, 1] };
  if (min === max) {
    const pad = Math.abs(min) || 1;
    min -= pad / 2;
    max += pad / 2;
  }
  const raw = (max - min) / Math.max(1, count);
  const magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
  const normalized = raw / magnitude;
  const stepFactor = normalized >= 5 ? 10 : normalized >= 2 ? 5 : normalized >= 1 ? 2 : 1;
  const step = stepFactor * magnitude;
  const start = Math.floor(min / step) * step;
  const end = Math.ceil(max / step) * step;
  const ticks = [];
  // Guard against a pathological step producing an unbounded loop.
  for (let value = start, guard = 0; value <= end + step / 2 && guard < 200; value += step, guard += 1) {
    ticks.push(Number(value.toFixed(10)));
  }
  return { ticks, domain: [start, end] };
}

const COMPACT = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
const PLAIN = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const PRECISE = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

/** 1,284 / 12.9K / 4.2M -- for axis ticks and stat tiles. */
export function compact(value) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return Math.abs(value) >= 10000 ? COMPACT.format(value) : PLAIN.format(value);
}

export function number(value) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return Math.abs(value) < 100 ? PRECISE.format(value) : PLAIN.format(value);
}

/**
 * Money keeps the currency label the marketplace used; it is not converted.
 *
 * A symbol sits flush against the number the way prices are actually written
 * ("$150.000"); an alphabetic code gets a space ("CLP 150.000").  Grouping comes
 * from the viewer's locale, so a Chilean browser renders 150000 as "150.000"
 * rather than "150,000".
 */
export function money(value, currency = "") {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const text = PLAIN.format(Math.round(value));
  if (!currency) return text;
  return /[\p{L}]/u.test(currency) ? `${currency} ${text}` : `${currency}${text}`;
}

export function percent(value) {
  if (value === null || !Number.isFinite(value)) return "—";
  return `${value > 0 ? "+" : ""}${PRECISE.format(value * 100)}%`;
}

const DAY_LABEL = new Intl.DateTimeFormat(undefined, { day: "2-digit", month: "short" });
const HOUR_LABEL = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const FULL_LABEL = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

export const formatDay = (ms) => DAY_LABEL.format(new Date(ms));
export const formatHour = (ms) => HOUR_LABEL.format(new Date(ms));
export const formatStamp = (ms) => (ms ? FULL_LABEL.format(new Date(ms)) : "—");

/**
 * Hover state for a chart.
 *
 * Returns handlers to spread onto marks plus the tooltip node.  Coordinates are
 * kept relative to the chart card so the tooltip can be a plain absolutely
 * positioned div rather than an SVG foreignObject.
 */
export function useTooltip() {
  const [tip, setTip] = useState(null);
  const show = useCallback((event, content) => {
    const host = event.currentTarget.closest(".chart-card") || event.currentTarget.ownerSVGElement;
    if (!host) return;
    const box = host.getBoundingClientRect();
    setTip({ x: event.clientX - box.left, y: event.clientY - box.top, content });
  }, []);
  const hide = useCallback(() => setTip(null), []);

  const node = tip
    ? html`<div
        class="chart-tooltip"
        role="tooltip"
        style=${{ left: `${tip.x}px`, top: `${tip.y}px` }}
      >
        ${tip.content}
      </div>`
    : null;

  return { show, hide, node, active: !!tip };
}

/** One row of a tooltip: a label and its value. */
export function tipRow(label, value) {
  return html`<div class="tip-row"><span>${label}</span><b>${value}</b></div>`;
}

/**
 * The SVG shell: sizing, gridlines, axes and tick labels.
 *
 * `render` is a function rather than children so marks are only built once the
 * pixel geometry is known -- and so it stays a real component, keeping its own
 * hooks out of the caller's hook order.  The rendered height includes the axis
 * band, so a card never grows a nested scrollbar just to show its x labels.
 */
export function ChartFrame({
  height = 220,
  yDomain,
  yTicks,
  yFormat = compact,
  xLabels = null,
  xLabelEvery = 1,
  rotateXLabels = false,
  margin = MARGIN,
  render,
}) {
  const hostRef = useRef(null);
  const width = useElementWidth(hostRef);
  const innerW = Math.max(10, width - margin.left - margin.right);
  const innerH = Math.max(10, height - margin.top - margin.bottom);

  const y = useMemo(() => linear(yDomain, [margin.top + innerH, margin.top]), [yDomain, innerH, margin.top]);
  // Label positions depend on the measured plot width, so callers hand over a
  // function rather than pre-computed coordinates.
  const ticks = xLabels ? xLabels({ x0: margin.left, innerW }) : [];

  return html`
    <div class="chart-host" ref=${hostRef}>
      <svg width=${width} height=${height} role="img" class="chart-svg">
        <g class="chart-grid">
          ${yTicks.map(
            (tick) => html`<line
              key=${`g${tick}`}
              x1=${margin.left}
              x2=${margin.left + innerW}
              y1=${y(tick)}
              y2=${y(tick)}
            />`,
          )}
        </g>
        <g class="chart-axis-text" text-anchor="end">
          ${yTicks.map(
            (tick) => html`<text key=${`y${tick}`} x=${margin.left - 8} y=${y(tick) + 4}>${yFormat(tick)}</text>`,
          )}
        </g>
        <line
          class="chart-baseline"
          x1=${margin.left}
          x2=${margin.left + innerW}
          y1=${margin.top + innerH}
          y2=${margin.top + innerH}
        />
        ${render({ x0: margin.left, y0: margin.top, innerW, innerH, y, width })}
        <g class="chart-axis-text" text-anchor=${rotateXLabels ? "end" : "middle"}>
          ${ticks.map((label, index) =>
            index % xLabelEvery !== 0
              ? null
              : html`<text
                  key=${`x${index}`}
                  x=${label.x}
                  y=${margin.top + innerH + 16}
                  transform=${rotateXLabels ? `rotate(-35 ${label.x} ${margin.top + innerH + 16})` : undefined}
                >
                  ${label.text}
                </text>`,
          )}
        </g>
      </svg>
    </div>
  `;
}

/**
 * Card wrapper shared by every chart: title, a chart/table toggle, and the
 * empty state.
 *
 * The table twin is not optional decoration -- it is how a value stays readable
 * without relying on hover or on distinguishing a hue, so every chart ships one.
 */
export function ChartCard({ title, subtitle, table, children, stale = false, wide = false }) {
  const [view, setView] = useState("chart");
  return html`
    <section class=${`chart-card${wide ? " wide" : ""}${stale ? " stale" : ""}`}>
      <header class="chart-card-head">
        <div>
          <h3>${title}</h3>
          ${subtitle ? html`<p class="chart-sub">${subtitle}</p>` : null}
        </div>
        ${table
          ? html`<div class="seg" role="group" aria-label="Vista">
              <button
                class=${view === "chart" ? "active" : ""}
                aria-pressed=${view === "chart"}
                onClick=${() => setView("chart")}
              >
                Gráfico
              </button>
              <button
                class=${view === "table" ? "active" : ""}
                aria-pressed=${view === "table"}
                onClick=${() => setView("table")}
              >
                Tabla
              </button>
            </div>`
          : null}
      </header>
      <div class="chart-body">${view === "table" && table ? table : children}</div>
    </section>
  `;
}

/** Table twin used by most charts: a plain two-or-more column table. */
export function DataTable({ columns, rows, empty = "Sin datos" }) {
  if (!rows.length) return html`<p class="chart-empty">${empty}</p>`;
  return html`
    <div class="table-scroll">
      <table class="data-table">
        <thead>
          <tr>
            ${columns.map((column) => html`<th key=${column.key} class=${column.numeric ? "num" : ""}>${column.label}</th>`)}
          </tr>
        </thead>
        <tbody>
          ${rows.map(
            (row, index) => html`<tr key=${row.key ?? index}>
              ${columns.map(
                (column) => html`<td key=${column.key} class=${column.numeric ? "num" : ""}>${column.render(row)}</td>`,
              )}
            </tr>`,
          )}
        </tbody>
      </table>
    </div>
  `;
}

/**
 * Defer mounting until the element scrolls near the viewport.
 *
 * The heavy charts (boxplots over every category, the long daily series) are
 * only built when they are about to be seen, which is what keeps opening the
 * panel fast on a large corpus.
 */
export function useNearViewport(ref, { rootMargin = "300px" } = {}) {
  const [near, setNear] = useState(false);
  useEffect(() => {
    const node = ref.current;
    if (!node || near) return undefined;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) setNear(true);
      },
      { rootMargin },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [ref, near, rootMargin]);
  return near;
}

/** Wrapper that renders a placeholder until its slot is near the viewport. */
export function Lazy({ height = 260, children }) {
  const ref = useRef(null);
  const near = useNearViewport(ref);
  return html`<div ref=${ref} class="lazy-slot" style=${{ minHeight: near ? undefined : `${height}px` }}>
    ${near ? children() : html`<div class="chart-skeleton" aria-hidden="true"></div>`}
  </div>`;
}
