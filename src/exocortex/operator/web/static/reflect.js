// reflect.js — /static/reflect.html
//
// Renders insights proposed by the reflect subsystem (contradictions,
// patterns, gaps, syntheses found across the memory store) as cards grouped
// by kind, with accept/dismiss actions. This is a pure projection over the
// audit log via ReflectionService — no local state beyond the "show
// resolved" toggle and per-card pending flags.
//
// Endpoints:
//   GET  /api/insights?include_resolved=<bool>
//   POST /api/insights/{id}/accept
//   POST /api/insights/{id}/dismiss
//
// Live updates: the shared /api/events WebSocket carries insight.* and
// reflection.* events — any of those trigger a debounced refetch.

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const el = (window.Exo && Exo.el) || null;
  const fetchJSON = (window.Exo && Exo.fetchJSON) || null;

  const KIND_DEFS = [
    { kind: "contradiction", label: "CONTRADICTIONS" },
    { kind: "pattern", label: "PATTERNS" },
    { kind: "gap", label: "GAPS" },
    { kind: "synthesis", label: "SYNTHESES" },
  ];
  const KNOWN_KINDS = new Set(KIND_DEFS.map((d) => d.kind));

  const state = {
    items: [],
    available: true,
    loaded: false,
    showResolved: false,
    pending: {}, // {insight_id: "accept" | "dismiss"}
  };

  function shortId(id) {
    if (!id) return "—";
    const s = String(id);
    return s.length <= 8 ? s : s.slice(0, 8);
  }

  function kindLabel(kind) {
    const def = KIND_DEFS.find((d) => d.kind === kind);
    return def ? def.label : String(kind || "other").toUpperCase();
  }

  async function fetchInsights() {
    if (!fetchJSON) return;
    const res = await fetchJSON(
      "/api/insights?include_resolved=" + (state.showResolved ? "true" : "false")
    );
    state.loaded = true;
    if (!res._ok) {
      state.available = false;
      render();
      return;
    }
    state.available = true;
    state.items = (res.data && res.data.items) || [];
    render();
  }

  function groupItems() {
    const groups = new Map();
    for (const def of KIND_DEFS) groups.set(def.kind, []);
    const other = [];
    for (const item of state.items) {
      const kind = item.kind;
      if (KNOWN_KINDS.has(kind)) {
        groups.get(kind).push(item);
      } else {
        other.push(item);
      }
    }
    return { groups, other };
  }

  function render() {
    const host = $("reflect-groups");
    const kpi = $("kpi-insights");
    if (!host) return;

    const openCount = state.items.filter((i) => (i.status || "proposed") === "proposed").length;
    if (kpi) kpi.textContent = String(openCount);

    host.innerHTML = "";

    if (!state.available) {
      host.appendChild(el("div", {
        class: "reflect-empty",
        text: "insights endpoint not ready (404/503) — will retry",
      }));
      return;
    }
    if (!state.loaded) {
      host.appendChild(el("div", { class: "reflect-empty", text: "loading…" }));
      return;
    }
    if (state.items.length === 0) {
      host.appendChild(el("div", {
        class: "reflect-empty",
        text: state.showResolved
          ? "no insights yet"
          : "no open insights — reflect will surface contradictions, patterns, gaps and syntheses here as it runs",
      }));
      return;
    }

    const { groups, other } = groupItems();
    for (const def of KIND_DEFS) {
      const items = groups.get(def.kind);
      if (items.length === 0) continue;
      host.appendChild(buildGroup(def.label, items));
    }
    if (other.length) {
      host.appendChild(buildGroup("OTHER", other));
    }
  }

  function buildGroup(label, items) {
    const head = el("div", { class: "reflect-group-head" }, [
      el("span", { class: "label", text: label }),
      el("span", { class: "count mono", text: "(" + items.length + ")" }),
    ]);
    const list = el("div", { class: "reflect-group-list" });
    for (const item of items) list.appendChild(buildCard(item));
    return el("section", { class: "reflect-group" }, [head, list]);
  }

  function buildCard(item) {
    const status = item.status || "proposed";
    const isOpen = status === "proposed";
    const refs = Array.isArray(item.refs) ? item.refs : [];
    const pending = state.pending[item.insight_id];

    const head = el("div", { class: "reflect-card-head" }, [
      el("span", { class: "reflect-status status-" + status, text: status }),
      el("span", { class: "reflect-title", text: item.title || "(untitled insight)" }),
    ]);

    const body = el("div", { class: "reflect-card-body" }, [
      item.detail ? el("div", { class: "reflect-detail", text: item.detail }) : null,
      refs.length
        ? el("div", { class: "reflect-refs mono" }, [
            el("span", { class: "k", text: "refs: " }),
            el("span", { class: "v", text: refs.map(shortId).join(", ") }),
          ])
        : null,
    ]);

    const card = el("div", {
      class: "reflect-card" + (isOpen ? "" : " resolved"),
      "data-insight-id": item.insight_id,
    }, [head, body]);

    if (isOpen) {
      const actions = el("div", { class: "reflect-card-actions" }, [
        el("button", {
          type: "button",
          class: "reflect-action reflect-accept",
          disabled: pending ? "disabled" : null,
          onclick: () => act(item.insight_id, "accept"),
        }, [pending === "accept" ? "accepting…" : "accept"]),
        el("button", {
          type: "button",
          class: "reflect-action reflect-dismiss",
          disabled: pending ? "disabled" : null,
          onclick: () => act(item.insight_id, "dismiss"),
        }, [pending === "dismiss" ? "dismissing…" : "dismiss"]),
      ]);
      card.appendChild(actions);
    }

    return card;
  }

  async function act(insightId, action) {
    if (!insightId || state.pending[insightId]) return;
    state.pending[insightId] = action;
    render();
    try {
      const r = await fetch(`/api/insights/${encodeURIComponent(insightId)}/${action}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!r.ok) {
        delete state.pending[insightId];
        render();
        return;
      }
    } catch (_) {
      delete state.pending[insightId];
      render();
      return;
    }
    delete state.pending[insightId];
    await fetchInsights();
  }

  function bind() {
    const cb = $("reflect-show-resolved");
    if (cb) {
      cb.addEventListener("change", () => {
        state.showResolved = cb.checked;
        fetchInsights();
      });
    }
  }

  function init() {
    bind();
    render();
    fetchInsights();

    // Safety-net poll in case a WS event is missed or the socket is down.
    setInterval(fetchInsights, 30_000);

    // Live updates: refetch (debounced) on any insight.*/reflection.* event,
    // and reconcile on every (re)connect so events missed while disconnected
    // aren't lost.
    if (window.Exo && Exo.connectWs) {
      let pending = null;
      Exo.connectWs("/api/events", {
        onOpen: fetchInsights,
        onMessage: (ev) => {
          if (!ev || !ev.kind) return;
          if (ev.kind.startsWith("insight.") || ev.kind.startsWith("reflection.")) {
            clearTimeout(pending);
            pending = setTimeout(fetchInsights, 400);
          }
        },
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
