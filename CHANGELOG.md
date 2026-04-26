# Changelog

All notable changes to Exocortex will be documented here.

The format is loosely [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows semantic versioning at the contract layer (every
contract carries a `schema_version`; additive-only changes within a major
version).

## [0.1.0] — 2026-04-26

First public release.

### Core platform

- Pydantic v2 contracts: `Task`, `Session`, `MemoryRecord`, `ToolInvocation`,
  `Handoff`, `ApprovalRequest`, `Event`, `Capability`. Every contract carries
  `schema_version` from day one.
- Task / Session FSMs and an in-process async event bus.
- Append-only audit log (`data/audit.jsonl`) — every event is replayable, and
  the entire UI is a projection over this log.
- Coordination layer: capability router, handoff serializer, merge gate, and
  worktree allocator (one `git worktree` per task).
- Policy engine with rule middleware and operator-approval flow. No
  `ToolInvocation` executes without a `PolicyDecision`.

### Memory

- Durable SQLite memory store with FTS5 keyword search and `sqlite-vec`
  semantic search.
- Mandatory provenance on every write (`source`, `confidence`, `timestamp`,
  `scope`).
- USER-scope memory and a `profile.*` record family for operator profile.
- Confidence promotion: ≥3 agents corroborate → record promoted to
  `asserted`.
- Deduplication clusters + `memory_merge`. Right-to-forget via
  `memory_forget` (audit-logged).
- **Memory chat** — local-first RAG over memory via Ollama, off by default.
  CLI: `precog chat-toggle`, `precog chat`. MCP: `memory_chat`. Web: `/chat`
  with citation chips, scope filter, history.

### Bridges

- **Codex bridge** (real binary). Path-handling note: both `-C <path>` and
  `cwd=<path>` must be absolute, or codex fails ENOENT in ~40ms.
- **Hermes bridge** (real binary).
- **Claude Code bridge** — tracked, blocked on a headless `claude exec`
  surface from Anthropic.

### Coordination + dispatch

- Capability-based routing with explicit handoff bundles.
- `dispatch_task` and `dispatch_batch` MCP tools for sequential and parallel
  dispatch.
- Chain-of-custody on every handoff: `from_agent` and `to_agent` auto-inferred
  from `parent_task_id` when not provided.

### Multi-agent conversations

- `ConversationService` with `open` / `add_turn` / `close` / `delete` /
  `inbox`. Transcripts are reconstructible from the audit log alone.
- `run_rounds` orchestrator: fires N rounds of dispatches with the transcript
  fed back as context. Robustness pass:
  - Per-turn timeout 300s by default (configurable per call).
  - Skip-failed-agents within a run — broken bridges don't burn the whole
    timeout budget.
  - Synthesize-turn-from-handoff fallback when an agent runs to completion
    but doesn't call `conversation_turn` itself.
- Soft-delete via append-only audit event preserves the audit trail while
  hiding the conversation from listings and blocking new turns.

### MCP server (31 tools)

- `precog mcp-server` is a stdio MCP server consumed by Hermes / Codex /
  Claude Code.
- **Auto-recall on session start.** `session_startup` returns unfinished
  tasks + recent decisions + a `profile_voice` snippet describing how the
  operator likes to be talked to. Every new agent session opens with
  picking-up-where-we-left-off context.
- Tools cover memory, dispatch, profile, chat, fs, shell, trace, and
  conversation primitives.

### Web UI (7 pages)

- `/` — dashboard with attention panel, what's-happening, what's-grown,
  handoff chains, sparklines, density toggle, persistent state.
- `/memory` — 3D constellation with live-thinking animation when chats fire,
  hover side-panel.
- `/agents` — per-agent history with a "why" drawer.
- `/chat` — memory chat (RAG) with citation chips and history.
- `/profile` — operator profile + question queue.
- `/conversations` — chat-bubble transcripts, custom-rounds run, soft-delete,
  history archive.
- `/debug` — failure triage with hint side-panel.

### Daemon

- `precog daemon start | stop | status` — long-running web server with PID
  file management and log rotation. Hosts the audit-log tailer, WebSocket
  fan-out, conversation router, and chain-of-custody pipeline.

### CLI

- `precog submit / ls / ps / tail / trace / tools` — task and tracing.
- `precog memory list / search / show / promote / recall` — memory.
- `precog chat-toggle / chat` — memory chat lifecycle.
- `precog profile show / question / answer / redact / freeze / export` —
  operator profile.
- `precog daemon` — web + MCP host lifecycle.
- `precog mcp-server` — stdio MCP server entry point.

### Quality

- Test suite with unit, contract, and e2e splits.
- Ruff + mypy clean.
- Real-binary integration tests are opt-in (`EXOCORTEX_RUN_HERMES=1`,
  `EXOCORTEX_RUN_CODEX=1`).

[0.1.0]: https://github.com/xD4O/exocortex/releases/tag/v0.1.0
