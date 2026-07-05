/* Exocortex operator UI v3 — single-page app.
 *
 * The refresh mockup is the design source of truth; this file adapts the
 * tool to it: every view is the mockup's markup fed by the real API.
 * No bundler — one file, plain JS, talks to /api/* and /api/events (WS).
 */
(function () {
  "use strict";

  // ═════════════════════ utilities ═════════════════════
  const $ = (id) => document.getElementById(id);

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const k of Object.keys(attrs)) {
        const v = attrs[k];
        if (v == null) continue;
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k === "style" && typeof v === "object") {
          for (const sk of Object.keys(v)) node.style[sk] = v[sk];
        } else if (k.slice(0, 2) === "on" && typeof v === "function") {
          node.addEventListener(k.slice(2).toLowerCase(), v);
        } else node.setAttribute(k, v);
      }
    }
    for (const c of [].concat(children || [])) {
      if (c == null) continue;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return node;
  }

  function empty(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function note(host, text) { empty(host); host.appendChild(el("div", { class: "empty-note", text })); }
  function truncate(s, n) {
    s = String(s == null ? "" : s);
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }
  function fmtRel(ms) {
    if (!ms) return "—";
    const d = Date.now() - ms;
    if (d < 0) return "just now";
    if (d < 60e3) return Math.max(1, Math.floor(d / 1e3)) + "s ago";
    if (d < 3600e3) return Math.floor(d / 60e3) + "m ago";
    if (d < 86400e3) return Math.floor(d / 3600e3) + "h ago";
    if (d < 2592e6) return Math.floor(d / 86400e3) + "d ago";
    return Math.floor(d / 2592e6) + "mo ago";
  }
  function fmtTime(ms) {
    const d = new Date(ms);
    const p = (n) => String(n).padStart(2, "0");
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }
  function fmtDur(ms) {
    if (ms == null || !isFinite(ms) || ms < 0) return "—";
    const s = Math.round(ms / 1e3);
    if (s < 90) return s + "s";
    const m = Math.floor(s / 60);
    if (m < 90) return m + "m" + (s % 60 ? " " + (s % 60) + "s" : "");
    return Math.floor(m / 60) + "h " + (m % 60) + "m";
  }
  async function api(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(path + " -> " + r.status);
    return r.json();
  }
  async function apiPost(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || path + " -> " + r.status);
    return data;
  }
  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  // agent identity — theme tokens, deterministic fallback for unknown ids
  const AGENT_TOKEN = {
    codex: "--ag-codex", hermes: "--ag-hermes",
    claude: "--ag-claude", claude_code: "--ag-claude", "claude-code": "--ag-claude",
    openclaw: "--ag-openclaw", operator: "--ag-operator", exocortex: "--ag-operator",
  };
  const EXTRA = ["--cl-personal", "--cl-reachy", "--cl-research", "--cl-trading", "--cl-exocortex"];
  function agentColor(id) {
    if (!id) return cssVar("--faint", "#8b949e");
    const tok = AGENT_TOKEN[id];
    if (tok) return cssVar(tok, "#8b949e");
    let h = 0;
    for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) % 9973;
    return cssVar(EXTRA[h % EXTRA.length], "#8b949e");
  }
  function agentAbbr(id) {
    if (!id) return "?";
    const parts = String(id).split(/[_-]/).filter(Boolean);
    return (parts.length >= 2 ? parts[0][0] + parts[1][0] : String(id).slice(0, 2)).toLowerCase();
  }
  function agChip(id) {
    return el("span", { class: "ag-chip" }, [
      el("span", { class: "dot", style: { background: agentColor(id) } }),
      el("span", { class: "mono", text: id || "?" }),
    ]);
  }

  const TYPE_GLYPH = {
    decision: "◆", observation: "●", note: "▪", question: "?", feedback: "✎",
  };
  function typeGlyph(t) {
    if (TYPE_GLYPH[t]) return TYPE_GLYPH[t];
    if (String(t).startsWith("profile")) return "◐";
    if (String(t).endsWith("_response")) return "⇄";
    return "•";
  }

  function sparkline(svgId, series) {
    const svg = $(svgId);
    if (!svg) return;
    empty(svg);
    if (!series || !series.length) return;
    const max = Math.max(...series, 1);
    const n = series.length;
    const pts = series.map((v, i) => [
      2 + (i / Math.max(1, n - 1)) * 68,
      22 - (v / max) * 16,
    ]);
    const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
    const area = line + " L70 24 L2 24 Z";
    const NS = "http://www.w3.org/2000/svg";
    const a = document.createElementNS(NS, "path");
    a.setAttribute("d", area); a.setAttribute("class", "area");
    const l = document.createElementNS(NS, "path");
    l.setAttribute("d", line); l.setAttribute("class", "line");
    const dot = document.createElementNS(NS, "circle");
    const last = pts[pts.length - 1];
    dot.setAttribute("cx", last[0]); dot.setAttribute("cy", last[1]);
    dot.setAttribute("r", "2.6"); dot.setAttribute("class", "dot");
    svg.appendChild(a); svg.appendChild(l); svg.appendChild(dot);
  }

  function hourlyBuckets(timestampsMs, hours) {
    const out = new Array(hours).fill(0);
    const now = Date.now();
    for (const t of timestampsMs) {
      const age = now - t;
      if (age < 0 || age >= hours * 3600e3) continue;
      out[hours - 1 - Math.floor(age / 3600e3)] += 1;
    }
    return out;
  }

  // ═════════════════════ theme + chrome ═════════════════════
  const THEMES = ["auto", "dark", "light", "phosphor", "sepia", "synthwave"];
  function themeGet() {
    try { const t = localStorage.getItem("exo-theme"); return THEMES.includes(t) ? t : "auto"; }
    catch (_) { return "auto"; }
  }
  function themeSet(t) {
    if (!THEMES.includes(t)) t = "auto";
    try { localStorage.setItem("exo-theme", t); } catch (_) { /* private mode */ }
    if (t === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = t;
    document.querySelectorAll("[data-theme-pick]").forEach((c) =>
      c.classList.toggle("on", c.dataset.themePick === t));
    // `cons` is declared later in the file; at first-paint themeSet runs
    // before its const initializes — guard the TDZ instead of reordering.
    try { if (cons.started) cons.needsColor = true; } catch (_) { /* boot */ }
  }
  document.querySelectorAll("[data-theme-pick]").forEach((c) =>
    c.addEventListener("click", () => themeSet(c.dataset.themePick)));
  themeSet(themeGet());

  function densityGet() {
    try { return localStorage.getItem("exo-density") || "comfortable"; } catch (_) { return "comfortable"; }
  }
  function densitySet(d) {
    try { localStorage.setItem("exo-density", d); } catch (_) { /* ok */ }
    document.body.dataset.density = d;
    $("density-toggle").textContent = d;
    $("set-density").textContent = d;
  }
  function densityToggle() { densitySet(densityGet() === "compact" ? "comfortable" : "compact"); }
  $("density-toggle").addEventListener("click", densityToggle);
  $("set-density").addEventListener("click", densityToggle);
  densitySet(densityGet());

  // memory chat toggle (topbar + settings share state)
  async function memchatRefresh() {
    try {
      const st = await api("/api/settings/memory_chat");
      const on = !!st.enabled;
      for (const id of ["memchat-toggle", "set-memchat"]) {
        const b = $(id);
        b.textContent = id === "memchat-toggle" ? "memory chat · " + (on ? "on" : "off") : (on ? "on" : "off");
        b.classList.toggle("on", on);
      }
    } catch (_) { /* endpoint absent — leave */ }
  }
  async function memchatToggle() {
    try { await apiPost("/api/settings/memory_chat/toggle"); } catch (_) { /* surfaced below */ }
    memchatRefresh();
  }
  $("memchat-toggle").addEventListener("click", memchatToggle);
  $("set-memchat").addEventListener("click", memchatToggle);

  async function freezeRefresh() {
    try {
      const st = await api("/api/settings/profile_freeze");
      const frozen = !!st.frozen;
      const label = frozen ? "frozen" : "learning · on";
      const ft = $("freeze-toggle"); ft.textContent = label; ft.classList.toggle("on", !frozen);
      const sf = $("set-freeze"); sf.textContent = frozen ? "frozen" : "learning"; sf.classList.toggle("on", !frozen);
    } catch (_) { /* ok */ }
  }
  async function freezeToggle() {
    try { await apiPost("/api/settings/profile_freeze/toggle"); } catch (_) { /* ok */ }
    freezeRefresh();
  }
  $("freeze-toggle").addEventListener("click", freezeToggle);
  $("set-freeze").addEventListener("click", freezeToggle);

  // ═════════════════════ router ═════════════════════
  const VIEW_PATH = {
    dashboard: "/", constellation: "/memory", tasks: "/tasks",
    agents: "/agents", conversations: "/conversations", chat: "/chat",
    profile: "/profile", reflect: "/reflect", settings: "/settings", debug: "/debug",
  };
  const PATH_VIEW = Object.fromEntries(Object.entries(VIEW_PATH).map(([v, p]) => [p, v]));
  const PAGE_META = {
    dashboard: ["operator / observe", "Dashboard"],
    constellation: ["operator / observe", "Constellation"],
    tasks: ["operator / observe", "Tasks"],
    agents: ["operator / agents", "Agents"],
    conversations: ["operator / agents", "Conversations"],
    chat: ["operator / agents", "Memory chat"],
    profile: ["operator / mind", "Profile"],
    reflect: ["operator / mind", "Reflect"],
    settings: ["operator / system", "Settings"],
    debug: ["operator / system", "Debug"],
  };
  const LOADERS = {}; // view -> async fn, registered below
  let currentView = null;

  function showPage(view, push) {
    if (!PAGE_META[view]) view = "dashboard";
    currentView = view;
    document.querySelectorAll(".rail .nav-item").forEach((n) =>
      n.classList.toggle("active", n.dataset.view === view));
    document.querySelectorAll(".page").forEach((p) => { p.hidden = p.id !== "page-" + view; });
    $("tb-crumb").textContent = PAGE_META[view][0];
    $("tb-title").textContent = PAGE_META[view][1];
    if (push !== false) history.pushState({ view }, "", VIEW_PATH[view]);
    const load = LOADERS[view];
    if (load) load().catch((e) => console.warn("load " + view, e));
  }
  document.querySelectorAll("[data-view]").forEach((a) => {
    a.addEventListener("click", (e) => { e.preventDefault(); showPage(a.dataset.view); });
    a.addEventListener("keydown", (e) => {
      if (a.tagName !== "A" && (e.key === "Enter" || e.key === " ")) {
        e.preventDefault(); showPage(a.dataset.view);
      }
    });
  });
  window.addEventListener("popstate", () => showPage(PATH_VIEW[location.pathname] || "dashboard", false));

  // global search: ⌘K or click → constellation search
  function gotoSearch() {
    showPage("constellation");
    setTimeout(() => $("cons-search").focus(), 60);
  }
  $("global-search").addEventListener("click", gotoSearch);
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
      const t = e.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      e.preventDefault(); gotoSearch();
    }
    if (e.key === "Escape") closeToaster();
  });

  // ═════════════════════ websocket + ev/s ═════════════════════
  let evCount = 0;
  let feedPaused = false;
  let tailPaused = false;
  setInterval(() => {
    $("evps").textContent = (evCount / 2).toFixed(1);
    evCount = 0;
  }, 2000);

  function wsStatus(on) {
    $("rail-ws").textContent = on ? "live" : "off";
    const dot = $("rail-ws-dot");
    dot.classList.toggle("live", on);
    dot.style.background = on ? "var(--good)" : "var(--danger)";
  }
  function connectWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    let backoff = 1000;
    function open() {
      const ws = new WebSocket(proto + "//" + location.host + "/api/events");
      ws.onopen = () => { backoff = 1000; wsStatus(true); };
      ws.onmessage = (m) => {
        let ev; try { ev = JSON.parse(m.data); } catch (_) { return; }
        if (ev.kind === "__hello__") return;
        evCount += 1;
        onLiveEvent(ev);
      };
      ws.onclose = () => {
        wsStatus(false);
        setTimeout(open, backoff);
        backoff = Math.min(8000, backoff * 2);
      };
      ws.onerror = () => ws.close();
    }
    open();
  }

  function feedRowEl(ev) {
    const kind = ev.kind || "—";
    const cls = kind.endsWith(".failed") || kind.endsWith(".rejected") ? "bad"
      : kind.endsWith(".completed") || kind.endsWith(".accepted") ? "ok"
      : kind.startsWith("handoff.") || kind.startsWith("approval.") ? "warn" : "";
    return el("div", { class: "feed-row" }, [
      el("span", { class: "ts num", text: fmtTime(ev.timestamp_ms || Date.parse(ev.timestamp) || Date.now()) }),
      el("span", { class: "adot", style: { background: agentColor(ev.agent_id || null) } }),
      el("span", { class: "what" }, [
        el("b", { text: ev.agent_id || "system" }),
        el("span", { text: " " + truncate(ev.payload_preview || kind, 90) }),
      ]),
      el("span", { class: "kind " + cls, text: kind.split(".")[0] }),
    ]);
  }
  function tailLineEl(ev) {
    const kind = ev.kind || "—";
    const k = kind.startsWith("memory.") ? "lk mem" : kind.endsWith(".failed") ? "lk err" : "lk";
    return el("div", { class: "ll" }, [
      el("span", { class: "lt", text: fmtTime(ev.timestamp_ms || Date.now()) + " " }),
      el("span", { class: k, text: kind }),
      el("span", { text: " agent=" + (ev.agent_id || "—") + (ev.payload_preview ? " " + truncate(ev.payload_preview, 90) : "") }),
    ]);
  }
  function onLiveEvent(ev) {
    // dashboard feed
    const feed = $("event-feed");
    if (feed && !feedPaused && currentView === "dashboard") {
      const kf = $("feed-kind").value;
      if (kf === "all" || (ev.kind || "").startsWith(kf + ".")) {
        feed.insertBefore(feedRowEl(ev), feed.firstChild);
        while (feed.children.length > 30) feed.removeChild(feed.lastChild);
      }
    }
    // debug tail
    const tail = $("log-tail");
    if (tail && !tailPaused && currentView === "debug") {
      const caret = tail.querySelector(".log-caret-line");
      tail.insertBefore(tailLineEl(ev), caret);
      while (tail.children.length > 40) tail.removeChild(tail.firstChild);
    }
  }
  $("feed-pause").addEventListener("click", () => {
    feedPaused = !feedPaused;
    $("feed-pause").textContent = feedPaused ? "▶ resume" : "⏸ pause";
  });
  $("tail-pause").addEventListener("click", () => {
    tailPaused = !tailPaused;
    $("tail-pause").textContent = tailPaused ? "▶ resume" : "⏸ pause";
  });

  // rail stats (every 30s)
  async function railStats() {
    try {
      const st = await api("/api/status");
      $("rail-records").textContent = (st.memory_records ?? 0).toLocaleString();
      $("rail-events").textContent = (st.events_total ?? 0).toLocaleString();
    } catch (_) { /* decorative */ }
  }
  setInterval(railStats, 30000);

  // ═════════════════════ chain toaster ═════════════════════
  const toaster = $("chain-toaster");
  async function traceEvents(chain) {
    // The chains endpoint strips payloads; per-task traces carry the full
    // story (dispatch from_agent, memory record ids, tool argv...).
    const out = [];
    const seenTask = new Set(), seenEv = new Set();
    for (const t of (chain.tasks || []).slice(0, 8)) {
      if (seenTask.has(t.task_id)) continue;
      seenTask.add(t.task_id);
      try {
        const tr = await api("/api/tasks/" + encodeURIComponent(t.task_id) + "/trace");
        for (const e of (tr.events || [])) {
          // trace events carry ISO `timestamp` + `id`; normalize to the
          // chains-endpoint shape the rest of the toaster expects
          if (!e.timestamp_ms) e.timestamp_ms = Date.parse(e.timestamp) || 0;
          const id = e.id || e.event_id || (e.kind + "|" + e.timestamp_ms + "|" + e.agent_id);
          if (seenEv.has(id)) continue;
          seenEv.add(id);
          out.push(e);
        }
      } catch (_) { /* fall back below */ }
    }
    return (out.length ? out : (chain.events || []).slice())
      .sort((a, b) => (a.timestamp_ms || 0) - (b.timestamp_ms || 0));
  }

  async function openToasterFor(chain) {
    const t0 = Date.parse(chain.started_at) || Date.now();
    const t1 = Math.max(t0 + 1000, Date.parse(chain.ended_at) || Date.now());
    const span = t1 - t0;
    const evs = await traceEvents(chain);

    // --- who really dispatched? the handoff payload knows. ---
    const basePath = chainAgents(chain);
    let dispatcher = basePath[0] || "?";
    for (const e of evs) {
      const p = e.payload || {};
      if (e.kind === "handoff.initiated" && p.from_agent) { dispatcher = p.from_agent; break; }
    }
    // custody-ordered display path: dispatcher, then each session opener
    const displayPath = [dispatcher];
    for (const e of evs) {
      if (e.kind === "session.opened" && e.agent_id && !displayPath.includes(e.agent_id)) {
        displayPath.push(e.agent_id);
      }
    }
    for (const a of basePath) {
      if (!displayPath.includes(a) && a !== "exocortex") displayPath.push(a);
    }

    const laneOf = (e) => {
      const p = e.payload || {};
      if (e.kind === "handoff.initiated") return p.from_agent || e.agent_id || dispatcher;
      if (!e.agent_id || (e.kind || "").startsWith("task.")) return dispatcher;
      return e.agent_id;
    };
    const SKIP_KINDS = new Set(["task.status_changed", "session.status_changed"]);
    const evLabel = (e) => {
      const k = e.kind || "";
      const p = e.payload || {};
      if (k.startsWith("tool.")) return k.split(".")[1] || "tool";
      if (k === "handoff.initiated") {
        return p.to_agent ? "dispatch \u2192 " + p.to_agent : "handoff back";
      }
      const m = {
        "task.created": "created", "task.dispatched": "dispatch",
        "session.opened": "accept", "memory.written": "memory_write",
        "memory.read": "memory_read", "task.completed": "completed",
        "task.failed": "failed", "session.closed": "close",
      };
      return m[k] || k.split(".").pop();
    };

    const hops = Math.max(1, displayPath.length - 1);
    const story = chainStory(chain);
    $("ct-title").textContent = truncate(story.what, 64);
    $("ct-meta").textContent =
      hops + (hops === 1 ? " hop" : " hops") + " \u00b7 " + fmtDur(span) + " \u00b7 " + (chain.status || "")
      + (story.did ? " \u00b7 " + story.did : "");

    // custody path row
    const cp = $("ct-path"); empty(cp);
    displayPath.forEach((a, i) => {
      cp.appendChild(el("div", { class: "chain-node" }, [
        el("span", { class: "nub", style: { background: agentColor(a) } }),
        el("span", { class: "nm", text: a }),
      ]));
      if (i < displayPath.length - 1) cp.appendChild(el("div", { class: "chain-link" }));
    });

    // ruler
    const ruler = $("ct-ruler"); empty(ruler);
    for (let k = 0; k <= 4; k++) {
      ruler.appendChild(el("span", {
        style: { left: (k * 25) + "%" },
        text: fmtTime(t0 + (span * k) / 4),
      }));
    }

    // --- custody segments: dispatcher holds; session.opened hands off;
    //     session.closed hands back. Blocks labeled by real activity. ---
    const lanes = $("ct-lanes"); empty(lanes);
    lanes.style.position = "relative";
    const segs = [];
    const handoffs = [];
    // custody is a STACK: nested dispatches (A→B→C) return to the previous
    // holder when a session closes, not to the root dispatcher.
    const stack = [dispatcher];
    let heldSince = t0;
    for (const e of evs) {
      const t = e.timestamp_ms || t0;
      const top = stack[stack.length - 1];
      if (e.kind === "session.opened" && e.agent_id && e.agent_id !== top) {
        segs.push({ agent: top, s: heldSince, e: t });
        handoffs.push({ t, to: e.agent_id });
        stack.push(e.agent_id); heldSince = t;
      } else if (e.kind === "session.closed" && e.agent_id === top && stack.length > 1) {
        segs.push({ agent: top, s: heldSince, e: t });
        stack.pop();
        handoffs.push({ t, to: stack[stack.length - 1] });
        heldSince = t;
      }
    }
    segs.push({ agent: stack[stack.length - 1], s: heldSince, e: t1 });

    for (const seg of segs) {
      if (seg.label) continue;
      const inside = evs.filter((e) => {
        const t = e.timestamp_ms || t0;
        return laneOf(e) === seg.agent && t >= seg.s - 500 && t <= seg.e + 500 && !SKIP_KINDS.has(e.kind);
      });
      const counts = [];
      for (const e of inside) {
        const k = evLabel(e);
        const last = counts[counts.length - 1];
        if (last && last.k === k) last.n += 1;
        else counts.push({ k, n: 1 });
      }
      seg.label = counts.map((c) => c.k + (c.n > 1 ? " \u00d7" + c.n : "")).join(" \u00b7 ")
        || (seg.agent === dispatcher ? "waiting" : "working");
    }

    // every task logged in the chain must appear: if a task's window has no
    // custody segment on its agent's lane (trace missing / session events
    // absent), draw a fallback block straight from the task log.
    for (const t of chain.tasks || []) {
      const a = t.agent_id;
      if (!a) continue;
      const ts = Date.parse(t.started_at) || t0;
      const te = Date.parse(t.ended_at) || t1;
      const covered = segs.some((sg) => sg.agent === a && sg.s < te && sg.e > ts);
      if (!covered) {
        segs.push({ agent: a, s: ts, e: te, label: (t.status || "task") + " \u00b7 from task log" });
      }
      if (!displayPath.includes(a)) displayPath.push(a);
    }

    for (const a of displayPath) {
      const track = el("div", { class: "g-track" });
      for (const seg of segs) {
        if (seg.agent !== a) continue;
        const leftPct = Math.max(0, ((seg.s - t0) / span) * 100);
        const wPct = Math.max(2.5, ((seg.e - seg.s) / span) * 100);
        track.appendChild(el("span", {
          class: "g-block", title: seg.label,
          style: {
            left: Math.min(leftPct, 97) + "%",
            width: Math.min(wPct, 100 - Math.min(leftPct, 97)) + "%",
            background: "color-mix(in srgb, " + agentColor(a) + " 82%, transparent)",
            color: agentColor(a),
          },
        }, [el("span", { style: { color: "#fff" }, text: seg.label })]));
      }
      lanes.appendChild(el("div", { class: "g-lane" }, [
        el("span", { class: "g-who ag-chip" }, [
          el("span", { class: "dot", style: { background: agentColor(a) } }),
          el("span", { class: "mono", text: a }),
        ]),
        track,
      ]));
    }
    for (const h of handoffs) {
      const leftPct = Math.max(0, Math.min(99, ((h.t - t0) / span) * 100));
      lanes.appendChild(el("div", {
        class: "g-handoff" + (leftPct > 80 ? " gh-flip" : ""),
        title: "custody \u2192 " + h.to + " @ " + fmtTime(h.t),
        style: { left: "calc(102px + (100% - 102px) * " + (leftPct / 100).toFixed(4) + ")" },
      }, [el("span", { class: "gh-label", style: { color: agentColor(h.to) }, text: "\u2192 " + h.to })]));
    }

    // ledger (payload-aware previews)
    const led = $("ct-events"); empty(led);
    for (const ev of evs.slice(0, 24)) {
      const p = ev.payload || {};
      const preview = ev.payload_preview
        || (ev.kind === "handoff.initiated" && p.from_agent ? p.from_agent + " \u2192 " + (p.to_agent || "done") : "")
        || (ev.kind === "memory.written" && p.record_id ? "record " + String(p.record_id).slice(0, 8) : "")
        || (ev.kind === "task.created" && p.goal ? truncate(String(p.goal).replace(/\s+/g, " "), 80) : "");
      led.appendChild(feedRowEl({
        kind: ev.kind, agent_id: laneOf(ev) === dispatcher && !ev.agent_id ? dispatcher : ev.agent_id,
        timestamp_ms: ev.timestamp_ms, payload_preview: preview,
      }));
    }

    // foot chips + memory jump
    const foot = $("ct-foot"); empty(foot);
    for (const [k, v] of [
      ["chain", String(chain.chain_id).slice(0, 8)],
      ["events", evs.length],
      ["agents", displayPath.length],
      ["tasks", (chain.tasks || []).length],
    ]) {
      foot.appendChild(el("span", { class: "ct-chip" }, [k + " ", el("b", { class: "num", text: String(v) })]));
    }
    const memEv = evs.find((e) => e.kind === "memory.written" && e.payload && e.payload.record_id);
    if (memEv) {
      foot.appendChild(el("span", {
        class: "ct-chip link",
        onclick: () => {
          closeToaster();
          showPage("constellation");
          cons.selId = memEv.payload.record_id;
          setTimeout(() => { renderConsDetail(); consDrawIfStill(); }, 200);
        },
      }, [
        el("span", { class: "cdot", style: { width: "7px", height: "7px", borderRadius: "50%", background: "var(--accent-2)" } }),
        "view written memory in constellation \u2197",
      ]));
    }
    toaster.classList.add("open");
  }
  async function openToasterByTask(taskId) {
    try {
      const chain = await api("/api/handoffs/chain/" + encodeURIComponent(taskId));
      openToasterFor(chain);
    } catch (e) { console.warn("chain fetch", e); }
  }
  function closeToaster() {
    toaster.classList.remove("open");
    setTimeout(() => {
      if (!toaster.classList.contains("open"))
        toaster.style.left = toaster.style.top = toaster.style.right = toaster.style.bottom = "";
    }, 420);
  }
  $("ct-close").addEventListener("click", closeToaster);
  // drag by header
  (function () {
    const drag = $("ct-drag");
    let on = false, ox = 0, oy = 0;
    drag.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".close")) return;
      on = true;
      const r = toaster.getBoundingClientRect();
      ox = e.clientX - r.left; oy = e.clientY - r.top;
      toaster.style.left = r.left + "px"; toaster.style.top = r.top + "px";
      toaster.style.right = "auto"; toaster.style.bottom = "auto";
      toaster.classList.add("dragging");
      drag.setPointerCapture(e.pointerId);
    });
    drag.addEventListener("pointermove", (e) => {
      if (!on) return;
      toaster.style.left = Math.min(Math.max(e.clientX - ox, 8 - toaster.offsetWidth * 0.6), window.innerWidth - 60) + "px";
      toaster.style.top = Math.min(Math.max(e.clientY - oy, 8), window.innerHeight - 48) + "px";
    });
    drag.addEventListener("pointerup", (e) => { on = false; toaster.classList.remove("dragging"); drag.releasePointerCapture(e.pointerId); });
  })();

  // ═════════════════════ dashboard ═════════════════════
  let chainMinHops = 1;
  let chainKind = "all";
  const chainAgentsOff = new Set();   // agents the operator toggled off
  let chainCache = [];

  LOADERS.dashboard = async function () {
    const [status, attention, growth, agentsRes, chainsRes, activity, tasksRes] = await Promise.all([
      api("/api/status"), api("/api/dashboard/attention"), api("/api/dashboard/growth"),
      api("/api/agents"), api("/api/handoffs/chains?limit=30"),
      api("/api/activity?limit=120"), api("/api/tasks?limit=200"),
    ]);

    // rail
    $("rail-records").textContent = (status.memory_records ?? 0).toLocaleString();
    $("rail-events").textContent = (status.events_total ?? 0).toLocaleString();

    // ---- KPIs + sparks ----
    const agents = agentsRes.agents || [];
    const evTs = (activity.items || []).map((e) => e.timestamp_ms).filter(Boolean);
    const taskTs = (tasksRes.tasks || []).map((t) => Date.parse(t.created_at)).filter(Boolean);
    $("kpi-tasks").textContent = status.tasks ?? tasksRes.count ?? 0;
    sparkline("spark-tasks", hourlyBuckets(taskTs, 8));
    const openCount = (tasksRes.tasks || []).filter((t) => t.status_bucket === "open").length;
    $("kpi-tasks-delta").textContent = openCount ? openCount + " running" : "none running";
    $("kpi-tasks-delta").className = "delta num" + (openCount ? " up" : "");

    $("kpi-events").textContent = status.events_last_hour ?? 0;
    sparkline("spark-events", hourlyBuckets(evTs, 8));
    $("kpi-events-delta").textContent = (status.events_total ?? 0).toLocaleString() + " total";

    $("kpi-records").textContent = status.memory_records ?? 0;
    const memTs = (activity.items || [])
      .filter((e) => (e.kind || "").startsWith("memory.written"))
      .map((e) => e.timestamp_ms);
    sparkline("spark-records", hourlyBuckets(memTs, 8));
    $("kpi-records-delta").textContent = "+" + (growth.records_today ?? 0) + " today";
    $("kpi-records-delta").className = "delta num" + ((growth.records_today ?? 0) > 0 ? " up" : "");

    const activeNames = status.agents_active_last_hour || [];
    $("kpi-agents").textContent = activeNames.length;
    sparkline("spark-agents", hourlyBuckets(evTs, 8));
    $("kpi-agents-delta").textContent = activeNames.length ? activeNames.join(" · ") : "of " + agents.length + " known";

    const attnItems = attention.items || [];
    $("kpi-attn").textContent = attnItems.length;
    $("kpi-attn-card").classList.toggle("attn", attnItems.length > 0);
    $("kpi-attn-glyph").style.color = attnItems.length ? "var(--warn)" : "var(--good)";
    $("kpi-attn-delta").textContent = attnItems.length ? "needs review ↓" : "all clear";
    $("kpi-attn-delta").className = "delta" + (attnItems.length ? " warn" : "");

    // ---- attention panel ----
    $("attn-count").textContent = attnItems.length;
    const al = $("attn-list");
    if (!attnItems.length) note(al, "● all clear");
    else {
      empty(al);
      for (const it of attnItems) {
        const sev = it.severity === "high" ? "warn" : "info";
        al.appendChild(el("div", { class: "attn-item " + sev }, [
          el("span", { class: "stripe" }),
          el("div", { class: "body" }, [
            el("div", { class: "t", text: it.title || it.kind || "attention" }),
            el("div", { class: "d", text: truncate(it.body || "", 160) }),
          ]),
          el("span", { class: "age num", text: fmtRel(Date.parse(it.since) || null) }),
          it.action_url ? el("button", {
            class: "act", type: "button", text: "Open",
            onclick: () => {
              const v = PATH_VIEW[String(it.action_url).split("?")[0]];
              if (v) showPage(v); else location.href = it.action_url;
            },
          }) : null,
        ]));
      }
    }

    // ---- presence ----
    const pl = $("presence-list"); empty(pl);
    const sorted = agents.slice().sort((a, b) =>
      (Date.parse(b.last_active_at) || 0) - (Date.parse(a.last_active_at) || 0));
    const now = Date.now();
    for (const a of sorted) {
      const id = a.agent_id || a.id;
      const last = Date.parse(a.last_active_at) || 0;
      const live = a.recently_active || now - last < 5 * 60e3;
      const dormant = !last || now - last > 86400e3;
      const color = agentColor(id);
      pl.appendChild(el("div", { class: "agent-card" + (live ? " is-live" : dormant ? " dormant" : "") }, [
        el("span", { class: "avatar", style: { background: color }, text: agentAbbr(id) }),
        el("div", { class: "who" }, [
          el("div", { class: "nm", text: id }),
          el("div", { class: "st" }, live
            ? [el("span", { class: "live-word", text: "● active" }), el("span", { text: " · " + fmtRel(last) })]
            : [el("span", { text: (dormant ? "dormant · last seen " : "idle ") + fmtRel(last) })]),
        ]),
        el("div", { class: "trace" }, [hourlyTrace(a.hourly || [], color, live, dormant)]),
      ]));
    }

    // ---- in-flight ----
    const inflight = (tasksRes.tasks || [])
      .filter((t) => t.status_bucket === "open")
      .slice(0, 5);
    const il = $("inflight-list");
    if (!inflight.length) note(il, "no in-flight dispatches");
    else {
      empty(il);
      for (const t of inflight) {
        const row = el("div", {
          class: "dispatch clickable", role: "button", tabindex: "0",
          onclick: () => openToasterByTask(t.task_id),
        }, [
          agChip(t.owning_agent || (t.agents || [])[0] || "?"),
          el("span", { class: "arrow mono", text: "⟶" }),
          el("span", { class: "task", text: truncate(t.title || t.goal || "", 80) }),
          el("span", { class: "elapsed num", text: fmtRel(Date.parse(t.created_at) || null) }),
        ]);
        il.appendChild(row);
        il.appendChild(el("div", { style: { height: "6px" } }));
      }
    }

    // ---- feed ----
    const feed = $("event-feed"); empty(feed);
    for (const ev of (activity.items || []).slice(0, 30)) feed.appendChild(feedRowEl(ev));
    $("feed-kind").onchange = () => LOADERS.dashboard();

    // ---- grown ----
    $("g-today").textContent = growth.records_today ?? 0;
    $("g-week").textContent = growth.records_week ?? 0;
    $("g-chat").textContent = growth.chat_queries_today ?? 0;
    $("g-pq").textContent = growth.profile_questions_open ?? 0;
    const tags = growth.top_tags || [];
    const gt = $("g-tags");
    if (!tags.length) note(gt, "—");
    else {
      empty(gt);
      const maxC = Math.max(...tags.map((t) => t.count_today || 0), 1);
      for (const t of tags.slice(0, 6)) {
        gt.appendChild(el("div", { class: "trow" }, [
          el("span", { class: "tname", text: t.tag }),
          el("span", { class: "bar" }, [el("i", { style: { width: ((t.count_today / maxC) * 100) + "%" } })]),
          el("span", { class: "tv num", text: String(t.count_today) }),
        ]));
      }
    }
    // latest decisions from constellation cache (lazy)
    fillDecisions();

    // ---- chains ----
    chainCache = chainsRes.items || [];
    buildChainAgentFilters();
    renderChains();
  };

  function hourlyTrace(hourly, color, live, dormant) {
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("width", "96"); svg.setAttribute("height", "22");
    svg.setAttribute("viewBox", "0 0 96 22"); svg.setAttribute("aria-hidden", "true");
    const max = Math.max(...hourly, 1);
    let d = "";
    const n = Math.max(hourly.length, 2);
    for (let i = 0; i < n; i++) {
      const x = (i / (n - 1)) * 96;
      const y = dormant ? 11 : 18 - ((hourly[i] || 0) / max) * 14;
      d += (i ? " L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
    }
    const base = document.createElementNS(NS, "path");
    base.setAttribute("d", d); base.setAttribute("fill", "none");
    base.setAttribute("stroke", color); base.setAttribute("stroke-width", "1.6");
    base.setAttribute("stroke-linejoin", "round");
    if (dormant) { base.setAttribute("opacity", ".45"); base.setAttribute("stroke-dasharray", "2 4"); }
    else base.setAttribute("opacity", live ? ".35" : ".55");
    svg.appendChild(base);
    if (live) {
      const sweep = document.createElementNS(NS, "path");
      sweep.setAttribute("d", d); sweep.setAttribute("fill", "none");
      sweep.setAttribute("stroke", color); sweep.setAttribute("stroke-width", "1.6");
      sweep.setAttribute("pathLength", "100"); sweep.setAttribute("class", "trace-sweep");
      svg.appendChild(sweep);
    }
    return svg;
  }

  async function fillDecisions() {
    try {
      const pts = await consData();
      const dec = pts.filter((p) => p.type === "decision")
        .sort((a, b) => b.ts - a.ts).slice(0, 3);
      const gd = $("g-decisions");
      if (!dec.length) return note(gd, "—");
      empty(gd);
      for (const d of dec) {
        gd.appendChild(el("div", { class: "clickable", onclick: () => { showPage("constellation"); cons.select(d.id); } }, [
          el("span", { style: { color: "var(--accent-2)" }, text: "◆ " }),
          el("span", { text: truncate(d.content, 64) + " " }),
          el("span", { class: "mono num", style: { fontSize: "10.5px", color: "var(--faint)" }, text: d.d }),
        ]));
      }
    } catch (_) { /* optional */ }
  }

  // Custody path derived from the audit events: who actually touched the
  // chain, in order (dispatcher first, executor after) — agents_path alone
  // often only names the executor.
  function chainAgents(c) {
    const seen = [];
    for (const e of c.events || []) {
      const a = e.agent_id;
      if (a && !seen.includes(a)) seen.push(a);
    }
    for (const a of c.agents_path || []) if (!seen.includes(a)) seen.push(a);
    return seen.length ? seen : ["?"];
  }
  // Human summary — what the handoff was + what it did, from the goal and
  // the event ledger (never the raw prompt).
  function chainStory(c) {
    const goal = ((c.tasks && c.tasks[0] && c.tasks[0].goal_preview) || c.goal || "").replace(/\s+/g, " ");
    let what = "";
    const conv = goal.match(/conversation with ([^.]+?) about:\s*(.+?)(?:\.|Transcript|$)/i);
    if (conv) {
      what = "conversation \u201c" + truncate(conv[2].trim(), 42) + "\u201d \u00b7 with " + truncate(conv[1].trim(), 36);
    } else {
      what = goal.replace(/^You are [\w-]+\.?,?\s*/i, "").trim();
      const firstSentence = what.split(/(?<=[.!?])\s/)[0] || what;
      what = truncate(firstSentence, 88);
    }
    const evs = c.events || [];
    const did = [];
    const mem = evs.filter((e) => e.kind === "memory.written").length;
    if (mem) did.push("wrote " + mem + (mem === 1 ? " memory" : " memories"));
    const tools = evs.filter((e) => (e.kind || "").startsWith("tool.")).length;
    if (tools) did.push(tools + (tools === 1 ? " tool call" : " tool calls"));
    const replies = evs.filter((e) => e.kind === "handoff.initiated" && e.agent_id && e.agent_id !== "exocortex").length;
    if (replies) did.push(replies + (replies === 1 ? " handoff back" : " handoffs back"));
    return { what: what || "task", did: did.join(" \u00b7 ") };
  }

  function chainIsConversation(c) {
    const goal = ((c.tasks && c.tasks[0] && c.tasks[0].goal_preview) || c.goal || "");
    return /multi-agent conversation|conversation with .+ about:/i.test(goal);
  }
  // The coordinator (first agent on the path) dispatches nearly every chain,
  // so filtering on it would blank the list — chips filter on the agents the
  // work was handed TO.
  function chainExecutors(c) {
    const path = chainAgents(c);
    return path.length > 1 ? path.slice(1) : path;
  }
  function buildChainAgentFilters() {
    const host = $("chain-agent-filters");
    if (!host) return;
    const seen = new Map();
    for (const c of chainCache) {
      for (const a of chainExecutors(c)) seen.set(a, (seen.get(a) || 0) + 1);
    }
    empty(host);
    if (!seen.size) { host.style.display = "none"; return; }
    host.style.display = "";
    host.appendChild(el("span", {
      class: "mono",
      style: { fontSize: "9.5px", letterSpacing: ".12em", textTransform: "uppercase", color: "var(--faint)" },
      text: "handed to",
    }));
    for (const [a, n] of [...seen.entries()].sort((x, y) => y[1] - x[1]).slice(0, 8)) {
      const off = chainAgentsOff.has(a);
      const chip = el("button", { class: "filter-chip" + (off ? " off" : " on"), type: "button", title: n + " chains" }, [
        el("span", { class: "dot", style: { background: agentColor(a) } }), a,
      ]);
      chip.addEventListener("click", () => {
        if (chainAgentsOff.has(a)) { chainAgentsOff.delete(a); chip.classList.add("on"); chip.classList.remove("off"); }
        else { chainAgentsOff.add(a); chip.classList.remove("on"); chip.classList.add("off"); }
        renderChains();
      });
      host.appendChild(chip);
    }
  }
  function renderChains() {
    const host = $("chains-list");
    const items = chainCache.filter((c) => {
      const path = chainAgents(c);
      if (Math.max(1, path.length - 1) < chainMinHops) return false;
      if (chainExecutors(c).some((a) => chainAgentsOff.has(a))) return false;   // hide chains handed to toggled-off agents
      if (chainKind === "conversation" && !chainIsConversation(c)) return false;
      if (chainKind === "dispatch" && chainIsConversation(c)) return false;
      return true;
    });
    $("chains-count").textContent = items.length;
    if (!items.length) return note(host, "no chains match — adjust the agent / kind / hop filters above");
    empty(host);
    for (const c of items.slice(0, 12)) {
      const path = chainAgents(c);
      const hops = Math.max(1, path.length - 1);
      const status = c.status || "unknown";
      const sCls = status === "completed" || status === "succeeded" ? "ok" : status === "failed" ? "bad" : "run";
      const lane = el("div", { class: "chain-lane" });
      path.forEach((a, i) => {
        lane.appendChild(el("div", { class: "chain-node" }, [
          el("span", { class: "nub", style: { background: agentColor(a) } }),
          el("span", { class: "nm", text: a }),
        ]));
        if (i < path.length - 1) lane.appendChild(el("div", { class: "chain-link" }));
      });
      const dur = (Date.parse(c.ended_at) || Date.now()) - (Date.parse(c.started_at) || Date.now());
      const story = chainStory(c);
      const card = el("div", {
        class: "chain-card", tabindex: "0", role: "button",
        onclick: () => openToasterFor(c),
        onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openToasterFor(c); } },
      }, [
        el("div", { class: "meta" }, [
          el("span", { class: "hops", text: hops + (hops === 1 ? " hop" : " hops") }),
          el("span", { text: "·" }),
          el("span", { class: "num", text: fmtDur(dur) }),
          el("span", { text: "·" }),
          el("span", {
            style: { color: sCls === "ok" ? "var(--good)" : sCls === "bad" ? "var(--danger)" : "var(--accent)" },
            text: (sCls === "ok" ? "✓ " : sCls === "bad" ? "✕ " : "● ") + status,
          }),
          el("span", { style: { marginLeft: "auto" }, text: String(c.chain_id).slice(0, 8) }),
        ]),
        lane,
        el("div", { class: "desc", text: story.what + (story.did ? " — " + story.did : "") }),
      ]);
      host.appendChild(card);
    }
  }
  document.querySelectorAll("#chain-kind-chips button").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll("#chain-kind-chips button").forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      chainKind = b.dataset.ckind;
      renderChains();
    }));
  document.querySelectorAll("#chain-hop-chips button").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll("#chain-hop-chips button").forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      chainMinHops = +b.dataset.minhops;
      renderChains();
    }));

  // ═════════════════════ constellation ═════════════════════
  const cons = {
    started: false, needsColor: true, points: [], edges: [],
    view: "graph", window: "all", selId: null, hoverId: -1,
    agentsOn: new Set(), typesOn: new Set(), searchIds: null,
    zoom: 1, panX: 0, panY: 0,
    pos: [], layouts: {}, colors: {}, scopes: [],
    canvas: null, ctx: null, W: 0, H: 0,
    select(id) { this.selId = id; renderConsDetail(); },
  };
  let consPointsPromise = null;
  function consData() {
    if (!consPointsPromise) {
      consPointsPromise = api("/api/memory/constellation").then((d) => {
        const pts = (d.points || []).map((p, i) => ({
          ...p, i, ts: Date.parse(p.timestamp) || 0,
          d: (p.timestamp || "").slice(0, 10),
        }));
        return pts;
      });
    }
    return consPointsPromise;
  }
  const SCOPE_TOKEN = {
    session: "--cl-reachy", task: "--cl-exocortex", project: "--cl-trading",
    global: "--cl-personal", user: "--cl-research",
  };

  LOADERS.constellation = async function () {
    const pts = await consData();
    if (!cons.started) consInit(pts);
    consResize();
    updateConsStats();
  };

  function consInit(pts) {
    cons.started = true;
    cons.points = pts;
    // normalized embedding layout from real x/y
    let minx = 1e9, maxx = -1e9, miny = 1e9, maxy = -1e9;
    for (const p of pts) {
      minx = Math.min(minx, p.x); maxx = Math.max(maxx, p.x);
      miny = Math.min(miny, p.y); maxy = Math.max(maxy, p.y);
    }
    const nx = (x) => 0.06 + 0.88 * ((x - minx) / (maxx - minx || 1));
    const ny = (y) => 0.08 + 0.82 * ((y - miny) / (maxy - miny || 1));
    cons.layouts.graph = pts.map((p) => [nx(p.x), ny(p.y)]);
    // scope clusters layout
    cons.scopes = [...new Set(pts.map((p) => p.scope || "task"))];
    const sc = {};
    cons.scopes.forEach((s, k) => {
      sc[s] = [0.16 + (k % 3) * 0.34, k < 3 ? 0.3 : 0.72];
    });
    const perIdx = {};
    cons.layouts.clusters = pts.map((p) => {
      const s = p.scope || "task";
      const i = perIdx[s] = (perIdx[s] || 0) + 1;
      const nn = pts.filter((q) => (q.scope || "task") === s).length;
      const ang = (i / nn) * Math.PI * 2;
      const r = 0.05 + 0.06 * ((i % 3) / 2);
      return [sc[s][0] + r * 1.4 * Math.cos(ang), sc[s][1] + r * Math.sin(ang)];
    });
    cons._scopeCenters = sc;
    // timeline layout
    const t0 = Math.min(...pts.map((p) => p.ts).filter(Boolean));
    const t1 = Date.now();
    cons.layouts.timeline = pts.map((p) => {
      const x = 0.06 + 0.86 * ((p.ts - t0) / (t1 - t0 || 1));
      const lane = cons.scopes.indexOf(p.scope || "task");
      return [x, 0.14 + lane * (0.7 / Math.max(1, cons.scopes.length - 1) || 0.2)];
    });
    cons.pos = cons.layouts.graph.map((p) => [...p]);
    // kNN edges (embedding distance ≈ similarity)
    const edges = [];
    for (let i = 0; i < pts.length; i++) {
      if (!pts[i].has_embedding) continue;
      const dists = [];
      for (let j = 0; j < pts.length; j++) {
        if (i === j || !pts[j].has_embedding) continue;
        const dx = pts[i].x - pts[j].x, dy = pts[i].y - pts[j].y;
        dists.push([dx * dx + dy * dy, j]);
      }
      dists.sort((a, b) => a[0] - b[0]);
      for (const [d2, j] of dists.slice(0, 2)) {
        if (i < j) edges.push([i, j, Math.max(0.25, 1 - Math.sqrt(d2) * 3)]);
        else if (!edges.some((e) => e[0] === j && e[1] === i)) edges.push([j, i, Math.max(0.25, 1 - Math.sqrt(d2) * 3)]);
      }
    }
    cons.edges = edges;
    // filters: agents + types present
    const srcCounts = {}, typeCounts = {};
    for (const p of pts) {
      srcCounts[p.source] = (srcCounts[p.source] || 0) + 1;
      typeCounts[p.type] = (typeCounts[p.type] || 0) + 1;
    }
    const topSrc = Object.entries(srcCounts).sort((a, b) => b[1] - a[1]).slice(0, 6).map((e) => e[0]);
    const topTypes = Object.entries(typeCounts).sort((a, b) => b[1] - a[1]).slice(0, 6).map((e) => e[0]);
    cons.agentsOn = new Set(topSrc);
    cons.typesOn = new Set(Object.keys(typeCounts));
    const cf = $("cons-filters"); empty(cf);
    cf.appendChild(el("span", { class: "mono", style: { fontSize: "9.5px", letterSpacing: ".12em", textTransform: "uppercase", color: "var(--faint)" }, text: "agents" }));
    for (const s of topSrc) {
      const chip = el("button", { class: "filter-chip on", type: "button" }, [
        el("span", { class: "dot", style: { background: agentColor(s) } }), s,
      ]);
      chip.addEventListener("click", () => {
        if (cons.agentsOn.has(s)) { cons.agentsOn.delete(s); chip.classList.remove("on"); chip.classList.add("off"); }
        else { cons.agentsOn.add(s); chip.classList.add("on"); chip.classList.remove("off"); }
        updateConsStats(); consDrawIfStill();
      });
      cf.appendChild(chip);
    }
    cf.appendChild(el("span", { class: "mono", style: { fontSize: "9.5px", letterSpacing: ".12em", textTransform: "uppercase", color: "var(--faint)", marginLeft: "10px" }, text: "types" }));
    for (const t of topTypes) {
      const chip = el("button", { class: "filter-chip on", type: "button", text: typeGlyph(t) + " " + t });
      chip.addEventListener("click", () => {
        if (cons.typesOn.has(t)) { cons.typesOn.delete(t); chip.classList.remove("on"); chip.classList.add("off"); }
        else { cons.typesOn.add(t); chip.classList.add("on"); chip.classList.remove("off"); }
        updateConsStats(); consDrawIfStill();
      });
      cf.appendChild(chip);
    }
    // legend: scope colors
    const lg = $("cons-legend"); empty(lg);
    for (const s of cons.scopes) {
      lg.appendChild(el("div", { class: "lrow" }, [
        el("span", { class: "swatch", style: { background: cssVar(SCOPE_TOKEN[s] || "--cl-exocortex", "#8A66EC") } }),
        s,
      ]));
    }

    // canvas
    cons.canvas = $("cons-canvas");
    cons.ctx = cons.canvas.getContext("2d");
    bindConsEvents();
    window.addEventListener("resize", consResize);
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    cons.reduced = reduced;
    if (!reduced) {
      setInterval(consSpawnPulse, 1100);
      requestAnimationFrame(consTick);
    } else consDraw();
    // search
    let debounce = null;
    $("cons-search").addEventListener("input", (e) => {
      clearTimeout(debounce);
      const q = e.target.value.trim();
      debounce = setTimeout(async () => {
        if (!q) { cons.searchIds = null; updateConsStats(); consDrawIfStill(); return; }
        try {
          const res = await api("/api/memory/search?q=" + encodeURIComponent(q) + "&limit=40");
          cons.searchIds = new Set((res.results || res.items || []).map((r) => r.id));
          updateConsStats(); consDrawIfStill();
        } catch (_) { /* search optional */ }
      }, 250);
    });
    // view chips + window
    document.querySelectorAll("[data-cview]").forEach((b) =>
      b.addEventListener("click", () => {
        document.querySelectorAll("[data-cview]").forEach((x) => x.classList.remove("on"));
        b.classList.add("on");
        cons.view = b.dataset.cview;
        if (cons.reduced) { cons.pos = cons.layouts[cons.view].map((p) => [...p]); consDraw(); }
      }));
    $("cons-window").addEventListener("change", (e) => {
      cons.window = e.target.value;
      updateConsStats(); consDrawIfStill();
    });
    $("cz-in").addEventListener("click", () => consZoomAt(cons.W / 2, cons.H / 2, 1.35));
    $("cz-out").addEventListener("click", () => consZoomAt(cons.W / 2, cons.H / 2, 1 / 1.35));
    $("cz-reset").addEventListener("click", () => { cons.zoom = 1; cons.panX = 0; cons.panY = 0; consDrawIfStill(); });
  }

  function consVisible(p) {
    if (!cons.typesOn.has(p.type)) return false;
    if (cons.agentsOn.size && !cons.agentsOn.has(p.source)) {
      // sources outside the top chips are visible unless explicitly filtered set exists and excludes:
      if ([...cons.agentsOn].length && Object.keys(AGENT_TOKEN).includes(p.source)) return false;
      if (cons.agentsOn.size && [...cons.agentsOn].some(() => true) && cons._chipSources && cons._chipSources.has(p.source)) return false;
    }
    if (cons.window !== "all" && Date.now() - p.ts > (+cons.window) * 86400e3) return false;
    if (cons.searchIds && !cons.searchIds.has(p.id)) return false;
    return true;
  }
  function updateConsStats() {
    const vis = cons.points.filter(consVisible).length;
    const ve = cons.edges.filter(([a, b]) => consVisible(cons.points[a]) && consVisible(cons.points[b])).length;
    $("cs-n").textContent = vis;
    $("cs-e").textContent = ve;
  }
  function consResize() {
    if (!cons.canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const r = cons.canvas.getBoundingClientRect();
    cons.W = r.width; cons.H = r.height;
    cons.canvas.width = r.width * dpr; cons.canvas.height = r.height * dpr;
    cons.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (cons.reduced) consDraw();
  }
  function consReadColors() {
    cons.colors = {
      edge: cssVar("--canvas-edge", "rgba(140,120,220,.25)"),
      text: cssVar("--muted", "#888"), faint: cssVar("--faint", "#666"),
      pulse: cssVar("--accent-2", "#a18bf5"),
    };
    for (const s of cons.scopes) cons.colors[s] = cssVar(SCOPE_TOKEN[s] || "--cl-exocortex", "#8A66EC");
    cons.needsColor = false;
  }
  function sx(x) { return x * cons.zoom + cons.panX; }
  function sy(y) { return y * cons.zoom + cons.panY; }
  let consT = 0;
  function pnx(i) {
    let b = cons.pos[i][0] * cons.W;
    if (cons.view === "graph" && !cons.reduced) b += Math.sin(consT * 0.008 + i * 1.7) * 2.0;
    return sx(b);
  }
  function pny(i) {
    let b = cons.pos[i][1] * cons.H;
    if (cons.view === "graph" && !cons.reduced) b += Math.cos(consT * 0.011 + i * 2.3) * 2.0;
    return sy(b);
  }
  function consZoomAt(mx, my, f) {
    const nz = Math.min(4, Math.max(0.4, cons.zoom * f));
    const ff = nz / cons.zoom;
    cons.panX = mx - (mx - cons.panX) * ff;
    cons.panY = my - (my - cons.panY) * ff;
    cons.zoom = nz;
    consDrawIfStill();
  }
  function consDrawIfStill() { if (cons.reduced) consDraw(); }
  const consPulses = [];
  function consSpawnPulse() {
    if (currentView !== "constellation") return;
    const vis = cons.edges.filter(([a, b, w]) =>
      w > 0.5 && consVisible(cons.points[a]) && consVisible(cons.points[b]));
    if (!vis.length) return;
    const e = vis[Math.floor(Math.random() * vis.length)];
    consPulses.push({ a: e[0], b: e[1], p: 0, sp: 0.008 + Math.random() * 0.006 });
    if (consPulses.length > 4) consPulses.shift();
  }
  function consTick() {
    consT += 1;
    if (currentView === "constellation") {
      const target = cons.layouts[cons.view];
      for (let i = 0; i < cons.pos.length; i++) {
        cons.pos[i][0] += (target[i][0] - cons.pos[i][0]) * 0.07;
        cons.pos[i][1] += (target[i][1] - cons.pos[i][1]) * 0.07;
      }
      for (let i = consPulses.length - 1; i >= 0; i--) {
        consPulses[i].p += consPulses[i].sp;
        if (consPulses[i].p >= 1) consPulses.splice(i, 1);
      }
      consDraw();
    }
    requestAnimationFrame(consTick);
  }
  function consDraw() {
    if (!cons.ctx) return;
    if (cons.needsColor) consReadColors();
    const ctx = cons.ctx, W = cons.W, H = cons.H;
    ctx.clearRect(0, 0, W, H);
    const pts = cons.points;
    // scaffolding
    ctx.font = "10px ui-monospace, Menlo, monospace";
    if (cons.view === "timeline") {
      ctx.fillStyle = cons.colors.faint;
      cons.scopes.forEach((s, k) => {
        ctx.fillStyle = cons.colors[s];
        ctx.globalAlpha = 0.8;
        ctx.fillText(s, sx(10), sy((0.14 + k * (0.7 / Math.max(1, cons.scopes.length - 1) || 0.2)) * H - 12));
        ctx.globalAlpha = 1;
      });
    }
    if (cons.view === "clusters" && cons._scopeCenters) {
      ctx.textAlign = "center";
      for (const [s, c] of Object.entries(cons._scopeCenters)) {
        ctx.fillStyle = cons.colors[s];
        ctx.globalAlpha = 0.85;
        ctx.fillText(s.toUpperCase(), sx(c[0] * W), sy((c[1] - 0.14) * H));
        ctx.globalAlpha = 1;
      }
      ctx.textAlign = "left";
    }
    const focus = cons.hoverId >= 0 ? cons.hoverId : (cons.selId != null ? pts.findIndex((p) => p.id === cons.selId) : -1);
    const neighbors = new Set();
    if (focus >= 0) for (const [a, b] of cons.edges) {
      if (a === focus) neighbors.add(b);
      if (b === focus) neighbors.add(a);
    }
    // edges
    for (const [a, b, w] of cons.edges) {
      const va = consVisible(pts[a]), vb = consVisible(pts[b]);
      ctx.beginPath();
      ctx.moveTo(pnx(a), pny(a));
      ctx.lineTo(pnx(b), pny(b));
      let alpha = va && vb ? 0.1 + w * 0.25 : 0.03;
      if (focus >= 0 && (a === focus || b === focus)) alpha = 0.75;
      else if (focus >= 0) alpha *= 0.35;
      ctx.strokeStyle = cons.colors.edge;
      ctx.globalAlpha = alpha;
      ctx.lineWidth = (0.7 + w * 1.3) * (0.6 + 0.4 * cons.zoom);
      ctx.stroke();
      ctx.globalAlpha = 1;
    }
    // pulses
    for (const p of consPulses) {
      const x = pnx(p.a) + (pnx(p.b) - pnx(p.a)) * p.p;
      const y = pny(p.a) + (pny(p.b) - pny(p.a)) * p.p;
      ctx.beginPath();
      ctx.arc(x, y, 2.4, 0, Math.PI * 2);
      ctx.fillStyle = cons.colors.pulse;
      ctx.globalAlpha = 0.9 * Math.sin(p.p * Math.PI);
      ctx.shadowColor = cons.colors.pulse; ctx.shadowBlur = 10;
      ctx.fill();
      ctx.shadowBlur = 0; ctx.globalAlpha = 1;
    }
    // nodes
    const now = Date.now();
    pts.forEach((p, i) => {
      const v = consVisible(p);
      const x = pnx(i), y = pny(i);
      const r = 3.2 * Math.sqrt(cons.zoom) + (p.has_embedding ? 0.8 : 0);
      const recent = now - p.ts < 3 * 86400e3;
      const isFocus = i === focus, isNb = neighbors.has(i);
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fillStyle = cons.colors[p.scope || "task"] || cons.colors.text;
      let alpha = v ? 0.95 : 0.06;
      if (focus >= 0 && !isFocus && !isNb) alpha = v ? 0.25 : 0.04;
      ctx.globalAlpha = alpha;
      if (v && (recent || isFocus)) {
        ctx.shadowColor = ctx.fillStyle;
        ctx.shadowBlur = recent && !cons.reduced ? 8 + Math.sin(consT * 0.05 + i) * 4 : 10;
      }
      ctx.fill();
      ctx.shadowBlur = 0;
      if (p.id === cons.selId && v) {
        ctx.beginPath();
        ctx.arc(x, y, r + 4, 0, Math.PI * 2);
        ctx.strokeStyle = cons.colors.pulse;
        ctx.globalAlpha = 0.9; ctx.lineWidth = 1.4;
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
      if (v && (isFocus || isNb || cons.zoom > 1.6)) {
        ctx.fillStyle = isFocus ? cons.colors.text : cons.colors.faint;
        ctx.globalAlpha = isFocus || isNb ? 1 : 0.8;
        ctx.fillText(truncate(p.content, 34), x + r + 5, y + 3);
        ctx.globalAlpha = 1;
      }
    });
  }
  function bindConsEvents() {
    const c = cons.canvas;
    let dragging = false, moved = false, lx = 0, ly = 0;
    c.style.cursor = "grab";
    c.addEventListener("pointerdown", (e) => { dragging = true; moved = false; lx = e.clientX; ly = e.clientY; c.setPointerCapture(e.pointerId); });
    c.addEventListener("pointerup", (e) => { dragging = false; c.releasePointerCapture(e.pointerId); c.style.cursor = cons.hoverId >= 0 ? "pointer" : "grab"; });
    c.addEventListener("pointermove", (e) => {
      const r = c.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      if (dragging) {
        const dx = e.clientX - lx, dy = e.clientY - ly;
        if (Math.abs(dx) + Math.abs(dy) > 2) moved = true;
        cons.panX += dx; cons.panY += dy;
        lx = e.clientX; ly = e.clientY;
        c.style.cursor = "grabbing";
        $("cons-tip").style.opacity = 0;
        consDrawIfStill();
        return;
      }
      cons.hoverId = -1;
      let best = 13;
      cons.points.forEach((p, i) => {
        if (!consVisible(p)) return;
        const d = Math.hypot(pnx(i) - mx, pny(i) - my);
        if (d < best) { best = d; cons.hoverId = i; }
      });
      const tip = $("cons-tip");
      if (cons.hoverId >= 0) {
        const p = cons.points[cons.hoverId];
        tip.innerHTML = "";
        tip.appendChild(el("div", { class: "tt", text: typeGlyph(p.type) + " " + truncate(p.content, 90) }));
        tip.appendChild(el("div", { class: "tm", text: (p.source || "?") + " · " + p.d + " · " + (p.scope || "") }));
        tip.style.left = Math.min(mx + 14, r.width - 270) + "px";
        tip.style.top = (my + 12) + "px";
        tip.style.opacity = 1;
        c.style.cursor = "pointer";
      } else { tip.style.opacity = 0; c.style.cursor = "grab"; }
      consDrawIfStill();
    });
    c.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = c.getBoundingClientRect();
      consZoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY * 0.0016));
    }, { passive: false });
    c.addEventListener("dblclick", (e) => { e.preventDefault(); cons.zoom = 1; cons.panX = 0; cons.panY = 0; consDrawIfStill(); });
    c.addEventListener("click", () => {
      if (moved) return;
      cons.selId = cons.hoverId >= 0 ? cons.points[cons.hoverId].id : null;
      renderConsDetail();
      consDrawIfStill();
    });
  }
  function renderConsDetail() {
    const host = $("cons-detail-body");
    const p = cons.points.find((q) => q.id === cons.selId);
    if (!p) {
      host.innerHTML = "";
      host.appendChild(el("div", { class: "empty", text: "click a star to inspect it — link brightness = similarity" }));
      return;
    }
    const i = p.i;
    const rel = cons.edges
      .filter(([a, b]) => a === i || b === i)
      .map(([a, b, w]) => ({ o: cons.points[a === i ? b : a], w }))
      .sort((x, y) => y.w - x.w).slice(0, 5);
    empty(host);
    host.appendChild(el("div", { class: "cd-type", style: { color: cons.colors[p.scope] || "var(--accent-2)" }, text: typeGlyph(p.type) + " " + p.type + " · " + (p.scope || "") + (p.scope_id ? "/" + truncate(p.scope_id, 18) : "") }));
    host.appendChild(el("div", { class: "cd-title", text: truncate(p.content, 120) }));
    host.appendChild(el("div", { class: "cd-meta" }, [
      el("span", { text: "by " + (p.source || "?") }),
      el("span", { text: p.d }),
      el("span", { text: p.confidence || "" }),
    ]));
    host.appendChild(el("div", { class: "cd-body", text: p.content }));
    if (p.tags && p.tags.length) {
      host.appendChild(el("div", { class: "cd-meta" }, p.tags.slice(0, 6).map((t) => el("span", { text: "#" + t }))));
    }
    if (rel.length) {
      host.appendChild(el("div", { class: "sub-h", style: { margin: "6px 0 2px" }, text: "Most similar" }));
      const rl = el("div", { class: "cd-rel" });
      for (const r of rel) {
        rl.appendChild(el("a", {
          href: "#",
          onclick: (e) => { e.preventDefault(); cons.selId = r.o.id; renderConsDetail(); consDrawIfStill(); },
        }, [
          el("span", { class: "rdot", style: { background: cons.colors[r.o.scope] || "var(--accent-2)" } }),
          truncate(r.o.content, 46),
          el("span", { class: "sim num", text: Math.round(r.w * 100) + "%" }),
        ]));
      }
      host.appendChild(rl);
    }
  }

  // ═════════════════════ tasks ═════════════════════
  let taskFilter = "all";
  LOADERS.tasks = async function () {
    const res = await api("/api/tasks?limit=200");
    const tasks = res.tasks || [];
    $("tasks-count").textContent = res.count ?? tasks.length;
    const host = $("tasks-body"); empty(host);
    const groups = [
      ["open", "In flight", "running"],
      ["completed", "Recently completed", "done"],
      ["failed", "Failed", "failed"],
    ];
    let any = false;
    for (const [bucket, label, stateName] of groups) {
      if (taskFilter !== "all" && taskFilter !== bucket) continue;
      const items = tasks.filter((t) => t.status_bucket === bucket).slice(0, bucket === "open" ? 20 : 10);
      if (!items.length) continue;
      any = true;
      host.appendChild(el("div", { class: "sub-h", text: label + " · " + items.length }));
      for (const t of items) {
        host.appendChild(el("div", {
          class: "task-row clickable", "data-status": bucket === "open" ? "running" : bucket === "failed" ? "failed" : "done",
          role: "button", tabindex: "0",
          onclick: () => openToasterByTask(t.task_id),
          onkeydown: (e) => { if (e.key === "Enter") openToasterByTask(t.task_id); },
        }, [
          el("span", { class: "stripe" }),
          el("div", { class: "tt" }, [
            truncate((t.title || t.goal || "").replace(/\s+/g, " "), 90),
            el("small", { text: (t.event_count || 0) + " events · " + (t.scope || "") }),
          ]),
          el("div", { class: "tchain" }, (t.agents || []).slice(0, 3).map(agChip)),
          el("div", { class: "tmeta num", text: fmtRel(Date.parse(t.last_event_at || t.created_at) || null) + "\ntask " + String(t.task_id).slice(0, 8) }),
          el("div", { class: "tstate", text: stateName }),
        ]));
      }
    }
    if (!any) note(host, "no tasks match this filter");
  };
  document.querySelectorAll("#task-filters button").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll("#task-filters button").forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      taskFilter = b.dataset.tf;
      LOADERS.tasks();
    }));

  // ═════════════════════ agents ═════════════════════
  let adAgent = null, adTab = "events", agentsCache = [];
  LOADERS.agents = async function () {
    const [res, chainsRes] = await Promise.all([api("/api/agents"), api("/api/handoffs/chains?limit=50")]);
    agentsCache = res.agents || [];
    const grid = $("agents-grid"); empty(grid);
    const now = Date.now();
    for (const a of agentsCache) {
      const id = a.agent_id || a.id;
      const last = Date.parse(a.last_active_at) || 0;
      const live = a.recently_active || now - last < 5 * 60e3;
      const dormant = !last || now - last > 7 * 86400e3;
      const color = agentColor(id);
      const hourly = a.hourly || [];
      const maxH = Math.max(...hourly, 1);
      const caps = [];
      if (a.memory_writes) caps.push(a.memory_writes + " writes");
      if (a.tool_invocations) caps.push(a.tool_invocations + " tools");
      if (a.chat_queries) caps.push(a.chat_queries + " chats");
      caps.push(a.kind || "agent");
      grid.appendChild(el("div", {
        class: "agent-big" + (live ? " is-live-h" : ""), "data-agent": id,
        tabindex: "0", role: "button", "aria-label": "inspect " + id,
        onclick: () => openAgent(id),
        onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openAgent(id); } },
      }, [
        el("div", { class: "top" }, [
          el("span", { class: "avatar", style: { background: color }, text: agentAbbr(id) }),
          el("div", {}, [
            el("div", { class: "nm", text: id }),
            el("div", { class: "role", text: (a.kind || "agent") + " · first seen " + fmtRel(Date.parse(a.first_seen_at) || null) }),
          ]),
          el("span", { class: "stat-pill " + (live ? "live" : dormant ? "dormant" : "idle"), text: live ? "● live" : dormant ? "dormant" : "idle " + fmtRel(last) }),
        ]),
        el("div", { class: "actbar", "aria-hidden": "true" },
          hourly.slice(-14).map((h) => el("i", {
            class: h === maxH && h > 0 ? "hot" : null,
            style: { height: Math.max(4, (h / maxH) * 100) + "%" },
          }))),
        el("div", { class: "nums" }, [
          el("div", {}, [el("div", { class: "k", text: "events" }), el("div", { class: "v num", text: (a.total_events || 0).toLocaleString() })]),
          el("div", {}, [el("div", { class: "k", text: "memories" }), el("div", { class: "v num", text: String(a.memory_writes || 0) })]),
          el("div", {}, [el("div", { class: "k", text: "dispatches" }), el("div", { class: "v num", text: String(a.dispatches || 0) })]),
        ]),
        el("div", { class: "caps" }, caps.slice(0, 4).map((c) => el("span", { class: "cap", text: c }))),
        el("div", { class: "last", text: "last: " + fmtRel(last) }),
      ]));
    }
    // dispatch routes from chains
    const pair = new Map();
    for (const c of chainsRes.items || []) {
      const p = c.agents_path || [];
      for (let i = 0; i + 1 < p.length; i++) {
        const k = p[i] + "→" + p[i + 1];
        pair.set(k, (pair.get(k) || 0) + 1);
      }
    }
    const rl = $("routes-list");
    if (!pair.size) note(rl, "no multi-agent dispatches yet — routes appear when agents pass parent_task_id");
    else {
      empty(rl);
      [...pair.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6).forEach(([k, n]) => {
        const [from, to] = k.split("→");
        rl.appendChild(el("div", { class: "route" }, [
          el("span", { class: "rl" }, [agChip(from)]),
          el("div", { class: "chain-lane" }, [el("div", { class: "chain-link" })]),
          agChip(to),
          el("span", { class: "rcount num", text: n + (n === 1 ? " task" : " tasks") }),
        ]));
      });
    }
  };
  function openAgent(id) {
    adAgent = id;
    document.querySelectorAll(".agent-big").forEach((c) => c.classList.toggle("sel", c.dataset.agent === id));
    $("agent-detail").hidden = false;
    renderAD();
    $("agent-detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  $("ad-close").addEventListener("click", () => {
    adAgent = null;
    $("agent-detail").hidden = true;
    document.querySelectorAll(".agent-big").forEach((c) => c.classList.remove("sel"));
  });
  document.querySelectorAll("[data-adtab]").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll("[data-adtab]").forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      adTab = b.dataset.adtab;
      if (adAgent) renderAD();
    }));
  async function renderAD() {
    const a = agentsCache.find((x) => (x.agent_id || x.id) === adAgent) || {};
    const head = $("ad-head"); empty(head);
    head.appendChild(el("span", { class: "avatar", style: { background: agentColor(adAgent), width: "30px", height: "30px", borderRadius: "9px", display: "grid", placeItems: "center", color: "#fff", fontFamily: "var(--mono)", fontSize: "11px", fontWeight: "700" }, text: agentAbbr(adAgent) }));
    head.appendChild(el("div", {}, [
      el("h2", { style: { margin: "0", fontFamily: "var(--disp)", fontSize: "14px", letterSpacing: ".07em", textTransform: "uppercase" }, text: adAgent }),
      el("div", { class: "sub", text: (a.total_events || 0) + " events · " + (a.memory_writes || 0) + " memories · " + (a.dispatches || 0) + " dispatches" }),
    ]));
    const body = $("ad-body");
    note(body, "loading…");
    try {
      const kindQ = adTab === "memories" ? "&kind=memory.written" : "";
      const hist = await api("/api/agents/" + encodeURIComponent(adAgent) + "/history?limit=200" + kindQ);
      let items = hist.items || [];
      if (adTab === "dispatches") {
        items = items.filter((e) => /^(task\.|handoff\.)/.test(e.kind || "") && !/heartbeat/.test(e.kind));
      }
      items = items.slice(0, 25);
      empty(body);
      if (!items.length) return note(body, "nothing here yet");
      if (adTab === "memories") {
        for (const ev of items) {
          const preview = ev.payload_preview || "";
          body.appendChild(el("div", {
            class: "mem-row", role: "button", tabindex: "0",
            onclick: () => jumpToMemory(preview),
          }, [
            el("span", { class: "mg", style: { color: "var(--accent-2)" }, text: "●" }),
            el("span", { class: "ml" }, [
              truncate(preview || "memory written", 90) + " ",
              el("span", { class: "goto", text: "view in constellation ↗" }),
            ]),
            el("span", { class: "msc" }, [el("span", { class: "cdot", style: { background: agentColor(adAgent) } }), "memory"]),
            el("span", { class: "md num", text: fmtRel(ev.timestamp_ms) }),
          ]));
        }
      } else {
        const feed = el("div", { class: "feed" });
        for (const ev of items) feed.appendChild(feedRowEl(ev));
        body.appendChild(feed);
      }
    } catch (e) {
      note(body, "history unavailable: " + e.message);
    }
  }
  async function jumpToMemory(preview) {
    const pts = await consData();
    const needle = String(preview).slice(0, 40).toLowerCase();
    const hit = pts.find((p) => (p.content || "").toLowerCase().startsWith(needle));
    showPage("constellation");
    if (hit) { cons.selId = hit.id; setTimeout(() => { renderConsDetail(); consDrawIfStill(); }, 150); }
  }

  // ═════════════════════ conversations ═════════════════════
  let convoSel = null;
  LOADERS.conversations = async function () {
    const status = $("convo-status").value || "*";
    const res = await api("/api/conversations?status=" + encodeURIComponent(status));
    const items = res.items || [];
    $("convo-count").textContent = items.length;
    const list = $("convo-list");
    if (!items.length) note(list, "no conversations yet — start one with ＋ New");
    else {
      empty(list);
      for (const c of items) {
        const item = el("div", {
          class: "convo-item" + (convoSel === c.id ? " sel" : ""),
          role: "button", tabindex: "0",
          onclick: () => selectConvo(c.id),
          onkeydown: (e) => { if (e.key === "Enter") selectConvo(c.id); },
        }, [
          el("div", { class: "ct" }, [
            truncate(c.topic || "untitled", 44) + " ",
            el("span", { class: "state " + (c.status === "open" ? "open" : "closed"), text: c.status }),
          ]),
          el("div", { class: "cm" }, [
            el("span", { class: "participants" }, (c.participants || []).slice(0, 4).map((p) =>
              el("span", { class: "pdot", style: { background: agentColor(p) }, title: p }))),
            el("span", { text: (c.turn_count || 0) + " turns" }),
            el("span", { text: "·" }),
            el("span", { class: "num", text: fmtRel(Date.parse(c.last_activity_at) || null) }),
          ]),
        ]);
        list.appendChild(item);
      }
    }
    if (convoSel) selectConvo(convoSel, true);
  };
  $("convo-status").addEventListener("change", () => LOADERS.conversations());
  async function selectConvo(id, keep) {
    convoSel = id;
    composeMode(false);
    if (!keep) document.querySelectorAll(".convo-item").forEach((x) => x.classList.remove("sel"));
    try {
      const snap = await api("/api/conversations/" + encodeURIComponent(id));
      $("convo-title").textContent = truncate(snap.topic || "conversation", 60);
      const acts = $("convo-actions"); empty(acts);
      if (snap.status === "open") {
        acts.appendChild(el("button", {
          class: "mini-btn", type: "button", text: "▶ run round",
          onclick: async (e) => {
            e.target.textContent = "running…"; e.target.disabled = true;
            try { await apiPost("/api/conversations/" + id + "/run", { rounds: 1 }); } catch (_) { /* shown by refresh */ }
            e.target.disabled = false; e.target.textContent = "▶ run round";
            selectConvo(id, true);
          },
        }));
        acts.appendChild(el("button", {
          class: "mini-btn danger", type: "button", text: "close",
          onclick: async () => { try { await apiPost("/api/conversations/" + id + "/close"); } catch (_) {} LOADERS.conversations(); },
        }));
      }
      const th = $("convo-thread"); empty(th); th.hidden = false;
      const turns = snap.turns || [];
      if (!turns.length) note(th, "no turns yet — ▶ run round asks the agents to speak");
      let lastDay = "";
      for (const t of turns) {
        const day = new Date(t.timestamp_ms || 0).toDateString();
        if (day !== lastDay) {
          th.appendChild(el("div", { class: "turn-divider", text: fmtRel(t.timestamp_ms) }));
          lastDay = day;
        }
        const isOp = (t.from_agent || "") === "operator";
        th.appendChild(el("div", { class: "msg" + (isOp ? " right" : "") }, [
          el("span", { class: "mavatar", style: { background: agentColor(t.from_agent) }, text: agentAbbr(t.from_agent) }),
          el("div", {}, [
            el("div", { class: "mh" }, [el("b", { text: t.from_agent || "?" }), " → " + (t.to_agent || "room")]),
            el("div", { class: "bubble", text: t.content || "" }),
          ]),
        ]));
      }
    } catch (e) {
      note($("convo-thread"), "conversation unavailable: " + e.message);
    }
  }
  function composeMode(on) {
    $("convo-compose").hidden = !on;
    $("convo-thread").hidden = on;
    if (on) {
      $("convo-title").textContent = "New conversation";
      empty($("convo-actions"));
      document.querySelectorAll(".convo-item").forEach((x) => x.classList.remove("sel"));
      buildAgentPicks();
      $("compose-topic").focus();
    }
  }
  function buildAgentPicks() {
    const host = $("agent-picks"); empty(host);
    const bridgeish = agentsCache.length
      ? agentsCache.filter((a) => ["bridge", "external"].includes(a.kind) || AGENT_TOKEN[a.agent_id || a.id]).slice(0, 6)
      : [{ agent_id: "hermes" }, { agent_id: "codex" }, { agent_id: "claude_code" }];
    const seen = new Set();
    for (const a of bridgeish) {
      const id = a.agent_id || a.id;
      if (seen.has(id) || id === "operator" || id === "exocortex" || id === "memory_chat") continue;
      seen.add(id);
      const pick = el("button", { type: "button", class: "agent-pick" + (["hermes", "codex"].includes(id) ? " on" : ""), "data-pick": id }, [
        el("span", { class: "avatar", style: { background: agentColor(id) }, text: agentAbbr(id) }),
        el("span", {}, [
          el("span", { class: "pn", text: id }), el("br"),
          el("span", { class: "pr", text: (a.kind || "agent") + " · " + (a.total_events || 0) + " events" }),
        ]),
        el("span", { class: "check", text: "✓" }),
      ]);
      pick.addEventListener("click", () => {
        pick.classList.toggle("on");
        const n = document.querySelectorAll("#agent-picks .agent-pick.on").length;
        $("compose-hint").textContent = n + " agent" + (n === 1 ? "" : "s") + " selected · every turn is written to shared memory + audit log";
      });
      host.appendChild(pick);
    }
  }
  $("convo-new").addEventListener("click", async () => {
    if (!agentsCache.length) { try { agentsCache = (await api("/api/agents")).agents || []; } catch (_) {} }
    composeMode(true);
  });
  $("compose-cancel").addEventListener("click", () => {
    composeMode(false);
    if (convoSel) selectConvo(convoSel);
    else note($("convo-thread"), "select a conversation");
  });
  $("compose-start").addEventListener("click", async () => {
    const topic = $("compose-topic").value.trim();
    const msg = $("compose-msg").value.trim();
    const picks = [...document.querySelectorAll("#agent-picks .agent-pick.on")].map((b) => b.dataset.pick);
    if (!topic) { $("compose-hint").textContent = "give it a topic first"; return; }
    if (!picks.length) { $("compose-hint").textContent = "pick at least one agent"; return; }
    $("compose-hint").textContent = "starting…";
    try {
      const convo = await apiPost("/api/conversations", { topic, participants: picks });
      if (msg) {
        await apiPost("/api/conversations/" + convo.id + "/turn", {
          from_agent: "operator", to_agent: picks.join(", "), content: msg,
        });
      }
      $("compose-topic").value = ""; $("compose-msg").value = "";
      convoSel = convo.id;
      await LOADERS.conversations();
    } catch (e) {
      $("compose-hint").textContent = "failed: " + e.message;
    }
  });

  // ═════════════════════ chat ═════════════════════
  LOADERS.chat = async function () { memchatRefresh(); };
  function chatBubble(who, content, extras) {
    const isOp = who === "operator";
    return el("div", { class: "msg" + (isOp ? " right" : "") }, [
      el("span", { class: "mavatar", style: { background: isOp ? agentColor("operator") : "color-mix(in srgb, var(--accent) 70%, #444)" }, text: isOp ? "op" : "mc" }),
      el("div", {}, [
        el("div", { class: "mh" }, [el("b", { text: who })]),
        el("div", { class: "bubble" }, [el("span", { text: content }), ...(extras || [])]),
      ]),
    ]);
  }
  async function chatAsk(q) {
    const th = $("chat-thread");
    if (th.querySelector(".empty-note")) empty(th);
    th.appendChild(chatBubble("operator", q));
    const pending = chatBubble("memory_chat", "thinking ");
    pending.querySelector(".bubble").appendChild(el("span", { class: "spin", text: "◌" }));
    th.appendChild(pending);
    th.scrollTop = th.scrollHeight;
    try {
      const res = await apiPost("/api/memory/chat", { question: q });
      pending.remove();
      const cites = [];
      const pts = await consData().catch(() => []);
      for (const rid of (res.cited_record_ids || []).slice(0, 5)) {
        const p = pts.find((x) => x.id === rid);
        cites.push(el("span", {
          class: "cite",
          onclick: () => { showPage("constellation"); cons.selId = rid; setTimeout(() => { renderConsDetail(); consDrawIfStill(); }, 150); },
        }, [
          el("span", { class: "cdot", style: { background: p ? cssVar(SCOPE_TOKEN[p.scope] || "--cl-exocortex", "#8A66EC") : "var(--accent-2)" } }),
          el("span", { text: p ? truncate(p.content, 34) : truncate(rid, 8) }),
        ]));
      }
      const extras = [];
      if (cites.length) extras.push(el("div", { class: "cite-row" }, cites));
      extras.push(el("div", { class: "chat-footnote", text: "cited " + (res.cited_record_ids || []).length + " records · " + (res.model || "local llm") + " · " + (res.latency_ms || "—") + "ms" }));
      th.appendChild(chatBubble("memory_chat", res.answer || "(no answer)", extras));
    } catch (e) {
      pending.remove();
      th.appendChild(chatBubble("memory_chat", "unavailable — " + e.message + " (is Ollama running and the toggle on?)"));
    }
    th.scrollTop = th.scrollHeight;
  }
  $("chat-send").addEventListener("click", () => {
    const q = $("chat-input").value.trim();
    if (!q) return;
    $("chat-input").value = "";
    chatAsk(q);
  });
  $("chat-input").addEventListener("keydown", (e) => { if (e.key === "Enter") $("chat-send").click(); });
  document.querySelectorAll("#chat-suggests .suggest").forEach((b) =>
    b.addEventListener("click", () => chatAsk(b.textContent)));

  // ═════════════════════ profile ═════════════════════
  let profCache = null, profSel = null;
  LOADERS.profile = async function () {
    freezeRefresh();
    const [prof, qs] = await Promise.all([api("/api/profile"), api("/api/profile/questions")]);
    profCache = prof;
    const sections = prof.sections || [];
    const total = sections.reduce((s, x) => s + (x.count || 0), 0);
    $("prof-count").textContent = total + " records";
    const dims = $("prof-dims");
    if (!sections.length) note(dims, "nothing yet — agents write profile observations as they work with you");
    else {
      empty(dims);
      const maxC = Math.max(...sections.map((s) => s.count || 0), 1);
      for (const s of sections) {
        const name = String(s.type || "").replace("profile.", "");
        dims.appendChild(el("div", {
          class: "dim-row clickable", role: "button", tabindex: "0",
          onclick: () => { profSel = s.type; renderProfItems(); },
        }, [
          el("span", { class: "dn", text: name }),
          el("span", { class: "dbar" }, [el("i", { style: { width: ((s.count / maxC) * 100) + "%" } })]),
          el("span", { class: "dv num", text: String(s.count) }),
          el("span", { class: "dm2", text: "records" }),
        ]));
      }
    }
    renderProfItems();
    // questions
    const items = qs.items || [];
    $("pq-count").textContent = items.length;
    const pl = $("pq-list");
    if (!items.length) note(pl, "none open — seed questions to teach the mind faster");
    else {
      empty(pl);
      for (const q of items) {
        const ta = el("textarea", { placeholder: "answer in your own words…" });
        pl.appendChild(el("div", { class: "q-card" }, [
          el("div", { class: "qq", text: q.question || q.content || "" }),
          el("div", { class: "qwhy", text: (q.dimension ? "grows " + q.dimension + " · " : "") + "asked " + fmtRel(Date.parse(q.created_at) || null) }),
          ta,
          el("div", { class: "qact" }, [
            el("button", {
              class: "btn-primary", type: "button", text: "Save answer",
              onclick: async (e) => {
                const ans = ta.value.trim();
                if (!ans) return;
                e.target.textContent = "saving…";
                try { await apiPost("/api/profile/answer", { question_id: q.id, answer: ans }); LOADERS.profile(); }
                catch (err) { e.target.textContent = "failed: " + truncate(err.message, 30); }
              },
            }),
          ]),
        ]));
      }
    }
    // recent observations across sections
    const all = sections.flatMap((s) => (s.items || []).map((it) => ({ ...it, _sec: s.type })));
    all.sort((a, b) => String(b.timestamp || "").localeCompare(String(a.timestamp || "")));
    const pr = $("prof-recent");
    if (!all.length) note(pr, "—");
    else {
      empty(pr);
      for (const it of all.slice(0, 8)) {
        pr.appendChild(el("div", { class: "obs-row" }, [
          el("span", { class: "odot", style: { background: agentColor(it.source) } }),
          el("span", { class: "ot", text: truncate(it.content, 90) }),
          el("span", { class: "odim", text: String(it._sec || "").replace("profile.", "") }),
          el("span", { class: "od num", text: fmtRel(Date.parse(it.timestamp) || null) }),
        ]));
      }
    }
  };
  function renderProfItems() {
    const host = $("prof-items");
    if (!profCache || !profSel) { note(host, "select a dimension above"); return; }
    const sec = (profCache.sections || []).find((s) => s.type === profSel);
    $("prof-sel-title").textContent = String(profSel).replace("profile.", "") + " records";
    if (!sec || !(sec.items || []).length) return note(host, "empty");
    empty(host);
    for (const it of sec.items) {
      host.appendChild(el("div", { class: "pi-row" }, [
        el("span", { class: "pit", text: it.content }),
        el("span", { class: "pim", text: (it.source || "") + " · " + (it.confidence || "") }),
        el("button", {
          class: "mini-btn danger", type: "button", text: "redact",
          onclick: async () => {
            try { await apiPost("/api/profile/redact", { record_id: it.id }); LOADERS.profile(); }
            catch (_) { /* keep row */ }
          },
        }),
      ]));
    }
  }
  $("pq-seed").addEventListener("click", async () => {
    try { await apiPost("/api/profile/seed_questions"); LOADERS.profile(); } catch (_) { /* ok */ }
  });

  // ═════════════════════ reflect ═════════════════════
  LOADERS.reflect = async function () {
    const [pending, all] = await Promise.all([
      api("/api/insights"), api("/api/insights?include_resolved=true"),
    ]);
    const pItems = pending.items || [];
    const resolved = (all.items || []).filter((i) => (i.status || "proposed") !== "proposed");
    $("ins-pending").textContent = pItems.length;
    $("ins-resolved").textContent = resolved.length;
    $("insight-count").textContent = pItems.length;
    const list = $("insight-list");
    if (!pItems.length) note(list, "no pending insights — agents propose them via insight_propose; reflection runs add more");
    else {
      empty(list);
      for (const ins of pItems) {
        const text = ins.text || ins.claim || ins.insight || ins.content || JSON.stringify(ins);
        const conf = typeof ins.confidence === "number" ? ins.confidence : null;
        const card = el("div", { class: "insight-card" }, [
          el("div", { class: "it", text: truncate(text, 160) }),
          conf != null ? el("div", { class: "iconf" }, [
            el("span", { class: "cbar" }, [el("i", { style: { width: (conf * 100) + "%" } })]),
            el("span", { class: "num", text: "confidence " + conf.toFixed(2) }),
          ]) : null,
          el("div", { class: "iact" }, [
            el("button", {
              class: "btn-primary", type: "button", text: "Promote",
              onclick: async () => { try { await apiPost("/api/insights/" + (ins.insight_id || ins.id) + "/accept"); } catch (_) {} LOADERS.reflect(); },
            }),
            el("button", {
              class: "btn-ghost", type: "button", text: "Dismiss",
              onclick: async () => { try { await apiPost("/api/insights/" + (ins.insight_id || ins.id) + "/dismiss"); } catch (_) {} LOADERS.reflect(); },
            }),
          ]),
        ]);
        list.appendChild(card);
      }
    }
    const hist = $("insight-history");
    if (!resolved.length) note(hist, "nothing resolved yet");
    else {
      empty(hist);
      for (const ins of resolved.slice(0, 10)) {
        const text = ins.text || ins.claim || ins.insight || ins.content || "";
        hist.appendChild(el("div", { class: "promo-row" }, [
          el("span", { class: "pd num", text: ins.status || "" }),
          el("span", { class: "pt", text: truncate(text, 90) }),
          el("span", { class: "pto", text: "→ " + (ins.status === "accepted" ? "memory" : "dismissed") }),
        ]));
      }
    }
  };

  // ═════════════════════ settings + debug ═════════════════════
  LOADERS.settings = async function () {
    memchatRefresh(); freezeRefresh();
    try {
      const st = await api("/api/status");
      $("sys-server").textContent = (location.host || "127.0.0.1:8756") + " · loopback only";
      $("sys-records").textContent = (st.memory_records ?? 0).toLocaleString();
      $("sys-events").textContent = (st.events_total ?? 0).toLocaleString();
      $("sys-bridges").textContent = st.bridges_registered ?? "—";
      const act = st.agents_active_last_hour || [];
      $("sys-active").textContent = act.length ? act.join(", ") : "none";
      $("sys-ws").textContent = st.ws_subscribers ?? "—";
    } catch (e) {
      $("sys-server").textContent = "status endpoint unreachable";
    }
  };

  LOADERS.debug = async function () {
    const [st, fails, activity] = await Promise.all([
      api("/api/status"), api("/api/debug/failures?limit=30"), api("/api/activity?limit=20"),
    ]);
    $("dbg-server").textContent = location.host || "—";
    $("dbg-ws").textContent = st.ws_subscribers ?? "—";
    $("dbg-mem").textContent = (st.memory_records ?? 0).toLocaleString();
    $("dbg-audit").textContent = (st.events_total ?? 0).toLocaleString();
    const items = fails.items || [];
    $("fail-count").textContent = items.length;
    const fl = $("fail-list");
    if (!items.length) note(fl, "none — clean run");
    else {
      empty(fl);
      for (const f of items.slice(0, 12)) {
        const sev = /failed/.test(f.kind || "") ? "danger" : /rejected|denied/.test(f.kind || "") ? "warn" : "info";
        const row = el("div", { class: "attn-item " + sev + " fail-row", role: "button", tabindex: "0" }, [
          el("span", { class: "stripe" }),
          el("div", { class: "body" }, [
            el("div", { class: "t", text: (f.kind || "failure") + (f.agent_id ? " — " + f.agent_id : "") }),
            el("div", { class: "d", text: truncate(f.payload_preview || "", 140) }),
          ]),
          el("span", { class: "age num", text: fmtRel(f.timestamp_ms) }),
        ]);
        row.addEventListener("click", async () => {
          const existing = row.nextElementSibling;
          if (existing && existing.classList.contains("fail-ctx")) { existing.remove(); return; }
          try {
            const ctx = await api("/api/debug/failures/" + f.event_id + "/context");
            const pre = el("div", { class: "fail-ctx", text: JSON.stringify(ctx, null, 2).slice(0, 2000) });
            row.after(pre);
          } catch (_) { /* no context */ }
        });
        fl.appendChild(row);
      }
    }
    // kinds breakdown
    const kinds = {};
    for (const f of items) kinds[f.kind || "?"] = (kinds[f.kind || "?"] || 0) + 1;
    const fk = $("fail-kinds");
    const ks = Object.entries(kinds).sort((a, b) => b[1] - a[1]);
    if (!ks.length) note(fk, "—");
    else {
      empty(fk);
      const maxK = ks[0][1];
      for (const [k, n] of ks.slice(0, 6)) {
        fk.appendChild(el("div", { class: "trow" }, [
          el("span", { class: "tname", text: k }),
          el("span", { class: "bar" }, [el("i", { style: { width: ((n / maxK) * 100) + "%", background: "var(--danger)" } })]),
          el("span", { class: "tv num", text: String(n) }),
        ]));
      }
    }
    // tail
    const tail = $("log-tail"); empty(tail);
    for (const ev of (activity.items || []).slice().reverse()) tail.appendChild(tailLineEl(ev));
    tail.appendChild(el("div", { class: "ll log-caret-line" }, [el("span", { class: "log-caret" })]));
  };

  // ═════════════════════ boot ═════════════════════
  connectWs();
  railStats();
  memchatRefresh();
  showPage(PATH_VIEW[location.pathname] || "dashboard", false);
})();
