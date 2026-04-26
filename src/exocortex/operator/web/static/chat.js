// chat.js — full-page memory chat (/chat)
//
// Renders the chat history zone, composer, and the per-page state (history
// in localStorage, scroll-pill, citation chips, "thinking" spinner). The
// header toggle is bound by memchat.js, the scope selector and tasks sidebar
// and activity strip are bound by memory_extras.js. We deliberately keep
// those concerns out of this file.
//
// All endpoints can 404/503; we render a one-line warning above the textarea
// rather than throwing.

(function () {
  "use strict";

  const HISTORY_LS_KEY = "exocortex.chat.history.v1";
  const MAX_TURNS = 50;
  const TEXTAREA_MIN_LINES = 1;
  const TEXTAREA_MAX_LINES = 6;
  const TEXTAREA_LINE_PX = 20; // matches font-size 13px * 1.55 line-height

  const state = {
    history: [],          // [{q, a, cites, model, latencyMs, ts, error?, system?}]
    pending: false,
    autoStick: true,      // user is at bottom; auto-scroll on new turn
    pendingNew: 0,        // count of new turns while user is scrolled up
  };

  const $ = (id) => document.getElementById(id);

  // ---------------------------------------------------------------------
  // History persistence
  // ---------------------------------------------------------------------

  function loadHistory() {
    try {
      const raw = localStorage.getItem(HISTORY_LS_KEY);
      if (!raw) return;
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        state.history = parsed.slice(-MAX_TURNS);
      }
    } catch (_) { /* ignore */ }
  }

  function saveHistory() {
    try {
      // Only persist completed turns; skip in-flight loading turns.
      const persistable = state.history.filter((t) => !t.loading);
      localStorage.setItem(
        HISTORY_LS_KEY,
        JSON.stringify(persistable.slice(-MAX_TURNS))
      );
    } catch (_) { /* ignore */ }
  }

  function clearHistory() {
    state.history = [];
    saveHistory();
    renderHistory();
    updateEmpty();
    updateTurnCounter();
  }

  // ---------------------------------------------------------------------
  // Rendering
  // ---------------------------------------------------------------------

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const k in attrs) {
        if (k === "class") node.className = attrs[k];
        else if (k === "text") node.textContent = attrs[k];
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

  function fmtLatency(ms) {
    if (ms == null) return "";
    if (ms < 1000) return ms + "ms";
    return (ms / 1000).toFixed(1) + "s";
  }

  function renderTurn(turn) {
    if (turn.system) {
      return el("div", { class: "ct-system" }, [turn.text || ""]);
    }

    const wrap = el("div", {
      class: "ct-turn" + (turn.error ? " error" : "") + (turn.loading ? " loading" : ""),
    });

    if (turn.q) {
      const q = el("div", { class: "ct-q" }, [
        el("span", { class: "label", text: "Q ›" }),
        el("span", { class: "txt", text: turn.q }),
      ]);
      wrap.appendChild(q);
    }

    const a = el("div", { class: "ct-a" });
    a.textContent = turn.a || (turn.loading ? "thinking" : "");
    wrap.appendChild(a);

    if (turn.cites && turn.cites.length) {
      const cites = el("div", { class: "ct-cites" });
      for (const cid of turn.cites) {
        const chip = el("a", {
          class: "ct-cite",
          href: "/memory#focus=" + encodeURIComponent(cid),
          target: "_blank",
          rel: "noopener",
          title: cid,
        }, [cid.slice(0, 8)]);
        cites.appendChild(chip);
      }
      wrap.appendChild(cites);
    }

    if (!turn.loading && !turn.error && (turn.model || turn.latencyMs != null || (turn.cites && turn.cites.length))) {
      const parts = [];
      if (turn.cites) {
        const n = turn.cites.length;
        parts.push("cited " + n + " record" + (n === 1 ? "" : "s"));
      }
      if (turn.model) parts.push("model " + turn.model);
      if (turn.latencyMs != null) parts.push(fmtLatency(turn.latencyMs));
      wrap.appendChild(el("div", { class: "ct-foot", text: parts.join(" · ") }));
    }

    return wrap;
  }

  function renderHistory() {
    const host = $("chat-history");
    if (!host) return;
    host.innerHTML = "";
    for (const turn of state.history) {
      host.appendChild(renderTurn(turn));
    }
  }

  function appendTurn(turn) {
    const host = $("chat-history");
    if (!host) return;
    host.appendChild(renderTurn(turn));
  }

  function updateEmpty() {
    const empty = $("chat-empty");
    if (!empty) return;
    // Show empty hint only if no real Q/A turns exist (system-only rows
    // shouldn't suppress it).
    const hasReal = state.history.some((t) => !t.system);
    empty.style.display = hasReal ? "none" : "";
  }

  function updateTurnCounter() {
    const k = $("kpi-turns");
    if (!k) return;
    const real = state.history.filter((t) => !t.system).length;
    k.textContent = String(real);
  }

  // ---------------------------------------------------------------------
  // Scroll behavior
  // ---------------------------------------------------------------------

  function scrollHost() { return $("chat-history"); }

  function isAtBottom() {
    const host = scrollHost();
    if (!host) return true;
    return host.scrollHeight - (host.scrollTop + host.clientHeight) < 24;
  }

  function scrollToBottom() {
    const host = scrollHost();
    if (!host) return;
    host.scrollTop = host.scrollHeight;
    state.autoStick = true;
    state.pendingNew = 0;
    updateScrollPill();
  }

  function updateScrollPill() {
    const pill = $("scroll-pill");
    const n = $("scroll-pill-n");
    if (!pill || !n) return;
    if (state.pendingNew > 0 && !state.autoStick) {
      pill.hidden = false;
      n.textContent = String(state.pendingNew);
    } else {
      pill.hidden = true;
    }
  }

  function bindScroll() {
    const host = scrollHost();
    if (host) {
      host.addEventListener("scroll", () => {
        state.autoStick = isAtBottom();
        if (state.autoStick) {
          state.pendingNew = 0;
          updateScrollPill();
        }
      });
    }
    const pill = $("scroll-pill");
    if (pill) pill.addEventListener("click", scrollToBottom);
  }

  // ---------------------------------------------------------------------
  // Composer
  // ---------------------------------------------------------------------

  function autosizeTextarea() {
    const ta = $("chat-input");
    if (!ta) return;
    ta.style.height = "auto";
    const lineH = TEXTAREA_LINE_PX;
    // padding + border ~ 18px (10px top + 6px bottom + borders).
    const padPx = 18;
    const min = lineH * TEXTAREA_MIN_LINES + padPx;
    const max = lineH * TEXTAREA_MAX_LINES + padPx;
    const next = Math.min(max, Math.max(min, ta.scrollHeight));
    ta.style.height = next + "px";
    ta.style.overflowY = ta.scrollHeight > max ? "auto" : "hidden";
  }

  function showWarning(msg) {
    const w = $("composer-warning");
    if (!w) return;
    if (!msg) {
      w.hidden = true;
      w.textContent = "";
      return;
    }
    w.hidden = false;
    w.textContent = msg;
  }

  function memChatStatus() {
    // Read the visual state set by memchat.js — it's the canonical source
    // for backend availability and ON/OFF.
    const t = $("mem-toggle");
    if (!t) return { available: true, enabled: true };
    if (t.classList.contains("unavailable")) {
      return { available: false, enabled: false };
    }
    return {
      available: true,
      enabled: t.classList.contains("on"),
    };
  }

  function refreshComposerEnabled() {
    const status = memChatStatus();
    const send = $("chat-send");
    const ta = $("chat-input");
    const indicator = $("chat-indicator");

    if (!status.available) {
      if (send) send.disabled = true;
      if (ta) ta.disabled = true;
      if (indicator) {
        indicator.textContent = "○  unavailable";
        indicator.style.color = "var(--warn)";
      }
      showWarning("memory chat backend not ready — will retry");
      return;
    }
    if (!status.enabled) {
      if (send) send.disabled = true;
      if (ta) ta.disabled = true;
      if (indicator) {
        indicator.textContent = "○  off";
        indicator.style.color = "var(--muted)";
      }
      showWarning("memory chat is off — turn it on in the header");
      return;
    }
    if (send) send.disabled = state.pending;
    if (ta) ta.disabled = false;
    if (indicator) {
      indicator.textContent = state.pending ? "●  thinking" : "●  ready";
      indicator.style.color = "var(--accent)";
    }
    if (!state.pending) showWarning(null);
  }

  // ---------------------------------------------------------------------
  // Ask
  // ---------------------------------------------------------------------

  async function ask(question) {
    const status = memChatStatus();
    if (!status.available || !status.enabled) return;
    const q = (question || "").trim();
    if (!q || state.pending) return;

    const turn = {
      q,
      a: "",
      cites: [],
      ts: Date.now(),
      loading: true,
    };
    state.history.push(turn);
    while (state.history.length > MAX_TURNS) state.history.shift();

    state.pending = true;
    refreshComposerEnabled();
    updateEmpty();
    updateTurnCounter();
    appendTurn(turn);
    if (state.autoStick) scrollToBottom();
    showThinking(true);

    const t0 = performance.now();
    try {
      const r = await fetch("/api/memory/chat", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ question: q, top_k: 6 }),
      });
      if (!r.ok) {
        turn.loading = false;
        turn.error = true;
        if (r.status === 404 || r.status === 503) {
          turn.a = "memory chat backend not ready (HTTP " + r.status + ")";
          // Show the contextual warning too
          let msg = "memory chat is OFF — flip the toggle in the header";
          try {
            const data = await r.json();
            if (data && typeof data.detail === "string") {
              if (/ollama/i.test(data.detail)) {
                msg = "Ollama unreachable — is `ollama serve` running?";
              } else if (/off|disabled/i.test(data.detail)) {
                msg = "memory chat is OFF — flip the toggle in the header";
              } else {
                msg = data.detail;
              }
            }
          } catch (_) { /* leave msg */ }
          showWarning(msg);
          // Don't add the failed turn to history — the spec says 503 should
          // surface as a warning bar and not persist.
          state.history.pop();
        } else {
          turn.a = "request failed (HTTP " + r.status + ")";
        }
      } else {
        const data = await r.json();
        turn.loading = false;
        turn.a = data.answer || "(empty answer)";
        turn.cites = Array.isArray(data.cited_record_ids) ? data.cited_record_ids : [];
        turn.model = data.model || null;
        turn.latencyMs = (typeof data.latency_ms === "number")
          ? data.latency_ms
          : Math.round(performance.now() - t0);
      }
    } catch (_e) {
      turn.loading = false;
      turn.error = true;
      turn.a = "network error contacting /api/memory/chat";
    }

    state.pending = false;
    showThinking(false);
    saveHistory();
    renderHistory();
    updateEmpty();
    updateTurnCounter();
    refreshComposerEnabled();
    if (state.autoStick) {
      scrollToBottom();
    } else {
      state.pendingNew += 1;
      updateScrollPill();
    }
  }

  function showThinking(on) {
    const t = $("chat-thinking");
    if (t) t.hidden = !on;
  }

  // ---------------------------------------------------------------------
  // System notes (e.g. "→ scoped to task abc12345")
  // ---------------------------------------------------------------------

  function appendSystemNote(text) {
    const note = { system: true, text, ts: Date.now() };
    state.history.push(note);
    while (state.history.length > MAX_TURNS) state.history.shift();
    saveHistory();
    appendTurn(note);
    if (state.autoStick) scrollToBottom();
  }

  // ---------------------------------------------------------------------
  // Bindings
  // ---------------------------------------------------------------------

  function bindComposer() {
    const ta = $("chat-input");
    const form = $("chat-form");
    const send = $("chat-send");

    if (ta) {
      ta.addEventListener("input", autosizeTextarea);
      ta.addEventListener("keydown", (e) => {
        // Cmd/Ctrl+Enter submits
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
          e.preventDefault();
          submit();
          return;
        }
        // Esc clears
        if (e.key === "Escape") {
          e.preventDefault();
          ta.value = "";
          autosizeTextarea();
        }
      });
    }
    if (form) {
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        submit();
      });
    }
    if (send) {
      send.addEventListener("click", (e) => {
        e.preventDefault();
        submit();
      });
    }

    function submit() {
      const v = ta ? ta.value : "";
      if (!v.trim()) return;
      const q = v;
      if (ta) {
        ta.value = "";
        autosizeTextarea();
      }
      ask(q);
    }
  }

  function bindClear() {
    const b = $("chat-clear");
    if (!b) return;
    b.addEventListener("click", () => {
      if (state.history.length === 0) return;
      const ok = window.confirm("Clear the chat conversation? This cannot be undone.");
      if (!ok) return;
      clearHistory();
    });
  }

  function bindToggleObserver() {
    // The header toggle's class is the canonical signal; watch for changes
    // so the composer enables/disables in sync.
    const t = $("mem-toggle");
    if (!t || !("MutationObserver" in window)) {
      // Fall back to polling.
      setInterval(refreshComposerEnabled, 1500);
      return;
    }
    new MutationObserver(refreshComposerEnabled).observe(t, {
      attributes: true,
      attributeFilter: ["class"],
    });
  }

  function bindTaskScopedEvent() {
    window.addEventListener("exocortex:task-scoped", (ev) => {
      const d = ev && ev.detail;
      if (!d) return;
      const tid = d.task_id || d.scope_id || "";
      const short = String(tid).slice(0, 8);
      appendSystemNote("→ scoped to task " + short);
    });
  }

  // ---------------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------------

  function init() {
    loadHistory();
    renderHistory();
    updateEmpty();
    updateTurnCounter();
    bindComposer();
    bindClear();
    bindScroll();
    bindToggleObserver();
    bindTaskScopedEvent();
    autosizeTextarea();
    refreshComposerEnabled();
    // Initial scroll to bottom (history may have prior turns).
    requestAnimationFrame(() => scrollToBottom());
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
