from __future__ import annotations

from uuid import UUID

from exocortex.contracts import Event, EventKind, Session, SessionStatus
from exocortex.contracts.common import now
from exocortex.core.events import EventBus
from exocortex.core.fsm import check_session_transition


class SessionManager:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._sessions: dict[UUID, Session] = {}

    async def open(
        self, task_id: UUID, agent_id: str, worktree_path: str | None = None
    ) -> Session:
        session = Session(
            task_id=task_id, agent_id=agent_id, worktree_path=worktree_path
        )
        self._sessions[session.id] = session
        await self._bus.publish(
            Event(
                kind=EventKind.SESSION_OPENED,
                task_id=task_id,
                session_id=session.id,
                agent_id=agent_id,
                payload={"worktree_path": worktree_path},
            )
        )
        return session

    async def transition(self, session_id: UUID, to: SessionStatus) -> Session:
        session = self._sessions[session_id]
        check_session_transition(session.status, to)
        from_status = session.status
        session.status = to
        if to in {SessionStatus.CLOSED, SessionStatus.TERMINATED}:
            session.ended_at = now()
        await self._bus.publish(
            Event(
                kind=EventKind.SESSION_STATUS_CHANGED,
                task_id=session.task_id,
                session_id=session.id,
                agent_id=session.agent_id,
                payload={"from": from_status.value, "to": to.value},
            )
        )
        if to == SessionStatus.CLOSED:
            await self._bus.publish(
                Event(
                    kind=EventKind.SESSION_CLOSED,
                    task_id=session.task_id,
                    session_id=session.id,
                    agent_id=session.agent_id,
                )
            )
        return session

    def get(self, session_id: UUID) -> Session:
        return self._sessions[session_id]

    def all(self) -> list[Session]:
        return list(self._sessions.values())
