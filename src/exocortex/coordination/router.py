from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from exocortex.agents.bridge import Bridge
from exocortex.contracts import AgentCapability, Task

BridgeFactory = Callable[[Path], Bridge]


class NoSuitableAgentError(Exception):
    pass


@dataclass(frozen=True)
class AgentRegistration:
    agent_id: str
    capability: AgentCapability
    bridge_factory: BridgeFactory


_CAPABILITY_FLAGS = frozenset(
    {
        "edit_files",
        "run_shell",
        "long_context",
        "structured_output",
        "mcp_client",
        "mcp_server",
        "interactive",
        "batch",
    }
)


class CapabilityRouter:
    """Selects the best agent for a task based on declared capability flags.

    Phase 5 ships a decision-table: explicit-preference takes priority, then
    capability-set match, then first-registered. Upgrades (weighted scoring,
    load-based tie-breaks, learned routing) swap in behind the same API.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, AgentRegistration] = {}
        self._order: list[str] = []

    def register(self, reg: AgentRegistration) -> None:
        if reg.agent_id in self._by_id:
            raise ValueError(f"agent already registered: {reg.agent_id}")
        self._by_id[reg.agent_id] = reg
        self._order.append(reg.agent_id)

    def resolve(self, agent_id: str) -> AgentRegistration:
        try:
            return self._by_id[agent_id]
        except KeyError as e:
            raise NoSuitableAgentError(
                f"agent {agent_id!r} is not registered"
            ) from e

    def route(self, task: Task) -> AgentRegistration:
        preferred = task.inputs.get("preferred_agent")
        if isinstance(preferred, str):
            if preferred not in self._by_id:
                raise NoSuitableAgentError(
                    f"preferred agent {preferred!r} is not registered"
                )
            return self._by_id[preferred]

        required_raw = task.inputs.get("required_capabilities", [])
        required: set[str] = (
            {str(c) for c in required_raw}
            if isinstance(required_raw, list)
            else set()
        )
        unknown = required - _CAPABILITY_FLAGS
        if unknown:
            raise NoSuitableAgentError(
                f"unknown capability flags requested: {sorted(unknown)}"
            )

        for agent_id in self._order:
            reg = self._by_id[agent_id]
            if self._satisfies(reg.capability, required):
                return reg

        raise NoSuitableAgentError(
            f"no registered agent satisfies {sorted(required)}"
        )

    def registered(self) -> list[AgentRegistration]:
        return [self._by_id[a] for a in self._order]

    def find_fallback(
        self,
        *,
        exclude_ids: set[str],
        required: set[str] | None = None,
    ) -> AgentRegistration | None:
        """Next registered agent not in exclude_ids that matches `required`
        capabilities (if given). Returns None if no such agent exists."""
        for agent_id in self._order:
            if agent_id in exclude_ids:
                continue
            reg = self._by_id[agent_id]
            if required and not self._satisfies(reg.capability, required):
                continue
            return reg
        return None

    @staticmethod
    def _satisfies(cap: AgentCapability, required: set[str]) -> bool:
        return all(getattr(cap, flag, False) for flag in required)
