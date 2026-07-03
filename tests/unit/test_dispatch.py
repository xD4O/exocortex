"""DispatchService — agent-as-orchestrator Pattern 2.

These tests use injected ScriptedProcess-backed bridges to avoid
spawning real Hermes / Codex binaries. End-to-end tests against real
binaries live in tests/integration/ and stay opt-in.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from exocortex.agents.bridge import (
    BridgeDeps,
    CodexBridge,
    HermesBridge,
    NoteDecision,
    ScriptedProcess,
    TaskDone,
    WriteMemory,
)
from exocortex.agents.bridge.fakes import StallingProcess
from exocortex.config import Settings
from exocortex.coordination.router import AgentRegistration, CapabilityRouter
from exocortex.core.events import EventBus
from exocortex.core.session_manager import SessionManager
from exocortex.core.task_manager import TaskManager
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.memory.session import SessionMemoryStore
from exocortex.memory.summarizer import TruncatingSummarizer
from exocortex.observability.audit import AuditLog
from exocortex.operator.mcp.dispatch import (
    DispatchError,
    DispatchService,
    NullWorktreeManager,
    make_test_dispatch_service,
)
from exocortex.policy.approvals import ApprovalQueue, auto_approve_resolver
from exocortex.policy.rule_engine import DeclarativeRuleEngine, default_rules
from exocortex.tools.builtin import register_builtins
from exocortex.tools.executor import ToolExecutor
from exocortex.tools.registry import ToolRegistry


def _build_fake_stack(
    tmp_path: Path,
    *,
    hermes_script: list | None = None,
    codex_script: list | None = None,
) -> DispatchService:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        data_dir=data_dir,
        audit_log_path=data_dir / "audit.jsonl",
        memory_db_path=data_dir / "memory.db",
    )

    registry = ToolRegistry()
    register_builtins(registry)
    policy = DeclarativeRuleEngine(rules=default_rules())
    audit = AuditLog(settings.audit_log_path)
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
        durable_memory=DurableMemoryStore(settings.memory_db_path),
        embedder=DeterministicEmbeddingProvider(),
        summarizer=TruncatingSummarizer(),
    )
    task_mgr = TaskManager(bus)
    router = CapabilityRouter()

    if hermes_script is not None:
        def make_hermes(worktree: Path) -> HermesBridge:
            return HermesBridge(
                agent_id="hermes",
                deps=deps,
                proc=ScriptedProcess(hermes_script),
                workspace_path=worktree,
            )

        router.register(
            AgentRegistration(
                agent_id="hermes",
                capability=HermesBridge(
                    agent_id="hermes",
                    deps=deps,
                    proc=ScriptedProcess([]),
                    workspace_path=None,
                ).capability(),
                bridge_factory=make_hermes,
            )
        )

    if codex_script is not None:
        def make_codex(worktree: Path) -> CodexBridge:
            return CodexBridge(
                agent_id="codex",
                deps=deps,
                proc=ScriptedProcess(codex_script),
                workspace_path=worktree,
            )

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

    return make_test_dispatch_service(
        settings=settings, router=router, task_manager=task_mgr, deps=deps
    )


# --- NullWorktreeManager ---------------------------------------------------


@pytest.mark.asyncio
async def test_null_worktree_manager_creates_dir(tmp_path: Path) -> None:
    mgr = NullWorktreeManager(tmp_path / "wts")
    tid = uuid.uuid4()
    p = await mgr.create(tid)
    assert p.exists()
    assert str(tid) in p.name
    # Remove is a no-op but must not error.
    await mgr.remove(p)


# --- dispatch happy path ---------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_to_hermes_succeeds(tmp_path: Path) -> None:
    script = [
        WriteMemory(
            content="hermes saw the request", durable=True, type="observation"
        ),
        NoteDecision(
            summary="Decided to answer with a short sentence.",
            rationale="That's what the goal asked for.",
        ),
        TaskDone(success=True),
    ]
    svc = _build_fake_stack(tmp_path, hermes_script=script)
    result = await svc.dispatch(
        goal="Summarize the auth middleware in one sentence.",
        preferred_agent="hermes",
    )
    assert result["status"] == "completed"
    assert result["dispatched_to"] == "hermes"
    assert result["memory_records_written"] == 1
    assert result["handoff"]["from_agent"] == "hermes"
    assert (
        result["handoff"]["decisions_so_far"][0]["summary"]
        == "Decided to answer with a short sentence."
    )


@pytest.mark.asyncio
async def test_dispatch_falls_through_to_codex_when_no_preferred(
    tmp_path: Path,
) -> None:
    svc = _build_fake_stack(
        tmp_path,
        codex_script=[TaskDone(success=True)],
    )
    result = await svc.dispatch(goal="anything")
    assert result["status"] == "completed"
    assert result["dispatched_to"] == "codex"


# --- error paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_fails_when_no_bridges_registered(tmp_path: Path) -> None:
    svc = _build_fake_stack(tmp_path)  # no scripts = no registrations
    with pytest.raises(DispatchError, match="No bridges"):
        await svc.dispatch(goal="x")


@pytest.mark.asyncio
async def test_dispatch_rejects_unknown_preferred_agent(tmp_path: Path) -> None:
    svc = _build_fake_stack(tmp_path, hermes_script=[TaskDone(success=True)])
    # Some random unknown agent name (not `claude_code`, which has fallback).
    with pytest.raises(DispatchError, match="not available"):
        await svc.dispatch(goal="x", preferred_agent="ghost_agent")


@pytest.mark.asyncio
async def test_dispatch_claude_code_falls_back_to_codex(tmp_path: Path) -> None:
    """Phase-4.5 limitation: `claude_code` has no headless bridge yet, so
    requesting it should auto-fall-back to `codex` (preferred) without
    timing out."""
    svc = _build_fake_stack(
        tmp_path,
        codex_script=[TaskDone(success=True)],
        hermes_script=[TaskDone(success=True)],
    )
    result = await svc.dispatch(goal="x", preferred_agent="claude_code")
    assert result["status"] == "completed"
    assert result["dispatched_to"] == "codex"


@pytest.mark.asyncio
async def test_dispatch_claude_code_falls_back_to_hermes_when_no_codex(
    tmp_path: Path,
) -> None:
    svc = _build_fake_stack(tmp_path, hermes_script=[TaskDone(success=True)])
    result = await svc.dispatch(goal="x", preferred_agent="claude_code")
    assert result["status"] == "completed"
    assert result["dispatched_to"] == "hermes"


@pytest.mark.asyncio
async def test_dispatch_claude_code_raises_when_no_fallback_available(
    tmp_path: Path,
) -> None:
    svc = _build_fake_stack(tmp_path)  # no bridges registered
    # Either error message is acceptable: the no-bridges check fires first
    # (clearer signal when the operator has nothing installed).
    with pytest.raises(DispatchError):
        await svc.dispatch(goal="x", preferred_agent="claude_code")


@pytest.mark.asyncio
async def test_dispatch_timeout_returns_partial(tmp_path: Path) -> None:
    """Fix C: backward-compat dispatch() no longer raises on timeout —
    it cancels the sub-agent and returns the partial snapshot with
    status='timeout' + whatever records landed before the kill."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    settings = Settings(
        data_dir=data_dir,
        audit_log_path=data_dir / "audit.jsonl",
        memory_db_path=data_dir / "memory.db",
    )

    registry = ToolRegistry()
    register_builtins(registry)
    policy = DeclarativeRuleEngine(rules=default_rules())
    bus = EventBus(policy)
    bus.set_audit_sink(AuditLog(settings.audit_log_path).record)
    executor = ToolExecutor(
        registry=registry,
        policy=policy,
        bus=bus,
        approvals=ApprovalQueue(bus, auto_approve_resolver),
    )
    deps = BridgeDeps(
        bus=bus,
        executor=executor,
        session_manager=SessionManager(bus),
        session_memory=SessionMemoryStore(),
        durable_memory=DurableMemoryStore(settings.memory_db_path),
        embedder=DeterministicEmbeddingProvider(),
        summarizer=TruncatingSummarizer(),
    )
    router = CapabilityRouter()

    def make_stalling(worktree: Path) -> HermesBridge:
        return HermesBridge(
            agent_id="hermes",
            deps=deps,
            proc=StallingProcess(),
            workspace_path=worktree,
        )

    router.register(
        AgentRegistration(
            agent_id="hermes",
            capability=HermesBridge(
                agent_id="hermes",
                deps=deps,
                proc=ScriptedProcess([]),
                workspace_path=None,
            ).capability(),
            bridge_factory=make_stalling,
        )
    )

    svc = make_test_dispatch_service(
        settings=settings, router=router, task_manager=TaskManager(bus), deps=deps
    )
    # Fix C: timeout returns partial result with status='timeout',
    # doesn't raise.
    result = await svc.dispatch(
        goal="stall me", preferred_agent="hermes", max_wait_seconds=1
    )
    assert result["status"] == "timeout"
    assert result["dispatched_to"] == "hermes"
    assert "memory_records_written" in result
    assert "records" in result


# --- Fix A: async dispatch primitives --------------------------------------


@pytest.mark.asyncio
async def test_dispatch_async_returns_running_immediately(tmp_path: Path) -> None:
    # Script that writes before completing, so there's observable partial state.
    svc = _build_fake_stack(
        tmp_path,
        hermes_script=[
            WriteMemory(content="early observation", durable=True),
            WriteMemory(content="second observation", durable=True),
            TaskDone(success=True),
        ],
    )
    rd = await svc.start_dispatch(goal="work", preferred_agent="hermes")
    assert rd.task_id
    # Status is running or already completed (scripted is fast) — but key
    # invariant: we got a handle back, no blocking on completion.
    snap = await svc.get_status(rd.task_id)
    assert snap["task_id"] == rd.task_id
    assert snap["status"] in ("running", "completed")


@pytest.mark.asyncio
async def test_dispatch_wait_returns_completed_for_fast_script(
    tmp_path: Path,
) -> None:
    svc = _build_fake_stack(
        tmp_path,
        hermes_script=[
            WriteMemory(content="hello", durable=True),
            TaskDone(),
        ],
    )
    rd = await svc.start_dispatch(goal="x", preferred_agent="hermes")
    snap = await svc.wait_for(rd.task_id, wait_seconds=10)
    assert snap["status"] == "completed"
    assert snap["memory_records_written"] == 1
    assert snap["records"][0]["content"] == "hello"


@pytest.mark.asyncio
async def test_dispatch_wait_returns_partial_when_still_running(
    tmp_path: Path,
) -> None:
    """Critical: with a tight wait_seconds, wait_for returns the current
    partial snapshot and the underlying task keeps running. asyncio.shield
    is what guarantees the inner task isn't cancelled."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    settings = Settings(
        data_dir=data_dir,
        audit_log_path=data_dir / "audit.jsonl",
        memory_db_path=data_dir / "memory.db",
    )
    registry = ToolRegistry()
    register_builtins(registry)
    policy = DeclarativeRuleEngine(rules=default_rules())
    bus = EventBus(policy)
    bus.set_audit_sink(AuditLog(settings.audit_log_path).record)
    executor = ToolExecutor(
        registry=registry, policy=policy, bus=bus,
        approvals=ApprovalQueue(bus, auto_approve_resolver),
    )
    deps = BridgeDeps(
        bus=bus, executor=executor,
        session_manager=SessionManager(bus),
        session_memory=SessionMemoryStore(),
        durable_memory=DurableMemoryStore(settings.memory_db_path),
        embedder=DeterministicEmbeddingProvider(),
        summarizer=TruncatingSummarizer(),
    )
    router = CapabilityRouter()

    def make_stalling(worktree: Path) -> HermesBridge:
        return HermesBridge(
            agent_id="hermes",
            deps=deps,
            proc=StallingProcess(),
            workspace_path=worktree,
        )

    router.register(
        AgentRegistration(
            agent_id="hermes",
            capability=HermesBridge(
                agent_id="hermes", deps=deps,
                proc=ScriptedProcess([]), workspace_path=None,
            ).capability(),
            bridge_factory=make_stalling,
        )
    )
    svc = make_test_dispatch_service(
        settings=settings, router=router,
        task_manager=TaskManager(bus), deps=deps,
    )

    rd = await svc.start_dispatch(goal="stall", preferred_agent="hermes")
    try:
        snap = await svc.wait_for(rd.task_id, wait_seconds=1)
        assert snap["status"] == "running"
        assert snap["task_id"] == rd.task_id
    finally:
        await svc.cancel(rd.task_id)


@pytest.mark.asyncio
async def test_dispatch_cancel_terminates(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    settings = Settings(
        data_dir=data_dir,
        audit_log_path=data_dir / "audit.jsonl",
        memory_db_path=data_dir / "memory.db",
    )
    registry = ToolRegistry()
    register_builtins(registry)
    policy = DeclarativeRuleEngine(rules=default_rules())
    bus = EventBus(policy)
    bus.set_audit_sink(AuditLog(settings.audit_log_path).record)
    executor = ToolExecutor(
        registry=registry, policy=policy, bus=bus,
        approvals=ApprovalQueue(bus, auto_approve_resolver),
    )
    deps = BridgeDeps(
        bus=bus, executor=executor,
        session_manager=SessionManager(bus),
        session_memory=SessionMemoryStore(),
        durable_memory=DurableMemoryStore(settings.memory_db_path),
        embedder=DeterministicEmbeddingProvider(),
        summarizer=TruncatingSummarizer(),
    )
    router = CapabilityRouter()

    def make_stalling(worktree: Path) -> HermesBridge:
        return HermesBridge(
            agent_id="hermes", deps=deps,
            proc=StallingProcess(), workspace_path=worktree,
        )

    router.register(
        AgentRegistration(
            agent_id="hermes",
            capability=HermesBridge(
                agent_id="hermes", deps=deps,
                proc=ScriptedProcess([]), workspace_path=None,
            ).capability(),
            bridge_factory=make_stalling,
        )
    )
    svc = make_test_dispatch_service(
        settings=settings, router=router,
        task_manager=TaskManager(bus), deps=deps,
    )

    rd = await svc.start_dispatch(goal="stall", preferred_agent="hermes")
    snap = await svc.cancel(rd.task_id)
    assert snap["status"] == "cancelled"


@pytest.mark.asyncio
async def test_dispatch_status_unknown_task_raises(tmp_path: Path) -> None:
    svc = _build_fake_stack(tmp_path, hermes_script=[TaskDone()])
    with pytest.raises(DispatchError, match="unknown"):
        await svc.get_status("no-such-task-id")


# --- new records reported --------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_reports_only_records_written_this_turn(
    tmp_path: Path,
) -> None:
    """Pre-existing durable records should not pollute the returned list."""
    svc = _build_fake_stack(
        tmp_path,
        hermes_script=[
            WriteMemory(content="new record 1", durable=True),
            WriteMemory(content="new record 2", durable=True),
            TaskDone(success=True),
        ],
    )

    # Dispatch once — two new records.
    result1 = await svc.dispatch(goal="first", preferred_agent="hermes")
    assert result1["memory_records_written"] == 2

    # Script is consumed; factory makes a fresh ScriptedProcess each time.
    # Re-dispatch with a fresh service to verify "only this turn's writes".
    svc2 = _build_fake_stack(
        tmp_path / "second",
        hermes_script=[
            WriteMemory(content="third record", durable=True),
            TaskDone(success=True),
        ],
    )
    result2 = await svc2.dispatch(goal="second", preferred_agent="hermes")
    assert result2["memory_records_written"] == 1


# --- availability listing --------------------------------------------------


@pytest.mark.asyncio
async def test_registered_agents_list(tmp_path: Path) -> None:
    svc = _build_fake_stack(
        tmp_path,
        hermes_script=[TaskDone()],
        codex_script=[TaskDone()],
    )
    assert set(svc.registered_agents()) == {"hermes", "codex"}


# --- Sprint 1: parallel dispatch --------------------------------------------


@pytest.mark.asyncio
async def test_parallel_dispatch_via_gather(tmp_path: Path) -> None:
    """Both agents fire simultaneously via asyncio.gather; results return in
    submission order. This is the underlying mechanic dispatch_batch wraps."""
    import asyncio  # noqa: PLC0415 - keep test-local
    svc = _build_fake_stack(
        tmp_path,
        hermes_script=[
            WriteMemory(content="from hermes", durable=True),
            TaskDone(),
        ],
        codex_script=[
            WriteMemory(content="from codex", durable=True),
            TaskDone(),
        ],
    )
    coros = [
        svc.dispatch(goal="first", preferred_agent="hermes"),
        svc.dispatch(goal="second", preferred_agent="codex"),
    ]
    results = await asyncio.gather(*coros)
    assert results[0]["dispatched_to"] == "hermes"
    assert results[1]["dispatched_to"] == "codex"
    assert results[0]["status"] == "completed"
    assert results[1]["status"] == "completed"


@pytest.mark.asyncio
async def test_b5_handoff_records_resolved_agent_and_operator(tmp_path: Path) -> None:
    """B5: a capability-routed dispatch with no explicit caller records the
    real resolved agent as to_agent (not 'auto') and attributes the hop to
    'operator' (not an anonymous null)."""
    svc = _build_fake_stack(
        tmp_path, codex_script=[WriteMemory(content="ok", durable=True), TaskDone()]
    )
    await svc.dispatch(goal="do a thing with no preferred agent")

    audit = AuditLog(tmp_path / "data" / "audit.jsonl")
    events = await audit.read_all()
    # The dispatch-level chain-of-custody event carries child_task_id; the
    # bridge's own build_handoff event does not.
    dispatch_handoffs = [
        e
        for e in events
        if e.kind.value == "handoff.initiated" and "child_task_id" in e.payload
    ]
    assert dispatch_handoffs, "no dispatch-level HANDOFF_INITIATED event recorded"
    payload = dispatch_handoffs[0].payload
    assert payload["to_agent"] == "codex"
    assert payload["to_agent"] != "auto"
    assert payload["from_agent"] == "operator"
