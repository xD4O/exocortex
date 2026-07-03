from __future__ import annotations

from pathlib import Path

import pytest

from exocortex.contracts import Confidence, MemoryRecord, MemoryScope
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider


def _rec(
    content: str,
    *,
    source: str = "codex",
    scope: MemoryScope = MemoryScope.TASK,
    scope_id: str = "t1",
    rtype: str = "observation",
    tags: list[str] | None = None,
) -> MemoryRecord:
    return MemoryRecord(
        type=rtype,
        content=content,
        source=source,
        confidence=Confidence.OBSERVED,
        scope=scope,
        scope_id=scope_id,
        tags=tags or [],
    )


@pytest.mark.asyncio
async def test_write_and_get(tmp_path: Path) -> None:
    store = DurableMemoryStore(tmp_path / "mem.db")
    r = _rec("Agent chose SQLite over Postgres for MVP.")
    await store.write(r)
    restored = await store.get(r.id)
    assert restored is not None
    assert restored == r


@pytest.mark.asyncio
async def test_fts_finds_keyword_matches(tmp_path: Path) -> None:
    store = DurableMemoryStore(tmp_path / "mem.db")
    await store.write(_rec("The quick brown fox jumps over the lazy dog"))
    await store.write(_rec("Cats sit on mats and watch the world"))
    await store.write(_rec("Foxes are clever animals"))

    hits = await store.search_fts("fox")
    contents = [h.content for h in hits]
    assert any("brown fox" in c for c in contents)
    assert any("Foxes are clever" in c for c in contents)
    assert not any("mats" in c for c in contents)


@pytest.mark.asyncio
async def test_fts_respects_scope(tmp_path: Path) -> None:
    store = DurableMemoryStore(tmp_path / "mem.db")
    await store.write(_rec("shared goal statement", scope=MemoryScope.PROJECT, scope_id="p1"))
    await store.write(_rec("shared observation", scope=MemoryScope.TASK, scope_id="t1"))

    hits = await store.search_fts("shared", scope=MemoryScope.TASK)
    assert len(hits) == 1
    assert hits[0].scope == MemoryScope.TASK


@pytest.mark.asyncio
async def test_embedding_storage_roundtrip(tmp_path: Path) -> None:
    store = DurableMemoryStore(tmp_path / "mem.db")
    embedder = DeterministicEmbeddingProvider()

    contents = ["alpha gamma", "beta gamma", "nothing related"]
    for c in contents:
        await store.write(_rec(c), embedding=embedder.embed(c))

    restored = await store.all_with_embeddings()
    assert len(restored) == 3
    for _, v in restored:
        assert len(v) == embedder.dim


@pytest.mark.asyncio
async def test_scale_100_records(tmp_path: Path) -> None:
    """Phase 2 exit criterion: write 100 records across sessions."""
    store = DurableMemoryStore(tmp_path / "mem.db")
    embedder = DeterministicEmbeddingProvider()
    for i in range(100):
        scope_id = f"session-{i % 10}"
        r = _rec(
            f"Record number {i} about topic {i % 5}",
            scope=MemoryScope.TASK,
            scope_id=scope_id,
        )
        await store.write(r, embedding=embedder.embed(r.content))
    assert await store.count() == 100


@pytest.mark.asyncio
async def test_fts_malformed_query_does_not_crash(tmp_path: Path) -> None:
    """A1/A8: FTS5 operator syntax in a raw user query must not raise
    sqlite3.OperationalError (which surfaces as an unhandled 500 / cheap DoS).
    It should degrade to a literal-phrase search or an empty result."""
    store = DurableMemoryStore(tmp_path / "mem.db")
    await store.write(_rec("we chose SQLite over Postgres"))

    for bad in ['"', "NEAR(", "col:", "AND OR", "foo)(bar", "*", '"unterminated']:
        hits = await store.search_fts(bad)  # must not raise
        assert isinstance(hits, list)


@pytest.mark.asyncio
async def test_fts_punctuation_query_matches_as_phrase(tmp_path: Path) -> None:
    """A punctuation-containing query still finds a literal match rather than
    erroring, thanks to the phrase-quote fallback."""
    store = DurableMemoryStore(tmp_path / "mem.db")
    await store.write(_rec("the endpoint is /api/events for the socket"))
    hits = await store.search_fts("/api/events")
    assert any("/api/events" in h.content for h in hits)
