# Auto-recall: agents remember on startup

The goal: every time you open a Hermes / Codex / Claude Code session,
the agent automatically says something like:

> Picking up where we left off — you have 2 unfinished tasks:
> 1. **Refactor the auth middleware** (in_progress, last touched 2h ago by codex)
> 2. **Build the memory summarizer** (in_progress, last touched 1d ago by codex)
>
> Recent durable decisions (6):
>   — Chose SQLite over Postgres for MVP (by operator)
>   — Split Bridge and Runner interfaces (by claude_code)
>
> Continue one of these, or start something new?

`session_startup` also returns a `profile_voice` snippet — concatenated
`profile.communication_style` + `profile.value` records — that agents
should prepend to their own system prompts. That's how each agent
inherits how the operator likes to be talked to (terse vs. detailed,
direct vs. diplomatic, etc.) without needing to be told from scratch
every session. Operator's profile lives at `MemoryScope.USER` and is
managed via `precog profile show|question|answer|freeze` and the
`/profile` web page.

There are two mechanisms, and you should use both. They're complementary.

## Mechanism 1: MCP-native — agents call `session_startup` themselves

When an MCP client connects to `precog mcp-server`, it receives the server's
INSTRUCTIONS string during the initialize handshake. Ours says (literally):

> ON YOUR FIRST TURN of every new session you MUST call `session_startup`
> (pass your agent id if known). It returns unfinished tasks, recent
> decisions, and a `text_for_user` string ready to show the operator.
> Render that summary and ask the operator whether to continue one of the
> unfinished items or start something new.

Well-behaved modern agents (Hermes, Codex, Claude Code) honor these
instructions — they'll call `session_startup` on turn one.

**Wiring (one-time per agent):**

### Claude Code

Add to `~/.claude/settings.json` (or the project `.claude/settings.json`):

```json
{
  "mcpServers": {
    "exocortex": {
      "command": "uv",
      "args": ["run", "precog", "mcp-server"],
      "cwd": "/path/to/exocortex"
    }
  }
}
```

Verify: `claude mcp list` should show `exocortex`.

### Hermes

```bash
hermes mcp add exocortex \
  --command "uv run precog mcp-server" \
  --cwd /path/to/exocortex
```

Verify: `hermes mcp list` should show `exocortex` with the `session_startup`
tool enabled.

### Codex

```bash
codex mcp add --name exocortex -- \
  bash -c "cd /path/to/exocortex && uv run precog mcp-server"
```

Verify: `codex mcp list`.

## Mechanism 2: Hook-injected — exocortex puts the summary in context before the agent even starts thinking

Mechanism 1 depends on the agent choosing to call the tool. Hooks **force**
the summary into the conversation context at session start — zero reliance
on agent compliance.

### Claude Code (`SessionStart` hook)

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          {
            "type": "command",
            "command": "cd /path/to/exocortex && uv run precog recall --agent claude_code"
          }
        ]
      }
    ]
  }
}
```

The hook's stdout becomes context for the first turn; Claude Code reads it
and will produce the recap prompt naturally.

### Hermes (shell-script hook)

Hermes has a hooks subsystem. Inspect yours with `hermes hooks`. A
pre-session hook that runs:

```bash
uv run --directory /path/to/exocortex precog recall --agent hermes
```

…and writes the output into the session context will achieve the same
thing. Exact wiring depends on your Hermes version — check `hermes hooks
--help`.

### Codex

Codex doesn't have a first-class session-start hook today. You have two
options:

1. Rely on Mechanism 1 alone (usually sufficient).
2. Shell-wrap your `codex exec` invocation:

   ```bash
   codex_with_recall() {
     (cd /path/to/exocortex && uv run precog recall --agent codex) \
       | codex exec -
   }
   ```

## Quick verification

With the MCP server wired in any of the three agents, start a new session
and ask:

> What was I working on?

If recall is wired, the agent reads from exocortex (via `session_startup`
or the hook-injected context) and answers with the actual list of
unfinished tasks + recent decisions. If it says "I don't know," check that
your wiring actually registered the server.

Also:

```bash
# See what the agent is seeing:
uv run precog recall --agent claude_code
uv run precog recall --json | jq '.unfinished_tasks[].goal'

# Writes made by any agent show up in the web UI immediately:
#   http://127.0.0.1:8756/memory
```

## What gets remembered

Anything captured in durable memory:
- Coordinator-driven agent runs write automatically.
- Agents using MCP `memory_write` during standalone sessions write explicitly.
- Operator-authored records via `memory_write` calls.

Session-scoped (ephemeral) records are NOT surfaced in recall by design —
they vanish with the session. Use `scope="task"` for unit-of-work context
and `scope="project"` (usually `scope_id="exocortex"`) for long-lived
decisions the next agent should see.
