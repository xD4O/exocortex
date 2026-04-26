from __future__ import annotations

from uuid import UUID

from exocortex.contracts import Budget, Event, EventKind, Task, TaskStatus
from exocortex.contracts.common import now
from exocortex.core.events import EventBus
from exocortex.core.fsm import check_task_transition


class TaskManager:
    """Owns the in-memory task registry and lifecycle transitions.

    Phase 1 uses an in-memory dict; Phase 2 swaps in durable storage. The event
    stream is the source of truth — the dict is reconstructible from the audit
    log, so crash recovery is a replay.
    """

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._tasks: dict[UUID, Task] = {}

    async def create(
        self,
        goal: str,
        *,
        inputs: dict[str, object] | None = None,
        constraints: list[str] | None = None,
        budget: Budget | None = None,
    ) -> Task:
        task = Task(
            goal=goal,
            inputs=dict(inputs or {}),
            constraints=list(constraints or []),
            budget=budget or Budget(),
        )
        self._tasks[task.id] = task
        await self._bus.publish(
            Event(
                kind=EventKind.TASK_CREATED,
                task_id=task.id,
                payload={"goal": goal, "constraints": task.constraints},
            )
        )
        return task

    async def transition(
        self,
        task_id: UUID,
        to: TaskStatus,
        *,
        error: str | None = None,
    ) -> Task:
        task = self._tasks[task_id]
        check_task_transition(task.status, to)
        from_status = task.status
        task.status = to
        task.updated_at = now()
        await self._bus.publish(
            Event(
                kind=EventKind.TASK_STATUS_CHANGED,
                task_id=task.id,
                payload={"from": from_status.value, "to": to.value},
            )
        )
        # Enrich terminal-state events with the task's goal so debug /
        # trace UIs can show what failed without joining to TASK_CREATED.
        if to == TaskStatus.COMPLETED:
            await self._bus.publish(
                Event(
                    kind=EventKind.TASK_COMPLETED,
                    task_id=task.id,
                    payload={"goal": task.goal},
                )
            )
        elif to == TaskStatus.FAILED:
            payload: dict[str, object] = {"goal": task.goal}
            if error:
                payload["error"] = error
            await self._bus.publish(
                Event(
                    kind=EventKind.TASK_FAILED,
                    task_id=task.id,
                    payload=payload,
                )
            )
        return task

    def get(self, task_id: UUID) -> Task:
        return self._tasks[task_id]

    def all(self) -> list[Task]:
        return list(self._tasks.values())
