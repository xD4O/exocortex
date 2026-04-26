from __future__ import annotations

from uuid import UUID

from exocortex.contracts import MemoryRecord, MemoryScope
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import EmbeddingProvider, cosine_similarity


def _rank_scores(ordered_ids: list[UUID]) -> dict[UUID, float]:
    n = len(ordered_ids)
    if n == 0:
        return {}
    return {rid: 1.0 - (i / n) for i, rid in enumerate(ordered_ids)}


class HybridRetrieval:
    """Keyword (FTS5) + semantic (cosine) retrieval with weighted rank merge.

    alpha=1.0 is pure keyword, alpha=0.0 is pure semantic, 0.5 is balanced.
    Rank-based normalization avoids BM25 / cosine unit-mismatch.
    """

    def __init__(
        self, store: DurableMemoryStore, embedder: EmbeddingProvider
    ) -> None:
        self._store = store
        self._embedder = embedder

    async def search(
        self,
        query: str,
        *,
        scope: MemoryScope | None = None,
        scope_id: str | None = None,
        limit: int = 10,
        alpha: float = 0.5,
    ) -> list[tuple[MemoryRecord, float]]:
        if not 0.0 <= alpha <= 1.0:
            raise ValueError(f"alpha must be in [0, 1], got {alpha}")

        fts_pool = limit * 4
        fts_hits = await self._store.search_fts(
            query, scope=scope, scope_id=scope_id, limit=fts_pool
        )
        candidates = await self._store.all_with_embeddings(
            scope=scope, scope_id=scope_id
        )

        q_vec = self._embedder.embed(query)
        semantic_scored = sorted(
            ((r, cosine_similarity(q_vec, v)) for r, v in candidates),
            key=lambda rs: -rs[1],
        )

        fts_ranks = _rank_scores([r.id for r in fts_hits])
        sem_ranks = _rank_scores([r.id for r, _ in semantic_scored[: fts_pool]])

        records_by_id: dict[UUID, MemoryRecord] = {r.id: r for r in fts_hits}
        for r, _ in semantic_scored:
            records_by_id.setdefault(r.id, r)

        merged: list[tuple[MemoryRecord, float]] = []
        for rid, record in records_by_id.items():
            score = alpha * fts_ranks.get(rid, 0.0) + (1 - alpha) * sem_ranks.get(
                rid, 0.0
            )
            merged.append((record, score))

        merged.sort(key=lambda rs: -rs[1])
        return merged[:limit]
