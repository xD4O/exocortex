"""Test-only AgentProcess implementations. Lives under src/ so tests can
import it without having to maintain sys.path tricks; none of the
coordination code paths reference these."""

from __future__ import annotations

import asyncio

from exocortex.agents.bridge.actions import AgentAction
from exocortex.contracts import Handoff, Task


class FailingProcess:
    """Raises on start(). Models an agent whose subprocess fails to launch,
    its MCP handshake fails, or similar instant-death scenarios."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error or RuntimeError("simulated agent failure")
        self._alive = True

    @property
    def is_alive(self) -> bool:
        return self._alive

    async def start(
        self, task: Task, handoff_in: Handoff | None = None
    ) -> None:
        _ = task, handoff_in
        self._alive = False
        raise self._error

    async def next_action(self) -> AgentAction | None:
        return None

    async def kill(self) -> None:
        self._alive = False


class StallingProcess:
    """Returns no actions and never completes — start() succeeds but the
    agent then hangs. Used to test per-hop timeouts."""

    def __init__(self) -> None:
        self._alive = True

    @property
    def is_alive(self) -> bool:
        return self._alive

    async def start(
        self, task: Task, handoff_in: Handoff | None = None
    ) -> None:
        _ = task, handoff_in

    async def next_action(self) -> AgentAction | None:
        await asyncio.sleep(10)  # longer than any test timeout
        return None

    async def kill(self) -> None:
        self._alive = False
