from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from exocortex.agents.bridge.actions import (
    AgentAction,
    InvokeTool,
    NoteDecision,
    RaiseQuestion,
    RequestHandoff,
    TaskDone,
    WriteMemory,
)
from exocortex.agents.bridge.process import AgentProcess
from exocortex.contracts import (
    AgentCapability,
    Confidence,
    Decision,
    Event,
    EventKind,
    Handoff,
    MemoryRecord,
    MemoryScope,
    Provenance,
    Session,
    SessionStatus,
    Task,
    ToolInvocationCursor,
)
from exocortex.core.events import EventBus
from exocortex.core.session_manager import SessionManager
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import EmbeddingProvider
from exocortex.memory.session import SessionMemoryStore
from exocortex.memory.summarizer import Summarizer, build_handoff
from exocortex.observability.logging import get_logger
from exocortex.tools.executor import ToolExecutor

logger = get_logger("exocortex.bridge")


@dataclass
class BridgeDeps:
    """Collaborators passed into any Bridge. Keeps the constructor flat and
    makes it obvious that bridges depend only on `core/` + `memory/` + `tools/`
    interfaces — never on provider-specific types. This is the seam that the
    MCP go/no-go check audits (CLAUDE-PLAN.MD §6 Phase 4).
    """

    bus: EventBus
    executor: ToolExecutor
    session_manager: SessionManager
    session_memory: SessionMemoryStore
    durable_memory: DurableMemoryStore
    embedder: EmbeddingProvider
    summarizer: Summarizer


class Bridge(ABC):
    """Thin adapter wrapping an AgentProcess with our normalized event + tool
    + memory + handoff machinery. Subclasses declare provider-specific
    capability; the runtime itself is identical.
    """

    def __init__(
        self,
        *,
        agent_id: str,
        deps: BridgeDeps,
        proc: AgentProcess,
        workspace_path: Path | None = None,
    ) -> None:
        self.agent_id = agent_id
        self._deps = deps
        self._proc = proc
        self._workspace = workspace_path

        self._task: Task | None = None
        self._session: Session | None = None
        self._handoff_in: Handoff | None = None
        self._decisions: list[Decision] = []
        self._questions: list[str] = []
        self._handoff_target: str | None = None
        self._expected_output: str = ""
        self._done: bool = False
        self._killed: bool = False

    # --- Subclass hooks ------------------------------------------------------

    @abstractmethod
    def capability(self) -> AgentCapability: ...

    # --- Lifecycle -----------------------------------------------------------

    async def start(
        self, task: Task, handoff_in: Handoff | None = None
    ) -> Session:
        self._task = task
        self._handoff_in = handoff_in
        self._session = await self._deps.session_manager.open(
            task.id,
            agent_id=self.agent_id,
            worktree_path=str(self._workspace) if self._workspace else None,
        )
        await self._deps.session_manager.transition(
            self._session.id, SessionStatus.ACTIVE
        )
        await self._proc.start(task, handoff_in)
        return self._session

    async def step(self) -> AgentAction | None:
        """Consume one AgentAction and dispatch it. Returns the action
        consumed, or None if the agent is done or has been killed."""
        if self._session is None or self._task is None:
            raise RuntimeError("Bridge.start(...) must be called before step()")
        if self._killed or self._done:
            return None

        action = await self._proc.next_action()
        if action is None:
            return None

        await self._dispatch(action, self._task, self._session)
        return action

    async def run_task(
        self, task: Task, handoff_in: Handoff | None = None
    ) -> Handoff:
        await self.start(task, handoff_in)
        while True:
            action = await self.step()
            if action is None:
                break
        return await self.build_handoff()

    async def kill(self) -> None:
        """Mid-flight kill: stop the agent process, preserve accumulated state.
        State already in memory stores + already-published events survive.
        build_handoff() must remain safe to call after kill().
        """
        self._killed = True
        await self._proc.kill()
        if self._session is not None:
            await self._deps.session_manager.transition(
                self._session.id, SessionStatus.TERMINATED
            )

    async def build_handoff(self) -> Handoff:
        if self._task is None or self._session is None:
            raise RuntimeError("Bridge.start(...) must be called before build_handoff()")

        session_records = self._deps.session_memory.list_session(
            str(self._session.id)
        )
        sequence_no = (
            self._handoff_in.sequence_no + 1 if self._handoff_in is not None else 0
        )
        memory_scope_ids = [
            f"session:{self._session.id}",
            f"task:{self._task.id}",
        ]

        handoff, _digest = build_handoff(
            task=self._task,
            from_agent=self.agent_id,
            to_agent=self._handoff_target or "",
            sequence_no=sequence_no,
            session_records=session_records,
            decisions=list(self._decisions),
            open_questions=list(self._questions),
            workspace=None,
            cursor=ToolInvocationCursor(),
            memory_scope_ids=memory_scope_ids,
            expected_output=self._expected_output,
            budget_remaining=self._task.budget,
            summarizer=self._deps.summarizer,
        )

        await self._deps.bus.publish(
            Event(
                kind=EventKind.HANDOFF_INITIATED,
                task_id=self._task.id,
                session_id=self._session.id,
                agent_id=self.agent_id,
                payload={
                    "handoff_id": str(handoff.id),
                    "to_agent": self._handoff_target or "",
                    "killed": self._killed,
                    "done": self._done,
                },
            )
        )
        if self._session is not None and not self._killed:
            await self._deps.session_manager.transition(
                self._session.id, SessionStatus.HANDING_OFF
            )
            await self._deps.session_manager.transition(
                self._session.id, SessionStatus.CLOSED
            )
        return handoff

    # --- Internal ------------------------------------------------------------

    async def _dispatch(
        self, action: AgentAction, task: Task, session: Session
    ) -> None:
        match action:
            case InvokeTool():
                await self._handle_invoke(action, task, session)
            case WriteMemory():
                await self._handle_memory(action, task, session)
            case NoteDecision():
                self._decisions.append(
                    Decision(summary=action.summary, rationale=action.rationale)
                )
            case RaiseQuestion():
                self._questions.append(action.question)
            case RequestHandoff():
                self._handoff_target = action.to_agent
                self._expected_output = action.expected_output
                self._done = True
            case TaskDone():
                self._done = True

    async def _handle_invoke(
        self, action: InvokeTool, task: Task, session: Session
    ) -> None:
        await self._deps.executor.invoke(
            tool=action.tool,
            arguments=action.arguments,
            provenance=Provenance(
                agent_id=self.agent_id,
                task_id=task.id,
                session_id=session.id,
            ),
            workspace_path=self._workspace,
            approval_reason=action.reason,
            approval_plan_b=action.plan_b,
        )

    async def _handle_memory(
        self, action: WriteMemory, task: Task, session: Session
    ) -> None:
        scope = MemoryScope.TASK if action.durable else MemoryScope.SESSION
        scope_id = str(task.id) if action.durable else str(session.id)
        record = MemoryRecord(
            type=action.type,
            content=action.content,
            source=self.agent_id,
            confidence=Confidence.OBSERVED,
            scope=scope,
            scope_id=scope_id,
        )
        if action.durable:
            embedding = self._deps.embedder.embed(action.content)
            await self._deps.durable_memory.write(record, embedding=embedding)
        else:
            self._deps.session_memory.write(record)
        await self._deps.bus.publish(
            Event(
                kind=EventKind.MEMORY_WRITTEN,
                task_id=task.id,
                session_id=session.id,
                agent_id=self.agent_id,
                payload={"record_id": str(record.id), "durable": action.durable},
            )
        )
