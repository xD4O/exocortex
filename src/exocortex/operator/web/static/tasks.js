// tasks.js — /tasks (Mission Brief)
//
// A live-mission console. Four zones, top to bottom:
//   1. NOW RUNNING       — hero cards, full width, breathing animation,
//                          rotating progress ring around the agent dot,
//                          live-events ticker, ticking elapsed counter.
//   2. RECENTLY COMPLETED — horizontal scroll shelf of dense cards.
//   3. FAILED MISSIONS   — vertical list with red stripe + reason text.
//   4. ARCHIVE           — dense, scannable, searchable, chip-filtered.
//
// Click any task -> a 480px side panel slides in from the right with the
// full mission brief: meta grid, goal, last decision, lineage breadcrumb,
// last 10 events, and quick-jump links into agents / debug.
//
// Hard rules:
//   - existing palette only (see /static/app.css :root)
//   - all transitions <= 300ms; the slow ring rotation is a documented
//     exception and is purely decorative
//   - mono for ids/agent names/timestamps; sans for goal text
//   - when /api/tasks 404/503s, render an empty state cleanly (no crash)
//
// localStorage key: "exocortex.tasks.v2"

const { truncate, agentColor, FALLBACK_AGENT_COLOR } = window.Exo;

// ---------------------------------------------------------------------------
// Constants — palette + behaviour
// ---------------------------------------------------------------------------

const STATUS_COLOR = {
  proposed:    "#8b9bab",
  routed:      "#3a6f9e",
  in_progress: "#58a6ff",
  completed:   "#7ee787",
  failed:      "#f85149",
};
const STATUS_GLYPH = {
  proposed: "○",
  routed: "◐",
  in_progress: "◉",
  completed: "✓",
  failed: "●",
};

const STORAGE_KEY = "exocortex.tasks.v2";
const REFETCH_DEBOUNCE_MS = 400;
const REFETCH_INTERVAL_MS = 30_000;
const ACTIVE_TRACE_INTERVAL_MS = 5_000;
const TICK_INTERVAL_MS = 1_000;
const ARCHIVE_PAGE_SIZE = 50;
const SHELF_LIMIT = 24; // cap the recent-completed shelf so it stays useful
const FAILED_LIMIT = 12;

// Active statuses — anything here is shown in the NOW RUNNING zone.
const ACTIVE_STATUSES = new Set(["proposed", "routed", "in_progress"]);

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  tasks: new Map(),                // task_id -> task object
  prevStatus: new Map(),           // task_id -> previous status (for pulse)
  traceCache: new Map(),           // task_id -> { events: [], fetchedAt }
  available: true,                 // false if /api/tasks gave 404/503
  selectedTaskId: null,
  archiveFilter: "all",
  archiveSearch: "",
  archivePage: 1,                  // show 50 * N rows
  refetchTimer: null,
  ws: null,
  // For active tasks: a periodic trace refresh.
  activeTraceTimer: null,
  // For ticking elapsed counters (every 1s).
  tickTimer: null,
};

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

function loadPersisted() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    const obj = JSON.parse(raw);
    if (obj && typeof obj === "object") {
      if (typeof obj.archiveFilter === "string") state.archiveFilter = obj.archiveFilter;
      if (typeof obj.archiveSearch === "string") state.archiveSearch = obj.archiveSearch;
      if (Number.isFinite(obj.archivePage) && obj.archivePage > 0) {
        state.archivePage = Math.min(20, obj.archivePage | 0);
      }
      if (typeof obj.selectedTaskId === "string") state.selectedTaskId = obj.selectedTaskId;
    }
  } catch (_) { /* ignore */ }
}

function persist() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      archiveFilter: state.archiveFilter,
      archiveSearch: state.archiveSearch,
      archivePage: state.archivePage,
      selectedTaskId: state.selectedTaskId,
    }));
  } catch (_) { /* ignore quota */ }
}

// ---------------------------------------------------------------------------
// Tiny DOM helpers
// ---------------------------------------------------------------------------

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

function svgEl(tag, attrs, children) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  if (attrs) {
    for (const k of Object.keys(attrs)) {
      if (attrs[k] == null) continue;
      node.setAttribute(k, String(attrs[k]));
    }
  }
  if (Array.isArray(children)) for (const c of children) if (c) node.appendChild(c);
  return node;
}

// ---------------------------------------------------------------------------
// Formatters
// ---------------------------------------------------------------------------

function statusColor(s) { return STATUS_COLOR[s] || FALLBACK_AGENT_COLOR; }
function statusGlyph(s) { return STATUS_GLYPH[s] || "·"; }

function bucketFor(status) {
  if (status === "completed") return "completed";
  if (status === "failed") return "failed";
  return "open";
}

function parseMs(input) {
  if (!input) return 0;
  if (typeof input === "number") return input;
  const ms = Date.parse(input);
  return Number.isFinite(ms) ? ms : 0;
}

function fmtRelative(input) {
  const ms = parseMs(input);
  if (!ms) return "—";
  const diff = Date.now() - ms;
  if (diff < 0) return "just now";
  if (diff < 60_000) return Math.max(1, Math.floor(diff / 1000)) + "s ago";
  if (diff < 3_600_000) return Math.floor(diff / 60_000) + "m ago";
  if (diff < 86_400_000) return Math.floor(diff / 3_600_000) + "h ago";
  return Math.floor(diff / 86_400_000) + "d ago";
}

function fmtRelativeShort(input) {
  const ms = parseMs(input);
  if (!ms) return "—";
  const diff = Date.now() - ms;
  if (diff < 60_000) return Math.max(1, Math.floor(diff / 1000)) + "s";
  if (diff < 3_600_000) return Math.floor(diff / 60_000) + "m";
  if (diff < 86_400_000) return Math.floor(diff / 3_600_000) + "h";
  return Math.floor(diff / 86_400_000) + "d";
}

// "2m 14s" style elapsed for the hero's live counter.
function fmtElapsed(input) {
  const ms = parseMs(input);
  if (!ms) return "—";
  let s = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60); s = s - m * 60;
  if (m < 60) return m + "m " + String(s).padStart(2, "0") + "s";
  const h = Math.floor(m / 60); const mm = m - h * 60;
  return h + "h " + String(mm).padStart(2, "0") + "m";
}

// "2m 04s" duration between two iso/ms.
function fmtDuration(start, end) {
  const a = parseMs(start), b = parseMs(end);
  if (!a || !b) return "—";
  let s = Math.max(0, Math.floor((b - a) / 1000));
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60); s = s - m * 60;
  if (m < 60) return m + "m " + String(s).padStart(2, "0") + "s";
  const h = Math.floor(m / 60); const mm = m - h * 60;
  return h + "h " + String(mm).padStart(2, "0") + "m";
}

function fmtClock(input) {
  const ms = parseMs(input);
  if (!ms) return "—";
  const d = new Date(ms);
  return String(d.getHours()).padStart(2, "0") + ":" +
         String(d.getMinutes()).padStart(2, "0") + ":" +
         String(d.getSeconds()).padStart(2, "0");
}

function shortId(s) {
  if (!s) return "—";
  return String(s).slice(0, 8);
}

function debounce(fn, delay) {
  let t = null;
  return function (...args) {
    if (t) clearTimeout(t);
    t = setTimeout(() => fn(...args), delay);
  };
}

// Pull a human-ish failure reason out of whatever we have on the task.
function reasonFor(task) {
  const fields = [task.last_decision, task.failure_reason, task.error, task.detail];
  for (const f of fields) {
    if (typeof f === "string" && f.trim()) return f.trim();
  }
  return "(no reason recorded)";
}

// Lineage chain: walk parents up. Returns oldest-first list of tasks.
function lineageChain(t) {
  const chain = [];
  let cursor = t;
  let safety = 0;
  while (cursor && safety < 16) {
    chain.unshift(cursor);
    const p = cursor.parent_task_id;
    if (!p) break;
    cursor = state.tasks.get(p);
    safety += 1;
  }
  return chain;
}

function ownerOf(t) {
  return t.owning_agent || (Array.isArray(t.agents) && t.agents[0]) || null;
}

function highlightJson(obj) {
  let s;
  try { s = JSON.stringify(obj, null, 2); } catch (_) { return ""; }
  s = s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return s.replace(
    /("(?:[^"\\]|\\.)*")(\s*:)?|\b(true|false|null)\b|\b(-?\d+(?:\.\d+)?)\b/g,
    (m, str, colon, bool, num) => {
      if (str) return `<span class="${colon ? "json-k" : "json-s"}">${str}</span>${colon || ""}`;
      if (bool) return `<span class="json-b">${bool}</span>`;
      if (num) return `<span class="json-n">${num}</span>`;
      return m;
    }
  );
}

function previewPayload(payload) {
  if (payload == null) return "";
  if (typeof payload === "string") return truncate(payload, 80);
  // pull out the most informative-looking field
  const keys = ["text", "content", "message", "decision", "goal", "reason", "tool", "path", "kind"];
  for (const k of keys) {
    if (payload[k] != null) return truncate(String(payload[k]), 80);
  }
  try {
    const s = JSON.stringify(payload);
    return truncate(s, 80);
  } catch (_) { return ""; }
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

async function fetchTasks() {
  try {
    const r = await fetch("/api/tasks?limit=200");
    if (!r.ok) {
      if (r.status === 404 || r.status === 503) {
        state.available = false;
        renderAll();
      }
      return;
    }
    const data = await r.json();
    state.available = true;
    const items = Array.isArray(data.items)
      ? data.items
      : (Array.isArray(data.tasks) ? data.tasks : []);
    ingestTasks(items);
    renderAll();
    // For each active task, refresh its trace.
    refreshActiveTraces();
  } catch (_) {
    state.available = false;
    renderAll();
  }
}

async function fetchTaskTrace(taskId) {
  try {
    const r = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/trace`);
    if (!r.ok) return null;
    return await r.json();
  } catch (_) {
    return null;
  }
}

function ingestTasks(items) {
  const incoming = new Set();
  for (const t of items) {
    const tid = t.task_id || t.id;
    if (!tid) continue;
    incoming.add(tid);
    const existing = state.tasks.get(tid);
    if (existing && existing.status !== t.status) {
      state.prevStatus.set(tid, existing.status);
    }
    state.tasks.set(tid, t);
  }
  // Drop tasks that fell off the limit window.
  for (const tid of [...state.tasks.keys()]) {
    if (!incoming.has(tid)) {
      state.tasks.delete(tid);
      state.traceCache.delete(tid);
    }
  }
}

async function refreshActiveTraces() {
  const actives = [...state.tasks.values()].filter((t) => ACTIVE_STATUSES.has(t.status));
  // Sequential is fine — usually <= 3 active tasks.
  for (const t of actives) {
    const tid = t.task_id || t.id;
    const trace = await fetchTaskTrace(tid);
    if (!trace) continue;
    const events = Array.isArray(trace.events) ? trace.events : [];
    state.traceCache.set(tid, { events, fetchedAt: Date.now() });
    // Update just this hero card's ticker.
    const tickerHost = document.querySelector(
      `.tasks-now-card[data-task-id="${cssEsc(tid)}"] .tasks-now-ticker`
    );
    if (tickerHost) renderTicker(tickerHost, events.slice(-5).reverse());
  }
}

function cssEsc(s) {
  if (window.CSS && CSS.escape) return CSS.escape(s);
  return String(s).replace(/["\\]/g, "\\$&");
}

// ---------------------------------------------------------------------------
// Rendering — KPI strip
// ---------------------------------------------------------------------------

function updateKpis() {
  const tasks = [...state.tasks.values()];
  let running = 0, done = 0, failed = 0;
  for (const t of tasks) {
    const b = bucketFor(t.status);
    if (b === "completed") done += 1;
    else if (b === "failed") failed += 1;
    else if (ACTIVE_STATUSES.has(t.status)) running += 1;
  }
  const setN = (id, v) => { const e = $(id); if (e) e.textContent = String(v); };
  setN("tasks-kpi-total", tasks.length);
  setN("tasks-kpi-running", running);
  setN("tasks-kpi-done", done);
  setN("tasks-kpi-failed", failed);
}

// ---------------------------------------------------------------------------
// Rendering — top-level
// ---------------------------------------------------------------------------

function renderAll() {
  const loading = $("tasks-loading");
  if (loading) loading.hidden = true;

  if (!state.available) {
    renderUnavailable();
    return;
  }

  updateKpis();
  renderNowRunning();
  renderShelf();
  renderFailed();
  renderArchive();
}

function renderUnavailable() {
  const zones = ["tasks-zone-now", "tasks-zone-done", "tasks-zone-failed"];
  for (const id of zones) { const e = $(id); if (e) e.hidden = true; }
  const list = $("tasks-archive-list");
  const arc = $("tasks-zone-archive");
  if (arc) arc.hidden = false;
  if (list) {
    list.innerHTML = "";
    list.appendChild(el("div", {
      class: "tasks-empty-text",
      text: "tasks endpoint not ready (404/503) — will retry as backend ships",
    }));
  }
}

// ---------------------------------------------------------------------------
// Zone 1 — NOW RUNNING (hero cards)
// ---------------------------------------------------------------------------

function renderNowRunning() {
  const zone = $("tasks-zone-now");
  const stack = $("tasks-now-stack");
  const empty = $("tasks-now-empty");
  const count = $("tasks-now-count");
  if (!zone || !stack) return;

  const all = [...state.tasks.values()].filter((t) => ACTIVE_STATUSES.has(t.status));
  // Most recent activity first.
  all.sort((a, b) => parseMs(b.last_event_at) - parseMs(a.last_event_at));

  zone.hidden = false;
  if (count) count.textContent = String(all.length);

  if (all.length === 0) {
    stack.innerHTML = "";
    if (empty) empty.hidden = false;
    return;
  }
  if (empty) empty.hidden = true;

  // Diff-render — keep cards stable so the breathing/ring animation doesn't
  // restart on every refetch.
  const seen = new Set();
  for (const t of all) {
    const tid = t.task_id || t.id;
    seen.add(tid);
    let card = stack.querySelector(`.tasks-now-card[data-task-id="${cssEsc(tid)}"]`);
    if (!card) {
      card = buildNowCard(t);
      stack.appendChild(card);
    } else {
      updateNowCard(card, t);
    }
    if (state.prevStatus.get(tid)) {
      card.classList.add("tasks-card-pulse");
      setTimeout(() => card.classList.remove("tasks-card-pulse"), 600);
      state.prevStatus.delete(tid);
    }
  }
  // Remove gone cards.
  for (const node of [...stack.querySelectorAll(".tasks-now-card")]) {
    const tid = node.getAttribute("data-task-id");
    if (!seen.has(tid)) {
      node.classList.add("tasks-card-leaving");
      setTimeout(() => { if (node.parentNode) node.parentNode.removeChild(node); }, 240);
    }
  }
}

function buildNowCard(t) {
  const tid = t.task_id || t.id;
  const owner = ownerOf(t);
  const ringColor = agentColor(owner);

  // Rotating ring SVG: 26x26 conic-via-stroke-dasharray. Slow infinite spin.
  const ringSize = 26;
  const ringR = 11;
  const ringC = 2 * Math.PI * ringR;
  const ringSvg = svgEl("svg", {
    class: "tasks-now-ring",
    width: ringSize, height: ringSize, viewBox: `0 0 ${ringSize} ${ringSize}`,
  }, [
    svgEl("circle", {
      cx: ringSize / 2, cy: ringSize / 2, r: ringR,
      fill: "none", stroke: "rgba(255,255,255,0.06)", "stroke-width": 2,
    }),
    svgEl("circle", {
      class: "tasks-now-ring-arc",
      cx: ringSize / 2, cy: ringSize / 2, r: ringR,
      fill: "none", stroke: ringColor, "stroke-width": 2,
      "stroke-linecap": "round",
      "stroke-dasharray": `${(ringC * 0.28).toFixed(2)} ${ringC.toFixed(2)}`,
      "stroke-dashoffset": "0",
      transform: `rotate(-90 ${ringSize / 2} ${ringSize / 2})`,
    }),
    svgEl("circle", {
      cx: ringSize / 2, cy: ringSize / 2, r: 3.2,
      fill: ringColor,
    }),
  ]);

  const card = el("article", {
    class: "tasks-now-card",
    "data-task-id": tid,
    "data-status": t.status,
    role: "button",
    tabindex: "0",
    onclick: () => selectTask(tid),
    onkeydown: (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectTask(tid); }
    },
  });
  card.style.setProperty("--tasks-agent-color", ringColor);

  // -- Header: agent + status + elapsed + events ---------------------------
  const head = el("header", { class: "tasks-now-head" }, [
    el("div", { class: "tasks-now-id-block" }, [
      el("span", { class: "tasks-now-ring-wrap" }, [ringSvg]),
      el("div", { class: "tasks-now-id-text" }, [
        el("div", { class: "tasks-now-agent mono", style: `color:${ringColor}`,
          text: owner || "unassigned" }),
        el("div", { class: "tasks-now-id mono", text: shortId(tid) }),
      ]),
    ]),
    el("div", { class: "tasks-now-meters" }, [
      el("div", { class: "tasks-now-meter" }, [
        el("span", { class: "tasks-now-meter-label", text: "status" }),
        el("span", {
          class: "tasks-now-meter-val mono tasks-now-status",
          style: `color:${statusColor(t.status)}`,
          text: statusGlyph(t.status) + "  " + (t.status || "—"),
        }),
      ]),
      el("div", { class: "tasks-now-meter" }, [
        el("span", { class: "tasks-now-meter-label", text: "elapsed" }),
        el("span", {
          class: "tasks-now-meter-val mono tasks-now-elapsed",
          text: fmtElapsed(t.created_at),
        }),
      ]),
      el("div", { class: "tasks-now-meter" }, [
        el("span", { class: "tasks-now-meter-label", text: "events" }),
        el("span", {
          class: "tasks-now-meter-val mono tasks-now-events",
          text: String(t.event_count || 0),
        }),
      ]),
    ]),
  ]);
  card.appendChild(head);

  // -- Goal -----------------------------------------------------------------
  card.appendChild(el("div", { class: "tasks-now-section" }, [
    el("div", { class: "tasks-section-label", text: "goal" }),
    el("p", { class: "tasks-now-goal", text: t.goal || t.title || "(no goal)" }),
  ]));

  // -- Live events ticker ---------------------------------------------------
  const ticker = el("div", { class: "tasks-now-ticker", "aria-live": "polite" });
  ticker.appendChild(el("div", { class: "tasks-ticker-empty mono", text: "waiting for events…" }));
  card.appendChild(el("div", { class: "tasks-now-section" }, [
    el("div", { class: "tasks-section-label", text: "live events" }),
    ticker,
  ]));

  // Pre-fill from cache if we have one.
  const cached = state.traceCache.get(tid);
  if (cached && Array.isArray(cached.events) && cached.events.length) {
    renderTicker(ticker, cached.events.slice(-5).reverse());
  }

  // -- Lineage breadcrumb ---------------------------------------------------
  const lineage = el("div", { class: "tasks-now-lineage" });
  buildLineage(lineage, t, /*clickable=*/false);
  card.appendChild(el("div", { class: "tasks-now-section" }, [
    el("div", { class: "tasks-section-label", text: "lineage" }),
    lineage,
  ]));

  // -- Footer actions -------------------------------------------------------
  card.appendChild(el("div", { class: "tasks-now-foot" }, [
    el("button", {
      type: "button", class: "tasks-now-btn primary",
      onclick: (e) => { e.stopPropagation(); selectTask(tid); },
      text: "open detail ↗",
    }),
    el("a", {
      class: "tasks-now-btn ghost",
      href: "/agents?task=" + encodeURIComponent(tid),
      target: "_blank",
      rel: "noopener noreferrer",
      onclick: (e) => e.stopPropagation(),
      text: "agents view ↗",
    }),
  ]));

  return card;
}

function updateNowCard(card, t) {
  const owner = ownerOf(t);
  const ringColor = agentColor(owner);
  card.style.setProperty("--tasks-agent-color", ringColor);
  card.dataset.status = t.status;

  const setText = (sel, txt, color) => {
    const e = card.querySelector(sel);
    if (e) {
      e.textContent = txt;
      if (color) e.style.color = color;
    }
  };
  setText(".tasks-now-agent", owner || "unassigned", ringColor);
  setText(".tasks-now-id", shortId(t.task_id || t.id));
  setText(".tasks-now-status", statusGlyph(t.status) + "  " + (t.status || "—"), statusColor(t.status));
  setText(".tasks-now-elapsed", fmtElapsed(t.created_at));
  setText(".tasks-now-events", String(t.event_count || 0));
  const goal = card.querySelector(".tasks-now-goal");
  if (goal) goal.textContent = t.goal || t.title || "(no goal)";
  const lineage = card.querySelector(".tasks-now-lineage");
  if (lineage) {
    lineage.innerHTML = "";
    buildLineage(lineage, t, false);
  }
  // ring arc color
  const arc = card.querySelector(".tasks-now-ring-arc");
  if (arc) arc.setAttribute("stroke", ringColor);
}

function renderTicker(host, events) {
  // events is newest-first.
  const prevKeys = new Set([...host.querySelectorAll(".tasks-tick-row")]
    .map((n) => n.getAttribute("data-evk")));
  host.innerHTML = "";
  if (!events.length) {
    host.appendChild(el("div", { class: "tasks-ticker-empty mono", text: "waiting for events…" }));
    return;
  }
  for (const ev of events) {
    const key = (ev.id || "") + "|" + (ev.timestamp || "");
    const isNew = !prevKeys.has(key);
    const row = el("div", {
      class: "tasks-tick-row" + (isNew ? " tasks-tick-new" : ""),
      "data-evk": key,
    }, [
      el("span", { class: "tasks-tick-ts mono", text: fmtClock(ev.timestamp) }),
      el("span", { class: "tasks-tick-kind mono", text: truncate(ev.kind || "event", 22) }),
      el("span", { class: "tasks-tick-preview mono", text: previewPayload(ev.payload) }),
    ]);
    host.appendChild(row);
  }
}

function buildLineage(host, t, clickable) {
  const chain = lineageChain(t);
  if (chain.length === 0) {
    host.appendChild(el("span", { class: "tasks-lineage-empty mono", text: "(root)" }));
    return;
  }
  // Always prepend an "operator" marker if we're inside the system at all.
  const opDot = el("span", { class: "tasks-lineage-step mono",
    style: { color: FALLBACK_AGENT_COLOR }, text: "● operator" });
  host.appendChild(opDot);
  host.appendChild(el("span", { class: "tasks-lineage-arrow mono", text: "→" }));

  chain.forEach((c, i) => {
    const owner = ownerOf(c) || "?";
    const color = agentColor(owner);
    const tid = c.task_id || c.id;
    const isCurrent = tid === (t.task_id || t.id);
    const step = el(clickable ? "button" : "span", {
      class: "tasks-lineage-step mono" + (isCurrent ? " current" : ""),
      style: { color },
      title: shortId(tid),
      type: clickable ? "button" : null,
      onclick: clickable ? (e) => { e.stopPropagation(); selectTask(tid); } : null,
      text: "● " + owner,
    });
    host.appendChild(step);
    if (i < chain.length - 1) {
      host.appendChild(el("span", { class: "tasks-lineage-arrow mono", text: "→" }));
    }
  });
}

// ---------------------------------------------------------------------------
// Zone 2 — RECENTLY COMPLETED (shelf)
// ---------------------------------------------------------------------------

function renderShelf() {
  const zone = $("tasks-zone-done");
  const shelf = $("tasks-shelf");
  const count = $("tasks-done-count");
  if (!zone || !shelf) return;

  const completed = [...state.tasks.values()]
    .filter((t) => bucketFor(t.status) === "completed")
    .sort((a, b) => parseMs(b.last_event_at) - parseMs(a.last_event_at))
    .slice(0, SHELF_LIMIT);

  if (count) count.textContent = String(completed.length);
  if (completed.length === 0) { zone.hidden = true; shelf.innerHTML = ""; return; }
  zone.hidden = false;

  shelf.innerHTML = "";
  for (const t of completed) {
    const tid = t.task_id || t.id;
    const owner = ownerOf(t);
    const color = agentColor(owner);
    const card = el("button", {
      type: "button",
      class: "tasks-shelf-card",
      "data-task-id": tid,
      onclick: () => selectTask(tid),
    }, [
      el("div", { class: "tasks-shelf-top" }, [
        el("span", { class: "tasks-shelf-glyph mono", text: "✓" }),
        el("span", { class: "tasks-shelf-agent mono", style: { color }, text: owner || "—" }),
        el("span", { class: "tasks-shelf-rel mono", text: fmtRelativeShort(t.last_event_at) }),
      ]),
      el("div", { class: "tasks-shelf-goal", text: t.goal || t.title || "(no goal)" }),
      el("div", { class: "tasks-shelf-foot mono",
        text: fmtDuration(t.created_at, t.last_event_at) }),
    ]);
    shelf.appendChild(card);
  }
}

// ---------------------------------------------------------------------------
// Zone 3 — FAILED MISSIONS (vertical list)
// ---------------------------------------------------------------------------

function renderFailed() {
  const zone = $("tasks-zone-failed");
  const list = $("tasks-failed-list");
  const count = $("tasks-failed-count");
  if (!zone || !list) return;

  const failed = [...state.tasks.values()]
    .filter((t) => bucketFor(t.status) === "failed")
    .sort((a, b) => parseMs(b.last_event_at) - parseMs(a.last_event_at))
    .slice(0, FAILED_LIMIT);

  if (count) count.textContent = String(failed.length);
  if (failed.length === 0) { zone.hidden = true; list.innerHTML = ""; return; }
  zone.hidden = false;

  list.innerHTML = "";
  for (const t of failed) {
    const tid = t.task_id || t.id;
    const owner = ownerOf(t);
    const color = agentColor(owner);
    const row = el("div", {
      class: "tasks-failed-card",
      "data-task-id": tid,
      onclick: () => selectTask(tid),
    }, [
      el("div", { class: "tasks-failed-stripe", "aria-hidden": "true" }),
      el("div", { class: "tasks-failed-body" }, [
        el("div", { class: "tasks-failed-top" }, [
          el("span", { class: "tasks-failed-glyph mono", text: "●" }),
          el("span", { class: "tasks-failed-agent mono", style: { color }, text: owner || "—" }),
          el("span", { class: "tasks-failed-rel mono", text: fmtRelative(t.last_event_at) }),
        ]),
        el("div", { class: "tasks-failed-goal",
          text: t.goal || t.title || "(no goal)" }),
        el("div", { class: "tasks-failed-reason mono",
          text: truncate(reasonFor(t), 220) }),
      ]),
      el("button", {
        type: "button",
        class: "tasks-failed-btn",
        onclick: (e) => { e.stopPropagation(); selectTask(tid); },
        text: "investigate ↗",
      }),
    ]);
    list.appendChild(row);
  }
}

// ---------------------------------------------------------------------------
// Zone 4 — ARCHIVE (search + filter chips + dense list + paginate)
// ---------------------------------------------------------------------------

function renderArchive() {
  const list = $("tasks-archive-list");
  const foot = $("tasks-archive-foot");
  const count = $("tasks-archive-count");
  if (!list) return;

  const all = [...state.tasks.values()];
  const q = (state.archiveSearch || "").trim().toLowerCase();
  const filter = state.archiveFilter || "all";

  const filtered = all.filter((t) => {
    if (filter !== "all" && bucketFor(t.status) !== filter) return false;
    if (q) {
      const hay = (
        (t.goal || "") + " " + (t.title || "") + " " + (t.task_id || "") + " " +
        (ownerOf(t) || "") + " " + (Array.isArray(t.agents) ? t.agents.join(" ") : "")
      ).toLowerCase();
      if (hay.indexOf(q) === -1) return false;
    }
    return true;
  });
  filtered.sort((a, b) => parseMs(b.last_event_at) - parseMs(a.last_event_at));

  if (count) count.textContent = String(filtered.length);

  // Sync the chip + search input UI to state.
  document.querySelectorAll("#tasks-archive-chips .tasks-chip").forEach((c) => {
    c.classList.toggle("active", c.dataset.filter === filter);
  });
  const search = $("tasks-archive-search");
  if (search && search.value !== state.archiveSearch) {
    search.value = state.archiveSearch;
  }

  list.innerHTML = "";
  if (filtered.length === 0) {
    list.appendChild(el("div", { class: "tasks-archive-empty",
      text: q ? "no missions match your search" : "no missions in this filter" }));
    if (foot) foot.innerHTML = "";
    return;
  }

  const limit = Math.min(filtered.length, state.archivePage * ARCHIVE_PAGE_SIZE);
  for (let i = 0; i < limit; i++) {
    const t = filtered[i];
    const tid = t.task_id || t.id;
    const owner = ownerOf(t);
    const color = agentColor(owner);
    const bucket = bucketFor(t.status);
    const row = el("div", {
      class: "tasks-arc-row",
      "data-task-id": tid,
      "data-bucket": bucket,
      onclick: () => selectTask(tid),
    }, [
      el("span", { class: "tasks-arc-rel mono", text: fmtRelativeShort(t.last_event_at) }),
      el("span", {
        class: "tasks-arc-glyph mono",
        style: { color: statusColor(t.status) },
        text: statusGlyph(t.status),
        title: t.status,
      }),
      el("span", {
        class: "tasks-arc-agent mono",
        style: { color },
        text: owner || "—",
      }),
      el("span", { class: "tasks-arc-goal", text: t.goal || t.title || "(no goal)" }),
      el("span", { class: "tasks-arc-id mono", text: shortId(tid) }),
    ]);
    list.appendChild(row);
  }

  if (foot) {
    foot.innerHTML = "";
    if (filtered.length > limit) {
      foot.appendChild(el("button", {
        type: "button", class: "tasks-archive-more",
        onclick: () => { state.archivePage += 1; persist(); renderArchive(); },
        text: `show ${Math.min(ARCHIVE_PAGE_SIZE, filtered.length - limit)} more · ${filtered.length - limit} hidden`,
      }));
    } else if (filtered.length > ARCHIVE_PAGE_SIZE) {
      foot.appendChild(el("div", { class: "tasks-archive-foot-info mono",
        text: `showing all ${filtered.length}` }));
    }
  }
}

// ---------------------------------------------------------------------------
// Side panel
// ---------------------------------------------------------------------------

async function selectTask(id) {
  state.selectedTaskId = id;
  persist();
  applySelectionClass();
  await openPanel(id);
}

function applySelectionClass() {
  document.querySelectorAll("[data-task-id].tasks-selected").forEach((n) => {
    if (n.getAttribute("data-task-id") !== state.selectedTaskId) {
      n.classList.remove("tasks-selected");
    }
  });
  if (!state.selectedTaskId) return;
  document
    .querySelectorAll(`[data-task-id="${cssEsc(state.selectedTaskId)}"]`)
    .forEach((n) => n.classList.add("tasks-selected"));
}

async function openPanel(id) {
  const panel = $("tasks-panel");
  const scrim = $("tasks-panel-scrim");
  const body = $("tasks-panel-body");
  const title = $("tasks-panel-title");
  if (!panel || !body) return;
  panel.classList.add("open");
  panel.setAttribute("aria-hidden", "false");
  if (scrim) {
    scrim.classList.add("open");
    scrim.setAttribute("aria-hidden", "false");
  }
  // Accessible dialog: focus in + Tab-trap + Esc + restore focus on close.
  if (window.Exo && Exo.openDialog) {
    state._dlgClose = Exo.openDialog(panel, {
      labelledBy: "tasks-panel-title",
      onClose: hidePanel,
    });
  }

  const t = state.tasks.get(id);
  if (!t) {
    body.innerHTML = "";
    body.appendChild(el("div", { class: "tasks-panel-empty",
      text: "task not found in current window" }));
    if (title) title.textContent = "mission " + shortId(id);
    return;
  }
  if (title) title.textContent = "mission " + shortId(id);

  const owner = ownerOf(t);
  const ownerColor = agentColor(owner);
  const bucket = bucketFor(t.status);

  body.innerHTML = "";

  // -- Hero strip ---------------------------------------------------------
  body.appendChild(el("div", {
    class: "tasks-panel-hero",
    style: { borderColor: statusColor(t.status) },
  }, [
    el("div", { class: "tasks-panel-hero-row" }, [
      el("span", { class: "tasks-panel-hero-glyph mono",
        style: { color: statusColor(t.status) }, text: statusGlyph(t.status) }),
      el("span", { class: "tasks-panel-hero-status mono",
        style: { color: statusColor(t.status) }, text: t.status || "—" }),
      el("span", { class: "tasks-panel-hero-agent mono",
        style: { color: ownerColor }, text: owner || "unassigned" }),
    ]),
    el("div", { class: "tasks-panel-hero-time mono",
      text: bucket === "completed"
        ? "completed " + fmtRelative(t.last_event_at) + " · ran " + fmtDuration(t.created_at, t.last_event_at)
        : bucket === "failed"
          ? "failed " + fmtRelative(t.last_event_at) + " · ran " + fmtDuration(t.created_at, t.last_event_at)
          : "running for " + fmtElapsed(t.created_at),
    }),
  ]));

  // -- Mission meta -------------------------------------------------------
  body.appendChild(panelSection("mission", el("div", { class: "tasks-meta" }, [
    el("div", { class: "k", text: "task_id" }),
    el("div", { class: "v mono", text: id }),
    el("div", { class: "k", text: "agent" }),
    el("div", { class: "v mono", style: { color: ownerColor }, text: owner || "unassigned" }),
    el("div", { class: "k", text: "status" }),
    el("div", { class: "v mono", style: { color: statusColor(t.status) }, text: t.status || "—" }),
    el("div", { class: "k", text: "scope" }),
    el("div", { class: "v mono", text: (t.scope || "—") + ":" + shortId(t.scope_id) }),
    el("div", { class: "k", text: "created" }),
    el("div", { class: "v mono", text: fmtRelative(t.created_at) }),
    el("div", { class: "k", text: "last_active" }),
    el("div", { class: "v mono", text: fmtRelative(t.last_event_at) }),
    el("div", { class: "k", text: "events" }),
    el("div", { class: "v mono", text: String(t.event_count || 0) }),
    Array.isArray(t.agents) && t.agents.length ? el("div", { class: "k", text: "involved" }) : null,
    Array.isArray(t.agents) && t.agents.length ? el("div", { class: "v mono", text: t.agents.join(", ") }) : null,
  ])));

  // -- Goal ---------------------------------------------------------------
  body.appendChild(panelSection("goal", el("div", { class: "tasks-goal-block",
    text: t.goal || "(no goal recorded)" })));

  // -- Last decision ------------------------------------------------------
  if (t.last_decision) {
    body.appendChild(panelSection("last decision",
      el("div", { class: "tasks-decision-block", text: t.last_decision })));
  }

  // -- Lineage ------------------------------------------------------------
  if (t.parent_task_id || lineageChain(t).length > 1) {
    const wrap = el("div", { class: "tasks-panel-lineage" });
    buildLineage(wrap, t, true);
    body.appendChild(panelSection("lineage", wrap));
  }

  // -- Recent events (loading) -------------------------------------------
  const eventsBox = el("div", { class: "tasks-events-list" }, [
    el("div", { class: "tasks-panel-empty", text: "loading recent events…" }),
  ]);
  body.appendChild(panelSection("recent events", eventsBox));

  // -- Actions ------------------------------------------------------------
  const actions = el("div", { class: "tasks-panel-actions" }, [
    el("a", {
      class: "tasks-link",
      href: "/agents?task=" + encodeURIComponent(id),
      target: "_blank", rel: "noopener noreferrer",
      text: "open in agents view ↗",
    }),
    bucket === "failed" ? el("a", {
      class: "tasks-link",
      href: "/static/debug.html",
      target: "_blank", rel: "noopener noreferrer",
      text: "open in debug ↗",
    }) : null,
  ]);
  body.appendChild(panelSection("actions", actions));

  // -- Async fill: events -------------------------------------------------
  const cached = state.traceCache.get(id);
  let events = cached && Array.isArray(cached.events) ? cached.events : null;
  if (!events) {
    const trace = await fetchTaskTrace(id);
    if (state.selectedTaskId !== id) return;
    events = trace && Array.isArray(trace.events) ? trace.events : [];
    state.traceCache.set(id, { events, fetchedAt: Date.now() });
  }
  eventsBox.innerHTML = "";
  if (!events.length) {
    eventsBox.appendChild(el("div", { class: "tasks-panel-empty",
      text: "no events recorded" }));
  } else {
    const slice = events.slice(-10).reverse();
    for (const ev of slice) {
      const ag = ev.agent_id || null;
      eventsBox.appendChild(el("div", { class: "tasks-event-row" }, [
        el("span", { class: "tasks-event-ts mono", text: fmtClock(ev.timestamp) }),
        el("span", { class: "tasks-event-kind mono", text: ev.kind || "event" }),
        ag ? el("span", { class: "tasks-event-agent mono",
          style: { color: agentColor(ag) }, text: ag }) : null,
        el("span", { class: "tasks-event-preview mono",
          text: previewPayload(ev.payload) }),
      ]));
    }
  }
}

function panelSection(label, ...children) {
  const sec = el("div", { class: "tasks-panel-section" }, [
    el("h4", { text: label }),
  ]);
  for (const c of children) if (c) sec.appendChild(c);
  return sec;
}

function hidePanel() {
  const panel = $("tasks-panel");
  const scrim = $("tasks-panel-scrim");
  if (!panel) return;
  panel.classList.remove("open");
  panel.setAttribute("aria-hidden", "true");
  if (scrim) {
    scrim.classList.remove("open");
    scrim.setAttribute("aria-hidden", "true");
  }
  state.selectedTaskId = null;
  persist();
  applySelectionClass();
}

function closePanel() {
  // Route through the accessible-dialog closer (restores focus); fall back to
  // a plain hide if the dialog helper wasn't active.
  if (state._dlgClose) {
    const c = state._dlgClose;
    state._dlgClose = null;
    c();
    return;
  }
  hidePanel();
}

// ---------------------------------------------------------------------------
// Live tick — updates elapsed counters + relative timestamps once a second.
// Cheap because we only touch text nodes that are visible.
// ---------------------------------------------------------------------------

function tick() {
  // Update hero card elapsed counters.
  const cards = document.querySelectorAll(".tasks-now-card");
  for (const c of cards) {
    const tid = c.getAttribute("data-task-id");
    const t = state.tasks.get(tid);
    if (!t) continue;
    const eEl = c.querySelector(".tasks-now-elapsed");
    if (eEl) eEl.textContent = fmtElapsed(t.created_at);
  }
}

// ---------------------------------------------------------------------------
// WS + polling
// ---------------------------------------------------------------------------

const refetchDebounced = debounce(fetchTasks, REFETCH_DEBOUNCE_MS);

const RELEVANT_KINDS = new Set([
  "task.created",
  "task.status_changed",
  "task.completed",
  "task.failed",
  "handoff.initiated",
  "dispatch.failed",
  "dispatch.fallback",
]);

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
    state.ws = ws;
    ws.addEventListener("open", () => { backoff = 1000; });
    ws.addEventListener("close", () => setTimeout(open, backoff = Math.min(backoff * 2, 8000)));
    ws.addEventListener("error", () => { /* close will follow */ });
    ws.addEventListener("message", (msg) => {
      let event;
      try { event = JSON.parse(msg.data); } catch (_) { return; }
      if (!event || event.kind === "__hello__") return;
      if (RELEVANT_KINDS.has(event.kind)) refetchDebounced();
    });
  }
  open();
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

function bindUi() {
  // Archive chips.
  document.querySelectorAll("#tasks-archive-chips .tasks-chip").forEach((c) => {
    c.addEventListener("click", () => {
      state.archiveFilter = c.dataset.filter || "all";
      state.archivePage = 1;
      persist();
      renderArchive();
    });
  });

  // Archive search.
  const search = $("tasks-archive-search");
  if (search) {
    const onSearch = debounce(() => {
      state.archiveSearch = search.value || "";
      state.archivePage = 1;
      persist();
      renderArchive();
    }, 120);
    search.addEventListener("input", onSearch);
  }

  // Side panel close.
  const close = $("tasks-panel-close");
  if (close) close.addEventListener("click", closePanel);
  const scrim = $("tasks-panel-scrim");
  if (scrim) scrim.addEventListener("click", closePanel);

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.selectedTaskId) closePanel();
  });
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

async function boot() {
  loadPersisted();
  bindUi();

  await fetchTasks();

  // Reopen the previously-viewed panel if the task is still around.
  if (state.selectedTaskId && state.tasks.has(state.selectedTaskId)) {
    openPanel(state.selectedTaskId);
  }

  // Live tick for hero cards' elapsed counter + relative time.
  state.tickTimer = setInterval(tick, TICK_INTERVAL_MS);
  // 30s safety-net poll.
  setInterval(fetchTasks, REFETCH_INTERVAL_MS);
  // Active-task trace refresh.
  state.activeTraceTimer = setInterval(refreshActiveTraces, ACTIVE_TRACE_INTERVAL_MS);

  connectWs();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
