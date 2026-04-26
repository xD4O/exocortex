"""Phase 1 exit criterion (CLAUDE-PLAN.MD §6):

A task can be created, progress through states, emit events, and have every
event policy-checked and audit-logged — even with no real agent attached.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exocortex.contracts import EventKind, PolicyDecisionKind, TaskStatus
from exocortex.core.events import EventBus
from exocortex.core.task_manager import TaskManager
from exocortex.observability.audit import AuditLog
from exocortex.policy.engine import PolicyEngine


@pytest.mark.asyncio
async def test_phase1_exit_criterion(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    bus = EventBus(PolicyEngine())
    bus.set_audit_sink(audit.record)
    tasks = TaskManager(bus)

    task = await tasks.create(goal="Refactor the memory subsystem")
    await tasks.transition(task.id, TaskStatus.ROUTED)
    await tasks.transition(task.id, TaskStatus.IN_PROGRESS)
    await tasks.transition(task.id, TaskStatus.COMPLETED)

    recorded = await audit.read_all()

    # Every transition we took produced at least one event.
    kinds = [e.kind for e in recorded]
    assert EventKind.TASK_CREATED in kinds
    assert EventKind.TASK_STATUS_CHANGED in kinds
    assert EventKind.TASK_COMPLETED in kinds

    # Every recorded event was policy-checked.
    for ev in recorded:
        assert ev.policy_decision is not None, f"no policy decision on {ev.kind}"
        assert ev.policy_decision.kind == PolicyDecisionKind.ALLOW

    # Every recorded event belongs to our task.
    assert {str(e.task_id) for e in recorded} == {str(task.id)}

    # Audit log content is replayable: second read returns the same events.
    replay = await audit.read_all()
    assert [e.id for e in replay] == [e.id for e in recorded]
