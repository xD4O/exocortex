"""Shared handoff protocol for the subprocess bridges (B1 + B2).

Two things every real bridge needs, factored out so Codex and Hermes behave
identically and both are unit-testable without spawning a subprocess:

* :func:`compose_agent_prompt` — turn an inbound :class:`Handoff` into the
  prompt the receiving agent actually sees. Previously bridges used only
  ``handoff_in.goal_restatement`` and discarded the constraints, prior
  decisions, open questions, and expected output the bundle carries (B2).

* :func:`parse_handoff_request` / :func:`build_response_actions` — let a real
  agent *initiate* a handoff by emitting a directive in its final message
  (e.g. ``@handoff-to: hermes``). Without this, subprocess bridges could only
  ever emit ``WriteMemory`` + ``TaskDone``, so ``to_agent`` was always empty
  and the coordinator chain terminated after the first hop (B1).
"""

from __future__ import annotations

import re

from exocortex.agents.bridge.actions import (
    AgentAction,
    RequestHandoff,
    TaskDone,
    WriteMemory,
)
from exocortex.contracts import Handoff, Task

# Agents an inbound directive may name. Keeps a typo'd or hallucinated target
# from silently terminating (or misrouting) a chain.
KNOWN_AGENTS: frozenset[str] = frozenset({"hermes", "codex", "claude_code"})

_HANDOFF_TO = re.compile(r"@handoff[-_ ]?to[:=\s]+([a-zA-Z][\w-]*)", re.IGNORECASE)
# The `@handoff` prefix on the expected clause is optional; this only runs
# once a handoff-to directive is confirmed present, so leniency is safe.
_HANDOFF_EXPECTED = re.compile(
    r"(?:@handoff[-_ ]?)?expected[:=\s]+(.+)", re.IGNORECASE
)


def compose_agent_prompt(task: Task, handoff_in: Handoff | None) -> str:
    """Build the prompt for a receiving agent from the full inbound bundle."""
    if handoff_in is None:
        return task.goal

    parts: list[str] = [handoff_in.goal_restatement or task.goal]
    if handoff_in.constraints_active:
        parts.append(
            "Constraints:\n"
            + "\n".join(f"- {c}" for c in handoff_in.constraints_active)
        )
    if handoff_in.decisions_so_far:
        lines = []
        for d in handoff_in.decisions_so_far:
            lines.append(f"- {d.summary} ({d.rationale})" if d.rationale else f"- {d.summary}")
        parts.append("Decisions so far:\n" + "\n".join(lines))
    if handoff_in.open_questions:
        parts.append(
            "Open questions:\n" + "\n".join(f"- {q}" for q in handoff_in.open_questions)
        )
    if handoff_in.expected_output:
        parts.append(f"Expected output:\n{handoff_in.expected_output}")
    return "\n\n".join(parts)


def parse_handoff_request(
    message: str | None, *, known_agents: frozenset[str] = KNOWN_AGENTS
) -> RequestHandoff | None:
    """Extract a handoff directive from an agent's final message, if present.

    Recognizes ``@handoff-to: <agent>`` (and ``@handoff to=<agent>`` etc.) with
    an optional ``@handoff-expected: <text>`` describing what the next agent
    should produce. Returns ``None`` when there is no directive or the named
    agent is unknown (so a typo can't silently misroute or kill the chain)."""
    if not message:
        return None
    to_match = _HANDOFF_TO.search(message)
    if to_match is None:
        return None
    to_agent = to_match.group(1).lower()
    if to_agent not in known_agents:
        return None
    exp_match = _HANDOFF_EXPECTED.search(message)
    expected = exp_match.group(1).strip() if exp_match else ""
    return RequestHandoff(to_agent=to_agent, expected_output=expected)


def build_response_actions(
    response: str,
    *,
    response_type: str,
    known_agents: frozenset[str] = KNOWN_AGENTS,
) -> list[AgentAction]:
    """The action sequence a bridge yields after a run: record the response,
    then either hand off (if the agent asked to) or finish."""
    actions: list[AgentAction] = [
        WriteMemory(content=response, durable=True, type=response_type)
    ]
    request = parse_handoff_request(response, known_agents=known_agents)
    actions.append(request if request is not None else TaskDone(success=True))
    return actions
