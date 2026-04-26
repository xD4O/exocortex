"""Real-binary integration tests for CodexBridge.

Opt-in only — these invoke the actual `codex exec` subprocess and will cost
LLM credits / API tokens. SKIPPED by default.

Run with:

    EXOCORTEX_RUN_CODEX=1 uv run pytest tests/integration/test_codex_real.py -v

Codex authenticated via a ChatGPT account is restricted to specific models.
Omit EXOCORTEX_CODEX_MODEL to use whatever the account's `codex` config
selects. If you set it, make sure the model is one Codex lets your account
use, or the run will fail with `400 invalid_request_error`.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from exocortex.agents.bridge import (
    ClaudeCodeBridge,
    CodexBridge,
    CodexSubprocessProcess,
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

RUN_CODEX = os.environ.get("EXOCORTEX_RUN_CODEX") == "1"
CODEX_MODEL = os.environ.get("EXOCORTEX_CODEX_MODEL")

pytestmark = [
    pytest.mark.skipif(
        not RUN_CODEX,
        reason="set EXOCORTEX_RUN_CODEX=1 to run real codex tests",
    ),
    pytest.mark.skipif(
        shutil.which("codex") is None, reason="codex binary not on PATH"
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
async def test_real_codex_bridge_completes_simple_task(tmp_path: Path) -> None:
    """Lightest possible real-codex test: one-shot exec, cheap prompt."""
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

    def make_codex(worktree: Path) -> CodexBridge:
        return CodexBridge(
            agent_id="codex",
            deps=deps,
            proc=CodexSubprocessProcess(
                worktree=worktree,
                model=CODEX_MODEL,
                # read-only is safe + fast for arithmetic.
                sandbox_mode="read-only",
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

    tasks = TaskManager(bus)
    coordinator = Coordinator(
        router=router,
        worktree_manager=WorktreeManager(repo, worktree_root=tmp_path / "wts"),
        merge_gate=MergeGate(bus),
        task_manager=tasks,
    )

    task = await tasks.create(
        goal="Reply with only the number: what is 2+2?",
        inputs={"preferred_agent": "codex"},
    )
    final = await coordinator.submit(task)
    assert final.status == TaskStatus.COMPLETED

    records = await deps.durable_memory.list_by_scope(
        MemoryScope.TASK, str(task.id)
    )
    codex_records = [r for r in records if r.source == "codex"]
    assert codex_records, "expected at least one durable record from codex"
    assert any(r.content.strip() for r in codex_records)


@pytest.mark.asyncio
async def test_real_claude_code_scripted_to_codex_handoff(
    tmp_path: Path,
) -> None:
    """Claude Code (scripted) hands off to real Codex.

    Proves cross-agent handoff with a real binary at the receiving end —
    mirroring the Hermes integration test on the Codex side.
    """
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

    claude_script = [
        WriteMemory(
            content="Draft: need codex to verify the arithmetic.",
            durable=True,
        ),
        NoteDecision(
            summary="Delegate verification to codex.",
            rationale="claude_code scripted, codex real — proves the pipe.",
        ),
        RaiseQuestion("What is 2+2?"),
        RequestHandoff(
            to_agent="codex",
            expected_output="the number 4",
        ),
    ]

    def make_claude(worktree: Path) -> ClaudeCodeBridge:
        return ClaudeCodeBridge(
            agent_id="claude_code",
            deps=deps,
            proc=ScriptedProcess(claude_script),
            workspace_path=worktree,
        )

    def make_codex(worktree: Path) -> CodexBridge:
        return CodexBridge(
            agent_id="codex",
            deps=deps,
            proc=CodexSubprocessProcess(
                worktree=worktree,
                model=CODEX_MODEL,
                sandbox_mode="read-only",
            ),
            workspace_path=worktree,
        )

    router = CapabilityRouter()
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

    tasks = TaskManager(bus)
    coordinator = Coordinator(
        router=router,
        worktree_manager=WorktreeManager(repo, worktree_root=tmp_path / "wts"),
        merge_gate=MergeGate(bus),
        task_manager=tasks,
    )

    task = await tasks.create(
        goal="Cross-agent test: claude_code drafts, codex verifies.",
        inputs={"preferred_agent": "claude_code"},
    )
    final = await coordinator.submit(task)
    assert final.status == TaskStatus.COMPLETED

    records = await deps.durable_memory.list_by_scope(
        MemoryScope.TASK, str(task.id)
    )
    sources = {r.source for r in records}
    assert {"claude_code", "codex"} <= sources, (
        f"expected both claude_code and codex to contribute durable memory; got {sources}"
    )
