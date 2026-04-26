from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from exocortex.contracts import Event, EventKind
from exocortex.contracts.common import new_id, now
from exocortex.core.events import EventBus


class MergeReview(BaseModel):
    schema_version: Literal[1] = 1

    id: UUID = Field(default_factory=new_id)
    task_id: UUID
    worktree_path: str
    from_agent: str
    summary: str

    created_at: datetime = Field(default_factory=now)
    resolved_at: datetime | None = None
    accepted: bool | None = None
    operator_note: str = ""


class MergeGate:
    """Records the need for operator review when a task completes. Phase 5
    ships the primitive + auto-resolve default so the exit criterion runs
    end-to-end. Real merge (git merge + PR gate) lands in Phase 5.5 / 7.
    """

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._pending: dict[UUID, MergeReview] = {}
        self._resolved: list[MergeReview] = []

    async def request(
        self,
        *,
        task_id: UUID,
        worktree_path: str,
        from_agent: str,
        summary: str,
    ) -> MergeReview:
        review = MergeReview(
            task_id=task_id,
            worktree_path=worktree_path,
            from_agent=from_agent,
            summary=summary,
        )
        self._pending[review.id] = review
        await self._bus.publish(
            Event(
                kind=EventKind.HANDOFF_INITIATED,
                task_id=task_id,
                agent_id=from_agent,
                payload={
                    "merge_review_id": str(review.id),
                    "worktree_path": worktree_path,
                    "kind": "merge_request",
                },
            )
        )
        return review

    async def resolve(
        self, review_id: UUID, *, accepted: bool, operator_note: str = ""
    ) -> MergeReview:
        review = self._pending.pop(review_id)
        review.accepted = accepted
        review.resolved_at = now()
        review.operator_note = operator_note
        self._resolved.append(review)
        await self._bus.publish(
            Event(
                kind=EventKind.HANDOFF_ACCEPTED,
                task_id=review.task_id,
                agent_id=review.from_agent,
                payload={
                    "merge_review_id": str(review.id),
                    "accepted": accepted,
                    "kind": "merge_resolved",
                },
            )
        )
        return review

    def pending(self) -> list[MergeReview]:
        return list(self._pending.values())

    def resolved(self) -> list[MergeReview]:
        return list(self._resolved)
