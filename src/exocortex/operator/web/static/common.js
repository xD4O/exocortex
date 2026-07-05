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
    codex: "#DE4A64",
    hermes: "#0FA093",
    claude: "#8A66EC",
    claude_code: "#8A66EC",
    openclaw: "#3E8FD8",
    operator: "#8593A9",
  };
  const FALLBACK_AGENT_COLOR = "#8b949e";
  // canonical id -> CSS token (themes retune the exact shade per surface)
  const AGENT_TOKEN = {
    codex: "--ag-codex",
    hermes: "--ag-hermes",
    claude: "--ag-claude",
    claude_code: "--ag-claude",
    openclaw: "--ag-openclaw",
    operator: "--ag-operator",
  };

  function agentColor(id) {
    const token = id && AGENT_TOKEN[id];
    if (token) {
      const v = getComputedStyle(document.documentElement)
        .getPropertyValue(token)
        .trim();
      if (v) return v;
    }
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

  // ---- UI v2: theme persistence -------------------------------------
  // data-theme on <html> wins over the OS preference; "auto" removes it.
  // Each page also inlines a tiny boot script in <head> so the attribute
  // is set before first paint (no flash); this is the shared API.
  const THEME_KEY = "exo-theme";
  const THEMES = ["auto", "dark", "light", "phosphor", "sepia", "synthwave"];

  function themeGet() {
    try {
      const t = localStorage.getItem(THEME_KEY);
      return THEMES.indexOf(t) >= 0 ? t : "auto";
    } catch (_) {
      return "auto";
    }
  }

  function themeSet(t) {
    if (THEMES.indexOf(t) < 0) t = "auto";
    try {
      localStorage.setItem(THEME_KEY, t);
    } catch (_) {
      /* private mode: theme just won't persist */
    }
    if (t === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = t;
    document.dispatchEvent(new CustomEvent("exo:theme", { detail: { theme: t } }));
  }

  // ---- UI v2: rail footer stats (present on every page) --------------
  function bootRail() {
    const rec = document.getElementById("rail-records");
    const ev = document.getElementById("rail-events");
    if (!rec && !ev) return;
    fetchJSON("/api/status")
      .then(function (res) {
        const st = res && res._ok ? res.data : null;
        if (!st) return;
        if (rec) rec.textContent = (st.memory_records ?? 0).toLocaleString();
        if (ev) ev.textContent = (st.events_total ?? 0).toLocaleString();
      })
      .catch(function () {
        /* rail stats are decorative; page keeps working */
      });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootRail);
  } else {
    bootRail();
  }

  // ---- UI v2: global search hotkey (constellation has the real search) --
  document.addEventListener("keydown", function (e) {
    if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
      const t = e.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      e.preventDefault();
      if (location.pathname !== "/memory") location.href = "/memory";
      else {
        const q = document.querySelector("#cons-search, .cons-search input, input[type=search]");
        if (q) q.focus();
      }
    }
  });

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
    theme: { get: themeGet, set: themeSet, THEMES },
  };
})(window);
