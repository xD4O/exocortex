from __future__ import annotations

import pytest

from exocortex.contracts import SessionStatus, TaskStatus
from exocortex.core.fsm import (
    InvalidTransitionError,
    can_session_transition,
    can_task_transition,
    check_session_transition,
    check_task_transition,
)


class TestTaskFSM:
    def test_happy_path(self) -> None:
        # proposed -> routed -> in_progress -> completed
        assert can_task_transition(TaskStatus.PROPOSED, TaskStatus.ROUTED)
        assert can_task_transition(TaskStatus.ROUTED, TaskStatus.IN_PROGRESS)
        assert can_task_transition(TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED)

    def test_cannot_jump_states(self) -> None:
        assert not can_task_transition(TaskStatus.PROPOSED, TaskStatus.IN_PROGRESS)
        assert not can_task_transition(TaskStatus.PROPOSED, TaskStatus.COMPLETED)

    def test_terminal_states_are_terminal(self) -> None:
        for terminal in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELED):
            for target in TaskStatus:
                assert not can_task_transition(terminal, target), (
                    f"{terminal} should not transition to {target}"
                )

    def test_awaiting_can_resume(self) -> None:
        assert can_task_transition(
            TaskStatus.AWAITING_APPROVAL, TaskStatus.IN_PROGRESS
        )
        assert can_task_transition(
            TaskStatus.AWAITING_HANDOFF, TaskStatus.IN_PROGRESS
        )

    def test_check_raises_on_invalid(self) -> None:
        with pytest.raises(InvalidTransitionError):
            check_task_transition(TaskStatus.PROPOSED, TaskStatus.COMPLETED)


class TestSessionFSM:
    def test_happy_path(self) -> None:
        assert can_session_transition(SessionStatus.OPENING, SessionStatus.ACTIVE)
        assert can_session_transition(SessionStatus.ACTIVE, SessionStatus.CLOSED)

    def test_terminated_from_any_non_terminal(self) -> None:
        for s in (
            SessionStatus.OPENING,
            SessionStatus.ACTIVE,
            SessionStatus.AWAITING_INPUT,
            SessionStatus.HANDING_OFF,
        ):
            assert can_session_transition(s, SessionStatus.TERMINATED)

    def test_closed_is_terminal(self) -> None:
        for target in SessionStatus:
            assert not can_session_transition(SessionStatus.CLOSED, target)

    def test_check_raises_on_invalid(self) -> None:
        with pytest.raises(InvalidTransitionError):
            check_session_transition(SessionStatus.CLOSED, SessionStatus.ACTIVE)
