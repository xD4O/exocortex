"""Phase 3 exit criterion (CLAUDE-PLAN.MD §6):

  "A shell command that writes outside the worktree is *rejected* by policy,
   not merely logged."

This is the R2 composition-escape protection in practice: deny-by-default +
worktree-confinement rules + executor that honors a DENY decision.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from exocortex.contracts import (
    ApprovalState,
    EventKind,
    PolicyDecisionKind,
    Provenance,
)
from exocortex.core.events import EventBus
from exocortex.observability.audit import AuditLog
from exocortex.policy.approvals import ApprovalQueue, auto_approve_resolver
from exocortex.policy.rule_engine import DeclarativeRuleEngine, default_rules
from exocortex.tools.builtin import register_builtins
from exocortex.tools.executor import ToolExecutor
from exocortex.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_shell_writing_outside_worktree_is_rejected(tmp_path: Path) -> None:
    worktree = tmp_path / "work"
    worktree.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    forbidden_target = outside / "should_not_be_created.txt"

    registry = ToolRegistry()
    register_builtins(registry)

    policy = DeclarativeRuleEngine(rules=default_rules())
    audit = AuditLog(tmp_path / "audit.jsonl")
    bus = EventBus(policy)
    bus.set_audit_sink(audit.record)
    # auto_approve would approve *if* the request ever reached it — it must not.
    approvals = ApprovalQueue(bus, auto_approve_resolver)
    executor = ToolExecutor(
        registry=registry, policy=policy, bus=bus, approvals=approvals
    )

    inv = await executor.invoke(
        tool="shell.exec",
        arguments={
            "argv": ["/bin/sh", "-c", f"touch {forbidden_target}"],
            "cwd": str(outside),
        },
        provenance=Provenance(agent_id="codex", task_id=uuid4()),
        workspace_path=worktree,
    )

    # Rejected by policy.
    assert inv.approval_state == ApprovalState.REJECTED
    assert inv.policy_decision is not None
    assert inv.policy_decision.kind == PolicyDecisionKind.DENY

    # Critically: the command did NOT run.
    assert not forbidden_target.exists(), (
        "policy should have prevented the shell command from touching outside the worktree"
    )

    # Approval was never requested — deny short-circuits before the queue.
    events = await audit.read_all()
    kinds = [e.kind for e in events]
    assert EventKind.TOOL_REJECTED in kinds
    assert EventKind.APPROVAL_REQUESTED not in kinds
    assert EventKind.TOOL_EXECUTED not in kinds
    assert EventKind.TOOL_APPROVED not in kinds


@pytest.mark.asyncio
async def test_shell_inside_worktree_requires_approval_not_denied(tmp_path: Path) -> None:
    """Companion to the exit test: inside-worktree is not denied, it goes to approval."""
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

    inv = await executor.invoke(
        tool="shell.exec",
        arguments={"argv": ["/bin/echo", "hello"], "cwd": str(worktree)},
        provenance=Provenance(agent_id="codex", task_id=uuid4()),
        workspace_path=worktree,
    )

    assert inv.approval_state == ApprovalState.SUCCEEDED
    assert inv.policy_decision is not None
    assert inv.policy_decision.kind == PolicyDecisionKind.REQUIRE_APPROVAL
