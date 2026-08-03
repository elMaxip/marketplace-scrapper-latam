// React bindings for the dashboard.
//
// React, ReactDOM and htm are vendored as classic UMD scripts (see
// static/vendor/), matching how CodeMirror and toml-edit-js are already loaded
// -- this project ships as a Python package and has no JS build step.  htm
// turns tagged template literals into React.createElement calls, so components
// read like JSX without needing a compiler.
//
// Every module imports `html` and the hooks from here rather than reaching for
// the globals, so there is exactly one place to change if the loading strategy
// ever does.

const React = window.React;
const ReactDOM = window.ReactDOM;

if (!React || !ReactDOM || !window.htm) {
  throw new Error("dashboard: React/ReactDOM/htm globals are missing (vendor scripts failed to load)");
}

export const html = window.htm.bind(React.createElement);

export const {
  Fragment,
  createContext,
  createElement,
  memo,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} = React;

export { React, ReactDOM };
export default React;
