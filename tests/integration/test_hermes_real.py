"""Real-binary integration tests for HermesBridge.

These run the actual `hermes chat -q ...` subprocess and will cost LLM
credits / API tokens. They are SKIPPED by default.

To run them:

    EXOCORTEX_RUN_HERMES=1 uv run pytest tests/integration/test_hermes_real.py -v

You can override the model, and which cheap model to use, via env:

    EXOCORTEX_HERMES_MODEL=anthropic/claude-haiku-4-5 EXOCORTEX_RUN_HERMES=1 \\
        uv run pytest tests/integration/test_hermes_real.py -v
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from exocortex.agents.bridge import (
    CodexBridge,
    HermesBridge,
    HermesSubprocessProcess,
    NoteDecision,
    RaiseQuestion,
    RequestHandoff,
    ScriptedProcess,
    WriteMemory,
)
from exocortex.agents.bridge.base import BridgeDeps
from exocortex.contracts import MemoryScope, TaskStatus
from exocortex.coordination.coordinator import Coordinator
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

RUN_HERMES = os.environ.get("EXOCORTEX_RUN_HERMES") == "1"
HERMES_MODEL = os.environ.get("EXOCORTEX_HERMES_MODEL")

pytestmark = [
    pytest.mark.skipif(
        not RUN_HERMES,
        reason="set EXOCORTEX_RUN_HERMES=1 to run real hermes tests",
    ),
    pytest.mark.skipif(
        shutil.which("hermes") is None, reason="hermes binary not on PATH"
    ),
    pytest.mark.skipif(
        shutil.which("git") is None, reason="git not available"
    ),
]


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
async def test_real_hermes_bridge_completes_simple_task(tmp_path: Path) -> None:
    """Lightest possible real-hermes test: one-shot chat, cheap prompt."""
    repo = tmp_path / "repo"
    await _init_repo(repo)

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

    def make_hermes(worktree: Path) -> HermesBridge:
        return HermesBridge(
            agent_id="hermes",
            deps=deps,
            proc=HermesSubprocessProcess(
                worktree=worktree,
                model=HERMES_MODEL,
                max_turns=2,
            ),
            workspace_path=worktree,
        )

    router = CapabilityRouter()
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

    coordinator = Coordinator(
        router=router,
        worktree_manager=WorktreeManager(repo, worktree_root=tmp_path / "wts"),
        merge_gate=MergeGate(bus),
        task_manager=TaskManager(bus),
    )

    tasks = TaskManager(bus)
    task = await tasks.create(
        goal="What is 2+2? Reply with the single number only, no prose.",
        inputs={"preferred_agent": "hermes"},
    )
    # Reuse the coordinator's task manager so FSM transitions are consistent.
    coordinator._tasks = tasks  # type: ignore[attr-defined]

    final = await coordinator.submit(task)
    assert final.status == TaskStatus.COMPLETED

    records = await deps.durable_memory.list_by_scope(
        MemoryScope.TASK, str(task.id)
    )
    hermes_sources = [r for r in records if r.source == "hermes"]
    assert hermes_sources, "expected at least one durable record from hermes"
    # The response should actually contain SOMETHING. Don't assert the exact
    # answer to avoid flakiness from phrasing / model variance.
    assert any(r.content.strip() for r in hermes_sources)


@pytest.mark.asyncio
async def test_real_codex_to_hermes_handoff(tmp_path: Path) -> None:
    """Scripted Codex hands off to real Hermes. Codex writes durable notes
    + raises a question; Hermes (the real binary) responds."""
    repo = tmp_path / "repo"
    await _init_repo(repo)

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

    codex_script = [
        WriteMemory(
            content="Draft plan: need a single-number answer for the handoff recipient.",
            durable=True,
        ),
        NoteDecision(
            summary="Delegate arithmetic to hermes.",
            rationale="codex scripted, hermes real — cross-agent proof.",
        ),
        RaiseQuestion("What is 2+2?"),
        RequestHandoff(
            to_agent="hermes",
            expected_output="the number 4",
        ),
    ]

    def make_codex(worktree: Path) -> CodexBridge:
        return CodexBridge(
            agent_id="codex",
            deps=deps,
            proc=ScriptedProcess(codex_script),
            workspace_path=worktree,
        )

    def make_hermes(worktree: Path) -> HermesBridge:
        return HermesBridge(
            agent_id="hermes",
            deps=deps,
            proc=HermesSubprocessProcess(
                worktree=worktree, model=HERMES_MODEL, max_turns=2
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

    tasks = TaskManager(bus)
    coordinator = Coordinator(
        router=router,
        worktree_manager=WorktreeManager(repo, worktree_root=tmp_path / "wts"),
        merge_gate=MergeGate(bus),
        task_manager=tasks,
    )

    task = await tasks.create(
        goal="Cross-agent test: codex drafts, hermes answers.",
        inputs={"preferred_agent": "codex"},
    )
    final = await coordinator.submit(task)
    assert final.status == TaskStatus.COMPLETED

    # Both agents should have written durable memory under the task scope.
    records = await deps.durable_memory.list_by_scope(
        MemoryScope.TASK, str(task.id)
    )
    sources = {r.source for r in records}
    assert {"codex", "hermes"} <= sources, (
        f"expected both codex and hermes to contribute durable memory; got {sources}"
    )
