"""Agent-as-orchestrator: `dispatch_task` MCP tool.

An agent inside a live session (e.g. Claude Code) calls `dispatch_task`
with a goal and an optional preferred_agent. This service spins up a
single-use bridge for the target agent (Hermes or Codex today — Claude
Code's real-binary bridge is pending Phase 4.5), runs it to completion,
and returns the final Handoff + the memory records the dispatched agent
wrote to exocortex's shared store.

Because writes flow through the shared SQLite-backed memory, the caller
can follow up with `memory_search` to see everything the dispatched
agent observed / decided. The constellation UI also picks up new stars
in real time.

This is Pattern 2 from the orchestration discussion: synchronous
dispatch, single hop, shared memory as the continuity surface. Pattern 3
(multi-step planning with chained handoffs) will sit on top of this
primitive — plan() → dispatch() → plan() → dispatch() → ...
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from exocortex.agents.bridge import (
    Bridge,
    BridgeDeps,
    CodexBridge,
    CodexSubprocessProcess,
    HermesBridge,
    HermesSubprocessProcess,
)
from exocortex.config import Settings
from exocortex.contracts import (
    Budget,
    Event,
    EventKind,
    Handoff,
    TaskStatus,
)
from exocortex.coordination.router import AgentRegistration, CapabilityRouter
from exocortex.core.events import EventBus
from exocortex.core.session_manager import SessionManager
from exocortex.core.task_manager import TaskManager
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.memory.session import SessionMemoryStore
from exocortex.memory.summarizer import TruncatingSummarizer
from exocortex.observability.audit import AuditLog
from exocortex.observability.logging import get_logger
from exocortex.policy.approvals import ApprovalQueue, auto_approve_resolver
from exocortex.policy.rule_engine import DeclarativeRuleEngine, default_rules
from exocortex.tools.builtin import register_builtins
from exocortex.tools.executor import ToolExecutor
from exocortex.tools.registry import ToolRegistry

DispatchStatus = Literal["running", "completed", "failed", "timeout", "cancelled"]

# Agents whose names are known but cannot currently be subprocess-dispatched.
# Today only `claude_code` — its real-binary bridge is pending Phase 4.5
# (blocked on Anthropic shipping a headless `claude exec` mode). When a
# caller asks for one of these, the dispatch service auto-falls-back to a
# capable registered agent in `_FALLBACK_PRIORITY` order rather than hanging.
_DISPATCH_UNSUPPORTED_AGENTS: frozenset[str] = frozenset({"claude_code"})
_FALLBACK_PRIORITY: tuple[str, ...] = ("codex", "hermes")

logger = get_logger("exocortex.dispatch")


class DispatchError(RuntimeError):
    pass


class NullWorktreeManager:
    """Stand-in WorktreeManager for dispatch contexts where we don't need
    git isolation — just a scratch directory each task can call its own.

    Dispatched agents that do edit files (e.g. Codex with
    `sandbox_mode="workspace-write"`) will have this directory as their
    workspace. They cannot escape it by default because of the sandbox
    policy on their side; exocortex's Coordinator-level git worktree
    isolation is only enforced when a real WorktreeManager is used.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    async def create(self, task_id: UUID) -> Path:
        path = self.root / f"dispatch-{task_id}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def remove(self, path: Path) -> None:  # pragma: no cover
        # Directories survive so the operator can inspect after-the-fact.
        return None


@dataclass
class RunningDispatch:
    """In-memory entry for a dispatch in flight (or completed)."""

    task_id: str
    dispatched_to: str
    started_at: float = field(default_factory=time.monotonic)
    before_ids: frozenset[str] = field(default_factory=frozenset)
    bridge: Any = None  # Bridge, but avoid the import cycle complication
    worktree: Path | None = None
    future: asyncio.Task[Any] | None = None
    state: DispatchStatus = "running"
    result_handoff: Handoff | None = None
    error: str | None = None


class DispatchService:
    """Lazily-initialized per-process dispatch helper.

    Lives for the lifetime of the MCP server process. The first call to
    `dispatch_task` builds the full coordination stack (event bus, policy
    engine, memory stores, bridges) once; subsequent calls reuse it.
    """

    def __init__(self, *, settings: Settings) -> None:
        self._settings = settings
        self._initialized = False
        self._router: CapabilityRouter | None = None
        self._task_manager: TaskManager | None = None
        self._deps: BridgeDeps | None = None
        self._audit: AuditLog | None = None
        self._worktree_root: Path = settings.data_dir / "dispatch-worktrees"
        self._running: dict[str, RunningDispatch] = {}

    async def _ensure_init(self) -> None:
        if self._initialized:
            return
        self._settings.ensure_dirs()
        self._worktree_root.mkdir(parents=True, exist_ok=True)

        registry = ToolRegistry()
        register_builtins(registry)
        policy = DeclarativeRuleEngine(rules=default_rules())
        audit = AuditLog(self._settings.audit_log_path)
        self._audit = audit
        bus = EventBus(policy)
        bus.set_audit_sink(audit.record)
        approvals = ApprovalQueue(bus, auto_approve_resolver)
        executor = ToolExecutor(
            registry=registry, policy=policy, bus=bus, approvals=approvals
        )
        deps = BridgeDeps(
            bus=bus,
            executor=executor,
            session_manager=SessionManager(bus),
            session_memory=SessionMemoryStore(),
            durable_memory=DurableMemoryStore(self._settings.memory_db_path),
            embedder=DeterministicEmbeddingProvider(),
            summarizer=TruncatingSummarizer(),
        )
        self._deps = deps
        self._task_manager = TaskManager(bus)
        self._router = self._build_router(deps)
        self._initialized = True

    @staticmethod
    def _build_router(deps: BridgeDeps) -> CapabilityRouter:
        router = CapabilityRouter()

        # Hermes — always registered if the binary is on PATH.
        if shutil.which("hermes") is not None:
            def _make_hermes(worktree: Path) -> HermesBridge:
                return HermesBridge(
                    agent_id="hermes",
                    deps=deps,
                    proc=HermesSubprocessProcess(worktree=worktree),
                    workspace_path=worktree,
                )

            router.register(
                AgentRegistration(
                    agent_id="hermes",
                    capability=HermesBridge(
                        agent_id="hermes",
                        deps=deps,
                        proc=HermesSubprocessProcess(),
                        workspace_path=None,
                    ).capability(),
                    bridge_factory=_make_hermes,
                )
            )

        # Codex — always registered if the binary is on PATH.
        # Dispatched codex subprocesses get `bypass_approvals=True` so MCP
        # tool calls (e.g. memory_search to read prior context) auto-approve;
        # without it, codex's `exec` mode silently cancels every MCP call.
        if shutil.which("codex") is not None:
            def _make_codex(worktree: Path) -> CodexBridge:
                return CodexBridge(
                    agent_id="codex",
                    deps=deps,
                    proc=CodexSubprocessProcess(
                        worktree=worktree,
                        bypass_approvals=True,
                    ),
                    workspace_path=worktree,
                )

            router.register(
                AgentRegistration(
                    agent_id="codex",
                    capability=CodexBridge(
                        agent_id="codex",
                        deps=deps,
                        proc=CodexSubprocessProcess(),
                        workspace_path=None,
                    ).capability(),
                    bridge_factory=_make_codex,
                )
            )

        return router

    def registered_agents(self) -> list[str]:
        """Helper: for `agents_available_for_dispatch` exposure."""
        if self._router is None:
            return []
        return [r.agent_id for r in self._router.registered()]

    async def _audit_failed(
        self,
        *,
        reason: str,
        preferred_agent: str | None,
        goal: str,
        detail: str = "",
    ) -> None:
        """Emit a DISPATCH_FAILED audit event for a pre-task-creation failure.
        Without this, dispatch errors that fire before a Task exists vanish
        into stderr — the audit log gets no record. This is what makes the
        /debug page possible."""
        if self._audit is None:
            return
        await self._audit.record(
            Event(
                kind=EventKind.DISPATCH_FAILED,
                agent_id="exocortex",
                payload={
                    "reason": reason,
                    "preferred_agent": preferred_agent,
                    "goal_preview": goal[:200],
                    "detail": detail,
                },
            )
        )

    async def _audit_fallback(
        self,
        *,
        requested: str,
        fallback: str,
        goal: str,
        reason: str,
    ) -> None:
        if self._audit is None:
            return
        await self._audit.record(
            Event(
                kind=EventKind.DISPATCH_FALLBACK,
                agent_id="exocortex",
                payload={
                    "requested": requested,
                    "fallback": fallback,
                    "reason": reason,
                    "goal_preview": goal[:200],
                },
            )
        )

    async def start_dispatch(  # noqa: PLR0912, PLR0915 — multi-fallback path
        self,
        *,
        goal: str,
        preferred_agent: str | None = None,
        parent_task_id: str | None = None,
        from_agent: str | None = None,
    ) -> RunningDispatch:
        """Spawn a sub-agent for this goal and return immediately with a
        handle. Use get_status / wait_for / cancel to drive it."""
        await self._ensure_init()
        assert self._deps is not None
        assert self._task_manager is not None
        assert self._router is not None

        registered = {r.agent_id for r in self._router.registered()}
        if not registered:
            await self._audit_failed(
                reason="no_bridges_registered",
                preferred_agent=preferred_agent,
                goal=goal,
                detail=(
                    "Neither `hermes` nor `codex` is on PATH. Install one to "
                    "give dispatch somewhere to route."
                ),
            )
            raise DispatchError(
                "No bridges are registered — neither `hermes` nor `codex` is on "
                "PATH. Install one of them (or both) so dispatch has somewhere "
                "to route the task."
            )

        # Auto-fallback for known-unsupported agents (today: `claude_code`).
        # Pick the first agent in _FALLBACK_PRIORITY that's registered;
        # audit-log the redirect so the operator knows what happened.
        original_request = preferred_agent
        fallback_used = False
        if preferred_agent in _DISPATCH_UNSUPPORTED_AGENTS:
            fallback = next(
                (a for a in _FALLBACK_PRIORITY if a in registered), None
            )
            if fallback is None:
                await self._audit_failed(
                    reason="no_fallback_for_unsupported_agent",
                    preferred_agent=preferred_agent,
                    goal=goal,
                    detail=(
                        f"{preferred_agent} has no headless bridge yet "
                        f"(Phase 4.5) and no fallback (codex/hermes) is "
                        f"registered."
                    ),
                )
                raise DispatchError(
                    f"preferred_agent={preferred_agent!r} has no real-binary "
                    f"bridge yet (Phase 4.5), and no fallback agent is "
                    f"registered. Install `codex` or `hermes` to enable "
                    f"automatic fallback."
                )
            logger.warning(
                "dispatch.fallback",
                requested=preferred_agent,
                fallback=fallback,
                reason="claude_code_no_headless_bridge",
            )
            await self._audit_fallback(
                requested=preferred_agent,
                fallback=fallback,
                goal=goal,
                reason="claude_code_no_headless_bridge",
            )
            preferred_agent = fallback
            fallback_used = True
        elif preferred_agent and preferred_agent not in registered:
            await self._audit_failed(
                reason="preferred_agent_not_registered",
                preferred_agent=preferred_agent,
                goal=goal,
                detail=f"Registered: {sorted(registered)}",
            )
            raise DispatchError(
                f"preferred_agent={preferred_agent!r} is not available. "
                f"Registered: {sorted(registered)}."
            )

        task_inputs: dict[str, Any] = {}
        if preferred_agent:
            task_inputs["preferred_agent"] = preferred_agent
        if fallback_used:
            task_inputs["original_preferred_agent"] = original_request
            task_inputs["fallback_reason"] = (
                "claude_code has no headless bridge yet — Phase 4.5"
            )
        if parent_task_id:
            task_inputs["parent_task_id"] = parent_task_id
        task = await self._task_manager.create(
            goal=goal,
            inputs=task_inputs,
            budget=Budget(),
        )

        # Resolve from_agent for chain-of-custody display. Priority:
        #   1. Explicit `from_agent` parameter (caller knows who they are)
        #   2. Parent task's owning agent (auto-inferred from chain)
        #   3. None — operator-initiated dispatches without an agent context
        resolved_from = from_agent
        if resolved_from is None and parent_task_id:
            try:
                parent_uuid = UUID(parent_task_id)
            except (ValueError, TypeError):
                parent_uuid = None
            if parent_uuid is not None:
                parent_task = await self._lookup_task_owner(parent_uuid)
                if parent_task:
                    resolved_from = parent_task

        # Audit-log the handoff so chain visualization has explicit linkage
        # between parent and child tasks. Even when parent_task_id is absent,
        # the event lets us reconstruct single-hop chains downstream.
        # `from_agent` + `to_agent` make the chain-of-custody explicit at
        # every hop — every timeline + the chain swimlane render this as
        # "from → to".
        if self._audit is not None:
            await self._audit.record(
                Event(
                    kind=EventKind.HANDOFF_INITIATED,
                    agent_id="exocortex",
                    task_id=task.id,
                    payload={
                        "from_agent": resolved_from,
                        "to_agent": preferred_agent or "auto",
                        "child_task_id": str(task.id),
                        "parent_task_id": parent_task_id,
                        "goal_preview": goal[:200],
                        "fallback_used": fallback_used,
                    },
                )
            )

        try:
            if preferred_agent:
                current = self._router.resolve(preferred_agent)
            else:
                current = self._router.route(task)
        except Exception as e:
            raise DispatchError(f"routing failed: {e}") from e

        worktree_mgr = NullWorktreeManager(self._worktree_root)
        worktree = await worktree_mgr.create(task.id)

        await self._task_manager.transition(task.id, TaskStatus.ROUTED)
        await self._task_manager.transition(task.id, TaskStatus.IN_PROGRESS)

        before_ids = frozenset(
            str(r.id)
            for r, _ in await self._deps.durable_memory.all_with_embeddings()
        )

        bridge: Bridge = current.bridge_factory(worktree)
        rd = RunningDispatch(
            task_id=str(task.id),
            dispatched_to=current.agent_id,
            before_ids=before_ids,
            bridge=bridge,
            worktree=worktree,
        )
        rd.future = asyncio.create_task(
            self._run_bridge(rd, bridge, task), name=f"dispatch-{task.id}"
        )
        self._running[rd.task_id] = rd
        return rd

    async def _run_bridge(
        self, rd: RunningDispatch, bridge: Bridge, task: Any
    ) -> None:
        try:
            handoff = await bridge.run_task(task)
            rd.result_handoff = handoff
            rd.state = "completed"
            assert self._task_manager is not None
            await self._task_manager.transition(task.id, TaskStatus.COMPLETED)
        except asyncio.CancelledError:
            rd.state = "cancelled"
            assert self._task_manager is not None
            with contextlib.suppress(Exception):
                await self._task_manager.transition(task.id, TaskStatus.FAILED)
            raise
        except Exception as e:
            rd.error = f"{type(e).__name__}: {e}"
            rd.state = "failed"
            assert self._task_manager is not None
            with contextlib.suppress(Exception):
                await self._task_manager.transition(task.id, TaskStatus.FAILED)

    async def get_status(self, task_id: str) -> dict[str, Any]:
        rd = self._running.get(task_id)
        if rd is None:
            raise DispatchError(f"unknown dispatch task_id: {task_id}")
        return await self._snapshot(rd)

    async def wait_for(
        self, task_id: str, wait_seconds: int = 30
    ) -> dict[str, Any]:
        rd = self._running.get(task_id)
        if rd is None:
            raise DispatchError(f"unknown dispatch task_id: {task_id}")
        if rd.future is not None and not rd.future.done():
            # asyncio.shield prevents wait_for from cancelling the inner
            # task on timeout — we want it to keep running so a later poll
            # can pick up its result.
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(
                    asyncio.shield(rd.future), timeout=wait_seconds
                )
        return await self._snapshot(rd)

    async def cancel(self, task_id: str) -> dict[str, Any]:
        rd = self._running.get(task_id)
        if rd is None:
            raise DispatchError(f"unknown dispatch task_id: {task_id}")
        if rd.future is not None and not rd.future.done():
            rd.future.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await rd.future
        if rd.bridge is not None:
            with contextlib.suppress(Exception):
                await rd.bridge.kill()
        if rd.state == "running":
            rd.state = "cancelled"
        return await self._snapshot(rd)

    async def _lookup_task_owner(self, task_id: UUID) -> str | None:
        """Find the agent_id that 'owns' a previously-dispatched task by
        scanning the audit log for the matching HANDOFF_INITIATED event.
        Returns None if not found — falls through to the explicit
        `from_agent` path or remains unattributed."""
        if self._audit is None:
            return None
        events = await self._audit.read_all()
        target_str = str(task_id)
        for ev in events:
            if ev.kind != EventKind.HANDOFF_INITIATED:
                continue
            child = ev.payload.get("child_task_id") if ev.payload else None
            if child == target_str:
                # The to_agent on the parent's handoff event = the agent
                # that owns this task in chain-of-custody terms.
                to_agent = ev.payload.get("to_agent") if ev.payload else None
                if isinstance(to_agent, str) and to_agent != "auto":
                    return to_agent
        return None

    async def dispatch(
        self,
        *,
        goal: str,
        preferred_agent: str | None = None,
        max_wait_seconds: int = 300,
        parent_task_id: str | None = None,
        from_agent: str | None = None,
    ) -> dict[str, Any]:
        """Backward-compat synchronous dispatch. Starts a dispatch, waits
        up to max_wait_seconds, then — on timeout — CANCELS the sub-agent
        and returns partial results (records written so far). This is
        safer than raising because it releases the subprocess; callers
        that want to let a dispatch keep running beyond their wait should
        use start_dispatch + wait_for + explicit cancel instead."""
        rd = await self.start_dispatch(
            goal=goal,
            preferred_agent=preferred_agent,
            parent_task_id=parent_task_id,
            from_agent=from_agent,
        )
        snap = await self.wait_for(rd.task_id, wait_seconds=max_wait_seconds)
        if snap["status"] == "running":
            snap = await self.cancel(rd.task_id)
            snap["status"] = "timeout"
        return snap

    async def _snapshot(self, rd: RunningDispatch) -> dict[str, Any]:
        assert self._deps is not None
        elapsed = round(time.monotonic() - rd.started_at, 2)
        pairs = await self._deps.durable_memory.all_with_embeddings()
        new_records = sorted(
            [r for r, _ in pairs if str(r.id) not in rd.before_ids],
            key=lambda r: r.timestamp,
        )
        result: dict[str, Any] = {
            "task_id": rd.task_id,
            "status": rd.state,
            "dispatched_to": rd.dispatched_to,
            "elapsed_seconds": elapsed,
            "memory_records_written": len(new_records),
            "memory_record_ids": [str(r.id) for r in new_records],
            "records": [
                {
                    "id": str(r.id),
                    "type": r.type,
                    "source": r.source,
                    "content": (
                        r.content
                        if len(r.content) <= 2000
                        else r.content[:2000] + "…"
                    ),
                }
                for r in new_records[:20]
            ],
            "worktree_path": str(rd.worktree) if rd.worktree else "",
        }
        if rd.result_handoff is not None:
            result["handoff"] = _handoff_summary(rd.result_handoff)
        if rd.error is not None:
            result["error"] = rd.error
        return result


def _handoff_summary(h: Handoff) -> dict[str, Any]:
    return {
        "id": str(h.id),
        "from_agent": h.from_agent,
        "to_agent": h.to_agent,
        "sequence_no": h.sequence_no,
        "goal_restatement": h.goal_restatement,
        "constraints_active": list(h.constraints_active),
        "decisions_so_far": [
            {"summary": d.summary, "rationale": d.rationale}
            for d in h.decisions_so_far
        ],
        "open_questions": list(h.open_questions),
        "expected_output": h.expected_output,
        "memory_scope_ids": list(h.memory_scope_ids),
    }


# --- Public factory for tests -----------------------------------------------


def make_test_dispatch_service(
    *,
    settings: Settings,
    router: CapabilityRouter,
    task_manager: TaskManager,
    deps: BridgeDeps,
) -> DispatchService:
    """Escape hatch for tests: inject a pre-built router + deps to avoid
    spinning up real subprocesses."""
    svc = DispatchService(settings=settings)
    svc._router = router  # noqa: SLF001
    svc._task_manager = task_manager  # noqa: SLF001
    svc._deps = deps  # noqa: SLF001
    svc._initialized = True  # noqa: SLF001
    return svc
