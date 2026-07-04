/* Shared front-end core (D1).
 *
 * The seven pages each re-defined el(), escapeHtml, a relative-time formatter,
 * the agent color map, and the WebSocket reconnect loop — in several dialects,
 * which drifted into visible bugs (the same agent rendered a different grey per
 * page; one event read "45d ago" on one page and "1mo ago" on another). This is
 * the single source of truth. Exposed as `window.Exo` so pages can adopt it
 * incrementally without a module bundler.
 */
(function (global) {
  "use strict";

  // Canonical agent palette — one place, so an agent is the same color on
  // every page. Unknown agents share one deliberate neutral (never a per-page
  // accident).
  const AGENT_COLORS = {
    codex: "#58a6ff",
    hermes: "#d29922",
    claude: "#7ee787",
    claude_code: "#7ee787",
    openclaw: "#bb6bd9",
    operator: "#8b9bab",
  };
  const FALLBACK_AGENT_COLOR = "#8b949e";

  function agentColor(id) {
    return (id && AGENT_COLORS[id]) || FALLBACK_AGENT_COLOR;
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function truncate(s, n) {
    if (s == null) return "";
    s = String(s);
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  // One relative-time formatter with one granularity, so timestamps agree
  // across pages.
  function fmtRelative(ms) {
    if (!ms) return "—";
    const diff = Date.now() - ms;
    if (diff < 0) return "just now";
    if (diff < 60_000) return Math.max(1, Math.floor(diff / 1000)) + "s ago";
    if (diff < 3_600_000) return Math.floor(diff / 60_000) + "m ago";
    if (diff < 86_400_000) return Math.floor(diff / 3_600_000) + "h ago";
    if (diff < 2_592_000_000) return Math.floor(diff / 86_400_000) + "d ago";
    if (diff < 31_536_000_000) return Math.floor(diff / 2_592_000_000) + "mo ago";
    return Math.floor(diff / 31_536_000_000) + "y ago";
  }

  // Small DOM factory: el("div", {class: "x", onclick: fn}, [child, "text"]).
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const k of Object.keys(attrs)) {
        const v = attrs[k];
        if (v == null) continue;
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k === "html") node.innerHTML = v;
        else if (k.slice(0, 2) === "on" && typeof v === "function") {
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

  // fetch → JSON that never throws: returns {_ok, status, data}.
  async function fetchJSON(url, opts) {
    try {
      const res = await fetch(url, opts);
      let data = null;
      try {
        data = await res.json();
      } catch (_) {
        /* non-JSON body */
      }
      return { _ok: res.ok, status: res.status, data };
    } catch (err) {
      return { _ok: false, status: 0, data: null, error: String(err) };
    }
  }

  // WebSocket with exponential-backoff reconnect (1s→8s) and a status hook.
  // onOpen fires after each (re)connect so callers can reconcile any events
  // missed while disconnected.
  function connectWs(path, { onMessage, onStatus, onOpen } = {}) {
    let ws = null;
    let backoff = 1000;
    let closed = false;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    function open() {
      if (closed) return;
      ws = new WebSocket(`${proto}//${location.host}${path}`);
      ws.onopen = () => {
        backoff = 1000;
        if (onStatus) onStatus("online");
        if (onOpen) onOpen();
      };
      ws.onmessage = (ev) => {
        if (!onMessage) return;
        try {
          onMessage(JSON.parse(ev.data));
        } catch (_) {
          /* ignore non-JSON frames like __hello__ */
        }
      };
      ws.onclose = () => {
        if (onStatus) onStatus("offline");
        if (closed) return;
        setTimeout(open, backoff);
        backoff = Math.min(backoff * 2, 8000);
      };
      ws.onerror = () => {
        try {
          ws.close();
        } catch (_) {}
      };
    }
    open();
    return {
      close() {
        closed = true;
        if (ws) ws.close();
      },
    };
  }

  global.Exo = {
    AGENT_COLORS,
    FALLBACK_AGENT_COLOR,
    agentColor,
    escapeHtml,
    truncate,
    fmtRelative,
    el,
    fetchJSON,
    connectWs,
  };
})(window);
