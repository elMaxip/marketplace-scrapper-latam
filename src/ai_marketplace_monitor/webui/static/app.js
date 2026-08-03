// AI Marketplace Monitor — Web UI frontend.
// Vanilla JS, no build step. Provides:
//   - Login form + session cookie handling
//   - TOML editor with line numbers and syntax highlighting (lightweight)
//   - Live log tail via WebSocket with level/text filtering + expand
//   - Save / Validate with inline error at the offending line

(() => {
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const state = {
    csrf: null,
    fileId: "primary",
    baseMtime: null,
    originalContent: "",
    currentContent: "",
    logLevel: "ALL",
    logKind: "",
    logItem: "",
    logMinScore: null,
    logFilter: "",
    ws: null,
    records: [],
    expanded: new Set(),
    knownItems: new Set(),
    lastActivity: null, // epoch seconds of the most recent log record
    monitorState: "disconnected", // "connected" | "idle" | "disconnected"
    wsConnected: false,
    errorCount: 0, // unread ERROR-level messages (for tab badge)
  };

  // ---------------------------------------------------------------
  // Cookies / auth
  // ---------------------------------------------------------------
  const getCookie = (name) => {
    const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : null;
  };

  const api = async (path, opts = {}) => {
    const headers = { ...(opts.headers || {}) };
    if (opts.method && opts.method !== "GET" && state.csrf) {
      headers["X-CSRF-Token"] = state.csrf;
    }
    if (opts.body && !(opts.body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
    }
    const res = await fetch(path, { ...opts, headers, credentials: "same-origin" });
    if (res.status === 401) {
      showLogin();
      throw new Error("unauthenticated");
    }
    return res;
  };

  // ---------------------------------------------------------------
  // Login flow
  // ---------------------------------------------------------------
  const showLogin = async () => {
    $("#login-screen").classList.remove("hidden");
    $("#app").classList.add("hidden");
    // Fetch the auth mode so we can decide between login form and open mode.
    try {
      const info = await (await fetch("/api/auth/info", { credentials: "same-origin" })).json();
      if (info.open) {
        // Open mode — no credentials configured, auto-login as anonymous.
        const res = await fetch("/api/login", {
          method: "POST",
          body: new FormData(),
          credentials: "same-origin",
        });
        if (res.ok) {
          const data = await res.json();
          state.csrf = data.csrf || getCookie("aimm_csrf");
          hideLogin();
          await bootstrap();
          return;
        }
      }
      // Authenticated mode — show sign-in form.
      const form = $("#login-form");
      const subtitle = $("#login-subtitle");
      subtitle.textContent =
        "Inicia sesión con las credenciales del marketplace de tu configuración.";
      subtitle.hidden = false;
      $("#login-submit").textContent = "Iniciar sesión";
      if (info.username_hint) form.username.value = info.username_hint;
    } catch (err) {
      // fall back to generic login form
    }
  };
  const hideLogin = () => {
    $("#login-screen").classList.add("hidden");
    $("#app").classList.remove("hidden");
  };

  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const body = new FormData();
    body.set("username", form.username.value);
    body.set("password", form.password.value);
    try {
      const res = await fetch("/api/login", { method: "POST", body, credentials: "same-origin" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Error al iniciar sesión" }));
        $("#login-error").textContent = err.detail || "Error al iniciar sesión";
        $("#login-error").hidden = false;
        return;
      }
      const data = await res.json();
      state.csrf = data.csrf || getCookie("aimm_csrf");
      $("#login-error").hidden = true;
      hideLogin();
      await bootstrap();
    } catch (err) {
      $("#login-error").textContent = String(err);
      $("#login-error").hidden = false;
    }
  });

  $("#logout-btn").addEventListener("click", async () => {
    await api("/api/logout", { method: "POST" });
    if (state.ws) state.ws.close();
    state.csrf = null;
    showLogin();
  });

  // ---------------------------------------------------------------
  // Editor — CodeMirror 5 with TOML syntax highlighting
  // ---------------------------------------------------------------
  const editorHost = $("#editor-host");

  // Thin wrapper so the rest of the code uses editor.getValue() / editor.setValue()
  // regardless of whether CodeMirror loaded successfully.
  let editor;
  let validateTimer = null;
  const onEditorChange = () => {
    state.currentContent = editor.getValue();
    const dirty = state.currentContent !== state.originalContent;
    $("#save-btn").disabled = !dirty;
    if (validateTimer) clearTimeout(validateTimer);
    if (dirty) {
      setEditorStatus("escribiendo…");
      validateTimer = setTimeout(() => {
        validateTimer = null;
        validateConfig();
      }, 400);
    }
  };

  if (window.CodeMirror) {
    editor = CodeMirror(editorHost, {
      mode: "toml",
      theme: "default",
      lineNumbers: true,
      indentUnit: 2,
      tabSize: 2,
      indentWithTabs: false,
      lineWrapping: false,
      extraKeys: {
        "Cmd-S": () => saveConfig(),
        "Ctrl-S": () => saveConfig(),
        Tab: (cm) => cm.replaceSelection("  ", "end"),
      },
    });
    editor.on("change", onEditorChange);
    // Expose a uniform API.
    editor.getValue = editor.getValue.bind(editor);
    editor.setValue = editor.setValue.bind(editor);
    editor.getScrollInfo = editor.getScrollInfo.bind(editor);
  } else {
    // Fallback: plain textarea if CodeMirror failed to load.
    const textarea = document.createElement("textarea");
    textarea.className = "aimm-editor";
    textarea.spellcheck = false;
    editorHost.appendChild(textarea);
    editor = {
      getValue: () => textarea.value,
      setValue: (v) => { textarea.value = v; },
      getScrollInfo: () => ({ top: textarea.scrollTop }),
      on: () => {},
      refresh: () => {},
    };
    textarea.addEventListener("input", onEditorChange);
    textarea.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        saveConfig();
      }
    });
  }

  // ---------------------------------------------------------------
  // Config load / save / validate
  // ---------------------------------------------------------------
  const setEditorStatus = (msg, cls = "") => {
    const el = $("#editor-status");
    el.className = "editor-status " + cls;
    el.textContent = msg;
  };

  const loadConfig = async () => {
    const files = await (await api("/api/config/files")).json();
    if (!files.files.length) return;
    const f = files.files[0];
    state.fileId = f.id;
    $("#config-name").textContent = f.path;
    $("#mtime").textContent = "modificado " + new Date(f.mtime * 1000).toLocaleString();

    const res = await (await api(`/api/config/file/${f.id}`)).json();
    state.originalContent = res.content;
    state.currentContent = res.content;
    state.baseMtime = res.mtime;
    editor.setValue(res.content);
    // Prefer the server-provided sections list, but fall back to a
    // client-side scan if the server didn't include one (e.g. user is
    // running an older aimm that hasn't been restarted yet).
    if (Array.isArray(res.sections) && res.sections.length) {
      state.sections = res.sections;
    } else {
      state.sections = scanSectionsClient(res.content);
    }
    renderGutter();
    $("#save-btn").disabled = true;
    if (res.has_masked_secrets) {
      setEditorStatus(
        `🔒 Secretos ocultos como "${res.mask_token}" — déjalos tal cual para conservarlos, o escribe encima para reemplazarlos.`,
        "ok"
      );
    } else {
      setEditorStatus("");
    }
  };

  const validateConfig = async () => {
    setEditorStatus("validando…");
    try {
      const res = await api("/api/config/validate", {
        method: "POST",
        body: JSON.stringify({ content: state.currentContent }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setEditorStatus(
          "✗ " + (data.detail || `HTTP ${res.status}`),
          "err"
        );
        return false;
      }
      if (data.valid) {
        setEditorStatus("✓ la configuración es válida", "ok");
        return true;
      }
      setEditorStatus("✗ " + (data.error || "inválida"), "err");
      return false;
    } catch (err) {
      console.error("validate failed", err);
      setEditorStatus("✗ falló la validación: " + err.message, "err");
      return false;
    }
  };

  const saveConfig = async () => {
    // If a debounced validate is pending, cancel it — the server will
    // re-validate on PUT anyway.
    if (validateTimer) {
      clearTimeout(validateTimer);
      validateTimer = null;
    }
    setEditorStatus("guardando…");
    let res, data;
    try {
      res = await api(`/api/config/file/${state.fileId}`, {
        method: "PUT",
        body: JSON.stringify({
          content: state.currentContent,
          base_mtime: state.baseMtime,
        }),
      });
      data = await res.json().catch(() => ({}));
    } catch (err) {
      console.error("save failed", err);
      setEditorStatus("✗ falló el guardado: " + err.message, "err");
      return;
    }
    if (!res.ok || !data.ok) {
      setEditorStatus(
        "✗ " + (data.error || data.detail || `HTTP ${res.status}`),
        "err"
      );
      if (res.status === 409) {
        if (confirm("La configuración cambió en disco. ¿Recargar desde disco y perder tus cambios?")) {
          await loadConfig();
        }
      }
      return;
    }
    state.originalContent = state.currentContent;
    state.baseMtime = data.mtime;
    $("#save-btn").disabled = true;
    setEditorStatus("✓ guardado — el monitor recargará en 1 s", "ok");
    $("#mtime").textContent = "modificado " + new Date(data.mtime * 1000).toLocaleString();
  };

  $("#save-btn").addEventListener("click", saveConfig);

  // ---------------------------------------------------------------
  // Logs
  // ---------------------------------------------------------------
  const LEVEL_ORDER = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };

  const matchesLevel = (record) => {
    if (state.logLevel === "ALL") return true;
    return LEVEL_ORDER[record.level] >= LEVEL_ORDER[state.logLevel];
  };
  const matchesFilter = (record) => {
    if (!state.logFilter) return true;
    return record.message.toLowerCase().includes(state.logFilter.toLowerCase());
  };
  const matchesKind = (record) => {
    if (!state.logKind) return true;
    return record.extra && record.extra.kind === state.logKind;
  };
  const matchesItem = (record) => {
    if (!state.logItem) return true;
    return record.extra && record.extra.item === state.logItem;
  };
  const matchesScore = (record) => {
    if (state.logMinScore == null) return true;
    const score = record.extra && record.extra.score;
    return typeof score === "number" && score >= state.logMinScore;
  };

  const updateItemDropdown = (record) => {
    const item = record.extra && record.extra.item;
    if (!item || state.knownItems.has(item)) return;
    state.knownItems.add(item);
    const opt = document.createElement("option");
    opt.value = item;
    opt.textContent = item;
    $("#item-filter").appendChild(opt);
  };

  const renderDetail = (record) => {
    const lines = [];
    lines.push(
      `<dl><dt>logger</dt><dd>${esc(record.logger)}</dd>` +
        `<dt>source</dt><dd>${esc(record.location)}</dd></dl>`
    );
    if (record.extra) {
      const extra = record.extra;
      const rows = Object.entries(extra)
        .map(([k, v]) => {
          if (k === "url" && typeof v === "string") {
            return `<dt>${esc(k)}</dt><dd><a href="${esc(v)}" target="_blank" rel="noopener">${esc(v)}</a></dd>`;
          }
          return `<dt>${esc(k)}</dt><dd>${esc(typeof v === "object" ? JSON.stringify(v) : String(v))}</dd>`;
        })
        .join("");
      lines.push(`<dl>${rows}</dl>`);
    }
    if (record.exc_text) {
      lines.push(`<pre>${esc(record.exc_text)}</pre>`);
    }
    return `<div class="log-detail">${lines.join("")}</div>`;
  };

  const renderLogs = () => {
    const container = $("#logs");
    const atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 16;
    const visible = state.records.filter(
      (r) =>
        matchesLevel(r) &&
        matchesFilter(r) &&
        matchesKind(r) &&
        matchesItem(r) &&
        matchesScore(r)
    );
    container.innerHTML = visible
      .map((r) => {
        const expanded = state.expanded.has(r.id);
        const kind = r.extra && r.extra.kind;
        const badge = kind
          ? `<span class="kind-badge kind-${esc(kind)}">${esc(kind.replace(/_/g, " "))}</span>`
          : "";
        return (
          `<div class="log-row level-${esc(r.level)}${expanded ? " expanded" : ""}" data-id="${r.id}">` +
          `<span class="log-time">${esc(r.iso_time)}</span>` +
          `<span class="log-level">${esc(r.level)}</span>` +
          `<span class="log-msg">${badge}${esc(r.message)}</span>` +
          (expanded ? renderDetail(r) : "") +
          `</div>`
        );
      })
      .join("");
    if ($("#autoscroll").checked && (atBottom || state.records.length < 20)) {
      container.scrollTop = container.scrollHeight;
    }
  };

  const esc = (s) =>
    String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");

  $("#logs").addEventListener("click", (e) => {
    const row = e.target.closest(".log-row");
    if (!row) return;
    const id = Number(row.dataset.id);
    if (state.expanded.has(id)) state.expanded.delete(id);
    else state.expanded.add(id);
    renderLogs();
  });

  $$(".level-chips .chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".level-chips .chip").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.logLevel = btn.dataset.level;
      // Clear error badge when user views errors.
      if (btn.dataset.level === "ERROR" || btn.dataset.level === "ALL") {
        state.errorCount = 0;
        renderErrorBadge();
      }
      renderLogs();
    });
  });

  $$(".kind-chips .chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".kind-chips .chip").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.logKind = btn.dataset.kind;
      renderLogs();
    });
  });

  $("#item-filter").addEventListener("change", (e) => {
    state.logItem = e.target.value;
    renderLogs();
  });

  $("#score-filter").addEventListener("change", (e) => {
    const v = e.target.value;
    state.logMinScore = v === "" ? null : Number(v);
    renderLogs();
  });

  $("#log-filter").addEventListener("input", (e) => {
    state.logFilter = e.target.value;
    renderLogs();
  });

  const loadLogs = async () => {
    const res = await (await api("/api/logs?limit=500")).json();
    state.records = res.records;
    state.records.forEach((r) => {
      updateItemDropdown(r);
      noteActivity(r);
    });
    renderLogs();
    renderMonitorStatus();
  };

  // -------- Monitor status chip derived from the log stream --------
  // Track activity timestamp from any log record.
  const noteActivity = (record) => {
    state.lastActivity = record.time;
    // Track error count for the Error tab badge.
    if (record.level === "ERROR" || record.level === "CRITICAL") {
      state.errorCount++;
      renderErrorBadge();
    }
  };

  const formatAgo = (epoch) => {
    if (!epoch) return "—";
    const s = Math.max(0, Math.round(Date.now() / 1000 - epoch));
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    return `${Math.round(s / 3600)}h ago`;
  };

  // Monitor status is purely about process liveness — driven by
  // WebSocket connection state, not log message content.
  const renderMonitorStatus = () => {
    const chip = $("#monitor-status");
    if (!chip) return;
    if (!state.wsConnected) {
      chip.className = "status-chip status-err";
      chip.textContent = "● monitor: desconectado";
      chip.title = "El proceso aimm puede haberse detenido. Reconectando…";
    } else if (!state.lastActivity) {
      chip.className = "status-chip status-warn";
      chip.textContent = "● monitor: conectado";
      chip.title = "Conectado, esperando el primer mensaje de registro.";
    } else {
      const ago = Math.round(Date.now() / 1000 - state.lastActivity);
      if (ago > 300) {
        chip.className = "status-chip status-warn";
        chip.textContent = `● monitor: idle · ${formatAgo(state.lastActivity)}`;
        chip.title = "Conectado pero sin actividad por más de 5 minutos.";
      } else {
        chip.className = "status-chip status-ok";
        chip.textContent = `● monitor: running · ${formatAgo(state.lastActivity)}`;
        chip.title = "El proceso está vivo y activo.";
      }
    }
  };

  // Error badge on the "Error" filter chip in the logs toolbar.
  const renderErrorBadge = () => {
    const errorChip = document.querySelector('.level-chips [data-level="ERROR"]');
    if (!errorChip) return;
    if (state.errorCount > 0) {
      errorChip.dataset.badge = state.errorCount;
      errorChip.classList.add("has-badge");
    } else {
      delete errorChip.dataset.badge;
      errorChip.classList.remove("has-badge");
    }
  };

  // Tick the "Xs ago" display once a second so it stays fresh even
  // without new log records.
  setInterval(renderMonitorStatus, 1000);

  // Restart button — soft-restarts the monitor by touching the config.
  const wireClick = (sel, fn) => {
    const el = $(sel);
    if (el) el.addEventListener("click", fn);
    else console.warn("missing element:", sel);
  };
  wireClick("#restart-btn", async () => {
    const btn = $("#restart-btn");
    if (btn) btn.disabled = true;
    try {
      const res = await api("/api/monitor/restart", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (res.ok) {
        setEditorStatus("▶ Despertando el monitor — buscando todos los ítems ahora…", "ok");
      } else {
        setEditorStatus("▶ Falló: " + (data.detail || "desconocido"), "err");
      }
    } catch (err) {
      setEditorStatus("↻ Falló el reinicio: " + err.message, "err");
    } finally {
      setTimeout(() => { if (btn) btn.disabled = false; }, 2000);
    }
  });

  wireClick("#export-csv-btn", async () => {
    const btn = $("#export-csv-btn");
    if (btn) btn.disabled = true;
    try {
      const res = await api("/api/found.csv");
      if (!res.ok) {
        setEditorStatus("⬇ Falló la exportación: " + res.status, "err");
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const stamp = new Date()
        .toISOString()
        .slice(0, 19)
        .replace(/[-:T]/g, "")
        .replace(/(\d{8})(\d{6})/, "$1-$2");
      const a = document.createElement("a");
      a.href = url;
      a.download = `found-items-${stamp}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setEditorStatus("⬇ Falló la exportación: " + err.message, "err");
    } finally {
      if (btn) btn.disabled = false;
    }
  });

  // ---------------------------------------------------------------
  // Theme
  // ---------------------------------------------------------------
  //
  // The stylesheet defaults to the OS preference; this stamps an explicit
  // choice on <html> which both the config editor and the dashboard read
  // through the same CSS custom properties.
  const THEME_KEY = "aimm_theme";

  const applyTheme = (theme) => {
    if (theme === "light" || theme === "dark") {
      document.documentElement.dataset.theme = theme;
    } else {
      delete document.documentElement.dataset.theme;
    }
    const btn = $("#theme-btn");
    if (btn) btn.title = theme === "dark" ? "Tema oscuro" : theme === "light" ? "Tema claro" : "Seguir el tema del sistema";
  };

  const currentTheme = () => {
    if (document.documentElement.dataset.theme) return document.documentElement.dataset.theme;
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  };

  try {
    applyTheme(localStorage.getItem(THEME_KEY));
  } catch (_) {
    // Private browsing can throw on localStorage; the OS preference still applies.
  }

  wireClick("#theme-btn", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    applyTheme(next);
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch (_) {}
  });

  // ---------------------------------------------------------------
  // View tabs (config editor / analytics dashboard)
  // ---------------------------------------------------------------
  //
  // The dashboard is a separate React bundle under /static/dashboard/. It is
  // imported the first time its tab is opened so the editor's startup path
  // never pays for React, IndexedDB or the listing sync.
  let dashboardLoading = null;

  const showView = (view) => {
    const isDashboard = view === "dashboard";
    const main = document.querySelector("main.main");
    const root = $("#dashboard-root");
    if (main) main.classList.toggle("hidden", isDashboard);
    if (root) root.classList.toggle("hidden", !isDashboard);
    $$(".view-tab").forEach((tab) => {
      const active = tab.dataset.view === view;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    if (!isDashboard || dashboardLoading) return;

    dashboardLoading = import("/static/dashboard/index.js")
      .then((module) => module.mount(root))
      .catch((err) => {
        console.error("dashboard failed to load:", err);
        if (root) {
          root.textContent = "No se pudo cargar el panel: " + (err && err.message ? err.message : err);
        }
        // Allow a retry on the next tab switch rather than wedging the tab.
        dashboardLoading = null;
      });
  };

  $$(".view-tab").forEach((tab) => {
    tab.addEventListener("click", () => showView(tab.dataset.view));
  });

  // ---------------------------------------------------------------
  // Sections sidebar (AI-assisted edit / delete / add)
  // ---------------------------------------------------------------
  //
  // The backend ships a list of section headers found in the file.
  // We render them in a sidebar with a ⋯ menu per section. Clicking
  // the section name scrolls the textarea to that section. No pixel
  // measurement of the textarea is needed.

  state.sections = [];

  const SECTION_HEADER_RE = /^\s*\[([^\]\n]+)\]\s*$/;

  const scanSectionsClient = (text) => {
    const lines = text.split("\n");
    const headers = [];
    for (let i = 0; i < lines.length; i++) {
      const m = lines[i].match(SECTION_HEADER_RE);
      if (m) headers.push({ lineIdx: i, name: m[1].trim() });
    }
    return headers.map((h, i) => {
      const dot = h.name.indexOf(".");
      const lineEnd = i + 1 < headers.length ? headers[i + 1].lineIdx : lines.length;
      return {
        name: h.name,
        prefix: dot >= 0 ? h.name.slice(0, dot) : h.name,
        suffix: dot >= 0 ? h.name.slice(dot + 1) : "",
        line_start: h.lineIdx,
        line_end: lineEnd,
      };
    });
  };

  // -------- Thin gutter with ⋯ buttons aligned to section headers --------

  const getLineMetrics = () => {
    if (editor.defaultTextHeight) {
      // CodeMirror path
      const lineHeight = editor.defaultTextHeight();
      const scrollInfo = editor.getScrollInfo();
      return { lineHeight, paddingTop: 0, scrollHeight: scrollInfo.height };
    }
    // Fallback textarea path
    const el = editorHost.querySelector("textarea");
    if (!el) return { lineHeight: 20, paddingTop: 0, scrollHeight: 0 };
    const cs = window.getComputedStyle(el);
    const lineHeight = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.55;
    const paddingTop = parseFloat(cs.paddingTop) || 0;
    return { lineHeight, paddingTop, scrollHeight: el.scrollHeight };
  };

  const renderGutter = () => {
    const inner = $("#gutter-inner");
    if (!inner) return;
    inner.innerHTML = "";
    const { lineHeight, paddingTop, scrollHeight } = getLineMetrics();
    // Match gutter height to editor scroll height so the transform
    // range is correct.
    inner.style.height = scrollHeight + "px";

    state.sections.forEach((section) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "section-btn";
      btn.innerHTML = "⋯";
      btn.title = `[${section.name}]`;
      btn.style.top = (paddingTop + lineHeight * section.line_start) + "px";
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        toggleSectionMenu(section, btn);
      });
      inner.appendChild(btn);
    });
  };

  // Sync gutter scroll position with the editor.
  const syncGutter = () => {
    const inner = $("#gutter-inner");
    if (inner) inner.style.transform = `translateY(${-editor.getScrollInfo().top}px)`;
  };
  if (editor.on) {
    editor.on("scroll", syncGutter);
    editor.on("change", () => {
      if (refreshSectionsFromBuffer._t) clearTimeout(refreshSectionsFromBuffer._t);
      refreshSectionsFromBuffer._t = setTimeout(refreshSectionsFromBuffer, 150);
    });
  }

  // Re-scan sections from the buffer after edits (debounced).
  const refreshSectionsFromBuffer = () => {
    state.sections = scanSectionsClient(editor.getValue());
    renderGutter();
  };

  // -------- Popover menu (Edit / Delete / Add another) --------

  const closeSectionMenus = () => {
    document.querySelectorAll(".section-menu").forEach((m) => m.remove());
  };

  const toggleSectionMenu = (section, btn) => {
    const existing = document.querySelector(".section-menu");
    if (existing && existing.dataset.section === section.name) {
      existing.remove();
      return;
    }
    closeSectionMenus();

    const menu = document.createElement("div");
    menu.className = "section-menu";
    menu.dataset.section = section.name;

    // Position relative to the button using viewport coordinates
    // (position: fixed in CSS) so the menu can escape the sidebar's
    // overflow clip.
    const rect = btn.getBoundingClientRect();
    menu.style.top = rect.bottom + 4 + "px";
    menu.style.left = rect.left + "px";

    const addMenuItem = (label, handler, cls = "") => {
      const item = document.createElement("button");
      item.type = "button";
      item.textContent = label;
      if (cls) item.className = cls;
      item.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        closeSectionMenus();
        handler();
      });
      menu.appendChild(item);
    };

    addMenuItem("Editar", () => openEditSectionModal(section.name));
    addMenuItem("Duplicar", () => duplicateSection(section));
    const sep = document.createElement("div");
    sep.className = "menu-sep";
    menu.appendChild(sep);
    addMenuItem("Eliminar", () => deleteSection(section), "danger");

    document.body.appendChild(menu);
  };

  // Close popovers when clicking anywhere else.
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".section-btn") && !e.target.closest(".section-menu")) {
      closeSectionMenus();
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeSectionMenus();
  });

  // -------- Delete section (pure client-side string op) --------

  const deleteSection = (section) => {
    const lines = editor.getValue().split("\n");
    let start = section.line_start;
    let end = section.line_end; // exclusive

    const preview = lines.slice(start, end).filter((l) => l.trim()).join("\n");
    const ok = confirm(
      `¿Eliminar [${section.name}]?\n\n` +
        preview +
        "\n\nThis only updates the editor buffer — click Save to commit."
    );
    if (!ok) return;

    // If there's a blank line right before this section, consume it
    // too so we don't leave a double blank.
    if (start > 0 && lines[start - 1].trim() === "") {
      start--;
    }
    const next = lines.slice(0, start).concat(lines.slice(end)).join("\n");
    editor.setValue(next);
    state.currentContent = next;
    const dirty = state.currentContent !== state.originalContent;
    $("#save-btn").disabled = !dirty;
    setEditorStatus(
      `[${section.name}] eliminada — revisa y pulsa Guardar para confirmar.`,
      "ok"
    );
    refreshSectionsFromBuffer();
  };

  // -------- Duplicate section --------
  // Opens the Add form pre-populated with the original section's values
  // and a unique auto-generated name.

  const duplicateSection = (section) => {
    const schema = findFormSchema(section.name);
    if (!schema) {
      alert(`No hay formulario para [${section.name}] — duplícala a mano en el editor TOML.`);
      return;
    }

    // Get the original fields (server-provided or client-parsed).
    let fields = (section && section.fields) || {};
    if (!Object.keys(fields).length && window.tomlEdit) {
      try {
        const parsed = window.tomlEdit.parse(state.currentContent);
        const parts = section.name.split(".");
        let node = parsed;
        for (const p of parts) { node = node && node[p]; }
        if (node && typeof node === "object") fields = node;
      } catch (err) { /* fall through with empty fields */ }
    }

    // Generate a unique suffix: append 1, 2, 3…
    const existingNames = new Set(state.sections.map((s) => s.name));
    let newSuffix = section.suffix;
    let i = 1;
    while (existingNames.has(`${section.prefix}.${newSuffix}`)) {
      newSuffix = section.suffix + i;
      i++;
    }

    formContext = {
      sectionName: `${section.prefix}.__new__`,
      fields,
      schema,
      addMode: true,
      addPrefix: section.prefix,
      nameValue: newSuffix,
    };
    activeTab = "left";
    $("#form-modal-title").textContent = `Duplicar [${section.name}]`;
    $("#form-modal-hint").hidden = false;
    $("#form-modal-hint").textContent =
      "Revisa los valores copiados de la sección original, cambia lo que necesites y guarda.";
    renderForm(schema, fields);
    formModal.open();
    setTimeout(() => $("#add-section-name").focus(), 50);
  };

  // ---------------------------------------------------------------
  // Section form modal (placeholder — full form rendering coming next)
  // ---------------------------------------------------------------
  //
  // The ⋯ menu wires Edit / Add another to the two functions below.
  // For now they pop a minimal placeholder modal so the page doesn't
  // crash. The form-rendering engine that uses toml-edit-js will be
  // wired here in the next iteration.

  // ---------------------------------------------------------------
  // Form-based section editor using toml-edit-js
  // ---------------------------------------------------------------

  // Form field schema definitions per section type. Each field:
  //   key       — TOML key name
  //   label     — display name
  //   type      — "text" | "password" | "number" | "select" | "textarea"
  //   options   — for select: [{value, label}, ...]
  //   required  — boolean
  //   help      — tooltip / small hint
  //   group     — optional group header (for visual grouping)
  //   advanced  — if true, hidden by default

  const BUILT_IN_REGIONS = [
    "usa", "usa_full", "can", "mex", "bra", "arg",
    "aus", "aus_miles", "nzl", "ind", "gbr", "fra", "spa",
  ];

  // override: true means this field can also be set per-item in [item.*]
  // to override the marketplace default.
  const OV = "Se puede sobrescribir por ítem en las secciones [item.*].";

  const CATEGORIES = [
    { value: "", label: "(cualquiera)" },
    { value: "vehicles", label: "Vehículos" },
    { value: "propertyrentals", label: "Arriendos" },
    { value: "apparel", label: "Ropa" },
    { value: "electronics", label: "Electrónica" },
    { value: "entertainment", label: "Entretenimiento" },
    { value: "family", label: "Familia" },
    { value: "freestuff", label: "Cosas gratis" },
    { value: "free", label: "Gratis" },
    { value: "garden", label: "Jardín" },
    { value: "hobbies", label: "Pasatiempos" },
    { value: "homegoods", label: "Artículos para el hogar" },
    { value: "homeimprovement", label: "Mejoras del hogar" },
    { value: "homesales", label: "Venta de propiedades" },
    { value: "musicalinstruments", label: "Instrumentos musicales" },
    { value: "officesupplies", label: "Artículos de oficina" },
    { value: "petsupplies", label: "Artículos para mascotas" },
    { value: "sportinggoods", label: "Artículos deportivos" },
    { value: "tickets", label: "Entradas" },
    { value: "toys", label: "Juguetes" },
    { value: "videogames", label: "Videojuegos" },
  ];

  const FORM_SCHEMAS = {
    "marketplace.facebook": [
      // ---- Left column: Facebook-specific ----
      { key: "username", label: "Usuario de Facebook (correo)", type: "text", column: "left",
        help: "El correo con que inicias sesión en Facebook." },
      { key: "password", label: "Contraseña de Facebook", type: "password", column: "left",
        help: "Déjalo en blanco para conservar la contraseña actual." },
      { key: "login_wait_time", label: "Espera de inicio de sesión (segundos)", type: "number", column: "left",
        help: "Segundos de espera tras el login para 2FA / CAPTCHA. La espera termina apenas la sesión queda activa. Por defecto: 300." },
      { key: "language", label: "Idioma", type: "text", column: "left", advanced: true,
        help: "Idioma de Facebook distinto del inglés — debe coincidir con una sección [translation.*]." },

      // ---- Right column: Shared — can be overridden per-item ----
      { key: "search_city", label: "Ciudad de búsqueda", type: "text", required: true, column: "right",
        help: "Código de ciudad de la URL de Marketplace (minúsculas o ID numérico, ej. 'santiago')." },
      { key: "search_region", label: "Región de búsqueda", type: "select", column: "right",
        options: [{ value: "", label: "(ninguna)" }].concat(
          BUILT_IN_REGIONS.map((r) => ({ value: r, label: r }))
        ),
        help: "Región predefinida (se expande a varias ciudades)." },

      // ---- Filters (advanced, overridable) ----
      { key: "category", label: "Categoría", type: "select", group: "Filtros", advanced: true, column: "right",
        options: CATEGORIES, help: "Categoría de la publicación en Marketplace." },
      { key: "condition", label: "Estado del producto", type: "checkboxes", advanced: true, column: "right",
        options: [
          { value: "new", label: "Nuevo" },
          { value: "used_like_new", label: "Usado — como nuevo" },
          { value: "used_good", label: "Usado — buen estado" },
          { value: "used_fair", label: "Usado — aceptable" },
        ],
        help: "Filtrar por estado del producto. " + OV },
      { key: "availability", label: "Disponibilidad", type: "checkboxes", advanced: true, column: "right",
        options: [
          { value: "all", label: "Todas" },
          { value: "in", label: "Disponible" },
          { value: "out", label: "Sin stock" },
        ] },
      { key: "date_listed", label: "Fecha de publicación", type: "checkboxes", advanced: true, column: "right",
        options: [
          { value: "1", label: "Últimas 24 horas" },
          { value: "7", label: "Últimos 7 días" },
          { value: "30", label: "Últimos 30 días" },
        ] },
      { key: "delivery_method", label: "Método de entrega", type: "checkboxes", advanced: true, column: "right",
        options: [
          { value: "local_pick_up", label: "Retiro en persona" },
          { value: "shipping", label: "Envío" },
        ] },
      { key: "seller_locations", label: "Ubicaciones del vendedor", type: "text", advanced: true, column: "right",
        help: "Nombres de ubicación separados por comas para filtrar." },
      { key: "exclude_sellers", label: "Excluir vendedores", type: "text", advanced: true, column: "right",
        help: "Nombres de vendedores separados por comas que se omitirán." },
      { key: "keywords", label: "Palabras clave (incluir)", type: "text", advanced: true, column: "right",
        help: "Expresión booleana, ej. 'drone AND (DJI OR Orqa)'" },
      { key: "antikeywords", label: "Palabras clave a excluir", type: "text", advanced: true, column: "right",
        help: "Expresión booleana de exclusión." },

      // ---- Pricing ----
      { key: "min_price", label: "Precio mínimo", type: "text", group: "Precio", advanced: true, column: "right",
        help: "ej. '50000' o '50000 CLP'" },
      { key: "max_price", label: "Precio máximo", type: "text", advanced: true, column: "right",
        help: "ej. '300000' o '300000 CLP'" },

      // ---- Location ----
      { key: "radius", label: "Radio de búsqueda (km)", type: "text", group: "Ubicación", advanced: true, column: "right",
        help: "Radio por ciudad separado por comas (debe coincidir con la cantidad de search_city)." },
      { key: "currency", label: "Moneda", type: "text", advanced: true, column: "right",
        help: "Código de moneda por ciudad separado por comas, ej. 'CLP, USD'." },

      // ---- AI evaluation ----
      { key: "ai", label: "Backends de IA", type: "text", group: "Evaluación IA", advanced: true, column: "right",
        help: "Nombres [ai.*] separados por comas." },
      { key: "rating", label: "Umbral de puntaje IA", type: "text", advanced: true, column: "right",
        help: "1–5 (o dos valores: inicial, siguientes)." },
      { key: "prompt", label: "Prompt de IA", type: "textarea", advanced: true, column: "right",
        help: "Prompt de evaluación personalizado (reemplaza el predeterminado)." },
      { key: "extra_prompt", label: "Prompt adicional", type: "textarea", advanced: true, column: "right",
        help: "Texto extra que se añade antes del prompt de puntuación." },
      { key: "rating_prompt", label: "Prompt de puntuación", type: "textarea", advanced: true, column: "right",
        help: "Instrucciones de puntuación propias (reemplazan la escala 1–5)." },

      // ---- Notification ----
      { key: "notify", label: "Notificar a usuarios", type: "text", group: "Notificación", advanced: true, column: "right",
        help: "Nombres [user.*] separados por comas. Por defecto: todos." },

      // ---- Schedule ----
      { key: "search_interval", label: "Intervalo de búsqueda", type: "text", group: "Programación", advanced: true, column: "right",
        help: "Duración, ej. '30m', '1h'. Por defecto: 30 min." },
      { key: "max_search_interval", label: "Intervalo máximo de búsqueda", type: "text", advanced: true, column: "right",
        help: "Límite superior para la variación aleatoria del intervalo." },
      { key: "start_at", label: "Comenzar a las", type: "text", advanced: true, column: "right",
        help: "Patrones de hora separados por comas: 'HH:MM', '*:MM', '*:*:SS'." },
    ],

    // ---- Item form ----
    // Matched by prefix "item" — see the lookup logic below.
    "item.*": [
      // Left: item-specific
      { key: "search_phrases", label: "Frases de búsqueda", type: "text", required: true, column: "left",
        help: "Separadas por comas. Ej. 'playstation 4, ps4 slim'" },
      { key: "description", label: "Descripción (ayuda a la IA)", type: "textarea", column: "left",
        help: "Describe en texto libre lo que buscas. La IA lo usa para evaluar las publicaciones." },
      { key: "marketplace", label: "Marketplace", type: "text", column: "left", advanced: true,
        help: "Qué [marketplace.*] usar. Por defecto: el primero definido." },

      // Right: overrides from marketplace defaults
      { key: "search_city", label: "Ciudad de búsqueda", type: "text", column: "right",
        help: "Sobrescribe la ciudad de búsqueda del marketplace para este ítem." },
      { key: "search_region", label: "Región de búsqueda", type: "select", column: "right",
        options: [{ value: "", label: "(heredar del marketplace)" }].concat(
          BUILT_IN_REGIONS.map((r) => ({ value: r, label: r }))
        ) },
      { key: "min_price", label: "Precio mínimo", type: "text", column: "right",
        help: "ej. '50000' o '50000 CLP'" },
      { key: "max_price", label: "Precio máximo", type: "text", column: "right",
        help: "ej. '300000' o '300000 CLP'" },
      { key: "category", label: "Categoría", type: "select", column: "right", advanced: true,
        options: CATEGORIES },
      { key: "condition", label: "Estado del producto", type: "checkboxes", column: "right", advanced: true,
        options: [
          { value: "new", label: "Nuevo" },
          { value: "used_like_new", label: "Usado — como nuevo" },
          { value: "used_good", label: "Usado — buen estado" },
          { value: "used_fair", label: "Usado — aceptable" },
        ] },
      { key: "availability", label: "Disponibilidad", type: "checkboxes", column: "right", advanced: true,
        options: [
          { value: "all", label: "Todas" },
          { value: "in", label: "Disponible" },
          { value: "out", label: "Sin stock" },
        ] },
      { key: "date_listed", label: "Fecha de publicación", type: "checkboxes", column: "right", advanced: true,
        options: [
          { value: "1", label: "Últimas 24 horas" },
          { value: "7", label: "Últimos 7 días" },
          { value: "30", label: "Últimos 30 días" },
        ] },
      { key: "delivery_method", label: "Método de entrega", type: "checkboxes", column: "right", advanced: true,
        options: [
          { value: "local_pick_up", label: "Retiro en persona" },
          { value: "shipping", label: "Envío" },
        ] },
      { key: "keywords", label: "Palabras clave (incluir)", type: "text", column: "right", advanced: true,
        help: "Expresión booleana, ej. 'drone AND (DJI OR Orqa)'" },
      { key: "antikeywords", label: "Palabras clave a excluir", type: "text", column: "right", advanced: true },
      { key: "seller_locations", label: "Ubicaciones del vendedor", type: "text", column: "right", advanced: true,
        help: "Separados por comas." },
      { key: "exclude_sellers", label: "Excluir vendedores", type: "text", column: "right", advanced: true },
      { key: "notify", label: "Notificar a usuarios", type: "text", column: "right", advanced: true,
        help: "Nombres [user.*] separados por comas. Por defecto: hereda del marketplace." },
      { key: "ai", label: "Backends de IA", type: "text", group: "AI", column: "right", advanced: true },
      { key: "rating", label: "Umbral de puntaje IA", type: "text", column: "right", advanced: true,
        help: "1–5 (o inicial,siguientes)." },
      { key: "prompt", label: "Prompt de IA", type: "textarea", column: "right", advanced: true },
      { key: "extra_prompt", label: "Prompt adicional", type: "textarea", column: "right", advanced: true },
      { key: "rating_prompt", label: "Prompt de puntuación", type: "textarea", column: "right", advanced: true },
      { key: "search_interval", label: "Intervalo de búsqueda", type: "text", group: "Programación", column: "right", advanced: true,
        help: "Duración, ej. '30m', '1h'." },
      { key: "max_search_interval", label: "Intervalo máximo de búsqueda", type: "text", column: "right", advanced: true },
      { key: "start_at", label: "Comenzar a las", type: "text", column: "right", advanced: true,
        help: "Patrones de hora separados por comas." },
    ],

    // ---- User form ----
    "user.*": [
      { key: "pushbullet_token", label: "Token de Pushbullet", type: "password",
        help: "Obtén tu token en pushbullet.com → Settings → Access tokens." },
      { key: "pushover_user_key", label: "Clave de usuario de Pushover", type: "password", group: "Pushover" },
      { key: "pushover_api_token", label: "Token de API de Pushover", type: "password" },
      { key: "telegram_token", label: "Token del bot de Telegram", type: "password", group: "Telegram",
        help: "Formato: 123456789:ABCdef..." },
      { key: "telegram_chat_id", label: "ID de chat de Telegram", type: "text",
        help: "ID numérico o @usuario." },
      { key: "ntfy_server", label: "URL del servidor ntfy", type: "text", group: "ntfy",
        help: "e.g. https://ntfy.sh" },
      { key: "ntfy_topic", label: "Tema de ntfy", type: "text" },
      { key: "email", label: "Correo electrónico", type: "text", group: "Correo",
        help: "Lista de destinatarios separados por comas." },
      { key: "smtp_server", label: "Servidor SMTP", type: "text", advanced: true },
      { key: "smtp_port", label: "Puerto SMTP", type: "number", advanced: true,
        help: "Por defecto: 587" },
      { key: "smtp_username", label: "Usuario SMTP", type: "text", advanced: true },
      { key: "smtp_password", label: "Contraseña SMTP (contraseña de aplicación)", type: "password", advanced: true },
      { key: "smtp_from", label: "Remitente SMTP", type: "text", advanced: true },
      { key: "notify_with", label: "Secciones de notificación", type: "text", group: "Otros", advanced: true,
        help: "Secciones [notification.*] separadas por comas, para credenciales compartidas." },
      { key: "remind", label: "Intervalo de recordatorio", type: "text", advanced: true,
        help: "Reenviar tras este intervalo, ej. '1d', '6h'. Por defecto: una sola vez." },
    ],

    // ---- AI backend form ----
    "ai.*": [
      { key: "api_key", label: "Clave de API", type: "password",
        help: "Si lo dejas en blanco se usa la variable de entorno del proveedor (ej. ${OPENAI_API_KEY}, ${ANTHROPIC_API_KEY}, ${DEEPSEEK_API_KEY})." },
      { key: "model", label: "Modelo", type: "text",
        help: "e.g. 'gpt-4o', 'deepseek-chat', 'deepseek-r1:14b', 'claude-sonnet-4-20250514'" },
      { key: "provider", label: "Proveedor (sobrescribir)", type: "text", advanced: true,
        help: "Sobrescribe el proveedor (se detecta del nombre de la sección). Sólo hace falta para endpoints propios compatibles con OpenAI." },
      { key: "base_url", label: "URL base", type: "text", advanced: true,
        help: "Endpoint de API propio. Obligatorio para Ollama (ej. http://localhost:11434/v1)." },
      { key: "timeout", label: "Tiempo límite (segundos)", type: "number", advanced: true },
      { key: "max_retries", label: "Reintentos máximos", type: "number", advanced: true,
        help: "Por defecto: 10" },
    ],
  };

  // Look up a schema for a section name. Exact match first, then
  // prefix-wildcard (e.g. "item.gopro" → "item.*").
  const findFormSchema = (sectionName) => {
    if (FORM_SCHEMAS[sectionName]) return FORM_SCHEMAS[sectionName];
    const dot = sectionName.indexOf(".");
    if (dot >= 0) {
      const wildcard = sectionName.slice(0, dot) + ".*";
      if (FORM_SCHEMAS[wildcard]) return FORM_SCHEMAS[wildcard];
    }
    return null;
  };

  // Tracks which section is currently being edited.
  let formContext = { sectionName: "", fields: {}, schema: [] };
  let showAdvanced = false;

  const formModal = {
    el: () => $("#form-modal"),
    open() { this.el().classList.remove("hidden"); },
    close() {
      this.el().classList.add("hidden");
      $("#form-error").hidden = true;
      const form = $("#section-form");
      if (form) form.innerHTML = "";
    },
  };

  // Which tab is selected (for two-tab forms).
  let activeTab = "left";

  // Render form fields into #section-form.
  const renderForm = (schema, fields) => {
    const form = $("#section-form");
    form.innerHTML = "";

    // Always render the section name field first. In edit mode it shows
    // the current suffix (editable for rename); in add/duplicate mode
    // it shows the suggested new name.
    const currentPrefix = formContext.addMode ? formContext.addPrefix : formContext.sectionName.split(".")[0];
    // For AI sections, show a dropdown of known providers instead of a
    // free-text name input.
    const aiAutoName = currentPrefix === "ai";
    const nameWrapper = document.createElement("div");
    nameWrapper.className = "form-field";
    const currentSuffix = formContext.nameValue ??
      (formContext.addMode ? "" : (formContext.sectionName.split(".").slice(1).join(".") || formContext.sectionName));
    if (aiAutoName) {
      const aiProviders = [
        { value: "openai", label: "OpenAI" },
        { value: "deepseek", label: "DeepSeek" },
        { value: "anthropic", label: "Anthropic" },
        { value: "ollama", label: "Ollama" },
      ];
      const opts = aiProviders.map((p) =>
        `<option value="${p.value}" ${currentSuffix === p.value ? "selected" : ""}>${p.label}</option>`
      ).join("");
      nameWrapper.innerHTML =
        `<label class="form-label">Proveedor de IA <span class="required">*</span></label>` +
        `<select id="add-section-name">${opts}</select>` +
        `<p class="form-help">[ai.<em>provider</em>]</p>`;
      const nameSelect = nameWrapper.querySelector("select");
      nameSelect.addEventListener("change", () => { formContext.nameValue = nameSelect.value; });
      // Set initial value.
      if (!currentSuffix) {
        formContext.nameValue = nameSelect.value;
      }
    } else {
      nameWrapper.innerHTML =
        `<label class="form-label">Nombre de la sección <span class="required">*</span></label>` +
        `<input type="text" id="add-section-name" value="${esc(currentSuffix)}" ` +
        `placeholder="ej. gopro, yo" />` +
        `<p class="form-help">[${esc(currentPrefix)}.<em>name</em>]</p>`;
      const nameInput = nameWrapper.querySelector("input");
      nameInput.addEventListener("input", () => { formContext.nameValue = nameInput.value; });
    }
    form.appendChild(nameWrapper);

    const hasColumns = schema.some((f) => f.column);
    const hasAdvanced = schema.some((f) => f.advanced);

    // If the schema uses columns, render tabs.
    if (hasColumns) {
      // Choose tab labels based on what kind of section we're editing.
      const prefix = formContext.sectionName.split(".")[0];
      const leftLabel =
        prefix === "marketplace" ? "Acceso a Facebook" : "Opciones del ítem";
      const rightLabel =
        prefix === "marketplace"
          ? "Búsqueda por defecto (sobrescribible por ítem)"
          : "Sobrescribir los valores del marketplace";

      const tabBar = document.createElement("div");
      tabBar.className = "form-tab-bar";
      const leftBtn = document.createElement("button");
      leftBtn.type = "button";
      leftBtn.className = "form-tab" + (activeTab === "left" ? " active" : "");
      leftBtn.textContent = leftLabel;
      leftBtn.addEventListener("click", () => { activeTab = "left"; renderForm(schema, fields); });
      const rightBtn = document.createElement("button");
      rightBtn.type = "button";
      rightBtn.className = "form-tab" + (activeTab === "right" ? " active" : "");
      rightBtn.textContent = rightLabel;
      rightBtn.addEventListener("click", () => { activeTab = "right"; renderForm(schema, fields); });
      tabBar.appendChild(leftBtn);
      tabBar.appendChild(rightBtn);
      form.appendChild(tabBar);
    }

    // Toggle for advanced fields.
    const visibleFields = hasColumns
      ? schema.filter((f) => (f.column || "left") === activeTab)
      : schema;
    const tabHasAdvanced = visibleFields.some((f) => f.advanced);
    if (tabHasAdvanced) {
      const toggle = document.createElement("label");
      toggle.className = "form-label";
      toggle.style.cursor = "pointer";
      toggle.innerHTML =
        `<input type="checkbox" id="show-advanced" ${showAdvanced ? "checked" : ""} /> ` +
        `Mostrar campos avanzados`;
      toggle.querySelector("input").addEventListener("change", (e) => {
        showAdvanced = e.target.checked;
        renderForm(schema, fields);
      });
      form.appendChild(toggle);
    }

    let lastGroup = null;
    visibleFields.forEach((fieldDef) => {
      if (fieldDef.advanced && !showAdvanced) return;

      // Group header.
      if (fieldDef.group && fieldDef.group !== lastGroup) {
        lastGroup = fieldDef.group;
        const groupEl = document.createElement("div");
        groupEl.className = "form-group-title";
        groupEl.textContent = fieldDef.group;
        form.appendChild(groupEl);
      }

      const wrapper = document.createElement("div");
      wrapper.className = "form-field";

      const label = document.createElement("label");
      label.className = "form-label";
      label.innerHTML =
        esc(fieldDef.label) +
        (fieldDef.required
          ? ' <span class="required">*</span>'
          : ' <span class="optional">opcional</span>');
      wrapper.appendChild(label);

      let input;
      const rawVal = fields[fieldDef.key];
      // Flatten arrays to comma-separated for text fields.
      const currentVal =
        Array.isArray(rawVal) ? rawVal.join(", ") : rawVal ?? "";
      // For checkboxes, track which values are currently selected.
      const checkedSet = new Set(
        Array.isArray(rawVal) ? rawVal.map(String) : currentVal ? [String(currentVal)] : []
      );

      if (fieldDef.type === "checkboxes") {
        // Render a group of checkboxes for multi-value fields.
        input = document.createElement("div");
        input.className = "checkboxes";
        input.dataset.key = fieldDef.key;
        (fieldDef.options || []).forEach((opt) => {
          const cb = document.createElement("input");
          cb.type = "checkbox";
          cb.value = opt.value;
          cb.checked = checkedSet.has(String(opt.value));
          cb.id = `field-${fieldDef.key}-${opt.value}`;
          const lbl = document.createElement("label");
          lbl.htmlFor = cb.id;
          lbl.appendChild(cb);
          lbl.append(` ${opt.label}`);
          input.appendChild(lbl);
        });
      } else if (fieldDef.type === "select") {
        input = document.createElement("select");
        (fieldDef.options || []).forEach((opt) => {
          const o = document.createElement("option");
          o.value = opt.value;
          o.textContent = opt.label;
          if (String(currentVal) === String(opt.value)) o.selected = true;
          input.appendChild(o);
        });
      } else if (fieldDef.type === "textarea") {
        input = document.createElement("textarea");
        input.rows = 3;
        input.value = currentVal;
      } else {
        input = document.createElement("input");
        input.type = fieldDef.type || "text";
        // For password fields with <REDACTED>, show placeholder instead.
        if (fieldDef.type === "password" && String(currentVal) === "<REDACTED>") {
          input.value = "";
          input.placeholder = "(unchanged — leave blank to keep current)";
        } else {
          input.value = currentVal;
        }
        if (fieldDef.type === "number") {
          input.min = "0";
          input.step = "1";
        }
      }
      if (fieldDef.type !== "checkboxes") {
        input.name = fieldDef.key;
        input.dataset.key = fieldDef.key;
        label.htmlFor = fieldDef.key;
        input.id = "field-" + fieldDef.key;
      }
      wrapper.appendChild(input);

      if (fieldDef.help) {
        const help = document.createElement("p");
        help.className = "form-help";
        help.textContent = fieldDef.help;
        wrapper.appendChild(help);
      }
      form.appendChild(wrapper);
    });

    // For AI sections in add mode, set the API key to the env var
    // reference matching the selected provider.
    if (aiAutoName && formContext.addMode) {
      const envVarMap = {
        openai: "${OPENAI_API_KEY}",
        deepseek: "${DEEPSEEK_API_KEY}",
        anthropic: "${ANTHROPIC_API_KEY}",
        ollama: "${OLLAMA_API_KEY}",
      };
      const nameSelect = $("#add-section-name");
      const apiKeyInput = form.querySelector('[data-key="api_key"]');
      if (nameSelect && apiKeyInput) {
        const syncApiKey = () => {
          const envRef = envVarMap[nameSelect.value] || "";
          // Only auto-fill if the user hasn't typed something custom.
          if (!apiKeyInput.value || apiKeyInput.value.startsWith("${")) {
            apiKeyInput.value = envRef;
          }
        };
        nameSelect.addEventListener("change", syncApiKey);
        syncApiKey();
      }
    }

  };

  // Collect form field values into a {key: coerced_value} dict.
  const collectFormValues = () => {
    const form = $("#section-form");
    const errors = [];
    const values = {};

    formContext.schema.forEach((fieldDef) => {
      if (fieldDef.advanced && !showAdvanced) return;
      const input = form.querySelector(`[data-key="${fieldDef.key}"]`);
      if (!input) return;

      let newVal;
      if (fieldDef.type === "checkboxes") {
        const checked = Array.from(input.querySelectorAll("input:checked")).map(
          (cb) => cb.value
        );
        newVal = checked.length ? checked.join(", ") : "";
      } else {
        newVal = input.value.trim();
      }

      if (fieldDef.required && !newVal) {
        errors.push(`${fieldDef.label} is required.`);
        return;
      }
      if (!newVal) return;
      if (fieldDef.type === "password" && !newVal) return;

      // Type coercion.
      let value;
      if (fieldDef.type === "number" && newVal) {
        value = parseInt(newVal, 10);
        if (isNaN(value)) { errors.push(`${fieldDef.label} must be a number.`); return; }
      } else if (newVal.includes(",") && fieldDef.type === "text") {
        const original = formContext.fields[fieldDef.key];
        if (Array.isArray(original) || newVal.includes(",")) {
          value = newVal.split(",").map((s) => s.trim()).filter(Boolean);
        } else {
          value = newVal;
        }
      } else {
        value = newVal;
      }
      values[fieldDef.key] = value;
    });
    return { values, errors };
  };

  // Generate a TOML section block as text for "add" mode.
  const generateSectionToml = (sectionFullName, values) => {
    const lines = [`[${sectionFullName}]`];
    for (const [key, val] of Object.entries(values)) {
      if (Array.isArray(val)) {
        const items = val.map((v) =>
          typeof v === "number" ? String(v) : `"${String(v).replace(/"/g, '\\"')}"`
        );
        lines.push(`${key} = [${items.join(", ")}]`);
      } else if (typeof val === "number") {
        lines.push(`${key} = ${val}`);
      } else if (typeof val === "boolean") {
        lines.push(`${key} = ${val}`);
      } else {
        lines.push(`${key} = "${String(val).replace(/"/g, '\\"')}"`);
      }
    }
    return lines.join("\n") + "\n";
  };

  // Save handler — works for both edit and add modes.
  const saveForm = async () => {
    const form = $("#section-form");
    const { values, errors } = collectFormValues();

    // ---- Add mode: generate a new section block and append ----
    if (formContext.addMode) {
      const nameInput = $("#add-section-name");
      const sectionSuffix = (nameInput ? nameInput.value.trim() : "").replace(/[^a-zA-Z0-9_\-]/g, "_");
      if (!sectionSuffix) {
        errors.push("Section name is required.");
      }
      if (errors.length) {
        $("#form-error").textContent = errors.join(" ");
        $("#form-error").hidden = false;
        return;
      }

      const fullName = `${formContext.addPrefix}.${sectionSuffix}`;
      // Check for duplicate.
      if (state.sections.some((s) => s.name === fullName)) {
        $("#form-error").textContent = `Section [${fullName}] already exists.`;
        $("#form-error").hidden = false;
        return;
      }

      const block = generateSectionToml(fullName, values);
      let buffer = state.currentContent;
      // Append after the last section of the same type, or at end.
      const samePrefixSections = state.sections.filter(
        (s) => s.prefix === formContext.addPrefix
      );
      if (samePrefixSections.length) {
        const last = samePrefixSections[samePrefixSections.length - 1];
        const lines = buffer.split("\n");
        const insertAt = last.line_end;
        lines.splice(insertAt, 0, "", ...block.split("\n"));
        buffer = lines.join("\n");
      } else {
        buffer = buffer.replace(/\n*$/, "") + "\n\n" + block;
      }

      editor.setValue(buffer);
      state.currentContent = buffer;
      const dirty = state.currentContent !== state.originalContent;
      $("#save-btn").disabled = !dirty;
      refreshSectionsFromBuffer();
      formModal.close();
      if (dirty) await saveConfig();
      return;
    }

    // ---- Edit mode ----
    // Check if the user renamed the section.
    const nameInput = $("#add-section-name");
    const newSuffix = nameInput ? nameInput.value.trim().replace(/[^a-zA-Z0-9_\-]/g, "_") : "";
    if (!newSuffix) {
      errors.push("Section name is required.");
    }
    const prefix = formContext.sectionName.split(".")[0];
    const newFullName = prefix + "." + newSuffix;
    const renamed = newFullName !== formContext.sectionName;

    if (renamed && state.sections.some((s) => s.name === newFullName)) {
      errors.push(`Section [${newFullName}] already exists.`);
    }
    if (errors.length) {
      $("#form-error").textContent = errors.join(" ");
      $("#form-error").hidden = false;
      return;
    }

    // For rename: delete the old section, then generate a fresh block
    // with the new name + all form values. This avoids fragile line-
    // patching and reuses the same code path as "add mode".
    if (renamed) {
      const section = state.sections.find((s) => s.name === formContext.sectionName);
      if (section) {
        const lines = state.currentContent.split("\n");
        let start = section.line_start;
        if (start > 0 && lines[start - 1].trim() === "") start--;
        const after = lines.slice(0, start).concat(lines.slice(section.line_end));
        state.currentContent = after.join("\n");
      }
      // Now append the new section (same logic as add mode).
      const block = generateSectionToml(newFullName, values);
      let buffer = state.currentContent;
      const samePrefixSections = scanSectionsClient(buffer).filter(
        (s) => s.prefix === prefix
      );
      if (samePrefixSections.length) {
        const last = samePrefixSections[samePrefixSections.length - 1];
        const lines = buffer.split("\n");
        lines.splice(last.line_end, 0, "", ...block.split("\n"));
        buffer = lines.join("\n");
      } else {
        buffer = buffer.replace(/\n*$/, "") + "\n\n" + block;
      }
      editor.setValue(buffer);
      state.currentContent = buffer;
    } else {
      // No rename — patch fields in place via tomlEdit.edit().
      if (!window.tomlEdit) {
        $("#form-error").textContent =
          "TOML editor library failed to load — edit the TOML directly.";
        $("#form-error").hidden = false;
        return;
      }
      let buffer = state.currentContent;
      const editErrors = [];
      formContext.schema.forEach((fieldDef) => {
        if (fieldDef.advanced && !showAdvanced) return;
        if (fieldDef.key in values) {
          try {
            buffer = window.tomlEdit.edit(
              buffer, formContext.sectionName + "." + fieldDef.key, values[fieldDef.key]
            );
          } catch (err) {
            editErrors.push(`Failed to set ${fieldDef.key}: ${err.message}`);
          }
        }
      });
      if (editErrors.length) {
        $("#form-error").textContent = editErrors.join(" ");
        $("#form-error").hidden = false;
        return;
      }
      editor.setValue(buffer);
      state.currentContent = buffer;
    }

    const dirty = state.currentContent !== state.originalContent;
    $("#save-btn").disabled = !dirty;
    refreshSectionsFromBuffer();
    formModal.close();
    if (dirty) await saveConfig();
  };

  // Open the Edit form for a specific section.
  const openEditSectionModal = (sectionName) => {
    // Find the section in state.sections (populated from the server
    // or the client-side scanner).
    const section = state.sections.find((s) => s.name === sectionName);
    let fields = (section && section.fields) || {};

    // If the server didn't provide parsed fields (e.g. aimm wasn't
    // restarted), try parsing the textarea content with tomlEdit.
    if (!Object.keys(fields).length && window.tomlEdit) {
      try {
        const parsed = window.tomlEdit.parse(state.currentContent);
        // Navigate the nested dict: "marketplace.facebook" → parsed.marketplace.facebook
        const parts = sectionName.split(".");
        let node = parsed;
        for (const p of parts) { node = node && node[p]; }
        if (node && typeof node === "object") fields = node;
      } catch (err) {
        console.warn("tomlEdit.parse failed for form:", err);
      }
    }

    // Look up the schema. If we don't have one for this section type,
    // show a "raw TOML only" message.
    const schema = findFormSchema(sectionName);
    if (!schema) {
      $("#form-modal-title").textContent = `Editar [${sectionName}]`;
      $("#form-modal-hint").hidden = false;
      $("#form-modal-hint").textContent =
        `Aún no hay formulario para [${sectionName}] — edita el TOML directamente ` +
        "en el editor.";
      $("#section-form").innerHTML = "";
      formModal.open();
      return;
    }

    const dot = sectionName.indexOf(".");
    const suffix = dot >= 0 ? sectionName.slice(dot + 1) : sectionName;
    formContext = { sectionName, fields, schema, nameValue: suffix };
    $("#form-modal-title").textContent = `Editar [${sectionName}]`;
    $("#form-modal-hint").hidden = true;
    renderForm(schema, fields);
    formModal.open();
  };

  // Open form in "add" mode: empty fields + a name input at top.
  const openAddSectionModal = (prefix) => {
    const schema = findFormSchema(prefix + ".*") || findFormSchema(prefix + ".facebook");
    if (!schema) {
      alert(`No form defined for [${prefix}.*] — add it manually in the TOML editor.`);
      return;
    }
    // Build a placeholder section name from the prefix.
    const existingNames = state.sections
      .filter((s) => s.prefix === prefix)
      .map((s) => s.suffix);
    let suggestedName = prefix === "marketplace" ? "facebook" : "";

    formContext = {
      sectionName: `${prefix}.__new__`,
      fields: {},
      schema,
      addMode: true,
      addPrefix: prefix,
      nameValue: suggestedName,
    };
    activeTab = "left";
    $("#form-modal-title").textContent = `Agregar una sección [${prefix}.*]`;
    $("#form-modal-hint").hidden = false;
    $("#form-modal-hint").textContent =
      "Elige un nombre y completa los campos. La nueva sección se agregará al final de tu configuración.";
    renderForm(schema, {});
    formModal.open();
    setTimeout(() => {
      const nameInput = $("#add-section-name");
      if (nameInput && !nameInput.value) nameInput.focus();
    }, 50);
  };

  wireClick("#form-modal-close", () => formModal.close());
  wireClick("#form-cancel", () => formModal.close());
  wireClick("#form-save", () => saveForm());
  const backdrop = document.querySelector("#form-modal .modal-backdrop");
  if (backdrop) backdrop.addEventListener("click", () => formModal.close());

  // -------- "+ Add" dropdown in the header --------
  wireClick("#add-btn", () => {
    const menu = $("#add-menu");
    if (menu) menu.classList.toggle("hidden");
  });
  // Close dropdown when clicking outside.
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#add-dropdown")) {
      const menu = $("#add-menu");
      if (menu) menu.classList.add("hidden");
    }
  });
  // Wire each menu item to openAddSectionModal.
  document.querySelectorAll("#add-menu button[data-prefix]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const menu = $("#add-menu");
      if (menu) menu.classList.add("hidden");
      openAddSectionModal(btn.dataset.prefix);
    });
  });

  const connectWs = () => {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/stream`);
    state.ws = ws;
    ws.onopen = () => {
      state.wsConnected = true;
      $("#ws-status").textContent = "● streaming";
      renderMonitorStatus();
    };
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "log") {
        state.records.push(msg.record);
        updateItemDropdown(msg.record);
        noteActivity(msg.record);
        if (state.records.length > 5000) state.records.shift();
        renderLogs();
        renderMonitorStatus();
      }
    };
    ws.onclose = () => {
      state.wsConnected = false;
      $("#ws-status").textContent = "● disconnected — retrying…";
      renderMonitorStatus();
      setTimeout(connectWs, 2000);
    };
    ws.onerror = () => {
      ws.close();
    };
  };

  // ---------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------
  const bootstrap = async () => {
    try {
      await loadConfig();
      // CodeMirror needs a refresh after becoming visible (the editor
      // host is hidden during the login screen).
      if (editor.refresh) editor.refresh();
      await loadLogs();
      connectWs();
    } catch (err) {
      console.error(err);
    }
  };

  // If we already have a session cookie from a prior visit, try bootstrapping.
  (async () => {
    try {
      const res = await fetch("/api/status", { credentials: "same-origin" });
      if (res.ok) {
        state.csrf = getCookie("aimm_csrf");
        try {
          const status = await res.clone().json();
          const browserBtn = document.getElementById("browser-btn");
          if (browserBtn && status && status.vnc_enabled) browserBtn.hidden = false;
        } catch (_) {}
        hideLogin();
        await bootstrap();
      } else {
        showLogin();
      }
    } catch (err) {
      showLogin();
    }
  })();
})();
