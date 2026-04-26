"""Find near-duplicate memory records via embedding similarity.

A record is "near-duplicate" of another when their embeddings have cosine
similarity ≥ threshold (default 0.92). Records are clustered with a simple
union-find over the similarity edges; each cluster's canonical record is
the highest-confidence one (ties broken by oldest, so its id is stable).

This is a *find*-and-*report* primitive — it does not mutate. Callers
decide whether to merge (via `merge_records`), forget the duplicates, or
leave them be. Operator-in-the-loop is the right default at MVP scale.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import anyio

from exocortex.contracts import Confidence, MemoryRecord, MemoryScope
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import cosine_similarity

# Confidence ordering for canonical-pick (higher = stronger).
_CONFIDENCE_RANK = {
    Confidence.OBSERVED: 4,
    Confidence.ASSERTED: 3,
    Confidence.INFERRED: 2,
    Confidence.EXTERNAL_CLAIM: 1,
}


@dataclass(frozen=True)
class DedupCluster:
    canonical: MemoryRecord
    duplicates: tuple[MemoryRecord, ...]

    @property
    def size(self) -> int:
        return 1 + len(self.duplicates)


async def find_dedup_clusters(
    store: DurableMemoryStore,
    *,
    scope: MemoryScope | None = None,
    scope_id: str | None = None,
    threshold: float = 0.92,
) -> list[DedupCluster]:
    """Scan memory for near-duplicates. Returns clusters of size ≥ 2."""
    if not 0.5 <= threshold <= 1.0:
        raise ValueError(f"threshold must be in [0.5, 1.0], got {threshold}")

    pairs = await store.all_with_embeddings(scope=scope, scope_id=scope_id)
    if len(pairs) < 2:
        return []

    parent = list(range(len(pairs)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # O(n²) over embedded records. At MVP scale this is fine — ~thousands
    # tops. If you grow past that, swap in an ANN index (faiss / sqlite-vec).
    for i in range(len(pairs)):
        for j in range(i + 1, len(pairs)):
            sim = cosine_similarity(pairs[i][1], pairs[j][1])
            if sim >= threshold:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(len(pairs)):
        groups.setdefault(find(i), []).append(i)

    clusters: list[DedupCluster] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        records = [pairs[i][0] for i in members]
        canonical = _pick_canonical(records)
        duplicates = tuple(r for r in records if r.id != canonical.id)
        clusters.append(
            DedupCluster(canonical=canonical, duplicates=duplicates)
        )

    # Largest clusters first — operator usually wants to address the
    # heaviest dedup before the long tail.
    clusters.sort(key=lambda c: -c.size)
    return clusters


def _pick_canonical(records: list[MemoryRecord]) -> MemoryRecord:
    """Highest confidence wins; oldest as tie-breaker (id is stable)."""
    return min(
        records,
        key=lambda r: (
            -_CONFIDENCE_RANK.get(r.confidence, 0),
            r.timestamp,
        ),
    )


async def merge_records(
    store: DurableMemoryStore,
    *,
    keep_id: str,
    drop_ids: list[str],
) -> int:
    """Delete the records in drop_ids; return count actually removed.

    The kept record's content is preserved as-is. Audit logging is the
    caller's responsibility (see CLI / MCP layer).
    """
    keep = await store.get(UUID(keep_id))
    if keep is None:
        raise ValueError(f"keep_id {keep_id} not found")

    removed = 0
    for did in drop_ids:
        if did == keep_id:
            continue
        deleted = await _delete_record(store, did)
        if deleted:
            removed += 1
    return removed


async def _delete_record(store: DurableMemoryStore, record_id: str) -> bool:
    """Hard-delete one record. Implementation note: DurableMemoryStore
    didn't ship with a delete method, so we go through the connection
    directly. Kept tight to avoid leaking SQLite into the rest of the
    codebase."""

    def _sync() -> int:
        cur = store._conn.execute(  # noqa: SLF001
            "DELETE FROM memory_records WHERE id = ?", (record_id,)
        )
        store._conn.commit()  # noqa: SLF001
        return cur.rowcount

    async with store._lock:  # noqa: SLF001
        return bool(await anyio.to_thread.run_sync(_sync))
