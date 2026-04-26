from __future__ import annotations

import pytest

from exocortex.contracts import (
    Event,
    EventKind,
    PolicyDecision,
    PolicyDecisionKind,
)
from exocortex.core.events import EventBus
from exocortex.policy.engine import PolicyEngine


class _DenyEngine(PolicyEngine):
    def evaluate_event(self, event: Event) -> PolicyDecision:  # type: ignore[override]
        return PolicyDecision(
            kind=PolicyDecisionKind.DENY, rule_id="test.deny_all", reason="deny"
        )


@pytest.mark.asyncio
async def test_publish_delivers_to_all_subscribers() -> None:
    bus = EventBus(PolicyEngine())
    seen_a: list[Event] = []
    seen_b: list[Event] = []

    async def sub_a(e: Event) -> None:
        seen_a.append(e)

    async def sub_b(e: Event) -> None:
        seen_b.append(e)

    bus.subscribe(sub_a)
    bus.subscribe(sub_b)

    await bus.publish(Event(kind=EventKind.TASK_CREATED))

    assert len(seen_a) == 1
    assert len(seen_b) == 1


@pytest.mark.asyncio
async def test_every_published_event_gets_policy_decision_attached() -> None:
    bus = EventBus(PolicyEngine())
    seen: list[Event] = []

    async def sub(e: Event) -> None:
        seen.append(e)

    bus.subscribe(sub)
    await bus.publish(Event(kind=EventKind.TASK_CREATED))

    assert len(seen) == 1
    assert seen[0].policy_decision is not None
    assert seen[0].policy_decision.kind == PolicyDecisionKind.ALLOW


@pytest.mark.asyncio
async def test_audit_sink_receives_denied_events_but_subscribers_do_not() -> None:
    bus = EventBus(_DenyEngine())
    audit_seen: list[Event] = []
    sub_seen: list[Event] = []

    async def audit(e: Event) -> None:
        audit_seen.append(e)

    async def sub(e: Event) -> None:
        sub_seen.append(e)

    bus.set_audit_sink(audit)
    bus.subscribe(sub)

    await bus.publish(Event(kind=EventKind.TOOL_PROPOSED))

    assert len(audit_seen) == 1
    assert audit_seen[0].policy_decision is not None
    assert audit_seen[0].policy_decision.kind == PolicyDecisionKind.DENY
    assert sub_seen == []


@pytest.mark.asyncio
async def test_subscriber_exception_does_not_break_other_subscribers() -> None:
    bus = EventBus(PolicyEngine())
    seen: list[Event] = []

    async def broken(e: Event) -> None:
        raise RuntimeError("boom")

    async def healthy(e: Event) -> None:
        seen.append(e)

    bus.subscribe(broken)
    bus.subscribe(healthy)

    await bus.publish(Event(kind=EventKind.TASK_CREATED))
    assert len(seen) == 1
