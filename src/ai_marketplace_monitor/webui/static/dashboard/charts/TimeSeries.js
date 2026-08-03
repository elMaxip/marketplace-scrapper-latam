// Change over time: a 2px line with a 10% area wash and a crosshair.
//
// Gaps are drawn, not skipped -- stats.timeSeries emits empty buckets, and a
// day with no listings is real information (a scraper outage or a dead market),
// so the line must not stride across it as if nothing happened.

import { html, useCallback, useRef, useState } from "../html.js";
import { ChartFrame, compact, formatStamp, linear, niceTicks, tipRow, useTooltip } from "./primitives.js";

/**
 * Bucket index -> x pixel.
 *
 * A single bucket is centred rather than pinned to the left edge, which is what
 * a naive domain of [0, 0] would do and would read as an empty chart.
 */
function positions(count, x0, innerW) {
  if (count <= 1) {
    const centre = x0 + innerW / 2;
    const scale = () => centre;
    scale.invert = () => 0;
    return scale;
  }
  return linear([0, count - 1], [x0, x0 + innerW]);
}

/** Path across the series, breaking wherever the value is missing. */
function linePath(points) {
  let path = "";
  let pen = false;
  for (const point of points) {
    if (point.value === null) {
      pen = false;
      continue;
    }
    path += `${pen ? "L" : "M"}${point.x},${point.y}`;
    pen = true;
  }
  return path;
}

/** Area wash under each unbroken run of the line. */
function areaPath(points, baseline) {
  let path = "";
  let run = [];
  const flush = () => {
    if (run.length > 1) {
      path += `M${run[0].x},${baseline}`;
      for (const point of run) path += `L${point.x},${point.y}`;
      path += `L${run[run.length - 1].x},${baseline}Z`;
    }
    run = [];
  };
  for (const point of points) {
    if (point.value === null) flush();
    else run.push(point);
  }
  flush();
  return path;
}

export function TimeSeries({
  series,
  pick = (point) => point.count,
  format = compact,
  labelFor,
  valueLabel = "Valor",
  height = 220,
  emptyText = "Sin datos en el rango",
}) {
  const { show, hide, node } = useTooltip();
  const [hover, setHover] = useState(null);
  const geometry = useRef(null);

  const onMove = useCallback(
    (event) => {
      const geo = geometry.current;
      if (!geo || !series.length) return;
      const box = event.currentTarget.getBoundingClientRect();
      const offset = event.clientX - box.left;
      const index = Math.max(0, Math.min(series.length - 1, Math.round(geo.x.invert(offset))));
      const point = series[index];
      setHover(index);
      show(
        event,
        html`<div>
          <div class="tip-title">${labelFor ? labelFor(point.ms) : formatStamp(point.ms)}</div>
          ${tipRow(valueLabel, format(pick(point)))}
          ${tipRow("Publicaciones", compact(point.count))}
        </div>`,
      );
    },
    [series, show, pick, format, labelFor, valueLabel],
  );

  const onLeave = useCallback(() => {
    setHover(null);
    hide();
  }, [hide]);

  if (!series.length) return html`<p class="chart-empty">${emptyText}</p>`;

  const raw = series.map(pick);
  const finite = raw.filter((value) => value !== null && Number.isFinite(value));
  const { ticks, domain } = niceTicks(0, finite.length ? Math.max(...finite) : 1, 4);

  const marks = ({ x0, innerW, innerH, y0, y }) => {
    const x = positions(series.length, x0, innerW);
    geometry.current = { x, y };
    const points = series.map((point, index) => {
      const value = pick(point);
      const usable = value !== null && Number.isFinite(value);
      return { x: x(index), y: usable ? y(value) : 0, value: usable ? value : null, point };
    });
    const last = [...points].reverse().find((entry) => entry.value !== null);
    const active = hover !== null ? points[hover] : null;

    return html`<g>
      <path class="area-fill" d=${areaPath(points, y0 + innerH)} />
      <path class="line-mark" d=${linePath(points)} />
      ${active && active.value !== null
        ? html`<g>
            <line class="crosshair" x1=${active.x} x2=${active.x} y1=${y0} y2=${y0 + innerH} />
            <circle class="point-marker" cx=${active.x} cy=${active.y} r="4.5" />
          </g>`
        : null}
      ${last && (!active || active.value === null)
        ? html`<circle class="point-marker" cx=${last.x} cy=${last.y} r="4.5" />`
        : null}
      <rect
        class="plot-hit"
        x=${x0}
        y=${y0}
        width=${innerW}
        height=${innerH}
        onMouseMove=${onMove}
        onMouseLeave=${onLeave}
      />
    </g>`;
  };

  // Thin the x labels so they never collide, whatever the bucket count.
  const every = Math.max(1, Math.ceil(series.length / 8));
  const xLabels = ({ x0, innerW }) => {
    const x = positions(series.length, x0, innerW);
    return series.map((point, index) => ({
      x: x(index),
      text: labelFor ? labelFor(point.ms) : formatStamp(point.ms),
    }));
  };

  return html`
    <div class="chart-wrap">
      <${ChartFrame}
        height=${height}
        yDomain=${domain}
        yTicks=${ticks}
        yFormat=${format}
        xLabels=${xLabels}
        xLabelEvery=${every}
        render=${marks}
      />
      ${node}
    </div>
  `;
}
