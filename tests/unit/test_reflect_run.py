from __future__ import annotations

import re
from pathlib import Path

import pytest

from exocortex.config import Settings
from exocortex.contracts import Confidence, EventKind, MemoryRecord, MemoryScope
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.memory.reflect import ReflectionService, run_reflection
from exocortex.observability.audit import AuditLog
from exocortex.operator.mcp.server import _propose_insight


@pytest.mark.asyncio
async def test_start_and_complete_run(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    svc = ReflectionService(audit=audit)
    rid = await svc.start_run(agent="codex", window_from=None)
    await svc.complete_run(rid, status="completed", count=3)
    kinds = [e.kind for e in await audit.read_all()]
    assert EventKind.REFLECTION_STARTED in kinds
    assert EventKind.REFLECTION_COMPLETED in kinds
    completed = [e for e in await audit.read_all()
                 if e.kind == EventKind.REFLECTION_COMPLETED][0]
    assert completed.payload["reflection_id"] == rid
    assert completed.payload["insight_count"] == 3


@pytest.mark.asyncio
async def test_run_reflection_counts_only_this_run(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    store = DurableMemoryStore(tmp_path / "m.db")
    emb = DeterministicEmbeddingProvider()
    rec = MemoryRecord(type="observation", content="chose SQLite", source="codex",
                       confidence=Confidence.OBSERVED, scope=MemoryScope.PROJECT,
                       scope_id="exocortex")
    await store.write(rec, embedding=emb.embed(rec.content))

    async def fake_dispatch(*, goal, **kwargs):
        rid = re.search(r"Reflection run id: (\S+)", goal).group(1)
        await _propose_insight(audit, kind="synthesis", title="t1", detail="d",
                               refs=[str(rec.id)], reflection_id=rid)
        await _propose_insight(audit, kind="gap", title="t2", detail="d",
                               refs=[str(rec.id)], reflection_id=rid)
        return {"dispatched_to": "codex"}

    settings = Settings(reflect_window_days=7, reflect_max_insights=20)
    out = await run_reflection(audit=audit, store=store, settings=settings,
                               dispatch=fake_dispatch, all_history=True)
    assert out["status"] == "completed"
    assert out["insight_count"] == 2          # only this run's two insights
    completed = [e for e in await audit.read_all()
                 if e.kind == EventKind.REFLECTION_COMPLETED][0]
    assert completed.payload["insight_count"] == 2
