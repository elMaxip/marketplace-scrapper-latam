// Price distribution as columns.
//
// Bins come from stats.histogram (Freedman-Diaconis, upper tail clipped), so
// this component only draws -- how many bins there are is a statistics decision,
// not a rendering one.

import { html } from "../html.js";
import { ChartFrame, band, compact, money, niceTicks, tipRow, useTooltip } from "./primitives.js";

const CORNER = 4;

/** Column with a rounded cap and a square foot on the baseline. */
function columnPath(x, y, width, height) {
  if (height <= 0) return "";
  const radius = Math.min(CORNER, width / 2, height);
  const bottom = y + height;
  return [
    `M${x},${bottom}`,
    `V${y + radius}`,
    `A${radius},${radius} 0 0 1 ${x + radius},${y}`,
    `H${x + width - radius}`,
    `A${radius},${radius} 0 0 1 ${x + width},${y + radius}`,
    `V${bottom}`,
    "Z",
  ].join(" ");
}

export function Histogram({ bins, clipped = 0, currency = "", height = 240 }) {
  const { show, hide, node } = useTooltip();
  if (!bins.length) return html`<p class="chart-empty">Sin precios legibles en esta selección</p>`;

  const { ticks, domain } = niceTicks(0, Math.max(...bins.map((bin) => bin.count)), 4);

  const marks = ({ x0, innerW, innerH, y0, y }) => {
    const scale = band(bins.length, [x0, x0 + innerW], { maxThickness: 24, gap: 2 });
    const baseline = y0 + innerH;
    return html`<g>
      ${bins.map((bin, index) => {
        const top = y(bin.count);
        return html`<g
          key=${index}
          class="col"
          onMouseMove=${(event) =>
            show(
              event,
              html`<div>
                ${tipRow("Rango", `${money(bin.from, currency)} – ${money(bin.to, currency)}`)}
                ${tipRow("Publicaciones", compact(bin.count))}
              </div>`,
            )}
          onMouseLeave=${hide}
        >
          <rect class="col-hit" x=${scale.start(index)} y=${y0} width=${scale.thickness} height=${innerH} />
          <path class="col-fill" d=${columnPath(scale.start(index), top, scale.thickness, baseline - top)} />
        </g>`;
      })}
    </g>`;
  };

  return html`
    <div class="chart-wrap">
      <${ChartFrame} height=${height} yDomain=${domain} yTicks=${ticks} yFormat=${compact} render=${marks} />
      <p class="chart-foot">
        ${`${bins.length} intervalos`}
        ${clipped ? html` · <span class="muted">${`${clipped} atípicos por encima del percentil 99 fuera del gráfico`}</span>` : null}
      </p>
      ${node}
    </div>
  `;
}
