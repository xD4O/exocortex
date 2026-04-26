from __future__ import annotations

from uuid import uuid4

import pytest

from exocortex.contracts import Event, EventKind
from exocortex.coordination.merge_gate import MergeGate
from exocortex.core.events import EventBus
from exocortex.policy.engine import PolicyEngine


@pytest.mark.asyncio
async def test_request_and_resolve() -> None:
    bus = EventBus(PolicyEngine())
    seen: list[Event] = []

    async def recorder(e: Event) -> None:
        seen.append(e)

    bus.subscribe(recorder)

    gate = MergeGate(bus)
    review = await gate.request(
        task_id=uuid4(),
        worktree_path="/tmp/wt/task-x",
        from_agent="codex",
        summary="done",
    )
    assert gate.pending() == [review]
    assert gate.resolved() == []

    resolved = await gate.resolve(review.id, accepted=True, operator_note="LGTM")
    assert resolved.accepted is True
    assert resolved.operator_note == "LGTM"
    assert resolved.resolved_at is not None
    assert gate.pending() == []
    assert gate.resolved() == [resolved]

    kinds = [e.kind for e in seen]
    assert EventKind.HANDOFF_INITIATED in kinds
    assert EventKind.HANDOFF_ACCEPTED in kinds


@pytest.mark.asyncio
async def test_reject_path() -> None:
    bus = EventBus(PolicyEngine())
    gate = MergeGate(bus)
    review = await gate.request(
        task_id=uuid4(),
        worktree_path="/tmp/wt/x",
        from_agent="codex",
        summary="iffy",
    )
    rejected = await gate.resolve(review.id, accepted=False, operator_note="unsafe")
    assert rejected.accepted is False
    assert rejected.operator_note == "unsafe"
