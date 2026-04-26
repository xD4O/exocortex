from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from exocortex.contracts import AgentCapability, Task
from exocortex.coordination.router import (
    AgentRegistration,
    CapabilityRouter,
    NoSuitableAgentError,
)


def _reg(agent_id: str, **flags: bool) -> AgentRegistration:
    cap = AgentCapability(agent_id=agent_id, kind="bridge", **flags)
    return AgentRegistration(
        agent_id=agent_id, capability=cap, bridge_factory=lambda _p: MagicMock()
    )


def test_explicit_preference_wins() -> None:
    r = CapabilityRouter()
    r.register(_reg("codex", edit_files=True))
    r.register(_reg("claude_code", edit_files=True))

    task = Task(goal="x", inputs={"preferred_agent": "claude_code"})
    assert r.route(task).agent_id == "claude_code"


def test_preference_unknown_raises() -> None:
    r = CapabilityRouter()
    r.register(_reg("codex", edit_files=True))
    task = Task(goal="x", inputs={"preferred_agent": "nonexistent"})
    with pytest.raises(NoSuitableAgentError):
        r.route(task)


def test_capability_match_first_registered_wins() -> None:
    r = CapabilityRouter()
    r.register(_reg("codex", edit_files=True, run_shell=True))
    r.register(_reg("claude_code", edit_files=True, run_shell=True))
    task = Task(
        goal="x",
        inputs={"required_capabilities": ["edit_files", "run_shell"]},
    )
    assert r.route(task).agent_id == "codex"


def test_unsatisfiable_capability_raises() -> None:
    r = CapabilityRouter()
    r.register(_reg("codex", edit_files=True))
    task = Task(goal="x", inputs={"required_capabilities": ["mcp_server"]})
    with pytest.raises(NoSuitableAgentError):
        r.route(task)


def test_unknown_capability_flag_raises() -> None:
    r = CapabilityRouter()
    r.register(_reg("codex"))
    task = Task(goal="x", inputs={"required_capabilities": ["teleportation"]})
    with pytest.raises(NoSuitableAgentError):
        r.route(task)


def test_resolve_returns_registered_agent() -> None:
    r = CapabilityRouter()
    r.register(_reg("codex", edit_files=True))
    assert r.resolve("codex").agent_id == "codex"
    with pytest.raises(NoSuitableAgentError):
        r.resolve("missing")


def test_duplicate_registration_raises() -> None:
    r = CapabilityRouter()
    r.register(_reg("codex"))
    with pytest.raises(ValueError):
        r.register(_reg("codex"))


def test_bridge_factory_gets_worktree_path(tmp_path: Path) -> None:
    received: list[Path] = []

    def factory(p: Path) -> MagicMock:
        received.append(p)
        return MagicMock()

    cap = AgentCapability(agent_id="codex", kind="bridge")
    reg = AgentRegistration(
        agent_id="codex", capability=cap, bridge_factory=factory
    )
    reg.bridge_factory(tmp_path / "work")
    assert received == [tmp_path / "work"]
