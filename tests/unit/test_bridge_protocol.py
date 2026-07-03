"""B1/B2: the shared bridge handoff protocol — prompt composition from the
full bundle, and parsing an agent-initiated handoff directive."""

from __future__ import annotations

from exocortex.agents.bridge.actions import RequestHandoff, TaskDone, WriteMemory
from exocortex.agents.bridge.protocol import (
    build_response_actions,
    compose_agent_prompt,
    parse_handoff_request,
)
from exocortex.contracts import Decision, Handoff, Task


def _handoff(**overrides) -> Handoff:
    base = dict(
        task_id=Task(goal="x").id,
        from_agent="codex",
        to_agent="hermes",
        sequence_no=1,
        goal_restatement="Ship the migration",
        expected_output="",
    )
    base.update(overrides)
    return Handoff(**base)


def test_compose_prompt_no_handoff_is_just_goal() -> None:
    assert compose_agent_prompt(Task(goal="do the thing"), None) == "do the thing"


def test_compose_prompt_includes_full_bundle() -> None:
    handoff = _handoff(
        goal_restatement="Finish the auth refactor",
        constraints_active=["no new deps", "keep the public API"],
        decisions_so_far=[
            Decision(summary="use JWT", rationale="stateless, already a dep")
        ],
        open_questions=["should refresh tokens rotate?"],
        expected_output="a PR with tests",
    )
    prompt = compose_agent_prompt(Task(goal="ignored when handoff present"), handoff)
    assert "Finish the auth refactor" in prompt
    assert "no new deps" in prompt
    assert "use JWT" in prompt and "stateless" in prompt
    assert "should refresh tokens rotate?" in prompt
    assert "a PR with tests" in prompt


def test_parse_handoff_directive_variants() -> None:
    for msg in (
        "Done my part.\n@handoff-to: hermes\n@handoff-expected: run the tests",
        "@handoff to=hermes expected= run the tests",
        "some text @handoff_to hermes and then @handoff_expected run the tests",
    ):
        req = parse_handoff_request(msg)
        assert isinstance(req, RequestHandoff)
        assert req.to_agent == "hermes"
        assert "run the tests" in req.expected_output


def test_parse_handoff_none_when_absent_or_unknown() -> None:
    assert parse_handoff_request("just a normal completion message") is None
    assert parse_handoff_request("@handoff-to: nonesuch") is None  # unknown agent
    assert parse_handoff_request("") is None
    assert parse_handoff_request(None) is None


def test_build_response_actions_finishes_by_default() -> None:
    actions = build_response_actions("all done", response_type="codex_response")
    assert isinstance(actions[0], WriteMemory)
    assert actions[0].durable is True
    assert isinstance(actions[1], TaskDone)


def test_build_response_actions_hands_off_when_requested() -> None:
    actions = build_response_actions(
        "did the analysis\n@handoff-to: hermes\n@handoff-expected: write it up",
        response_type="codex_response",
    )
    assert isinstance(actions[0], WriteMemory)
    assert isinstance(actions[1], RequestHandoff)
    assert actions[1].to_agent == "hermes"
    assert actions[1].expected_output == "write it up"
