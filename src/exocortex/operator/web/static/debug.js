// debug.js — failure triage page (v2 with side panel)
//
// Layout:
//   sidebar (kinds + counts) | main (filter chips + list of failures) | side panel (slides in from right with full event detail)
//
// Click a row in the list -> side panel slides in with: event meta,
// payload JSON, preceding-events, hints. List doesn't shift; rows
// stay where they were. Esc / × button / outside click closes.
//
// Endpoints:
//   GET  /api/debug/failures?limit=200&kind=*&agent=*&since_ms=N
//   GET  /api/debug/failures/{event_id}/context
//
(function () {
  "use strict";

  // -----------------------------------------------------------------
  // Tiny DOM helpers
  // -----------------------------------------------------------------

  const $ = (id) => document.getElementById(id);

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const k of Object.keys(attrs)) {
        const v = attrs[k];
        if (v == null) continue;
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k === "html") node.innerHTML = v;
        else if (k === "style" && typeof v === "object") {
          for (const s of Object.keys(v)) node.style[s] = v[s];
        } else if (k.startsWith("on") && typeof v === "function") {
          node.addEventListener(k.slice(2).toLowerCase(), v);
        } else {
          node.setAttribute(k, String(v));
        }
      }
    }
    if (Array.isArray(children)) {
      for (const c of children) {
        if (c == null) continue;
        node.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
      }
    }
    return node;
  }

  // -----------------------------------------------------------------
  // State + persistence
  // -----------------------------------------------------------------

  const STATE_KEY = "exocortex.debug.v2";

  const KIND_DEFS = [
    { kind: "*",                  label: "all failures",   sev: "high",   group: "_TOP" },
    { kind: "dispatch.failed",    label: "dispatch failed", sev: "high",  group: "FAILURES" },
    { kind: "task.failed",        label: "task failed",     sev: "high",  group: "FAILURES" },
    { kind: "tool.rejected",      label: "tool rejected",   sev: "medium", group: "ATTENTION" },
    { kind: "approval.requested", label: "approvals",       sev: "low",   group: "ATTENTION" },
  ];

  const defaultPersisted = {
    selectedKind: "*",
    rangeMs: 0,
    agentFilter: "all",
  };

  function loadPersisted() {
    try {
      const raw = localStorage.getItem(STATE_KEY);
      if (!raw) return Object.assign({}, defaultPersisted);
      return Object.assign({}, defaultPersisted, JSON.parse(raw) || {});
    } catch (_) { return Object.assign({}, defaultPersisted); }
  }
  function savePersisted() {
    try { localStorage.setItem(STATE_KEY, JSON.stringify(persisted)); } catch (_) {}
  }
  let persisted = loadPersisted();

  const state = {
    failures: [],
    serverCounts: null,
    available: true,
    counts: new Map(),
    knownAgents: new Set(),
    contextCache: new Map(),
    selectedEventId: null,   // currently open in side panel
  };

  // -----------------------------------------------------------------
  // Formatting
  // -----------------------------------------------------------------

  function fmtTimeFromMs(ms) {
    if (!ms) return "—";
    const d = new Date(ms);
    const h = String(d.getHours()).padStart(2, "0");
    const m = String(d.getMinutes()).padStart(2, "0");
    const s = String(d.getSeconds()).padStart(2, "0");
    return `${h}:${m}:${s}`;
  }

  function fmtFullTimeFromMs(ms) {
    if (!ms) return "—";
    const d = new Date(ms);
    return d.toLocaleString();
  }

  function fmtRelative(ms) {
    if (!ms) return "";
    const delta = Date.now() - ms;
    const s = Math.max(1, Math.floor(delta / 1000));
    if (s < 60) return `${s}s ago`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ago`;
    const d = Math.floor(h / 24);
    return `${d}d ago`;
  }

  function truncate(s, n) {
    if (!s) return "";
    if (s.length <= n) return s;
    return s.slice(0, n - 1) + "…";
  }

  function severityFor(f) {
    if (f.severity) return f.severity;
    if (f.kind === "dispatch.failed" || f.kind === "task.failed") return "high";
    if (f.kind === "tool.rejected") return "medium";
    return "low";
  }

  function highlightJson(obj) {
    let json;
    try { json = JSON.stringify(obj, null, 2); }
    catch (_) { return ""; }
    json = json.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    return json.replace(
      /("(?:[^"\\]|\\.)*")(\s*:)?|\b(true|false|null)\b|\b(-?\d+(?:\.\d+)?)\b/g,
      (m, str, colon, bool, num) => {
        if (str) {
          const cls = colon ? "json-k" : "json-s";
          return `<span class="${cls}">${str}</span>${colon || ""}`;
        }
        if (bool) return `<span class="json-b">${bool}</span>`;
        if (num) return `<span class="json-n">${num}</span>`;
        return m;
      }
    );
  }

  // -----------------------------------------------------------------
  // Fetching
  // -----------------------------------------------------------------

  async function fetchJSON(url) {
    try {
      const r = await fetch(url);
      const ct = r.headers.get("content-type") || "";
      if (!r.ok) return { _status: r.status };
      if (!ct.includes("application/json")) return { _status: r.status };
      const j = await r.json();
      j._status = r.status;
      return j;
    } catch (_) { return { _status: 0 }; }
  }

  function normalizeFailure(item) {
    return {
      event_id: item.event_id,
      kind: item.kind || "",
      agent_id: item.agent_id || "",
      timestamp_ms: item.timestamp_ms || (item.timestamp ? Date.parse(item.timestamp) : 0),
      task_id: item.task_id || "",
      session_id: item.session_id || "",
      payload_preview: item.payload_preview || "",
      severity: item.severity || null,
    };
  }

  async function fetchFailures() {
    const params = new URLSearchParams({ limit: "200" });
    params.set("kind", persisted.selectedKind || "*");
    if (persisted.agentFilter && persisted.agentFilter !== "all") {
      params.set("agent", persisted.agentFilter);
    }
    if (persisted.rangeMs > 0) {
      params.set("since_ms", String(Date.now() - persisted.rangeMs));
    }
    const res = await fetchJSON("/api/debug/failures?" + params.toString());
    if (res._status === 404 || res._status === 503 || res._status === 0) {
      state.available = false;
      state.failures = [];
      renderAll();
      return;
    }
    state.available = true;
    state.failures = (res.items || []).map(normalizeFailure);
    state.serverCounts = res.counts_by_kind || null;
    for (const f of state.failures) if (f.agent_id) state.knownAgents.add(f.agent_id);
    renderAll();
  }

  async function fetchContext(eventId) {
    if (state.contextCache.has(eventId)) return state.contextCache.get(eventId);
    const res = await fetchJSON(
      "/api/debug/failures/" + encodeURIComponent(eventId) + "/context"
    );
    if (res._status === 404 || res._status === 503 || res._status === 0) {
      const ctx = { available: false };
      state.contextCache.set(eventId, ctx);
      return ctx;
    }
    const ctx = {
      available: true,
      event: res.event || null,
      preceding: res.preceding || [],
      hints: res.hints || [],
    };
    state.contextCache.set(eventId, ctx);
    return ctx;
  }

  // -----------------------------------------------------------------
  // Rendering: KPIs + sidebar + list
  // -----------------------------------------------------------------

  function applyFilters(items) {
    const now = Date.now();
    const cutoff = persisted.rangeMs > 0 ? now - persisted.rangeMs : 0;
    const af = persisted.agentFilter;
    const out = [];
    for (const f of items) {
      if (cutoff && f.timestamp_ms < cutoff) continue;
      if (af !== "all" && f.agent_id !== af) continue;
      out.push(f);
    }
    out.sort((a, b) => (b.timestamp_ms || 0) - (a.timestamp_ms || 0));
    return out;
  }

  function computeCounts() {
    state.counts.clear();
    if (state.serverCounts) {
      let total = 0;
      for (const k of Object.keys(state.serverCounts)) {
        const v = state.serverCounts[k] || 0;
        state.counts.set(k, v);
        total += v;
      }
      state.counts.set("*", total);
    } else {
      let total = 0;
      for (const f of state.failures) {
        total += 1;
        state.counts.set(f.kind, (state.counts.get(f.kind) || 0) + 1);
      }
      state.counts.set("*", total);
    }
    const filtered = applyFilters(state.failures);
    const sev = { high: 0, medium: 0, low: 0 };
    for (const f of filtered) sev[severityFor(f)] = (sev[severityFor(f)] || 0) + 1;
    const setNum = (id, n) => { const e = $(id); if (e) e.textContent = String(n); };
    setNum("kpi-failures", filtered.length);
    setNum("kpi-high", sev.high);
    setNum("kpi-medium", sev.medium);
    setNum("kpi-low", sev.low);
  }

  function renderSidebar() {
    const host = $("dbg-kind-list");
    if (!host) return;
    host.innerHTML = "";

    // Top group (just "all failures")
    const top = KIND_DEFS.filter((k) => k.group === "_TOP");
    for (const def of top) host.appendChild(buildKindRow(def));

    // Sub-groups
    const groups = new Map();
    for (const def of KIND_DEFS) {
      if (def.group === "_TOP") continue;
      if (!groups.has(def.group)) groups.set(def.group, []);
      groups.get(def.group).push(def);
    }
    for (const [name, defs] of groups) {
      host.appendChild(el("div", { class: "dbg2-group-head", text: name }));
      for (const def of defs) host.appendChild(buildKindRow(def));
    }
  }

  function buildKindRow(def) {
    const count = state.counts.get(def.kind) || 0;
    const isActive = persisted.selectedKind === def.kind;
    const sev = def.sev;
    const glyph = sev === "high" ? "●" : sev === "medium" ? "●" : "○";
    return el("div", {
      class: "dbg2-kind-row sev-" + sev + (isActive ? " active" : ""),
      onclick: () => {
        if (persisted.selectedKind === def.kind) return;
        persisted.selectedKind = def.kind;
        savePersisted();
        renderSidebar();
        fetchFailures();
      },
    }, [
      el("span", { class: "glyph", text: glyph }),
      el("span", { class: "label", text: def.label }),
      el("span", { class: "count", text: count > 0 ? String(count) : "" }),
    ]);
  }

  function renderAgentChips() {
    const host = $("dbg-agent-chips");
    if (!host) return;
    host.innerHTML = "";

    const allChip = el("button", {
      type: "button",
      class: "dbg2-chip" + (persisted.agentFilter === "all" ? " active" : ""),
      "data-agent": "all",
      text: "all",
      onclick: () => setAgentFilter("all"),
    });
    host.appendChild(allChip);

    const agents = Array.from(state.knownAgents).sort();
    for (const a of agents) {
      host.appendChild(el("button", {
        type: "button",
        class: "dbg2-chip" + (persisted.agentFilter === a ? " active" : ""),
        "data-agent": a,
        text: a,
        onclick: () => setAgentFilter(a),
      }));
    }
  }

  function setAgentFilter(a) {
    if (persisted.agentFilter === a) return;
    persisted.agentFilter = a;
    savePersisted();
    renderAgentChips();
    fetchFailures();
  }

  function renderList() {
    const host = $("dbg-list");
    if (!host) return;
    host.innerHTML = "";

    if (!state.available) {
      host.appendChild(el("div", {
        class: "dbg2-empty",
        text: "debug failures endpoint not ready (404/503) — will retry as backend ships",
      }));
      return;
    }

    const filtered = applyFilters(state.failures);
    if (filtered.length === 0) {
      host.appendChild(el("div", {
        class: "dbg2-empty",
        text: "no failures in this view — pick another kind, widen the time range, or unfilter the agent",
      }));
      return;
    }

    for (const f of filtered) host.appendChild(buildFailureRow(f));
  }

  function buildFailureRow(f) {
    const sev = severityFor(f);
    const isActive = state.selectedEventId === f.event_id;
    const row = el("div", {
      class: "dbg2-row sev-" + sev + (isActive ? " active" : ""),
      "data-event-id": f.event_id,
      onclick: () => openPanel(f),
    }, [
      el("div", { class: "dbg2-row-stripe" }),
      el("div", { class: "dbg2-row-meat" }, [
        el("div", { class: "dbg2-row-head" }, [
          el("span", { class: "dbg2-row-kind", text: f.kind || "—" }),
          el("span", { class: "dbg2-row-ts", text: fmtTimeFromMs(f.timestamp_ms) }),
          el("span", { class: "dbg2-row-agent", text: f.agent_id || "—" }),
          el("span", { class: "dbg2-row-rel", text: fmtRelative(f.timestamp_ms) }),
        ]),
        el("div", { class: "dbg2-row-preview", text: truncate(f.payload_preview || "", 220) }),
        el("div", { class: "dbg2-row-foot" }, [
          f.task_id ? el("span", { class: "dbg2-badge", text: "task: " + truncate(f.task_id, 8) }) : null,
          f.session_id ? el("span", { class: "dbg2-badge", text: "session: " + truncate(f.session_id, 8) }) : null,
        ]),
      ]),
    ]);
    return row;
  }

  // -----------------------------------------------------------------
  // Side panel (the detail view)
  // -----------------------------------------------------------------

  async function openPanel(f) {
    state.selectedEventId = f.event_id;
    const panel = $("dbg-panel");
    const body = $("dbg-panel-body");
    const title = $("dbg-panel-title");
    if (!panel || !body) return;

    if (title) title.textContent = (f.kind || "failure") + " · " + truncate(f.event_id, 8);
    panel.classList.add("open");
    panel.setAttribute("aria-hidden", "false");

    // Mark the active row + de-mark previous.
    document.querySelectorAll(".dbg2-row.active").forEach((r) => r.classList.remove("active"));
    const rowEl = document.querySelector(`.dbg2-row[data-event-id="${CSS.escape(f.event_id)}"]`);
    if (rowEl) rowEl.classList.add("active");

    body.innerHTML = "";

    // Section 1: failure meta
    const sev = severityFor(f);
    body.appendChild(el("div", { class: "dbg2-section" }, [
      el("h4", { text: "failure" }),
      el("div", { class: "dbg2-meta" }, [
        el("span", { class: "k", text: "severity" }),
        el("span", { class: "v sev-" + sev, text: sev }),
        el("span", { class: "k", text: "kind" }),
        el("span", { class: "v", text: f.kind || "—" }),
        el("span", { class: "k", text: "agent" }),
        el("span", { class: "v", text: f.agent_id || "—" }),
        el("span", { class: "k", text: "when" }),
        el("span", { class: "v", text: fmtFullTimeFromMs(f.timestamp_ms) + " (" + fmtRelative(f.timestamp_ms) + ")" }),
        f.task_id ? el("span", { class: "k", text: "task" }) : null,
        f.task_id ? el("span", { class: "v", text: f.task_id }) : null,
        f.session_id ? el("span", { class: "k", text: "session" }) : null,
        f.session_id ? el("span", { class: "v", text: f.session_id }) : null,
      ]),
    ]));

    // Section 2: payload (loading)
    const payloadSection = el("div", { class: "dbg2-section" }, [
      el("h4", { text: "event payload" }),
      el("pre", { class: "dbg2-json", html: highlightJson({
        event_id: f.event_id, kind: f.kind, agent_id: f.agent_id,
        task_id: f.task_id, session_id: f.session_id,
        timestamp_ms: f.timestamp_ms,
        payload_preview: f.payload_preview,
      }) }),
    ]);
    body.appendChild(payloadSection);

    // Sections 3 + 4 will be filled in after the context fetch.
    const precedingSection = el("div", { class: "dbg2-section" }, [
      el("h4", { text: "what came before" }),
      el("div", { class: "dbg2-empty", text: "loading…" }),
    ]);
    body.appendChild(precedingSection);

    const ctx = await fetchContext(f.event_id);
    if (state.selectedEventId !== f.event_id) return;

    // Replace payload with the full event from the server.
    if (ctx.event) {
      const pre = payloadSection.querySelector(".dbg2-json");
      if (pre) pre.innerHTML = highlightJson(ctx.event);
    }

    // Replace "what came before"
    const precWrap = precedingSection;
    precWrap.innerHTML = "";
    precWrap.appendChild(el("h4", { text: "what came before" }));
    if (!ctx.available) {
      precWrap.appendChild(el("div", { class: "dbg2-empty",
        text: "context endpoint not ready — will work once backend ships" }));
    } else if (!ctx.preceding.length) {
      precWrap.appendChild(el("div", { class: "dbg2-empty",
        text: "no preceding events found in same task/session" }));
    } else {
      const list = el("div", { class: "dbg2-preceding" });
      for (const p of ctx.preceding) {
        list.appendChild(el("div", { class: "dbg2-pre-row" }, [
          el("span", { class: "ts", text: fmtTimeFromMs(p.timestamp_ms || (p.timestamp ? Date.parse(p.timestamp) : 0)) }),
          el("div", { class: "body" }, [
            el("span", { class: "kind", text: p.kind || "" }),
            el("span", { class: "preview", text: p.payload_preview || "" }),
          ]),
        ]));
      }
      precWrap.appendChild(list);
    }

    // Hints + "open in agents" link
    if (ctx.hints && ctx.hints.length) {
      const hintsSection = el("div", { class: "dbg2-section" }, [
        el("h4", { text: "hints" }),
      ]);
      const ul = el("ul", { class: "dbg2-hints" });
      for (const h of ctx.hints) ul.appendChild(el("li", { text: h }));
      hintsSection.appendChild(ul);
      body.appendChild(hintsSection);
    }
    if (f.agent_id) {
      body.appendChild(el("a", {
        class: "dbg2-link",
        href: "/agents?agent=" + encodeURIComponent(f.agent_id) + "&event=" + encodeURIComponent(f.event_id),
        target: "_blank",
        rel: "noopener",
        text: "open in agents view ↗",
      }));
    }
  }

  function closePanel() {
    const panel = $("dbg-panel");
    if (!panel) return;
    panel.classList.remove("open");
    panel.setAttribute("aria-hidden", "true");
    state.selectedEventId = null;
    document.querySelectorAll(".dbg2-row.active").forEach((r) => r.classList.remove("active"));
  }

  // -----------------------------------------------------------------
  // Event wiring
  // -----------------------------------------------------------------

  function renderAll() {
    computeCounts();
    renderSidebar();
    renderAgentChips();
    renderList();
  }

  function bind() {
    // Range chips
    const rangeWrap = $("dbg-range-chips");
    if (rangeWrap) {
      rangeWrap.querySelectorAll(".dbg2-chip").forEach((b) => {
        // Set initial active state from persisted.
        const r = parseInt(b.getAttribute("data-range"), 10) || 0;
        b.classList.toggle("active", r === persisted.rangeMs);
        b.addEventListener("click", () => {
          persisted.rangeMs = r;
          savePersisted();
          rangeWrap.querySelectorAll(".dbg2-chip").forEach((x) => x.classList.remove("active"));
          b.classList.add("active");
          fetchFailures();
        });
      });
    }
    // Side panel close
    const close = $("dbg-panel-close");
    if (close) close.addEventListener("click", closePanel);
    // Esc closes the panel
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closePanel();
    });
  }

  // -----------------------------------------------------------------
  // Boot
  // -----------------------------------------------------------------

  async function boot() {
    bind();
    renderAll();
    await fetchFailures();
    // Auto-poll every 30s as a safety net for missed WS events.
    setInterval(fetchFailures, 30_000);

    // Deep-link: /debug?event=<id> auto-opens that one.
    try {
      const u = new URL(window.location.href);
      const eid = u.searchParams.get("event");
      if (eid) {
        const f = state.failures.find((x) => x.event_id === eid);
        if (f) openPanel(f);
      }
    } catch (_) {}
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
