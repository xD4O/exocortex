"""Phase 5.5: retry / timeout / fallback policies in the Coordinator.

Covers: retry recovers, retry exhausts, timeout enforced, fallback switches
to alternate agent, fallback disabled respects that.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from exocortex.agents.bridge import (
    ClaudeCodeBridge,
    CodexBridge,
    ScriptedProcess,
    TaskDone,
    WriteMemory,
)
from exocortex.agents.bridge.base import BridgeDeps
from exocortex.agents.bridge.fakes import FailingProcess, StallingProcess
from exocortex.contracts import MemoryScope, TaskStatus
from exocortex.coordination.coordinator import Coordinator, CoordinatorError
from exocortex.coordination.merge_gate import MergeGate
from exocortex.coordination.policies import (
    CoordinatorPolicies,
    FallbackPolicy,
    RetryPolicy,
    TimeoutPolicy,
)
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


def _wire(tmp_path: Path) -> tuple[BridgeDeps, TaskManager, CapabilityRouter]:
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
    return deps, TaskManager(bus), CapabilityRouter()


def _success_script() -> list:
    return [
        WriteMemory(content="agent worked", durable=True),
        TaskDone(success=True),
    ]


# --- Retry ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_recovers_on_last_attempt(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    await _init_repo(repo)
    deps, tasks, router = _wire(tmp_path)

    attempts = {"n": 0}

    def flaky_factory(worktree: Path) -> CodexBridge:
        attempts["n"] += 1
        proc = (
            FailingProcess() if attempts["n"] < 3 else ScriptedProcess(_success_script())
        )
        return CodexBridge(
            agent_id="codex",
            deps=deps,
            proc=proc,
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
            bridge_factory=flaky_factory,
        )
    )

    coordinator = Coordinator(
        router=router,
        worktree_manager=WorktreeManager(repo, worktree_root=tmp_path / "wts"),
        merge_gate=MergeGate(deps.bus),
        task_manager=tasks,
        policies=CoordinatorPolicies(retry=RetryPolicy(max_attempts=3)),
    )

    task = await tasks.create(goal="x", inputs={"preferred_agent": "codex"})
    result = await coordinator.submit(task)
    assert result.status == TaskStatus.COMPLETED
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_retry_exhausted_fails_task(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    await _init_repo(repo)
    deps, tasks, router = _wire(tmp_path)

    def always_broken(worktree: Path) -> CodexBridge:
        return CodexBridge(
            agent_id="codex",
            deps=deps,
            proc=FailingProcess(RuntimeError("persistent failure")),
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
            bridge_factory=always_broken,
        )
    )

    coordinator = Coordinator(
        router=router,
        worktree_manager=WorktreeManager(repo, worktree_root=tmp_path / "wts"),
        merge_gate=MergeGate(deps.bus),
        task_manager=tasks,
        policies=CoordinatorPolicies(retry=RetryPolicy(max_attempts=2)),
    )

    task = await tasks.create(goal="x", inputs={"preferred_agent": "codex"})
    with pytest.raises(CoordinatorError) as ei:
        await coordinator.submit(task)
    assert "codex" in str(ei.value)
    assert tasks.get(task.id).status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_default_no_retry(tmp_path: Path) -> None:
    """Phase 5 default behavior: one failure → task failed, no second attempt."""
    repo = tmp_path / "repo"
    await _init_repo(repo)
    deps, tasks, router = _wire(tmp_path)

    attempts = {"n": 0}

    def counting_broken(worktree: Path) -> CodexBridge:
        attempts["n"] += 1
        return CodexBridge(
            agent_id="codex",
            deps=deps,
            proc=FailingProcess(),
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
            bridge_factory=counting_broken,
        )
    )

    coordinator = Coordinator(
        router=router,
        worktree_manager=WorktreeManager(repo, worktree_root=tmp_path / "wts"),
        merge_gate=MergeGate(deps.bus),
        task_manager=tasks,
    )

    task = await tasks.create(goal="x", inputs={"preferred_agent": "codex"})
    with pytest.raises(CoordinatorError):
        await coordinator.submit(task)
    assert attempts["n"] == 1


# --- Timeout ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_triggers_failure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    await _init_repo(repo)
    deps, tasks, router = _wire(tmp_path)

    def stalling(worktree: Path) -> CodexBridge:
        return CodexBridge(
            agent_id="codex",
            deps=deps,
            proc=StallingProcess(),
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
            bridge_factory=stalling,
        )
    )

    coordinator = Coordinator(
        router=router,
        worktree_manager=WorktreeManager(repo, worktree_root=tmp_path / "wts"),
        merge_gate=MergeGate(deps.bus),
        task_manager=tasks,
        policies=CoordinatorPolicies(
            timeout=TimeoutPolicy(per_hop_seconds=0.1)
        ),
    )

    task = await tasks.create(goal="x", inputs={"preferred_agent": "codex"})
    with pytest.raises(CoordinatorError):
        await coordinator.submit(task)
    assert tasks.get(task.id).status == TaskStatus.FAILED


# --- Fallback ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_to_alternate_agent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    await _init_repo(repo)
    deps, tasks, router = _wire(tmp_path)

    def broken_codex(worktree: Path) -> CodexBridge:
        return CodexBridge(
            agent_id="codex",
            deps=deps,
            proc=FailingProcess(RuntimeError("codex down")),
            workspace_path=worktree,
        )

    def working_claude(worktree: Path) -> ClaudeCodeBridge:
        return ClaudeCodeBridge(
            agent_id="claude_code",
            deps=deps,
            proc=ScriptedProcess(_success_script()),
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
            bridge_factory=broken_codex,
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
            bridge_factory=working_claude,
        )
    )

    coordinator = Coordinator(
        router=router,
        worktree_manager=WorktreeManager(repo, worktree_root=tmp_path / "wts"),
        merge_gate=MergeGate(deps.bus),
        task_manager=tasks,
        policies=CoordinatorPolicies(
            fallback=FallbackPolicy(enabled=True, max_alternatives=2),
        ),
    )

    task = await tasks.create(goal="x", inputs={"preferred_agent": "codex"})
    result = await coordinator.submit(task)
    assert result.status == TaskStatus.COMPLETED

    # Durable memory should carry the claude_code contribution, not codex.
    records = await deps.durable_memory.list_by_scope(
        MemoryScope.TASK, str(task.id)
    )
    sources = {r.source for r in records}
    assert "claude_code" in sources


@pytest.mark.asyncio
async def test_fallback_disabled_does_not_switch(tmp_path: Path) -> None:
    """Even with a healthy alternative registered, fallback=disabled means
    a broken preferred agent still fails the task."""
    repo = tmp_path / "repo"
    await _init_repo(repo)
    deps, tasks, router = _wire(tmp_path)

    def broken(worktree: Path) -> CodexBridge:
        return CodexBridge(
            agent_id="codex",
            deps=deps,
            proc=FailingProcess(),
            workspace_path=worktree,
        )

    def working(worktree: Path) -> ClaudeCodeBridge:
        return ClaudeCodeBridge(
            agent_id="claude_code",
            deps=deps,
            proc=ScriptedProcess(_success_script()),
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
            bridge_factory=broken,
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
            bridge_factory=working,
        )
    )

    coordinator = Coordinator(
        router=router,
        worktree_manager=WorktreeManager(repo, worktree_root=tmp_path / "wts"),
        merge_gate=MergeGate(deps.bus),
        task_manager=tasks,
        policies=CoordinatorPolicies(),  # fallback not enabled
    )

    task = await tasks.create(goal="x", inputs={"preferred_agent": "codex"})
    with pytest.raises(CoordinatorError):
        await coordinator.submit(task)
    assert tasks.get(task.id).status == TaskStatus.FAILED
