"""Sprint 1: deduplication + right-to-forget."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from exocortex.contracts import (
    Confidence,
    EventKind,
    MemoryRecord,
    MemoryScope,
)
from exocortex.memory.dedup import (
    find_dedup_clusters,
    merge_records,
)
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.memory.retrieval import HybridRetrieval
from exocortex.observability.audit import AuditLog
from exocortex.operator.mcp.handlers import MemoryHandlers


def _rec(
    content: str,
    *,
    source: str = "codex",
    confidence: Confidence = Confidence.OBSERVED,
) -> MemoryRecord:
    return MemoryRecord(
        type="observation",
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
    retrieval = HybridRetrieval(store, embedder)
    audit = AuditLog(tmp_path / "audit.jsonl")
    handlers = MemoryHandlers(
        store=store, embedder=embedder, retrieval=retrieval, audit=audit
    )
    return store, embedder, audit, handlers


# --- Deduplication ----------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_finds_identical_content(stack) -> None:  # type: ignore[no-untyped-def]
    store, embedder, _, _ = stack
    a = _rec("Chose SQLite over Postgres for MVP")
    b = _rec("Chose SQLite over Postgres for MVP")  # exact dup
    c = _rec("Auth tokens rotate every 60 minutes")  # unrelated
    for r in (a, b, c):
        await store.write(r, embedding=embedder.embed(r.content))

    clusters = await find_dedup_clusters(store, threshold=0.92)
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.size == 2
    assert {r.content for r in (cluster.canonical, *cluster.duplicates)} == {
        "Chose SQLite over Postgres for MVP"
    }


@pytest.mark.asyncio
async def test_dedup_canonical_picks_highest_confidence(stack) -> None:  # type: ignore[no-untyped-def]
    store, embedder, _, _ = stack
    weak = _rec("X is true", confidence=Confidence.INFERRED)
    strong = _rec("X is true", confidence=Confidence.OBSERVED)
    for r in (weak, strong):
        await store.write(r, embedding=embedder.embed(r.content))

    clusters = await find_dedup_clusters(store, threshold=0.92)
    assert len(clusters) == 1
    assert clusters[0].canonical.id == strong.id


@pytest.mark.asyncio
async def test_dedup_threshold_too_loose_fails(stack) -> None:  # type: ignore[no-untyped-def]
    store, _, _, _ = stack
    with pytest.raises(ValueError, match="threshold"):
        await find_dedup_clusters(store, threshold=0.3)


@pytest.mark.asyncio
async def test_dedup_with_no_records(stack) -> None:  # type: ignore[no-untyped-def]
    store, _, _, _ = stack
    assert await find_dedup_clusters(store) == []


@pytest.mark.asyncio
async def test_dedup_respects_scope_filter(stack) -> None:  # type: ignore[no-untyped-def]
    store, embedder, _, _ = stack
    a = MemoryRecord(
        type="observation", content="auth flow rewrite",
        source="codex", confidence=Confidence.OBSERVED,
        scope=MemoryScope.TASK, scope_id="t1",
    )
    b = MemoryRecord(
        type="observation", content="auth flow rewrite",
        source="codex", confidence=Confidence.OBSERVED,
        scope=MemoryScope.TASK, scope_id="t2",
    )
    for r in (a, b):
        await store.write(r, embedding=embedder.embed(r.content))

    # Different scope_ids → not in same scope filter, no cluster.
    clusters = await find_dedup_clusters(
        store, scope=MemoryScope.TASK, scope_id="t1"
    )
    assert clusters == []


@pytest.mark.asyncio
async def test_merge_records_removes_duplicates(stack) -> None:  # type: ignore[no-untyped-def]
    store, embedder, _, _ = stack
    keep = _rec("the canonical record")
    drop1 = _rec("the canonical record")
    drop2 = _rec("the canonical record")
    for r in (keep, drop1, drop2):
        await store.write(r, embedding=embedder.embed(r.content))

    removed = await merge_records(
        store, keep_id=str(keep.id), drop_ids=[str(drop1.id), str(drop2.id)]
    )
    assert removed == 2
    assert await store.count() == 1
    assert await store.get(keep.id) is not None


# --- Right-to-forget --------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_forget_deletes_and_audits(stack) -> None:  # type: ignore[no-untyped-def]
    store, embedder, audit, handlers = stack
    rec = _rec("ephemeral fact, please forget")
    await store.write(rec, embedding=embedder.embed(rec.content))

    result = await handlers.memory_forget(record_id=str(rec.id))
    assert result["status"] == "forgotten"
    assert await store.get(rec.id) is None

    events = await audit.read_all()
    forgotten = [e for e in events if e.kind == EventKind.MEMORY_FORGOTTEN]
    assert len(forgotten) == 1
    assert forgotten[0].payload["record_id"] == str(rec.id)
    assert "ephemeral fact" in forgotten[0].payload["content_preview"]


@pytest.mark.asyncio
async def test_memory_forget_unknown_id_returns_not_found(stack) -> None:  # type: ignore[no-untyped-def]
    _, _, audit, handlers = stack
    result = await handlers.memory_forget(record_id=str(uuid.uuid4()))
    assert result["status"] == "not_found"
    # No audit event for not-found.
    events = await audit.read_all()
    assert not any(e.kind == EventKind.MEMORY_FORGOTTEN for e in events)


@pytest.mark.asyncio
async def test_memory_forget_invalid_uuid_raises(stack) -> None:  # type: ignore[no-untyped-def]
    _, _, _, handlers = stack
    with pytest.raises(ValueError, match="invalid UUID"):
        await handlers.memory_forget(record_id="not-a-uuid")


# --- memory_merge handler --------------------------------------------------


@pytest.mark.asyncio
async def test_memory_merge_handler_audits(stack) -> None:  # type: ignore[no-untyped-def]
    store, embedder, audit, handlers = stack
    keep = _rec("canonical text")
    drop = _rec("canonical text")
    for r in (keep, drop):
        await store.write(r, embedding=embedder.embed(r.content))

    result = await handlers.memory_merge(
        keep_id=str(keep.id), drop_ids=[str(drop.id)]
    )
    assert result["removed_count"] == 1
    events = await audit.read_all()
    merged = [e for e in events if e.kind == EventKind.MEMORY_MERGED]
    assert len(merged) == 1
    assert merged[0].payload["keep_id"] == str(keep.id)


@pytest.mark.asyncio
async def test_memory_merge_unknown_keep_raises(stack) -> None:  # type: ignore[no-untyped-def]
    _, _, _, handlers = stack
    with pytest.raises(ValueError, match="not found"):
        await handlers.memory_merge(
            keep_id=str(uuid.uuid4()), drop_ids=[]
        )


# --- Cluster reporting via handler ----------------------------------------


@pytest.mark.asyncio
async def test_memory_dedup_clusters_handler_returns_canonical_first(stack) -> None:  # type: ignore[no-untyped-def]
    store, embedder, _, handlers = stack
    a = _rec("same text")
    b = _rec("same text")
    await store.write(a, embedding=embedder.embed(a.content))
    await store.write(b, embedding=embedder.embed(b.content))

    out = await handlers.memory_dedup_clusters(threshold=0.92)
    assert out["cluster_count"] == 1
    assert out["clusters"][0]["size"] == 2
    canonical = out["clusters"][0]["canonical"]
    duplicates = out["clusters"][0]["duplicates"]
    assert canonical["id"] != duplicates[0]["id"]
