# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## What this is

Exocortex is a local-first multi-agent coordination platform. It gives multiple
coding agents (Claude Code, ChatGPT Codex, Hermes) a shared substrate so they
can cooperate on a single task without stepping on each other:

- A memory store (SQLite + FTS, optional semantic) with mandatory provenance.
- A unified, policy-enforced tool surface every agent reaches through.
- A coordination layer that moves work between agents via an explicit handoff
  bundle.
- An append-only audit log so every session is fully replayable.

The trust boundary is a **trusted operator on a local machine**; the threat we
design against is "an agent makes a mistake," not a malicious agent or a hostile
multi-tenant environment.

## Stack

- **Language:** Python 3.12+
- **Package manager:** `uv` (with hatchling as the build backend)
- **Contracts:** Pydantic v2
- **Async:** `anyio`
- **Logging:** `structlog`
- **CLI:** `typer` + `rich`; `textual` for TUI modes
- **Storage:** SQLite (local), `sqlite-vec` for semantic memory
- **Tests:** `pytest` + `pytest-asyncio`

## Commands

Run these from the repo root:

```bash
uv sync --all-extras            # install + create venv (dev tools included)
uv run pytest                   # full test suite
uv run pytest tests/unit -x     # unit tests only, stop on first failure
uv run pytest -k roundtrip      # filter tests by name
uv run ruff check .             # lint
uv run ruff format .            # format
uv run mypy src                 # type check
uv run precog --help            # CLI
uv run precog daemon start      # long-running daemon (web UI + MCP fan-out)
uv run precog daemon status     # show pid, log, port
uv run precog serve --port 8756 # inline web server (use this OR daemon)
uv run precog mcp-server        # stdio MCP server for Hermes / Codex / Claude Code
```

Web pages served at `:8756`: `/` dashboard, `/memory` constellation, `/agents`,
`/chat`, `/profile`, `/conversations`, `/debug`.

**Reload caveat.** The MCP and web-server processes snapshot Python at startup
and don't hot-reload. After modifying anything under
`src/exocortex/operator/mcp/**` or `src/exocortex/operator/web/**`, restart the
relevant process — for MCP, that means exiting and reopening any agent session
that has the server attached.

`pip install -e ".[dev]"` works as a fallback if `uv` is unavailable.

## Architecture map

Source layout under `src/exocortex/`:

```
contracts/         Pydantic v2 models — Task, Session, MemoryRecord,
                   ToolInvocation, Handoff, ApprovalRequest, Event, Capability
core/              Task / Session FSMs, in-process async event bus
memory/            session store (TTL), durable SQLite store, retrieval
                   (keyword + semantic), summarizer for handoff digests
tools/             tool registry + spec, builtin tools (fs, shell, git),
                   policy-checked executor
policy/            PolicyEngine, rule engine, approval flow
agents/bridge/     subprocess + MCP bridges (Codex, Hermes; Claude Code is
                   tracked, blocked on a headless `claude exec`)
coordination/      capability router, handoff serializer, merge gate, worktree
                   allocator, budget enforcement
operator/          typer CLI (`precog`), web UI, MCP server
observability/     append-only audit log, structlog setup, OpenTelemetry hooks
```

`tests/` mirrors the layout with `unit/`, `contract/` (adapter conformance),
and `e2e/` suites.

## Load-bearing design rules

These keep the architecture from drifting. Violating any of them is a code
review failure, not a stylistic choice:

- **`core/` and `coordination/` import no adapter-specific code.** Provider
  quirks live in `agents/bridge/*` or `agents/runner/*`. Contract leaks are
  tracked as a KPI.
- **`Bridges` and `Runners` are different interfaces.** Do not unify them into
  a single "adapter" abstraction.
- **Every `MemoryRecord` write carries provenance** (`source`, `confidence`
  enum, `timestamp`, `scope`). There is no "quick write" path.
- **Every `ToolInvocation` passes through policy before execution.** Policy is
  middleware, not an optional add-on. No tool executes without a
  `PolicyDecision`.
- **Workspace isolation is `git worktree add`,** not conflict detection after
  the fact.
- **Every contract has `schema_version` from day one.** Additive-only changes
  within a major version. Breaking changes require a documented migration.
- **Event bus + memory store are append-only and fully timestamped.** This is
  what makes `precog trace` reconstructible.

## Useful entry points when getting oriented

- `docs/roadmap.md` — what's shipped, what's next.
- `docs/auto-recall.md` — how `session_startup` + the SessionStart hook give
  agents picking-up-where-we-left-off behavior on every session open.
- `docs/conversations.md` — the multi-agent conversation primitive: domain
  model, MCP tools, web endpoints, `run_rounds` orchestrator, UI.
- `docs/memory-chat-plan.md` — design of the local-first RAG layer (Ollama).
