from __future__ import annotations

from pathlib import Path

import pytest

from exocortex.contracts import Confidence, MemoryRecord, MemoryScope
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.memory.retrieval import HybridRetrieval


def _rec(content: str, *, scope_id: str = "t1") -> MemoryRecord:
    return MemoryRecord(
        type="observation",
        content=content,
        source="codex",
        confidence=Confidence.OBSERVED,
        scope=MemoryScope.TASK,
        scope_id=scope_id,
    )


async def _seed(store: DurableMemoryStore, corpus: list[str]) -> None:
    emb = DeterministicEmbeddingProvider()
    for content in corpus:
        r = _rec(content)
        await store.write(r, embedding=emb.embed(r.content))


@pytest.mark.asyncio
async def test_keyword_only_retrieval(tmp_path: Path) -> None:
    store = DurableMemoryStore(tmp_path / "mem.db")
    await _seed(
        store,
        [
            "auth middleware rewrite is driven by compliance",
            "session tokens must rotate every hour",
            "legal flagged the old token storage format",
            "cats are sometimes graceful",
        ],
    )
    r = HybridRetrieval(store, DeterministicEmbeddingProvider())

    hits = await r.search("auth compliance", alpha=1.0)
    assert hits
    assert any("auth middleware" in h.content for h, _ in hits[:2])


@pytest.mark.asyncio
async def test_rank_merge_returns_valid_scores(tmp_path: Path) -> None:
    store = DurableMemoryStore(tmp_path / "mem.db")
    await _seed(
        store,
        [
            "topic alpha one",
            "topic alpha two",
            "topic beta one",
            "topic beta two",
            "unrelated gamma",
        ],
    )
    r = HybridRetrieval(store, DeterministicEmbeddingProvider())

    hits = await r.search("alpha", alpha=0.5, limit=5)
    assert hits
    scores = [s for _, s in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 <= s <= 1.0 for s in scores)


@pytest.mark.asyncio
async def test_alpha_bounds_enforced(tmp_path: Path) -> None:
    store = DurableMemoryStore(tmp_path / "mem.db")
    r = HybridRetrieval(store, DeterministicEmbeddingProvider())
    with pytest.raises(ValueError):
        await r.search("x", alpha=1.5)


@pytest.mark.asyncio
async def test_respects_scope_filter(tmp_path: Path) -> None:
    store = DurableMemoryStore(tmp_path / "mem.db")
    emb = DeterministicEmbeddingProvider()
    for content, sid in [
        ("auth flow rewrite", "t1"),
        ("auth flow rewrite", "t2"),
        ("unrelated content", "t1"),
    ]:
        rec = _rec(content, scope_id=sid)
        await store.write(rec, embedding=emb.embed(rec.content))

    r = HybridRetrieval(store, emb)
    hits = await r.search(
        "auth", scope=MemoryScope.TASK, scope_id="t1", alpha=1.0, limit=10
    )
    assert hits
    assert all(h.scope_id == "t1" for h, _ in hits)
