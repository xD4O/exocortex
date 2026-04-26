// agents.js — /static/agents.html
//
// Two-column layout. Left: list of agents with stats + sparkline.
// Right: timeline of selected agent. Click a row to expand the "why" drawer.
//
// All endpoints (GET /api/agents, /api/agents/{id}/history,
// /api/agents/{id}/context/{event_id}) can 404/503 — we degrade to empty
// states + "endpoint not ready" copy. We never crash.

(function () {
  "use strict";

  // -------------------------------------------------------------------
  // Constants
  // -------------------------------------------------------------------

  const AGENT_COLORS = {
    codex: "#58a6ff",
    hermes: "#d29922",
    claude: "#7ee787",
    claude_code: "#7ee787",
    openclaw: "#bb6bd9",
  };
  const FALLBACK_AGENT_COLOR = "#8b949e";

  const KIND_GLYPH = {
    "memory.written": "M",
    "memory.read": "M",
    "tool.proposed": "T",
    "tool.approved": "T",
    "tool.executed": "T",
    "tool.rejected": "T",
    "task.created": "+",
    "task.completed": "✓",
    "task.failed": "!",
    "task.status_changed": "~",
    "handoff.initiated": "→",
    "handoff.accepted": "←",
    "approval.requested": "?",
    "approval.resolved": "✓",
    "chat.queried": "Q",
  };

  const KIND_CLASS = {
    "task.created": "ok",
    "task.completed": "ok",
    "task.failed": "bad",
    "tool.rejected": "bad",
    "tool.approved": "ok",
    "tool.executed": "ok",
    "approval.requested": "warn",
    "approval.resolved": "ok",
    "handoff.initiated": "warn",
    "handoff.accepted": "ok",
    "memory.written": "info",
  };

  // Map a "filter chip kind" -> set of event kinds that count.
  const CHIP_KIND_MAP = {
    all: null, // null means everything
    memory: new Set(["memory.written", "memory.read"]),
    tools: new Set([
      "tool.proposed", "tool.approved", "tool.executed", "tool.rejected",
      "approval.requested", "approval.resolved",
    ]),
    dispatch: new Set(["task.created", "task.completed", "task.failed",
      "task.status_changed", "handoff.initiated", "handoff.accepted"]),
    chat: new Set(["chat.queried", "chat.response"]),
  };

  // -------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const k in attrs) {
        if (k === "class") node.className = attrs[k];
        else if (k === "text") node.textContent = attrs[k];
        else if (k === "html") node.innerHTML = attrs[k];
        else if (k === "style") {
          for (const sk in attrs[k]) node.style[sk] = attrs[k][sk];
        }
        else if (k.startsWith("on")) node.addEventListener(k.slice(2), attrs[k]);
        else node.setAttribute(k, attrs[k]);
      }
    }
    if (children) {
      for (const c of children) {
        if (c == null) continue;
        node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
      }
    }
    return node;
  }

  function agentColor(id) {
    if (!id) return FALLBACK_AGENT_COLOR;
    return AGENT_COLORS[id] || FALLBACK_AGENT_COLOR;
  }

  function kindGlyph(k) { return KIND_GLYPH[k] || "·"; }

  function fmtTimeFromMs(ms) {
    try {
      const d = new Date(ms);
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      const ss = String(d.getSeconds()).padStart(2, "0");
      return `${hh}:${mm}:${ss}`;
    } catch (_) { return "--:--:--"; }
  }

  function fmtDayHeader(ms) {
    try {
      const d = new Date(ms);
      const yyyy = d.getFullYear();
      const mm = String(d.getMonth() + 1).padStart(2, "0");
      const dd = String(d.getDate()).padStart(2, "0");
      const days = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];
      return `${yyyy}-${mm}-${dd} · ${days[d.getDay()]}`;
    } catch (_) { return "—"; }
  }

  function fmtRelative(ms) {
    if (!ms) return "—";
    const diff = Date.now() - ms;
    if (diff < 0) return "just now";
    if (diff < 60_000) return Math.max(1, Math.floor(diff / 1000)) + "s ago";
    if (diff < 3_600_000) return Math.floor(diff / 60_000) + "m ago";
    if (diff < 86_400_000) return Math.floor(diff / 3_600_000) + "h ago";
    return Math.floor(diff / 86_400_000) + "d ago";
  }

  function truncate(s, n) {
    if (s == null) return "";
    s = String(s);
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  function dayKey(ms) {
    const d = new Date(ms);
    return d.getFullYear() + "-" + (d.getMonth() + 1) + "-" + d.getDate();
  }

  // Tiny JSON syntax highlighter (no deps). Returns escaped HTML.
  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function highlightJson(obj) {
    let s;
    try { s = JSON.stringify(obj, null, 2); } catch (_) { s = String(obj); }
    s = escapeHtml(s);
    // Strings (incl keys), numbers, booleans, null
    s = s.replace(/("(?:\\.|[^"\\])*")(\s*:)?/g, function (m, str, colon) {
      const cls = colon ? "k" : "s";
      return `<span class="${cls}">${str}</span>` + (colon || "");
    });
    s = s.replace(/\b(-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)\b/gi, '<span class="n">$1</span>');
    s = s.replace(/\b(true|false)\b/g, '<span class="b">$1</span>');
    s = s.replace(/\bnull\b/g, '<span class="nu">null</span>');
    return s;
  }

  // -------------------------------------------------------------------
  // State
  // -------------------------------------------------------------------

  const state = {
    agents: [],
    agentsAvailable: true,
    selectedAgentId: null,
    history: [],            // current agent's events
    historyAvailable: true,
    chip: "all",
    rangeMs: 86_400_000,
    expandedEventId: null,
    expandedRowEl: null,
    drawerEl: null,
    contextCache: new Map(),
    taskFilter: null,       // {scope:"task", id} pinned by clicking a badge
    sessionFilter: null,
  };

  // -------------------------------------------------------------------
  // Fetchers
  // -------------------------------------------------------------------

  async function fetchAgents() {
    try {
      const r = await fetch("/api/agents");
      if (!r.ok) {
        if (r.status === 404 || r.status === 503) {
          state.agentsAvailable = false;
        }
        renderAgentsSidebar();
        return;
      }
      const data = await r.json();
      state.agentsAvailable = true;
      // Accept {items:[...]} (new) or {agents:[...]} (existing).
      const list = Array.isArray(data.items)
        ? data.items
        : (Array.isArray(data.agents) ? data.agents : []);
      state.agents = list.map(normalizeAgent);
      const kpiA = document.getElementById("kpi-agents");
      const kpiE = document.getElementById("kpi-events");
      if (kpiA) kpiA.textContent = state.agents.length;
      if (kpiE) {
        const total = state.agents.reduce((acc, a) => acc + (a.total_events || 0), 0);
        kpiE.textContent = total;
      }
      renderAgentsSidebar();
    } catch (_) {
      state.agentsAvailable = false;
      renderAgentsSidebar();
    }
  }

  function normalizeAgent(a) {
    function toMs(v) {
      if (v == null) return 0;
      if (typeof v === "number") return v;
      const n = Date.parse(v);
      return isNaN(n) ? 0 : n;
    }
    return {
      agent_id: a.agent_id || a.id || "",
      total_events: a.total_events || 0,
      memory_writes: a.memory_writes || 0,
      tool_invocations: a.tool_invocations || 0,
      dispatches: a.dispatches || 0,
      chat_queries: a.chat_queries || 0,
      last_active_at: toMs(a.last_active_at) || toMs(a.last_event_at) || 0,
      first_seen_at: toMs(a.first_seen_at) || 0,
      hourly: Array.isArray(a.hourly) ? a.hourly : null,  // optional sparkline data
    };
  }

  async function fetchHistory(agentId) {
    if (!agentId) return;
    try {
      const params = new URLSearchParams();
      params.set("limit", "200");
      const r = await fetch(`/api/agents/${encodeURIComponent(agentId)}/history?` + params.toString());
      if (!r.ok) {
        if (r.status === 404 || r.status === 503) {
          state.historyAvailable = false;
        }
        state.history = [];
        renderTimeline();
        return;
      }
      const data = await r.json();
      state.historyAvailable = true;
      const items = Array.isArray(data.items) ? data.items : [];
      state.history = items.map(normalizeEvent);
      renderTimeline();
    } catch (_) {
      state.historyAvailable = false;
      state.history = [];
      renderTimeline();
    }
  }

  function normalizeEvent(e) {
    return {
      event_id: e.event_id || (e.timestamp_ms + ":" + e.kind),
      kind: e.kind || "",
      agent_id: e.agent_id || "",
      timestamp_ms: e.timestamp_ms
        || (e.timestamp ? Date.parse(e.timestamp) : Date.now()),
      payload: e.payload || null,
      payload_preview: e.payload_preview || (e.payload ? shortPreview(e.payload) : ""),
      task_id: e.task_id || (e.payload && e.payload.task_id) || null,
      session_id: e.session_id || (e.payload && e.payload.session_id) || null,
      scope: e.scope || null,
      scope_id: e.scope_id || null,
    };
  }

  function shortPreview(p) {
    if (!p || typeof p !== "object") return String(p || "");
    const keys = Object.keys(p).slice(0, 3);
    return keys.map((k) => `${k}=${truncate(JSON.stringify(p[k]), 30)}`).join(" ");
  }

  async function fetchContext(agentId, eventId) {
    const key = agentId + "::" + eventId;
    if (state.contextCache.has(key)) return state.contextCache.get(key);
    try {
      const r = await fetch(`/api/agents/${encodeURIComponent(agentId)}/context/${encodeURIComponent(eventId)}`);
      if (!r.ok) {
        if (r.status === 404 || r.status === 503) {
          const ctx = { available: false };
          state.contextCache.set(key, ctx);
          return ctx;
        }
        return { available: true, event: null, preceding: [], records_referenced: [] };
      }
      const data = await r.json();
      const ctx = {
        available: true,
        event: data.event || null,
        preceding: Array.isArray(data.preceding) ? data.preceding.map(normalizeEvent) : [],
        records_referenced: Array.isArray(data.records_referenced) ? data.records_referenced : [],
      };
      state.contextCache.set(key, ctx);
      return ctx;
    } catch (_) {
      return { available: false };
    }
  }

  // -------------------------------------------------------------------
  // Renderers
  // -------------------------------------------------------------------

  function renderAgentsSidebar() {
    const host = document.getElementById("agents-sidebar");
    if (!host) return;
    host.innerHTML = "";

    if (!state.agentsAvailable) {
      host.appendChild(el("div", {
        class: "ag-empty",
        text: "agents endpoint not ready — will retry",
      }));
      return;
    }
    if (state.agents.length === 0) {
      host.appendChild(el("div", {
        class: "ag-empty",
        text: "no agent activity yet — agents will appear here once they call MCP tools.",
      }));
      return;
    }

    // Stable order: most-recently-active first.
    const sorted = state.agents.slice().sort(
      (a, b) => (b.last_active_at || 0) - (a.last_active_at || 0)
    );
    for (const a of sorted) {
      const color = agentColor(a.agent_id);
      const card = el("div", {
        class: "ag-card" + (state.selectedAgentId === a.agent_id ? " active" : ""),
        style: { borderLeftColor: color },
        onclick: () => selectAgent(a.agent_id),
      }, [
        el("div", { class: "id", text: a.agent_id || "—" }),
        el("div", { class: "last-active", text: "last active " + fmtRelative(a.last_active_at) }),
        el("div", { class: "stats" }, [
          el("span", { class: "stat" }, [
            el("span", { class: "glyph", text: "✎ " }),
            document.createTextNode(a.memory_writes + " writes"),
          ]),
          el("span", { class: "stat" }, [
            el("span", { class: "glyph", text: "⚙ " }),
            document.createTextNode(a.tool_invocations + " tools"),
          ]),
          el("span", { class: "stat" }, [
            el("span", { class: "glyph", text: "↗ " }),
            document.createTextNode(a.dispatches + " dispatches"),
          ]),
          el("span", { class: "stat" }, [
            el("span", { class: "glyph", text: "» " }),
            document.createTextNode(a.chat_queries + " chats"),
          ]),
        ]),
        buildSparkline(a, color),
      ]);
      host.appendChild(card);
    }
  }

  function buildSparkline(agent, color) {
    // 24 bars, one per hour. If agent.hourly is provided we use it.
    // Otherwise we fake a flat low-contrast row so empty agents don't look broken.
    const bars = el("div", { class: "sparkline" });
    const data = Array.isArray(agent.hourly) && agent.hourly.length === 24
      ? agent.hourly
      : new Array(24).fill(0);
    const max = Math.max(1, ...data);
    for (const v of data) {
      const h = Math.max(1, Math.round((v / max) * 14));
      const b = el("div", {
        class: "bar",
        style: {
          height: h + "px",
          background: v > 0 ? color : "var(--border)",
          opacity: v > 0 ? "0.55" : "0.35",
        },
      });
      bars.appendChild(b);
    }
    return bars;
  }

  function renderTimeline() {
    const host = document.getElementById("agents-timeline");
    const cur = document.getElementById("ag-current");
    if (!host) return;

    if (cur) cur.textContent = state.selectedAgentId || "select an agent";

    if (!state.selectedAgentId) {
      host.innerHTML = "";
      host.appendChild(el("div", { class: "ag-empty", text: "select an agent on the left to see its history" }));
      return;
    }

    if (!state.historyAvailable) {
      host.innerHTML = "";
      host.appendChild(el("div", { class: "ag-empty", text: "history endpoint not ready — will retry" }));
      return;
    }

    const filtered = applyFilters(state.history);
    host.innerHTML = "";

    if (filtered.length === 0) {
      host.appendChild(el("div", { class: "ag-empty", text: "no events match the current filters" }));
      return;
    }

    let lastDay = null;
    for (const ev of filtered) {
      const d = dayKey(ev.timestamp_ms);
      if (d !== lastDay) {
        host.appendChild(el("div", { class: "day-header", text: fmtDayHeader(ev.timestamp_ms) }));
        lastDay = d;
      }
      host.appendChild(buildEventRow(ev));
      // Re-attach drawer if this is the expanded row
      if (state.expandedEventId === ev.event_id) {
        const drawer = state.drawerEl;
        if (drawer && drawer.parentNode) drawer.parentNode.removeChild(drawer);
        // Drawer will be re-rendered async after re-fetch. Skip — we keep
        // expansion state but the drawer body is rebuilt below if needed.
      }
    }
  }

  function applyFilters(events) {
    const now = Date.now();
    const cutoff = state.rangeMs > 0 ? now - state.rangeMs : 0;
    const allow = CHIP_KIND_MAP[state.chip] || null;
    const out = [];
    for (const ev of events) {
      if (cutoff && ev.timestamp_ms < cutoff) continue;
      if (allow && !allow.has(ev.kind)) continue;
      if (state.taskFilter && ev.task_id !== state.taskFilter) continue;
      if (state.sessionFilter && ev.session_id !== state.sessionFilter) continue;
      out.push(ev);
    }
    // newest first
    out.sort((a, b) => (b.timestamp_ms || 0) - (a.timestamp_ms || 0));
    return out;
  }

  function buildEventRow(ev) {
    const cls = "kind " + (KIND_CLASS[ev.kind] || "");
    const row = el("div", {
      class: "ev-row" + (state.expandedEventId === ev.event_id ? " expanded" : ""),
      "data-event-id": ev.event_id,
      onclick: (e) => {
        // Don't trigger drawer when clicking a badge
        if (e.target && e.target.classList && e.target.classList.contains("badge")) return;
        toggleDrawer(ev, row);
      },
    }, [
      el("div", { class: "gutter" }, [
        el("span", { class: "ts", text: fmtTimeFromMs(ev.timestamp_ms) }),
        el("span", { class: cls, text: kindGlyph(ev.kind) + " " + ev.kind }),
      ]),
      el("div", { class: "body", text: ev.payload_preview || "(no preview)" }),
      el("div", { class: "badges" }, [
        ev.task_id ? el("span", {
          class: "badge task",
          title: "filter to this task: " + ev.task_id,
          text: "task:" + truncate(ev.task_id, 8),
          onclick: (e) => { e.stopPropagation(); toggleTaskFilter(ev.task_id); },
        }) : null,
        ev.session_id ? el("span", {
          class: "badge session",
          title: "filter to this session: " + ev.session_id,
          text: "session:" + truncate(ev.session_id, 8),
          onclick: (e) => { e.stopPropagation(); toggleSessionFilter(ev.session_id); },
        }) : null,
      ]),
    ]);
    return row;
  }

  function toggleTaskFilter(id) {
    state.taskFilter = state.taskFilter === id ? null : id;
    state.sessionFilter = null;
    renderTimeline();
  }
  function toggleSessionFilter(id) {
    state.sessionFilter = state.sessionFilter === id ? null : id;
    state.taskFilter = null;
    renderTimeline();
  }

  // -------------------------------------------------------------------
  // Why drawer
  // -------------------------------------------------------------------

  async function toggleDrawer(ev, rowEl) {
    // If this row is already expanded, close.
    if (state.expandedEventId === ev.event_id) {
      closeDrawer();
      return;
    }
    closeDrawer();
    state.expandedEventId = ev.event_id;
    state.expandedRowEl = rowEl;
    rowEl.classList.add("expanded");

    const drawer = el("div", { class: "why-drawer", "data-for": ev.event_id }, [
      el("h4", { text: "Event payload" }),
      buildJsonBlock(ev),
      el("h4", { text: "What came before in this task/session" }),
      el("div", { class: "preceding" }, [
        el("div", { class: "ag-empty", style: { padding: "8px", textAlign: "left" }, text: "loading…" }),
      ]),
    ]);
    state.drawerEl = drawer;
    rowEl.parentNode.insertBefore(drawer, rowEl.nextSibling);

    const ctx = await fetchContext(state.selectedAgentId, ev.event_id);
    if (state.expandedEventId !== ev.event_id) return; // closed during fetch

    // Replace placeholder
    const precWrap = drawer.querySelector(".preceding");
    if (precWrap) precWrap.innerHTML = "";

    if (!ctx.available) {
      precWrap.appendChild(el("div", {
        class: "ag-empty",
        style: { padding: "8px", textAlign: "left" },
        text: "context endpoint not ready (404/503) — will work once backend ships",
      }));
    } else if ((ctx.preceding || []).length === 0) {
      precWrap.appendChild(el("div", {
        class: "ag-empty",
        style: { padding: "8px", textAlign: "left" },
        text: "no preceding events found",
      }));
    } else {
      for (const p of ctx.preceding) {
        precWrap.appendChild(el("div", { class: "pre-row" }, [
          el("span", { text: fmtTimeFromMs(p.timestamp_ms) }),
          el("span", {}, [
            el("span", { class: "kind", text: kindGlyph(p.kind) + " " + p.kind }),
            document.createTextNode(" " + truncate(p.payload_preview || "", 80)),
          ]),
        ]));
      }
    }

    // Records referenced
    if (ctx.records_referenced && ctx.records_referenced.length) {
      drawer.appendChild(el("h4", { text: "Records referenced" }));
      const recsWrap = el("div", { class: "records" });
      for (const r of ctx.records_referenced) {
        recsWrap.appendChild(buildRecordCard(r));
      }
      drawer.appendChild(recsWrap);
    }
  }

  function buildJsonBlock(ev) {
    const pre = el("pre", { class: "event-json" });
    pre.innerHTML = highlightJson({
      event_id: ev.event_id,
      kind: ev.kind,
      agent_id: ev.agent_id,
      timestamp_ms: ev.timestamp_ms,
      task_id: ev.task_id,
      session_id: ev.session_id,
      scope: ev.scope,
      scope_id: ev.scope_id,
      payload: ev.payload != null ? ev.payload : ev.payload_preview,
    });
    return pre;
  }

  function buildRecordCard(r) {
    const id = r.id || r.record_id || "";
    return el("div", { class: "rec" }, [
      el("div", { class: "head-row" }, [
        el("span", { text: truncate(id, 12) }),
        el("span", { text: " · " }),
        el("span", { text: r.type || "—" }),
        el("span", { text: " · " }),
        el("span", { text: r.source || "—" }),
        el("a", {
          class: "focus-chip",
          href: "/memory#focus=" + encodeURIComponent(id),
          target: "_blank",
          rel: "noopener noreferrer",
          text: "focus →",
          onclick: (e) => { e.stopPropagation(); },
        }),
      ]),
      el("div", { class: "content", text: truncate(r.content || "", 600) }),
    ]);
  }

  function closeDrawer() {
    if (state.drawerEl && state.drawerEl.parentNode) {
      state.drawerEl.parentNode.removeChild(state.drawerEl);
    }
    if (state.expandedRowEl) {
      state.expandedRowEl.classList.remove("expanded");
    }
    state.drawerEl = null;
    state.expandedEventId = null;
    state.expandedRowEl = null;
  }

  // -------------------------------------------------------------------
  // Selection + bindings
  // -------------------------------------------------------------------

  async function selectAgent(agentId) {
    state.selectedAgentId = agentId;
    state.taskFilter = null;
    state.sessionFilter = null;
    closeDrawer();
    renderAgentsSidebar();
    document.getElementById("ag-current").textContent = agentId;
    document.getElementById("agents-timeline").innerHTML = "";
    document.getElementById("agents-timeline").appendChild(
      el("div", { class: "ag-empty", text: "loading history…" })
    );
    await fetchHistory(agentId);
    // Honor ?event=<id> deep link if present.
    const params = new URL(window.location).searchParams;
    const eventId = params.get("event");
    if (eventId) {
      const ev = state.history.find((x) => x.event_id === eventId);
      if (ev) {
        // Wait for next frame so the row is in the DOM
        requestAnimationFrame(() => {
          const row = document.querySelector(`.ev-row[data-event-id="${cssEscape(eventId)}"]`);
          if (row) {
            row.scrollIntoView({ block: "center" });
            toggleDrawer(ev, row);
          }
        });
      }
    }
  }

  function cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/["\\]/g, "\\$&");
  }

  function bindChips() {
    document.querySelectorAll("#ag-chips .chip").forEach((c) => {
      c.addEventListener("click", () => {
        document.querySelectorAll("#ag-chips .chip").forEach((cc) =>
          cc.classList.toggle("active", cc.dataset.kind === c.dataset.kind)
        );
        state.chip = c.dataset.kind;
        renderTimeline();
      });
    });
  }

  function bindRange() {
    const sel = document.getElementById("ag-range");
    if (!sel) return;
    sel.addEventListener("change", () => {
      state.rangeMs = Number(sel.value) || 0;
      renderTimeline();
    });
  }

  function bindEsc() {
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeDrawer();
    });
  }

  // Live updates: append to history if the event is for the selected agent.
  function connectWs() {
    let backoff = 1000;
    function open() {
      let ws;
      try {
        const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
        ws = new WebSocket(`${proto}//${window.location.host}/api/events`);
      } catch (_) {
        setTimeout(open, backoff = Math.min(backoff * 2, 8000));
        return;
      }
      ws.addEventListener("open", () => { backoff = 1000; });
      ws.addEventListener("close", () => setTimeout(open, backoff = Math.min(backoff * 2, 8000)));
      ws.addEventListener("error", () => { /* close will follow */ });
      ws.addEventListener("message", (msg) => {
        let event;
        try { event = JSON.parse(msg.data); } catch (_) { return; }
        if (event.kind === "__hello__") return;
        if (state.selectedAgentId && event.agent_id === state.selectedAgentId) {
          state.history.unshift(normalizeEvent(event));
          if (state.history.length > 500) state.history.pop();
          renderTimeline();
        }
      });
    }
    open();
  }

  // -------------------------------------------------------------------
  // Boot
  // -------------------------------------------------------------------

  async function boot() {
    bindChips();
    bindRange();
    bindEsc();
    await fetchAgents();

    const params = new URL(window.location).searchParams;
    const initialAgent = params.get("agent");
    if (initialAgent) {
      selectAgent(initialAgent);
    }

    setInterval(fetchAgents, 30_000);
    connectWs();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
