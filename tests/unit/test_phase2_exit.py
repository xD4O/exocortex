"""Phase 2 exit criterion (CLAUDE-PLAN.MD §6):

  "Write 100 records across sessions, retrieve by hybrid score, produce a
   handoff digest under token budget, hand off between two mocks with zero
   field-loss."
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exocortex.contracts import (
    Budget,
    Confidence,
    Handoff,
    MemoryRecord,
    MemoryScope,
    Task,
    ToolInvocationCursor,
)
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.memory.retrieval import HybridRetrieval
from exocortex.memory.summarizer import TruncatingSummarizer, build_handoff


@pytest.mark.asyncio
async def test_phase2_exit_criterion(tmp_path: Path) -> None:
    store = DurableMemoryStore(tmp_path / "mem.db")
    embedder = DeterministicEmbeddingProvider()

    # --- 1. Write 100 records across 10 sessions ---
    for i in range(100):
        scope_id = f"session-{i % 10}"
        record = MemoryRecord(
            type="observation",
            content=f"Observation {i} about topic {i % 5}: the quick brown fox",
            source="codex" if i % 2 == 0 else "claude_code",
            confidence=Confidence.OBSERVED,
            scope=MemoryScope.TASK,
            scope_id=scope_id,
        )
        await store.write(record, embedding=embedder.embed(record.content))

    assert await store.count() == 100

    # --- 2. Retrieve by hybrid score ---
    retrieval = HybridRetrieval(store, embedder)
    hits = await retrieval.search("topic 3 brown", alpha=0.5, limit=10)
    assert 1 <= len(hits) <= 10
    scores = [s for _, s in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 <= s <= 1.0 for s in scores)
    # At least one top hit should mention the queried topic.
    assert any("topic 3" in r.content for r, _ in hits[:5])

    # --- 3. Produce a handoff digest under token budget ---
    task = Task(goal="Continue memory work", budget=Budget(tokens_limit=40_000))
    session_records = [
        MemoryRecord(
            type="note",
            content=f"Session scratch {i}: something somewhat verbose to compress " * 3,
            source="codex",
            confidence=Confidence.ASSERTED,
            scope=MemoryScope.SESSION,
            scope_id="session-current",
        )
        for i in range(30)
    ]
    char_budget = 800
    handoff, digest = build_handoff(
        task=task,
        from_agent="codex",
        to_agent="claude_code",
        sequence_no=1,
        session_records=session_records,
        decisions=[],
        open_questions=["Is TTL-based pruning enough for project-scope records?"],
        workspace=None,
        cursor=ToolInvocationCursor(),
        memory_scope_ids=[f"task:{task.id}"],
        expected_output="All memory tests green",
        budget_remaining=Budget(tokens_limit=30_000),
        summarizer=TruncatingSummarizer(),
        digest_char_budget=char_budget,
    )
    assert len(digest) <= char_budget, "digest exceeded char budget"
    # Rough token proxy: 1 token ≈ 4 chars; budget was 40k tokens → 160k chars ceiling.
    assert len(digest) * 1 < task.budget.tokens_limit * 4  # type: ignore[operator]

    # --- 4. Handoff round-trips with zero field-loss ---
    payload = handoff.model_dump_json()
    restored = Handoff.model_validate_json(payload)
    assert restored == handoff
    assert restored.model_dump_json() == payload
