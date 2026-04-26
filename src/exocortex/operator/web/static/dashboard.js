// Dashboard v2.
//
// Three vertical zones: KPI strip, attention panel, two-column "happening / grown".
// All UI state persists in localStorage under STATE_KEY so navigating away and
// back does not flash defaults.
//
// Endpoints used (tolerant of 404/503 — degrade to empty states):
//   GET  /api/status           (existing)
//   GET  /api/tasks?limit=…    (existing)
//   GET  /api/agents           (existing)
//   GET  /api/activity         (existing, optional)
//   GET  /api/dashboard/attention  (NEW; 404 -> all-clear)
//   GET  /api/dashboard/growth     (NEW; 404 -> dashes)
//   WS   /api/events           (existing) — live feed
//
// Visual rules: dark palette only, no gradients/glows/pastels. Severity is a
// left-edge color stripe. Active agents pulse a 1px dot every 2s.

(function () {
  "use strict";

  const STATE_KEY = "exocortex.dashboard.v1";
  const MAX_FEED_LINES = 30;
  const AGENT_ACTIVE_WINDOW_MS = 30_000;
  const SPARK_BUCKETS = 24;            // 24 hourly samples
  const SPARK_TICK_MS = 60_000;        // resample once a minute
  const ATTENTION_POLL_MS = 15_000;
  const GROWTH_POLL_MS = 30_000;
  const CHAINS_POLL_MS = 30_000;
  const CHAINS_REFRESH_DEBOUNCE_MS = 400;
  const CHAINS_LIMIT = 20;
  const TIME_WINDOWS = {
    "1h":  60 * 60 * 1000,
    "24h": 24 * 60 * 60 * 1000,
    "7d":  7 * 24 * 60 * 60 * 1000,
    "all": null,
  };

  const AGENT_COLORS = {
    codex: "#58a6ff",
    hermes: "#d29922",
    claude: "#7ee787",
    claude_code: "#7ee787",
    openclaw: "#bb6bd9",
  };
  const FALLBACK_AGENT_COLOR = "#8b949e";

  function agentColor(id) {
    if (!id) return FALLBACK_AGENT_COLOR;
    return AGENT_COLORS[id] || FALLBACK_AGENT_COLOR;
  }

  // ------------------------------------------------------------------
  // Persisted UI state
  // ------------------------------------------------------------------

  const defaultPersisted = {
    density: "comfortable",       // "comfortable" | "compact"
    attnCollapsed: false,
    attnDismissed: {},            // {fingerprint: ts}
    happenAgentFilter: "all",     // "all" | "active" | <agent_id>
    happenFeedKind: "all",
    happenPaused: false,
    happenFeedScroll: 0,
    grownTagFilter: null,         // reserved
    chainsMinHops: 1,             // 1 | 2 | 3
    chainsTimeWindow: "24h",      // "1h" | "24h" | "7d" | "all"
    lastViewedChain: null,        // chain_id last opened
  };

  function loadPersisted() {
    try {
      const raw = localStorage.getItem(STATE_KEY);
      if (!raw) return Object.assign({}, defaultPersisted);
      const parsed = JSON.parse(raw);
      return Object.assign({}, defaultPersisted, parsed || {});
    } catch (_) {
      return Object.assign({}, defaultPersisted);
    }
  }

  function savePersisted() {
    try {
      localStorage.setItem(STATE_KEY, JSON.stringify(persisted));
    } catch (_) { /* quota etc. ignore */ }
  }

  const persisted = loadPersisted();

  // ------------------------------------------------------------------
  // In-memory state
  // ------------------------------------------------------------------

  const state = {
    tasks: [],
    agents: new Map(),         // agent_id -> {agent, lastSeen}
    feed: [],                  // newest first
    feedCount: 0,
    eventsInWindow: [],        // for EPS
    lastEps: 0,
    attention: { items: [], available: true },
    growth: { available: true, data: null },
    sparks: {                  // rolling 24h samples
      events: new Array(SPARK_BUCKETS).fill(0),
      tasks:  new Array(SPARK_BUCKETS).fill(0),
      records:new Array(SPARK_BUCKETS).fill(0),
      agents: new Array(SPARK_BUCKETS).fill(0),
    },
    sparkLastTick: 0,
    activity: [],              // last activity events (for in-flight detection)
    inFlight: new Map(),       // task_id -> {agent, startMs, kind, body}
    statusCache: { tasks: 0, events_last_hour: 0, memory_records: 0,
                   agents_active_last_hour: [] },
    wsOk: false,
    epsAnimFrom: 0,
    chains: { available: true, items: [], lastStatus: null },
    chainCache: new Map(),     // chain_id -> full chain object (latest fetch)
    drawerOpen: false,
    drawerChainId: null,
  };

  // ------------------------------------------------------------------
  // DOM helpers
  // ------------------------------------------------------------------

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

  function $(id) { return document.getElementById(id); }

  function fmtTime(iso) {
    try {
      const d = typeof iso === "number" ? new Date(iso) : new Date(iso);
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      const ss = String(d.getSeconds()).padStart(2, "0");
      return `${hh}:${mm}:${ss}`;
    } catch (_) { return "--:--:--"; }
  }

  function fmtRelative(ms) {
    if (!ms) return "—";
    const diff = Date.now() - ms;
    if (diff < 0) return "just now";
    if (diff < 60_000) return Math.max(1, Math.floor(diff / 1000)) + "s ago";
    if (diff < 3_600_000) return Math.floor(diff / 60_000) + "m ago";
    if (diff < 86_400_000) {
      const h = Math.floor(diff / 3_600_000);
      const mins = Math.floor((diff % 3_600_000) / 60_000);
      return mins ? `${h}h${mins}m ago` : `${h}h ago`;
    }
    return Math.floor(diff / 86_400_000) + "d ago";
  }

  function truncate(s, n) {
    if (s == null) return "";
    s = String(s);
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  function fmtNum(n) {
    if (n == null || isNaN(n)) return "—";
    if (n >= 10000) return (n / 1000).toFixed(1) + "k";
    if (n >= 1000) return (n / 1000).toFixed(1) + "k";
    return String(n);
  }

  // ------------------------------------------------------------------
  // Fetchers
  // ------------------------------------------------------------------

  async function fetchJson(url, opts) {
    try {
      const r = await fetch(url, opts);
      if (!r.ok) return { _ok: false, _status: r.status };
      const data = await r.json();
      data._ok = true;
      data._status = r.status;
      return data;
    } catch (_) {
      return { _ok: false, _status: 0 };
    }
  }

  async function refreshStatus() {
    const s = await fetchJson("/api/status");
    if (!s._ok) return;
    state.statusCache = {
      tasks: s.tasks || 0,
      events_last_hour: s.events_last_hour || 0,
      memory_records: s.memory_records || 0,
      agents_active_last_hour: s.agents_active_last_hour || [],
    };
    setKpiNum("kpi-tasks", s.tasks);
    setKpiNum("kpi-events-hr", s.events_last_hour);
    setKpiNum("kpi-records", s.memory_records);
    setKpiNum("kpi-agents", (s.agents_active_last_hour || []).length);

    // seed last spark bucket
    const sp = state.sparks;
    sp.events[SPARK_BUCKETS - 1] = s.events_last_hour || 0;
    sp.tasks[SPARK_BUCKETS - 1] = s.tasks || 0;
    sp.records[SPARK_BUCKETS - 1] = s.memory_records || 0;
    sp.agents[SPARK_BUCKETS - 1] = (s.agents_active_last_hour || []).length;
    drawSparklines();
  }

  async function refreshTasks() {
    const res = await fetchJson("/api/tasks?limit=100");
    if (!res._ok) return;
    state.tasks = (res.tasks || []).slice();
  }

  async function refreshAgents() {
    const res = await fetchJson("/api/agents");
    if (!res._ok) return;
    const list = res.agents || res.items || [];
    for (const a of list) {
      const id = a.id || a.agent_id;
      if (!id) continue;
      const existing = state.agents.get(id) || { agent: null, lastSeen: 0 };
      existing.agent = a;
      if (a.recently_active || a.last_active_at) {
        const last = typeof a.last_active_at === "number"
          ? a.last_active_at
          : (a.last_active_at ? Date.parse(a.last_active_at) : 0);
        if (last > existing.lastSeen) existing.lastSeen = last;
        if (a.recently_active && !existing.lastSeen) existing.lastSeen = Date.now();
      }
      state.agents.set(id, existing);
    }
    renderHappeningAgents();
    populateAgentFilter();
  }

  async function refreshAttention() {
    const res = await fetchJson("/api/dashboard/attention");
    if (!res._ok) {
      // 404/503 -> empty state, but mark unavailable so we don't show stale.
      if (res._status === 404 || res._status === 503) {
        state.attention = { items: [], available: false };
      } else {
        state.attention.available = false;
      }
      renderAttention();
      return;
    }
    state.attention = {
      items: Array.isArray(res.items) ? res.items : [],
      available: true,
    };
    renderAttention();
  }

  async function refreshGrowth() {
    const res = await fetchJson("/api/dashboard/growth");
    if (!res._ok) {
      if (res._status === 404 || res._status === 503) {
        state.growth = { available: false, data: null };
      } else {
        state.growth = { available: false, data: null };
      }
      renderGrowth();
      return;
    }
    state.growth = { available: true, data: res };
    renderGrowth();
  }

  async function refreshActivity() {
    // Best-effort. Used to seed the feed when WS is offline.
    const res = await fetchJson("/api/activity?limit=30");
    if (!res._ok) return;
    const items = res.items || res.events || [];
    if (!items.length) return;
    state.feed = items.slice(0, MAX_FEED_LINES);
    renderFeed();
  }

  async function refreshChains() {
    const minHops = persisted.chainsMinHops || 1;
    const win = persisted.chainsTimeWindow || "24h";
    const winMs = TIME_WINDOWS[win];
    const params = new URLSearchParams();
    params.set("limit", String(CHAINS_LIMIT));
    params.set("min_depth", String(Math.max(1, minHops)));
    if (winMs != null) params.set("since_ms", String(Date.now() - winMs));
    const res = await fetchJson("/api/handoffs/chains?" + params.toString());
    if (!res._ok) {
      state.chains = {
        available: false,
        items: [],
        lastStatus: res._status || 0,
      };
      renderChains();
      return;
    }
    const items = Array.isArray(res.items) ? res.items : [];
    state.chains = { available: true, items, lastStatus: 200 };
    // Cache full chain objects so the drawer can open without a network roundtrip.
    for (const c of items) {
      if (c && c.chain_id) state.chainCache.set(c.chain_id, c);
    }
    renderChains();

    // If a drawer was open with this chain, refresh its swimlane.
    if (state.drawerOpen && state.drawerChainId) {
      const fresh = state.chainCache.get(state.drawerChainId);
      if (fresh) renderDrawer(fresh);
    }
  }

  async function fetchChainByTaskId(taskId) {
    const res = await fetchJson("/api/handoffs/chain/" + encodeURIComponent(taskId));
    if (!res._ok) return null;
    if (res.chain_id) state.chainCache.set(res.chain_id, res);
    return res;
  }

  // ------------------------------------------------------------------
  // KPI strip
  // ------------------------------------------------------------------

  function setKpiNum(id, value) {
    const node = $(id);
    if (!node) return;
    const txt = (value == null) ? "—" : fmtNum(value);
    if (node.textContent !== txt) node.textContent = txt;
  }

  function drawSparklines() {
    drawSparkline("kpi-tasks-spark",   state.sparks.tasks,   "var(--accent-2)");
    drawSparkline("kpi-events-spark",  state.sparks.events,  "var(--accent)");
    drawSparkline("kpi-records-spark", state.sparks.records, "var(--accent-2)");
    drawSparkline("kpi-agents-spark",  state.sparks.agents,  "var(--warn)");
  }

  function drawSparkline(svgId, data, color) {
    const svg = $(svgId);
    if (!svg) return;
    const w = 60, h = 14;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    if (!data || data.length === 0) return;
    let max = 1;
    for (const v of data) if (v > max) max = v;
    const n = data.length;
    const step = w / Math.max(1, n - 1);
    let d = "";
    for (let i = 0; i < n; i++) {
      const x = (i * step).toFixed(2);
      const y = (h - 1 - ((data[i] / max) * (h - 2))).toFixed(2);
      d += (i === 0 ? "M" : "L") + x + "," + y + " ";
    }
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d.trim());
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", color);
    path.setAttribute("stroke-width", "1.25");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    path.setAttribute("opacity", "0.85");
    svg.appendChild(path);
    // Last-point dot
    const lastX = ((n - 1) * step).toFixed(2);
    const lastY = (h - 1 - ((data[n - 1] / max) * (h - 2))).toFixed(2);
    const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    dot.setAttribute("cx", lastX);
    dot.setAttribute("cy", lastY);
    dot.setAttribute("r", "1.4");
    dot.setAttribute("fill", color);
    svg.appendChild(dot);
  }

  function rotateSparkBucketsIfDue() {
    const now = Date.now();
    if (now - state.sparkLastTick < SPARK_TICK_MS) return;
    state.sparkLastTick = now;
    for (const k of Object.keys(state.sparks)) {
      const arr = state.sparks[k];
      arr.shift();
      arr.push(0);
    }
    // Will be filled on next refreshStatus; redraw with current best.
    drawSparklines();
  }

  // ------------------------------------------------------------------
  // EPS counter (with digit transition)
  // ------------------------------------------------------------------

  function tickEps() {
    const now = Date.now();
    state.eventsInWindow = state.eventsInWindow.filter(t => now - t < 10_000);
    const eps = state.eventsInWindow.length / 10;
    const node = $("kpi-eps");
    if (!node) return;
    const fixed = eps.toFixed(1);
    if (node.textContent !== fixed) {
      node.textContent = fixed;
      node.classList.remove("kpi-eps-tick");
      // force reflow to restart animation
      // eslint-disable-next-line no-unused-expressions
      void node.offsetWidth;
      node.classList.add("kpi-eps-tick");
    }
    state.lastEps = eps;
    // count up bucket
    state.sparks.events[SPARK_BUCKETS - 1] = Math.max(
      state.sparks.events[SPARK_BUCKETS - 1] || 0,
      Math.round(eps * 60)
    );
  }

  // ------------------------------------------------------------------
  // Attention panel
  // ------------------------------------------------------------------

  function attentionFingerprint(item) {
    return [
      item.kind || "",
      item.related_event_id || "",
      item.related_task_id || "",
      item.title || "",
    ].join("::");
  }

  function setKpiAttention(count) {
    const node = $("kpi-attention");
    const glyph = $("kpi-attention-glyph");
    const delta = $("kpi-attention-delta");
    const card = $("kpi-attention-card");
    if (!node) return;
    node.textContent = String(count);
    if (count > 0) {
      glyph.textContent = "⚠";
      glyph.style.color = "var(--danger)";
      delta.textContent = "click ↗";
      card.classList.add("alarm");
    } else {
      glyph.textContent = "●";
      glyph.style.color = "var(--accent)";
      delta.textContent = "all clear";
      card.classList.remove("alarm");
    }
  }

  function renderAttention() {
    const list = $("attn-list");
    const empty = $("attn-empty");
    const count = $("attn-count");
    if (!list) return;

    // Hide dismissed entries that are still pending; expire old dismissals.
    const now = Date.now();
    for (const fp of Object.keys(persisted.attnDismissed)) {
      if (now - persisted.attnDismissed[fp] > 24 * 3600 * 1000) {
        delete persisted.attnDismissed[fp];
      }
    }

    let visible;
    if (!state.attention.available) {
      visible = []; // treat as all-clear
    } else {
      visible = (state.attention.items || []).filter(
        (it) => !(attentionFingerprint(it) in persisted.attnDismissed)
      );
    }

    list.innerHTML = "";
    if (visible.length === 0) {
      empty.style.display = "flex";
      list.style.display = "none";
    } else {
      empty.style.display = "none";
      list.style.display = "flex";
      for (const it of visible) {
        list.appendChild(buildAttentionRow(it));
      }
    }
    count.textContent = String(visible.length);
    setKpiAttention(visible.length);
  }

  function buildAttentionRow(it) {
    const sev = (it.severity || "low").toLowerCase();
    const sevLabel = sev === "high" ? "HIGH" : sev === "medium" ? "MED" : "LOW";
    const ts = it.since ? Date.parse(it.since) : null;
    const rel = ts ? fmtRelative(ts) : "";
    const actionUrl = it.action_url || derivedActionUrl(it);
    const row = el("div", { class: "attn-row sev-" + sev, role: "listitem" }, [
      el("div", { class: "attn-stripe" }),
      el("div", { class: "attn-meat" }, [
        el("div", { class: "attn-head" }, [
          el("span", { class: "attn-sev", text: "[" + sevLabel + "]" }),
          el("span", { class: "attn-kind", text: kindShort(it.kind || "") }),
          rel ? el("span", { class: "attn-rel", text: rel }) : null,
          el("span", { class: "attn-title", text: it.title || "" }),
        ]),
        it.body ? el("div", { class: "attn-body", text: it.body }) : null,
      ]),
      el("div", { class: "attn-actions" }, [
        actionUrl ? el("a", {
          class: "attn-action",
          href: actionUrl,
          text: actionLabel(it.kind) + " ↗",
        }) : null,
        el("button", {
          class: "attn-dismiss",
          title: "dismiss (will reappear after 24h or if it changes)",
          onclick: () => {
            persisted.attnDismissed[attentionFingerprint(it)] = Date.now();
            savePersisted();
            row.classList.add("attn-row-leaving");
            setTimeout(renderAttention, 180);
          },
          text: "×",
        }),
      ]),
    ]);
    return row;
  }

  function kindShort(k) {
    if (!k) return "—";
    return k.replace(/_/g, " ");
  }

  function actionLabel(kind) {
    switch (kind) {
      case "approval_pending": return "resolve";
      case "ollama_unreachable": return "retry";
      default: return "investigate";
    }
  }

  function derivedActionUrl(it) {
    if (it.related_event_id) {
      return "/static/debug.html?event=" + encodeURIComponent(it.related_event_id);
    }
    if (it.related_task_id) {
      return "/?task=" + encodeURIComponent(it.related_task_id);
    }
    if (it.kind === "ollama_unreachable") return "/chat";
    if (it.kind === "approval_pending") return "/static/agents.html";
    if (it.kind === "chat_disabled_with_pending_q") return "/static/profile.html";
    return "/static/debug.html";
  }

  // ------------------------------------------------------------------
  // Happening panel
  // ------------------------------------------------------------------

  function populateAgentFilter() {
    const sel = $("happen-agent-filter");
    if (!sel) return;
    const wanted = persisted.happenAgentFilter || "all";
    const present = new Set(["all", "active"]);
    for (const id of state.agents.keys()) present.add(id);

    // Add missing options
    for (const id of state.agents.keys()) {
      if (![...sel.options].some((o) => o.value === id)) {
        const opt = document.createElement("option");
        opt.value = id;
        opt.textContent = id;
        sel.appendChild(opt);
      }
    }
    // Remove options for vanished agents (other than all/active)
    for (const opt of [...sel.options]) {
      if (opt.value !== "all" && opt.value !== "active" && !state.agents.has(opt.value)) {
        sel.removeChild(opt);
      }
    }
    if (sel.value !== wanted && [...sel.options].some((o) => o.value === wanted)) {
      sel.value = wanted;
    }
  }

  function renderHappeningAgents() {
    const host = $("happen-agents");
    if (!host) return;
    host.innerHTML = "";
    const now = Date.now();
    const filter = persisted.happenAgentFilter;
    const all = Array.from(state.agents.values()).sort(
      (a, b) => (b.lastSeen || 0) - (a.lastSeen || 0)
    );
    const filtered = all.filter((entry) => {
      const a = entry.agent || {};
      const id = a.id || a.agent_id;
      if (filter === "all") return true;
      if (filter === "active") return (now - entry.lastSeen) < AGENT_ACTIVE_WINDOW_MS;
      return id === filter;
    });

    if (filtered.length === 0) {
      host.appendChild(el("div", { class: "happen-empty", text: "no agents have spoken yet" }));
      return;
    }
    for (const entry of filtered) {
      const a = entry.agent || {};
      const id = a.id || a.agent_id || "?";
      const active = (now - entry.lastSeen) < AGENT_ACTIVE_WINDOW_MS;
      const lastEv = a.last_event_kind || a.last_kind || "";
      host.appendChild(el("div", {
        class: "happen-agent" + (active ? " active" : ""),
      }, [
        el("span", { class: "ha-dot" + (active ? " pulse" : ""), text: active ? "●" : "○" }),
        el("span", { class: "ha-id mono", text: id }),
        el("span", { class: "ha-meta mono", text:
          (entry.lastSeen ? "(" + fmtRelative(entry.lastSeen) : "(idle")
          + (lastEv ? ", " + lastEv : "") + ")" }),
      ]));
    }
  }

  function renderInFlight() {
    const host = $("happen-dispatches");
    if (!host) return;
    host.innerHTML = "";
    const now = Date.now();
    // Garbage-collect very old in-flight entries (>5min)
    for (const [tid, item] of state.inFlight) {
      if (now - item.startMs > 5 * 60_000) state.inFlight.delete(tid);
    }
    const items = Array.from(state.inFlight.values()).sort((a, b) => b.startMs - a.startMs);
    if (items.length === 0) {
      host.appendChild(el("div", { class: "happen-empty", text: "no in-flight dispatches" }));
      return;
    }
    for (const it of items) {
      const elapsed = Math.floor((now - it.startMs) / 1000);
      host.appendChild(el("div", { class: "in-flight" }, [
        el("span", { class: "if-agent mono", text: "[" + (it.agent || "?") + "]" }),
        el("span", { class: "if-body", text: it.body || it.kind || "in flight" }),
        el("span", { class: "if-elapsed mono", text: "elapsed " + elapsed + "s" }),
      ]));
    }
  }

  function feedPassesKind(event) {
    const kf = persisted.happenFeedKind;
    if (!kf || kf === "all") return true;
    return (event.kind || "").startsWith(kf + ".");
  }

  function renderFeed() {
    const host = $("happen-feed");
    if (!host) return;
    host.innerHTML = "";
    const items = state.feed.filter(feedPassesKind).slice(0, MAX_FEED_LINES);
    if (items.length === 0) {
      host.appendChild(el("div", { class: "happen-empty", text: "no events yet — actions will appear here live" }));
      return;
    }
    for (const ev of items) {
      const cls = kindClass(ev.kind);
      host.appendChild(el("div", { class: "feed-row" }, [
        el("span", { class: "feed-ts mono", text: fmtTime(ev.timestamp || ev.timestamp_ms || Date.now()) }),
        el("span", { class: "feed-agent mono", text: ev.agent_id || "—" }),
        el("span", { class: "feed-kind mono " + cls, text: ev.kind || "—" }),
        el("span", { class: "feed-summary", text: summarizePayload(ev) }),
      ]));
    }
    // Restore scroll position
    if (persisted.happenFeedScroll) {
      host.scrollTop = persisted.happenFeedScroll;
    }
  }

  function kindClass(kind) {
    if (!kind) return "";
    if (kind.endsWith(".failed") || kind.endsWith(".rejected") || kind.endsWith(".denied")) return "bad";
    if (kind.endsWith(".completed") || kind.endsWith(".approved") || kind.endsWith(".executed") || kind.endsWith(".accepted")) return "ok";
    if (kind.startsWith("approval.") || kind.startsWith("handoff.")) return "warn";
    return "info";
  }

  function summarizePayload(event) {
    const p = event.payload || event.payload_preview;
    if (typeof p === "string") return truncate(p, 120);
    if (!p) return "";
    switch (event.kind) {
      case "task.created": return `goal=${truncate(JSON.stringify(p.goal || ""), 80)}`;
      case "task.status_changed": return `${p.from || "?"} → ${p.to || "?"}`;
      case "tool.proposed":
      case "tool.approved":
      case "tool.rejected":
      case "tool.executed": {
        const tool = p.tool || p.tool_name || "?";
        return `${tool}`;
      }
      case "memory.written": return `${p.scope || "?"} ${p.type || ""} (${p.source || ""})`;
      case "handoff.initiated": return `→ ${p.to_agent || "?"}`;
      case "handoff.accepted": return `from ${p.from_agent || "?"}`;
      case "approval.requested": return `${p.tool || "?"} (${p.risk_tier || "?"})`;
      case "approval.resolved": return `${p.resolution || "?"}`;
      default: {
        if (typeof p === "object") {
          const keys = Object.keys(p).slice(0, 3);
          return keys.map((k) => `${k}=${truncate(JSON.stringify(p[k]), 30)}`).join(" ");
        }
        return "";
      }
    }
  }

  // ------------------------------------------------------------------
  // Growth panel
  // ------------------------------------------------------------------

  function renderGrowth() {
    const setText = (id, v) => { const n = $(id); if (n) n.textContent = (v == null) ? "—" : String(v); };
    if (!state.growth.available || !state.growth.data) {
      setText("grown-records-today", "—");
      setText("grown-records-week", "—");
      setText("grown-chat-queries", "—");
      setText("grown-profile-q", "—");
      const link = $("grown-profile-link");
      if (link) link.hidden = true;
      const tags = $("grown-tags");
      if (tags) {
        tags.innerHTML = "";
        tags.appendChild(el("div", { class: "grown-empty", text: "—" }));
      }
      const dims = $("grown-dims");
      if (dims) {
        dims.innerHTML = "";
        dims.appendChild(el("div", { class: "grown-empty", text: "—" }));
      }
      return;
    }

    const d = state.growth.data;
    setText("grown-records-today", d.records_today);
    setText("grown-records-week", d.records_week);
    setText("grown-chat-queries", d.chat_queries_today);
    setText("grown-profile-q", d.profile_questions_open);
    const link = $("grown-profile-link");
    if (link) link.hidden = !(d.profile_questions_open > 0);

    const tags = $("grown-tags");
    if (tags) {
      tags.innerHTML = "";
      const arr = (d.top_tags || []).slice(0, 8);
      if (arr.length === 0) {
        tags.appendChild(el("div", { class: "grown-empty", text: "—" }));
      } else {
        const max = Math.max(1, ...arr.map((t) => t.count_today || 0));
        for (const t of arr) {
          const pct = Math.round(((t.count_today || 0) / max) * 100);
          const delta = (typeof t.delta_pct === "number")
            ? (t.delta_pct > 0 ? "+" : "") + Math.round(t.delta_pct) + "%"
            : (t.delta_pct === null ? "new" : "—");
          tags.appendChild(el("div", { class: "grown-tag-row" }, [
            el("span", { class: "gt-name mono", text: t.tag || "?" }),
            el("span", { class: "gt-bar" }, [
              el("span", { class: "gt-bar-fill", style: { width: pct + "%" } }),
            ]),
            el("span", { class: "gt-delta mono " + (
              typeof t.delta_pct === "number" && t.delta_pct < 0 ? "down"
              : t.delta_pct === null ? "new" : "up"
            ), text: delta }),
          ]));
        }
      }
    }

    const dims = $("grown-dims");
    if (dims) {
      dims.innerHTML = "";
      const arr = d.profile_dimensions_growing || [];
      if (arr.length === 0) {
        // empty here is information — render nothing.
      } else {
        for (const dim of arr) {
          dims.appendChild(el("div", { class: "grown-dim-row" }, [
            el("span", { class: "gd-name mono", text: dim.dimension || "?" }),
            el("span", { class: "gd-delta mono", text:
              "+" + (dim.added_week || 0) + " this week" }),
          ]));
        }
      }
    }
  }

  // ------------------------------------------------------------------
  // WebSocket
  // ------------------------------------------------------------------

  function setWsStatus(ok) {
    state.wsOk = ok;
    const dot = $("kpi-ws-dot");
    const txt = $("kpi-ws");
    if (!dot || !txt) return;
    dot.style.color = ok ? "var(--accent)" : "var(--danger)";
    txt.textContent = ok ? "live" : "offline";
  }

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
      ws.addEventListener("open", () => {
        backoff = 1000;
        setWsStatus(true);
      });
      ws.addEventListener("close", () => {
        setWsStatus(false);
        setTimeout(open, backoff = Math.min(backoff * 2, 8000));
      });
      ws.addEventListener("error", () => { /* close fires */ });
      ws.addEventListener("message", (msg) => {
        let event;
        try { event = JSON.parse(msg.data); } catch (_) { return; }
        if (event.kind === "__hello__") return;
        ingestEvent(event);
      });
    }
    open();
  }

  function ingestEvent(event) {
    state.eventsInWindow.push(Date.now());

    // Track agent last-seen
    if (event.agent_id) {
      const entry = state.agents.get(event.agent_id) || {
        agent: { id: event.agent_id, name: event.agent_id, capabilities: [] },
        lastSeen: 0,
      };
      entry.lastSeen = Date.now();
      state.agents.set(event.agent_id, entry);
    }

    // In-flight dispatches: open on dispatch.requested-ish kinds, close on done/failed.
    const k = event.kind || "";
    const tid = event.task_id || (event.payload && event.payload.task_id);
    if (tid) {
      if (k === "handoff.initiated" || k === "task.created" || k === "task.status_changed") {
        if (k === "handoff.initiated" || (k === "task.status_changed" && event.payload && event.payload.to === "in_progress")) {
          state.inFlight.set(tid, {
            agent: event.agent_id || "?",
            startMs: Date.now(),
            kind: k,
            body: summarizePayload(event) || "in-flight",
          });
        }
      }
      if (k === "task.completed" || k === "task.failed" || k === "handoff.accepted") {
        state.inFlight.delete(tid);
      }
    }

    // Feed: prepend, cap.
    if (!persisted.happenPaused) {
      state.feed.unshift(event);
      if (state.feed.length > MAX_FEED_LINES) state.feed.length = MAX_FEED_LINES;
      renderFeed();
    }

    // Refresh attention fast on failures / approvals
    if (k === "task.failed" || k === "tool.rejected" || k === "approval.requested" ||
        k === "policy.denied" || k.startsWith("dispatch.")) {
      scheduleAttentionRefresh();
    }

    // Refresh chains on handoff/dispatch/task.completed (debounced).
    if (k === "handoff.initiated" || k === "handoff.accepted" ||
        k === "dispatch.failed" || k === "dispatch.fallback" ||
        k === "task.completed" || k === "task.failed") {
      scheduleChainsRefresh();
    }

    // Renderers
    renderHappeningAgents();
    renderInFlight();
  }

  let attnRefreshTimer = null;
  function scheduleAttentionRefresh() {
    if (attnRefreshTimer) return;
    attnRefreshTimer = setTimeout(() => {
      attnRefreshTimer = null;
      refreshAttention();
    }, 600);
  }

  // ------------------------------------------------------------------
  // Handoff chains panel + drawer
  // ------------------------------------------------------------------

  function chainStatusGlyph(status) {
    const s = (status || "").toLowerCase();
    if (s === "completed" || s === "succeeded") return "✓";
    if (s === "failed") return "🔴";
    if (s === "running" || s === "in_progress") return "●";
    if (s === "cancelled") return "○";
    return "·";
  }

  function chainStatusClass(status) {
    const s = (status || "").toLowerCase();
    if (s === "completed" || s === "succeeded") return "ok";
    if (s === "failed") return "bad";
    if (s === "running" || s === "in_progress") return "running";
    return "neutral";
  }

  function chainDurationMs(chain) {
    const lo = chain.started_at ? Date.parse(chain.started_at) : null;
    const hi = chain.ended_at   ? Date.parse(chain.ended_at)   : null;
    if (lo == null) return null;
    if (hi == null) return Date.now() - lo;
    return Math.max(0, hi - lo);
  }

  function fmtDur(ms) {
    if (ms == null || ms < 0) return "—";
    const s = Math.round(ms / 1000);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    const rs = s % 60;
    if (m < 60) return `${m}m${rs.toString().padStart(2, "0")}s`;
    const h = Math.floor(m / 60);
    const rm = m % 60;
    return `${h}h${rm.toString().padStart(2, "0")}m`;
  }

  function shortChainId(s) {
    if (!s) return "";
    s = String(s);
    return s.length > 8 ? s.slice(0, 8) : s;
  }

  function renderChains() {
    const list = $("chains-list");
    const empty = $("chains-empty");
    const count = $("chains-count");
    if (!list) return;

    list.innerHTML = "";

    if (!state.chains.available) {
      const status = state.chains.lastStatus;
      const why = (status === 404 || status === 503)
        ? `endpoint not ready (${status}) — will retry`
        : status
          ? `chain endpoint returned ${status} — will retry`
          : "chain endpoint unreachable — will retry";
      empty.style.display = "flex";
      list.style.display = "none";
      const txt = empty.querySelector(".txt");
      if (txt) txt.textContent = why;
      count.textContent = "0";
      return;
    }

    // Filter by min hops client-side as well (defensive — backend already filters).
    const minHops = persisted.chainsMinHops || 1;
    const items = (state.chains.items || []).filter(
      (c) => (c.hops || (c.agents_path || []).length || 1) >= minHops
    );

    count.textContent = String(items.length);

    if (items.length === 0) {
      empty.style.display = "flex";
      list.style.display = "none";
      const txt = empty.querySelector(".txt");
      if (txt) {
        txt.innerHTML = "no chains in window — agents form chains when they pass " +
                        "<code>parent_task_id</code> to <code>dispatch_task</code>";
      }
      return;
    }

    empty.style.display = "none";
    list.style.display = "flex";
    for (const c of items) list.appendChild(buildChainCard(c));
  }

  function buildChainCard(chain) {
    const path = Array.isArray(chain.agents_path) && chain.agents_path.length
      ? chain.agents_path
      : (Array.isArray(chain.tasks)
          ? Array.from(new Set(chain.tasks.map((t) => t.agent_id).filter(Boolean)))
          : []);
    const hops = chain.hops || path.length || 1;
    const dur = fmtDur(chainDurationMs(chain));
    const sCls = chainStatusClass(chain.status);
    const glyph = chainStatusGlyph(chain.status);

    // Agent path: dot + name + arrow.
    const pathRow = el("div", { class: "chain-path" });
    for (let i = 0; i < path.length; i++) {
      const a = path[i];
      pathRow.appendChild(el("span", { class: "chain-node mono" }, [
        el("span", {
          class: "chain-dot",
          style: { color: agentColor(a) },
          text: "●",
        }),
        el("span", {
          class: "chain-aname",
          style: { color: agentColor(a) },
          text: a || "?",
        }),
      ]));
      if (i < path.length - 1) {
        pathRow.appendChild(el("span", { class: "chain-arrow mono", text: "→" }));
      }
    }

    const card = el("article", {
      class: "chain-card status-" + sCls,
      "data-chain-id": chain.chain_id || "",
    }, [
      el("div", { class: "chain-card-stripe" }),
      el("div", { class: "chain-card-meat" }, [
        el("div", { class: "chain-head mono" }, [
          el("span", { class: "ch-id", text: "chain " + shortChainId(chain.chain_id) }),
          el("span", { class: "ch-sep", text: " · " }),
          el("span", { class: "ch-hops", text: hops + " hops" }),
          el("span", { class: "ch-sep", text: " · " }),
          el("span", { class: "ch-dur", text: dur }),
          el("span", { class: "ch-sep", text: " · " }),
          el("span", {
            class: "ch-status status-" + sCls,
            text: glyph + " " + (chain.status || "unknown"),
          }),
        ]),
        pathRow,
      ]),
      el("div", { class: "chain-card-actions" }, [
        el("button", {
          class: "chain-view-btn",
          type: "button",
          text: "view ↗",
          onclick: (ev) => {
            ev.stopPropagation();
            openChainDrawer(chain.chain_id);
          },
        }),
      ]),
    ]);

    card.addEventListener("click", () => openChainDrawer(chain.chain_id));

    return card;
  }

  function openChainDrawer(chainId) {
    if (!chainId) return;
    let chain = state.chainCache.get(chainId);
    state.drawerOpen = true;
    state.drawerChainId = chainId;
    persisted.lastViewedChain = chainId;
    savePersisted();

    const drawer = $("chain-drawer");
    if (drawer) {
      drawer.classList.add("open");
      drawer.setAttribute("aria-hidden", "false");
    }

    if (chain) {
      renderDrawer(chain);
    } else {
      // Show loading state then fetch by chain_id (which is the root task id).
      renderDrawerLoading(chainId);
      fetchChainByTaskId(chainId).then((c) => {
        if (!state.drawerOpen || state.drawerChainId !== chainId) return;
        if (c) renderDrawer(c);
        else renderDrawerError(chainId);
      });
    }
  }

  function closeChainDrawer() {
    state.drawerOpen = false;
    state.drawerChainId = null;
    const drawer = $("chain-drawer");
    if (drawer) {
      drawer.classList.remove("open");
      drawer.setAttribute("aria-hidden", "true");
    }
  }

  function renderDrawer(chain) {
    const body = $("chain-drawer-body");
    if (!body) return;
    body.innerHTML = "";
    if (window.exocortexChains && typeof window.exocortexChains.renderSwimlane === "function") {
      window.exocortexChains.renderSwimlane(body, chain);
    } else {
      body.appendChild(el("div", { class: "chain-swim-empty mono",
        text: "swimlane module unavailable" }));
    }
  }

  function renderDrawerLoading(chainId) {
    const body = $("chain-drawer-body");
    if (!body) return;
    body.innerHTML = "";
    body.appendChild(el("div", { class: "chain-swim-empty mono",
      text: "loading chain " + shortChainId(chainId) + "…" }));
  }

  function renderDrawerError(chainId) {
    const body = $("chain-drawer-body");
    if (!body) return;
    body.innerHTML = "";
    body.appendChild(el("div", { class: "chain-swim-empty mono",
      text: "could not load chain " + shortChainId(chainId) +
            " — endpoint may not be ready" }));
  }

  let chainsRefreshTimer = null;
  function scheduleChainsRefresh() {
    if (chainsRefreshTimer) return;
    chainsRefreshTimer = setTimeout(() => {
      chainsRefreshTimer = null;
      refreshChains();
    }, CHAINS_REFRESH_DEBOUNCE_MS);
  }

  function applyChainChips() {
    const chips = document.querySelectorAll(".chain-chip");
    chips.forEach((c) => {
      const v = parseInt(c.getAttribute("data-min-hops"), 10) || 1;
      if (v === (persisted.chainsMinHops || 1)) c.classList.add("active");
      else c.classList.remove("active");
    });
  }

  // ------------------------------------------------------------------
  // Trace slide-out (preserved from v1)
  // ------------------------------------------------------------------

  async function openTrace(taskId) {
    const panel = $("trace-panel");
    const host = $("trace-events");
    $("trace-task-id").textContent = taskId;
    host.innerHTML = "loading…";
    panel.classList.add("open");
    const res = await fetchJson(`/api/tasks/${taskId}/trace`);
    if (!res._ok) {
      host.innerHTML = "";
      host.appendChild(el("div", { class: "trace-event", text: "Failed to load." }));
      return;
    }
    host.innerHTML = "";
    for (const ev of res.events || []) {
      host.appendChild(el("div", { class: "trace-event" }, [
        el("div", { class: "head" }, [
          el("span", { class: "ts", text: fmtTime(ev.timestamp) }),
          el("span", { class: "kind", text: ev.kind }),
          el("span", { text: ev.agent_id || "—" }),
        ]),
        el("div", { class: "payload", text: JSON.stringify(ev.payload, null, 2) }),
      ]));
    }
  }

  // ------------------------------------------------------------------
  // Bindings
  // ------------------------------------------------------------------

  function applyDensity() {
    document.body.dataset.density = persisted.density;
    const lab = $("dash-density-label");
    if (lab) lab.textContent = persisted.density;
  }

  function applyAttnCollapsed() {
    const panel = $("attn-panel");
    const btn = $("attn-collapse");
    if (!panel || !btn) return;
    if (persisted.attnCollapsed) {
      panel.classList.add("collapsed");
      btn.textContent = "▶";
      btn.setAttribute("aria-expanded", "false");
    } else {
      panel.classList.remove("collapsed");
      btn.textContent = "▼";
      btn.setAttribute("aria-expanded", "true");
    }
  }

  function bind() {
    const dens = $("dash-density");
    if (dens) {
      dens.addEventListener("click", () => {
        persisted.density = persisted.density === "compact" ? "comfortable" : "compact";
        savePersisted();
        applyDensity();
      });
    }

    const collapse = $("attn-collapse");
    if (collapse) {
      collapse.addEventListener("click", () => {
        persisted.attnCollapsed = !persisted.attnCollapsed;
        savePersisted();
        applyAttnCollapsed();
      });
    }

    const traceClose = $("trace-close");
    if (traceClose) {
      traceClose.addEventListener("click", () => {
        $("trace-panel").classList.remove("open");
      });
    }

    const af = $("happen-agent-filter");
    if (af) {
      af.value = persisted.happenAgentFilter || "all";
      af.addEventListener("change", () => {
        persisted.happenAgentFilter = af.value;
        savePersisted();
        renderHappeningAgents();
      });
    }

    const fk = $("happen-feed-kind");
    if (fk) {
      fk.value = persisted.happenFeedKind || "all";
      fk.addEventListener("change", () => {
        persisted.happenFeedKind = fk.value;
        savePersisted();
        renderFeed();
      });
    }

    const pause = $("happen-pause");
    if (pause) {
      pause.checked = !!persisted.happenPaused;
      pause.addEventListener("change", () => {
        persisted.happenPaused = pause.checked;
        savePersisted();
      });
    }

    const feed = $("happen-feed");
    if (feed) {
      feed.addEventListener("scroll", () => {
        persisted.happenFeedScroll = feed.scrollTop;
        // throttle save
        clearTimeout(state._scrollSaveTimer);
        state._scrollSaveTimer = setTimeout(savePersisted, 300);
      });
    }

    const attnCard = $("kpi-attention-card");
    if (attnCard) {
      attnCard.addEventListener("click", () => {
        const panel = $("attn-panel");
        if (!panel) return;
        if (persisted.attnCollapsed) {
          persisted.attnCollapsed = false;
          savePersisted();
          applyAttnCollapsed();
        }
        panel.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    }

    // Chain chips
    const chips = document.querySelectorAll(".chain-chip");
    chips.forEach((c) => {
      c.addEventListener("click", () => {
        const v = parseInt(c.getAttribute("data-min-hops"), 10) || 1;
        persisted.chainsMinHops = v;
        savePersisted();
        applyChainChips();
        renderChains();
        // Re-fetch with new threshold
        refreshChains();
      });
    });

    // Chains time-window select
    const winSel = $("chains-window");
    if (winSel) {
      winSel.value = persisted.chainsTimeWindow || "24h";
      winSel.addEventListener("change", () => {
        persisted.chainsTimeWindow = winSel.value;
        savePersisted();
        refreshChains();
      });
    }

    // Drawer close
    const drawerClose = $("chain-drawer-close");
    if (drawerClose) {
      drawerClose.addEventListener("click", (ev) => {
        ev.stopPropagation();
        closeChainDrawer();
      });
    }

    // Click outside drawer to close.
    document.addEventListener("click", (ev) => {
      if (!state.drawerOpen) return;
      const drawer = $("chain-drawer");
      if (!drawer) return;
      if (drawer.contains(ev.target)) return;
      // Ignore clicks that originated in the chain list (they re-open or open another chain).
      const card = ev.target.closest && ev.target.closest(".chain-card");
      const chip = ev.target.closest && ev.target.closest(".chain-chip");
      const winsel = ev.target.closest && ev.target.closest("#chains-window");
      if (card || chip || winsel) return;
      closeChainDrawer();
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        if (state.drawerOpen) {
          closeChainDrawer();
        } else {
          $("trace-panel").classList.remove("open");
        }
      }
    });
  }

  // ------------------------------------------------------------------
  // Boot
  // ------------------------------------------------------------------

  async function boot() {
    // Apply persisted UI state BEFORE first paint.
    applyDensity();
    applyAttnCollapsed();
    applyChainChips();
    bind();

    await Promise.all([
      refreshStatus(), refreshTasks(), refreshAgents(),
      refreshAttention(), refreshGrowth(), refreshActivity(),
      refreshChains(),
    ]);

    drawSparklines();
    renderHappeningAgents();
    renderInFlight();
    renderFeed();
    renderGrowth();
    renderAttention();
    renderChains();

    connectWs();

    setInterval(refreshStatus, 10_000);
    setInterval(refreshTasks, 30_000);
    setInterval(refreshAgents, 30_000);
    setInterval(refreshAttention, ATTENTION_POLL_MS);
    setInterval(refreshGrowth, GROWTH_POLL_MS);
    setInterval(refreshChains, CHAINS_POLL_MS);
    setInterval(renderHappeningAgents, 5_000);
    setInterval(renderInFlight, 1_000);
    setInterval(tickEps, 1_000);
    setInterval(rotateSparkBucketsIfDue, 5_000);

    const url = new URL(window.location);
    const q = url.searchParams.get("task");
    if (q) openTrace(q);
    const chainQ = url.searchParams.get("chain");
    if (chainQ) {
      openChainDrawer(chainQ);
    } else if (persisted.lastViewedChain) {
      // Reopen last-viewed chain as a UX nicety, but only if it still appears
      // in the current (unfiltered) chain set.
      const exists = (state.chains.items || []).some(
        (c) => c.chain_id === persisted.lastViewedChain
      );
      if (exists) openChainDrawer(persisted.lastViewedChain);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
