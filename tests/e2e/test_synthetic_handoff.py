"""Synthetic cross-agent handoff e2e.

Closes gap-analysis Check 1 / failure mode 2 in CLAUDE-PLAN.MD:
  "Handoff schema designed on paper in Phase 0 but never stress-tested
   against a real cross-agent flow → by Phase 5 it's wrong."

Two MockAgents share a bus + memory. Agent A does work, writes session +
durable memory, produces a handoff bundle. Agent B accepts the bundle,
hydrates memory, and sees everything A recorded durably. The bundle itself
round-trips Pydantic → JSON → Pydantic with zero field-loss.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exocortex.agents.mock import MockAgent
from exocortex.contracts import (
    EventKind,
    Handoff,
    SessionStatus,
    TaskStatus,
)
from exocortex.core.events import EventBus
from exocortex.core.session_manager import SessionManager
from exocortex.core.task_manager import TaskManager
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.memory.session import SessionMemoryStore
from exocortex.memory.summarizer import TruncatingSummarizer
from exocortex.observability.audit import AuditLog
from exocortex.policy.engine import PolicyEngine


async def _setup(tmp_path: Path) -> tuple[EventBus, AuditLog, DurableMemoryStore]:
    audit = AuditLog(tmp_path / "audit.jsonl")
    bus = EventBus(PolicyEngine())
    bus.set_audit_sink(audit.record)
    store = DurableMemoryStore(tmp_path / "mem.db")
    return bus, audit, store


def _make_agent(
    agent_id: str, bus: EventBus, store: DurableMemoryStore
) -> MockAgent:
    return MockAgent(
        agent_id=agent_id,
        event_bus=bus,
        session_memory=SessionMemoryStore(),
        durable_memory=store,
        embedder=DeterministicEmbeddingProvider(),
        summarizer=TruncatingSummarizer(),
    )


@pytest.mark.asyncio
async def test_synthetic_cross_agent_handoff(tmp_path: Path) -> None:
    bus, audit, store = await _setup(tmp_path)
    tasks = TaskManager(bus)
    sessions = SessionManager(bus)

    agent_a = _make_agent("codex", bus, store)
    agent_b = _make_agent("claude_code", bus, store)

    # --- Agent A works the task ---
    task = await tasks.create(goal="Build memory summarizer")
    await tasks.transition(task.id, TaskStatus.ROUTED)
    await tasks.transition(task.id, TaskStatus.IN_PROGRESS)

    session_a = await sessions.open(task.id, agent_id="codex")

    # Durable observations — carry across the handoff.
    await agent_a.record_observation(
        session_a, "Chose sqlite-vec deferred; using pure-python cosine.", durable=True
    )
    await agent_a.record_observation(
        session_a, "Schema v1 locked with provenance columns required.", durable=True
    )
    # Session-only scratch — compressed into the digest, not rehydrated.
    await agent_a.record_observation(session_a, "draft note: check FTS tokenizer")
    await agent_a.record_observation(session_a, "draft note: verify ttl eviction path")

    agent_a.note_decision(
        summary="Defer sqlite-vec; pure-python cosine is fast enough at MVP scale.",
        rationale="Avoids a native extension dep and keeps Phase 2 portable.",
    )
    agent_a.raise_question("Should summarizer also compact durable memory, or only session?")

    handoff, digest = await agent_a.produce_handoff(
        task=task,
        session=session_a,
        to_agent="claude_code",
        sequence_no=1,
        expected_output="Passing summarizer tests; handoff-digest round-trips.",
        digest_char_budget=500,
    )

    # --- Handoff bundle integrity ---
    payload = handoff.model_dump_json()
    restored = Handoff.model_validate_json(payload)
    assert restored == handoff, "handoff bundle lost fidelity through JSON"
    assert restored.model_dump_json() == payload

    assert len(digest) <= 500
    assert handoff.constraints_active == list(task.constraints)
    assert handoff.expected_output
    assert handoff.from_agent == "codex"
    assert handoff.to_agent == "claude_code"
    assert handoff.decisions_so_far[0].summary.startswith("Defer sqlite-vec")
    assert "summarizer also compact" in handoff.open_questions[0]

    await sessions.transition(session_a.id, SessionStatus.ACTIVE)
    await sessions.transition(session_a.id, SessionStatus.HANDING_OFF)
    await sessions.transition(session_a.id, SessionStatus.CLOSED)
    await tasks.transition(task.id, TaskStatus.AWAITING_HANDOFF)

    # --- Agent B picks up the bundle ---
    await tasks.transition(task.id, TaskStatus.IN_PROGRESS)
    session_b = await sessions.open(task.id, agent_id="claude_code")
    loaded = await agent_b.accept_handoff(handoff)

    task_scope_key = f"task:{task.id}"
    assert task_scope_key in loaded
    task_records = loaded[task_scope_key]
    assert len(task_records) == 2  # the two durable observations A wrote
    contents = {r.content for r in task_records}
    assert "Chose sqlite-vec deferred; using pure-python cosine." in contents
    assert "Schema v1 locked with provenance columns required." in contents

    # --- Audit trail has handoff events ---
    events = await audit.read_all()
    kinds = [e.kind for e in events]
    assert EventKind.HANDOFF_INITIATED in kinds
    assert EventKind.HANDOFF_ACCEPTED in kinds

    # B can finish the task
    await tasks.transition(task.id, TaskStatus.COMPLETED)
    final = tasks.get(task.id)
    assert final.status == TaskStatus.COMPLETED
    # Session B is still open — that's fine; lifecycle not required for exit.
    _ = session_b
