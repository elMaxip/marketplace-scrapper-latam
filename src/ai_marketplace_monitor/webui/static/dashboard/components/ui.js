// Small shared UI pieces.  Deliberately unstyled beyond class names -- the
// visual language lives in app.css alongside the rest of the web UI, so the
// dashboard inherits the existing theme tokens instead of inventing a second
// design system.

import { html, useEffect, useRef, useState } from "../html.js";

export function Select({ label, value, onChange, options, placeholder = "Cualquiera" }) {
  return html`
    <label class="fld">
      <span>${label}</span>
      <select value=${value} onChange=${(event) => onChange(event.target.value)}>
        <option value="">${placeholder}</option>
        ${options.map((option) => {
          const key = typeof option === "string" ? option : option.value;
          const text = typeof option === "string" ? option : option.label;
          return html`<option key=${key} value=${key}>${text}</option>`;
        })}
      </select>
    </label>
  `;
}

export function NumberField({ label, value, onChange, placeholder = "" }) {
  return html`
    <label class="fld narrow">
      <span>${label}</span>
      <input
        type="number"
        inputmode="numeric"
        placeholder=${placeholder}
        value=${value === null || value === undefined ? "" : value}
        onChange=${(event) => {
          const raw = event.target.value;
          onChange(raw === "" ? null : Number(raw));
        }}
      />
    </label>
  `;
}

export function DateField({ label, value, onChange }) {
  return html`
    <label class="fld narrow">
      <span>${label}</span>
      <input type="date" value=${value} onChange=${(event) => onChange(event.target.value)} />
    </label>
  `;
}

/**
 * Search box that only pushes upstream after a pause.
 *
 * Filtering is fast, but every chart downstream recomputes on the result, so
 * debouncing keeps typing smooth on a large corpus.
 */
export function SearchField({ label, value, onChange, placeholder = "", delay = 200 }) {
  const [draft, setDraft] = useState(value);
  const timer = useRef(null);

  // Keep in step when the value is changed elsewhere (e.g. "clear filters").
  useEffect(() => setDraft(value), [value]);

  useEffect(() => () => clearTimeout(timer.current), []);

  return html`
    <label class="fld grow">
      <span>${label}</span>
      <input
        type="search"
        placeholder=${placeholder}
        value=${draft}
        onInput=${(event) => {
          const next = event.target.value;
          setDraft(next);
          clearTimeout(timer.current);
          timer.current = setTimeout(() => onChange(next), delay);
        }}
      />
    </label>
  `;
}

export function Toggle({ label, checked, onChange, title = "" }) {
  return html`
    <label class="toggle" title=${title}>
      <input type="checkbox" checked=${checked} onChange=${(event) => onChange(event.target.checked)} />
      <span>${label}</span>
    </label>
  `;
}

/** Section heading with an optional right-hand slot for actions. */
export function SectionHead({ title, hint, children }) {
  return html`
    <header class="section-head">
      <div>
        <h2>${title}</h2>
        ${hint ? html`<p class="hint">${hint}</p>` : null}
      </div>
      ${children ? html`<div class="section-actions">${children}</div>` : null}
    </header>
  `;
}

export function Empty({ children }) {
  return html`<p class="chart-empty">${children}</p>`;
}

/** Small inline note used where a number needs a caveat attached to it. */
export function Note({ tone = "info", children }) {
  return html`<p class=${`note ${tone}`}>${children}</p>`;
}

export function Pill({ tone = "", children, title = "" }) {
  return html`<span class=${`pill ${tone}`} title=${title}>${children}</span>`;
}
