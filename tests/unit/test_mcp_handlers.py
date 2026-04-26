"""Unit tests for the MCP handler layer. These cover the actual semantics —
the FastMCP wiring in server.py is a thin re-export layer tested by
`test_mcp_server_smoke`."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from exocortex.contracts import (
    Confidence,
    Event,
    EventKind,
    MemoryRecord,
    MemoryScope,
)
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.memory.retrieval import HybridRetrieval
from exocortex.observability.audit import AuditLog
from exocortex.operator.mcp.handlers import MemoryHandlers


def _build(tmp_path: Path) -> tuple[MemoryHandlers, DurableMemoryStore, AuditLog]:
    store = DurableMemoryStore(tmp_path / "mem.db")
    embedder = DeterministicEmbeddingProvider()
    retrieval = HybridRetrieval(store, embedder)
    audit = AuditLog(tmp_path / "audit.jsonl")
    h = MemoryHandlers(
        store=store, embedder=embedder, retrieval=retrieval, audit=audit
    )
    return h, store, audit


# --- Writes -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_write_persists_and_audits(tmp_path: Path) -> None:
    h, store, audit = _build(tmp_path)
    out = await h.memory_write(
        content="SQLite chosen over Postgres for MVP",
        source="operator",
        scope="project",
        scope_id="exocortex",
        type="decision",
        confidence="asserted",
        tags=["storage", "mvp"],
    )
    assert "id" in out
    # Record is durably stored.
    records = await store.list_by_scope(MemoryScope.PROJECT, "exocortex")
    assert len(records) == 1
    assert records[0].content.startswith("SQLite chosen")
    assert records[0].source == "operator"
    assert records[0].confidence == Confidence.ASSERTED
    assert records[0].tags == ["storage", "mvp"]
    # Audit log captured a `memory.written` event marked as via=mcp.
    events = await audit.read_all()
    written = [e for e in events if e.kind == EventKind.MEMORY_WRITTEN]
    assert len(written) == 1
    assert written[0].payload["via"] == "mcp"
    assert written[0].payload["durable"] is True


@pytest.mark.asyncio
async def test_memory_write_rejects_bad_scope(tmp_path: Path) -> None:
    h, _, _ = _build(tmp_path)
    with pytest.raises(ValueError, match="invalid scope"):
        await h.memory_write(content="x", scope="nonsense")


@pytest.mark.asyncio
async def test_memory_write_rejects_bad_confidence(tmp_path: Path) -> None:
    h, _, _ = _build(tmp_path)
    with pytest.raises(ValueError, match="invalid confidence"):
        await h.memory_write(content="x", confidence="wobbly")


# --- Search + list + get ----------------------------------------------------


@pytest.mark.asyncio
async def test_memory_search_hybrid(tmp_path: Path) -> None:
    h, _, _ = _build(tmp_path)
    for c in (
        "auth middleware rewrite",
        "memory summarizer compression",
        "cat facts",
    ):
        await h.memory_write(content=c, source="codex", scope="project", scope_id="precog")
    result = await h.memory_search(query="auth", alpha=1.0)
    assert result["count"] >= 1
    assert any("auth middleware" in r["content"] for r in result["results"])


@pytest.mark.asyncio
async def test_memory_search_scope_filter(tmp_path: Path) -> None:
    """HybridRetrieval returns in-scope records ranked by relevance; the
    scope filter hard-excludes out-of-scope records."""
    h, _, _ = _build(tmp_path)
    await h.memory_write(content="shared goal", scope="project", scope_id="p1")
    await h.memory_write(content="task observation", scope="task", scope_id="t1")
    result = await h.memory_search(
        query="shared", scope="task", scope_id="t1", alpha=1.0
    )
    project_records = await h.memory_list(scope="project", scope_id="p1")
    project_ids = {r["id"] for r in project_records["records"]}
    returned_ids = {r["id"] for r in result["results"]}
    assert returned_ids.isdisjoint(project_ids), (
        "project-scope record leaked into task-scope search"
    )
    # And no task-scope record actually contains the word "shared".
    for r in result["results"]:
        assert "shared" not in r["content"]


@pytest.mark.asyncio
async def test_memory_list_by_scope(tmp_path: Path) -> None:
    h, _, _ = _build(tmp_path)
    for i in range(3):
        await h.memory_write(
            content=f"note {i}", scope="task", scope_id="alpha"
        )
    out = await h.memory_list(scope="task", scope_id="alpha", limit=10)
    assert out["count"] == 3
    assert [r["content"] for r in out["records"]] == ["note 0", "note 1", "note 2"]


@pytest.mark.asyncio
async def test_memory_get_returns_record(tmp_path: Path) -> None:
    h, _, _ = _build(tmp_path)
    out = await h.memory_write(content="x", scope="global", scope_id="global")
    got = await h.memory_get(record_id=out["id"])
    assert got is not None
    assert got["id"] == out["id"]
    assert got["content"] == "x"


@pytest.mark.asyncio
async def test_memory_get_invalid_uuid_raises(tmp_path: Path) -> None:
    h, _, _ = _build(tmp_path)
    with pytest.raises(ValueError, match="invalid UUID"):
        await h.memory_get(record_id="not-a-uuid")


@pytest.mark.asyncio
async def test_memory_get_missing_returns_none(tmp_path: Path) -> None:
    h, _, _ = _build(tmp_path)
    got = await h.memory_get(record_id=str(uuid.uuid4()))
    assert got is None


# --- trace_recent -----------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_recent_returns_events(tmp_path: Path) -> None:
    h, _, audit = _build(tmp_path)
    task_id = uuid.uuid4()
    for kind in (EventKind.TASK_CREATED, EventKind.TOOL_PROPOSED, EventKind.TOOL_EXECUTED):
        await audit.record(Event(kind=kind, task_id=task_id, agent_id="codex"))
    out = await h.trace_recent()
    assert out["count"] >= 3
    kinds = [e["kind"] for e in out["events"]]
    assert "task.created" in kinds


@pytest.mark.asyncio
async def test_trace_recent_filters_by_task_prefix(tmp_path: Path) -> None:
    h, _, audit = _build(tmp_path)
    a = uuid.uuid4()
    b = uuid.uuid4()
    await audit.record(Event(kind=EventKind.TASK_CREATED, task_id=a))
    await audit.record(Event(kind=EventKind.TASK_CREATED, task_id=b))
    out = await h.trace_recent(task_id=str(a)[:8])
    assert all(e["task_id"] == str(a) for e in out["events"])


# --- agents_list ------------------------------------------------------------


@pytest.mark.asyncio
async def test_agents_list_enumerates_bridges(tmp_path: Path) -> None:
    h, _, _ = _build(tmp_path)
    out = await h.agents_list()
    assert out["count"] == 3
    agent_ids = {a["agent_id"] for a in out["agents"]}
    assert agent_ids == {"codex", "claude_code", "hermes"}
    for a in out["agents"]:
        assert a["kind"] == "bridge"
        assert "mcp_client" in a["capabilities"]


# --- Round-trip UI-visibility check ----------------------------------------


@pytest.mark.asyncio
async def test_write_is_visible_via_search(tmp_path: Path) -> None:
    """The full loop an agent cares about: write then recall."""
    h, _, _ = _build(tmp_path)
    await h.memory_write(
        content="the user prefers SQLite for local tools",
        source="hermes",
        scope="global",
        scope_id="global",
        confidence="asserted",
    )
    # Fresh "agent" comes online, queries.
    result = await h.memory_search(query="sqlite", alpha=1.0)
    assert result["count"] >= 1
    assert "SQLite" in result["results"][0]["content"]


# --- Seed records ensure the DurableMemoryStore contract is preserved ------


@pytest.mark.asyncio
async def test_written_record_has_embedding(tmp_path: Path) -> None:
    h, store, _ = _build(tmp_path)
    await h.memory_write(content="embedded content", scope="project", scope_id="p")
    pairs = await store.all_with_embeddings()
    assert len(pairs) == 1
    rec, vec = pairs[0]
    assert isinstance(rec, MemoryRecord)
    assert len(vec) == DeterministicEmbeddingProvider().dim
