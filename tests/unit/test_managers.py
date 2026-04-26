from __future__ import annotations

import pytest

from exocortex.contracts import Event, EventKind, SessionStatus, TaskStatus
from exocortex.core.events import EventBus
from exocortex.core.fsm import InvalidTransitionError
from exocortex.core.session_manager import SessionManager
from exocortex.core.task_manager import TaskManager
from exocortex.policy.engine import PolicyEngine


def _bus_and_recorder() -> tuple[EventBus, list[Event]]:
    bus = EventBus(PolicyEngine())
    seen: list[Event] = []

    async def recorder(e: Event) -> None:
        seen.append(e)

    bus.subscribe(recorder)
    return bus, seen


@pytest.mark.asyncio
async def test_create_emits_task_created() -> None:
    bus, seen = _bus_and_recorder()
    mgr = TaskManager(bus)

    task = await mgr.create(goal="Refactor auth")

    assert task.status == TaskStatus.PROPOSED
    kinds = [e.kind for e in seen]
    assert EventKind.TASK_CREATED in kinds


@pytest.mark.asyncio
async def test_transition_emits_status_changed() -> None:
    bus, seen = _bus_and_recorder()
    mgr = TaskManager(bus)
    task = await mgr.create(goal="x")

    await mgr.transition(task.id, TaskStatus.ROUTED)
    await mgr.transition(task.id, TaskStatus.IN_PROGRESS)
    await mgr.transition(task.id, TaskStatus.COMPLETED)

    status_changes = [e for e in seen if e.kind == EventKind.TASK_STATUS_CHANGED]
    assert len(status_changes) == 3
    assert [e.payload["to"] for e in status_changes] == [
        TaskStatus.ROUTED.value,
        TaskStatus.IN_PROGRESS.value,
        TaskStatus.COMPLETED.value,
    ]
    assert any(e.kind == EventKind.TASK_COMPLETED for e in seen)


@pytest.mark.asyncio
async def test_invalid_transition_raises_and_emits_nothing_new() -> None:
    bus, seen = _bus_and_recorder()
    mgr = TaskManager(bus)
    task = await mgr.create(goal="x")
    baseline = len(seen)

    with pytest.raises(InvalidTransitionError):
        await mgr.transition(task.id, TaskStatus.COMPLETED)

    assert len(seen) == baseline


@pytest.mark.asyncio
async def test_session_open_and_close_lifecycle() -> None:
    bus, seen = _bus_and_recorder()
    tasks = TaskManager(bus)
    sessions = SessionManager(bus)

    task = await tasks.create(goal="x")
    session = await sessions.open(task.id, agent_id="codex", worktree_path="wt/abc")

    assert session.status == SessionStatus.OPENING

    await sessions.transition(session.id, SessionStatus.ACTIVE)
    await sessions.transition(session.id, SessionStatus.CLOSED)

    kinds = [e.kind for e in seen]
    assert EventKind.SESSION_OPENED in kinds
    assert EventKind.SESSION_CLOSED in kinds
    assert sessions.get(session.id).ended_at is not None
