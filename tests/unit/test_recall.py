"""RecallService — reconstructs unfinished-work summaries for fresh sessions."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from exocortex.contracts import (
    Confidence,
    Event,
    EventKind,
    MemoryRecord,
    MemoryScope,
)
from exocortex.contracts.common import now
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.observability.audit import AuditLog
from exocortex.operator.recall import RecallService


def _build(tmp_path: Path) -> tuple[RecallService, DurableMemoryStore, AuditLog]:
    store = DurableMemoryStore(tmp_path / "mem.db")
    audit = AuditLog(tmp_path / "audit.jsonl")
    return RecallService(store=store, audit=audit), store, audit


@pytest.mark.asyncio
async def test_empty_stores_return_fresh_message(tmp_path: Path) -> None:
    svc, _, _ = _build(tmp_path)
    s = await svc.summarize(agent_id="claude_code")
    assert s.unfinished_tasks == []
    assert s.recent_decisions == []
    assert s.total_memory_records == 0
    assert s.total_events == 0
    assert "no prior memory" in s.text_for_user.lower()
    assert any("Start a new task" in p for p in s.suggested_prompts)


@pytest.mark.asyncio
async def test_unfinished_task_surfaces(tmp_path: Path) -> None:
    svc, _, audit = _build(tmp_path)
    tid = uuid4()
    await audit.record(
        Event(
            kind=EventKind.TASK_CREATED,
            task_id=tid,
            payload={"goal": "Refactor the auth middleware"},
        )
    )
    await audit.record(
        Event(
            kind=EventKind.TASK_STATUS_CHANGED,
            task_id=tid,
            agent_id="codex",
            payload={"from": "proposed", "to": "in_progress"},
        )
    )

    s = await svc.summarize(agent_id="claude_code")
    assert len(s.unfinished_tasks) == 1
    t = s.unfinished_tasks[0]
    assert t.task_id == str(tid)
    assert t.goal == "Refactor the auth middleware"
    assert t.status == "in_progress"
    assert "codex" in t.agents
    assert "Refactor the auth middleware" in s.text_for_user
    assert any("Continue: Refactor" in p for p in s.suggested_prompts)


@pytest.mark.asyncio
async def test_completed_task_is_excluded(tmp_path: Path) -> None:
    svc, _, audit = _build(tmp_path)
    tid = uuid4()
    await audit.record(
        Event(kind=EventKind.TASK_CREATED, task_id=tid, payload={"goal": "done"})
    )
    await audit.record(Event(kind=EventKind.TASK_COMPLETED, task_id=tid))

    s = await svc.summarize()
    assert s.unfinished_tasks == []


@pytest.mark.asyncio
async def test_failed_task_is_excluded(tmp_path: Path) -> None:
    svc, _, audit = _build(tmp_path)
    tid = uuid4()
    await audit.record(
        Event(kind=EventKind.TASK_CREATED, task_id=tid, payload={"goal": "x"})
    )
    await audit.record(Event(kind=EventKind.TASK_FAILED, task_id=tid))

    s = await svc.summarize()
    assert s.unfinished_tasks == []


@pytest.mark.asyncio
async def test_recent_decisions_surfaced(tmp_path: Path) -> None:
    svc, store, _ = _build(tmp_path)
    rec = MemoryRecord(
        type="decision",
        content="Chose SQLite over Postgres",
        source="operator",
        confidence=Confidence.ASSERTED,
        scope=MemoryScope.PROJECT,
        scope_id="exocortex",
    )
    await store.write(
        rec, embedding=DeterministicEmbeddingProvider().embed(rec.content)
    )
    s = await svc.summarize()
    assert len(s.recent_decisions) == 1
    assert "SQLite" in s.recent_decisions[0]["content"]
    assert "SQLite" in s.text_for_user


@pytest.mark.asyncio
async def test_old_decisions_excluded_by_window(tmp_path: Path) -> None:
    svc, store, _ = _build(tmp_path)
    rec = MemoryRecord(
        type="decision",
        content="ancient decision",
        source="operator",
        confidence=Confidence.ASSERTED,
        scope=MemoryScope.PROJECT,
        scope_id="exocortex",
    )
    rec.timestamp = now() - timedelta(days=30)
    await store.write(
        rec, embedding=DeterministicEmbeddingProvider().embed(rec.content)
    )
    s = await svc.summarize(decision_window_days=7)
    assert s.recent_decisions == []


@pytest.mark.asyncio
async def test_agents_last_seen_tracks_mix(tmp_path: Path) -> None:
    svc, _, audit = _build(tmp_path)
    tid = uuid4()
    await audit.record(
        Event(kind=EventKind.TASK_CREATED, task_id=tid, payload={"goal": "x"})
    )
    await audit.record(
        Event(kind=EventKind.SESSION_OPENED, task_id=tid, agent_id="codex")
    )
    await audit.record(
        Event(kind=EventKind.HANDOFF_INITIATED, task_id=tid, agent_id="hermes")
    )
    s = await svc.summarize()
    assert "codex" in s.last_agent_activity
    assert "hermes" in s.last_agent_activity


@pytest.mark.asyncio
async def test_unfinished_ordered_by_recency(tmp_path: Path) -> None:
    svc, _, audit = _build(tmp_path)
    for i, goal in enumerate(["old task", "newer task", "newest task"]):
        tid = uuid4()
        ev = Event(
            kind=EventKind.TASK_CREATED, task_id=tid, payload={"goal": goal}
        )
        # Backdate so ordering is visible.
        ev.timestamp = now() - timedelta(days=3 - i)
        await audit.record(ev)

    s = await svc.summarize()
    assert len(s.unfinished_tasks) == 3
    goals = [t.goal for t in s.unfinished_tasks]
    assert goals == ["newest task", "newer task", "old task"]


@pytest.mark.asyncio
async def test_suggested_prompts_always_include_new_task(tmp_path: Path) -> None:
    svc, _, _ = _build(tmp_path)
    s = await svc.summarize()
    assert "Start a new task" in s.suggested_prompts


@pytest.mark.asyncio
async def test_to_dict_is_json_shape(tmp_path: Path) -> None:
    svc, _, audit = _build(tmp_path)
    tid = uuid4()
    await audit.record(
        Event(kind=EventKind.TASK_CREATED, task_id=tid, payload={"goal": "x"})
    )
    s = await svc.summarize(agent_id="hermes")
    d = s.to_dict()
    # Serializable + round-trips cleanly.
    payload = json.dumps(d)
    back = json.loads(payload)
    assert back["agent_id"] == "hermes"
    assert back["unfinished_tasks"][0]["goal"] == "x"
