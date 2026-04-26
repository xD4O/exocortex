from __future__ import annotations

from uuid import uuid4

from exocortex.contracts import (
    Budget,
    Confidence,
    Handoff,
    MemoryRecord,
    MemoryScope,
    Task,
    ToolInvocationCursor,
)
from exocortex.memory.summarizer import TruncatingSummarizer, build_handoff


def _session_rec(content: str, *, session_id: str = "s1") -> MemoryRecord:
    return MemoryRecord(
        type="observation",
        content=content,
        source="codex",
        confidence=Confidence.OBSERVED,
        scope=MemoryScope.SESSION,
        scope_id=session_id,
    )


def test_truncating_summarizer_respects_budget() -> None:
    s = TruncatingSummarizer()
    records = [_session_rec("x" * 500) for _ in range(10)]
    out = s.summarize(records, max_chars=200)
    assert len(out) <= 200


def test_truncating_summarizer_preserves_ordering() -> None:
    s = TruncatingSummarizer()
    records = [_session_rec(f"note {i}") for i in range(3)]
    out = s.summarize(records, max_chars=10_000)
    assert out.index("note 0") < out.index("note 1") < out.index("note 2")


def test_summarizer_empty_when_zero_budget() -> None:
    assert TruncatingSummarizer().summarize([_session_rec("x")], max_chars=0) == ""


def test_build_handoff_produces_bundle_under_budget() -> None:
    task = Task(goal="refactor memory", constraints=["no breaking schema"], budget=Budget())
    records = [_session_rec(f"fact {i}") for i in range(20)]

    handoff, digest = build_handoff(
        task=task,
        from_agent="codex",
        to_agent="claude_code",
        sequence_no=1,
        session_records=records,
        decisions=[],
        open_questions=["what's the TTL policy?"],
        workspace=None,
        cursor=ToolInvocationCursor(),
        memory_scope_ids=[f"task:{task.id}"],
        expected_output="passing tests",
        budget_remaining=Budget(tokens_limit=50_000),
        summarizer=TruncatingSummarizer(),
        digest_char_budget=300,
    )

    assert len(digest) <= 300
    assert handoff.goal_restatement.startswith("refactor memory")
    assert digest in handoff.goal_restatement
    assert handoff.constraints_active == ["no breaking schema"]
    assert handoff.open_questions == ["what's the TTL policy?"]
    assert handoff.from_agent == "codex"
    assert handoff.to_agent == "claude_code"


def test_build_handoff_roundtrips_through_json() -> None:
    task = Task(goal="x", budget=Budget())
    handoff, _ = build_handoff(
        task=task,
        from_agent="a",
        to_agent="b",
        sequence_no=0,
        session_records=[_session_rec("one"), _session_rec("two")],
        decisions=[],
        open_questions=[],
        workspace=None,
        cursor=ToolInvocationCursor(pending_ids=[uuid4()]),
        memory_scope_ids=[f"task:{task.id}"],
        expected_output="done",
        budget_remaining=Budget(),
        summarizer=TruncatingSummarizer(),
    )
    restored = Handoff.model_validate_json(handoff.model_dump_json())
    assert restored == handoff
