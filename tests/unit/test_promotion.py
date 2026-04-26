"""Confidence promotion: when N agents agree, bump the canonical record."""

from __future__ import annotations

from pathlib import Path

import pytest

from exocortex.contracts import (
    Confidence,
    EventKind,
    MemoryRecord,
    MemoryScope,
)
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.memory.promotion import (
    apply_promotions,
    find_promotion_candidates,
)
from exocortex.observability.audit import AuditLog


def _rec(
    content: str,
    *,
    source: str,
    confidence: Confidence = Confidence.OBSERVED,
    type: str = "decision",
) -> MemoryRecord:
    return MemoryRecord(
        type=type,
        content=content,
        source=source,
        confidence=confidence,
        scope=MemoryScope.PROJECT,
        scope_id="exocortex",
    )


@pytest.fixture
async def stack(tmp_path: Path):
    store = DurableMemoryStore(tmp_path / "mem.db")
    embedder = DeterministicEmbeddingProvider()
    audit = AuditLog(tmp_path / "audit.jsonl")
    return store, embedder, audit


# --- find_promotion_candidates ---------------------------------------------


@pytest.mark.asyncio
async def test_three_distinct_sources_promote_observed_to_asserted(stack) -> None:  # type: ignore[no-untyped-def]
    store, embedder, _ = stack
    text = "the gateway times out at 5 seconds under load"
    for source in ("codex", "hermes", "claude"):
        r = _rec(text, source=source)
        await store.write(r, embedding=embedder.embed(r.content))

    promotions = await find_promotion_candidates(store, min_agents=3)
    assert len(promotions) == 1
    p = promotions[0]
    assert p.from_confidence == Confidence.OBSERVED
    assert p.to_confidence == Confidence.ASSERTED
    assert p.cluster_size == 3
    assert set(p.supporting_sources) == {"codex", "hermes", "claude"}


@pytest.mark.asyncio
async def test_only_two_sources_does_not_promote_when_min_three(stack) -> None:  # type: ignore[no-untyped-def]
    store, embedder, _ = stack
    text = "lock contention on writes when k=8"
    for source in ("codex", "hermes"):
        r = _rec(text, source=source)
        await store.write(r, embedding=embedder.embed(r.content))

    promotions = await find_promotion_candidates(store, min_agents=3)
    assert promotions == []


@pytest.mark.asyncio
async def test_same_source_repeated_does_not_count_as_multiple_agents(stack) -> None:  # type: ignore[no-untyped-def]
    store, embedder, _ = stack
    text = "use FTS5 for keyword search"
    for _ in range(5):
        r = _rec(text, source="codex")
        await store.write(r, embedding=embedder.embed(r.content))

    promotions = await find_promotion_candidates(store, min_agents=2)
    assert promotions == []


@pytest.mark.asyncio
async def test_already_asserted_record_is_not_promoted(stack) -> None:  # type: ignore[no-untyped-def]
    store, embedder, _ = stack
    text = "audit log is append-only JSONL"
    for source in ("codex", "hermes", "claude"):
        r = _rec(text, source=source, confidence=Confidence.ASSERTED)
        await store.write(r, embedding=embedder.embed(r.content))

    promotions = await find_promotion_candidates(store, min_agents=3)
    assert promotions == []


@pytest.mark.asyncio
async def test_inferred_promotes_one_step_to_observed(stack) -> None:  # type: ignore[no-untyped-def]
    store, embedder, _ = stack
    text = "embedder is deterministic 16-dim"
    for source in ("codex", "hermes", "claude"):
        r = _rec(text, source=source, confidence=Confidence.INFERRED)
        await store.write(r, embedding=embedder.embed(r.content))

    promotions = await find_promotion_candidates(store, min_agents=3)
    assert len(promotions) == 1
    assert promotions[0].from_confidence == Confidence.INFERRED
    assert promotions[0].to_confidence == Confidence.OBSERVED


@pytest.mark.asyncio
async def test_min_agents_below_two_raises(stack) -> None:  # type: ignore[no-untyped-def]
    store, _, _ = stack
    with pytest.raises(ValueError, match="multiple agents"):
        await find_promotion_candidates(store, min_agents=1)


# --- apply_promotions ------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_promotions_updates_db_and_emits_audit(stack) -> None:  # type: ignore[no-untyped-def]
    store, embedder, audit = stack
    text = "agents must respect approval gates for write_file"
    records = []
    for source in ("codex", "hermes", "claude"):
        r = _rec(text, source=source)
        await store.write(r, embedding=embedder.embed(r.content))
        records.append(r)

    promotions = await find_promotion_candidates(store, min_agents=3)
    applied = await apply_promotions(store, audit, promotions)
    assert applied == 1

    # Confidence on canonical was bumped; check the surviving record.
    canonical_id = promotions[0].record_id
    bumped = await store.get(canonical_id)
    assert bumped is not None
    assert bumped.confidence == Confidence.ASSERTED

    # Audit emitted exactly one MEMORY_PROMOTED event.
    events = await audit.read_all()
    promoted = [e for e in events if e.kind == EventKind.MEMORY_PROMOTED]
    assert len(promoted) == 1
    payload = promoted[0].payload
    assert payload["record_id"] == canonical_id
    assert payload["from"] == Confidence.OBSERVED.value
    assert payload["to"] == Confidence.ASSERTED.value
    assert set(payload["supporting_sources"]) == {"codex", "hermes", "claude"}
    assert payload["cluster_size"] == 3


@pytest.mark.asyncio
async def test_apply_promotions_idempotent_on_empty_list(stack) -> None:  # type: ignore[no-untyped-def]
    store, _, audit = stack
    applied = await apply_promotions(store, audit, [])
    assert applied == 0
    events = await audit.read_all()
    assert all(e.kind != EventKind.MEMORY_PROMOTED for e in events)
