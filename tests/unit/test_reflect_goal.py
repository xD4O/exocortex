from __future__ import annotations

from exocortex.contracts import Confidence, MemoryRecord, MemoryScope
from exocortex.coordination.reflect_goal import build_reflect_goal


def test_goal_mentions_tool_kinds_and_records() -> None:
    rec = MemoryRecord(type="observation", content="chose SQLite over Postgres",
                       source="codex", confidence=Confidence.OBSERVED,
                       scope=MemoryScope.PROJECT, scope_id="exocortex")
    goal = build_reflect_goal("refl-1", [rec], max_insights=20)
    assert "insight_propose" in goal
    for kind in ("contradiction", "pattern", "gap", "synthesis"):
        assert kind in goal
    assert "refl-1" in goal
    assert str(rec.id) in goal            # records are cited so the agent can ground refs
    assert "20" in goal                   # max insights communicated
