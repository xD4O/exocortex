# Exocortex

A local-first coordination platform that lets multiple coding agents — Claude
Code, ChatGPT Codex, and Hermes — share a memory, see each other's work, and
hand tasks off to one another.

> **Status:** v0.1 — first public release. Core platform shipped: contracts,
> task/session runtime, memory, policy-gated tools, Codex + Hermes bridges,
> coordination layer, MCP server (33 tools), and an 8-page web UI. Claude Code's
> real-binary integration is tracked, blocked on a headless `claude exec` mode.

## Why

Coding agents are powerful one at a time. They get fragile when you try to
combine them: one agent's notes are invisible to the next, two agents touching
the same checkout race each other, and there's no audit trail when something
goes wrong.

Exocortex fixes that with four pieces:

- **Shared memory** — a SQLite-backed store every agent reads and writes
  through, with mandatory provenance on every record.
- **Policy-enforced tools** — every tool call passes through a policy
  middleware before execution; risky calls require operator approval.
- **Workspace isolation** — each task runs in its own `git worktree`, not via
  optimistic conflict detection.
- **Append-only audit log** — every event is durable, replayable, and visible
  in real time through the web UI.

The trust boundary is a **trusted operator on a local machine**. The threat
model is "an agent makes a mistake," not a malicious agent or a hostile
multi-tenant environment.

## Quick install

One-line install (requires `git` + a working shell):

```bash
curl -LsSf https://raw.githubusercontent.com/xD4O/exocortex/main/install.sh | bash
```

Or do it manually:

```bash
# 1. Install uv if you don't have it.
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Clone and sync.
git clone https://github.com/xD4O/exocortex.git
cd exocortex
uv sync

# 3. Try the CLI.
uv run precog --help
```

After install:

```bash
uv run precog daemon start          # web UI on http://127.0.0.1:8756
uv run precog daemon status         # check pid + log path
uv run precog daemon stop           # shut it down
```

## What you get

### A web UI on `:8756`

Eight pages, all served by `precog daemon start`:

| Page | What it shows |
|---|---|
| `/` | Live dashboard: attention panel, what's-happening, what's-grown, handoff chains, sparklines. |
| `/memory` | 3D constellation of memory records with hover details and live-thinking animation. |
| `/agents` | Per-agent history with a "why" drawer for each event. |
| `/chat` | RAG over your memory, with citation chips, scope filter, history. |
| `/profile` | Operator profile (USER-scope memory) + question queue. |
| `/conversations` | Multi-agent conversations with chat-bubble transcripts and run-N-rounds. |
| `/debug` | Failure triage with hint side-panel. |
| `/reflect` | Reflective agent: proposed insights with kind filters, accept/dismiss queue, suggested actions, apply + confirm. |

### A 33-tool MCP server

`precog mcp-server` is a stdio MCP server that any compatible agent (Hermes,
Codex, Claude Code) can attach to. Tools cover memory, dispatch, profile,
chat, fs, shell, trace, and conversation primitives.

Highlights:

- **`session_startup`** — every agent calls this on its first turn and gets
  back unfinished tasks + recent decisions + a `profile_voice` snippet
  describing how the operator likes to be talked to. The result: every new
  session opens with picking-up-where-we-left-off context.
- **`memory_chat`** — RAG-grounded answers over your memory, off by default.
  Local-first via [Ollama](https://ollama.com).
- **`dispatch_task` / `dispatch_batch`** — fire one or many tasks at the
  best-suited agent, with a real handoff bundle the next agent inherits.
- **`conversation_*`** — open a topic, have N agents discuss it for M rounds,
  archive or soft-delete the transcript.

### A `precog` CLI

```bash
precog submit <goal>                   # create a task; logs task.created
precog ls / precog ps                  # list tasks
precog tail [--task/--kind/--agent]    # stream events from the audit log
precog trace <task_id>                 # reconstruct a task's timeline
precog memory list / search / show     # browse durable memory
precog memory promote --apply          # >=3 agents corroborate -> asserted
precog recall [--agent X]              # "here's what we were working on"
precog chat-toggle [on|off|status]     # master switch for memory chat
precog chat "<question>"               # one-shot RAG query
precog reflect [--agent X]             # run reflective agent over memory window
precog insights show / dismiss / apply # review + act on proposed insights
precog profile show / question / answer
precog daemon start / status / stop    # long-running web + MCP host
precog mcp-server                      # stdio MCP server
precog tools                           # list registered tools with risk tier
```

Run `uv run precog --help` (and `--help` on any subcommand) for the
authoritative flag set.

## Configuration

All runtime knobs are environment variables — see `.env.example` for the full
list with defaults. Copy it to `.env` and edit, or export them yourself.

Common ones:

```bash
# Memory chat — off by default, requires Ollama at this endpoint when on.
EXOCORTEX_MEMORY_CHAT_ENDPOINT=http://localhost:11434
EXOCORTEX_MEMORY_CHAT_CHAT_MODEL=        # empty = auto-detect
EXOCORTEX_MEMORY_CHAT_EMBEDDING_MODEL=nomic-embed-text

# Where audit log + memory.db + flag files live (relative to CWD by default).
EXOCORTEX_DATA_DIR=./data

# Logging.
EXOCORTEX_LOG_LEVEL=INFO            # DEBUG | INFO | WARNING | ERROR
EXOCORTEX_LOG_FORMAT=console        # console | json
```

## Wiring an agent to the MCP server

After `precog daemon start` is running, point any MCP client at
`precog mcp-server`. See `docs/auto-recall.md` for copy-pasteable
configuration for Claude Code, Hermes, and Codex.

Once an agent is attached, ask it: *"What was I working on?"* — it should pull
unfinished tasks and recent decisions from the shared store and answer
concretely.

## Documentation

- **`docs/roadmap.md`** — what's shipped, what's next.
- **`docs/auto-recall.md`** — wire `session_startup` + a SessionStart hook so
  agents pick up where you left off automatically.
- **`docs/memory-chat-plan.md`** — local-first RAG (Ollama) design.
- **`docs/conversations.md`** — multi-agent conversation primitive.
- **`CHANGELOG.md`** — release history.
- **`CONTRIBUTING.md`** — development setup and contribution flow.

## License

MIT. See [LICENSE](LICENSE).

## Acknowledgements

Exocortex stands on `uv`, `pydantic`, `typer`, `rich`, `structlog`, `anyio`,
`fastapi`, `sqlite-vec`, and the MCP ecosystem. Thank you to everyone working
on the agent tooling that makes a coordination layer like this possible.
