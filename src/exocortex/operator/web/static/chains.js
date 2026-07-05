// chains.js — handoff-chain swimlane visualization.
//
// Exposes a single function on window.exocortexChains:
//
//   renderSwimlane(container, chain) -> void
//
// `container` is a DOM node to render into (cleared first).
// `chain`     is the API shape from /api/handoffs/chain/{task_id} or
//             one item from /api/handoffs/chains:
//
//   { chain_id, hops, depth, agents_path: [...], started_at, ended_at,
//     status, tasks: [{task_id, agent_id, parent_task_id?, started_at,
//                     ended_at, status, goal_preview}],
//     events: [{event_id, kind, agent_id, timestamp_ms,
//               parent_task_id?, child_task_id?, to_agent?}] }
//
// All times are accepted as ms-since-epoch numbers OR ISO-8601 strings.
// Render is SVG-based for accessibility + crisp scaling. Bars use the
// established per-agent palette; status overlays (running pulse, failed
// stripe, completed solid) sit on top.
//
// The bar/arrow geometry is computed against a configurable viewBox so
// the swimlane scales to any container width. We use viewBox + a fixed
// internal coordinate system; the SVG itself is `width="100%"`.

(function () {
  "use strict";

  const AGENT_COLORS = {
    codex: "#58a6ff",
    hermes: "#d29922",
    claude: "#7ee787",
    claude_code: "#7ee787",
    openclaw: "#bb6bd9",
  };
  const FALLBACK_AGENT_COLOR = "#8b949e";
  const STATUS_COLORS = {
    running:   "#58a6ff",
    completed: "#7ee787",
    succeeded: "#7ee787",
    failed:    "#f85149",
    cancelled: "#8b9bab",
    pending:   "#d29922",
  };

  // Viewbox geometry (internal "design" coordinates).
  const VB = {
    width: 1000,
    leftPad: 110,    // agent label gutter
    rightPad: 24,
    topPad: 22,
    rowHeight: 40,
    barHeight: 22,
    bottomPad: 36,   // axis room
  };

  function agentColor(id) {
    if (!id) return FALLBACK_AGENT_COLOR;
    return AGENT_COLORS[id] || FALLBACK_AGENT_COLOR;
  }

  function statusColor(s) {
    return STATUS_COLORS[(s || "").toLowerCase()] || FALLBACK_AGENT_COLOR;
  }

  function toMs(v) {
    if (v == null) return null;
    if (typeof v === "number") return v;
    const n = Date.parse(v);
    return isNaN(n) ? null : n;
  }

  function fmtDur(ms) {
    if (ms == null || ms < 0) return "—";
    const s = Math.round(ms / 1000);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    const rs = s % 60;
    if (m < 60) return `${m}m${rs.toString().padStart(2, "0")}s`;
    const h = Math.floor(m / 60);
    const rm = m % 60;
    return `${h}h${rm.toString().padStart(2, "0")}m`;
  }

  function fmtClock(ms) {
    if (ms == null) return "—";
    try {
      const d = new Date(ms);
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      const ss = String(d.getSeconds()).padStart(2, "0");
      return `${hh}:${mm}:${ss}`;
    } catch (_) { return "—"; }
  }

  function shortId(s) {
    if (!s) return "";
    s = String(s);
    return s.length > 8 ? s.slice(0, 8) : s;
  }

  function svgEl(name, attrs) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", name);
    if (attrs) {
      for (const k in attrs) {
        if (k === "text") node.textContent = attrs[k];
        else node.setAttribute(k, attrs[k]);
      }
    }
    return node;
  }

  function htmlEl(tag, attrs, children) {
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

  // Build the ordered list of agent rows. Use agents_path order if present,
  // otherwise fall back to first-seen order from tasks.
  function deriveAgentRows(chain) {
    const seen = new Set();
    const rows = [];
    const path = Array.isArray(chain.agents_path) ? chain.agents_path : [];
    for (const a of path) {
      if (a && !seen.has(a)) { seen.add(a); rows.push(a); }
    }
    for (const t of (chain.tasks || [])) {
      if (t.agent_id && !seen.has(t.agent_id)) {
        seen.add(t.agent_id);
        rows.push(t.agent_id);
      }
    }
    // Don't silently drop tasks whose owning agent couldn't be derived — give
    // them a placeholder lane so they still show on the swimlane. (C7)
    if ((chain.tasks || []).some((t) => !t.agent_id)) {
      rows.push("(unknown)");
    }
    return rows;
  }

  function computeTimeBounds(chain) {
    let lo = Infinity;
    let hi = -Infinity;
    for (const t of (chain.tasks || [])) {
      const s = toMs(t.started_at);
      const e = toMs(t.ended_at);
      if (s != null && s < lo) lo = s;
      if (e != null && e > hi) hi = e;
      if (s != null && s > hi) hi = s; // running task w/o end
    }
    for (const ev of (chain.events || [])) {
      const ts = toMs(ev.timestamp_ms || ev.timestamp);
      if (ts != null) {
        if (ts < lo) lo = ts;
        if (ts > hi) hi = ts;
      }
    }
    const cs = toMs(chain.started_at);
    const ce = toMs(chain.ended_at);
    if (cs != null && cs < lo) lo = cs;
    if (ce != null && ce > hi) hi = ce;
    if (!isFinite(lo)) lo = Date.now();
    if (!isFinite(hi)) hi = lo;
    if (hi <= lo) hi = lo + 1000;  // avoid div0; 1s minimum span
    return { lo, hi, span: hi - lo };
  }

  // Render the swimlane SVG into `host`.
  function renderSwimlane(host, chain) {
    if (!host) return;
    while (host.firstChild) host.removeChild(host.firstChild);

    if (!chain || !chain.tasks || chain.tasks.length === 0) {
      host.appendChild(htmlEl("div", {
        class: "chain-swim-empty mono",
        text: "no tasks in this chain — backend may still be assembling it",
      }));
      return;
    }

    const agents = deriveAgentRows(chain);
    if (agents.length === 0) {
      host.appendChild(htmlEl("div", {
        class: "chain-swim-empty mono",
        text: "no agents recorded in chain",
      }));
      return;
    }

    const bounds = computeTimeBounds(chain);
    const innerWidth = VB.width - VB.leftPad - VB.rightPad;
    const totalH = VB.topPad + agents.length * VB.rowHeight + VB.bottomPad;

    const xFor = (ms) => {
      if (ms == null) return VB.leftPad;
      const t = (ms - bounds.lo) / bounds.span;
      return VB.leftPad + Math.max(0, Math.min(1, t)) * innerWidth;
    };
    const yForRow = (idx) => VB.topPad + idx * VB.rowHeight + VB.rowHeight / 2;

    // Header line: chain id · hops · duration · status
    const dur = fmtDur(
      (toMs(chain.ended_at) || bounds.hi) - (toMs(chain.started_at) || bounds.lo)
    );
    const statusGlyph = chainStatusGlyph(chain.status);
    host.appendChild(htmlEl("div", { class: "chain-swim-header" }, [
      htmlEl("div", { class: "csh-title mono" }, [
        htmlEl("span", { class: "csh-id", text: "chain " + shortId(chain.chain_id) }),
        htmlEl("span", { class: "csh-sep", text: " · " }),
        htmlEl("span", { class: "csh-hops", text: (chain.hops || agents.length) + " hops" }),
        htmlEl("span", { class: "csh-sep", text: " · " }),
        htmlEl("span", { class: "csh-dur", text: dur }),
        htmlEl("span", { class: "csh-sep", text: " · " }),
        htmlEl("span", {
          class: "csh-status status-" + (chain.status || "unknown").toLowerCase(),
          text: statusGlyph + " " + (chain.status || "unknown"),
        }),
      ]),
      htmlEl("div", { class: "csh-meta mono" }, [
        htmlEl("span", { text: fmtClock(bounds.lo) + " → " + fmtClock(bounds.hi) }),
      ]),
    ]));

    // SVG container.
    const wrap = htmlEl("div", { class: "chain-swim-wrap" });
    host.appendChild(wrap);

    const svg = svgEl("svg", {
      class: "chain-swim-svg",
      viewBox: `0 0 ${VB.width} ${totalH}`,
      "preserveAspectRatio": "xMidYMid meet",
      role: "img",
      "aria-label": `Swimlane for chain ${shortId(chain.chain_id)} across ${agents.length} agents`,
    });
    wrap.appendChild(svg);

    // <defs> for striped pattern (failed) and arrow heads per agent.
    const defs = svgEl("defs");
    defs.appendChild(failPattern());
    for (const a of agents) {
      defs.appendChild(arrowHead(`csarr-${cssIdent(a)}`, agentColor(a)));
    }
    defs.appendChild(arrowHead("csarr-default", FALLBACK_AGENT_COLOR));
    svg.appendChild(defs);

    // Background row stripes + agent labels.
    for (let i = 0; i < agents.length; i++) {
      const a = agents[i];
      const y = VB.topPad + i * VB.rowHeight;
      svg.appendChild(svgEl("rect", {
        class: "csw-row" + (i % 2 ? " odd" : ""),
        x: 0,
        y,
        width: VB.width,
        height: VB.rowHeight,
      }));
      // agent label
      const lbl = svgEl("text", {
        class: "csw-row-label mono",
        x: VB.leftPad - 12,
        y: y + VB.rowHeight / 2 + 4,
        "text-anchor": "end",
        fill: agentColor(a),
        text: a,
      });
      svg.appendChild(lbl);
      // tiny dot
      svg.appendChild(svgEl("circle", {
        class: "csw-row-dot",
        cx: VB.leftPad - 6,
        cy: y + VB.rowHeight / 2,
        r: 3,
        fill: agentColor(a),
      }));
    }

    // Vertical gridlines (4 ticks).
    const ticks = 4;
    for (let t = 0; t <= ticks; t++) {
      const x = VB.leftPad + (innerWidth * t) / ticks;
      svg.appendChild(svgEl("line", {
        class: "csw-grid",
        x1: x, x2: x,
        y1: VB.topPad,
        y2: VB.topPad + agents.length * VB.rowHeight,
      }));
    }

    // Tasks: bars.
    const taskById = new Map();
    for (const t of (chain.tasks || [])) {
      if (t.task_id) taskById.set(t.task_id, t);
      const rowIdx = agents.indexOf(t.agent_id || "(unknown)");
      if (rowIdx < 0) continue;
      const startMs = toMs(t.started_at) || bounds.lo;
      const endMsRaw = toMs(t.ended_at);
      const endMs = endMsRaw != null ? endMsRaw : bounds.hi;
      const x1 = xFor(startMs);
      const x2 = Math.max(xFor(endMs), x1 + 4);   // min visual width
      const y = VB.topPad + rowIdx * VB.rowHeight + (VB.rowHeight - VB.barHeight) / 2;
      const status = (t.status || "").toLowerCase();
      const color = agentColor(t.agent_id);

      const g = svgEl("g", {
        class: "csw-task" +
          (status ? " status-" + status : "") +
          (endMsRaw == null ? " is-running" : ""),
        "data-task-id": t.task_id || "",
      });

      // Base bar
      g.appendChild(svgEl("rect", {
        class: "csw-bar",
        x: x1,
        y,
        width: x2 - x1,
        height: VB.barHeight,
        rx: 4,
        ry: 4,
        fill: color,
        "fill-opacity": status === "completed" || status === "succeeded" ? 0.92 : 0.78,
        stroke: color,
        "stroke-opacity": 0.9,
      }));

      // Status overlay
      if (status === "failed") {
        g.appendChild(svgEl("rect", {
          class: "csw-bar-overlay fail",
          x: x1, y, width: x2 - x1, height: VB.barHeight,
          rx: 4, ry: 4,
          fill: "url(#csw-fail-stripes)",
        }));
      } else if (endMsRaw == null || status === "running" || status === "in_progress") {
        const pulse = svgEl("rect", {
          class: "csw-bar-overlay pulse",
          x: x1, y, width: x2 - x1, height: VB.barHeight,
          rx: 4, ry: 4,
          fill: color,
          "fill-opacity": 0.0,
        });
        g.appendChild(pulse);
      }

      // Status pip (right edge)
      g.appendChild(svgEl("circle", {
        class: "csw-bar-pip",
        cx: x2 - 5, cy: y + 5, r: 2.5,
        fill: statusColor(status || (endMsRaw == null ? "running" : "completed")),
      }));

      // Click handler -> popover
      g.addEventListener("click", (ev) => {
        ev.stopPropagation();
        showTaskPopover(host, wrap, t, x2, y);
      });

      svg.appendChild(g);
    }

    // Events: arrows (handoff edges).
    const events = Array.isArray(chain.events) ? chain.events : [];
    for (const ev of events) {
      const k = (ev.kind || "").toLowerCase();
      if (k && !(
        k === "handoff.initiated" || k === "handoff.accepted" ||
        k === "dispatch.requested" || k === "dispatch.accepted" ||
        k === "dispatch.fallback")) continue;

      const fromTask = ev.parent_task_id ? taskById.get(ev.parent_task_id) : null;
      const toTask   = ev.child_task_id  ? taskById.get(ev.child_task_id)  : null;
      const fromAgent = (fromTask && fromTask.agent_id) || ev.agent_id;
      const toAgent   = (toTask   && toTask.agent_id)   || ev.to_agent;
      if (!fromAgent || !toAgent) continue;

      const fromIdx = agents.indexOf(fromAgent);
      const toIdx   = agents.indexOf(toAgent);
      if (fromIdx < 0 || toIdx < 0) continue;
      if (fromIdx === toIdx) continue;  // self-loops not drawn

      const ts = toMs(ev.timestamp_ms || ev.timestamp);
      const x = ts != null ? xFor(ts)
              : (fromTask && toMs(fromTask.ended_at) != null) ? xFor(toMs(fromTask.ended_at))
              : (toTask && toMs(toTask.started_at) != null) ? xFor(toMs(toTask.started_at))
              : VB.leftPad;
      const y1 = yForRow(fromIdx) + 4;   // exit just below center of bar
      const y2 = yForRow(toIdx) - 6;     // arrive just above center of dest bar
      const midY = (y1 + y2) / 2;
      const path = `M ${x.toFixed(1)} ${y1.toFixed(1)} ` +
                   `C ${x.toFixed(1)} ${midY.toFixed(1)}, ` +
                   `${(x + 14).toFixed(1)} ${midY.toFixed(1)}, ` +
                   `${(x + 14).toFixed(1)} ${y2.toFixed(1)}`;
      const arrowId = `csarr-${cssIdent(fromAgent)}`;
      svg.appendChild(svgEl("path", {
        class: "csw-edge",
        d: path,
        fill: "none",
        stroke: agentColor(fromAgent),
        "stroke-width": 1.4,
        "stroke-opacity": 0.85,
        "marker-end": `url(#${arrowId})`,
      }));
    }

    // Time axis.
    const axisY = VB.topPad + agents.length * VB.rowHeight + 18;
    svg.appendChild(svgEl("line", {
      class: "csw-axis",
      x1: VB.leftPad, x2: VB.leftPad + innerWidth,
      y1: axisY, y2: axisY,
    }));
    for (let t = 0; t <= ticks; t++) {
      const x = VB.leftPad + (innerWidth * t) / ticks;
      svg.appendChild(svgEl("line", {
        class: "csw-axis-tick",
        x1: x, x2: x, y1: axisY, y2: axisY + 4,
      }));
      const ms = bounds.lo + (bounds.span * t) / ticks;
      svg.appendChild(svgEl("text", {
        class: "csw-axis-label mono",
        x, y: axisY + 16,
        "text-anchor": t === 0 ? "start" : t === ticks ? "end" : "middle",
        fill: "#8b9bab",
        text: t === 0 ? fmtClock(ms)
              : t === ticks ? fmtClock(ms) + "  (+" + fmtDur(bounds.span) + ")"
              : "+" + fmtDur((bounds.span * t) / ticks),
      }));
    }

    // Outside click closes popovers.
    wrap.addEventListener("click", () => closePopovers(host));

    // Hint line.
    host.appendChild(htmlEl("div", {
      class: "chain-swim-hint mono",
      text: "click a bar to see task details · esc closes",
    }));
  }

  function chainStatusGlyph(s) {
    s = (s || "").toLowerCase();
    if (s === "completed" || s === "succeeded") return "✓";
    if (s === "failed") return "🔴";
    if (s === "running" || s === "in_progress") return "●";
    if (s === "cancelled") return "○";
    return "·";
  }

  function failPattern() {
    const pat = svgEl("pattern", {
      id: "csw-fail-stripes",
      patternUnits: "userSpaceOnUse",
      width: 6,
      height: 6,
      patternTransform: "rotate(45)",
    });
    pat.appendChild(svgEl("rect", {
      x: 0, y: 0, width: 6, height: 6, fill: "rgba(248,81,73,0.0)",
    }));
    pat.appendChild(svgEl("line", {
      x1: 0, y1: 0, x2: 0, y2: 6,
      stroke: "#f85149", "stroke-width": 2, "stroke-opacity": 0.55,
    }));
    return pat;
  }

  function arrowHead(id, color) {
    const m = svgEl("marker", {
      id, viewBox: "0 0 8 8",
      refX: 7, refY: 4,
      markerWidth: 7, markerHeight: 7,
      orient: "auto-start-reverse",
    });
    m.appendChild(svgEl("path", {
      d: "M0,0 L8,4 L0,8 Z",
      fill: color,
      "fill-opacity": 0.9,
    }));
    return m;
  }

  function cssIdent(s) {
    return String(s || "default").replace(/[^a-zA-Z0-9_-]/g, "_");
  }

  // ---- Task popover -----------------------------------------------------

  function closePopovers(host) {
    if (!host) return;
    const pops = host.querySelectorAll(".csw-popover");
    pops.forEach((p) => p.parentNode && p.parentNode.removeChild(p));
  }

  function showTaskPopover(host, wrap, task, anchorVbX, anchorVbY) {
    closePopovers(host);
    // Convert viewBox coords to wrap-relative px.
    const rect = wrap.getBoundingClientRect();
    if (rect.width === 0) return;
    const scale = rect.width / VB.width;
    let leftPx = anchorVbX * scale + 12;
    const topPx = anchorVbY * scale - 4;

    const pop = htmlEl("div", { class: "csw-popover" }, [
      htmlEl("div", { class: "csw-pop-head" }, [
        htmlEl("span", {
          class: "csw-pop-agent mono",
          style: { color: agentColor(task.agent_id) },
          text: task.agent_id || "?",
        }),
        htmlEl("span", {
          class: "csw-pop-status mono status-" + (task.status || "").toLowerCase(),
          text: task.status || "—",
        }),
        htmlEl("button", {
          class: "csw-pop-close",
          "aria-label": "close",
          text: "×",
          onclick: (e) => { e.stopPropagation(); closePopovers(host); },
        }),
      ]),
      htmlEl("div", { class: "csw-pop-goal", text: task.goal_preview || "(no goal preview)" }),
      htmlEl("dl", { class: "csw-pop-dl mono" }, [
        htmlEl("dt", { text: "task" }),
        htmlEl("dd", {}, [
          task.task_id
            ? htmlEl("a", {
                href: "/static/agents.html?task=" + encodeURIComponent(task.task_id),
                text: shortId(task.task_id),
                title: task.task_id,
              })
            : document.createTextNode("—"),
        ]),
        htmlEl("dt", { text: "started" }),
        htmlEl("dd", { text: fmtClock(toMs(task.started_at)) }),
        htmlEl("dt", { text: "ended" }),
        htmlEl("dd", { text: task.ended_at ? fmtClock(toMs(task.ended_at)) : "running" }),
        task.parent_task_id ? htmlEl("dt", { text: "parent" }) : null,
        task.parent_task_id ? htmlEl("dd", {
          text: shortId(task.parent_task_id),
          title: task.parent_task_id,
        }) : null,
      ]),
    ]);
    pop.style.position = "absolute";
    pop.style.top = `${Math.max(8, topPx)}px`;
    pop.style.left = `${Math.max(8, leftPx)}px`;
    pop.addEventListener("click", (e) => e.stopPropagation());
    wrap.appendChild(pop);

    // Nudge into viewport if it overflows right.
    const popRect = pop.getBoundingClientRect();
    if (popRect.right > rect.right - 8) {
      const overflow = popRect.right - (rect.right - 8);
      pop.style.left = `${Math.max(8, leftPx - overflow)}px`;
    }
  }

  // ---- Public API -------------------------------------------------------

  window.exocortexChains = {
    renderSwimlane,
    agentColor,
    chainStatusGlyph,
    fmtDur,
    shortId,
    toMs,
  };
})();
