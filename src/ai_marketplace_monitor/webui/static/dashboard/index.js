// Dashboard entry point.
//
// Loaded lazily by app.js the first time the Dashboard tab is opened, so the
// config editor's startup path never pays for React, IndexedDB or the sync.

import { ReactDOM, html } from "./html.js";
import { App } from "./App.js";

let root = null;

/** Mount into `container`, or no-op if already mounted. */
export function mount(container) {
  if (root) return root;
  root = ReactDOM.createRoot(container);
  root.render(html`<${App} />`);
  return root;
}

export function unmount() {
  if (!root) return;
  root.unmount();
  root = null;
}
