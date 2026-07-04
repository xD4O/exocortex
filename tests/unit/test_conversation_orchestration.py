"""B3/B4: conversation turn synthesis prefers the agent's real reply (not the
echoed instruction prompt), and attributes substituted turns honestly."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from exocortex.coordination.conversation import (
    ConversationService,
    _extract_agent_reply,
    run_rounds,
)
from exocortex.observability.audit import AuditLog

# --- B3: reply extraction ---------------------------------------------------

def test_extract_prefers_response_record_over_goal_echo() -> None:
    goal = "You are codex. Transcript... call conversation_turn(...)"
    result = {
        "handoff": {"goal_restatement": goal + "\n\n---\ndigest"},
        "records": [
            {"type": "observation", "content": "looked around"},
            {"type": "codex_response", "content": "I think we should ship incrementally."},
        ],
    }
    assert _extract_agent_reply(result, dispatched_goal=goal) == (
        "I think we should ship incrementally."
    )


def test_extract_never_echoes_dispatched_goal() -> None:
    goal = "You are codex. Do the thing. call conversation_turn(...)"
    # No records, goal_restatement just repeats the instruction prompt.
    result = {"handoff": {"goal_restatement": goal}, "records": []}
    assert _extract_agent_reply(result, dispatched_goal=goal) == ""


def test_extract_uses_goal_restatement_when_genuinely_new() -> None:
    result = {"handoff": {"goal_restatement": "a real distinct summary"}, "records": []}
    assert _extract_agent_reply(result, dispatched_goal="unrelated goal") == (
        "a real distinct summary"
    )


# --- B4: honest attribution on substitution ---------------------------------


class _FakeDispatcher:
    """Stands in for DispatchService: claude_code runs on codex, and the run
    does not itself land a conversation_turn (so run_rounds must synthesize)."""

    def __init__(self) -> None:
        self.dispatched_from: list[str] = []

    async def resolve_effective_agent(self, preferred: str | None) -> str | None:
        return "codex" if preferred == "claude_code" else preferred

    async def dispatch(
        self, *, goal: str, preferred_agent: str, from_agent: str, **_: Any
    ) -> dict[str, Any]:
        self.dispatched_from.append(from_agent)
        return {
            "status": "completed",
            "dispatched_to": "codex" if preferred_agent == "claude_code" else preferred_agent,
            "records": [
                {"type": "codex_response", "content": f"{from_agent} says hi"}
            ],
            "handoff": {"goal_restatement": goal},  # the echo trap
        }


@pytest.mark.asyncio
async def test_substituted_turn_attributed_to_effective_agent(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    service = ConversationService(audit=audit)
    convo = await service.open(topic="design", participants=["claude_code", "hermes"])

    dispatcher = _FakeDispatcher()
    await run_rounds(
        service=service,
        dispatcher=dispatcher,
        conversation_id=convo.id,
        rounds=1,
    )

    snapshot = await service.get(convo.id)
    speakers = {t["from_agent"] for t in snapshot["turns"]}
    # The claude_code turn must be attributed to codex (who actually ran it),
    # never claude_code (B4). hermes speaks as itself.
    assert "codex" in speakers
    assert "claude_code" not in speakers
    # And the synthesized content is the agent's reply, not the echoed prompt.
    contents = [t["content"] for t in snapshot["turns"]]
    assert any("says hi" in c for c in contents)
    assert not any("conversation_turn" in c for c in contents)
