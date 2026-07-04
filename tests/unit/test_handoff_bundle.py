"""B1/B2 at the bridge level: the built handoff carries the agent's actual
work (durable digest), a workspace snapshot, and an agent-initiated target."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from exocortex.agents.bridge import CodexBridge, ScriptedProcess
from exocortex.agents.bridge.actions import TaskDone, WriteMemory
from exocortex.agents.bridge.base import BridgeDeps
from exocortex.agents.bridge.protocol import build_response_actions
from exocortex.contracts import Task
from exocortex.core.events import EventBus
from exocortex.core.session_manager import SessionManager
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


def _deps(tmp_path: Path) -> BridgeDeps:
    registry = ToolRegistry()
    register_builtins(registry)
    policy = DeclarativeRuleEngine(rules=default_rules())
    bus = EventBus(policy)
    bus.set_audit_sink(AuditLog(tmp_path / "a.jsonl").record)
    approvals = ApprovalQueue(bus, auto_approve_resolver)
    executor = ToolExecutor(
        registry=registry, policy=policy, bus=bus, approvals=approvals
    )
    return BridgeDeps(
        bus=bus,
        executor=executor,
        session_manager=SessionManager(bus),
        session_memory=SessionMemoryStore(),
        durable_memory=DurableMemoryStore(tmp_path / "mem.db"),
        embedder=DeterministicEmbeddingProvider(),
        summarizer=TruncatingSummarizer(),
    )


@pytest.mark.asyncio
async def test_handoff_digests_durable_work(tmp_path: Path) -> None:
    """The agent's durable response must show up in the outbound bundle —
    previously the digest read session memory only and came back empty."""
    deps = _deps(tmp_path)
    bridge = CodexBridge(
        agent_id="codex",
        deps=deps,
        proc=ScriptedProcess(
            [
                WriteMemory(
                    content="I chose the incremental migration over a big-bang.",
                    durable=True,
                    type="codex_response",
                ),
                TaskDone(success=True),
            ]
        ),
    )
    handoff = await bridge.run_task(Task(goal="Plan the DB migration"))
    assert "incremental migration" in handoff.goal_restatement


@pytest.mark.asyncio
async def test_handoff_populates_workspace_state_from_git(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    ident = ["-c", "user.email=t@t", "-c", "user.name=t"]
    for args in (
        ["init", "-q"],
        [*ident, "commit", "--allow-empty", "-q", "-m", "init"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True)  # noqa: S603
    (repo / "scratch.txt").write_text("wip", encoding="utf-8")

    deps = _deps(tmp_path)
    bridge = CodexBridge(
        agent_id="codex",
        deps=deps,
        proc=ScriptedProcess([TaskDone(success=True)]),
        workspace_path=repo,
    )
    handoff = await bridge.run_task(Task(goal="do work in the repo"))
    ws = handoff.workspace_state
    assert ws is not None
    assert len(ws.repo_ref) >= 7  # a commit sha
    assert "scratch.txt" in ws.untracked_manifest


@pytest.mark.asyncio
async def test_agent_initiated_handoff_sets_target(tmp_path: Path) -> None:
    """A @handoff-to directive in the response makes the outbound bundle name
    the next agent — the coordinator can now continue past hop 0 (B1)."""
    deps = _deps(tmp_path)
    # build_response_actions is exactly what the real subprocess bridge yields
    # from an agent message; here we feed those actions through the base bridge.
    actions = build_response_actions(
        "Analysis done.\n@handoff-to: hermes\n@handoff-expected: write the final report",
        response_type="codex_response",
    )
    bridge = CodexBridge(agent_id="codex", deps=deps, proc=ScriptedProcess(actions))
    handoff = await bridge.run_task(Task(goal="analyze then hand off"))
    assert handoff.to_agent == "hermes"
    assert handoff.expected_output == "write the final report"
