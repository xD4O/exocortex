// memory_extras.js
//
// Adds three features to /memory beyond the existing constellation + memchat:
//   1. Scope selector pinned above the chat input (POST body for /api/memory/chat
//      gets `scope` and `scope_id`; also dispatches `constellation:filter-scope`).
//   2. Tasks sidebar on the right (collapsible, tabs, click → set scope).
//   3. Activity strip under the constellation (last ~30 events, live via WS).
//
// All endpoints can 404/503; we degrade gracefully — the existing memchat.js
// pattern is the model. We never throw on missing data.
//
// We respect ownership boundaries: this file does not modify memchat.js or
// constellation.js. We reach into the shared chat POST by wrapping window.fetch
// (URL-scoped) so the scope flows through without needing to touch memchat.

(function () {
  "use strict";

  // ---------------------------------------------------------------------
  // 0. Tiny utilities
  // ---------------------------------------------------------------------

  const SCOPE_LS_KEY = "exocortex.memchat.scope.v1";
  const TASKS_TAB_LS_KEY = "exocortex.memory.tasks.tab.v1";
  const TASKS_COLLAPSED_LS_KEY = "exocortex.memory.tasks.collapsed.v1";

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
  function kindGlyph(kind) { return KIND_GLYPH[kind] || "·"; }

  function agentColor(agentId) {
    if (!agentId) return FALLBACK_AGENT_COLOR;
    return AGENT_COLORS[agentId] || FALLBACK_AGENT_COLOR;
  }

  function fmtTimeFromMs(ms) {
    try {
      const d = new Date(ms);
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      const ss = String(d.getSeconds()).padStart(2, "0");
      return `${hh}:${mm}:${ss}`;
    } catch (_) { return "--:--:--"; }
  }

  function truncate(s, n) {
    if (s == null) return "";
    s = String(s);
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

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

  // ---------------------------------------------------------------------
  // 1. Scope selector
  // ---------------------------------------------------------------------

  const scopeState = {
    scope: "all",
    scopeId: "",
  };

  function loadScope() {
    try {
      const raw = localStorage.getItem(SCOPE_LS_KEY);
      if (!raw) return;
      const obj = JSON.parse(raw);
      if (obj && typeof obj === "object") {
        if (typeof obj.scope === "string") scopeState.scope = obj.scope;
        if (typeof obj.scopeId === "string") scopeState.scopeId = obj.scopeId;
      }
    } catch (_) { /* ignore */ }
  }

  function saveScope() {
    try {
      localStorage.setItem(SCOPE_LS_KEY, JSON.stringify(scopeState));
    } catch (_) { /* ignore */ }
  }

  function broadcastScope() {
    const detail = scopeState.scope === "all"
      ? { scope: null, scope_id: null }
      : { scope: scopeState.scope, scope_id: scopeState.scopeId || null };
    window.dispatchEvent(new CustomEvent("constellation:filter-scope", { detail }));
  }

  function renderScopeUi() {
    const sel = document.getElementById("scope-select");
    const inp = document.getElementById("scope-id-input");
    const wrap = document.getElementById("scope-selector");
    if (!sel || !inp || !wrap) return;
    sel.value = scopeState.scope;
    inp.value = scopeState.scopeId;
    inp.style.display = scopeState.scope === "all" ? "none" : "";
    // On /memory, only show the scope selector when the slide-up chat panel
    // is open. On /chat there is no slide-up — the composer always shows the
    // selector. The presence (or absence) of #mem-chat decides which mode.
    const chat = document.getElementById("mem-chat");
    if (!chat) {
      wrap.style.display = "";
      return;
    }
    const visible = !!chat.classList.contains("open");
    wrap.style.display = visible ? "" : "none";
  }

  function bindScope() {
    const sel = document.getElementById("scope-select");
    const inp = document.getElementById("scope-id-input");
    if (sel) {
      sel.addEventListener("change", () => {
        scopeState.scope = sel.value;
        if (scopeState.scope === "all") scopeState.scopeId = "";
        saveScope();
        renderScopeUi();
        broadcastScope();
      });
    }
    if (inp) {
      let t = null;
      inp.addEventListener("input", () => {
        scopeState.scopeId = inp.value.trim();
        if (t) clearTimeout(t);
        t = setTimeout(() => {
          saveScope();
          broadcastScope();
        }, 250);
      });
    }
    // Watch for chat panel open/close to show/hide scope selector.
    const chat = document.getElementById("mem-chat");
    if (chat && "MutationObserver" in window) {
      new MutationObserver(renderScopeUi).observe(chat, { attributes: true, attributeFilter: ["class"] });
    }
  }

  // Wrap window.fetch so /api/memory/chat POSTs include the current scope.
  // memchat.js owns that endpoint; we don't touch its file.
  (function patchFetch() {
    const origFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
      try {
        const url = typeof input === "string"
          ? input
          : (input && input.url) || "";
        const method = (init && init.method)
          || (typeof input !== "string" && input && input.method)
          || "GET";
        if (
          url.indexOf("/api/memory/chat") !== -1 &&
          String(method).toUpperCase() === "POST" &&
          init && init.body
        ) {
          let body = init.body;
          if (typeof body === "string") {
            try {
              const parsed = JSON.parse(body);
              if (scopeState.scope !== "all") {
                parsed.scope = scopeState.scope;
                if (scopeState.scopeId) parsed.scope_id = scopeState.scopeId;
              }
              init = Object.assign({}, init, { body: JSON.stringify(parsed) });
            } catch (_) { /* leave body alone */ }
          }
        }
      } catch (_) { /* never let our wrapper break fetch */ }
      return origFetch(input, init);
    };
  })();

  // Public: programmatically set scope (used by tasks sidebar).
  function setScope(scope, scopeId) {
    scopeState.scope = scope || "all";
    scopeState.scopeId = (scopeId || "").trim();
    saveScope();
    renderScopeUi();
    broadcastScope();
  }
  window.__exoSetScope = setScope;

  // ---------------------------------------------------------------------
  // 2. Tasks sidebar
  // ---------------------------------------------------------------------

  const tasksState = {
    tab: "open",
    items: [],
    collapsed: false,
    activeTaskId: null,
    pollTimer: null,
    available: true,
  };

  function loadTasksPrefs() {
    try {
      const t = localStorage.getItem(TASKS_TAB_LS_KEY);
      if (t === "open" || t === "completed" || t === "all") tasksState.tab = t;
      const c = localStorage.getItem(TASKS_COLLAPSED_LS_KEY);
      if (c === "1") tasksState.collapsed = true;
    } catch (_) { /* ignore */ }
  }

  function saveTasksPrefs() {
    try {
      localStorage.setItem(TASKS_TAB_LS_KEY, tasksState.tab);
      localStorage.setItem(TASKS_COLLAPSED_LS_KEY, tasksState.collapsed ? "1" : "0");
    } catch (_) { /* ignore */ }
  }

  async function fetchTasks() {
    try {
      const r = await fetch(`/api/tasks?status=${tasksState.tab}&limit=50`);
      if (!r.ok) {
        if (r.status === 404 || r.status === 503) {
          tasksState.available = false;
          tasksState.items = [];
          renderTasks();
          return;
        }
        return;
      }
      const data = await r.json();
      tasksState.available = true;
      // Accept either {items:[...]} (new) or {tasks:[...]} (existing dashboard).
      const items = Array.isArray(data.items)
        ? data.items
        : (Array.isArray(data.tasks) ? data.tasks : []);
      tasksState.items = items.map(normalizeTask);
      renderTasks();
    } catch (_) {
      tasksState.available = false;
      renderTasks();
    }
  }

  function normalizeTask(t) {
    return {
      task_id: t.task_id || t.id || "",
      title: t.title || t.goal || "(no title)",
      status: t.status || "open",
      // status_bucket from existing backend = open|completed|failed
      status_bucket: t.status_bucket || (
        ["completed", "succeeded", "done"].indexOf((t.status || "").toLowerCase()) >= 0
          ? "completed"
          : (["failed", "denied", "rejected"].indexOf((t.status || "").toLowerCase()) >= 0
              ? "failed" : "open")
      ),
      last_decision: t.last_decision || "",
      last_event_at: t.last_event_at || "",
      scope_id: t.scope_id || t.task_id || t.id || "",
      scope: t.scope || "task",
    };
  }

  function applyCollapsed() {
    // Either the constellation layout (/memory) or the chat layout (/chat)
    // hosts the tasks sidebar. We toggle the .tasks-collapsed class on
    // whichever wrapper is present.
    const layout =
      document.querySelector(".constellation-layout") ||
      document.querySelector(".chat-layout");
    if (!layout) return;
    if (tasksState.collapsed) layout.classList.add("tasks-collapsed");
    else layout.classList.remove("tasks-collapsed");
    const btn = document.getElementById("ts-collapse");
    if (btn) btn.textContent = tasksState.collapsed ? "‹" : "›";
  }

  function renderTasks() {
    const host = document.getElementById("ts-list");
    const countEl = document.getElementById("ts-count");
    if (!host || !countEl) return;
    countEl.textContent = String(tasksState.items.length);
    host.innerHTML = "";

    if (!tasksState.available) {
      host.appendChild(el("div", {
        class: "ts-empty",
      }, ["tasks endpoint not ready — will retry"]));
      return;
    }
    if (tasksState.items.length === 0) {
      const empty = el("div", { class: "ts-empty" }, [
        "no tasks yet — dispatch one with ",
        el("span", { class: "cmd", text: "precog dispatch ..." }),
      ]);
      host.appendChild(empty);
      return;
    }

    for (const t of tasksState.items) {
      const isCompleted = t.status_bucket === "completed";
      const glyph = isCompleted ? "○" : "●";
      const cls = isCompleted ? "completed" : "open";
      const row = el("div", {
        class: "ts-row" + (tasksState.activeTaskId === t.task_id ? " active" : ""),
        title: t.last_decision || t.title,
        onclick: () => onTaskRowClick(t),
      }, [
        el("div", { class: "top" }, [
          el("span", { class: "glyph " + cls, text: glyph }),
          el("span", { class: "title", text: t.title || "(no title)" }),
        ]),
        t.last_decision
          ? el("div", { class: "decision", text: "↳ " + truncate(t.last_decision, 80) })
          : null,
      ]);
      host.appendChild(row);
    }
  }

  function onTaskRowClick(t) {
    tasksState.activeTaskId = t.task_id;
    renderTasks();
    // Set chat scope to this task
    setScope("task", t.scope_id || t.task_id);
    // Also dispatch the constellation filter (setScope already does this,
    // but explicit task scope is the canonical event payload).
    window.dispatchEvent(new CustomEvent("constellation:filter-scope", {
      detail: { scope: "task", scope_id: t.scope_id || t.task_id },
    }));
    // Notify any listener (e.g. chat.js) that wants to react — we use a
    // CustomEvent rather than a direct callback so multiple consumers can
    // subscribe without coupling.
    window.dispatchEvent(new CustomEvent("exocortex:task-scoped", {
      detail: {
        task_id: t.task_id,
        title: t.title,
        scope: "task",
        scope_id: t.scope_id || t.task_id,
      },
    }));
  }

  function bindTasks() {
    const tabs = document.querySelectorAll("#tasks-sidebar .ts-tabs button");
    tabs.forEach((b) => {
      b.classList.toggle("active", b.dataset.tab === tasksState.tab);
      b.addEventListener("click", () => {
        tasksState.tab = b.dataset.tab;
        tabs.forEach((bb) => bb.classList.toggle("active", bb.dataset.tab === tasksState.tab));
        saveTasksPrefs();
        fetchTasks();
      });
    });
    const col = document.getElementById("ts-collapse");
    const stub = document.getElementById("ts-collapse-stub");
    if (col) col.addEventListener("click", () => {
      tasksState.collapsed = true;
      saveTasksPrefs();
      applyCollapsed();
    });
    if (stub) stub.addEventListener("click", () => {
      tasksState.collapsed = false;
      saveTasksPrefs();
      applyCollapsed();
    });
  }

  // ---------------------------------------------------------------------
  // 3. Activity strip
  // ---------------------------------------------------------------------

  const ACTIVITY_MAX = 30;
  const activityState = {
    items: [],
    available: true,
    lastSeenMs: 0,
  };

  async function fetchActivity() {
    try {
      const r = await fetch("/api/activity?limit=" + ACTIVITY_MAX);
      if (!r.ok) {
        if (r.status === 404 || r.status === 503) {
          activityState.available = false;
        }
        renderActivity();
        return;
      }
      activityState.available = true;
      const data = await r.json();
      const items = Array.isArray(data.items) ? data.items : [];
      activityState.items = items.slice(0, ACTIVITY_MAX);
      if (items.length) {
        activityState.lastSeenMs = Math.max.apply(
          null,
          items.map((x) => x.timestamp_ms || 0).concat([0])
        );
      }
      renderActivity();
    } catch (_) {
      activityState.available = false;
      renderActivity();
    }
  }

  function renderActivity() {
    const host = document.getElementById("activity-strip");
    if (!host) return;
    host.innerHTML = "";
    if (!activityState.available) {
      host.appendChild(el("div", {
        class: "as-empty",
        text: "activity feed not ready — will retry",
      }));
      return;
    }
    if (activityState.items.length === 0) {
      host.appendChild(el("div", {
        class: "as-empty",
        text: "no activity yet — agents will fill this strip as they call MCP tools",
      }));
      return;
    }
    // Newest left → oldest right (so new events slide in from the right we
    // need oldest first; but the spec says "slide in from the right". To get
    // a card animating in from the right while existing cards stay put, we
    // append newest at the right edge — i.e. oldest on left.
    const sorted = activityState.items
      .slice()
      .sort((a, b) => (a.timestamp_ms || 0) - (b.timestamp_ms || 0));
    for (const ev of sorted) {
      host.appendChild(buildActivityCard(ev));
    }
    host.scrollLeft = host.scrollWidth;
  }

  function buildActivityCard(ev) {
    const color = agentColor(ev.agent_id);
    const card = el("div", {
      class: "as-card",
      style: { borderLeftColor: color },
      title: (ev.agent_id || "?") + " · " + (ev.kind || "") + "\n" + (ev.payload_preview || ""),
      onclick: () => {
        const p = new URLSearchParams();
        if (ev.agent_id) p.set("agent", ev.agent_id);
        if (ev.event_id) p.set("event", ev.event_id);
        window.open("/static/agents.html?" + p.toString(), "_blank");
      },
    }, [
      el("div", { class: "ts", text: fmtTimeFromMs(ev.timestamp_ms || Date.now()) }),
      el("div", { class: "mid" }, [
        el("span", { class: "kind", text: kindGlyph(ev.kind) }),
        el("span", { class: "agent", style: { color }, text: ev.agent_id || "—" }),
      ]),
      el("div", { class: "preview", text: truncate(ev.payload_preview || ev.kind || "", 28) }),
    ]);
    return card;
  }

  function pushActivityFromWs(event) {
    if (!event || event.kind === "__hello__") return;
    if (!activityState.available) return;
    const item = {
      event_id: event.event_id || (event.timestamp + ":" + event.kind),
      kind: event.kind,
      agent_id: event.agent_id,
      timestamp_ms: event.timestamp_ms || (event.timestamp ? Date.parse(event.timestamp) : Date.now()),
      payload_preview: shortPreview(event),
      task_id: event.task_id,
      session_id: event.session_id,
      scope: event.scope,
      scope_id: event.scope_id,
    };
    activityState.items.push(item);
    while (activityState.items.length > ACTIVITY_MAX) activityState.items.shift();
    activityState.lastSeenMs = Math.max(activityState.lastSeenMs, item.timestamp_ms);
    renderActivity();
  }

  function shortPreview(event) {
    const p = event.payload || {};
    if (typeof event.payload_preview === "string") return event.payload_preview;
    const keys = Object.keys(p).slice(0, 2);
    return keys.map((k) => `${k}=${truncate(JSON.stringify(p[k]), 24)}`).join(" ");
  }

  // Hook into the existing WebSocket on /api/events.
  function connectActivityWs() {
    let ws;
    let backoff = 1000;
    function open() {
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

        // Activity strip
        pushActivityFromWs(event);

        // Tasks sidebar refresh on memory.written
        if (event.kind === "memory.written") {
          scheduleTasksRefresh();
        }
        if (event.kind === "task.created" || event.kind === "task.completed" ||
            event.kind === "task.status_changed" || event.kind === "task.failed") {
          scheduleTasksRefresh();
        }
      });
    }
    open();
  }

  let tasksRefreshTimer = null;
  function scheduleTasksRefresh() {
    if (tasksRefreshTimer) return;
    tasksRefreshTimer = setTimeout(() => {
      tasksRefreshTimer = null;
      fetchTasks();
    }, 400);
  }

  // ---------------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------------

  function init() {
    // Scope
    loadScope();
    bindScope();
    renderScopeUi();
    broadcastScope();

    // Tasks
    loadTasksPrefs();
    bindTasks();
    applyCollapsed();
    renderTasks();
    fetchTasks();
    if (tasksState.pollTimer == null) {
      tasksState.pollTimer = setInterval(fetchTasks, 10_000);
    }

    // Activity
    fetchActivity();
    setInterval(() => {
      if (!activityState.available) fetchActivity();
    }, 15_000);
    connectActivityWs();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
