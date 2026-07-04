from __future__ import annotations

import uuid
from pathlib import Path

import pytest

# Build handlers the same way tests/unit/test_mcp_handlers.py does; assume a
# fixture `handlers` exists there. Minimal standalone version:
from exocortex.config import Settings
from exocortex.contracts import Event, EventKind
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.memory.retrieval import HybridRetrieval
from exocortex.observability.audit import AuditLog
from exocortex.operator.mcp.handlers import MemoryHandlers


def _handlers(tmp_path: Path, *, reflect_enabled: bool = True) -> MemoryHandlers:
    s = Settings(data_dir=tmp_path, audit_log_path=tmp_path / "a.jsonl",
                 memory_db_path=tmp_path / "m.db", reflect_enabled=reflect_enabled)
    store = DurableMemoryStore(s.memory_db_path)
    emb = DeterministicEmbeddingProvider()
    return MemoryHandlers(store=store, embedder=emb,
                          retrieval=HybridRetrieval(store, emb),
                          audit=AuditLog(s.audit_log_path), settings=s)


@pytest.mark.asyncio
async def test_session_startup_includes_pending_insights(tmp_path: Path) -> None:
    h = _handlers(tmp_path, reflect_enabled=True)
    await h.audit.record(Event(kind=EventKind.INSIGHT_PROPOSED, payload={
        "insight_id": str(uuid.uuid4()), "kind": "gap", "title": "unanswered X",
        "detail": "d", "refs": [str(uuid.uuid4())]}))
    result = await h.session_startup(agent_id="codex")
    assert result["pending_insights"]["count"] == 1
    assert result["pending_insights"]["top"][0]["title"] == "unanswered X"


@pytest.mark.asyncio
async def test_session_startup_pending_insights_gated_when_disabled(
    tmp_path: Path,
) -> None:
    h = _handlers(tmp_path, reflect_enabled=False)
    await h.audit.record(Event(kind=EventKind.INSIGHT_PROPOSED, payload={
        "insight_id": str(uuid.uuid4()), "kind": "gap", "title": "unanswered X",
        "detail": "d", "refs": [str(uuid.uuid4())]}))
    result = await h.session_startup(agent_id="codex")
    assert result["pending_insights"] == {"count": 0, "top": []}
