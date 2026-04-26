from __future__ import annotations

from collections.abc import Awaitable, Callable

from exocortex.contracts import Event, PolicyDecisionKind
from exocortex.observability.logging import get_logger
from exocortex.observability.tracing import get_tracer
from exocortex.policy.engine import PolicyEngine

Subscriber = Callable[[Event], Awaitable[None]]

logger = get_logger("exocortex.events")
tracer = get_tracer("exocortex.events")


class EventBus:
    """In-process async event bus with policy-first pipeline.

    Pipeline on publish: policy.evaluate -> attach decision -> audit-log (if
    subscribed) -> deliver to remaining subscribers. Deny decisions are still
    logged; they just don't deliver to non-audit subscribers.
    """

    def __init__(self, policy: PolicyEngine) -> None:
        self._policy = policy
        self._subscribers: list[Subscriber] = []
        self._audit: Subscriber | None = None

    def subscribe(self, handler: Subscriber) -> None:
        self._subscribers.append(handler)

    def set_audit_sink(self, handler: Subscriber) -> None:
        self._audit = handler

    async def publish(self, event: Event) -> None:
        with tracer.start_as_current_span(f"event.{event.kind}") as span:
            span.set_attribute("event.kind", event.kind)
            span.set_attribute("event.id", str(event.id))
            if event.task_id:
                span.set_attribute("task.id", str(event.task_id))

            decision = self._policy.evaluate_event(event)
            event.policy_decision = decision

            # Audit always records, even denies.
            if self._audit is not None:
                try:
                    await self._audit(event)
                except Exception:
                    logger.exception("audit.sink_failed", event_id=str(event.id))

            if decision.kind == PolicyDecisionKind.DENY:
                logger.warning(
                    "event.denied",
                    kind=event.kind,
                    rule_id=decision.rule_id,
                    reason=decision.reason,
                )
                return

            for sub in self._subscribers:
                try:
                    await sub(event)
                except Exception:
                    logger.exception(
                        "subscriber.failed",
                        event_id=str(event.id),
                        handler=str(sub),
                    )
