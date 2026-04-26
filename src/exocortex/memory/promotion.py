"""Confidence promotion: when N independent agents agree on a fact,
auto-bump its confidence. The simplest mechanism that turns scattered
observations into shared belief.

Rule: any cluster of near-duplicate records (cosine ≥ threshold) whose
content is asserted by ≥`min_agents` distinct sources gets its canonical
record's confidence promoted one level (observed → asserted, inferred →
observed). Promotion is idempotent — already-asserted records stay put.

Read-only at MVP scale (operator runs `precog memory promote` on
demand). A future scheduled job can fire this nightly.
"""

from __future__ import annotations

from dataclasses import dataclass

import anyio

from exocortex.contracts import (
    Confidence,
    Event,
    EventKind,
    MemoryRecord,
    MemoryScope,
)
from exocortex.memory.dedup import find_dedup_clusters
from exocortex.memory.durable import DurableMemoryStore
from exocortex.observability.audit import AuditLog

# Promotion ladder: each step is one bump. Already-strongest stays put.
_PROMOTION_NEXT: dict[Confidence, Confidence] = {
    Confidence.EXTERNAL_CLAIM: Confidence.INFERRED,
    Confidence.INFERRED: Confidence.OBSERVED,
    Confidence.OBSERVED: Confidence.ASSERTED,
    Confidence.ASSERTED: Confidence.ASSERTED,  # ceiling
}


@dataclass(frozen=True)
class Promotion:
    record_id: str
    from_confidence: Confidence
    to_confidence: Confidence
    supporting_sources: tuple[str, ...]
    cluster_size: int


async def find_promotion_candidates(
    store: DurableMemoryStore,
    *,
    scope: MemoryScope | None = None,
    scope_id: str | None = None,
    threshold: float = 0.92,
    min_agents: int = 3,
) -> list[Promotion]:
    """Scan memory for clusters that satisfy the promotion rule. Read-only."""
    if min_agents < 2:
        raise ValueError("min_agents must be >= 2 to mean 'multiple agents'")

    clusters = await find_dedup_clusters(
        store, scope=scope, scope_id=scope_id, threshold=threshold
    )
    promotions: list[Promotion] = []
    for cluster in clusters:
        all_records: list[MemoryRecord] = [cluster.canonical, *cluster.duplicates]
        sources = sorted({r.source for r in all_records})
        if len(sources) < min_agents:
            continue
        next_confidence = _PROMOTION_NEXT.get(
            cluster.canonical.confidence, cluster.canonical.confidence
        )
        if next_confidence == cluster.canonical.confidence:
            continue  # already at ceiling
        promotions.append(
            Promotion(
                record_id=str(cluster.canonical.id),
                from_confidence=cluster.canonical.confidence,
                to_confidence=next_confidence,
                supporting_sources=tuple(sources),
                cluster_size=cluster.size,
            )
        )
    return promotions


async def apply_promotions(
    store: DurableMemoryStore,
    audit: AuditLog,
    promotions: list[Promotion],
) -> int:
    """Apply pending promotions: update confidence on the canonical record
    and emit a `MEMORY_PROMOTED` event per change. Returns count applied.
    """
    applied = 0
    for promo in promotions:
        def _sync(p: Promotion = promo) -> int:
            cur = store._conn.execute(  # noqa: SLF001
                "UPDATE memory_records SET confidence = ? WHERE id = ?",
                (p.to_confidence.value, p.record_id),
            )
            store._conn.commit()  # noqa: SLF001
            return cur.rowcount

        async with store._lock:  # noqa: SLF001
            rowcount = await anyio.to_thread.run_sync(_sync)
        if rowcount:
            applied += 1
            await audit.record(
                Event(
                    kind=EventKind.MEMORY_PROMOTED,
                    agent_id="exocortex",
                    payload={
                        "record_id": promo.record_id,
                        "from": promo.from_confidence.value,
                        "to": promo.to_confidence.value,
                        "supporting_sources": list(promo.supporting_sources),
                        "cluster_size": promo.cluster_size,
                    },
                )
            )
    return applied
