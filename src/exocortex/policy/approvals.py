from __future__ import annotations

from collections.abc import Awaitable, Callable

from exocortex.contracts import (
    ApprovalRequest,
    ApprovalResolution,
    Event,
    EventKind,
)
from exocortex.contracts.common import now
from exocortex.core.events import EventBus

Resolver = Callable[[ApprovalRequest], Awaitable[ApprovalResolution]]


async def auto_approve_resolver(_request: ApprovalRequest) -> ApprovalResolution:
    return ApprovalResolution.APPROVED


async def auto_deny_resolver(_request: ApprovalRequest) -> ApprovalResolution:
    return ApprovalResolution.DENIED


class ApprovalQueue:
    """In-memory pending-request queue.

    Phase 3 ships with programmatic resolvers (tests + autonomous flows) and
    keeps the queue local. Phase 4 will add a file-backed queue so the
    operator CLI can approve/deny from a separate process.
    """

    def __init__(self, bus: EventBus, resolver: Resolver) -> None:
        self._bus = bus
        self._resolver = resolver
        self._pending: dict[str, ApprovalRequest] = {}
        self._resolved: list[ApprovalRequest] = []

    async def submit(self, request: ApprovalRequest) -> ApprovalResolution:
        self._pending[str(request.id)] = request
        await self._bus.publish(
            Event(
                kind=EventKind.APPROVAL_REQUESTED,
                payload={
                    "approval_id": str(request.id),
                    "invocation_id": str(request.invocation_id),
                    "reason": request.reason_from_agent,
                    "plan_b": request.plan_b,
                },
            )
        )
        resolution = await self._resolver(request)
        request.resolution = resolution
        request.resolved_at = now()
        self._pending.pop(str(request.id), None)
        self._resolved.append(request)
        await self._bus.publish(
            Event(
                kind=EventKind.APPROVAL_RESOLVED,
                payload={
                    "approval_id": str(request.id),
                    "resolution": resolution.value,
                },
            )
        )
        return resolution

    def pending(self) -> list[ApprovalRequest]:
        return list(self._pending.values())

    def resolved(self) -> list[ApprovalRequest]:
        return list(self._resolved)
