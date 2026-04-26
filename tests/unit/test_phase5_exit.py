"""Phase 5 exit criterion (CLAUDE-PLAN.MD §6):

  "A task starts in Codex, hands off to Claude Code, finishes successfully.
   The operator can trace every step."

This is the first real cross-agent handoff — the synthetic-mock version in
tests/e2e/test_synthetic_handoff.py proved the schema round-trips; this one
proves the Coordinator + Router + Bridges + WorktreeManager + MergeGate +
BudgetTracker actually compose end-to-end.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from exocortex.agents.bridge import (
    ClaudeCodeBridge,
    CodexBridge,
    NoteDecision,
    RaiseQuestion,
    RequestHandoff,
    ScriptedProcess,
    TaskDone,
    WriteMemory,
)
from exocortex.agents.bridge.base import BridgeDeps
from exocortex.contracts import (
    EventKind,
    MemoryScope,
    TaskStatus,
)
from exocortex.coordination.coordinator import Coordinator, CoordinatorError
from exocortex.coordination.merge_gate import MergeGate
from exocortex.coordination.router import AgentRegistration, CapabilityRouter
from exocortex.coordination.worktree import WorktreeManager
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

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)


async def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    env = [
        "-c",
        "user.email=test@exocortex.local",
        "-c",
        "user.name=test",
        "-c",
        "init.defaultBranch=main",
        "-c",
        "commit.gpgsign=false",
    ]
    for args in (["init"], [*env, "commit", "--allow-empty", "-m", "init"]):
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        assert proc.returncode == 0, stderr.decode()


@pytest.mark.asyncio
async def test_phase5_codex_to_claude_code_e2e(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    await _init_repo(repo)

    # --- Shared infrastructure ---
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
    task_mgr = TaskManager(bus)
    worktree_mgr = WorktreeManager(repo, worktree_root=tmp_path / "wts")
    merge_gate = MergeGate(bus)

    durable = DurableMemoryStore(tmp_path / "mem.db")
    session_memory = SessionMemoryStore()
    embedder = DeterministicEmbeddingProvider()
    summarizer = TruncatingSummarizer()

    deps = BridgeDeps(
        bus=bus,
        executor=executor,
        session_manager=session_mgr,
        session_memory=session_memory,
        durable_memory=durable,
        embedder=embedder,
        summarizer=summarizer,
    )

    # --- Agent scripts ---
    def codex_script() -> list:
        return [
            WriteMemory(content="codex durable: input seen", durable=True),
            WriteMemory(content="codex session: scratch note", durable=False),
            NoteDecision(
                summary="Delegate polish to claude_code.",
                rationale="codex drafted; claude_code reviews + lands.",
            ),
            RaiseQuestion("Should we also update CHANGELOG?"),
            RequestHandoff(
                to_agent="claude_code",
                expected_output="Tests green + CHANGELOG updated",
            ),
        ]

    def claude_script() -> list:
        return [
            WriteMemory(
                content="claude_code durable: confirmed codex's decision",
                durable=True,
            ),
            NoteDecision(
                summary="Yes, update CHANGELOG.",
                rationale="Question resolved; covers compliance.",
            ),
            TaskDone(success=True),
        ]

    # --- Bridge factories (fresh bridge per agent invocation) ---
    def make_codex(worktree: Path) -> CodexBridge:
        return CodexBridge(
            agent_id="codex",
            deps=deps,
            proc=ScriptedProcess(codex_script()),
            workspace_path=worktree,
        )

    def make_claude(worktree: Path) -> ClaudeCodeBridge:
        return ClaudeCodeBridge(
            agent_id="claude_code",
            deps=deps,
            proc=ScriptedProcess(claude_script()),
            workspace_path=worktree,
        )

    # --- Router ---
    router = CapabilityRouter()
    router.register(
        AgentRegistration(
            agent_id="codex",
            capability=CodexBridge(
                agent_id="codex",
                deps=deps,
                proc=ScriptedProcess([]),
                workspace_path=None,
            ).capability(),
            bridge_factory=make_codex,
        )
    )
    router.register(
        AgentRegistration(
            agent_id="claude_code",
            capability=ClaudeCodeBridge(
                agent_id="claude_code",
                deps=deps,
                proc=ScriptedProcess([]),
                workspace_path=None,
            ).capability(),
            bridge_factory=make_claude,
        )
    )

    # --- Coordinator ---
    coordinator = Coordinator(
        router=router,
        worktree_manager=worktree_mgr,
        merge_gate=merge_gate,
        task_manager=task_mgr,
    )

    # --- Drive a task through the full lifecycle ---
    task = await task_mgr.create(
        goal="Ship the memory summarizer docs",
        inputs={"preferred_agent": "codex"},
    )
    final = await coordinator.submit(task)

    # --- Exit-criterion assertions ---
    assert final.status == TaskStatus.COMPLETED

    # Both agents contributed durable memory under the task scope.
    records = await durable.list_by_scope(MemoryScope.TASK, str(task.id))
    sources = {r.source for r in records}
    assert {"codex", "claude_code"} <= sources

    # Operator can trace every step through the audit log.
    events = await audit.read_all()
    agents_seen = {e.agent_id for e in events if e.agent_id}
    assert {"codex", "claude_code"} <= agents_seen

    kinds = [e.kind for e in events]
    # Task lifecycle is visible.
    assert kinds.count(EventKind.TASK_CREATED) == 1
    assert EventKind.TASK_COMPLETED in kinds
    # Both agents opened + closed a session.
    session_opens = [e for e in events if e.kind == EventKind.SESSION_OPENED]
    assert {e.agent_id for e in session_opens} == {"codex", "claude_code"}
    # Handoff from codex is present.
    handoff_events = [
        e
        for e in events
        if e.kind == EventKind.HANDOFF_INITIATED
        and e.agent_id == "codex"
        and e.payload.get("to_agent") == "claude_code"
    ]
    assert len(handoff_events) >= 1

    # Merge gate was requested AND resolved.
    assert len(merge_gate.resolved()) == 1
    resolved_review = merge_gate.resolved()[0]
    assert resolved_review.task_id == task.id
    assert resolved_review.accepted is True

    # Worktree was actually created on disk.
    worktree_path = Path(resolved_review.worktree_path)
    assert worktree_path.exists()


@pytest.mark.asyncio
async def test_coordinator_fails_on_unknown_handoff_target(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    await _init_repo(repo)

    registry = ToolRegistry()
    register_builtins(registry)
    policy = DeclarativeRuleEngine(rules=default_rules())
    bus = EventBus(policy)
    bus.set_audit_sink(AuditLog(tmp_path / "audit.jsonl").record)
    approvals = ApprovalQueue(bus, auto_approve_resolver)
    executor = ToolExecutor(
        registry=registry, policy=policy, bus=bus, approvals=approvals
    )

    deps = BridgeDeps(
        bus=bus,
        executor=executor,
        session_manager=SessionManager(bus),
        session_memory=SessionMemoryStore(),
        durable_memory=DurableMemoryStore(tmp_path / "mem.db"),
        embedder=DeterministicEmbeddingProvider(),
        summarizer=TruncatingSummarizer(),
    )

    def make_codex(worktree: Path) -> CodexBridge:
        return CodexBridge(
            agent_id="codex",
            deps=deps,
            proc=ScriptedProcess(
                [RequestHandoff(to_agent="nonexistent", expected_output="x")]
            ),
            workspace_path=worktree,
        )

    router = CapabilityRouter()
    router.register(
        AgentRegistration(
            agent_id="codex",
            capability=CodexBridge(
                agent_id="codex",
                deps=deps,
                proc=ScriptedProcess([]),
                workspace_path=None,
            ).capability(),
            bridge_factory=make_codex,
        )
    )

    task_mgr = TaskManager(bus)
    coordinator = Coordinator(
        router=router,
        worktree_manager=WorktreeManager(repo, worktree_root=tmp_path / "wts"),
        merge_gate=MergeGate(bus),
        task_manager=task_mgr,
    )

    task = await task_mgr.create(goal="x", inputs={"preferred_agent": "codex"})
    with pytest.raises(CoordinatorError):
        await coordinator.submit(task)
    assert task_mgr.get(task.id).status == TaskStatus.FAILED
