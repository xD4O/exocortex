"""Bridge conformance suite.

Per CLAUDE-PLAN.MD Bet B: CodexBridge and ClaudeCodeBridge must be
interchangeable from the coordination layer's point of view. Both must pass
the *same* parameterized suite — if one passes and the other doesn't, the
contract is leaking provider specifics.

Also enforces the MCP go/no-go check from §6 Phase 4: `core/` (and
`coordination/` once it exists) must contain zero provider-specific branches.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from exocortex.agents.bridge import (
    Bridge,
    BridgeDeps,
    ClaudeCodeBridge,
    CodexBridge,
    HermesBridge,
    InvokeTool,
    NoteDecision,
    RaiseQuestion,
    RequestHandoff,
    ScriptedProcess,
    TaskDone,
    WriteMemory,
)
from exocortex.contracts import (
    AgentCapability,
    Budget,
    EventKind,
    Handoff,
    TaskStatus,
    ToolInvocationCursor,
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

BRIDGE_CLASSES: list[type[Bridge]] = [CodexBridge, ClaudeCodeBridge, HermesBridge]


def _wire(tmp_path: Path) -> tuple[BridgeDeps, AuditLog, TaskManager]:
    worktree = tmp_path / "work"
    worktree.mkdir()

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
    deps = BridgeDeps(
        bus=bus,
        executor=executor,
        session_manager=SessionManager(bus),
        session_memory=SessionMemoryStore(),
        durable_memory=DurableMemoryStore(tmp_path / "mem.db"),
        embedder=DeterministicEmbeddingProvider(),
        summarizer=TruncatingSummarizer(),
    )
    return deps, audit, TaskManager(bus)


@pytest.mark.parametrize("bridge_cls", BRIDGE_CLASSES, ids=lambda c: c.__name__)
def test_capability_declares_bridge_kind(
    bridge_cls: type[Bridge], tmp_path: Path
) -> None:
    deps, _, _ = _wire(tmp_path)
    bridge = bridge_cls(
        agent_id=bridge_cls.__name__,
        deps=deps,
        proc=ScriptedProcess([]),
    )
    cap = bridge.capability()
    assert isinstance(cap, AgentCapability)
    assert cap.kind == "bridge"
    assert cap.mcp_client is True


@pytest.mark.parametrize("bridge_cls", BRIDGE_CLASSES, ids=lambda c: c.__name__)
@pytest.mark.asyncio
async def test_task_produces_normalized_events_and_handoff(
    bridge_cls: type[Bridge], tmp_path: Path
) -> None:
    deps, audit, tasks = _wire(tmp_path)
    worktree = tmp_path / "work"

    (worktree / "input.txt").write_text("seed input", encoding="utf-8")

    script = [
        InvokeTool(
            tool="fs.read",
            arguments={"path": str(worktree / "input.txt")},
        ),
        WriteMemory(content="Observed input: seed input", durable=True),
        WriteMemory(content="scratch note about approach", durable=False),
        NoteDecision(
            summary="Read inputs before writing outputs.",
            rationale="Avoid clobbering operator-provided state.",
        ),
        RaiseQuestion("Should outputs go to out/ or results/?"),
        RequestHandoff(
            to_agent="next-agent",
            expected_output="decision on output directory + passing tests",
        ),
    ]

    task = await tasks.create(goal="Normalize a memory pipeline")
    await tasks.transition(task.id, TaskStatus.ROUTED)
    await tasks.transition(task.id, TaskStatus.IN_PROGRESS)

    bridge = bridge_cls(
        agent_id=bridge_cls.__name__,
        deps=deps,
        proc=ScriptedProcess(script),
        workspace_path=worktree,
    )
    handoff = await bridge.run_task(task)

    # Handoff bundle is populated.
    assert handoff.from_agent == bridge_cls.__name__
    assert handoff.to_agent == "next-agent"
    assert handoff.expected_output.startswith("decision on output directory")
    assert handoff.constraints_active == list(task.constraints)
    assert len(handoff.decisions_so_far) == 1
    assert handoff.decisions_so_far[0].summary.startswith("Read inputs")
    assert handoff.open_questions == ["Should outputs go to out/ or results/?"]
    assert f"task:{task.id}" in handoff.memory_scope_ids
    assert handoff.goal_restatement.startswith("Normalize a memory pipeline")

    # Normalized events were emitted.
    events = await audit.read_all()
    kinds = {e.kind for e in events}
    for required in (
        EventKind.TASK_CREATED,
        EventKind.SESSION_OPENED,
        EventKind.TOOL_PROPOSED,
        EventKind.TOOL_POLICY_CHECKED,
        EventKind.TOOL_EXECUTED,
        EventKind.MEMORY_WRITTEN,
        EventKind.HANDOFF_INITIATED,
        EventKind.SESSION_CLOSED,
    ):
        assert required in kinds, f"{bridge_cls.__name__} missing {required}"

    # Handoff round-trips.
    restored = Handoff.model_validate_json(handoff.model_dump_json())
    assert restored == handoff


@pytest.mark.parametrize("bridge_cls", BRIDGE_CLASSES, ids=lambda c: c.__name__)
@pytest.mark.asyncio
async def test_bridge_hydrates_incoming_handoff(
    bridge_cls: type[Bridge], tmp_path: Path
) -> None:
    deps, _, tasks = _wire(tmp_path)
    worktree = tmp_path / "work"

    task = await tasks.create(goal="Continue prior work")
    await tasks.transition(task.id, TaskStatus.ROUTED)
    await tasks.transition(task.id, TaskStatus.IN_PROGRESS)

    incoming = Handoff(
        task_id=task.id,
        from_agent="prior-agent",
        to_agent=bridge_cls.__name__,
        sequence_no=3,
        goal_restatement="Prior session digest...",
        constraints_active=["no breaking API changes"],
        decisions_so_far=[],
        open_questions=["resolve Q1"],
        tool_invocation_cursor=ToolInvocationCursor(),
        memory_scope_ids=[f"task:{task.id}"],
        expected_output="",
        budget_remaining=Budget(),
    )

    bridge = bridge_cls(
        agent_id=bridge_cls.__name__,
        deps=deps,
        proc=ScriptedProcess([TaskDone(success=True)]),
        workspace_path=worktree,
    )
    out = await bridge.run_task(task, handoff_in=incoming)
    assert out.sequence_no == incoming.sequence_no + 1


def test_core_and_coordination_have_no_provider_specific_branches() -> None:
    """MCP go/no-go audit (CLAUDE-PLAN.MD §6 Phase 4):
    core/ AND coordination/ must have ZERO references to provider-specific
    names. The whole point of Bet B is that routing decisions are
    capability-driven, not name-matched."""
    src_root = Path(__file__).resolve().parents[2] / "src" / "exocortex"
    targets = [src_root / "core", src_root / "coordination"]
    pattern = re.compile(r"\b(codex|claude_code|claude-code|hermes|openclaw)\b", re.I)
    offenders: list[tuple[Path, int, str]] = []
    for base in targets:
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                # Strip comments — we only care about code references.
                code = line.split("#", 1)[0]
                if pattern.search(code):
                    offenders.append((path.relative_to(src_root), lineno, line.rstrip()))
    assert not offenders, (
        "core/ leaked provider-specific names:\n"
        + "\n".join(f"{p}:{ln}: {text}" for p, ln, text in offenders)
    )
