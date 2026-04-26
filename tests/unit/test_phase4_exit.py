"""Phase 4 exit criterion (CLAUDE-PLAN.MD §6):

  "A task routed to either bridge produces normalized events + a populated
   handoff bundle. Kill the task mid-flight; the bundle is still valid."

The parameterized conformance suite (tests/contract/test_bridge_conformance.py)
covers 'normalized events + populated bundle' for both bridges. This module
covers the mid-flight kill case.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exocortex.agents.bridge import (
    Bridge,
    BridgeDeps,
    ClaudeCodeBridge,
    CodexBridge,
    InvokeTool,
    NoteDecision,
    RaiseQuestion,
    ScriptedProcess,
    WriteMemory,
)
from exocortex.contracts import (
    EventKind,
    Handoff,
    MemoryScope,
    SessionStatus,
    TaskStatus,
)
from exocortex.core.events import EventBus
from exocortex.core.session_manager import SessionManager
from exocortex.core.task_manager import TaskManager
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.memory.session import SessionMemoryStore
from exocortex.memory.summarizer import TruncatingSummarizer
from exocortex.observability.audit import AuditLog
from exocortex.policy.approvals import ApprovalQueue, auto_approve_resolver
from exocortex.policy.rule_engine import DeclarativeRuleEngine, default_rules
from exocortex.tools.builtin import register_builtins
from exocortex.tools.executor import ToolExecutor
from exocortex.tools.registry import ToolRegistry


def _wire(tmp_path: Path) -> tuple[BridgeDeps, AuditLog, TaskManager, SessionManager]:
    registry = ToolRegistry()
    register_builtins(registry)
    policy = DeclarativeRuleEngine(rules=default_rules())
    audit = AuditLog(tmp_path / "audit.jsonl")
    bus = EventBus(policy)
    bus.set_audit_sink(audit.record)
    approvals = ApprovalQueue(bus, auto_approve_resolver)
    executor = ToolExecutor(
        registry=registry, policy=policy, bus=bus, approvals=approvals
    )
    session_mgr = SessionManager(bus)
    deps = BridgeDeps(
        bus=bus,
        executor=executor,
        session_manager=session_mgr,
        session_memory=SessionMemoryStore(),
        durable_memory=DurableMemoryStore(tmp_path / "mem.db"),
        embedder=DeterministicEmbeddingProvider(),
        summarizer=TruncatingSummarizer(),
    )
    return deps, audit, TaskManager(bus), session_mgr


@pytest.mark.parametrize(
    "bridge_cls", [CodexBridge, ClaudeCodeBridge], ids=lambda c: c.__name__
)
@pytest.mark.asyncio
async def test_mid_flight_kill_produces_valid_bundle(
    bridge_cls: type[Bridge], tmp_path: Path
) -> None:
    deps, audit, tasks, sessions = _wire(tmp_path)
    worktree = tmp_path / "work"
    worktree.mkdir()
    (worktree / "a.txt").write_text("seed", encoding="utf-8")

    # Long script: 3 actions the agent will perform before we pull the plug.
    script = [
        InvokeTool(tool="fs.read", arguments={"path": str(worktree / "a.txt")}),
        WriteMemory(content="durable fact captured before kill", durable=True),
        NoteDecision(summary="Interim decision.", rationale="Before kill."),
        # --- anything past here must not be observed ---
        RaiseQuestion("This should NOT appear in the bundle."),
        WriteMemory(content="post-kill content should never land", durable=True),
    ]

    task = await tasks.create(goal="Work that will be interrupted")
    await tasks.transition(task.id, TaskStatus.ROUTED)
    await tasks.transition(task.id, TaskStatus.IN_PROGRESS)

    bridge = bridge_cls(
        agent_id=bridge_cls.__name__,
        deps=deps,
        proc=ScriptedProcess(script),
        workspace_path=worktree,
    )
    await bridge.start(task)

    # Consume exactly 3 actions — through NoteDecision — then kill.
    for _ in range(3):
        consumed = await bridge.step()
        assert consumed is not None

    await bridge.kill()

    # Further step() returns None; process is dead, state frozen.
    assert await bridge.step() is None

    # Bundle is still buildable, and reflects state up to the kill point only.
    handoff = await bridge.build_handoff()
    assert isinstance(handoff, Handoff)
    assert handoff.from_agent == bridge_cls.__name__
    assert len(handoff.decisions_so_far) == 1
    assert handoff.decisions_so_far[0].summary == "Interim decision."
    assert handoff.open_questions == []  # the raise_question was queued after kill

    # Bundle round-trips.
    restored = Handoff.model_validate_json(handoff.model_dump_json())
    assert restored == handoff

    # Session is terminated, not gracefully closed.
    session = next(s for s in sessions.all() if s.task_id == task.id)
    assert session.status == SessionStatus.TERMINATED
    assert session.ended_at is not None

    # Durable content that landed before the kill is preserved in the store.
    durable = await deps.durable_memory.list_by_scope(MemoryScope.TASK, str(task.id))
    contents = {r.content for r in durable}
    assert "durable fact captured before kill" in contents
    assert "post-kill content should never land" not in contents

    # Audit trail records the HANDOFF_INITIATED even though we killed the agent.
    events = await audit.read_all()
    kinds = {e.kind for e in events}
    assert EventKind.HANDOFF_INITIATED in kinds
    # But the post-kill WriteMemory never emitted its event.
    memory_events = [e for e in events if e.kind == EventKind.MEMORY_WRITTEN]
    assert len(memory_events) == 1  # only the pre-kill one
