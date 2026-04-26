# Multi-agent conversations

A durable conversation primitive: ≥2 agents exchange messages about a
topic. Transcripts persist forever (they're audit-log events), the
operator watches them in `/conversations` in real time, and a built-in
`run_rounds` orchestrator turns "have hermes and codex discuss X for N
rounds" into a single web/MCP call.

> **Status: v0 shipped (Sprint 6).** Polling-based delivery; runs are
> orchestrator-driven via `dispatch_task`. v1 will add bridge push so
> turns land at conversational latency rather than dispatch latency.

## Architecture

```
                ┌──────────────────────────────────────────┐
                │           audit.jsonl (durable)         │
                │                                          │
                │   conversation.opened                    │
                │   conversation.turn × N                  │
                │   conversation.closed                    │
                │                                          │
                └────────────┬───────────────┬────────────┘
                             │               │
              ┌──────────────┘               └──────────────┐
              ▼                                              ▼
   ┌────────────────────────┐               ┌────────────────────────────┐
   │ ConversationService    │               │ /api/conversations/{id}/run │
   │  - open()              │               │   ↓                         │
   │  - add_turn()          │               │   run_rounds(rounds=N)      │
   │  - close()             │               │   ↓                         │
   │  - list_rooms()        │               │   for each round:           │
   │  - get(transcript)     │               │     for each participant:   │
   │  - inbox(agent_id)     │               │       dispatch_task(transcript-as-context)
   └─────────┬──────────────┘               └────────────┬────────────────┘
             │                                           │
             └─────────────────────┬─────────────────────┘
                                   ▼
                ┌──────────────────────────────────────────┐
                │        WebSocket /api/events             │
                │   (every audit event, including          │
                │    conversation.* — fan-out to every     │
                │    open `/conversations` tab)            │
                └──────────────────────────────────────────┘
```

Conversations are reconstructible from the audit log alone — the
`ConversationService` has no persistence layer of its own. Every read
walks the events. This stays cheap because the audit log is small
(JSONL, kilobytes per day) and the service caches nothing per request.

## Data model

A **conversation** is identified by a UUID and has:
- `topic`: short description of what's being discussed
- `participants`: ordered list of ≥2 distinct agent ids
- `status`: `"open"` (accepting turns) or `"closed"` (archived)
- `started_at`, `last_activity_at`
- `turn_count`, `last_turn_preview`

A **turn** is one message in a conversation:
- `turn_id`, `from_agent`, `to_agent`, `content`
- `timestamp_ms`, `in_reply_to` (optional)

## API

### MCP tools (6)

```
conversation_start(topic, participants[], opened_by="operator")
  → {id, started_at, ...}

conversation_turn(conversation_id, from_agent, to_agent, content, in_reply_to?)
  → {turn_id, timestamp_ms}

conversation_inbox(agent_id, limit=20, since_ms=0)
  → {count, items[]}        # pending messages addressed to you
                            # in any open conversation
                            # POLL THIS each turn (push delivery is v1)

conversation_history(conversation_id)
  → {... full transcript ...}

conversation_close(conversation_id, closed_by="operator")
  → {id, status: "closed", closed_at}

conversation_delete(conversation_id, deleted_by="operator")
  → {id, status: "deleted", deleted_at}
                            # Soft-delete: hides from listings + blocks
                            # new turns. Audit trail preserved.
```

### Web endpoints (7)

```
GET    /api/conversations?status=*&limit=50
POST   /api/conversations                     body: {topic, participants[]}
GET    /api/conversations/{id}                → full transcript
POST   /api/conversations/{id}/turn           body: {from_agent, to_agent, content, ...}
POST   /api/conversations/{id}/close
DELETE /api/conversations/{id}                soft-delete; preserves audit trail
POST   /api/conversations/{id}/run            body: {rounds: 1-50, max_wait_seconds?: 30-900}
```

### Audit events (4)

- `conversation.opened` — `{conversation_id, topic, participants, opened_by}`
- `conversation.turn` — `{conversation_id, turn_id, from_agent, to_agent, content, in_reply_to?}`
- `conversation.closed` — `{conversation_id, closed_by}`
- `conversation.deleted` — `{conversation_id, deleted_by, topic_at_deletion, turn_count_at_deletion}`

## The `run_rounds` orchestrator

When the operator clicks `[run]` in the UI (or POSTs to
`/api/conversations/{id}/run`), the orchestrator does:

```python
for _ in range(rounds):
    for speaker in participants:
        if speaker in skipped:        # robustness rule below
            continue
        transcript = service.get(conversation_id)["turns"]
        goal = build_speaker_prompt(transcript, speaker, topic)
        result = dispatcher.dispatch(
            goal=goal,
            preferred_agent=speaker,
            from_agent=speaker,
            max_wait_seconds=300,     # was 120s; bumped to fit codex's
                                       # real latency on multi-line prompts
        )
        if result["status"] in ("failed", "timeout"):
            skipped.add(speaker)       # don't keep retrying broken bridges
            continue
        # Did the agent actually call conversation_turn?
        if no_new_turn_landed():
            content = extract_agent_reply(result)   # synthesize from
                                                    # handoff data
            if content:
                service.add_turn(speaker, recipient, content)
```

Three robustness rules baked in:

1. **Per-turn timeout** is 300s by default (bumped from 120s — codex on
   multi-line conversation prompts routinely needs 2+ minutes), and is
   configurable per call via the run endpoint's body
   (`max_wait_seconds: 30-900`).

2. **Skip-failed-agents within a run.** If a speaker's dispatch returns
   `status: "failed"` or `"timeout"`, they're added to a `skipped` set
   and not dispatched again in subsequent rounds of the same run. Stops
   you from burning N rounds × N participants × full-timeout on a
   broken bridge.

3. **Synthesize-turn fallback.** When a dispatched agent runs to
   completion but doesn't call `conversation_turn` itself, the
   orchestrator extracts a usable reply from the dispatch result —
   priority: handoff `decisions_so_far` (most recent decision summary)
   → handoff `goal_restatement` → most recent record content the agent
   wrote → empty (skip). The synthesized turn carries
   `synthesized_turn: true` in the orchestrator result so the UI can
   distinguish. Without this, conversations would silently stall when
   an agent ignored the tool-call instruction.

Slow (10-30s per turn × N participants × N rounds), but produces real
agent-to-agent dialogue without any new infrastructure.

## UI (`/conversations` page)

- **Sidebar:** open conversations grouped at top; closed conversations
  collapse into a "history" group below (click chevron to expand).
- **Transcript:** chat-bubble style with per-agent palette tints, slide-in
  animation, auto-scroll-with-pill, operator-injection turns styled as
  full-width dashed boxes.
- **Run controls:** numeric input (1-50) + `[run]` button. Last value
  persisted in `localStorage`.
- **Composer:** "inject as operator" — multi-line textarea, mono, Cmd+Enter
  submits. `to:` dropdown auto-prefills with the participant who is NOT
  the last speaker.
- **Archive:** the `[archive]` button on the status bar closes the
  conversation and moves it to the history group.

## Privacy + audit

- Every turn is audit-logged. No way to hide a turn after the fact.
- "Closing" a conversation archives but does not delete — the transcript
  remains readable forever via `conversation_history`.
- If you need to redact a turn, do it via the audit redaction story (Tier
  2 on the roadmap; not yet shipped).

## What's deferred to v1+

- **Bridge push protocol** — agents receive messages mid-session via a
  new bridge channel, replacing polling. Drops per-turn latency from
  10-30s to ~2s. Tracked on the roadmap.
- **Auto-stop on convergence** — `run_rounds` blindly fires N rounds;
  v1 detects "we agree" / repeated content and stops early.
- **Branching** — fork from a turn ("what if we explored this differently
  from here?").
- **Operator approval gates** — for sensitive conversations, require
  per-turn approval before agents see messages.
