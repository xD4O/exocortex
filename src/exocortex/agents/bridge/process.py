from __future__ import annotations

from typing import Protocol

from exocortex.agents.bridge.actions import AgentAction
from exocortex.contracts import Handoff, Task


class AgentProcess(Protocol):
    """Subprocess-like agent runtime.

    Production implementation (Phase 4.5): spawns claude-code / codex CLI via
    asyncio.subprocess and speaks MCP (JSON-RPC 2.0) over stdio, translating
    tool calls + hook events into AgentActions.

    Test implementation (ScriptedProcess below): yields a pre-defined list of
    AgentActions so bridge lifecycle can be exercised without a real binary.
    """

    @property
    def is_alive(self) -> bool: ...

    async def start(
        self, task: Task, handoff_in: Handoff | None = None
    ) -> None: ...

    async def next_action(self) -> AgentAction | None: ...

    async def kill(self) -> None: ...


class ScriptedProcess:
    """Deterministic test fixture standing in for a real agent subprocess."""

    def __init__(self, actions: list[AgentAction]) -> None:
        self._queue: list[AgentAction] = list(actions)
        self._started = False
        self._alive = True

    @property
    def is_alive(self) -> bool:
        return self._alive

    async def start(
        self, task: Task, handoff_in: Handoff | None = None
    ) -> None:
        _ = task, handoff_in
        self._started = True

    async def next_action(self) -> AgentAction | None:
        if not self._alive or not self._queue:
            return None
        return self._queue.pop(0)

    async def kill(self) -> None:
        self._alive = False
        self._queue.clear()
