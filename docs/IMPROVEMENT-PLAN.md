# Exocortex — Improvement Plan (v0.2 batch)

_Audit date: 2026-07-03 · Scope: full source tree (~11.4k LOC Python + 7-page web UI)_

This plan is the output of a four-track audit (security, agent handoffs,
observability, web UI) against the project's own stated threat model — **a
trusted operator on a local machine; the threat is "an agent makes a mistake,"
not a malicious agent** — plus a second lens: _what happens if the local port or
MCP surface is ever exposed._

It is organized around your three priorities — **smooth agent-to-agent
handoffs**, **legible read-only observability**, and **a better UI** — preceded
by a security track, because several of the platform's headline safety promises
are currently not enforced in code. The last section sequences everything into
shippable phases.

Severity key: **P0** must-fix (a documented guarantee is false, or data
corruption) · **P1** high leverage · **P2** important as usage scales.

---

## TL;DR — state of the system

The **contracts and architecture are genuinely good**: the `Handoff` bundle,
the append-only event log, the policy rule DSL, and the capability router are
all well-designed. The problem is a consistent gap between _what the design
promises_ and _what the running code does_:

1. **The safety layer is not on the path agents actually use.** The MCP
   `shell_exec` / `fs_read` / `fs_list` tools bypass the policy engine, approval
   queue, and worktree isolation entirely. The one place the policy pipeline
   _is_ wired (dispatch) auto-approves every action marked "requires approval."
2. **Agent-to-agent handoff does not actually work with the real binaries.**
   Codex/Hermes bridges can only emit `WriteMemory` + `TaskDone`; they cannot
   initiate a handoff, and the bundle they'd pass is nearly empty. True multi-hop
   handoff is exercised only by a test fixture.
3. **Observability fragments the story.** Every read re-parses the entire audit
   log (O(n) per request, 17+ endpoints), the true actor of a handoff is hidden
   in an untyped payload, reads are never audited, and there is no single "what
   changed and why" view.
4. **The UI is 7 bespoke pages with no shared core** — `el()` is redefined 8×,
   colors/time-formatters drift and produce visibly wrong output, keyboard
   accessibility is broken across the board, and several 30s polls wipe
   in-progress operator input.

None of these are hard to fix. The rest of this document is the concrete list.

---

## Track A — Security & safety: make the guarantees true

The CLAUDE.md load-bearing rule "_No tool executes without a PolicyDecision_" and
the README's "_read-only lens_" are both false as written. These are correctness
bugs even under the benign threat model, and become remote-exploitable the moment
`--host 0.0.0.0` is used.

| # | Sev | Finding | Location | Fix |
|---|-----|---------|----------|-----|
| A1 | **P0** | MCP `shell_exec`/`fs_read`/`fs_list` bypass the entire policy engine, approval queue, and worktree confinement — arbitrary command execution and arbitrary file read/write with zero enforcement. The whole `policy/` package is dead code on the primary surface. | `operator/mcp/server.py:835-945` | Route these three tools through `ToolExecutor.invoke()` with a real `PolicyEngine` + `InvocationContext` + workspace root, exactly like the builtin path. Delete the direct `read_text`/`iterdir`/`create_subprocess_exec` calls. |
| A2 | **P0** | Web server has **no auth, no CORS policy, no CSRF protection, no Origin/Host check**. The "read-only lens" exposes state-changing endpoints, including `POST /api/conversations/{id}/run`, which spawns real codex/hermes subprocesses. The event WebSocket accepts any Origin, so any site the operator visits can stream the full audit feed (cross-site WS hijack) or trigger dispatches (CSRF). | `operator/web/server.py:26-99`, `routes.py:731`, `routes.py:1679-1703` | Enforce loopback-only bind; add a required localhost-origin / token check on all `/api/*`; add an `Origin`/`Host` allowlist on the WebSocket handshake; require a non-forgeable header (or Origin allowlist) on every non-GET route. Move `/run` + toggles behind that boundary. |
| A3 | **P1** | Dispatched Codex runs with `--dangerously-bypass-approvals-and-sandbox` inside a **fake worktree** — `NullWorktreeManager` is a plain `mkdir`, not `git worktree add`, and its docstring claims an isolation the flag explicitly disables. Contradicts CLAUDE.md's "workspace isolation is `git worktree add`." | `mcp/dispatch.py:81-104,202-225`, `bridge/codex.py:111-117` | Use a real git-worktree manager for dispatch and keep Codex's sandbox on (`workspace-write`), or gate the bypass behind an explicit, operator-confirmed "unsafe mode." Don't claim isolation the code doesn't provide. |
| A4 | **P1** | `REQUIRE_APPROVAL` rules (`fs.write`, `shell.exec`) are silently downgraded to auto-approved in every dispatch via `auto_approve_resolver`. "Requires operator approval" means "approved instantly, no human." Approval timeout never enforced. | `mcp/dispatch.py:153`, `policy/approvals.py:17-19`, `executor.py:112-135` | Provide a real (file-backed) approval queue the operator resolves, or mark these ALLOW explicitly and stop advertising them as gated. |
| A5 | **P2** | Secrets leak two ways: `shell_exec` auto-records raw `argv` (`mysql -pHUNTER2`, bearer tokens) into the **shared memory DB**, surfaced to every agent and the UI; and every subprocess inherits the daemon's full environment (`env=` never scrubbed). | `mcp/server.py:909-938`, `tools/builtin/shell.py:22-27`, `coordination/worktree.py:52-58` | Redact secret-shaped tokens from `argv` before recording; pass a scrubbed `env=` allowlist to every subprocess spawn. |
| A6 | **P2** | `fs` builtins do no path validation of their own — containment relies entirely on the policy layer, so any caller that skips the executor gets unrestricted traversal. `_fs_write` even `mkdir(parents=True)`. | `tools/builtin/fs.py:10-36` | Enforce a configured sandbox root inside each handler (resolve + `relative_to`) as defense in depth. |
| A7 | **P2** | `install.sh` is `curl \| bash` that clones a **mutable `main`**, pipes a second remote script to `sh`, and runs `uv sync` (arbitrary build hooks) — no pinning, checksum, or signature. Any CDN/MITM compromise → RCE on the operator's machine. | `install.sh:6-7,54-81` | Pin to a tag/commit, verify a checksum, and prefer download-inspect-run over nested pipe-to-shell. |
| A8 | **P2** | FTS5 `MATCH` receives the raw user query; inputs like `"`, `NEAR(`, `col:` raise an uncaught `OperationalError` → unhandled 500, cheap to trigger repeatedly. (No SQL injection found — all queries are parameterized.) | `memory/durable.py:140-168` | Wrap FTS parsing in try/except → clean 400, or quote the query into a phrase. |

---

## Track B — Agent-to-agent handoffs (your priority #1)

The `Handoff` schema is rich (goal restatement, constraints, decisions, open
questions, workspace state, tool cursor, budget). The failure is that **almost
nothing populates it, and receivers read almost none of it.**

| # | Sev | Finding | Location | Fix |
|---|-----|---------|----------|-----|
| B1 | **P0** | Real bridges cannot initiate a handoff. `CodexSubprocessProcess.start` / `HermesSubprocessProcess.start` always build exactly `[WriteMemory, TaskDone]`; only a `RequestHandoff` action (emitted solely by the test `ScriptedProcess`) sets a handoff target. The coordinator's `if not handoff_out.to_agent: break` terminates every real chain after hop 0. | `bridge/codex.py:204-207`, `bridge/hermes.py:148-151`, `bridge/base.py:220-225`, `coordinator.py:107` | Teach the subprocess bridges to parse a structured handoff request from the agent's final message (or a dedicated MCP tool the agent calls) and translate it into `RequestHandoff`. |
| B2 | **P0** | The handoff bundle is nearly empty and the receiver reads only `goal_restatement`. `workspace_state` is hardcoded `None`; `decisions_so_far`/`open_questions` are always empty for real agents; `budget_remaining` carries the _original_ budget; the tool cursor is empty; and the digest reads **session** memory while agents write to **durable** memory, so it's usually empty and `goal_restatement` collapses to just `task.goal`. | `bridge/base.py:150-179`, `summarizer.py:43-89`, `codex.py:139-160` | Populate `workspace_state` in `build_handoff`; digest durable records (or move agent responses to session scope); have receivers prepend constraints + decisions + open questions + expected output to the prompt. |
| B3 | **P1** | The conversation "synthesize turn" fallback fabricates content — because decisions are empty it falls to `goal_restatement`, which in the conversation path _is the instruction prompt_. So the agent's "reply" recorded in the transcript is the instruction reflected back, while the real response record (`codex_response`) is never reached. | `conversation.py:390-418,526-541` | Reorder `_extract_agent_reply` to prefer the actual response record; never fall back to `goal_restatement` when it equals the dispatched goal. |
| B4 | **P1** | The `claude_code → codex` auto-fallback fires on the _name_, not capability. In conversations, codex produces the text but the turn is recorded with `from_agent="claude_code"` — the transcript lies about who spoke. | `mcp/dispatch.py:322-356`, `conversation.py:494-543` | Propagate the effective agent back into conversation attribution; clearly re-label substituted turns. |
| B5 | **P1** | Anonymous handoffs slip through the chain-of-custody layer. `from_agent` is schema-required on the model but _optional_ on the MCP tools; the `HANDOFF_INITIATED` event is recorded with `from_agent=None`, and `to_agent` is logged as `"auto"` for capability-routed dispatches, which then can't be inherited by children. | `mcp/server.py:207-215`, `dispatch.py:388-421,515-534` | Make `from_agent` required on the dispatch tools (fail fast); record the _resolved_ agent as `to_agent`, never `"auto"`. |
| B6 | **P1** | Routing is preferred-agent-name / first-registered, never capability-matched. `CapabilityRouter.route` has real logic, but `required_capabilities` is never plumbed from any MCP tool into `task.inputs`, so it always falls through to "first registered agent." | `router.py:64-92`, `dispatch.py:369-427` | Add a `required_capabilities` parameter to the dispatch tools and thread it into `task_inputs`. |
| B7 | **P1** | Worktrees leak on every task. `Coordinator.submit` creates a worktree and never removes it (success, failure, or exhaustion); dispatch scratch dirs are never GC'd. Unbounded disk growth + a growing `git worktree list`. | `coordinator.py:55,129-142`, `dispatch.py:96-103` | Wrap `submit` in try/finally → `worktree_mgr.remove`; add a TTL sweep for dispatch scratch dirs. |
| B8 | **P1** | The merge gate never merges. It records `HANDOFF_INITIATED`/`ACCEPTED` events, performs no git merge and no conflict detection, and the coordinator immediately auto-accepts. Two tasks touching the same files are both declared "merged" while nothing is integrated. | `merge_gate.py:29-89`, `coordinator.py:129-139` | Implement a real `git merge --no-ff` (or PR creation) with conflict → operator review; emit `HANDOFF_ACCEPTED` only on a clean merge. |
| B9 | **P1** | Dispatch/task/session state does not survive a restart. `_running`, `_tasks`, `_sessions` are in-memory dicts; after an MCP restart, in-flight dispatch handles become permanently unresolvable and `TaskManager.get` raises `KeyError`, despite docstrings claiming "reconstructible from the audit log." | `dispatch.py:138,478-513`, `task_manager.py:11-21`, `session_manager.py:11-14` | Rehydrate `_running`/`_tasks`/`_sessions` from the audit log on startup, or move dispatch state to durable storage. |
| B10 | **P2** | `budget_remaining` is never decremented — the coordinator only calls `budget.check()`; tokens/dollars/approvals usage is never recorded, and the handoff carries the full original budget. Budget hand-off is cosmetic. | `coordinator.py:105`, `budget.py:31-74`, `base.py:177` | Record usage as tools/approvals fire; thread `limit − used` into `build_handoff`. |
| B11 | **P2** | Conversation inbox is poll-only and re-reads the entire audit log per poll (O(total events) per agent per turn → the 10-30s/turn latency). The `since_ms` filter uses `<=`, silently dropping same-millisecond turns; pending messages in a closed conversation are lost. | `conversation.py:258-293` | Ship the roadmap's bridge-push protocol; interim, index turns by conversation and use a strict `>` cursor keyed on `(ts, turn_id)`. |
| B12 | **P2** | Coordinator fallback can route to a capability-mismatched agent — with no `required_capabilities`, `find_fallback(required=None)` returns the next registered agent regardless of what it can do; a failed `edit_files` task can "succeed" with a no-op. | `coordinator.py:201-248`, `router.py:97-112` | Infer required capabilities from the failed agent's declared set when the task didn't specify them. |

---

## Track C — Observability & read-only audits (your priority #2)

Goal: answer **who did what, why, and in what order** in _one_ place, cheaply.
Today that requires stitching across `/debug`, `/agents`, the chains drawer, and
`/memory`, and every read re-parses the whole log.

| # | Sev | Finding | Location | Fix |
|---|-----|---------|----------|-----|
| C1 | **P1** | The event model hides the story in an untyped payload. On `HANDOFF_INITIATED` the top-level `agent_id` is `"exocortex"` — the real actor is buried in `payload["to_agent"]`. There is no `correlation_id`, `causation_id`, or top-level `parent_task_id`; ordering is wall-clock only; there is no human-readable "why" field, and `policy_decision.reason` isn't even rendered. | `contracts/event.py:61-76`, `render.py:37-49` | Promote to typed top-level `Event` columns: `actor` (true agent), `parent_task_id`, `caused_by_event_id`, optional `reason`. This single change fixes WHO/WHY/ORDER and makes chains robust to sloppy agents. |
| C2 | **P1** | `AuditLog.read_all()` reads and Pydantic-validates the **entire** log on every call, and 17+ endpoints call it fresh — the dashboard polls several on 10-30s timers, re-parsing the whole log many times per minute. O(events) per request in both IO and validation; no cache anywhere except the constellation. | `audit.py:33-42`, `routes.py` (17 call sites) | Maintain a shared, append-aware in-process event cache (tail from last byte offset — `events.py` already does this for the WS feed) plus a `task_id → byte-offset` index, used by all read endpoints. |
| C3 | **P2** | No rotation, compaction, or index on the audit log. The code _anticipates_ a rotated log (`recall.py:166`) but nothing ever rotates it, and orphan events (missing `TASK_CREATED`) are silently dropped from reconstruction. | `config.py:19`, `audit.py`, `recall.py:165-167` | Add periodic snapshot/rotation with an explicit "rotated" marker so `recall`/`trace` know pre-rotation work is archived, not lost. |
| C4 | **P1** | Reads are never audited — `memory_search`/`get`/`list` emit no event, so "what did the agent consult before it decided X" is unanswerable from the trace. Combined with no `derived_from` links, the "decision → justifying memory" chain can't be shown at all. | `handlers.py:446-582`, `event.py:23-27` | Add a `MEMORY_READ` event kind emitted by the read handlers (roadmap already lists this). |
| C5 | **P1** | The CLI and web renderers diverge. The nice per-kind `_event_preview` formatters live only in `routes.py`; `precog trace` dumps raw sorted `k=v` truncated at 80 chars, shows only `%H:%M:%S` (no date — a task spanning midnight is unreadable), and never renders `reason`/`policy_decision`. Many event kinds (`SESSION_CLOSED`, `APPROVAL_RESOLVED`, `PROFILE_*`, `CONVERSATION_*`) render a blank summary. | `render.py:37-49`, `routes.py:163-197` | Lift one `EventKind → human sentence` formatter shared by CLI + web; add formatters for the blank kinds; render `reason`/`policy_decision`; add a full date to `render_event_line`. |
| C6 | **P1** | There is no "trace everything" view and no way to follow a chain. `/debug` is failure-only (hardcoded to 4 kinds), has no free-text search and no export; `precog trace` matches a single `task_id`, so following work across a handoff (which spawns a child task with a new id) requires manually finding the child and re-running. | `debug.js:56-62`, `routes.py:989-996`, `cli.py:158-162` | Unify `/debug` into an all-kinds, text-searchable, exportable (JSON/CSV) event browser; add `precog trace --follow-chain` that walks child tasks. |
| C7 | **P2** | Chain/swimlane rendering has silent data loss and field drift. The agent color map is hardcoded and duplicated (drift between files), tasks whose owning agent can't be derived are `continue`-skipped (vanish from the swimlane), and `summarizePayload` reads `p.tool`/`p.risk_tier` while the server writes `tool_name`, so live-feed summaries render blank. Note also `tracing.py` is a **no-op OTel shim** — spans emit nothing. | `chains.js:29-35,306-307`, `dashboard.js:708-718`, `tracing.py:14-30` | Data-drive and dedupe the color map; render a placeholder lane for unknown-agent tasks; align `summarizePayload` field names; either wire real OTel or drop the shim's pretense. |

---

## Track D — Web UI (your priority #3)

Seven internally-coherent pages with **no shared front-end core**. The palette
and token discipline are good; the problems are structural duplication, broken
keyboard accessibility, and re-renders that fight the operator.

| # | Sev | Finding | Location | Fix |
|---|-----|---------|----------|-----|
| D1 | **P1** | No shared module. The DOM helper `el()` is redefined in **8 files** (two dialects), `escapeHtml` twice, the relative-time formatter 4× with different granularity (one event reads "45d ago" on one page and "1mo ago" on another), `AGENT_COLORS` 3× with a mismatched fallback gray (same agent renders different colors per page), and `highlightJson` emits `.k` vs `.json-k` classes so the widget can't share CSS. The nav bar is copy-pasted with inconsistent URLs. | `static/*.js` (all) | Extract `static/common.js` (el, colors, time, highlightJson, `connectWs`, `fetchJSON`, a shared panel/drawer controller) + a shared header include with one canonical URL set. |
| D2 | **P1** | Keyboard accessibility is broken platform-wide. `outline:0/none` appears 10× with **no `:focus-visible` anywhere** — keyboard users have no visible focus. Core interactions are clickable `<div>`s (agent cards, event rows, filter badges, failure rows, question cards) with no `tabindex`/`role`/key handler. The constellation canvas and swimlane bars are entirely mouse-only and invisible to screen readers; the swimlane hint says "esc closes" but no Escape handler exists. Drawers lack `role="dialog"` and focus management. | `app.css` (10 sites), `agents.js`, `tasks.js`, `debug.js`, `constellation.js`, `chains.js` | Global `:focus-visible`; convert clickable rows to real `<button>`s; give the canvas + swimlane a focusable text-equivalent; shared drawer controller with `role="dialog"` + focus trap/restore. |
| D3 | **P1** | 30s polls clobber in-progress operator input via full `innerHTML=""` re-renders. The worst: the profile questions poll wipes a **half-typed answer** (and focus) every 30s — destroying exactly the slow interaction the page exists for. The agents timeline rebuild destroys the open "why" drawer mid-read; a 500 leaves the profile page stuck on "loading…" forever because the loaded flag isn't set on error. | `profile.js:662,952`, `agents.js:420,706-715`, `profile.js:303,599` | Diff-render by stable id (skip re-render while a textarea has content/focus); set the loaded flag on every error path; treat any non-ok response as a distinct error state, not "empty." |
| D4 | **P1** | The constellation is untrustworthy and expensive. Edges are labeled "semantic ≥ 0.78" but are actually **layout-proximity** edges (the endpoint only exposes a `has_embedding` boolean), a 30-day hard cap permanently hides older records from a _durable_ store, and there's no loading/empty/error state (plus a crash path when `points` is missing). It rebuilds up to 12 cluster-label divs and re-uploads the entire point buffer **every frame** even when idle. | `constellation.js:527-563,98,1666-1725,1733-1736`, `memory.html:70,92` | Relabel or drop the fake semantic edges; add an unbounded age option; guard null/empty/error/loading; render-on-demand (build labels once, dirty-flag buffer uploads). |
| D5 | **P1** | Live-update gaps. `/debug` — the failure console where you most want live pushes — has **no WebSocket** at all (up to 30s latency). On reconnect there is no backfill, and the agents timeline is refreshed _only_ by WS pushes with no periodic reconcile, so a dropped socket silently loses history. A running conversation is a synchronous `POST /run` that can block up to 900s with no socket to recover from. | `debug.js:571`, `agents.js:736`, `routes.py:1679-1703` | Wire the shared `connectWs` into debug; re-run the page's primary fetch on WS `open` to reconcile the gap; make `/run` return a job id and push progress over the WS. |

---

## Sequenced roadmap — the next batch

Each phase is a coherent, shippable unit. Phases 0-1 restore _truth_ (the code
matching its promises); 2-3 deliver your stated priorities; 4 hardens for scale.

### Phase 0 — Make the safety story true _(≈1 sprint)_
The platform currently advertises controls it doesn't enforce. Close that first.
- **A1** route MCP fs/shell through `ToolExecutor` · **A2** web auth + Origin/CSRF + WS origin check · **A4** real approval gate (or stop advertising it) · **A8** FTS 400.
- _Exit test:_ an MCP agent calling `fs_read("~/.ssh/id_rsa")` is denied and audited; a cross-origin `POST /run` is rejected.

### Phase 1 — Make handoffs actually work _(≈1-2 sprints, priority #1)_
- **B1** bridges can initiate a structured handoff · **B2** populate + consume the full bundle (workspace, decisions, questions, real budget) · **B5** required `from_agent` + resolved `to_agent` · **B6** capability routing plumbed.
- Then the correctness fixes that stop the operator's view being wrong: **B3** stop fabricating conversation turns · **B4** honest speaker attribution.
- _Exit test:_ a real Codex→Hermes handoff carries prior decisions + workspace SHA across the boundary, and the chains view shows a named two-hop chain.

### Phase 2 — Make observability legible _(≈1 sprint, priority #2)_
- **C1** typed `actor`/`parent_task_id`/`caused_by_event_id`/`reason` on `Event` · **C2** shared append-aware event cache + task index · **C5** one shared human-sentence renderer (CLI + web) with dates · **C6** "trace everything" browser + `trace --follow-chain` · **C4** `MEMORY_READ` events.
- _Exit test:_ `precog trace --follow-chain <id>` reads as a legible cross-agent narrative; a dashboard poll no longer re-parses the whole log.

### Phase 3 — UI foundation + polish _(≈1 sprint, priority #3)_
- **D1** shared `common.js` + header (kills the drift bugs) · **D2** keyboard a11y (`:focus-visible`, real buttons, dialog drawers) · **D3** diff-render (stop wiping typed answers) · then **D4** trustworthy/cheap constellation · **D5** debug WS + reconnect reconcile + honest error states.
- _Exit test:_ full keyboard traversal of every page; typing an answer survives a 30s poll; the same agent is the same color everywhere.

### Phase 4 — Durability & scale _(as usage grows)_
- **B7** worktree GC · **B8** real merge gate · **B9** rehydrate state on restart · **B11** bridge push protocol + inbox index · **C3** log rotation · **A3** real dispatch worktree + sandbox · **A5/A6** secret redaction + fs defense-in-depth · **B10/B12** live budget + capability-aware fallback.

---

## What the audit did _not_ find (checked, clean)

- **No SQL injection** — all `durable.py` queries use `?` placeholders.
- **No unsafe deserialization** — only `json` + Pydantic; no `pickle`/`yaml.load`/`eval`.
- **No `shell=True`** — both shell paths use `create_subprocess_exec(*argv)`; the risk is unrestricted argv/cwd (A1/A4), not metacharacter injection.
- **Bridge argv construction** builds lists, not shell strings — no argument injection.
- The **default bind is `127.0.0.1`** (safe); the risk is the lack of defense-in-depth if that's changed.
- The **dashboard/agents/tasks/profile WebSocket clients** have correct exponential-backoff reconnect; the token discipline in `app.css` and the `fetchJSON` wrapper in `debug.js` are good patterns worth promoting to the shared core.

---

_Generated from a four-track parallel audit. File:line references are to the
tree as of the audit date; verify against current `main` before implementing._
