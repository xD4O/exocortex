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

  // Accessible modal panel (slide-in side panels / overlay drawers). Sets
  // dialog semantics, moves focus into the panel, traps Tab within it, closes
  // on Escape, and restores focus to the trigger on close. Returns a close()
  // function the caller invokes from its own close paths (close button, etc.).
  function openDialog(panel, opts) {
    opts = opts || {};
    const trigger = document.activeElement;
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    if (opts.label) panel.setAttribute("aria-label", opts.label);
    if (opts.labelledBy) panel.setAttribute("aria-labelledby", opts.labelledBy);

    function focusables() {
      const sel =
        'a[href], button:not([disabled]), textarea:not([disabled]), ' +
        'input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';
      return Array.prototype.slice
        .call(panel.querySelectorAll(sel))
        .filter((n) => n.offsetParent !== null || n === document.activeElement);
    }

    const first = focusables()[0];
    if (first) {
      first.focus();
    } else {
      panel.setAttribute("tabindex", "-1");
      panel.focus();
    }

    let closed = false;
    function onKey(e) {
      if (e.key === "Escape") {
        e.stopPropagation();
        close();
        return;
      }
      if (e.key !== "Tab") return;
      const f = focusables();
      if (f.length === 0) {
        e.preventDefault();
        return;
      }
      const a = f[0];
      const z = f[f.length - 1];
      if (e.shiftKey && document.activeElement === a) {
        e.preventDefault();
        z.focus();
      } else if (!e.shiftKey && document.activeElement === z) {
        e.preventDefault();
        a.focus();
      }
    }
    panel.addEventListener("keydown", onKey);

    function close() {
      if (closed) return;
      closed = true;
      panel.removeEventListener("keydown", onKey);
      if (opts.onClose) {
        try {
          opts.onClose();
        } catch (_) {
          /* caller close is best-effort */
        }
      }
      if (
        trigger &&
        typeof trigger.focus === "function" &&
        document.contains(trigger)
      ) {
        trigger.focus();
      }
    }
    return close;
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
    openDialog,
  };
})(window);
