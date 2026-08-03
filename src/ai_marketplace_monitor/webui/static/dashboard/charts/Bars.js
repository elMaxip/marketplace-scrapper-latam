// Horizontal bar chart for ranked categories.
//
// Horizontal rather than columns because every category axis in this panel is
// text -- comunas, cities, search items, seller names -- and horizontal bars
// give those labels a full line instead of a 35-degree rotation.
//
// One measure, one series, so every bar wears categorical slot 1: coloring bars
// by their own rank would double-encode length as hue.

import { html, useRef } from "../html.js";
import { compact, useElementWidth, useTooltip } from "./primitives.js";

const ROW_HEIGHT = 26;
const BAR_THICKNESS = 14;
const LABEL_WIDTH = 140;
const VALUE_WIDTH = 84;
const CORNER = 4;

/** Approximate glyph width at the label's font size, for fitting text. */
const CHAR_PX = 6.6;

/**
 * Bar outline with the data-end rounded and the baseline end square.
 * A plain `rx` would round all four corners, detaching the bar from its axis.
 */
function barPath(x, y, width, height) {
  const radius = Math.min(CORNER, width, height / 2);
  const right = x + width;
  return [
    `M${x},${y}`,
    `H${right - radius}`,
    `A${radius},${radius} 0 0 1 ${right},${y + radius}`,
    `V${y + height - radius}`,
    `A${radius},${radius} 0 0 1 ${right - radius},${y + height}`,
    `H${x}`,
    "Z",
  ].join(" ");
}

/** Shorten to fit the label gutter; the full text stays in the tooltip. */
function fit(text, maxWidth) {
  const limit = Math.floor(maxWidth / CHAR_PX);
  const value = String(text || "");
  return value.length <= limit ? value : `${value.slice(0, Math.max(1, limit - 1))}…`;
}

export function Bars({ data, format = compact, tooltip, onSelect, emphasisKey = null, emptyText = "Sin datos" }) {
  const hostRef = useRef(null);
  const width = useElementWidth(hostRef);
  const { show, hide, node } = useTooltip();

  if (!data.length) return html`<p class="chart-empty">${emptyText}</p>`;

  const trackWidth = Math.max(40, width - LABEL_WIDTH - VALUE_WIDTH);
  const max = Math.max(...data.map((entry) => Math.abs(entry.value) || 0), 1);
  const height = data.length * ROW_HEIGHT;

  return html`
    <div class="chart-host" ref=${hostRef}>
      <svg width=${width} height=${height} class="chart-svg bars" role="img">
        ${data.map((entry, index) => {
          const y = index * ROW_HEIGHT;
          const length = Math.max(2, (Math.abs(entry.value) / max) * trackWidth);
          const dim = emphasisKey !== null && entry.key !== emphasisKey;
          return html`<g
            key=${entry.key}
            class=${`bar-row${dim ? " dim" : ""}${onSelect ? " clickable" : ""}`}
            onMouseMove=${(event) => show(event, tooltip ? tooltip(entry) : entry.label)}
            onMouseLeave=${hide}
            onClick=${onSelect ? () => onSelect(entry) : undefined}
          >
            <rect class="bar-hit" x="0" y=${y} width=${Math.max(width, 1)} height=${ROW_HEIGHT} />
            <text class="bar-label" x="0" y=${y + ROW_HEIGHT / 2 + 4}>${fit(entry.label, LABEL_WIDTH - 10)}</text>
            <path
              class="bar-fill"
              d=${barPath(LABEL_WIDTH, y + (ROW_HEIGHT - BAR_THICKNESS) / 2, length, BAR_THICKNESS)}
            />
            <text class="bar-value" x=${LABEL_WIDTH + length + 8} y=${y + ROW_HEIGHT / 2 + 4}>
              ${format(entry.value)}
            </text>
          </g>`;
        })}
      </svg>
      ${node}
    </div>
  `;
}
