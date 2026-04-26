// conversations.js — /static/conversations.html
//
// Two-column layout. Left: list of conversations grouped by status + a
// "+ new" button. Right: transcript of the selected conversation in
// chat-bubble form, plus a composer with "run rounds" and "inject as
// operator" affordances.
//
// All endpoints (GET/POST /api/conversations*, GET /api/agents) can 404/503;
// we degrade to empty / "endpoint not ready" copy. We never crash. The
// WebSocket at /api/events drives live updates; reconnection uses 1s -> 8s
// exponential backoff matching the rest of the app.

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
  const OPERATOR_COLOR = "#8b9bab";

  const LS_KEY = "exocortex.conversations.v1";
  const WS_DEBOUNCE_MS = 400;
  const MAX_TRANSCRIPT_TURNS = 500;

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
        } else if (k.startsWith("on")) {
          node.addEventListener(k.slice(2), attrs[k]);
        } else {
          node.setAttribute(k, attrs[k]);
        }
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

  function agentColor(id) {
    if (!id) return FALLBACK_AGENT_COLOR;
    if (id === "operator") return OPERATOR_COLOR;
    return AGENT_COLORS[id] || FALLBACK_AGENT_COLOR;
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

  function toMs(v) {
    if (v == null) return 0;
    if (typeof v === "number") return v;
    const n = Date.parse(v);
    return isNaN(n) ? 0 : n;
  }

  // -------------------------------------------------------------------
  // Persistence
  // -------------------------------------------------------------------

  function loadPrefs() {
    try {
      const raw = localStorage.getItem(LS_KEY);
      if (!raw) return {};
      const parsed = JSON.parse(raw);
      return (parsed && typeof parsed === "object") ? parsed : {};
    } catch (_) { return {}; }
  }
  function savePrefs(patch) {
    try {
      const cur = loadPrefs();
      localStorage.setItem(LS_KEY, JSON.stringify({ ...cur, ...patch }));
    } catch (_) { /* ignore */ }
  }

  // -------------------------------------------------------------------
  // State
  // -------------------------------------------------------------------

  const state = {
    listAvailable: true,
    list: [],                  // [{id, topic, status, participants, turn_count, started_at, last_activity_at, last_turn_preview}]
    selectedId: null,
    detail: null,              // full detail of selected conversation
    detailAvailable: true,
    agents: [],                // for new-conversation form + to-agent dropdown
    agentsAvailable: true,
    composerToAgent: null,
    runDefault: 1,
    runInFlight: false,
    autoStick: true,
    pendingNew: 0,
    newFormOpen: false,
    newFormSelected: new Set(),
    closedExpanded: false,
    // WS
    ws: null,
    wsBackoff: 1000,
    wsDebounceTimer: null,
    pendingListRefresh: false,
  };

  // -------------------------------------------------------------------
  // Fetchers
  // -------------------------------------------------------------------

  async function fetchList() {
    try {
      const r = await fetch("/api/conversations?limit=50&status=*");
      if (!r.ok) {
        if (r.status === 404 || r.status === 503) state.listAvailable = false;
        renderSidebar();
        return;
      }
      const data = await r.json();
      state.listAvailable = true;
      const items = Array.isArray(data.items) ? data.items : [];
      state.list = items.map(normalizeConv);
      updateKpis();
      renderSidebar();
    } catch (_) {
      state.listAvailable = false;
      renderSidebar();
    }
  }

  function normalizeConv(c) {
    return {
      id: c.id || "",
      topic: c.topic || "(no topic)",
      status: c.status === "closed" ? "closed" : "open",
      participants: Array.isArray(c.participants) ? c.participants : [],
      turn_count: typeof c.turn_count === "number" ? c.turn_count : 0,
      started_at: toMs(c.started_at),
      last_activity_at: toMs(c.last_activity_at) || toMs(c.started_at),
      last_turn_preview: c.last_turn_preview || "",
    };
  }

  async function fetchDetail(id) {
    if (!id) return;
    try {
      const r = await fetch(`/api/conversations/${encodeURIComponent(id)}`);
      if (!r.ok) {
        if (r.status === 404 || r.status === 503) state.detailAvailable = false;
        state.detail = null;
        renderMain();
        return;
      }
      const data = await r.json();
      state.detailAvailable = true;
      state.detail = normalizeDetail(data);
      renderMain();
      // Land on the bottom of the transcript on first load
      requestAnimationFrame(() => scrollTranscriptToBottom(true));
    } catch (_) {
      state.detailAvailable = false;
      state.detail = null;
      renderMain();
    }
  }

  function normalizeDetail(d) {
    const turns = Array.isArray(d.turns) ? d.turns : [];
    return {
      id: d.id || "",
      topic: d.topic || "(no topic)",
      status: d.status === "closed" ? "closed" : "open",
      participants: Array.isArray(d.participants) ? d.participants : [],
      started_at: toMs(d.started_at),
      last_activity_at: toMs(d.last_activity_at),
      turns: turns.map(normalizeTurn),
    };
  }

  function normalizeTurn(t) {
    return {
      turn_id: t.turn_id || (t.timestamp_ms + ":" + (t.from_agent || "")),
      from_agent: t.from_agent || "",
      to_agent: t.to_agent || "",
      content: t.content || "",
      timestamp_ms: t.timestamp_ms || toMs(t.timestamp) || Date.now(),
      in_reply_to: t.in_reply_to || null,
    };
  }

  async function fetchAgents() {
    try {
      const r = await fetch("/api/agents");
      if (!r.ok) {
        if (r.status === 404 || r.status === 503) state.agentsAvailable = false;
        return;
      }
      const data = await r.json();
      state.agentsAvailable = true;
      const list = Array.isArray(data.items)
        ? data.items
        : (Array.isArray(data.agents) ? data.agents : []);
      state.agents = list
        .map((a) => a.agent_id || a.id || "")
        .filter((s) => s && s !== "operator");
      // If we already have known participants, fold them in too — this keeps
      // the to-agent dropdown working even before /api/agents fills.
    } catch (_) {
      state.agentsAvailable = false;
    }
  }

  // -------------------------------------------------------------------
  // Sidebar render
  // -------------------------------------------------------------------

  function updateKpis() {
    const open = state.list.filter((c) => c.status === "open").length;
    const total = state.list.length;
    const o = $("kpi-conv-open"); if (o) o.textContent = String(open);
    const t = $("kpi-conv-total"); if (t) t.textContent = String(total);
  }

  function renderSidebar() {
    const groups = $("conv-sidebar-groups");
    if (!groups) return;
    groups.innerHTML = "";

    if (!state.listAvailable) {
      groups.appendChild(el("div", {
        class: "conv-sidebar-empty",
        text: "conversations endpoint not ready — will retry",
      }));
      return;
    }

    if (state.list.length === 0) {
      // Empty sidebar; the right pane shows the big empty-state CTA, but we
      // still render a tiny hint here so the column doesn't look broken.
      groups.appendChild(el("div", {
        class: "conv-sidebar-empty",
        text: "no conversations yet",
      }));
      return;
    }

    // Sort: most-recently-active first.
    const sorted = state.list.slice().sort(
      (a, b) => (b.last_activity_at || 0) - (a.last_activity_at || 0)
    );
    const open = sorted.filter((c) => c.status === "open");
    const closed = sorted.filter((c) => c.status === "closed");

    // Open group
    groups.appendChild(buildGroupHeader("open", open.length, false));
    for (const c of open) groups.appendChild(buildConvRow(c));

    // History group (archived/closed conversations) — collapsible.
    if (closed.length > 0) {
      groups.appendChild(buildGroupHeader("history", closed.length, true));
      if (state.closedExpanded) {
        for (const c of closed) groups.appendChild(buildConvRow(c));
      }
    }
  }

  function buildGroupHeader(label, count, collapsible) {
    const dotCls = label === "open" ? "open" : "closed";
    const arrow = collapsible
      ? (state.closedExpanded ? "▼" : "▶")
      : "";
    return el("div", {
      class: "conv-group-h" + (collapsible ? " collapsible" : ""),
      onclick: collapsible ? () => {
        state.closedExpanded = !state.closedExpanded;
        renderSidebar();
      } : null,
    }, [
      el("span", { class: "dot " + dotCls, text: "●" }),
      el("span", { class: "label", text: label }),
      el("span", { class: "count", text: "(" + count + ")" }),
      collapsible ? el("span", { class: "arrow", text: arrow }) : null,
    ]);
  }

  function buildConvRow(c) {
    const cls = "conv-row" + (state.selectedId === c.id ? " active" : "")
      + (c.status === "closed" ? " closed" : "");
    const stripeColor = c.status === "open" ? "var(--accent)" : "var(--muted)";

    const partsLine = c.participants.length
      ? c.participants.join(" ↔ ")
      : "—";
    const statusGlyph = c.status === "open" ? "●" : "─";

    return el("div", {
      class: cls,
      style: { borderLeftColor: stripeColor },
      onclick: () => selectConversation(c.id),
    }, [
      el("div", { class: "row-top" }, [
        el("span", { class: "glyph", text: statusGlyph,
          style: { color: stripeColor === "var(--accent)" ? "var(--accent)" : "var(--muted)" } }),
        el("span", { class: "topic", text: truncate(c.topic, 60) }),
      ]),
      el("div", { class: "row-parts mono", text: partsLine }),
      el("div", { class: "row-meta mono" }, [
        el("span", { text: c.turn_count + " turn" + (c.turn_count === 1 ? "" : "s") }),
        el("span", { text: " · " }),
        el("span", { text: fmtRelative(c.last_activity_at) }),
      ]),
      c.last_turn_preview ? el("div", {
        class: "row-preview",
        text: truncate(c.last_turn_preview, 90),
      }) : null,
    ]);
  }

  // -------------------------------------------------------------------
  // Main pane render
  // -------------------------------------------------------------------

  function renderMain() {
    const bar = $("conv-bar");
    const transcript = $("conv-transcript");
    const empty = $("conv-empty");
    const composer = $("conv-composer");
    const topicEl = $("conv-topic");
    const statusPill = $("conv-status-pill");
    const partsEl = $("conv-participants");
    const closeBtn = $("conv-close-btn");
    if (!bar || !transcript || !empty || !composer) return;

    // Empty pane: no selection.
    if (!state.selectedId) {
      bar.classList.add("muted");
      topicEl.textContent = state.list.length === 0
        ? "no conversations yet"
        : "select a conversation on the left";
      statusPill.hidden = true;
      partsEl.innerHTML = "";
      closeBtn.hidden = true;
      const deleteBtn = $("conv-delete-btn");
      if (deleteBtn) deleteBtn.hidden = true;
      transcript.innerHTML = "";
      composer.hidden = true;
      empty.style.display = "flex";
      const msg = $("conv-empty-msg");
      const cta = $("conv-empty-cta");
      if (state.list.length === 0) {
        if (msg) msg.innerHTML = "no conversations yet — click <strong>+ new</strong> to start one";
        if (cta) cta.style.display = "";
      } else {
        if (msg) msg.textContent = "select a conversation on the left to see its transcript";
        if (cta) cta.style.display = "none";
      }
      return;
    }

    // Selected but detail not yet available
    if (!state.detailAvailable) {
      bar.classList.remove("muted");
      topicEl.textContent = "—";
      statusPill.hidden = true;
      partsEl.innerHTML = "";
      closeBtn.hidden = true;
      const deleteBtn = $("conv-delete-btn");
      if (deleteBtn) deleteBtn.hidden = true;
      transcript.innerHTML = "";
      composer.hidden = true;
      empty.style.display = "flex";
      const msg = $("conv-empty-msg");
      if (msg) msg.textContent = "conversation endpoint not ready — will retry";
      const cta = $("conv-empty-cta"); if (cta) cta.style.display = "none";
      return;
    }

    if (!state.detail) {
      // Loading
      empty.style.display = "flex";
      const msg = $("conv-empty-msg");
      if (msg) msg.textContent = "loading…";
      const cta = $("conv-empty-cta"); if (cta) cta.style.display = "none";
      transcript.innerHTML = "";
      composer.hidden = true;
      return;
    }

    bar.classList.remove("muted");
    topicEl.textContent = state.detail.topic;
    statusPill.hidden = false;
    statusPill.textContent = state.detail.status;
    statusPill.className = "conv-status-pill " + state.detail.status;

    // Participants display
    partsEl.innerHTML = "";
    for (let i = 0; i < state.detail.participants.length; i++) {
      const p = state.detail.participants[i];
      const color = agentColor(p);
      partsEl.appendChild(el("span", { class: "conv-part" }, [
        el("span", { class: "dot", style: { color: color }, text: "●" }),
        el("span", { class: "name mono", text: p }),
      ]));
    }

    closeBtn.hidden = state.detail.status !== "open";
    const deleteBtn = $("conv-delete-btn");
    if (deleteBtn) deleteBtn.hidden = false;  // delete is allowed in any state

    // Composer: only for open conversations.
    composer.hidden = state.detail.status !== "open";

    // Transcript
    transcript.innerHTML = "";
    if (!state.detail.turns.length) {
      empty.style.display = "flex";
      const msg = $("conv-empty-msg");
      if (msg) msg.innerHTML = "no turns yet — click <strong>run 1 round</strong> to fire the first dispatch, or inject as operator below";
      const cta = $("conv-empty-cta"); if (cta) cta.style.display = "none";
    } else {
      empty.style.display = "none";
      for (const t of state.detail.turns) {
        transcript.appendChild(buildTurnBubble(t, state.detail.participants));
      }
    }

    // To-agent dropdown
    populateToAgentDropdown();
    refreshRunButtons();
    updateScrollPill();
  }

  function buildTurnBubble(turn, participants) {
    const isOperator = turn.from_agent === "operator";
    const color = agentColor(turn.from_agent);

    if (isOperator) {
      const wrap = el("div", { class: "conv-bubble operator", "data-turn-id": turn.turn_id }, [
        el("div", { class: "head mono" }, [
          el("span", { class: "arrow", text: "→ " }),
          el("span", { class: "name", text: "operator injection" }),
          turn.to_agent ? el("span", { class: "to", text: " → " + turn.to_agent }) : null,
          el("span", { class: "ts", text: " · " + fmtTimeFromMs(turn.timestamp_ms) }),
        ]),
        el("div", { class: "body", text: turn.content }),
      ]);
      return wrap;
    }

    // Alignment: first-listed participant left, second-listed right, else left
    let side = "left";
    const idx = participants.indexOf(turn.from_agent);
    if (idx >= 0 && participants.length === 2) {
      side = idx === 0 ? "left" : "right";
    } else if (idx >= 0) {
      side = idx % 2 === 0 ? "left" : "right";
    }

    const wrap = el("div", {
      class: "conv-bubble " + side,
      "data-turn-id": turn.turn_id,
      style: {
        // Subtle alpha-tinted background based on agent color, plus a 1px
        // colored border. Inline style is the cleanest path here because
        // the color list is open-ended.
        background: hexWithAlpha(color, 0.08),
        borderColor: color,
      },
    }, [
      el("div", { class: "head mono" }, [
        el("span", { class: "dot", style: { color: color }, text: "●" }),
        el("span", { class: "name", style: { color: color }, text: turn.from_agent || "—" }),
        turn.to_agent ? el("span", { class: "to", text: " → " + turn.to_agent }) : null,
        el("span", { class: "ts", text: " · " + fmtTimeFromMs(turn.timestamp_ms) }),
      ]),
      el("div", { class: "body", text: turn.content }),
    ]);
    return wrap;
  }

  function hexWithAlpha(hex, alpha) {
    // Accept #RRGGBB; return rgba(r,g,b,a). Falls back gracefully.
    const m = /^#([0-9a-f]{6})$/i.exec(hex || "");
    if (!m) return hex;
    const v = m[1];
    const r = parseInt(v.slice(0, 2), 16);
    const g = parseInt(v.slice(2, 4), 16);
    const b = parseInt(v.slice(4, 6), 16);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }

  // -------------------------------------------------------------------
  // To-agent dropdown
  // -------------------------------------------------------------------

  function populateToAgentDropdown() {
    const sel = $("conv-inject-to");
    if (!sel || !state.detail) return;
    const parts = state.detail.participants.slice();
    const prev = state.composerToAgent;
    sel.innerHTML = "";
    for (const p of parts) {
      const opt = document.createElement("option");
      opt.value = p;
      opt.textContent = p;
      sel.appendChild(opt);
    }
    // Preselect: use stored preference if it's still a participant; else
    // pick the participant who was NOT the last speaker.
    let target = null;
    if (prev && parts.indexOf(prev) >= 0) {
      target = prev;
    } else if (state.detail.turns.length > 0) {
      const lastSpeaker = state.detail.turns[state.detail.turns.length - 1].from_agent;
      target = parts.find((p) => p !== lastSpeaker) || parts[0] || null;
    } else {
      target = parts[0] || null;
    }
    if (target) sel.value = target;
    state.composerToAgent = target;
  }

  // -------------------------------------------------------------------
  // Selection
  // -------------------------------------------------------------------

  async function selectConversation(id) {
    if (state.selectedId === id) return;
    state.selectedId = id;
    state.detail = null;
    state.detailAvailable = true;
    state.autoStick = true;
    state.pendingNew = 0;
    savePrefs({ selectedConversationId: id });
    renderSidebar();
    renderMain();
    await fetchDetail(id);
  }

  // -------------------------------------------------------------------
  // Scroll behavior
  // -------------------------------------------------------------------

  function transcriptHost() { return $("conv-transcript"); }

  function isAtBottom() {
    const host = transcriptHost();
    if (!host) return true;
    return host.scrollHeight - (host.scrollTop + host.clientHeight) < 24;
  }

  function scrollTranscriptToBottom(force) {
    const host = transcriptHost();
    if (!host) return;
    host.scrollTop = host.scrollHeight;
    state.autoStick = true;
    state.pendingNew = 0;
    if (!force) updateScrollPill();
    else {
      const pill = $("conv-scroll-pill");
      if (pill) pill.hidden = true;
    }
  }

  function updateScrollPill() {
    const pill = $("conv-scroll-pill");
    const n = $("conv-scroll-pill-n");
    if (!pill || !n) return;
    if (state.pendingNew > 0 && !state.autoStick) {
      pill.hidden = false;
      n.textContent = String(state.pendingNew);
    } else {
      pill.hidden = true;
    }
  }

  function bindScroll() {
    const host = transcriptHost();
    if (host) {
      host.addEventListener("scroll", () => {
        state.autoStick = isAtBottom();
        if (state.autoStick) {
          state.pendingNew = 0;
          updateScrollPill();
        }
      });
    }
    const pill = $("conv-scroll-pill");
    if (pill) pill.addEventListener("click", () => scrollTranscriptToBottom(true));
  }

  // -------------------------------------------------------------------
  // New-conversation form
  // -------------------------------------------------------------------

  function openNewForm() {
    state.newFormOpen = true;
    state.newFormSelected = new Set();
    const form = $("conv-new-form");
    if (form) form.hidden = false;
    const topic = $("conv-new-topic");
    if (topic) { topic.value = ""; topic.focus(); }
    populateNewParticipants();
    showNewWarning(null);
  }
  function closeNewForm() {
    state.newFormOpen = false;
    const form = $("conv-new-form");
    if (form) form.hidden = true;
    showNewWarning(null);
  }

  function populateNewParticipants() {
    const host = $("conv-new-participants");
    if (!host) return;
    host.innerHTML = "";

    // Build the list of selectable agents. If /api/agents wasn't available,
    // offer the canonical four so the form still works.
    let candidates = state.agents.slice();
    if (candidates.length === 0) {
      candidates = ["codex", "hermes", "claude_code", "openclaw"];
    }

    for (const a of candidates) {
      const id = "conv-new-p-" + a;
      const checked = state.newFormSelected.has(a);
      const color = agentColor(a);
      const lab = el("label", {
        class: "conv-new-p" + (checked ? " checked" : ""),
        for: id,
        style: { borderColor: checked ? color : "" },
      }, [
        el("input", {
          type: "checkbox",
          id: id,
          value: a,
          ...(checked ? { checked: "checked" } : {}),
          onchange: (e) => {
            if (e.target.checked) state.newFormSelected.add(a);
            else state.newFormSelected.delete(a);
            populateNewParticipants();
          },
        }),
        el("span", { class: "dot", style: { color: color }, text: "●" }),
        el("span", { class: "name mono", text: a }),
      ]);
      host.appendChild(lab);
    }
  }

  function showNewWarning(msg) {
    const w = $("conv-new-warning");
    if (!w) return;
    if (!msg) { w.hidden = true; w.textContent = ""; return; }
    w.hidden = false;
    w.textContent = msg;
  }

  async function createNewConversation() {
    const topic = ($("conv-new-topic") && $("conv-new-topic").value || "").trim();
    if (!topic) {
      showNewWarning("topic is required");
      return;
    }
    const participants = Array.from(state.newFormSelected);
    if (participants.length < 2) {
      showNewWarning("pick at least 2 participants");
      return;
    }
    showNewWarning(null);
    try {
      const r = await fetch("/api/conversations", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ topic, participants }),
      });
      if (!r.ok) {
        if (r.status === 404 || r.status === 503) {
          showNewWarning("backend not ready (HTTP " + r.status + ") — try again shortly");
        } else if (r.status === 400) {
          let msg = "rejected by backend";
          try { const d = await r.json(); if (d && d.detail) msg = d.detail; } catch (_) {}
          showNewWarning(msg);
        } else {
          showNewWarning("create failed (HTTP " + r.status + ")");
        }
        return;
      }
      const data = await r.json();
      closeNewForm();
      // Optimistically insert into the list so the sidebar updates before WS.
      state.list.unshift({
        id: data.id,
        topic,
        status: "open",
        participants,
        turn_count: 0,
        started_at: toMs(data.started_at) || Date.now(),
        last_activity_at: toMs(data.started_at) || Date.now(),
        last_turn_preview: "",
      });
      updateKpis();
      renderSidebar();
      await selectConversation(data.id);
    } catch (_) {
      showNewWarning("network error contacting /api/conversations");
    }
  }

  // -------------------------------------------------------------------
  // Run rounds / inject / close
  // -------------------------------------------------------------------

  function refreshRunButtons() {
    const go = $("conv-run-go");
    const input = $("conv-run-rounds");
    const status = $("conv-run-status");
    const closed = !state.detail || state.detail.status !== "open";
    const inFlight = state.runInFlight;
    if (go) go.disabled = closed || inFlight;
    if (input) input.disabled = closed || inFlight;
    if (status) {
      if (inFlight) {
        status.hidden = false;
        status.textContent = "running… agents thinking";
      } else {
        status.hidden = true;
        status.textContent = "";
      }
    }
    // Restore last-used round count from prefs.
    if (input && !inFlight && state.runDefault) {
      input.value = String(state.runDefault);
    }
  }

  function showRunWarning(msg) {
    const w = $("conv-run-warning");
    if (!w) return;
    if (!msg) { w.hidden = true; w.textContent = ""; return; }
    w.hidden = false;
    w.textContent = msg;
  }

  async function runRounds(n) {
    if (!state.detail || state.detail.status !== "open") return;
    if (state.runInFlight) return;
    state.runDefault = n;
    savePrefs({ runDefault: n });
    state.runInFlight = true;
    showRunWarning(null);
    refreshRunButtons();
    try {
      const r = await fetch(`/api/conversations/${encodeURIComponent(state.detail.id)}/run`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ rounds: n }),
      });
      if (!r.ok) {
        if (r.status === 404 || r.status === 503) {
          showRunWarning("run endpoint not ready (HTTP " + r.status + ")");
        } else {
          showRunWarning("run failed (HTTP " + r.status + ")");
        }
      }
      // Even on success, we leave the in-flight state until either WS turns
      // arrive or a watchdog fires below.
    } catch (_) {
      showRunWarning("network error contacting /run");
    }
    // Watchdog: clear in-flight after 30s if no WS activity arrives. The
    // backend may settle faster — incoming "conversation.turn" events will
    // also clear it.
    setTimeout(() => {
      if (state.runInFlight) {
        state.runInFlight = false;
        refreshRunButtons();
      }
    }, 30_000);
  }

  async function injectAsOperator(content) {
    if (!state.detail || state.detail.status !== "open") return;
    const v = (content || "").trim();
    if (!v) return;
    const to = state.composerToAgent || (state.detail.participants[0] || "");
    if (!to) {
      showRunWarning("pick a 'to' agent first");
      return;
    }
    const sendBtn = $("conv-inject-send");
    if (sendBtn) sendBtn.disabled = true;
    try {
      const r = await fetch(`/api/conversations/${encodeURIComponent(state.detail.id)}/turn`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ from_agent: "operator", to_agent: to, content: v }),
      });
      if (!r.ok) {
        if (r.status === 404 || r.status === 503) {
          showRunWarning("turn endpoint not ready (HTTP " + r.status + ")");
        } else {
          showRunWarning("send failed (HTTP " + r.status + ")");
        }
        if (sendBtn) sendBtn.disabled = false;
        return;
      }
      const data = await r.json();
      // Optimistic append; the WS event will reconcile.
      const turn = normalizeTurn({
        turn_id: data.turn_id,
        from_agent: "operator",
        to_agent: to,
        content: v,
        timestamp_ms: data.timestamp_ms || Date.now(),
      });
      appendTurnLocal(turn);
      const ta = $("conv-inject-input");
      if (ta) { ta.value = ""; autosizeInjectTextarea(); }
    } catch (_) {
      showRunWarning("network error contacting /turn");
    }
    if (sendBtn) sendBtn.disabled = false;
  }

  async function closeConversation() {
    if (!state.detail || state.detail.status !== "open") return;
    if (!window.confirm("Close this conversation? It will become read-only.")) return;
    try {
      const r = await fetch(`/api/conversations/${encodeURIComponent(state.detail.id)}/close`, {
        method: "POST",
      });
      if (!r.ok) {
        if (r.status === 404 || r.status === 503) {
          showRunWarning("close endpoint not ready (HTTP " + r.status + ")");
        } else {
          showRunWarning("close failed (HTTP " + r.status + ")");
        }
        return;
      }
      // Optimistic
      state.detail.status = "closed";
      const li = state.list.find((c) => c.id === state.detail.id);
      if (li) li.status = "closed";
      updateKpis();
      renderSidebar();
      renderMain();
    } catch (_) {
      showRunWarning("network error contacting /close");
    }
  }

  async function deleteConversation() {
    if (!state.detail) return;
    const cid = state.detail.id;
    const topic = state.detail.topic || "this conversation";
    if (!window.confirm(
      `Delete "${topic}" permanently?\n\n` +
      `The conversation will disappear from the UI. ` +
      `(The audit log entries are preserved — exocortex's audit log is append-only.)`
    )) return;
    try {
      const r = await fetch(`/api/conversations/${encodeURIComponent(cid)}`, {
        method: "DELETE",
      });
      if (!r.ok) {
        if (r.status === 404 || r.status === 503) {
          showRunWarning("delete endpoint not ready (HTTP " + r.status + ")");
        } else {
          showRunWarning("delete failed (HTTP " + r.status + ")");
        }
        return;
      }
      // Optimistic: remove from list, clear selection.
      state.list = state.list.filter((c) => c.id !== cid);
      if (state.selectedId === cid) {
        state.selectedId = null;
        state.detail = null;
        savePrefs({ selectedConversationId: null });
      }
      updateKpis();
      renderSidebar();
      renderMain();
    } catch (_) {
      showRunWarning("network error contacting /delete");
    }
  }

  // -------------------------------------------------------------------
  // Live WS append
  // -------------------------------------------------------------------

  function appendTurnLocal(turn) {
    if (!state.detail) return;
    // Dedup by turn_id
    if (state.detail.turns.some((t) => t.turn_id === turn.turn_id)) return;
    state.detail.turns.push(turn);
    if (state.detail.turns.length > MAX_TRANSCRIPT_TURNS) {
      state.detail.turns.shift();
    }
    state.detail.last_activity_at = turn.timestamp_ms;
    const host = transcriptHost();
    const empty = $("conv-empty");
    if (host) {
      // Hide empty if showing
      if (empty) empty.style.display = "none";
      const bubble = buildTurnBubble(turn, state.detail.participants);
      host.appendChild(bubble);
      if (state.autoStick) {
        scrollTranscriptToBottom(false);
      } else {
        state.pendingNew += 1;
        updateScrollPill();
      }
    }
    // Run is no longer obviously in-flight when an agent turn lands
    if (turn.from_agent && turn.from_agent !== "operator" && state.runInFlight) {
      // Heuristic: clear after a short tick so multi-round bursts don't
      // re-enable mid-flight.
      setTimeout(() => {
        state.runInFlight = false;
        refreshRunButtons();
      }, 600);
    }
  }

  function scheduleListRefresh() {
    if (state.pendingListRefresh) return;
    state.pendingListRefresh = true;
    if (state.wsDebounceTimer) clearTimeout(state.wsDebounceTimer);
    state.wsDebounceTimer = setTimeout(() => {
      state.pendingListRefresh = false;
      fetchList();
    }, WS_DEBOUNCE_MS);
  }

  function handleWsEvent(ev) {
    if (!ev || typeof ev !== "object") return;
    const kind = ev.kind || "";
    if (kind === "__hello__") return;

    if (kind === "conversation.opened") {
      scheduleListRefresh();
      return;
    }
    if (kind === "conversation.closed") {
      scheduleListRefresh();
      // If the closed conversation is the one we have open, mark it closed.
      const cid = (ev.payload && ev.payload.id) || ev.conversation_id;
      if (cid && state.detail && state.detail.id === cid) {
        state.detail.status = "closed";
        renderMain();
      }
      return;
    }
    if (kind === "conversation.turn") {
      const cid = (ev.payload && ev.payload.conversation_id)
        || ev.conversation_id;
      const turn = ev.payload && ev.payload.turn ? ev.payload.turn : ev.payload;
      // Sidebar refresh (turn_count, preview, last_activity)
      scheduleListRefresh();
      // If this is the open conversation, append directly.
      if (cid && state.detail && state.detail.id === cid && turn) {
        appendTurnLocal(normalizeTurn(turn));
      }
      return;
    }
  }

  function connectWs() {
    function open() {
      let ws;
      try {
        const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
        ws = new WebSocket(`${proto}//${window.location.host}/api/events`);
      } catch (_) {
        scheduleReconnect();
        return;
      }
      state.ws = ws;
      ws.addEventListener("open", () => { state.wsBackoff = 1000; });
      ws.addEventListener("close", () => { state.ws = null; scheduleReconnect(); });
      ws.addEventListener("error", () => { /* close will follow */ });
      ws.addEventListener("message", (msg) => {
        let event;
        try { event = JSON.parse(msg.data); } catch (_) { return; }
        handleWsEvent(event);
      });
    }
    function scheduleReconnect() {
      const delay = state.wsBackoff;
      state.wsBackoff = Math.min(state.wsBackoff * 2, 8000);
      setTimeout(open, delay);
    }
    open();
  }

  // -------------------------------------------------------------------
  // Composer textarea autosize + keybinds
  // -------------------------------------------------------------------

  function autosizeInjectTextarea() {
    const ta = $("conv-inject-input");
    if (!ta) return;
    ta.style.height = "auto";
    const lineH = 20;
    const padPx = 18;
    const min = lineH * 2 + padPx;
    const max = lineH * 6 + padPx;
    const next = Math.min(max, Math.max(min, ta.scrollHeight));
    ta.style.height = next + "px";
    ta.style.overflowY = ta.scrollHeight > max ? "auto" : "hidden";
  }

  // -------------------------------------------------------------------
  // Bindings
  // -------------------------------------------------------------------

  function bindAll() {
    const newBtn = $("conv-new-btn");
    if (newBtn) newBtn.addEventListener("click", () => {
      if (state.newFormOpen) closeNewForm(); else openNewForm();
    });
    const cancelBtn = $("conv-new-cancel");
    if (cancelBtn) cancelBtn.addEventListener("click", closeNewForm);
    const createBtn = $("conv-new-create");
    if (createBtn) createBtn.addEventListener("click", createNewConversation);
    const newTopic = $("conv-new-topic");
    if (newTopic) newTopic.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); createNewConversation(); }
      if (e.key === "Escape") { e.preventDefault(); closeNewForm(); }
    });

    const emptyCta = $("conv-empty-cta");
    if (emptyCta) emptyCta.addEventListener("click", openNewForm);

    const goBtn = $("conv-run-go");
    const roundsInput = $("conv-run-rounds");
    function readRounds() {
      const raw = parseInt(roundsInput && roundsInput.value, 10);
      if (!Number.isFinite(raw)) return 1;
      return Math.max(1, Math.min(50, raw));
    }
    if (goBtn) goBtn.addEventListener("click", () => runRounds(readRounds()));
    if (roundsInput) {
      roundsInput.addEventListener("change", () => {
        const n = readRounds();
        roundsInput.value = String(n);
        state.runDefault = n;
        savePrefs({ runDefault: n });
      });
      roundsInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          runRounds(readRounds());
        }
      });
    }

    const closeBtn = $("conv-close-btn");
    if (closeBtn) closeBtn.addEventListener("click", closeConversation);

    const deleteBtn = $("conv-delete-btn");
    if (deleteBtn) deleteBtn.addEventListener("click", deleteConversation);

    const injForm = $("conv-inject-form");
    if (injForm) injForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const ta = $("conv-inject-input");
      if (ta) injectAsOperator(ta.value);
    });
    const injTa = $("conv-inject-input");
    if (injTa) {
      injTa.addEventListener("input", autosizeInjectTextarea);
      injTa.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
          e.preventDefault();
          injectAsOperator(injTa.value);
        }
      });
    }
    const sel = $("conv-inject-to");
    if (sel) sel.addEventListener("change", () => {
      state.composerToAgent = sel.value || null;
      savePrefs({ composerToAgent: state.composerToAgent });
    });
  }

  // -------------------------------------------------------------------
  // Boot
  // -------------------------------------------------------------------

  async function boot() {
    const prefs = loadPrefs();
    if (typeof prefs.runDefault === "number") {
      state.runDefault = Math.max(1, Math.min(50, Math.round(prefs.runDefault)));
    }
    if (prefs.composerToAgent) state.composerToAgent = prefs.composerToAgent;

    bindAll();
    bindScroll();
    autosizeInjectTextarea();

    // Fire list + agents in parallel.
    await Promise.all([fetchList(), fetchAgents()]);

    // Restore last-viewed conversation if it still exists.
    let initial = null;
    const params = new URL(window.location).searchParams;
    const qsId = params.get("id");
    if (qsId) initial = qsId;
    else if (prefs.selectedConversationId
        && state.list.some((c) => c.id === prefs.selectedConversationId)) {
      initial = prefs.selectedConversationId;
    }
    if (initial) {
      await selectConversation(initial);
    } else {
      renderMain();
    }

    // Periodic safety-net refresh in case WS misses something
    setInterval(fetchList, 30_000);
    connectWs();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
