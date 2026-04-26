// Memory chat — header toggle + chat panel.
//
// The toggle is on every page (dashboard + memory). The chat panel is only
// rendered on /memory. The backend exposes:
//   GET  /api/settings/memory_chat
//   POST /api/settings/memory_chat/toggle
//   POST /api/memory/chat
//
// Endpoints can 404 / 503 — backend may not be ready. We render graceful
// "unavailable" states rather than crashing or hiding the toggle.

(function () {
  "use strict";

  const MAX_HISTORY = 5;

  const state = {
    enabled: false,
    available: true,        // false -> backend 404/503
    model: null,
    embeddingModel: null,
    endpointReachable: false,
    history: [],            // [{q, a, cites, model, latencyMs, error?}]
    pending: false,
  };

  const toggleEl = () => document.getElementById("mem-toggle");
  const panelEl = () => document.getElementById("mem-chat");

  function setToggleVisual() {
    const t = toggleEl();
    if (!t) return;
    t.classList.remove("on", "off", "unavailable");
    if (!state.available) {
      t.classList.add("unavailable");
      t.title = "memory chat backend not ready";
      return;
    }
    t.classList.add(state.enabled ? "on" : "off");
    t.title = state.enabled
      ? `memory chat ON${state.model ? " — " + state.model : ""}`
      : "memory chat OFF — click to enable";
  }

  function setPanelVisual() {
    const p = panelEl();
    if (!p) return; // not on /memory
    const head = document.getElementById("mem-chat-head");
    const scroll = document.getElementById("mem-chat-scroll");
    const form = document.getElementById("mem-chat-form");
    const stub = document.getElementById("mem-chat-stub");
    const indicator = document.getElementById("mem-chat-indicator");

    if (!state.available) {
      p.classList.remove("open");
      p.classList.add("collapsed-stub");
      stub.style.display = "flex";
      stub.textContent = "memory chat backend not ready (will retry)";
      head.style.display = "none";
      scroll.style.display = "none";
      form.style.display = "none";
      return;
    }

    if (state.enabled) {
      p.classList.add("open");
      p.classList.remove("collapsed-stub");
      stub.style.display = "none";
      head.style.display = "flex";
      scroll.style.display = "flex";
      form.style.display = "flex";
      indicator.textContent = state.endpointReachable
        ? `●  ready${state.model ? "  ·  " + state.model : ""}`
        : "○  endpoint unreachable";
      indicator.style.color = state.endpointReachable
        ? "var(--accent)"
        : "var(--warn)";
    } else {
      p.classList.remove("open");
      p.classList.add("collapsed-stub");
      stub.style.display = "flex";
      stub.textContent = "memory chat is off — flip the header toggle to enable";
      head.style.display = "none";
      scroll.style.display = "none";
      form.style.display = "none";
    }
  }

  function renderHistory() {
    const host = document.getElementById("mem-chat-scroll");
    if (!host) return;
    host.innerHTML = "";
    for (const turn of state.history) {
      const div = document.createElement("div");
      div.className = "turn" + (turn.error ? " error" : "") + (turn.loading ? " loading" : "");
      const q = document.createElement("div");
      q.className = "q";
      q.textContent = turn.q;
      div.appendChild(q);

      const a = document.createElement("div");
      a.className = "a";
      a.textContent = turn.a || (turn.loading ? "thinking" : "");
      div.appendChild(a);

      if (turn.cites && turn.cites.length) {
        const cites = document.createElement("div");
        cites.className = "cites";
        for (const cid of turn.cites) {
          const chip = document.createElement("span");
          chip.className = "cite";
          chip.textContent = cid.slice(0, 8);
          chip.title = cid;
          chip.addEventListener("click", () => {
            // Drive the constellation. Custom event for constellation.js.
            window.dispatchEvent(new CustomEvent("constellation:focus", {
              detail: { recordId: cid },
            }));
          });
          cites.appendChild(chip);
        }
        div.appendChild(cites);
      }

      if (!turn.loading && !turn.error && (turn.model || turn.latencyMs != null)) {
        const f = document.createElement("div");
        f.className = "footer";
        const parts = [];
        if (turn.model) parts.push("answered by " + turn.model);
        if (turn.latencyMs != null) parts.push("in " + (turn.latencyMs / 1000).toFixed(1) + "s");
        if (turn.cites && turn.cites.length) parts.push("cited " + turn.cites.length + " record" + (turn.cites.length === 1 ? "" : "s"));
        f.textContent = parts.join(" · ");
        div.appendChild(f);
      }

      host.appendChild(div);
    }
    // Scroll to most recent
    host.scrollTop = host.scrollHeight;
  }

  async function fetchSettings() {
    try {
      const r = await fetch("/api/settings/memory_chat");
      if (!r.ok) {
        state.available = (r.status !== 404 && r.status !== 503);
        if (r.status === 404 || r.status === 503) {
          state.available = false;
        }
        setToggleVisual();
        setPanelVisual();
        return;
      }
      const data = await r.json();
      state.available = true;
      state.enabled = !!data.enabled;
      state.model = data.model || null;
      state.embeddingModel = data.embedding_model || null;
      state.endpointReachable = !!data.endpoint_reachable;
    } catch (_) {
      state.available = false;
    }
    setToggleVisual();
    setPanelVisual();
  }

  async function toggleEnabled() {
    if (!state.available) return;
    try {
      const r = await fetch("/api/settings/memory_chat/toggle", { method: "POST" });
      if (!r.ok) {
        state.available = (r.status !== 404 && r.status !== 503);
        setToggleVisual();
        setPanelVisual();
        return;
      }
      const data = await r.json();
      state.enabled = !!data.enabled;
      if (data.model !== undefined) state.model = data.model || null;
      if (data.endpoint_reachable !== undefined) state.endpointReachable = !!data.endpoint_reachable;
    } catch (_) {
      state.available = false;
    }
    setToggleVisual();
    setPanelVisual();
  }

  async function askChat(question) {
    if (!state.enabled || !question.trim() || state.pending) return;
    const turn = { q: question, a: "", cites: [], loading: true };
    state.history.unshift(turn);
    while (state.history.length > MAX_HISTORY) state.history.pop();
    state.pending = true;
    document.getElementById("mem-chat-send").disabled = true;
    renderHistory();

    const t0 = performance.now();
    try {
      const r = await fetch("/api/memory/chat", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ question, top_k: 6 }),
      });
      if (!r.ok) {
        turn.loading = false;
        turn.error = true;
        turn.a = r.status === 404 || r.status === 503
          ? "memory chat backend not ready (got HTTP " + r.status + ")"
          : "request failed (HTTP " + r.status + ")";
      } else {
        const data = await r.json();
        turn.loading = false;
        turn.a = data.answer || "(empty answer)";
        turn.cites = Array.isArray(data.cited_record_ids) ? data.cited_record_ids : [];
        turn.model = data.model || state.model;
        turn.latencyMs = (typeof data.latency_ms === "number") ? data.latency_ms : Math.round(performance.now() - t0);
      }
    } catch (_e) {
      turn.loading = false;
      turn.error = true;
      turn.a = "network error contacting /api/memory/chat";
    }
    state.pending = false;
    document.getElementById("mem-chat-send").disabled = false;
    renderHistory();
  }

  function bind() {
    const t = toggleEl();
    if (t) t.addEventListener("click", toggleEnabled);

    const form = document.getElementById("mem-chat-form");
    if (form) {
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        const inp = document.getElementById("mem-chat-input");
        const q = inp.value;
        inp.value = "";
        askChat(q);
      });
    }
  }

  function init() {
    bind();
    setToggleVisual();
    setPanelVisual();
    fetchSettings();
    // Poll occasionally so a backend that comes up later flips us into
    // the available state without a page reload.
    setInterval(() => {
      if (!state.available || !state.endpointReachable) fetchSettings();
    }, 8000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
