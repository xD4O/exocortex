"""Phase 0 exit criterion: every contract round-trips Pydantic → JSON → Pydantic."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import BaseModel

from exocortex.contracts import (
    AgentCapability,
    ApprovalRequest,
    Budget,
    Confidence,
    Decision,
    Handoff,
    MemoryRecord,
    MemoryScope,
    PolicyDecision,
    PolicyDecisionKind,
    Provenance,
    Task,
    ToolInvocation,
    ToolInvocationCursor,
    WorkspaceState,
)


def assert_roundtrip(obj: BaseModel) -> None:
    payload = obj.model_dump_json()
    restored = type(obj).model_validate_json(payload)
    assert restored == obj
    assert restored.model_dump_json() == payload


# --- Empty / default-construction round-trips --------------------------------


def test_empty_handoff_roundtrips() -> None:
    """CLAUDE-PLAN.MD §6 Phase 0 exit criterion."""
    handoff = Handoff(
        task_id=uuid4(),
        from_agent="codex",
        to_agent="claude_code",
        sequence_no=0,
        goal_restatement="",
        expected_output="",
    )
    assert_roundtrip(handoff)


def test_empty_task_roundtrips() -> None:
    assert_roundtrip(Task(goal=""))


def test_empty_budget_roundtrips() -> None:
    assert_roundtrip(Budget())


# --- Populated round-trips ---------------------------------------------------


def test_task_roundtrips() -> None:
    task = Task(
        goal="Refactor the auth middleware",
        inputs={"repo": "exocortex", "branch": "main"},
        constraints=["no breaking API changes", "must pass existing tests"],
        owner="claude_code",
        budget=Budget(tokens_limit=100_000, wallclock_seconds=3600, approvals_limit=20),
    )
    assert_roundtrip(task)


def test_memory_record_roundtrips() -> None:
    record = MemoryRecord(
        type="decision",
        content="Chose SQLite over Postgres for MVP; single-operator only.",
        source="operator",
        confidence=Confidence.ASSERTED,
        scope=MemoryScope.PROJECT,
        scope_id="exocortex",
        tags=["storage", "phase-0"],
    )
    assert_roundtrip(record)


def test_tool_invocation_roundtrips() -> None:
    invocation = ToolInvocation(
        tool="shell.exec",
        arguments={"argv": ["git", "status"]},
        provenance=Provenance(agent_id="codex", task_id=uuid4()),
        workspace_ref="worktrees/task-abc",
        policy_decision=PolicyDecision(
            kind=PolicyDecisionKind.ALLOW,
            rule_id="shell.read_only",
            reason="read-only git command",
        ),
    )
    assert_roundtrip(invocation)


def test_approval_request_roundtrips() -> None:
    request = ApprovalRequest(
        invocation_id=uuid4(),
        reason_from_agent="Need to write to /etc/hosts to test local DNS override.",
        plan_b="Skip the DNS override; mock the lookup in the test instead.",
        redacted_context="shell.exec argv=[sudo, tee, /etc/hosts]",
        allowed_duration_seconds=300,
    )
    assert_roundtrip(request)


def test_agent_capability_roundtrips() -> None:
    cap = AgentCapability(
        agent_id="claude_code",
        kind="bridge",
        edit_files=True,
        run_shell=True,
        long_context=True,
        structured_output=True,
        mcp_client=True,
        mcp_server=True,
        interactive=True,
    )
    assert_roundtrip(cap)


def test_populated_handoff_roundtrips() -> None:
    task_id = uuid4()
    inv_a = uuid4()
    inv_b = uuid4()
    memory_ref = uuid4()
    handoff = Handoff(
        task_id=task_id,
        from_agent="codex",
        to_agent="claude_code",
        sequence_no=3,
        goal_restatement="Finish the memory/summarizer module started in session 2.",
        constraints_active=["preserve provenance columns", "no breaking schema changes"],
        decisions_so_far=[
            Decision(
                summary="Use sqlite-vec for semantic search",
                rationale="Stays in-process; avoids pgvector infra for MVP.",
                memory_record_id=memory_ref,
            )
        ],
        open_questions=["What's the TTL policy for session-scoped records?"],
        workspace_state=WorkspaceState(
            repo_ref="abc123def456",
            branch="feat/memory-summarizer",
            worktree_path="worktrees/memory-summarizer",
            untracked_manifest=["scratch/notes.md"],
        ),
        tool_invocation_cursor=ToolInvocationCursor(
            pending_ids=[inv_a],
            completed_ids=[inv_b],
        ),
        memory_scope_ids=[f"task:{task_id}", "project:exocortex"],
        expected_output="Passing tests in tests/unit/memory/; summarizer regression suite green.",
        budget_remaining=Budget(tokens_limit=50_000, approvals_limit=5),
    )
    assert_roundtrip(handoff)


# --- Schema-version guard ----------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        Task(goal=""),
        MemoryRecord(
            type="x",
            content="x",
            source="operator",
            confidence=Confidence.OBSERVED,
            scope=MemoryScope.SESSION,
            scope_id="s1",
        ),
        ToolInvocation(
            tool="fs.read",
            provenance=Provenance(agent_id="a", task_id=uuid4()),
        ),
        Handoff(
            task_id=uuid4(),
            from_agent="a",
            to_agent="b",
            sequence_no=0,
            goal_restatement="",
            expected_output="",
        ),
        ApprovalRequest(
            invocation_id=uuid4(),
            reason_from_agent="",
            plan_b="",
            redacted_context="",
            allowed_duration_seconds=60,
        ),
        AgentCapability(agent_id="a", kind="bridge"),
    ],
)
def test_every_contract_carries_schema_version_1(model: BaseModel) -> None:
    """R12 mitigation: every contract is schema_version=1 from day one."""
    assert model.model_dump()["schema_version"] == 1
