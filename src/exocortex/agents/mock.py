from __future__ import annotations

from dataclasses import dataclass, field

from exocortex.contracts import (
    Confidence,
    Decision,
    Event,
    EventKind,
    Handoff,
    MemoryRecord,
    MemoryScope,
    Session,
    Task,
    ToolInvocationCursor,
)
from exocortex.core.events import EventBus
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import EmbeddingProvider
from exocortex.memory.session import SessionMemoryStore
from exocortex.memory.summarizer import Summarizer, build_handoff


@dataclass
class MockAgent:
    """Stand-in for a Bridge/Runner; exists only to exercise the handoff pipe.
    Production adapters arrive in Phase 4 (bridges) and Phase 6 (runners).
    """

    agent_id: str
    event_bus: EventBus
    session_memory: SessionMemoryStore
    durable_memory: DurableMemoryStore
    embedder: EmbeddingProvider
    summarizer: Summarizer
    decisions_made: list[tuple[str, str]] = field(default_factory=list)
    open_questions_raised: list[str] = field(default_factory=list)

    async def record_observation(
        self,
        session: Session,
        content: str,
        *,
        durable: bool = False,
    ) -> MemoryRecord:
        scope = MemoryScope.TASK if durable else MemoryScope.SESSION
        scope_id = str(session.task_id) if durable else str(session.id)
        record = MemoryRecord(
            type="observation",
            content=content,
            source=self.agent_id,
            confidence=Confidence.OBSERVED,
            scope=scope,
            scope_id=scope_id,
        )
        if durable:
            embedding = self.embedder.embed(content)
            await self.durable_memory.write(record, embedding=embedding)
        else:
            self.session_memory.write(record)
        await self.event_bus.publish(
            Event(
                kind=EventKind.MEMORY_WRITTEN,
                task_id=session.task_id,
                session_id=session.id,
                agent_id=self.agent_id,
                payload={"record_id": str(record.id), "durable": durable},
            )
        )
        return record

    def note_decision(self, summary: str, rationale: str) -> None:
        self.decisions_made.append((summary, rationale))

    def raise_question(self, question: str) -> None:
        self.open_questions_raised.append(question)

    async def produce_handoff(
        self,
        *,
        task: Task,
        session: Session,
        to_agent: str,
        sequence_no: int,
        expected_output: str,
        digest_char_budget: int = 2000,
    ) -> tuple[Handoff, str]:
        session_records = self.session_memory.list_session(str(session.id))
        decisions = [
            Decision(summary=s, rationale=r) for s, r in self.decisions_made
        ]
        memory_scope_ids = [
            f"session:{session.id}",
            f"task:{session.task_id}",
        ]
        handoff, digest = build_handoff(
            task=task,
            from_agent=self.agent_id,
            to_agent=to_agent,
            sequence_no=sequence_no,
            session_records=session_records,
            decisions=decisions,
            open_questions=list(self.open_questions_raised),
            workspace=None,
            cursor=ToolInvocationCursor(),
            memory_scope_ids=memory_scope_ids,
            expected_output=expected_output,
            budget_remaining=task.budget,
            summarizer=self.summarizer,
            digest_char_budget=digest_char_budget,
        )
        await self.event_bus.publish(
            Event(
                kind=EventKind.HANDOFF_INITIATED,
                task_id=task.id,
                session_id=session.id,
                agent_id=self.agent_id,
                payload={"to_agent": to_agent, "handoff_id": str(handoff.id)},
            )
        )
        return handoff, digest

    async def accept_handoff(self, handoff: Handoff) -> dict[str, list[MemoryRecord]]:
        """Hydrate context from a handoff bundle. Returns records loaded by scope."""
        loaded: dict[str, list[MemoryRecord]] = {}
        for scope_id in handoff.memory_scope_ids:
            kind, _, sid = scope_id.partition(":")
            if kind == "task":
                loaded[scope_id] = await self.durable_memory.list_by_scope(
                    MemoryScope.TASK, sid
                )
            elif kind == "project":
                loaded[scope_id] = await self.durable_memory.list_by_scope(
                    MemoryScope.PROJECT, sid
                )
            # session-scoped records are not persisted durably; the digest
            # in handoff.goal_restatement carries their compressed form.
        await self.event_bus.publish(
            Event(
                kind=EventKind.HANDOFF_ACCEPTED,
                task_id=handoff.task_id,
                agent_id=self.agent_id,
                payload={
                    "handoff_id": str(handoff.id),
                    "from_agent": handoff.from_agent,
                },
            )
        )
        return loaded
