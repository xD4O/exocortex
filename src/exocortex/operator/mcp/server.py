"""FastMCP wiring for the exocortex shared-memory server.

Exposes `memory_write`, `memory_search`, `memory_list`, `memory_get`,
`trace_recent`, and `agents_list` as MCP tools over stdio. Any MCP-client
agent (Hermes, Codex, Claude Code, and future bridges) can be configured
to consume this server and thereby read from + write to the shared
memory store — regardless of whether the agent was launched through the
exocortex Coordinator.

Typical operator wiring (one-time, per agent):

    hermes mcp add exocortex --command "uv run precog mcp-server" \\
        --cwd /path/to/exocortex
    codex mcp add --name exocortex -- uv run precog mcp-server

Once configured, agents have tools like `memory_search` available; prior
sessions + cross-agent state become recallable from any turn.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from exocortex.config import Settings
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.memory.retrieval import HybridRetrieval
from exocortex.observability.audit import AuditLog
from exocortex.operator.mcp.dispatch import DispatchError, DispatchService
from exocortex.operator.mcp.handlers import MemoryHandlers
from exocortex.operator.mcp.toolgate import McpToolGate, redact_argv

Scope = Literal["session", "task", "project", "global"]
ConfidenceLiteral = Literal["observed", "inferred", "asserted", "external_claim"]

INSTRUCTIONS = """\
Exocortex shared-memory server. This is the cross-agent continuity layer.

═════════════════════════════════════════════════════════════════════
IDENTIFY YOURSELF ON EVERY CALL — non-negotiable.

Every tool you call here MUST identify *you*. Specifically:
  • `memory_write(..., source="<your_id>")`     ← always
  • `dispatch_task(..., from_agent="<your_id>")` ← always
  • `dispatch_async(..., from_agent="<your_id>")`← always
  • `session_startup(agent_id="<your_id>")`     ← on first turn

Your `<your_id>` is one of: "hermes", "codex", "claude_code". If you
don't know which you are, ask the operator before calling any tool.

If you are running inside a dispatched session (another agent spun
you up), ALSO pass `parent_task_id=<your_own_task_id>` so multi-hop
chains render correctly in the operator's UI.

This is what makes the chain-of-custody visualization work. Without
your identity, every handoff looks anonymous to the operator.
═════════════════════════════════════════════════════════════════════

ON YOUR FIRST TURN of every new session you MUST call `session_startup`
(pass your agent id if known). It returns unfinished tasks, recent
decisions, and a `text_for_user` string ready to show the operator.
Render that summary and ask the operator whether to continue one of the
unfinished items or start something new. This is how sessions stay
coherent across restarts and across agents.

Call `memory_search` before starting real work to recall context relevant
to whatever the operator asks. You share memory with Codex, Claude Code,
Hermes, and any other agent configured against this server.

Call `memory_write` during and after work to record:
  - decisions (type="decision", confidence="asserted")
  - observations (type="observation", confidence="observed")
  - open questions the next session should resolve (type="question")

Records are durable and visible to every other agent.

Scopes:
  - session: ephemeral per-session scratch (rarely the right choice here)
  - task:    one unit of work; scope_id = task UUID or stable name
  - project: long-lived; scope_id = project name (use 'exocortex' unless told otherwise)
  - global:  applies everywhere; scope_id = 'global'

Confidence:
  - observed:       directly saw it in tool output / file contents
  - inferred:       derived from observed facts
  - asserted:       stated without direct grounding (use sparingly)
  - external_claim: came from the operator or another agent

Orchestration — delegate to another agent:
  - `dispatch_task(goal, preferred_agent, max_wait_seconds, from_agent)` —
    synchronous dispatch to Hermes or Codex. Blocks until done or timeout;
    on timeout returns partial results (records written so far) rather
    than failing. Good for ≤30s subtasks.
  - For long-running work, prefer the async pattern:
      1. `dispatch_async(goal, preferred_agent, from_agent)` → returns a
         task_id immediately; sub-agent runs in background.
      2. Continue the conversation with the operator if useful.
      3. Poll `dispatch_status(task_id)` or block with
         `dispatch_wait(task_id, wait_seconds=30)` to check progress.
         These return the records the sub-agent has written so far even
         while still running, so you can see work-in-flight.
      4. `dispatch_cancel(task_id)` if the operator wants to stop it.

  CHAIN OF CUSTODY — IMPORTANT:
    Every call to `dispatch_task` / `dispatch_async` MUST include
    `from_agent` set to your agent identity (one of: "hermes", "codex",
    "claude_code"). This is what makes the operator's chain visualization
    show "hermes → codex" instead of "? → codex". Without it, your
    handoff is unattributed.

    If you are inside a dispatched session yourself (i.e. another agent
    called you), ALSO pass `parent_task_id` set to your own task_id so
    multi-hop chains (hermes → codex → claude → hermes) render correctly.
    You can read your task_id from the `inputs.parent_task_id` of the
    `session_startup` response, or from the dispatch goal you were given.

Other tools:
  - memory_list — enumerate records in a scope
  - memory_get  — fetch one record by UUID
  - trace_recent — recent audit-log events (optionally filtered to a task prefix)
  - agents_list — list the agent bridges exocortex knows about
"""


def _build_handlers(settings: Settings | None = None) -> MemoryHandlers:
    s = settings or Settings()
    s.ensure_dirs()
    store = DurableMemoryStore(s.memory_db_path)
    embedder = DeterministicEmbeddingProvider()
    retrieval = HybridRetrieval(store, embedder)
    audit = AuditLog(s.audit_log_path)
    return MemoryHandlers(
        store=store,
        embedder=embedder,
        retrieval=retrieval,
        audit=audit,
        settings=s,
    )


async def _safe_dispatch(
    dispatcher: DispatchService,
    goal: str,
    preferred_agent: str | None,
    max_wait_seconds: int,
) -> dict[str, Any]:
    """Helper for dispatch_batch: catches DispatchError so one failure in
    a batch doesn't poison the rest. Each result stands alone."""
    try:
        return await dispatcher.dispatch(
            goal=goal,
            preferred_agent=preferred_agent,
            max_wait_seconds=max_wait_seconds,
        )
    except DispatchError as e:
        return {"status": "failed", "error": str(e), "goal": goal}


def build_mcp_server(settings: Settings | None = None) -> FastMCP:  # noqa: PLR0915
    handlers = _build_handlers(settings)
    effective_settings = settings or Settings()
    effective_settings.ensure_dirs()
    dispatcher = DispatchService(settings=effective_settings)
    # Policy-checked, audited gateway for the ad-hoc fs/shell tools (A1).
    tool_gate = McpToolGate(settings=effective_settings, audit=handlers.audit)
    mcp = FastMCP(name="exocortex", instructions=INSTRUCTIONS)

    @mcp.tool()
    async def dispatch_task(
        goal: Annotated[
            str,
            Field(description=(
                "Clear, self-contained goal for the dispatched agent. "
                "Include any context the receiving agent needs — they start "
                "from a fresh session."
            )),
        ],
        preferred_agent: Annotated[
            Literal["hermes", "codex", "claude_code"] | None,
            Field(description=(
                "Route to this agent specifically. Omit to let the router "
                "pick by capability. `claude_code` has no real-binary "
                "bridge yet (Phase 4.5) — requesting it auto-falls-back "
                "to `codex` (preferred) or `hermes`, audit-logged."
            )),
        ] = None,
        max_wait_seconds: Annotated[
            int,
            Field(description=(
                "Hard ceiling on how long the dispatched agent can run. "
                "Default 300s (5 minutes)."
            )),
        ] = 300,
        parent_task_id: Annotated[
            str | None,
            Field(description=(
                "If you are calling this from inside a dispatched session, "
                "pass your own task_id so the chain visualization can link "
                "your dispatch to its origin. Multi-hop chains "
                "(hermes → codex → claude → hermes) only render correctly "
                "when each hop sets this. Omit if you're the root caller."
            )),
        ] = None,
        from_agent: Annotated[
            str | None,
            Field(description=(
                "Your own agent id (codex / hermes / claude_code / operator). "
                "Captured as the 'from' side of the handoff for chain-of-"
                "custody display. If you provide parent_task_id but omit "
                "from_agent, it's auto-inferred from the parent task."
            )),
        ] = None,
    ) -> dict[str, Any]:
        """Delegate a subtask to another agent synchronously.

        The dispatched agent runs as a subprocess through its Bridge
        adapter, shares the same durable memory store, and its work
        shows up in `memory_search` + the constellation UI in real time.
        Returns the final Handoff summary + a list of memory record ids
        the dispatched agent wrote. Use `memory_search` or `memory_get`
        afterwards to dig into what the sub-agent produced.
        """
        try:
            return await dispatcher.dispatch(
                goal=goal,
                preferred_agent=preferred_agent,
                max_wait_seconds=max_wait_seconds,
                parent_task_id=parent_task_id,
                from_agent=from_agent,
            )
        except DispatchError as e:
            return {"status": "failed", "error": str(e)}

    @mcp.tool()
    async def dispatch_async(
        goal: Annotated[
            str,
            Field(description=(
                "Self-contained goal for the dispatched agent."
            )),
        ],
        preferred_agent: Annotated[
            Literal["hermes", "codex", "claude_code"] | None,
            Field(description=(
                "Target agent, or None to route by capability. `claude_code` "
                "auto-falls-back to codex/hermes (no headless bridge yet)."
            )),
        ] = None,
        parent_task_id: Annotated[
            str | None,
            Field(description=(
                "Set this to your own task_id when calling from a dispatched "
                "session — required for multi-hop chain visualization."
            )),
        ] = None,
        from_agent: Annotated[
            str | None,
            Field(description=(
                "Your own agent id. Captured as the 'from' side of the "
                "handoff. Auto-inferred from parent_task_id if omitted."
            )),
        ] = None,
    ) -> dict[str, Any]:
        """Fire-and-forget: start a dispatch, return a task_id IMMEDIATELY
        without waiting for it to finish. Continue your session; poll with
        `dispatch_status` or `dispatch_wait`, cancel with `dispatch_cancel`.

        Use this for long-running subtasks (>30s) so you can keep talking
        to the operator while the sub-agent works.
        """
        try:
            rd = await dispatcher.start_dispatch(
                goal=goal,
                preferred_agent=preferred_agent,
                parent_task_id=parent_task_id,
                from_agent=from_agent,
            )
            return await dispatcher.get_status(rd.task_id)
        except DispatchError as e:
            return {"status": "failed", "error": str(e)}

    @mcp.tool()
    async def dispatch_status(
        task_id: Annotated[
            str, Field(description="Task id returned by dispatch_async.")
        ],
    ) -> dict[str, Any]:
        """Non-blocking poll of an in-flight or completed dispatch.

        Returns current status (running / completed / failed / timeout /
        cancelled), elapsed time, and all memory records the sub-agent
        has written so far — even if it's still running. This is how you
        watch work-in-flight.
        """
        try:
            return await dispatcher.get_status(task_id)
        except DispatchError as e:
            return {"status": "failed", "error": str(e)}

    @mcp.tool()
    async def dispatch_wait(
        task_id: Annotated[
            str, Field(description="Task id returned by dispatch_async.")
        ],
        wait_seconds: Annotated[
            int,
            Field(description=(
                "Max time to block. If the dispatch isn't done by then, "
                "the call returns the current partial state (status still "
                "'running'); the sub-agent keeps going in the background. "
                "Default 30s."
            )),
        ] = 30,
    ) -> dict[str, Any]:
        """Block up to wait_seconds for a dispatch to finish; return
        current state either way. Sub-agent keeps running past the
        timeout — call again or `dispatch_cancel` as needed.
        """
        try:
            return await dispatcher.wait_for(task_id, wait_seconds=wait_seconds)
        except DispatchError as e:
            return {"status": "failed", "error": str(e)}

    @mcp.tool()
    async def dispatch_cancel(
        task_id: Annotated[
            str, Field(description="Task id returned by dispatch_async.")
        ],
    ) -> dict[str, Any]:
        """Stop a running dispatch. Returns the final snapshot with
        status='cancelled' and whatever records the sub-agent managed to
        write before termination.
        """
        try:
            return await dispatcher.cancel(task_id)
        except DispatchError as e:
            return {"status": "failed", "error": str(e)}

    @mcp.tool()
    async def dispatch_batch(
        tasks: Annotated[
            list[dict[str, Any]],
            Field(description=(
                "Array of dispatch specs. Each item: "
                "{goal: str, preferred_agent?: 'hermes'|'codex', "
                "max_wait_seconds?: int}. All tasks fire in parallel via "
                "asyncio.gather; the response order matches input order."
            )),
        ],
    ) -> dict[str, Any]:
        """Fan out N subtasks in parallel. Returns a list of dispatch
        results in the same order as input. Use when subtasks are
        independent (e.g., 'have codex review tests while hermes drafts
        docs'). For dependent steps, chain with `dispatch_async` +
        `dispatch_wait` instead."""
        coros = []
        for spec in tasks:
            goal = spec.get("goal")
            if not isinstance(goal, str) or not goal.strip():
                coros.append(asyncio.sleep(0, result={
                    "status": "failed",
                    "error": "task missing 'goal' string",
                }))
                continue
            preferred = spec.get("preferred_agent")
            timeout = int(spec.get("max_wait_seconds") or 300)
            coros.append(
                _safe_dispatch(dispatcher, goal, preferred, timeout)
            )
        results = await asyncio.gather(*coros)
        return {"count": len(results), "results": list(results)}

    @mcp.tool()
    async def memory_forget(
        record_id: Annotated[
            str, Field(description="Full UUID of the record to delete.")
        ],
    ) -> dict[str, Any]:
        """Hard-delete a memory record. Audit-logged: the fact that the
        record existed (and was forgotten) stays in the immutable audit
        trail; the content is gone."""
        try:
            return await handlers.memory_forget(record_id=record_id)
        except ValueError as e:
            return {"status": "error", "error": str(e)}

    @mcp.tool()
    async def memory_dedup_clusters(
        scope: Annotated[
            Scope | None,
            Field(description="Restrict to one scope, or omit for all."),
        ] = None,
        scope_id: Annotated[
            str | None, Field(description="Scope id within the chosen scope.")
        ] = None,
        threshold: Annotated[
            float,
            Field(description=(
                "Cosine-similarity cutoff for considering two records "
                "near-duplicates. Default 0.92 — strict. Lower values "
                "(0.85) catch looser variants but yield more false positives."
            )),
        ] = 0.92,
    ) -> dict[str, Any]:
        """Find clusters of near-duplicate memory records. Reports only.
        Use `memory_merge` to act on a cluster."""
        return await handlers.memory_dedup_clusters(
            scope=scope, scope_id=scope_id, threshold=threshold
        )

    @mcp.tool()
    async def memory_merge(
        keep_id: Annotated[
            str, Field(description="UUID of the canonical record to keep.")
        ],
        drop_ids: Annotated[
            list[str],
            Field(description="UUIDs of duplicate records to delete."),
        ],
    ) -> dict[str, Any]:
        """Merge a dedup cluster: keeps `keep_id`, hard-deletes everything
        in `drop_ids`. Audit-logged. Pair with `memory_dedup_clusters` to
        find candidates first."""
        try:
            return await handlers.memory_merge(
                keep_id=keep_id, drop_ids=drop_ids
            )
        except ValueError as e:
            return {"status": "error", "error": str(e)}

    @mcp.tool()
    async def memory_chat(
        question: Annotated[
            str,
            Field(description=(
                "Natural-language question about the memory store. Returns "
                "a grounded answer with record citations."
            )),
        ],
        top_k: Annotated[
            int,
            Field(description="How many memory records to retrieve as context."),
        ] = 8,
        scope: Annotated[
            Scope | None,
            Field(description="Restrict retrieval to a scope, or omit for all."),
        ] = None,
        scope_id: Annotated[
            str | None, Field(description="Scope id within the chosen scope.")
        ] = None,
    ) -> dict[str, Any]:
        """Ask exocortex a question. RAG over memory: hybrid retrieval +
        local chat model (Ollama). Off by default — operator must enable
        via `precog chat-toggle on` or the UI header toggle. Read-only:
        does NOT write to memory."""
        return await handlers.memory_chat(
            question=question, top_k=top_k, scope=scope, scope_id=scope_id
        )

    @mcp.tool()
    async def session_startup(
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Call this on your FIRST turn of a new session. Returns unfinished
        tasks, recent decisions, a ready-to-show text summary, and a
        `profile_voice` snippet describing how the operator likes to be
        communicated with — prepend that to your system prompt.
        """
        return await handlers.session_startup(agent_id=agent_id)

    @mcp.tool()
    async def conversation_start(
        topic: Annotated[
            str,
            Field(description=(
                "What the conversation is about. One short sentence."
            )),
        ],
        participants: Annotated[
            list[str],
            Field(description=(
                "≥2 distinct agents. Use canonical ids: hermes, codex, "
                "claude_code. Order matters — first participant goes first."
            )),
        ],
        opened_by: Annotated[
            str,
            Field(description="Who's opening this — agent id or 'operator'."),
        ] = "operator",
    ) -> dict[str, Any]:
        """Open a conversation room. Returns a conversation_id you can
        feed to `conversation_turn`, `conversation_history`, and the
        `/api/conversations/{id}/run` endpoint."""
        return await handlers.conversation_start(
            topic=topic, participants=list(participants), opened_by=opened_by
        )

    @mcp.tool()
    async def conversation_turn(
        conversation_id: Annotated[
            str, Field(description="The conversation id from conversation_start.")
        ],
        from_agent: Annotated[
            str,
            Field(description=(
                "Your agent id. This is what shows in the transcript "
                "as the speaker."
            )),
        ],
        to_agent: Annotated[
            str,
            Field(description=(
                "The agent you're addressing. Their `conversation_inbox` "
                "will surface this turn."
            )),
        ],
        content: Annotated[
            str, Field(description="Your message. 2-4 sentences usually.")
        ],
        in_reply_to: Annotated[
            str | None,
            Field(description=(
                "Optional turn_id you're replying to — for threading."
            )),
        ] = None,
    ) -> dict[str, Any]:
        """Add one message to an open conversation. Audit-logged."""
        return await handlers.conversation_turn(
            conversation_id=conversation_id,
            from_agent=from_agent,
            to_agent=to_agent,
            content=content,
            in_reply_to=in_reply_to,
        )

    @mcp.tool()
    async def conversation_inbox(
        agent_id: Annotated[
            str, Field(description="Your agent id (hermes/codex/claude_code).")
        ],
        limit: Annotated[
            int, Field(description="Max messages returned.")
        ] = 20,
        since_ms: Annotated[
            int,
            Field(description=(
                "Only return messages newer than this UNIX-ms cutoff. "
                "Pass 0 to get everything."
            )),
        ] = 0,
    ) -> dict[str, Any]:
        """Pull pending messages addressed to you across all open
        conversations. Poll this each turn — push delivery is a future
        enhancement."""
        return await handlers.conversation_inbox(
            agent_id=agent_id, limit=limit, since_ms=since_ms
        )

    @mcp.tool()
    async def conversation_history(
        conversation_id: Annotated[str, Field(description="Conversation id.")],
    ) -> dict[str, Any]:
        """Full transcript of a conversation."""
        return await handlers.conversation_history(
            conversation_id=conversation_id
        )

    @mcp.tool()
    async def conversation_close(
        conversation_id: Annotated[str, Field(description="Conversation id.")],
        closed_by: Annotated[
            str, Field(description="Who's closing — agent id or 'operator'.")
        ] = "operator",
    ) -> dict[str, Any]:
        """Mark a conversation closed. No further turns allowed."""
        return await handlers.conversation_close(
            conversation_id=conversation_id, closed_by=closed_by
        )

    @mcp.tool()
    async def conversation_delete(
        conversation_id: Annotated[str, Field(description="Conversation id.")],
        deleted_by: Annotated[
            str, Field(description="Who's deleting — agent id or 'operator'.")
        ] = "operator",
    ) -> dict[str, Any]:
        """Soft-delete a conversation. The conversation disappears from
        listings + cannot accept new turns; the audit trail of what was
        said is preserved (audit log is append-only by design)."""
        return await handlers.conversation_delete(
            conversation_id=conversation_id, deleted_by=deleted_by
        )

    @mcp.tool()
    async def profile_observe(
        content: Annotated[
            str,
            Field(description=(
                "A candidate fact about the operator (preference, skill, "
                "goal, constraint, routine, communication style, value). "
                "Phrase as a third-person observation."
            )),
        ],
        type: Annotated[
            str,
            Field(description=(
                "Profile dimension: preference / skill / goal / constraint / "
                "routine / communication_style / relationship / value. The "
                "`profile.` prefix is added automatically."
            )),
        ] = "preference",
        confidence: Annotated[
            str,
            Field(description=(
                "external_claim / inferred / observed / asserted. Default "
                "`inferred` — only assert when the operator stated it directly."
            )),
        ] = "inferred",
        evidence_record_ids: Annotated[
            list[str],
            Field(description=(
                "Record IDs that support this observation (chat citations, "
                "specific memory records). Used for review + provenance."
            )),
        ] = [],  # noqa: B006
        agent_id: Annotated[
            str | None,
            Field(description="Your agent id (codex, hermes, claude_code)."),
        ] = None,
    ) -> dict[str, Any]:
        """Drop a candidate profile fact about the operator into USER-scope
        memory. Use sparingly. Operator can review + redact via `/profile`.
        Honors the freeze flag — returns `{"status":"frozen"}` if paused."""
        return await handlers.profile_observe(
            content=content,
            type=type,
            confidence=confidence,
            evidence_record_ids=list(evidence_record_ids),
            agent_id=agent_id,
        )

    @mcp.tool()
    async def profile_recall(
        question: Annotated[
            str,
            Field(description=(
                "What you want to know about the operator. E.g. "
                "'how does the operator prefer code review?'"
            )),
        ],
        top_k: Annotated[
            int, Field(description="Max records to return.")
        ] = 8,
    ) -> dict[str, Any]:
        """RAG-style retrieval restricted to USER-scope (the operator
        themselves). Use before answering anything where the operator's
        preferences, constraints, goals, or communication style might
        matter — i.e. always."""
        return await handlers.profile_recall(question=question, top_k=top_k)

    @mcp.tool()
    async def profile_freeze_toggle() -> dict[str, Any]:
        """Flip the master switch on profile collection. When frozen, all
        `profile_observe` calls return `frozen` and write nothing.
        Persistent across processes."""
        return await handlers.profile_freeze_toggle()

    @mcp.tool()
    async def profile_questions(
        status: Annotated[
            str,
            Field(description="open | answered | skipped | * (all)."),
        ] = "open",
        limit: Annotated[int, Field(description="Max questions returned.")] = 5,
    ) -> dict[str, Any]:
        """Return the queue of profile questions the exocortex would like
        to ask the operator. Surface one at a time — never block."""
        return await handlers.profile_questions(status=status, limit=limit)

    @mcp.tool()
    async def profile_answer(
        question_id: Annotated[
            str, Field(description="The question record id to mark answered.")
        ],
        answer: Annotated[
            str, Field(description="The operator's answer text, verbatim.")
        ],
        agent_id: Annotated[
            str | None, Field(description="Your agent id, if applicable.")
        ] = None,
    ) -> dict[str, Any]:
        """Close an open profile question with the operator's answer. The
        answer becomes a new asserted profile record under the question's
        dimension."""
        return await handlers.profile_answer(
            question_id=question_id, answer=answer, agent_id=agent_id
        )

    @mcp.tool()
    async def memory_write(  # noqa: PLR0913
        content: Annotated[
            str,
            Field(description=(
                "The actual text to remember. Be concise but complete — this "
                "is what another agent or session will read verbatim."
            )),
        ],
        source: Annotated[
            str,
            Field(description=(
                "Who is writing this — agent id like 'hermes', 'codex', "
                "'claude_code', or 'operator'."
            )),
        ] = "external",
        scope: Annotated[
            Scope,
            Field(description=(
                "Visibility scope. 'project' = long-lived, shared across tasks "
                "(default). 'task' = one unit of work. 'global' = crosses all "
                "projects. 'session' = ephemeral, rarely wanted."
            )),
        ] = "project",
        scope_id: Annotated[
            str,
            Field(description=(
                "Identifier within the scope. For 'project' scope use "
                "'exocortex' (or your project name). For 'task' scope the "
                "task UUID or a stable task name. For 'global', use 'global'."
            )),
        ] = "exocortex",
        record_type: Annotated[
            str,
            Field(description=(
                "Kind of record: 'decision' (a durable choice), 'observation' "
                "(something you saw), 'question' (open question for future "
                "sessions), or 'note' (other)."
            )),
        ] = "observation",
        confidence: Annotated[
            ConfidenceLiteral,
            Field(description=(
                "'observed' = directly saw it. 'inferred' = derived from "
                "observed facts. 'asserted' = stated without direct grounding. "
                "'external_claim' = from operator or another agent."
            )),
        ] = "observed",
        tags: Annotated[
            list[str],
            Field(description=(
                "Free-form tags for later filtering. Pass as an array of strings."
            )),
        ] = [],  # noqa: B006 -- FastMCP requires a literal default here
    ) -> dict[str, Any]:
        """Persist a memory record so any future agent session can recall it.

        Call this whenever you or the operator decide something durable,
        observe a load-bearing fact, or surface an open question. All
        parameters except `content` have sensible defaults — prefer the
        default scope='project' scope_id='exocortex' unless you have a
        specific reason to scope differently.
        """
        return await handlers.memory_write(
            content=content,
            source=source,
            scope=scope,
            scope_id=scope_id,
            type=record_type,
            confidence=confidence,
            tags=list(tags) if tags else [],
        )

    @mcp.tool()
    async def memory_search(
        query: str,
        scope: str | None = None,
        scope_id: str | None = None,
        limit: int = 10,
        alpha: float = 0.5,
    ) -> dict[str, Any]:
        """Hybrid keyword+semantic search. alpha 1=keyword, 0=semantic."""
        return await handlers.memory_search(
            query=query, scope=scope, scope_id=scope_id, limit=limit, alpha=alpha
        )

    @mcp.tool()
    async def memory_list(
        scope: str, scope_id: str, limit: int = 50
    ) -> dict[str, Any]:
        """List records in a scope, most-recent-last."""
        return await handlers.memory_list(scope=scope, scope_id=scope_id, limit=limit)

    @mcp.tool()
    async def memory_get(record_id: str) -> dict[str, Any] | None:
        """Fetch one record by full UUID. Returns null if not found."""
        return await handlers.memory_get(record_id=record_id)

    @mcp.tool()
    async def trace_recent(
        task_id: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Recent audit-log events, optionally filtered by task prefix."""
        return await handlers.trace_recent(task_id=task_id, limit=limit)

    @mcp.tool()
    async def agents_list() -> dict[str, Any]:
        """Enumerate the bridges exocortex has adapters for."""
        return await handlers.agents_list()

    # ------------------------------------------------------------------
    # Auto-capture file + shell tools (Phase 6.6).
    # When an agent runs one of these, the invocation + result are
    # recorded to memory automatically — no explicit memory_write call
    # needed. Agents that use these instead of their native equivalents
    # get a free durable audit trail.
    # ------------------------------------------------------------------

    async def _auto_record(
        *, content: str, source: str, record_type: str, tags: list[str]
    ) -> None:
        # Best-effort; don't let a recording failure kill the tool call.
        with contextlib.suppress(Exception):  # pragma: no cover - defensive
            await handlers.memory_write(
                content=content,
                source=source,
                scope="project",
                scope_id="exocortex",
                type=record_type,
                confidence="observed",
                tags=tags,
            )

    @mcp.tool()
    async def fs_read(
        path: Annotated[str, Field(description="Absolute or cwd-relative file path.")],
        source: Annotated[
            str,
            Field(description="Agent id making the call (for attribution)."),
        ] = "external",
        max_chars: Annotated[
            int,
            Field(description="Truncate content at this many chars (for huge files)."),
        ] = 50_000,
    ) -> dict[str, Any]:
        """Read a text file AND auto-record the read as a memory observation.

        Routed through the policy gate: confined to the sandbox root and
        denied for secret-bearing paths. Every call is audited."""
        result = await tool_gate.invoke(
            tool="fs.read", arguments={"path": path}, agent_id=source
        )
        raw = result.get("content", "")
        truncated = raw[:max_chars]
        p = result.get("path", path)
        await _auto_record(
            content=f"fs_read {p} ({len(raw)} chars)",
            source=source,
            record_type="observation",
            tags=["fs_read", "auto"],
        )
        return {
            "path": p,
            "content": truncated,
            "full_size": len(raw),
            "truncated": len(raw) > max_chars,
        }

    @mcp.tool()
    async def fs_list(
        path: Annotated[str, Field(description="Directory path.")],
        source: Annotated[
            str, Field(description="Agent id making the call.")
        ] = "external",
    ) -> dict[str, Any]:
        """List entries in a directory AND auto-record the listing.

        Routed through the policy gate (sandbox-confined, audited)."""
        result = await tool_gate.invoke(
            tool="fs.list", arguments={"path": path}, agent_id=source
        )
        entries = result.get("entries", [])
        p = result.get("path", path)
        await _auto_record(
            content=f"fs_list {p} ({len(entries)} entries)",
            source=source,
            record_type="observation",
            tags=["fs_list", "auto"],
        )
        return {"path": p, "entries": entries, "count": len(entries)}

    @mcp.tool()
    async def shell_exec(
        argv: Annotated[
            list[str],
            Field(description=(
                "Command argv. MUST be an array of strings, e.g. "
                "['git', 'status']. No shell-string assembly."
            )),
        ],
        cwd: Annotated[
            str, Field(description="Working directory. Use an absolute path.")
        ],
        source: Annotated[
            str, Field(description="Agent id making the call.")
        ] = "external",
        timeout_seconds: Annotated[
            int, Field(description="Kill the process if it runs longer than this.")
        ] = 60,
    ) -> dict[str, Any]:
        """Run a shell command AND auto-record the invocation + exit code.

        Stdout/stderr are returned to the caller. The memory record
        captures a REDACTED argv + cwd + return code (secret-shaped tokens
        are masked so they never enter shared memory); the full output is
        NOT persisted (too much noise for a cross-session memory layer).

        Routed through the policy gate: cwd is confined to the sandbox root,
        commands referencing secret paths are denied, and every call is
        audited.
        """
        if not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("argv must be a non-empty list of strings")
        result = await tool_gate.invoke(
            tool="shell.exec",
            arguments={"argv": argv, "cwd": cwd, "timeout_seconds": timeout_seconds},
            agent_id=source,
        )
        rc = result.get("returncode")
        safe_argv = redact_argv(argv)
        await _auto_record(
            content=f"shell_exec rc={rc} argv={safe_argv} cwd={cwd}",
            source=source,
            record_type="observation",
            tags=["shell_exec", "auto", f"rc={rc}"],
        )
        return {
            "argv": argv,
            "cwd": cwd,
            "returncode": rc,
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
        }

    return mcp


def run_stdio() -> None:
    """Entry point: run the MCP server over stdio (the standard transport
    that `hermes mcp add` and `codex mcp add` consume)."""
    mcp = build_mcp_server()
    mcp.run()
