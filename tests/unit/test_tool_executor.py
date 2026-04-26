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
from exocortex.policy.approvals import (
    ApprovalQueue,
    auto_approve_resolver,
    auto_deny_resolver,
)
from exocortex.policy.rule_engine import DeclarativeRuleEngine, default_rules
from exocortex.tools.builtin import register_builtins
from exocortex.tools.executor import ToolExecutor
from exocortex.tools.registry import ToolRegistry


def _prov(task_id: str | None = None) -> Provenance:
    return Provenance(agent_id="codex", task_id=uuid4() if task_id is None else uuid4())


def _wire(
    tmp_path: Path,
    *,
    approver: object,
) -> tuple[ToolExecutor, AuditLog]:
    registry = ToolRegistry()
    register_builtins(registry)
    policy = DeclarativeRuleEngine(rules=default_rules())
    audit = AuditLog(tmp_path / "audit.jsonl")
    bus = EventBus(policy)
    bus.set_audit_sink(audit.record)
    approvals = ApprovalQueue(bus, approver)  # type: ignore[arg-type]
    return ToolExecutor(
        registry=registry, policy=policy, bus=bus, approvals=approvals
    ), audit


@pytest.mark.asyncio
async def test_read_inside_worktree_is_auto_approved(tmp_path: Path) -> None:
    worktree = tmp_path / "work"
    worktree.mkdir()
    (worktree / "a.txt").write_text("hello")

    executor, _ = _wire(tmp_path, approver=auto_deny_resolver)

    inv = await executor.invoke(
        tool="fs.read",
        arguments={"path": str(worktree / "a.txt")},
        provenance=_prov(),
        workspace_path=worktree,
    )

    assert inv.approval_state == ApprovalState.SUCCEEDED
    assert inv.policy_decision is not None
    assert inv.policy_decision.kind == PolicyDecisionKind.ALLOW
    assert inv.result is not None
    assert inv.result["content"] == "hello"


@pytest.mark.asyncio
async def test_write_requires_approval_and_executes_when_approved(tmp_path: Path) -> None:
    worktree = tmp_path / "work"
    worktree.mkdir()

    executor, audit = _wire(tmp_path, approver=auto_approve_resolver)

    target = worktree / "out.txt"
    inv = await executor.invoke(
        tool="fs.write",
        arguments={"path": str(target), "content": "payload"},
        provenance=_prov(),
        workspace_path=worktree,
        approval_reason="write demo",
        approval_plan_b="keep in memory",
    )

    assert inv.approval_state == ApprovalState.SUCCEEDED
    assert target.read_text() == "payload"

    events = await audit.read_all()
    kinds = [e.kind for e in events]
    assert EventKind.TOOL_PROPOSED in kinds
    assert EventKind.TOOL_POLICY_CHECKED in kinds
    assert EventKind.APPROVAL_REQUESTED in kinds
    assert EventKind.APPROVAL_RESOLVED in kinds
    assert EventKind.TOOL_APPROVED in kinds
    assert EventKind.TOOL_EXECUTED in kinds


@pytest.mark.asyncio
async def test_write_denied_approval_does_not_execute(tmp_path: Path) -> None:
    worktree = tmp_path / "work"
    worktree.mkdir()

    executor, _ = _wire(tmp_path, approver=auto_deny_resolver)

    target = worktree / "should_not_exist.txt"
    inv = await executor.invoke(
        tool="fs.write",
        arguments={"path": str(target), "content": "nope"},
        provenance=_prov(),
        workspace_path=worktree,
    )

    assert inv.approval_state == ApprovalState.REJECTED
    assert not target.exists()
    assert inv.result is None
