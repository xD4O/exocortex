from __future__ import annotations

from exocortex.contracts import SessionStatus, TaskStatus

TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PROPOSED: frozenset({TaskStatus.ROUTED, TaskStatus.CANCELED}),
    TaskStatus.ROUTED: frozenset({TaskStatus.IN_PROGRESS, TaskStatus.CANCELED}),
    TaskStatus.IN_PROGRESS: frozenset(
        {
            TaskStatus.AWAITING_APPROVAL,
            TaskStatus.AWAITING_HANDOFF,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELED,
        }
    ),
    TaskStatus.AWAITING_APPROVAL: frozenset(
        {TaskStatus.IN_PROGRESS, TaskStatus.FAILED, TaskStatus.CANCELED}
    ),
    TaskStatus.AWAITING_HANDOFF: frozenset(
        {TaskStatus.IN_PROGRESS, TaskStatus.FAILED, TaskStatus.CANCELED}
    ),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELED: frozenset(),
}

SESSION_TRANSITIONS: dict[SessionStatus, frozenset[SessionStatus]] = {
    SessionStatus.OPENING: frozenset({SessionStatus.ACTIVE, SessionStatus.TERMINATED}),
    SessionStatus.ACTIVE: frozenset(
        {
            SessionStatus.AWAITING_INPUT,
            SessionStatus.HANDING_OFF,
            SessionStatus.CLOSED,
            SessionStatus.TERMINATED,
        }
    ),
    SessionStatus.AWAITING_INPUT: frozenset(
        {SessionStatus.ACTIVE, SessionStatus.CLOSED, SessionStatus.TERMINATED}
    ),
    SessionStatus.HANDING_OFF: frozenset(
        {SessionStatus.CLOSED, SessionStatus.TERMINATED}
    ),
    SessionStatus.CLOSED: frozenset(),
    SessionStatus.TERMINATED: frozenset(),
}


class InvalidTransitionError(ValueError):
    pass


def can_task_transition(from_: TaskStatus, to: TaskStatus) -> bool:
    return to in TASK_TRANSITIONS.get(from_, frozenset())


def check_task_transition(from_: TaskStatus, to: TaskStatus) -> None:
    if not can_task_transition(from_, to):
        raise InvalidTransitionError(f"Task: {from_.value} -> {to.value} not allowed")


def can_session_transition(from_: SessionStatus, to: SessionStatus) -> bool:
    return to in SESSION_TRANSITIONS.get(from_, frozenset())


def check_session_transition(from_: SessionStatus, to: SessionStatus) -> None:
    if not can_session_transition(from_, to):
        raise InvalidTransitionError(
            f"Session: {from_.value} -> {to.value} not allowed"
        )
