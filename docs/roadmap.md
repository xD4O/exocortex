# Exocortex roadmap

The full set of improvements scoped from the operator-UX brainstorm. Each
item has an effort estimate and a sequencing tier. Pick by tier; within a
tier, pick by which constraint you feel most.

**Effort scale:** S = ≤3 hours, M = half-day to full day, L = multi-day.

---

## Tier 0 — shipped or in-flight

| Item | Status | Notes |
|---|---|---|
| Shared memory store with provenance | ✅ | `memory_write` / `memory_search` / `memory_list` |
| Recall on session start | ✅ | `session_startup` MCP tool + Claude Code SessionStart hook + Hermes skill |
| Auto-capture fs/shell tools | ✅ | `fs_read`, `fs_list`, `shell_exec` |
| Agent dispatch (sync + async + cancel + status + wait + batch) | ✅ | `dispatch_task`, `dispatch_async`, `dispatch_status`, `dispatch_wait`, `dispatch_cancel`, `dispatch_batch` |
| Codex/Hermes real-binary integration | ✅ | bypass-approvals fix shipped |
| **Deduplication** | ✅ Sprint 1 | `memory_dedup_clusters` / `memory_merge` |
| **Right-to-forget** | ✅ Sprint 1 | `memory_forget` |
| **Parallel dispatch** | ✅ Sprint 1 | `dispatch_batch` |
| **Confidence promotion** | ✅ Sprint 2 | ≥3 distinct sources corroborate → bump confidence; CLI: `precog memory promote` |
| **Memory chat (RAG over memory)** | ✅ Sprint 2 | Local-first via Ollama, off by default. CLI: `precog chat-toggle`, `precog chat`. MCP: `memory_chat`. |
| **User-profile memory** | ✅ Sprint 3 | `MemoryScope.USER`, `profile.*` types, freeze toggle, question queue, `/profile` web page. CLI: `precog profile`. MCP: 5 tools. |
| **Agent voice-prefix in `session_startup`** | ✅ Sprint 3 | Communication-style + value records auto-load into every agent's first turn. |
| **Chain-of-custody on dispatches** | ✅ Sprint 4 | `from_agent` + `to_agent` on every `HANDOFF_INITIATED`. Auto-inferred from `parent_task_id`. claude_code → codex auto-fallback. `DISPATCH_FAILED` + `DISPATCH_FALLBACK` audit events. |
| **Dashboard rebuild** | ✅ Sprint 5 | Attention panel (failed dispatches / stuck tasks / pending approvals / Ollama down / tool denials), what's-happening, what's-grown, sparklines, density toggle, full state persistence. |
| **`/debug` page** | ✅ Sprint 5 | Failure triage: kind sidebar with counts, severity-coded rows, drawer with preceding events + operator hints. |
| **Handoff-chains panel + swimlane** | ✅ Sprint 5 | Dashboard panel with depth filter; click → SVG swimlane drawer with agent rows + colored task bars + Bézier handoff arrows. |
| **Multi-agent conversations** | ✅ Sprint 6 | `ConversationService` + `run_rounds` orchestrator. `/conversations` page with chat-bubble transcript, custom rounds (1-50), history archive group. CLI/MCP: 5 tools. |
| **`precog daemon`** | ✅ Sprint 6 | `start \| stop \| status` lifecycle wrapping the web server. PID + log management. |
| **Web UI** | ✅ | 7 pages: `/` dashboard, `/memory`, `/agents`, `/chat`, `/profile`, `/conversations`, `/debug`. |

---

## Tier 1 — highest leverage; do these next

### Memory quality (compounds with usage)
- [x] **Confidence promotion.** Shipped in Sprint 2 — `precog memory promote [--apply]`.
- [ ] **Staleness tagging.** Records older than N days with no reinforcement get a `stale` tag (not demoted — staleness is metadata, not loss). **S.**
- [ ] **Contradiction detection.** New write embedding-similar to existing record but with opposing keyword/sentiment → flag for operator review instead of stacking. **M.**
- [ ] **Compaction.** `precog memory compact --scope X --scope-id Y` — group records semantically, summarize a cluster into one consolidated record, mark originals `compacted_into=<id>`. Reuses existing `TruncatingSummarizer`; LLM-backed summarizer when available. **M-L.**

### Memory chat (RAG) — beyond v0
- [x] **v0: FTS-only retrieval + Ollama chat + toggle + audit-logged.** Shipped.
- [ ] **v1: Re-embed all records with Ollama** in the background; chat uses semantic + FTS hybrid. **M.**
- [ ] **Streaming token output** for the `/chat` page — perceived latency drops even when generation is slow. **S-M.**
- [ ] **Per-scope chat in UI** — chat scope is already wired in the API; UI just needs to expose the dropdown wherever chat appears. **S.** (already done on `/chat`; missing on `/memory` — no longer applicable since the slide-up overlay was removed.)
- [ ] **"Save this answer as a decision"** flow — explicit, operator-confirmed write back to memory. **S.**
- [ ] **Multi-turn conversational memory** — chat history feeds back as context. **M.**

### Profile memory — beyond v1
- [x] **v1: USER scope + 5 MCP tools + CLI + `/profile` page + freeze toggle.** Shipped.
- [x] **v2: Heuristic gap analysis + question queue + answer flow.** Shipped.
- [x] **v3 (partial): `profile_voice` snippet in `session_startup`.** Shipped.
- [ ] **Smarter answer routing.** Currently the answer's profile dimension comes from the question's dimension, not the answer's content. Tiny LLM call (or operator-pick at answer time) should classify by content. **S-M.**
- [ ] **Decay model.** Profile facts decay confidence after N days unless renewed (e.g. "user is in Vietnam" expires; "user prefers concise output" doesn't). **M.**
- [ ] **Profile-aware system prompts in agent bridges.** `voice_prefix` is returned by `session_startup`; bridges should auto-prepend it to outgoing system prompts so codex/hermes/claude_code each adapt their tone consistently. **S.**
- [ ] **"Coach mode" (v4).** Proactive suggestions tied to current context + profile. Calendar / external integrations are part of this and out of scope until v5. **L.**

### Orchestration depth
- [ ] **Pattern 3 — multi-step planning.** New `plan_and_dispatch(steps: list[str], goal: str)` MCP tool. Agent decomposes the high-level goal, runs the steps sequentially with handoff bundles chained. **M.**
- [ ] **Capability-first routing.** Dispatch service inspects `task.required_capabilities` and routes by capability flag match instead of preferred-agent name. Architecture already supports this; just needs the wire-up. **S.**
- [ ] **Quorum dispatch.** `dispatch_quorum(goal, agents: list[str], merge: "majority"|"side-by-side")` — fires same goal to multiple agents, returns combined view. Useful for decisions where you want diversity. **S-M.**

### Operator UX (daemon partially shipped Sprint 6)
- [x] **`precog daemon` lifecycle.** Sprint 6 — start/stop/status wrapping the web server. PID + log management.
- [ ] **File-backed approval queue.** Persistent approval requests that survive operator terminal restart. Build on top of the daemon. **M.**
- [ ] **Persistent dispatch queue.** Today dispatches are in-process; daemon-managed dispatches would survive restarts. **M.**
- [ ] **`precog watch` TUI.** Textual-based live activity dashboard. Three-pane view (active dispatches / event stream / memory tail). Lighter than the web UI for always-on monitoring. **M.**

### Multi-agent conversations — beyond v0
- [x] **v0: domain primitive + 5 MCP tools + 6 web endpoints + `/conversations` page + run_rounds orchestrator.** Sprint 6.
- [x] **Soft-delete (Sprint 7):** `CONVERSATION_DELETED` audit event, `DELETE /api/conversations/{id}` endpoint, `conversation_delete` MCP tool, `[delete]` button in UI. Audit trail preserved (append-only).
- [x] **run_rounds robustness (Sprint 7):** per-turn timeout bumped to 300s (was 120s — caused historical timeouts), `max_wait_seconds` configurable per call, skip agents that fail in a run, **synthesize turn from dispatch result** when an agent doesn't call `conversation_turn` itself (extracts content from handoff `decisions_so_far` → `goal_restatement` → most-recent record). Conversations always advance.
- [ ] **Bridge push protocol.** Codex / Hermes receive messages mid-session via a new bridge channel — replaces polling `conversation_inbox`. Drops per-turn latency from 10-30s to ~2s. **L.**
- [ ] **Auto-stop heuristics for run_rounds.** Detect convergence ("we agree", repeated content) and stop early. **S-M.**
- [ ] **Branching conversations.** Fork a conversation from a turn — e.g., "what if we explored this differently from here?" **M.**

### Tasks page — Mission Brief redesign
- [x] **Sprint 7:** d3-force diagram retired in favor of "Mission Brief" — Now Running (live hero cards w/ rotating progress rings + 1s elapsed counter + breathing animation), Recently Completed (snap-aligned horizontal shelf), Failed Missions (red-stripe cards with reasons), Archive (dense searchable list). Side panel pattern shared with `/debug`.
- [x] **Codex bridge fix (Sprint 7):** absolute path resolution for `-C` arg + cwd. Was the silent "ENOENT in 0.04s" cause that made every codex turn in a conversation fail. Hermes was unaffected because Hermes doesn't pass an additional `-C` flag.

### Dashboard — single-screen layout
- [x] **Sprint 7:** at ≥1280×>760 viewports, dashboard fits the viewport entirely — outer container has `overflow:hidden`, each panel scrolls internally. KPI strip · attention (max 200px scroll) · 3-col row (happening / grown / chains) all visible without outer scroll. Below 1280px or 760px, falls back to outer scroll. Every panel respects its bounds, no panel dominates.

### `/debug` page — full UI rewrite
- [x] **Sprint 7:** redesigned from inline-drawer (chronically cramped) to side-panel pattern. Three-zone layout: kind sidebar with severity-colored counts → main list with filter chips → side panel that slides in from right with full event payload + preceding events + operator hints + "open in agents view" link. Default range = "all" (was 24h, hid older failures). KPI strip in header shows severity breakdown.
- [ ] **Notifications.** macOS `osascript -e 'display notification ...'` (Linux `notify-send`) when long dispatch completes or stalls. **S.**
- [ ] **Daily digest cron.** Scheduled job runs `precog recall` + summarizer, writes a global-scope `digest` record once per day. Optional Slack/email forward. **S** for the cron, **M** with forwards.
- [ ] **Pattern recognition / proactive nudges.** Detect "operator approved this 3 times this week" → suggest converting to a policy rule. CLAUDE-PLAN.MD §5.3 already specifies this; just build it. **M.**

---

## Tier 2 — important once usage scales

### Trust + governance
- [ ] **Per-agent permission scopes.** Extend rule engine: codex can write project but not global; claude_code can read everything but only write within its task scope. Rules are data, declarative. **M.**
- [ ] **Audit redaction.** Pre-write scanner flags potential secrets (API key patterns, AWS access keys, JWT tokens). Choices: redact / reject / warn. **S-M.**
- [ ] **Memory access logs.** Currently every write is audited; add reads. New event kind `MEMORY_READ` emitted by `memory_get` / `memory_search` / `memory_list`. **S.**
- [ ] **Cascading right-to-forget.** Today `forget(id)` deletes one record; cascading version finds any record whose content references the dropped id (best-effort substring + embedding-similar) and offers to forget those too. **M.**

### Knowledge structure
- [ ] **Knowledge graph layer.** Extract entities (people, projects, files, decisions) from memory content; link them with typed relationships (`supersedes`, `depends_on`, `contradicts`, `implements`). Browse the graph in the web UI. **L.**
- [ ] **Causal trace.** When agent X cites memory record M while writing record N, record `derived_from=M`. Build a directed graph of decisions and their justifications. **M.**

---

## Tier 3 — bigger bets; high value but bigger commitment

### Self-improvement loop
- [ ] **Reflective agent (nightly).** Scheduled background dispatch. Reads recent memory, identifies patterns / contradictions / gaps, writes meta-observations. Effectively a junior research assistant for your past self. **L.**
- [ ] **Continual learning loop.** Evaluator agent scores past dispatch quality (did the answer actually solve the operator's problem?). Future dispatches start with relevant lessons learned in their context. **L.**
- [ ] **Constitutional review.** Agent that reviews memory writes against project values (declarative `~/.exocortex/principles.yaml`). Flags drift. **M-L.**

### Cross-machine
- [ ] **Encrypted git-as-sync.** Memory store + audit log live in a git-managed dir; a sync command pushes/pulls encrypted blobs from a remote. Append-only nature of the audit log makes this naturally git-friendly. **L.**
- [ ] **Multi-operator collaboration.** Multiple operators share certain scopes (project), keep others private (session, task). Conflict resolution rules. **L.**

### Integrations
- [ ] **Git pre-commit hook.** Auto-write a memory record summarizing the diff + commit message. Future agents see commit history as searchable memory. **S.**
- [ ] **Browser clipper.** Browser extension: select text → "save to exocortex with scope=research." Useful for the digest workflow. **M.**
- [ ] **Editor sidebar (VS Code / JetBrains).** Show relevant exocortex memory for the file you're editing. Hybrid retrieval already supports this; needs the UI shim. **M.**
- [ ] **Slack / email forward.** When a memory record matching `tags=[…]` is written, post to a configured channel. **S.**
- [ ] **Voice / dictation interface.** Record observations from the road. **L.**
- [ ] **Calendar integration.** Link memories to meetings / time blocks. **M.**

---

## Tier 4 — moonshots / open questions

- [ ] **Claude Code real-binary bridge (Phase 4.5 completion).** Today Claude Code is MCP-client-only. Closing this gives symmetric three-agent orchestration. Blocked on Anthropic exposing a `--print` / `claude exec` style headless mode in the CLI. **L.**
- [ ] **Federated exocortex.** Multiple operators' instances exchange selected memory via a privacy-respecting protocol. Real research project, not a feature. **XL.**
- [ ] **Memory marketplace.** Share curated memory packs (e.g., "best practices for FastAPI projects") between operators. Community-governance question, not just engineering. **XL.**

---

## Sequencing recommendation

**Updated after Sprint 6 (conversations + daemon + chain-of-custody + debug page + dashboard rebuild):**

Top priority now is the **bridge push protocol for conversations** — agents currently poll `conversation_inbox` rather than receiving real-time pushes mid-session. This is what turns "slow turn-by-turn dispatch" (10-30s/turn) into actual conversational latency. Multi-day work because each Bridge (Codex, Hermes) needs a "receive" channel, but it's the next concrete unlock.

After that: **agent-side compliance with `from_agent` / `parent_task_id`** — the schema accepts these, the skills tell agents to set them, but compliance is patchy. Could be hardened by making `from_agent` schema-required (failing fast when missing) so agents can't silently produce anonymous handoffs.

Then: **profile decay + smarter answer routing** — without these the profile drifts and entries get filed under wrong dimensions.

Then: **memory chat v1** (re-embed everything with Ollama → semantic retrieval) and **persistent dispatch queue migration to daemon** (so dispatches survive operator terminal restarts).

Tier 2 should follow naturally as usage hits limits — you'll feel the need for permissions when an agent does something inappropriate; you'll feel the need for the knowledge graph when search isn't enough. Build them when you need them, not before.

Tier 3 is the "exocortex starts getting interesting on its own" tier. The reflective agent specifically is the highest-novelty item — it turns this from a memory store into a thinking partner.

Tier 4 is genuinely uncertain; revisit when the rest is solid.
