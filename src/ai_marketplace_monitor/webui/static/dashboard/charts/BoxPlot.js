// Price spread per category, as horizontal Tukey boxes.
//
// This is the chart that answers "is this listing actually cheap, or is the
// whole category cheap?" -- the box shows the middle half of the market and the
// points past the whiskers are the listings worth looking at.
//
// Boxes share one scale so categories are directly comparable; that only works
// while the selection is a single currency, which the panel enforces upstream.

import { html, useRef } from "../html.js";
import { compact, money, tipRow, useElementWidth, useTooltip } from "./primitives.js";

const ROW_HEIGHT = 34;
const BOX_HEIGHT = 14;
const LABEL_WIDTH = 140;
const RIGHT_PAD = 16;
const AXIS_HEIGHT = 22;
const CHAR_PX = 6.6;

function fit(text, maxWidth) {
  const limit = Math.floor(maxWidth / CHAR_PX);
  const value = String(text || "");
  return value.length <= limit ? value : `${value.slice(0, Math.max(1, limit - 1))}…`;
}

export function BoxPlot({ groups, currency = "", emptyText = "Sin datos suficientes" }) {
  const hostRef = useRef(null);
  const width = useElementWidth(hostRef);
  const { show, hide, node } = useTooltip();

  if (!groups.length) return html`<p class="chart-empty">${emptyText}</p>`;

  const trackWidth = Math.max(60, width - LABEL_WIDTH - RIGHT_PAD);
  // Scale to the whiskers, not the outliers: one absurd listing would otherwise
  // squeeze every box into a few pixels.  Outliers past the edge are clamped and
  // called out in the tooltip.
  const high = Math.max(...groups.map((group) => group.box.max), 1);
  const low = Math.min(...groups.map((group) => group.box.min), high);
  const span = high - low || 1;
  const scale = (value) => LABEL_WIDTH + ((Math.min(Math.max(value, low), high) - low) / span) * trackWidth;
  const height = groups.length * ROW_HEIGHT + AXIS_HEIGHT;

  return html`
    <div class="chart-host" ref=${hostRef}>
      <svg width=${width} height=${height} class="chart-svg boxplot" role="img">
        ${groups.map((group, index) => {
          const y = index * ROW_HEIGHT;
          const mid = y + ROW_HEIGHT / 2;
          const box = group.box;
          const boxLeft = scale(box.q1);
          const boxRight = scale(box.q3);
          return html`<g
            key=${group.key}
            class="box-row"
            onMouseMove=${(event) =>
              show(
                event,
                html`<div>
                  <div class="tip-title">${group.label}</div>
                  ${tipRow("Publicaciones", compact(box.count))}
                  ${tipRow("Mediana", money(box.median, currency))}
                  ${tipRow("Rango intercuartil", `${money(box.q1, currency)} – ${money(box.q3, currency)}`)}
                  ${tipRow("Bigotes", `${money(box.min, currency)} – ${money(box.max, currency)}`)}
                  ${box.outliers.length ? tipRow("Atípicos", compact(box.outliers.length)) : null}
                </div>`,
              )}
            onMouseLeave=${hide}
          >
            <rect class="bar-hit" x="0" y=${y} width=${Math.max(width, 1)} height=${ROW_HEIGHT} />
            <text class="bar-label" x="0" y=${mid + 4}>${fit(group.label, LABEL_WIDTH - 10)}</text>
            <line class="whisker" x1=${scale(box.min)} x2=${boxLeft} y1=${mid} y2=${mid} />
            <line class="whisker" x1=${boxRight} x2=${scale(box.max)} y1=${mid} y2=${mid} />
            <line class="whisker-cap" x1=${scale(box.min)} x2=${scale(box.min)} y1=${mid - 5} y2=${mid + 5} />
            <line class="whisker-cap" x1=${scale(box.max)} x2=${scale(box.max)} y1=${mid - 5} y2=${mid + 5} />
            <rect
              class="box-fill"
              x=${boxLeft}
              y=${mid - BOX_HEIGHT / 2}
              width=${Math.max(2, boxRight - boxLeft)}
              height=${BOX_HEIGHT}
              rx="3"
            />
            <line
              class="box-median"
              x1=${scale(box.median)}
              x2=${scale(box.median)}
              y1=${mid - BOX_HEIGHT / 2}
              y2=${mid + BOX_HEIGHT / 2}
            />
            ${box.outliers.map(
              (value, outlierIndex) =>
                html`<circle key=${outlierIndex} class="outlier" cx=${scale(value)} cy=${mid} r="2.5" />`,
            )}
          </g>`;
        })}
        <g class="chart-axis-text" text-anchor="middle">
          <line
            class="chart-baseline"
            x1=${LABEL_WIDTH}
            x2=${LABEL_WIDTH + trackWidth}
            y1=${groups.length * ROW_HEIGHT}
            y2=${groups.length * ROW_HEIGHT}
          />
          <text x=${LABEL_WIDTH} y=${height - 6} text-anchor="start">${money(low, currency)}</text>
          <text x=${LABEL_WIDTH + trackWidth} y=${height - 6} text-anchor="end">${money(high, currency)}</text>
        </g>
      </svg>
      ${node}
    </div>
  `;
}
