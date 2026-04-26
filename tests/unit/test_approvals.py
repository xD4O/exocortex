from __future__ import annotations

from uuid import uuid4

import pytest

from exocortex.contracts import (
    ApprovalRequest,
    ApprovalResolution,
    Event,
    EventKind,
)
from exocortex.core.events import EventBus
from exocortex.policy.approvals import (
    ApprovalQueue,
    auto_approve_resolver,
    auto_deny_resolver,
)
from exocortex.policy.engine import PolicyEngine


def _request() -> ApprovalRequest:
    return ApprovalRequest(
        invocation_id=uuid4(),
        reason_from_agent="needs a write",
        plan_b="skip and mock",
        redacted_context="fs.write(path=…)",
        allowed_duration_seconds=60,
    )


@pytest.mark.asyncio
async def test_auto_approve_resolver() -> None:
    bus = EventBus(PolicyEngine())
    q = ApprovalQueue(bus, auto_approve_resolver)
    r = _request()
    resolution = await q.submit(r)
    assert resolution == ApprovalResolution.APPROVED
    assert r.resolution == ApprovalResolution.APPROVED
    assert r.resolved_at is not None
    assert q.pending() == []
    assert q.resolved() == [r]


@pytest.mark.asyncio
async def test_auto_deny_resolver() -> None:
    bus = EventBus(PolicyEngine())
    q = ApprovalQueue(bus, auto_deny_resolver)
    r = _request()
    assert await q.submit(r) == ApprovalResolution.DENIED


@pytest.mark.asyncio
async def test_events_are_published() -> None:
    bus = EventBus(PolicyEngine())
    seen: list[Event] = []

    async def recorder(e: Event) -> None:
        seen.append(e)

    bus.subscribe(recorder)
    q = ApprovalQueue(bus, auto_approve_resolver)
    await q.submit(_request())

    kinds = [e.kind for e in seen]
    assert EventKind.APPROVAL_REQUESTED in kinds
    assert EventKind.APPROVAL_RESOLVED in kinds
