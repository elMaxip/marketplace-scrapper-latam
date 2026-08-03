// Sortable table that only renders the rows on screen.
//
// The listing table has to stay usable at tens of thousands of rows, and the
// DOM is the bottleneck long before the data is.  Rows are a fixed height so
// the scroll offset maps to a row index arithmetically -- no measuring, no
// layout thrash while scrolling.

import { html, useLayoutEffect, useMemo, useRef, useState } from "../html.js";

export const ROW_HEIGHT = 38;
const OVERSCAN = 8;

/**
 * @param columns  {key, label, numeric?, width?, render(row), sortValue(row)?}
 * @param sortable set a column's `sortValue` to make it sortable
 */
export function VirtualTable({
  columns,
  rows,
  rowKey = (row) => row.key,
  onRowClick = null,
  initialSort = null,
  height = 480,
  empty = "Sin resultados",
}) {
  const [sort, setSort] = useState(initialSort);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewport, setViewport] = useState(height);
  const bodyRef = useRef(null);

  useLayoutEffect(() => {
    const node = bodyRef.current;
    if (!node) return undefined;
    const observer = new ResizeObserver((entries) => setViewport(entries[0].contentRect.height));
    observer.observe(node);
    setViewport(node.getBoundingClientRect().height || height);
    return () => observer.disconnect();
  }, [height]);

  const sorted = useMemo(() => {
    if (!sort) return rows;
    const column = columns.find((entry) => entry.key === sort.key);
    if (!column || !column.sortValue) return rows;
    const direction = sort.dir === "asc" ? 1 : -1;
    // Copy before sorting: `rows` is derived state shared with other panels.
    return [...rows].sort((a, b) => {
      const left = column.sortValue(a);
      const right = column.sortValue(b);
      // Missing values sort last whichever way the column is pointing, so a
      // descending sort does not open with a wall of blanks.
      if (left === null || left === undefined) return right === null || right === undefined ? 0 : 1;
      if (right === null || right === undefined) return -1;
      if (typeof left === "string" || typeof right === "string") {
        return String(left).localeCompare(String(right)) * direction;
      }
      return (left - right) * direction;
    });
  }, [rows, sort, columns]);

  const total = sorted.length;
  const first = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
  const visibleCount = Math.ceil(viewport / ROW_HEIGHT) + OVERSCAN * 2;
  const slice = sorted.slice(first, Math.min(total, first + visibleCount));

  const toggleSort = (column) => {
    if (!column.sortValue) return;
    setSort((prev) => {
      if (!prev || prev.key !== column.key) return { key: column.key, dir: column.numeric ? "desc" : "asc" };
      if (prev.dir === "asc") return { key: column.key, dir: "desc" };
      return null;
    });
    if (bodyRef.current) bodyRef.current.scrollTop = 0;
  };

  const template = columns.map((column) => column.width || "1fr").join(" ");

  return html`
    <div class="vtable">
      <div class="vtable-head" style=${{ gridTemplateColumns: template }}>
        ${columns.map(
          (column) => html`<button
            key=${column.key}
            type="button"
            class=${`vth${column.numeric ? " num" : ""}${column.sortValue ? " sortable" : ""}`}
            aria-sort=${sort && sort.key === column.key ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
            onClick=${() => toggleSort(column)}
          >
            ${column.label}
            ${sort && sort.key === column.key ? html`<span class="sort-caret">${sort.dir === "asc" ? "▲" : "▼"}</span>` : null}
          </button>`,
        )}
      </div>
      <div
        class="vtable-body"
        ref=${bodyRef}
        style=${{ height: `${height}px` }}
        onScroll=${(event) => setScrollTop(event.currentTarget.scrollTop)}
      >
        ${total === 0
          ? html`<p class="chart-empty">${empty}</p>`
          : html`<div class="vtable-spacer" style=${{ height: `${total * ROW_HEIGHT}px` }}>
              <div class="vtable-window" style=${{ transform: `translateY(${first * ROW_HEIGHT}px)` }}>
                ${slice.map(
                  (row) => html`<div
                    key=${rowKey(row)}
                    class=${`vtr${onRowClick ? " clickable" : ""}`}
                    style=${{ gridTemplateColumns: template, height: `${ROW_HEIGHT}px` }}
                    onClick=${onRowClick ? () => onRowClick(row) : undefined}
                    tabIndex=${onRowClick ? 0 : undefined}
                    onKeyDown=${
                      onRowClick
                        ? (event) => {
                            if (event.key === "Enter" || event.key === " ") {
                              event.preventDefault();
                              onRowClick(row);
                            }
                          }
                        : undefined
                    }
                  >
                    ${columns.map(
                      (column) => html`<div key=${column.key} class=${`vtd${column.numeric ? " num" : ""}`}>
                        ${column.render(row)}
                      </div>`,
                    )}
                  </div>`,
                )}
              </div>
            </div>`}
      </div>
      <footer class="vtable-foot">${total.toLocaleString()} filas</footer>
    </div>
  `;
}
