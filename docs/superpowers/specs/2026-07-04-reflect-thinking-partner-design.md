# Reflect — the reflective thinking-partner subsystem

_Design spec · 2026-07-04 · status: approved, pre-implementation_

## Purpose

Turn exocortex from a passive cross-agent memory into a **thinking partner**: a
reflective agent that periodically reads accumulated memory and surfaces
**insights** the operator can review and act on. It answers "what should I
notice about my own knowledge base that I wouldn't spot by searching?"

Because exocortex accumulates from multiple agents (Codex, Hermes, Claude Code)
with mandatory provenance, it is uniquely positioned to notice **cross-agent**
patterns — conflicts between what different agents recorded, recurring
decisions, stale facts, and unlinked-but-related memories.

This is the roadmap's Tier-3 "reflective agent" bet, designed as one coherent
engine rather than four separate features.

## Scope

**In scope (v1):**
- A reflective *dispatch* that reads a window of memory and proposes insights.
- Four insight kinds: `contradiction`, `pattern`, `gap`, `synthesis`.
- A reviewable **insight queue** with accept / dismiss, and a conservative,
  type-specific **act** on accept (propose-then-confirm; nothing auto-applied).
- Surfacing at `session_startup`, a `/reflect` web page, and `precog` CLI.
- On-demand execution; opt-in scheduling as a thin wrapper.

**Out of scope (v1, noted for later):**
- A full token/cost budget system (bounded here by window + max-insights caps;
  the existing `Budget` can be wired in later).
- A new scheduler (scheduled runs just invoke `precog reflect`).
- Auto-applying any mutation without operator confirmation.
- Local-Ollama or hybrid deterministic engines (v1 dispatches a capable agent;
  a deterministic candidate pre-pass is a possible later optimization).

## Non-negotiable invariants (inherited from the platform)

1. **The audit log is the source of truth; every view is a projection over
   it.** Reflect introduces no mutable on-disk state. `precog trace` and full
   replay keep working.
2. **Every insight is grounded.** An insight must cite the memory `record_id`s
   it is based on; an ungrounded proposal is rejected at the tool boundary.
3. **An insight is `inferred` and inert until accepted.** Even accept only
   *proposes* a mutating step (rule creation, record supersede), which takes a
   second explicit confirm. The reflective agent can only ever *suggest* — it
   cannot corrupt memory or policy.
4. **Off by default.** Like memory-chat, Reflect is opt-in and never spends
   tokens unless the operator runs it.

## Core model

### The `Insight` (contract)

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | |
| `schema_version` | Literal[1] | Additive-only within major, like every contract. |
| `kind` | enum | `contradiction` \| `pattern` \| `gap` \| `synthesis` |
| `title` | str | One-line summary. |
| `detail` | str | The reasoning. |
| `refs` | list[UUID] | Memory `record_id`s the insight is grounded in. **Required, non-empty.** |
| `suggested_action` | `SuggestedAction \| None` | Typed proposal (below); `None` = informational only. |
| `confidence` | Confidence | Always `inferred`. |
| `reflection_id` | UUID | The run that produced it. |
| `created_at` | datetime | |

`status` (`proposed` \| `accepted` \| `dismissed`) is **derived from events**,
not stored on the object.

### `SuggestedAction` (tagged union by insight kind)

- `{type: "supersede", stale_record_id: UUID}` — for `contradiction`.
- `{type: "create_rule", rule: <Rule literal>}` — for `pattern`/nudge.
- `{type: "track_gap", question: str, dimension: str}` — for `gap`.
- `{type: "record_decision", content: str}` — for `synthesis`.
- `{type: "none"}` — informational.

### New event kinds

Bracketing a run (mirrors `CONVERSATION_OPENED/CLOSED`):
- `REFLECTION_STARTED` — payload: `window_from`, `window_to`, `agent`,
  `reflection_id`.
- `REFLECTION_COMPLETED` — payload: `reflection_id`, `status`
  (`completed` \| `failed`), `insight_count`, optional `error`.

Per insight:
- `INSIGHT_PROPOSED` — the full Insight payload; `actor` = the reflective
  agent (C1 typed field), `reason` = the title.
- `INSIGHT_ACCEPTED` — payload: `insight_id`, optional `acted`
  (what the act did, e.g. `{superseded_by: <new_record_id>}` or
  `{rule_drafted: <rule_id>}`).
- `INSIGHT_DISMISSED` — payload: `insight_id`, optional `note`.

All `INSIGHT_*` and `REFLECTION_*` events get a per-kind sentence in
`observability/humanize.py` (C5) so they render cleanly in CLI + web.

## Components

```
memory/reflect.py        ReflectionService: window computation, queue
                         projection over INSIGHT_*/REFLECTION_* events,
                         accept/dismiss/act orchestration.
contracts/insight.py     Insight + SuggestedAction models.
operator/mcp/…           insight_propose MCP tool (validates grounding + kind
                         + action schema); reflect dispatch entry point.
coordination/…           reflective goal builder (the structured prompt).
operator/cli.py          `precog reflect [--since|--all|--schedule]`,
                         `precog insights [list|show|accept|dismiss]`.
operator/web/…           /reflect page + /api/insights* routes (projection).
observability/humanize   sentences for the new event kinds.
config.py                EXOCORTEX_REFLECT_* settings.
```

Each unit has one purpose and a clear interface:
- **ReflectionService** — folds the append-only events into queue state and runs
  the accept/dismiss/act transitions. Depends only on `AuditLog` + the memory
  store; no provider code.
- **insight_propose tool** — the only way an agent creates an insight; the
  validation boundary. Structured input, no prose parsing.
- **reflective goal builder** — turns a window into the dispatch prompt.

## Data flow

### A reflection run
1. `precog reflect` → `REFLECTION_STARTED` recorded (window computed as: since
   last `REFLECTION_COMPLETED`, capped at `WINDOW_DAYS`; `--since`/`--all`
   override).
2. Coordinator dispatches the reflective goal to a capable agent
   (`EXOCORTEX_REFLECT_AGENT` or capability-routed), `from_agent="reflect"`.
3. The agent surveys the window via `memory_search` / `memory_list` (these emit
   `MEMORY_READ`, so the run's own consultations are auditable — reuses C4).
4. For each finding, the agent calls `insight_propose(kind, title, detail,
   refs, suggested_action)`. Each call validates and emits `INSIGHT_PROPOSED`.
   Capped at `MAX_INSIGHTS` per run.
5. On dispatch completion → `REFLECTION_COMPLETED` with the count.

### Review + act
1. Operator sees pending insights at `session_startup`, on `/reflect`, or via
   `precog insights`.
2. **Dismiss** → `INSIGHT_DISMISSED`; removed from the queue.
3. **Accept** → `INSIGHT_ACCEPTED`. The mutating step is a **two-call**
   sequence so nothing is auto-applied:
   - **Accept** (`precog insights accept <id>`, or the web "accept" button, or
     `POST /api/insights/{id}/accept`) records `INSIGHT_ACCEPTED` and, if a
     `suggested_action` exists, returns the concrete proposed mutation (the
     drafted `Rule`, the supersede target, etc.) for the operator to see.
   - **Apply** (`precog insights accept <id> --apply`, the web "confirm"
     button, or `POST /api/insights/{id}/act`) performs the mutation and stamps
     the result into the `acted` payload of a follow-up event. Accept without
     apply leaves memory/policy untouched.

   The type-specific mutations are:
   - `contradiction` → confirm writes a new record superseding the stale one
     (`supersedes: <old_id>`); never deletes.
   - `pattern` → confirm writes the drafted `Rule` (declarative data) into
     policy.
   - `gap` → confirm seeds a tracked profile question.
   - `synthesis` → confirm writes a confirmed `decision`/`digest` record citing
     its `refs`.

## Surfacing

- **`session_startup`**: add `pending_insights` = count + top-N titles.
- **`/reflect` web page**: queue grouped by kind; each card shows title,
  detail, clickable cited records (`refs`), and the suggested action with
  accept/dismiss. Projection over `INSIGHT_*` events; benefits from the C2
  incremental audit reads.
- **CLI**: `precog reflect`, `precog insights`, `precog insights show <id>`,
  `precog insights accept|dismiss <id>`.

## Config (env-var, conservative defaults)

| Var | Default | Meaning |
|---|---|---|
| `EXOCORTEX_REFLECT_ENABLED` | `false` | Opt-in, like memory-chat. |
| `EXOCORTEX_REFLECT_WINDOW_DAYS` | `7` | Cap on the reflect window. |
| `EXOCORTEX_REFLECT_AGENT` | _(empty)_ | Preferred agent; else capability-routed. |
| `EXOCORTEX_REFLECT_MAX_INSIGHTS` | `20` | Per-run cap (bounds tokens + queue size). |

## Error handling

- Agent dies / times out mid-run → `REFLECTION_COMPLETED status=failed`;
  already-proposed insights survive (append-only). Same resilience as dispatch.
- `insight_propose` with empty `refs`, unknown `kind`, or malformed
  `suggested_action` → rejected at the tool boundary (fail fast).
- No new memory since last run → run completes with zero insights (not an
  error).
- Accept succeeds but the mutating confirm fails (e.g. invalid rule) → the
  `INSIGHT_ACCEPTED` event still stands; the mutation is a separate confirm that
  can be retried without losing the acceptance.

## Testing (scripted process, no real binaries — mirrors dispatch/conversations)

**Unit**
- `Insight` + `insight_propose` validation: grounding required, kind enum,
  `SuggestedAction` schema per kind.
- Queue projection: fold `INSIGHT_*` events → `proposed → accepted | dismissed`.
- Window computation: since-last-reflection, `WINDOW_DAYS` cap, `--since`,
  `--all`.

**Integration**
- A `ScriptedProcess` reflective agent proposes insights across all four kinds →
  assert queue projects correctly and `REFLECTION_COMPLETED.insight_count`
  matches.

**Accept-act**
- Accept `pattern` → drafts a valid `Rule`, does **not** apply until the second
  confirm.
- Accept `contradiction` → writes a `supersedes` record, never deletes the
  original.
- Dismiss → suppresses from queue, decision recorded as an event.

## Open questions / assumptions (MEDIUM confidence)

- Assumes the dispatched agent reliably calls `insight_propose` rather than
  free-texting findings. Mitigation: the goal prompt is explicit and the tool is
  the only path; if compliance is patchy we can add a synthesize-from-response
  fallback (but avoid the goal-echo trap fixed in B3).
- `MAX_INSIGHTS=20` and `WINDOW_DAYS=7` are starting guesses; tune with use.
- Deterministic candidate pre-pass (embedding-similar records, audit-log
  frequency) is deferred; it would cut tokens and improve recall but adds build
  cost.

## Confidence

**MEDIUM–HIGH.** The architecture reuses proven subsystems (dispatch,
event-projection, session-startup, humanize) and preserves every load-bearing
invariant. The main unverified assumption is reflective-agent compliance with
the structured tool, which is testable with a scripted process before any real
binary is involved.
