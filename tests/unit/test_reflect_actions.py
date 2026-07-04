from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from exocortex.contracts import Confidence, Event, EventKind, MemoryRecord, MemoryScope
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.memory.reflect import ReflectionService
from exocortex.observability.audit import AuditLog


async def _seed_proposed(audit, iid, action):
    await audit.record(Event(kind=EventKind.INSIGHT_PROPOSED, payload={
        "insight_id": iid, "kind": "contradiction", "title": "t", "detail": "d",
        "refs": [str(uuid.uuid4())], "suggested_action": action}))


@pytest.mark.asyncio
async def test_accept_without_apply_is_inert(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    svc = ReflectionService(audit=audit)
    await _seed_proposed(audit, "i1", {"type": "none"})
    out = await svc.accept("i1", apply=False)
    assert out["status"] == "accepted" and out["applied"] is False
    kinds = [e.kind for e in await audit.read_all()]
    assert EventKind.INSIGHT_ACCEPTED in kinds
    # no memory record written
    assert await DurableMemoryStore(tmp_path / "m.db").count() == 0


@pytest.mark.asyncio
async def test_apply_contradiction_supersedes_never_deletes(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    store = DurableMemoryStore(tmp_path / "m.db")
    emb = DeterministicEmbeddingProvider()
    stale = MemoryRecord(type="observation", content="Vietnam", source="codex",
                         confidence=Confidence.OBSERVED, scope=MemoryScope.PROJECT,
                         scope_id="exocortex")
    await store.write(stale, embedding=emb.embed(stale.content))
    svc = ReflectionService(audit=audit)
    await _seed_proposed(audit, "i2",
                         {"type": "supersede", "stale_record_id": str(stale.id)})
    out = await svc.accept("i2", apply=True, store=store, embedder=emb)
    assert out["applied"] is True
    assert await store.get(stale.id) is not None       # original NOT deleted
    assert await store.count() == 2                     # a superseding record added


@pytest.mark.asyncio
async def test_dismiss(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    svc = ReflectionService(audit=audit)
    await _seed_proposed(audit, "i3", {"type": "none"})
    await svc.dismiss("i3", note="not useful")
    assert [i["insight_id"] for i in await svc.list_insights()] == []
