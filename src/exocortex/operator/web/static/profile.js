// profile.js — /static/profile.html
//
// Renders the user's view of what the exocortex has learned about *them*
// (preferences, skills, goals, constraints, routines, communication style,
// relationships, values), plus a queue of "questions exocortex would like
// to ask you" that the user can answer or skip.
//
// All endpoints can 404/503 — backend may not yet be live. We render
// graceful empty states rather than throwing, mirroring memchat.js.
//
// Endpoints:
//   GET  /api/profile
//   POST /api/profile/redact          { record_id }
//   GET  /api/profile/questions
//   POST /api/profile/answer          { question_id, answer }
//   GET  /api/settings/profile_freeze
//   POST /api/settings/profile_freeze/toggle
//
// Live updates: the existing /api/events WebSocket carries
//   profile.observed   → re-fetch /api/profile
//   profile.questioned → re-fetch /api/profile/questions

(function () {
  "use strict";

  // ---------------------------------------------------------------------
  // Constants
  // ---------------------------------------------------------------------

  const SECTIONS_LS_KEY = "exocortex.profile.sections.v1";
  const POLL_PROFILE_MS = 30_000;
  const POLL_FREEZE_MS = 8_000;
  const REDACT_FLASH_MS = 300;

  // Canonical section order and metadata. Anything in `sections` whose `type`
  // is "profile.<x>" but not in this map falls into the OTHER catch-all.
  const SECTION_DEFS = [
    {
      key: "profile.preference",
      label: "PREFERENCES",
      sing: "preference",
      hint: "no preferences recorded yet — agents will populate this as they observe you, or seed manually with `precog memory write --type profile.preference --scope user --scope-id <your-id> \"…\"`",
    },
    {
      key: "profile.skill",
      label: "SKILLS",
      sing: "skill",
      hint: "no skills recorded yet — exocortex builds this from observed work patterns, or `precog memory write --type profile.skill --scope user --scope-id <your-id> \"…\"`",
    },
    {
      key: "profile.goal",
      label: "GOALS",
      sing: "goal",
      hint: "no goals recorded yet — mention long-term goals to any agent, or `precog memory write --type profile.goal --scope user --scope-id <your-id> \"…\"`",
    },
    {
      key: "profile.constraint",
      label: "CONSTRAINTS",
      sing: "constraint",
      hint: "no constraints recorded yet — these are hard limits like time, tools, environments. Add with `precog memory write --type profile.constraint --scope user --scope-id <your-id> \"…\"`",
    },
    {
      key: "profile.routine",
      label: "ROUTINES",
      sing: "routine",
      hint: "no routines recorded yet — when do you work, in what order, with what cadence? `precog memory write --type profile.routine --scope user --scope-id <your-id> \"…\"`",
    },
    {
      key: "profile.communication_style",
      label: "COMMUNICATION",
      sing: "communication style",
      hint: "no communication-style notes yet — terse vs verbose, prose vs lists, etc. `precog memory write --type profile.communication_style --scope user --scope-id <your-id> \"…\"`",
    },
    {
      key: "profile.relationship",
      label: "RELATIONSHIPS",
      sing: "relationship",
      hint: "no relationships recorded yet — people you collaborate with regularly. `precog memory write --type profile.relationship --scope user --scope-id <your-id> \"…\"`",
    },
    {
      key: "profile.value",
      label: "VALUES",
      sing: "value",
      hint: "no values recorded yet — what do you optimize for, what's non-negotiable? `precog memory write --type profile.value --scope user --scope-id <your-id> \"…\"`",
    },
  ];
  const SECTION_KEYS = SECTION_DEFS.map((d) => d.key);

  const AGENT_COLORS = {
    codex: "#58a6ff",
    hermes: "#d29922",
    claude: "#7ee787",
    claude_code: "#7ee787",
    openclaw: "#bb6bd9",
    operator: "#e6edf3",
  };
  // Canonical unknown-agent color (was #8b9bab, inconsistent with other pages). D1.
  const FALLBACK_AGENT_COLOR = (window.Exo && Exo.FALLBACK_AGENT_COLOR) || "#8b949e";

  // ---------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------

  const state = {
    profileAvailable: true,           // /api/profile reachable
    profileLoaded: false,
    userId: "operator",
    sections: [],                     // [{type, count, items: [...]}, ...]
    collapsedSections: {},            // {section_key: true}

    questionsAvailable: true,
    questionsLoaded: false,
    questions: [],                    // [{id, content, dimension, asked_at, status}]
    skipped: {},                      // {question_id: true} client-side dismissals

    freezeAvailable: true,
    frozen: false,

    pollProfileTimer: null,
    pollFreezeTimer: null,

    // Per-row interaction state
    redactPending: {},                // {record_id: true} mid-confirm
    answerPending: {},                // {question_id: true} mid-submit
  };

  // ---------------------------------------------------------------------
  // Tiny utilities
  // ---------------------------------------------------------------------

  const $ = (id) => document.getElementById(id);

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

  function agentColor(agentId) {
    if (!agentId) return FALLBACK_AGENT_COLOR;
    return AGENT_COLORS[agentId] || FALLBACK_AGENT_COLOR;
  }

  function shortId(id) {
    if (!id) return "—";
    const s = String(id);
    if (s.length <= 8) return s;
    return s.slice(0, 8);
  }

  function relativeTime(ts) {
    if (!ts) return "—";
    let ms;
    if (typeof ts === "number") {
      // Heuristic: if it looks like seconds (10-digit), upgrade to ms
      ms = ts < 1e12 ? ts * 1000 : ts;
    } else {
      ms = Date.parse(ts);
      if (isNaN(ms)) return String(ts);
    }
    const diff = Date.now() - ms;
    if (diff < 0) return "just now";
    const sec = Math.floor(diff / 1000);
    if (sec < 60) return sec + "s ago";
    const min = Math.floor(sec / 60);
    if (min < 60) return min + "m ago";
    const hr = Math.floor(min / 60);
    if (hr < 24) return hr + "h ago";
    const day = Math.floor(hr / 24);
    if (day < 30) return day + "d ago";
    const mo = Math.floor(day / 30);
    if (mo < 12) return mo + "mo ago";
    const yr = Math.floor(day / 365);
    return yr + "y ago";
  }

  // Decide whether an evidence id points at an event (link to /agents) or
  // a memory record (link to /memory#focus=<id>). Heuristic only — event
  // ids in this codebase tend to look like "evt_…" or 26-32 char ulid-ish
  // strings starting with a timestamp prefix, while memory record ids have
  // a "rec_" or "mem_" prefix. Fall through to /memory.
  function evidenceHref(id) {
    if (!id) return null;
    const s = String(id);
    if (/^evt[_-]/i.test(s)) {
      return "/static/agents.html?event=" + encodeURIComponent(s);
    }
    return "/memory#focus=" + encodeURIComponent(s);
  }

  // ---------------------------------------------------------------------
  // Section collapse persistence
  // ---------------------------------------------------------------------

  function loadCollapsed() {
    try {
      const raw = localStorage.getItem(SECTIONS_LS_KEY);
      if (!raw) return;
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object") {
        state.collapsedSections = parsed;
      }
    } catch (_) { /* ignore */ }
  }

  function saveCollapsed() {
    try {
      localStorage.setItem(SECTIONS_LS_KEY, JSON.stringify(state.collapsedSections));
    } catch (_) { /* ignore */ }
  }

  // ---------------------------------------------------------------------
  // Freeze pill
  // ---------------------------------------------------------------------

  function setFreezeVisual() {
    const btn = $("profile-freeze");
    const stateLabel = $("profile-freeze-state");
    const banner = $("pm-frozen-banner");
    if (!btn || !stateLabel) return;
    btn.classList.remove("live", "frozen", "unavailable");
    if (!state.freezeAvailable) {
      btn.classList.add("unavailable");
      stateLabel.textContent = "UNAVAIL";
      btn.title = "profile freeze backend not ready";
      if (banner) banner.hidden = true;
      return;
    }
    if (state.frozen) {
      btn.classList.add("frozen");
      stateLabel.textContent = "FROZEN";
      btn.title = "profile collection frozen — click to resume";
      if (banner) banner.hidden = false;
    } else {
      btn.classList.add("live");
      stateLabel.textContent = "LIVE";
      btn.title = "profile collection live — click to freeze";
      if (banner) banner.hidden = true;
    }
  }

  async function fetchFreeze() {
    try {
      const r = await fetch("/api/settings/profile_freeze");
      if (!r.ok) {
        if (r.status === 404 || r.status === 503) {
          state.freezeAvailable = false;
        }
        setFreezeVisual();
        return;
      }
      const data = await r.json();
      state.freezeAvailable = true;
      state.frozen = !!data.frozen;
    } catch (_) {
      state.freezeAvailable = false;
    }
    setFreezeVisual();
  }

  async function toggleFreeze() {
    if (!state.freezeAvailable) return;
    try {
      const r = await fetch("/api/settings/profile_freeze/toggle", { method: "POST" });
      if (!r.ok) {
        if (r.status === 404 || r.status === 503) state.freezeAvailable = false;
        setFreezeVisual();
        return;
      }
      const data = await r.json();
      state.frozen = !!data.frozen;
    } catch (_) {
      state.freezeAvailable = false;
    }
    setFreezeVisual();
  }

  // ---------------------------------------------------------------------
  // Profile fetch + render
  // ---------------------------------------------------------------------

  async function fetchProfile() {
    try {
      const r = await fetch("/api/profile");
      if (!r.ok) {
        if (r.status === 404 || r.status === 503) {
          state.profileAvailable = false;
          state.profileLoaded = true;
          renderProfile();
          return;
        }
        return;
      }
      const data = await r.json();
      state.profileAvailable = true;
      state.profileLoaded = true;
      state.userId = data.user_id || state.userId || "operator";
      if (typeof data.frozen === "boolean") {
        // /api/profile may also report freeze state; defer to dedicated freeze
        // endpoint when available, but use this as a fallback.
        if (!state.freezeAvailable) {
          state.frozen = data.frozen;
          setFreezeVisual();
        }
      }
      state.sections = Array.isArray(data.sections) ? data.sections : [];
      renderProfile();
    } catch (_) {
      state.profileAvailable = false;
      state.profileLoaded = true;
      renderProfile();
    }
  }

  function sectionItemsOfType(type) {
    if (!Array.isArray(state.sections)) return [];
    for (const s of state.sections) {
      if (!s || s.type !== type) continue;
      return Array.isArray(s.items) ? s.items : [];
    }
    return [];
  }

  function renderProfile() {
    const host = $("pm-sections");
    const userIdEl = $("profile-user-id");
    const metaEl = $("profile-meta");
    if (!host || !userIdEl || !metaEl) return;

    userIdEl.textContent = state.userId || "—";

    if (!state.profileAvailable) {
      host.innerHTML = "";
      host.appendChild(el("div", {
        class: "pm-empty mono",
      }, [
        "profile backend not ready — will retry. ",
        "agents will populate this as they observe you.",
      ]));
      metaEl.textContent = "backend offline · retrying";
      return;
    }

    if (!state.profileLoaded) {
      host.innerHTML = "";
      host.appendChild(el("div", { class: "pm-loading mono", text: "loading profile…" }));
      metaEl.textContent = "loading…";
      return;
    }

    let totalItems = 0;
    for (const s of state.sections) {
      totalItems += Array.isArray(s.items) ? s.items.length : 0;
    }
    metaEl.textContent =
      `${totalItems} record${totalItems === 1 ? "" : "s"} across ${SECTION_DEFS.length} dimension${SECTION_DEFS.length === 1 ? "" : "s"}`;

    host.innerHTML = "";

    // Render canonical sections in defined order
    for (const def of SECTION_DEFS) {
      const items = sectionItemsOfType(def.key);
      host.appendChild(buildSection(def, items));
    }

    // OTHER catch-all
    const other = (state.sections || []).filter((s) => {
      return s && typeof s.type === "string" && s.type.startsWith("profile.")
        && SECTION_KEYS.indexOf(s.type) < 0;
    });
    if (other.length) {
      const merged = [];
      for (const s of other) {
        if (Array.isArray(s.items)) merged.push(...s.items);
      }
      const otherDef = {
        key: "profile.__other__",
        label: "OTHER",
        sing: "entry",
        hint: "uncategorized profile records — these don't fit a canonical dimension",
      };
      host.appendChild(buildSection(otherDef, merged));
    }
  }

  function buildSection(def, items) {
    const isCollapsed = !!state.collapsedSections[def.key];
    const isEmpty = !items || items.length === 0;
    const count = isEmpty ? 0 : items.length;

    const head = el("div", {
      class: "ps-head" + (isEmpty ? " empty" : "") + (isCollapsed ? " collapsed" : ""),
      onclick: () => {
        state.collapsedSections[def.key] = !isCollapsed;
        saveCollapsed();
        renderProfile();
      },
    }, [
      el("span", { class: "caret mono", text: isCollapsed ? "▸" : "▾" }),
      el("span", { class: "label", text: def.label }),
      el("span", { class: "count mono", text: "(" + count + ")" }),
    ]);

    const body = el("div", { class: "ps-body" });
    if (!isCollapsed) {
      if (isEmpty) {
        body.appendChild(el("div", { class: "ps-empty mono", text: def.hint }));
      } else {
        for (const item of items) {
          body.appendChild(buildItemRow(def, item));
        }
      }
    }

    return el("section", { class: "ps", "data-section": def.key }, [head, body]);
  }

  function buildItemRow(def, item) {
    const recordId = item.id || item.record_id || "";
    const source = item.source || item.source_agent || "—";
    const conf = item.confidence || "—";
    const ts = item.timestamp || item.created_at || item.ts || null;
    const tags = Array.isArray(item.tags) ? item.tags : [];
    const evidence = Array.isArray(item.evidence) ? item.evidence : [];
    const content = item.content || item.text || "";

    const row = el("div", {
      class: "pi-row",
      "data-record-id": recordId,
    });

    // Top metadata
    const meta = el("div", { class: "pi-meta mono" }, [
      el("span", { class: "pi-id", text: "id:" + shortId(recordId) }),
      el("span", { class: "pi-sep", text: "·" }),
      el("span", {
        class: "pi-source",
        style: { color: agentColor(source) },
        text: source,
      }),
      el("span", { class: "pi-sep", text: "·" }),
      buildConfidenceBadge(conf),
      el("span", { class: "pi-sep", text: "·" }),
      el("span", { class: "pi-time", text: relativeTime(ts), title: typeof ts === "string" ? ts : "" }),
    ]);
    row.appendChild(meta);

    // Body content
    row.appendChild(el("div", { class: "pi-body", text: content }));

    // Tags
    if (tags.length) {
      const tagRow = el("div", { class: "pi-tags mono" }, [
        el("span", { class: "k", text: "tags:" }),
        el("span", { class: "v", text: tags.join(", ") }),
      ]);
      row.appendChild(tagRow);
    }

    // Footer: evidence + redact
    const footer = el("div", { class: "pi-footer" });
    if (evidence.length) {
      const ev = evidence[0];
      const href = evidenceHref(ev);
      const chip = el("a", {
        class: "pi-evidence mono",
        href: href || "#",
        target: "_blank",
        rel: "noopener",
        title: "open evidence: " + ev,
      }, [
        "evidence: " + evidence.length,
        el("span", { class: "ext", text: " ↗" }),
      ]);
      if (!href) {
        chip.addEventListener("click", (e) => e.preventDefault());
      }
      footer.appendChild(chip);
    }

    const redactWrap = el("span", { class: "pi-redact-wrap" });
    redactWrap.appendChild(buildRedactControl(row, redactWrap, recordId));
    footer.appendChild(redactWrap);
    row.appendChild(footer);

    return row;
  }

  function buildConfidenceBadge(conf) {
    const cls = "pi-conf conf-" + String(conf || "unknown").replace(/[^a-z_]/gi, "_");
    return el("span", { class: cls, text: conf });
  }

  function buildRedactControl(row, wrap, recordId) {
    if (!recordId) {
      return el("span", { class: "pi-redact-disabled mono", text: "—", title: "no record id" });
    }
    const btn = el("button", {
      type: "button",
      class: "pi-redact mono",
      title: "redact this entry",
      onclick: () => {
        showRedactConfirm(row, wrap, recordId);
      },
    }, ["redact"]);
    return btn;
  }

  function showRedactConfirm(row, wrap, recordId) {
    wrap.innerHTML = "";
    const yes = el("button", {
      type: "button",
      class: "pi-redact-yes mono",
      onclick: () => doRedact(row, wrap, recordId),
    }, ["yes"]);
    const cancel = el("button", {
      type: "button",
      class: "pi-redact-cancel mono",
      onclick: () => {
        wrap.innerHTML = "";
        wrap.appendChild(buildRedactControl(row, wrap, recordId));
      },
    }, ["cancel"]);
    wrap.appendChild(el("span", { class: "pi-redact-prompt mono", text: "redact this entry?" }));
    wrap.appendChild(yes);
    wrap.appendChild(cancel);
  }

  async function doRedact(row, wrap, recordId) {
    if (state.redactPending[recordId]) return;
    state.redactPending[recordId] = true;
    wrap.innerHTML = "";
    wrap.appendChild(el("span", { class: "pi-redact-prompt mono", text: "redacting…" }));
    try {
      const r = await fetch("/api/profile/redact", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ record_id: recordId }),
      });
      if (r.ok) {
        // Flash strikethrough, then remove and update counts.
        row.classList.add("redacting");
        setTimeout(() => {
          // Remove from local sections cache so re-render counts are correct.
          for (const s of state.sections) {
            if (!Array.isArray(s.items)) continue;
            const idx = s.items.findIndex(
              (it) => (it.id || it.record_id) === recordId
            );
            if (idx >= 0) {
              s.items.splice(idx, 1);
              if (typeof s.count === "number") s.count = Math.max(0, s.count - 1);
            }
          }
          delete state.redactPending[recordId];
          renderProfile();
        }, REDACT_FLASH_MS);
      } else {
        delete state.redactPending[recordId];
        row.classList.add("redact-error");
        setTimeout(() => row.classList.remove("redact-error"), REDACT_FLASH_MS);
        wrap.innerHTML = "";
        wrap.appendChild(buildRedactControl(row, wrap, recordId));
      }
    } catch (_) {
      delete state.redactPending[recordId];
      row.classList.add("redact-error");
      setTimeout(() => row.classList.remove("redact-error"), REDACT_FLASH_MS);
      wrap.innerHTML = "";
      wrap.appendChild(buildRedactControl(row, wrap, recordId));
    }
  }

  // ---------------------------------------------------------------------
  // Questions fetch + render
  // ---------------------------------------------------------------------

  async function fetchQuestions() {
    try {
      const r = await fetch("/api/profile/questions");
      if (!r.ok) {
        if (r.status === 404 || r.status === 503) {
          state.questionsAvailable = false;
          state.questionsLoaded = true;
          renderQuestions();
          return;
        }
        return;
      }
      const data = await r.json();
      state.questionsAvailable = true;
      state.questionsLoaded = true;
      state.questions = Array.isArray(data.items) ? data.items : [];
      renderQuestions();
    } catch (_) {
      state.questionsAvailable = false;
      state.questionsLoaded = true;
      renderQuestions();
    }
  }

  function openQuestions() {
    return state.questions.filter((q) => {
      if (!q || q.status === "answered") return false;
      if (state.skipped[q.id]) return false;
      return true;
    });
  }

  function renderQuestions() {
    const host = $("pq-list");
    const empty = $("pq-empty");
    const countEl = $("pq-count");
    if (!host || !countEl) return;

    const open = openQuestions();
    countEl.textContent = String(open.length);

    if (!state.questionsAvailable) {
      host.innerHTML = "";
      host.appendChild(el("div", {
        class: "pq-empty mono",
      }, ["questions backend not ready — will retry"]));
      return;
    }

    if (!state.questionsLoaded) {
      host.innerHTML = "";
      host.appendChild(el("div", { class: "pq-empty mono", text: "loading…" }));
      return;
    }

    if (open.length === 0) {
      host.innerHTML = "";
      host.appendChild(el("div", { class: "pq-empty mono" }, [
        "no questions right now — exocortex will surface them as it learns you. ",
        "Try ",
        el("span", { class: "cmd", text: "precog profile question" }),
        " to force-poll.",
      ]));
      return;
    }

    host.innerHTML = "";
    const expanded = open.slice(0, 3);
    const collapsed = open.slice(3);

    for (const q of expanded) {
      host.appendChild(buildQuestionCard(q, true));
    }

    if (collapsed.length) {
      const collapsedHead = el("div", { class: "pq-older-head mono" }, [
        "older questions (" + collapsed.length + ")",
      ]);
      host.appendChild(collapsedHead);
      const list = el("div", { class: "pq-older-list" });
      for (const q of collapsed) {
        list.appendChild(buildQuestionCard(q, false));
      }
      host.appendChild(list);
    }
  }

  function buildQuestionCard(q, expanded) {
    const id = q.id || "";
    const dim = q.dimension || "profile.unknown";
    const ts = q.asked_at || q.timestamp || null;

    if (!expanded) {
      // Single-line collapsed row
      const row = el("div", {
        class: "pq-card collapsed",
        onclick: () => {
          // Move this question to expanded by reordering — for simplicity,
          // just remove the skip flag if any and re-render expanded form
          // by promoting it to the top.
          promoteQuestion(id);
        },
      }, [
        el("span", { class: "pq-glyph mono", text: "[?]" }),
        el("span", { class: "pq-line", text: q.content || "(no content)" }),
        el("span", { class: "pq-time mono", text: relativeTime(ts) }),
      ]);
      return row;
    }

    const isPending = !!state.answerPending[id];

    const card = el("div", { class: "pq-card", "data-question-id": id });

    const head = el("div", { class: "pq-head-line mono" }, [
      el("span", { class: "pq-glyph", text: "[?]" }),
      el("span", { class: "pq-dim", text: "dimension:" + dim }),
      el("span", { class: "pq-sep", text: "·" }),
      el("span", { class: "pq-time", text: "asked " + relativeTime(ts) }),
    ]);
    card.appendChild(head);

    const body = el("div", { class: "pq-body", text: q.content || "(no content)" });
    card.appendChild(body);

    const ta = el("textarea", {
      class: "pq-textarea mono",
      rows: "2",
      placeholder: "your answer…",
      spellcheck: "false",
      autocomplete: "off",
    });
    ta.addEventListener("input", () => autosizeTextarea(ta));
    ta.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        e.preventDefault();
        submitAnswer(card, q, ta.value);
      } else if (e.key === "Escape") {
        ta.value = "";
        autosizeTextarea(ta);
      }
    });
    card.appendChild(ta);

    const row = el("div", { class: "pq-actions" });
    const answerBtn = el("button", {
      type: "button",
      class: "pq-answer",
      disabled: isPending ? "disabled" : null,
      onclick: () => submitAnswer(card, q, ta.value),
    }, [isPending ? "saving…" : "answer"]);
    const skipBtn = el("button", {
      type: "button",
      class: "pq-skip",
      onclick: () => {
        state.skipped[id] = true;
        renderQuestions();
      },
    }, ["skip"]);
    const hint = el("span", { class: "pq-hint mono", text: "Cmd+⏎ submit · Esc clear" });
    row.appendChild(answerBtn);
    row.appendChild(skipBtn);
    row.appendChild(hint);
    card.appendChild(row);

    return card;
  }

  function promoteQuestion(id) {
    // Find the question, move it to the front so it renders expanded.
    const idx = state.questions.findIndex((q) => q && q.id === id);
    if (idx < 0) return;
    const q = state.questions.splice(idx, 1)[0];
    state.questions.unshift(q);
    renderQuestions();
  }

  function autosizeTextarea(ta) {
    if (!ta) return;
    ta.style.height = "auto";
    const lineHeight = 20;
    const min = lineHeight * 2 + 14;
    const max = lineHeight * 6 + 14;
    const next = Math.max(min, Math.min(max, ta.scrollHeight));
    ta.style.height = next + "px";
  }

  async function submitAnswer(card, q, answerText) {
    const id = q.id;
    if (!id || !answerText || !answerText.trim()) return;
    if (state.answerPending[id]) return;
    state.answerPending[id] = true;

    // Disable controls
    const btn = card.querySelector(".pq-answer");
    const ta = card.querySelector(".pq-textarea");
    const skip = card.querySelector(".pq-skip");
    if (btn) { btn.disabled = true; btn.textContent = "saving…"; }
    if (ta) ta.disabled = true;
    if (skip) skip.disabled = true;

    try {
      const r = await fetch("/api/profile/answer", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ question_id: id, answer: answerText.trim() }),
      });
      if (r.ok) {
        let data = {};
        try { data = await r.json(); } catch (_) { /* ignore */ }
        const newRecordId = data.new_record_id || null;
        // Mark answered locally
        for (const qq of state.questions) {
          if (qq && qq.id === id) qq.status = "answered";
        }
        delete state.answerPending[id];
        // Replace card content with success state
        card.innerHTML = "";
        const ok = el("div", { class: "pq-success mono" }, [
          el("span", { class: "check", text: "✓" }),
          el("span", { class: "msg" }, [
            "saved → ",
            el("a", {
              class: "pq-jump",
              href: "#",
              onclick: (e) => {
                e.preventDefault();
                jumpToRecord(newRecordId, q.dimension);
              },
            }, [dimensionLabel(q.dimension)]),
          ]),
        ]);
        card.appendChild(ok);
        // Refresh profile after a short delay so the new record appears.
        setTimeout(() => fetchProfile(), 400);
        // Remove this question from view after a moment.
        setTimeout(() => {
          state.questions = state.questions.filter((qq) => !(qq && qq.id === id));
          renderQuestions();
        }, 2400);
      } else {
        delete state.answerPending[id];
        if (btn) { btn.disabled = false; btn.textContent = "answer"; }
        if (ta) ta.disabled = false;
        if (skip) skip.disabled = false;
        flashError(card, r.status === 404 || r.status === 503
          ? "answer endpoint not ready"
          : "save failed (HTTP " + r.status + ")");
      }
    } catch (_) {
      delete state.answerPending[id];
      if (btn) { btn.disabled = false; btn.textContent = "answer"; }
      if (ta) ta.disabled = false;
      if (skip) skip.disabled = false;
      flashError(card, "network error");
    }
  }

  function flashError(card, msg) {
    let err = card.querySelector(".pq-error");
    if (!err) {
      err = el("div", { class: "pq-error mono" });
      card.appendChild(err);
    }
    err.textContent = msg;
    card.classList.add("error");
    setTimeout(() => card.classList.remove("error"), 800);
  }

  function dimensionLabel(dim) {
    const def = SECTION_DEFS.find((d) => d.key === dim);
    return def ? def.label.toLowerCase() : (dim || "profile");
  }

  function jumpToRecord(recordId, dim) {
    // Scroll the right column to the section corresponding to the dimension.
    if (!dim) return;
    const sect = document.querySelector('[data-section="' + dim + '"]');
    if (sect && sect.scrollIntoView) {
      sect.scrollIntoView({ behavior: "smooth", block: "start" });
      sect.classList.add("highlight");
      setTimeout(() => sect.classList.remove("highlight"), 800);
    }
  }

  // ---------------------------------------------------------------------
  // WebSocket — listen for profile.observed / profile.questioned
  // ---------------------------------------------------------------------

  let wsRefreshProfileTimer = null;
  let wsRefreshQuestionsTimer = null;

  function scheduleProfileRefresh() {
    if (wsRefreshProfileTimer) return;
    wsRefreshProfileTimer = setTimeout(() => {
      wsRefreshProfileTimer = null;
      fetchProfile();
    }, 400);
  }
  function scheduleQuestionsRefresh() {
    if (wsRefreshQuestionsTimer) return;
    wsRefreshQuestionsTimer = setTimeout(() => {
      wsRefreshQuestionsTimer = null;
      fetchQuestions();
    }, 400);
  }

  function connectEvents() {
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
        if (!event || !event.kind) return;
        if (event.kind === "__hello__") return;
        if (event.kind === "profile.observed") scheduleProfileRefresh();
        if (event.kind === "profile.questioned") scheduleQuestionsRefresh();
      });
    }
    open();
  }

  // ---------------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------------

  function bind() {
    const fz = $("profile-freeze");
    if (fz) fz.addEventListener("click", toggleFreeze);
  }

  function init() {
    loadCollapsed();
    bind();
    setFreezeVisual();
    renderProfile();
    renderQuestions();

    // Parallel initial load
    fetchProfile();
    fetchQuestions();
    fetchFreeze();

    // Polling
    if (state.pollProfileTimer == null) {
      state.pollProfileTimer = setInterval(() => {
        fetchProfile();
        fetchQuestions();
      }, POLL_PROFILE_MS);
    }
    if (state.pollFreezeTimer == null) {
      state.pollFreezeTimer = setInterval(fetchFreeze, POLL_FREEZE_MS);
    }

    // WS for live updates (best-effort; may 404 if backend not ready)
    connectEvents();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
